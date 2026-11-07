"""A COM-shaped world over a FakeBucsClient, to test ComtypesBucsClient without comtypes or Windows COM.

``ComWorld(backend).bundle()`` is what ``ComtypesBucsClient(bundle_factory=...)`` takes. The objects it hands
out behave like comtypes' pointers to WITec's BUCS server:

* methods named like the IBUCS* interfaces, out-parameters returned as tuples (comtypes style);
* **thread affinity**: every call from a thread other than the one that created the core object raises
  ``RPC_E_WRONG_THREAD`` — so a client that forgets to funnel COM through its own thread fails the test;
* ``QueryInterface`` is checked against the parameter's kind (asking for IBUCSFloat on a trigger is E_NOINTERFACE);
* the server does not guard anything: writes go straight to the backend if write access is held, which is
  exactly why the client's own guard must hold (``world.writes`` is what actually reached "WITec").
"""

from __future__ import annotations

import threading
from typing import Any

from mast.instruments.witec import params as P
from mast.instruments.witec.bucs import BucsConnectionLost, BucsNoValue, BucsNotRunning
from mast.instruments.witec.comtypes_client import ComtypesBundle, HRESULT

try:                                     # the real class on Windows, a look-alike elsewhere
    from _ctypes import COMError
except ImportError:                      # pragma: no cover
    class COMError(Exception):
        def __init__(self, hresult, text, details):
            super().__init__(hresult, text, details)
            self.hresult, self.text, self.details = hresult, text, details


def com_error(hresult: int, text: str = "", description: str | None = None) -> Exception:
    return COMError(hresult, text, (description or text, "WITec.COMAutomation", None, 0, None))


def os_error(winerror: int, text: str = "") -> OSError:
    exc = OSError(0, text, None, winerror)
    if getattr(exc, "winerror", None) != winerror:          # pragma: no cover - non-Windows
        exc.winerror = winerror                              # type: ignore[attr-defined]
    return exc


_IFACE_FOR_KIND = {"int": "IBUCSInt", "float": "IBUCSFloat", "bool": "IBUCSBool", "string": "IBUCSString",
                   "enum": "IBUCSEnum", "trigger": "IBUCSTrigger", "status": "IBUCSStatusContainer"}
_IID_FOR_KIND = {k: P.INTERFACE_IIDS[v] for k, v in _IFACE_FOR_KIND.items()}


class FakeLib:
    """Stands for ``comtypes.gen.BasicUniversalCOMServerLib``."""

    def __init__(self):
        for name in ("CBUCSCore", "IBUCSCore", "IBUCSAccess", "IBUCSSubSystemInfo", "IBUCSSingleValue",
                     "IBUCSSingleValueHasLimits", "IBUCSInt", "IBUCSIntMinMaxValues", "IBUCSFloat",
                     "IBUCSFloatMinMaxValues", "IBUCSString", "IBUCSBool", "IBUCSEnum", "IBUCSTrigger",
                     "IBUCSStatusContainer"):
            setattr(self, name, type(name, (), {}))


class ComWorld:
    def __init__(self, backend, *, enforce_affinity: bool = True):
        self.backend = backend
        self.lib = FakeLib()
        self.enforce_affinity = enforce_affinity
        self.calls: list[tuple[str, int]] = []
        self.writes: list[tuple[str, str, Any]] = []        # what actually reached "WITec": (op, path, value)
        self.qi_log: list[tuple[str, str]] = []
        self.get_module_args: list[Any] = []
        self.create_args: list[dict] = []
        self.owner_thread: int | None = None
        self.fail_get_module: Exception | None = None
        self.fail_create: Exception | None = None
        self.fail_table: Exception | None = None
        self.fail_next: list[Exception] = []
        self.hang_event: threading.Event | None = None        # set to make the next call block until released
        self.init_calls = 0
        self.uninit_calls = 0
        self.server_alive = True

    # -- bundle -----------------------------------------------------------------------

    def bundle(self) -> ComtypesBundle:
        def get_module(tlib):
            self.get_module_args.append(tlib)
            if self.fail_get_module is not None:
                raise self.fail_get_module

        def create_object(cls, interface=None, machine=None, **kw):
            self.create_args.append({"cls": cls, "interface": interface, "machine": machine, **kw})
            if self.fail_create is not None:
                raise self.fail_create
            if not self.backend.running:
                raise os_error(HRESULT.REGDB_E_CLASSNOTREG, "Class not registered")
            self.backend.connect()
            self.server_alive = True
            self.owner_thread = threading.get_ident()
            return _Core(self)

        def co_init():
            self.init_calls += 1

        def co_uninit():
            self.uninit_calls += 1

        return ComtypesBundle(get_module=get_module, create_object=create_object,
                              import_lib=lambda: self.lib, co_initialize=co_init, co_uninitialize=co_uninit)

    # -- common entry for every COM method ---------------------------------------------------------

    def enter(self, method: str) -> None:
        self.calls.append((method, threading.get_ident()))
        if self.enforce_affinity and self.owner_thread is not None \
                and threading.get_ident() != self.owner_thread:
            raise com_error(HRESULT.RPC_E_WRONG_THREAD, "The application called an interface that was "
                                                        "marshalled for a different thread.")
        if self.hang_event is not None:
            ev, self.hang_event = self.hang_event, None
            ev.wait(30)
        if self.fail_next:
            raise self.fail_next.pop(0)
        if not self.backend.running or not self.server_alive:
            raise com_error(HRESULT.RPC_S_SERVER_UNAVAILABLE, "The RPC server is unavailable.")

    def translate(self, exc: Exception) -> Exception:
        if isinstance(exc, BucsNoValue):
            return com_error(HRESULT.E_ACCESSDENIED, "Access is denied.")
        if isinstance(exc, (BucsConnectionLost, BucsNotRunning)):
            return com_error(HRESULT.RPC_S_SERVER_UNAVAILABLE, "The RPC server is unavailable.")
        return exc


class _Core:
    def __init__(self, world: ComWorld):
        self.w = world

    def QueryInterface(self, iface):
        self.w.enter("Core.QueryInterface")
        self.w.qi_log.append(("<core>", iface.__name__))
        return _Access(self.w)

    def GetSubSystemsList(self, name, depth):
        self.w.enter("GetSubSystemsList")
        assert name is None and depth == 5, (name, depth)
        if self.w.fail_table is not None:
            raise self.w.fail_table
        return _SubList(self.w)

    def GetSubSystemDefaultInterface(self, path):
        self.w.enter("GetSubSystemDefaultInterface")
        if path not in self.w.backend._tree:
            raise com_error(HRESULT.E_FAIL, "Unspecified error",
                            f"Invalid Class-ID (GUID_NULL) for Sub-System with name: {path}")
        return _Unk(self.w, path)


class _Access:
    def __init__(self, w: ComWorld):
        self.w = w

    def HasWriteAccess(self):
        self.w.enter("HasWriteAccess")
        return self.w.backend._has_write_access_raw()

    def HasReadAccess(self):
        self.w.enter("HasReadAccess")
        return True

    def RequestWriteAccess(self, on):
        self.w.enter("RequestWriteAccess")
        self.w.writes.append(("request_write", "", bool(on)))
        self.w.backend._request_write_access_raw(bool(on))
        return self.w.backend._has_write_access_raw()


class _SubList:
    def __init__(self, w: ComWorld):
        self.w = w
        self.items = []
        for path, f in w.backend._tree.items():
            dtype = f.dtype
            self.items.append((f"{dtype.capitalize()}:{path}", _IID_FOR_KIND[f.kind]))
        self.items.append(("Group:UserParameters|SomeGroup", "00000000-0000-0000-0000-000000000000"))

    def GetNumberOfSystems(self):
        self.w.enter("GetNumberOfSystems")
        return len(self.items)

    def GetSubSystemNameAndIId(self, i):
        self.w.enter("GetSubSystemNameAndIId")
        return self.items[i]


class _Unk:
    def __init__(self, w: ComWorld, path: str):
        self.w, self.path = w, path

    def QueryInterface(self, iface):
        self.w.enter("Unk.QueryInterface")
        self.w.qi_log.append((self.path, iface.__name__))
        return _Mod(self.w, self.path).check(iface)


class _Mod:
    """One object implementing every interface of a parameter; ``check`` mimics QueryInterface's refusal."""

    def __init__(self, w: ComWorld, path: str):
        self.w, self.path = w, path

    @property
    def f(self):
        return self.w.backend._tree[self.path]

    def check(self, iface):
        kind, name = self.f.kind, iface.__name__
        allowed = {_IFACE_FOR_KIND[kind], "IBUCSSubSystemInfo", "IBUCSSingleValue"}
        if kind in ("int", "float"):
            allowed |= {"IBUCSSingleValueHasLimits", "IBUCSIntMinMaxValues" if kind == "int"
                        else "IBUCSFloatMinMaxValues"}
        if name not in allowed:
            raise com_error(HRESULT.E_NOINTERFACE, "No such interface supported")
        return self

    def QueryInterface(self, iface):
        self.w.enter("Mod.QueryInterface")
        self.w.qi_log.append((self.path, iface.__name__))
        return self.check(iface)

    def _info(self):
        return self.w.backend._tbl[self.path] if self.w.backend._tbl else None

    # IBUCSSubSystemInfo
    def GetEnabled(self):
        self.w.enter("GetEnabled")
        return self.w.backend._is_enabled(self.w.backend.describe(self.path))

    def GetName(self):
        self.w.enter("GetName")
        return self.path

    # limits
    def HasMinimum(self):
        self.w.enter("HasMinimum")
        return self.f.lo is not None

    def HasMaximum(self):
        self.w.enter("HasMaximum")
        return self.f.hi is not None

    def GetMinimum(self):
        self.w.enter("GetMinimum")
        return self.f.lo

    def GetMaximum(self):
        self.w.enter("GetMaximum")
        return self.f.hi

    # values
    def _write_ok(self):
        if not self.w.backend._has_write_access_raw():
            raise com_error(HRESULT.E_ACCESSDENIED, "Access is denied.")

    def GetValue(self):
        self.w.enter("GetValue")
        f = self.f
        if f.kind == "enum":
            idx = int(f.getter() if f.getter else f.value)
            return (f.enum[idx], idx)                       # (label, index), comtypes out-param order
        try:
            return self.w.backend._get_raw(self.w.backend.describe(self.path))
        except Exception as exc:  # noqa: BLE001
            raise self.w.translate(exc)

    def SetValue(self, v):
        self.w.enter("SetValue")
        self._write_ok()
        self.w.writes.append(("set", self.path, v))
        self.w.backend._set_raw(self.w.backend.describe(self.path), v)

    def SetValueNumeric(self, v):
        self.w.enter("SetValueNumeric")
        self._write_ok()
        self.w.writes.append(("set", self.path, int(v)))
        self.w.backend._set_raw(self.w.backend.describe(self.path), int(v))

    def SetValueString(self, v):
        self.w.enter("SetValueString")
        self._write_ok()
        self.w.writes.append(("set", self.path, v))
        self.w.backend._set_raw(self.w.backend.describe(self.path), v)

    def GetAvailableValues(self):
        self.w.enter("GetAvailableValues")
        e = self.f.enum or {}
        return (list(e), [e[i] for i in e], len(e))

    def OperateTrigger(self):
        self.w.enter("OperateTrigger")
        self._write_ok()
        self.w.writes.append(("trigger", self.path, None))
        self.w.backend._trigger_raw(self.w.backend.describe(self.path))

    # status container
    def Update(self):
        self.w.enter("Update")

    def _status(self):
        try:
            return self.w.backend._get_raw(self.w.backend.describe(self.path)), True
        except Exception as exc:  # noqa: BLE001
            raise self.w.translate(exc)

    def GetSingleValueAsInt(self):
        self.w.enter("GetSingleValueAsInt")
        v, ok = self._status()
        return (int(v), ok)

    def GetSingleValueAsDouble(self):
        self.w.enter("GetSingleValueAsDouble")
        v, ok = self._status()
        return (float(v), ok)

    def GetSingleValueAsString(self):
        self.w.enter("GetSingleValueAsString")
        v, ok = self._status()
        return (str(v), ok)
