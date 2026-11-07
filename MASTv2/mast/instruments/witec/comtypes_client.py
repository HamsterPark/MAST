"""ComtypesBucsClient — WITec Control over COM, the way WITecSDK 1.4.2 does it.

Connection recipe (SDK ``Parameters/COMClient.py``, ``Parameters/__init__.py``; docs §3)::

    comtypes.client.GetModule(({E84FEF4C-…}, 1, 0))                          # type library of the BUCS server
    core = CreateObject(CBUCSCore, interface=IBUCSCore, machine=gethostname())
    access = core.QueryInterface(IBUCSAccess)
    core.GetSubSystemsList(None, 5)  →  [("<type>:<path>", "<default IID>"), …]
    core.GetSubSystemDefaultInterface(path)  →  QueryInterface(IBUCSFloat | IBUCSBool | IBUCSInt | IBUCSString |
                                                              IBUCSEnum | IBUCSTrigger | IBUCSStatusContainer)

What this module adds on top of the vendor recipe, each for a concrete reason:

* **comtypes is imported lazily** — importing this module never imports comtypes, so MAST boots on a machine
  without it and :class:`~mast.instruments.witec.bucs.BucsDependencyMissing` appears only when WITec is used.
* **The generated wrapper cache is redirected before comtypes is imported** into a writable data directory
  (:func:`prepare_comtypes_gen_cache`): the frozen install directory is read-only, and comtypes would otherwise
  fall back to a temp directory or fail.
* **One dedicated COM thread.** COM proxies belong to the apartment that created them; MAST calls drivers from
  whatever job thread happens to run a skill. Every COM call is therefore marshalled onto one thread that owns
  the connection, which also gives a *timeout* (a WITec Control stuck in a modal dialog would otherwise freeze a
  job thread forever) and serialises calls. A call whose caller gave up before it started is dropped, never
  executed late.
* **HRESULT mapping** (:func:`map_com_error`): WITec not running → ``BucsNotRunning``; type library / interface
  not registered (the registration is 32-bit only, docs trap 6) → ``BucsTypeLibMissing`` *with the advice not to
  touch the registry*; RPC unavailable / "No callback function pointer registered" → ``BucsConnectionLost``.

**Not tested on hardware** — the 64-bit question (docs trap 6) is exactly what the first run on the WITec PC
answers; use ``scripts/witec/probe_witec_com.py`` (read-only) first.
"""

from __future__ import annotations

import contextlib
import gc
import importlib
import logging
import os
import queue
import socket
import sys
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from mast.instruments.witec import params as P
from mast.instruments.witec.bucs import (BucsClient, BucsConnectionLost, BucsDependencyMissing, BucsError,
                                          BucsNoValue, BucsNoWriteAccess, BucsNotRunning,
                                          BucsParameterMissing, BucsTimeout, BucsTypeLibMissing,
                                          BucsValueError, ParamInfo)

logger = logging.getLogger(__name__)

__all__ = ["ComtypesBucsClient", "ComtypesBundle", "HRESULT", "hresult_of", "com_text", "map_com_error",
           "default_gen_dir", "prepare_comtypes_gen_cache"]


class HRESULT:
    """The failure codes that matter here, as the *signed* ints Python reports them as."""

    REGDB_E_CLASSNOTREG = -2147221164        # 0x80040154
    REGDB_E_IIDNOTREG = -2147221163          # 0x80040155  "Interface not registered"
    E_ACCESSDENIED = -2147024891             # 0x80070005
    E_NOTIMPL = -2147467263                  # 0x80004001
    E_NOINTERFACE = -2147467262              # 0x80004002
    E_FAIL = -2147467259                     # 0x80004005
    TYPE_E_LIBNOTREGISTERED = -2147319779    # 0x8002801D
    RPC_S_SERVER_UNAVAILABLE = -2147023174   # 0x800706BA
    RPC_S_CALL_FAILED = -2147023170          # 0x800706BE
    RPC_S_CALL_FAILED_DNE = -2147023169      # 0x800706BF
    RPC_E_DISCONNECTED = -2147417848         # 0x80010108
    RPC_E_SERVER_DIED = -2147418105          # 0x80010007
    RPC_E_SERVER_DIED_DNE = -2147418094      # 0x80010012
    RPC_E_WRONG_THREAD = -2147417842         # 0x8001010E
    CO_E_NOTINITIALIZED = -2147221008        # 0x800401F0


_CONNECTION_LOST = frozenset({
    HRESULT.RPC_S_SERVER_UNAVAILABLE, HRESULT.RPC_S_CALL_FAILED, HRESULT.RPC_S_CALL_FAILED_DNE,
    HRESULT.RPC_E_DISCONNECTED, HRESULT.RPC_E_SERVER_DIED, HRESULT.RPC_E_SERVER_DIED_DNE,
})

_BRIDGE_HINT = (
    "The BUCS type library / interfaces are registered for 32-bit clients only (docs trap 6), so this 64-bit "
    "process cannot reach them. Do NOT edit the registry. The fallback is a 32-bit bridge process "
    "(not implemented here — docs §11); run scripts/witec/probe_witec_com.py to see the exact failure.")


def _signed(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v >= 0x80000000 else v


def hresult_of(exc: BaseException) -> int | None:
    """The HRESULT carried by a ``COMError`` (``.hresult``) or an ``OSError`` (``.winerror``), signed; else ``None``."""
    for attr in ("hresult", "winerror"):
        v = getattr(exc, attr, None)
        if isinstance(v, int) and not isinstance(v, bool):
            return _signed(v)
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int) and not isinstance(args[0], bool) and abs(args[0]) > 0xFFFF:
        return _signed(args[0])
    return None


def com_text(exc: BaseException) -> str:
    """All human text a COM error carries: ``.text``, the ISupportErrorInfo description and source, ``str()``."""
    bits: list[str] = []
    t = getattr(exc, "text", None)
    if isinstance(t, str):
        bits.append(t)
    details = getattr(exc, "details", None)
    if isinstance(details, (tuple, list)):
        bits += [d for d in details[:2] if isinstance(d, str)]
    s = getattr(exc, "strerror", None)
    if isinstance(s, str):
        bits.append(s)
    bits.append(str(exc))
    out: list[str] = []
    for b in bits:
        if b and b not in out:
            out.append(b)
    return " | ".join(out)


def map_com_error(exc: BaseException, *, phase: str = "call", path: str = "") -> BucsError:
    """Translate a COM / OS error into the :class:`BucsError` family.

    *phase* disambiguates codes that mean different things: ``connect`` (CreateObject), ``typelib``
    (GetModule / first QueryInterface), ``table``, ``read``, ``status`` (status container), ``write``,
    ``trigger``, ``access``. ``E_ACCESSDENIED`` is "WITec Control is not running" at connect (the SDK's own
    reading), "no sequencer active" on a status read, and "no write access" on a write.
    """
    if isinstance(exc, BucsError):
        return exc
    hr = hresult_of(exc)
    text = com_text(exc)
    low = text.casefold()
    where = f" [{path}]" if path else ""
    code = f"0x{hr & 0xFFFFFFFF:08X}" if hr is not None else "no HRESULT"

    if (hr in (HRESULT.TYPE_E_LIBNOTREGISTERED, HRESULT.REGDB_E_IIDNOTREG)
            or "interface not registered" in low or "library not registered" in low
            or (hr == HRESULT.E_NOINTERFACE and phase in ("connect", "typelib"))):
        return BucsTypeLibMissing(f"WITec COM registration problem ({code}): {text}. {_BRIDGE_HINT}")
    if phase == "connect" and hr in (HRESULT.REGDB_E_CLASSNOTREG, HRESULT.E_ACCESSDENIED,
                                     HRESULT.RPC_S_SERVER_UNAVAILABLE):
        return BucsNotRunning(
            f"WITec Control is not running or not reachable ({code}: {text}). COM cannot start it — "
            "a person has to open WITec Control first.")
    if hr in _CONNECTION_LOST:
        return BucsConnectionLost(
            f"connection to WITec Control lost ({code}){where}: {text}. Reconnect; a person must press "
            "'COM Automation' again before there is write access.")
    if hr == HRESULT.E_FAIL and "no callback function pointer registered" in low:
        return BucsConnectionLost(f"WITec Control is disconnecting{where}: {text}")
    if "invalid class-id" in low or "guid_null" in low:
        return BucsParameterMissing(f"no such parameter on this WITec Control{where}: {text}")
    if hr == HRESULT.E_ACCESSDENIED:
        if phase in ("write", "trigger"):
            return BucsNoWriteAccess(f"WITec Control refused the write{where} ({code}): {text}. A person "
                                     "must press 'COM Automation' first.")
        return BucsNoValue(f"no value right now{where} (E_ACCESSDENIED — e.g. no sequencer is active, or remote "
                           f"read access is denied in WITec Control): {text}")
    if hr == HRESULT.RPC_E_WRONG_THREAD:
        return BucsError(f"COM call from the wrong thread{where} ({code}) — this is a bug in the client")
    if hr == HRESULT.CO_E_NOTINITIALIZED:
        return BucsError(f"COM is not initialised on the calling thread{where} ({code}) — bug in the client")
    return BucsError(f"WITec COM error {code}{where}: {text}")


# ── the comtypes generated-code cache ───────────────────────────────────────────────


def default_gen_dir() -> Path:
    """Where comtypes may write the generated ``BasicUniversalCOMServerLib`` wrapper: a directory under the
    MAST data root (never the possibly read-only install tree). Override with ``MAST_COMTYPES_GEN_DIR``."""
    env = os.environ.get("MAST_COMTYPES_GEN_DIR", "").strip()
    if env:
        return Path(env)
    from mast._runtime_paths import project_root

    return project_root() / "artifacts" / "witec" / "comtypes_gen"


def prepare_comtypes_gen_cache(gen_dir: str | Path | None = None) -> Path:
    """Point comtypes' generated-code package at a writable directory, then import ``comtypes.client``.

    Call it *before* anything imports ``comtypes.client``: comtypes picks its cache directory at that
    import (``comtypes.client.gen_dir``). We pre-create the ``comtypes.gen`` package pointing at *gen_dir* —
    the same mechanism comtypes itself uses for its memory-only fallback — so its choice is ours. If
    ``comtypes.client`` was already imported by somebody else, ``gen_dir`` and ``comtypes.gen.__path__`` are
    redirected afterwards. Returns the directory.
    """
    cache = Path(gen_dir) if gen_dir else default_gen_dir()
    try:
        cache.mkdir(parents=True, exist_ok=True)
        probe = cache / ".write-test"
        probe.write_text("x", encoding="ascii")
        probe.unlink()
    except OSError as exc:
        raise BucsError(f"comtypes cache directory {cache} is not writable: {exc}") from exc
    try:
        import comtypes
    except ImportError as exc:
        raise BucsDependencyMissing(
            "comtypes is not importable: install it, or point PYTHONPATH at a directory containing it "
            f"(it is pure Python; the .whl can simply be unzipped). {exc}") from exc
    gen = sys.modules.get("comtypes.gen")
    if gen is None:
        gen = types.ModuleType("comtypes.gen")
        gen.__doc__ = "comtypes.gen — generated COM wrappers (MAST: written to a data directory)"
        gen.__path__ = [str(cache)]                            # type: ignore[attr-defined]
        sys.modules["comtypes.gen"] = gen
        comtypes.gen = gen                                     # type: ignore[attr-defined]
    elif str(cache) not in [str(p) for p in list(gen.__path__)]:
        gen.__path__ = [str(cache)] + list(gen.__path__)       # type: ignore[attr-defined]
    try:
        import comtypes.client as cc
    except ImportError as exc:
        raise BucsDependencyMissing(f"comtypes.client is not importable: {exc}") from exc
    cc.gen_dir = str(cache)
    return cache


@dataclass
class ComtypesBundle:
    """What the client needs from comtypes; tests hand in a fake."""

    get_module: Callable[..., Any]
    create_object: Callable[..., Any]
    import_lib: Callable[[], Any]
    co_initialize: Callable[[], Any]
    co_uninitialize: Callable[[], Any]


def _default_bundle(gen_dir: str | Path | None) -> ComtypesBundle:
    prepare_comtypes_gen_cache(gen_dir)
    import comtypes
    import comtypes.client as cc

    return ComtypesBundle(
        get_module=cc.GetModule, create_object=cc.CreateObject,
        import_lib=lambda: importlib.import_module("comtypes.gen.BasicUniversalCOMServerLib"),
        co_initialize=comtypes.CoInitializeEx, co_uninitialize=comtypes.CoUninitialize)


# ── one thread owns the COM connection ─────────────────────────────────────────────


class _Task:
    __slots__ = ("fn", "done", "lock", "started", "cancelled", "result", "exc")

    def __init__(self, fn: Callable[[], Any]):
        self.fn = fn
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.started = False
        self.cancelled = False
        self.result: Any = None
        self.exc: BaseException | None = None


_STOP = object()


class _ComThread:
    """A worker thread that runs callables one at a time. Callers wait with a timeout; a call whose caller
    already gave up and that has not started is skipped (a late write would be worse than a failed one)."""

    def __init__(self, name: str, on_exit: Callable[[], None] | None = None):
        self._q: queue.Queue = queue.Queue()
        self._on_exit = on_exit
        self._running_since: float | None = None
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def thread_id(self) -> int | None:
        return self._thread.ident

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        while True:
            task = self._q.get()
            if task is _STOP:
                break
            with task.lock:
                if task.cancelled:
                    task.done.set()
                    continue
                task.started = True
            self._running_since = time.monotonic()
            try:
                task.result = task.fn()
            except BaseException as exc:  # noqa: BLE001 - handed to the caller
                task.exc = exc
            finally:
                self._running_since = None
                task.done.set()
        if self._on_exit is not None:
            try:
                self._on_exit()
            except Exception:  # noqa: BLE001
                logger.debug("COM thread exit hook failed", exc_info=True)

    def running_for(self) -> float | None:
        """Seconds the call in progress has been running, or ``None`` when idle."""
        since = self._running_since
        return None if since is None else time.monotonic() - since

    def call(self, fn: Callable[[], Any], timeout: float) -> Any:
        if not self.alive:
            raise BucsConnectionLost("the WITec COM thread is not running")
        stuck = self.running_for()
        if stuck is not None and stuck > timeout:
            # nothing queued behind a call that has already outlived this caller's patience can start in time:
            # fail at once instead of piling up callers that each wait their full timeout
            raise BucsTimeout(f"WITec Control has not answered a previous call for {stuck:.0f} s (longer than "
                              f"the {timeout:.0f} s allowed here) — is a dialog open on the WITec PC? "
                              "This call was NOT executed")
        task = _Task(fn)
        self._q.put(task)
        if not task.done.wait(timeout):
            with task.lock:
                if not task.started:
                    task.cancelled = True
                    raise BucsTimeout(
                        f"WITec Control did not take the call within {timeout:.0f} s (a previous call is "
                        "still running); this call was NOT executed")
            raise BucsTimeout(
                f"WITec Control did not answer within {timeout:.0f} s — is a dialog open on the WITec PC? "
                "The call was sent; whether it took effect is unknown")
        if task.exc is not None:
            raise task.exc
        return task.result

    def stop(self, join_s: float = 5.0) -> None:
        self._q.put(_STOP)
        if threading.current_thread() is not self._thread:
            self._thread.join(join_s)


@dataclass
class _Bound:
    """The interfaces of one parameter (all live on the COM thread only)."""

    mod: Any
    sub: Any
    lim: Any = None
    mm: Any = None


def _norm_iid(iid: Any) -> str:
    return str(iid).strip().strip("{}").upper()


class ComtypesBucsClient(BucsClient):
    """:class:`~mast.instruments.witec.bucs.BucsClient` over comtypes. See the module docstring."""

    def __init__(self, *, machine: str | None = None, call_timeout_s: float = 30.0,
                 connect_timeout_s: float = 180.0, gen_dir: str | Path | None = None,
                 bundle_factory: Callable[[], ComtypesBundle] | None = None, name: str = "witec-com"):
        super().__init__()
        self.machine = machine
        self.call_timeout_s = float(call_timeout_s)
        self.connect_timeout_s = float(connect_timeout_s)
        self._gen_dir = gen_dir
        self._bundle_factory = bundle_factory
        self._name = name
        self._actor: _ComThread | None = None
        self._actor_lock = threading.Lock()
        self._connected = False
        self._tl = threading.local()               # per-thread call-timeout override
        # only ever touched on the COM thread -------------------------------------------
        self._bundle: ComtypesBundle | None = None
        self._com_inited = False
        self._lib: Any = None
        self._core: Any = None
        self._access: Any = None
        self._bound: dict[str, _Bound] = {}

    # -- plumbing --------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    def _lock_wait_s(self) -> float:
        override = getattr(self._tl, "timeout", None)
        return float(override) if override is not None else self.call_timeout_s

    def _ensure_actor(self) -> _ComThread:
        with self._actor_lock:                      # never the table lock: that one may be held through a slow connect
            if self._actor is None or not self._actor.alive:
                self._actor = _ComThread(f"{self._name}-com", on_exit=self._com_thread_exit)
            return self._actor

    def _com_thread_exit(self) -> None:           # runs on the COM thread
        self._drop_connection()
        if self._bundle is not None and self._com_inited:
            try:
                self._bundle.co_uninitialize()
            except Exception:  # noqa: BLE001
                pass
            self._com_inited = False

    def _drop_connection(self) -> None:           # COM thread
        self._bound = {}
        self._access = None
        self._core = None
        self._lib = None
        gc.collect()

    @staticmethod
    def _looks_like_com(exc: BaseException) -> bool:
        return hresult_of(exc) is not None or isinstance(exc, OSError)

    @contextlib.contextmanager
    def call_timeout(self, seconds: float) -> Iterator[None]:
        prev = getattr(self._tl, "timeout", None)
        self._tl.timeout = float(seconds)
        try:
            yield
        finally:
            self._tl.timeout = prev

    def _run(self, fn: Callable[[], Any], *, phase: str, path: str = "",
             timeout: float | None = None) -> Any:
        actor = self._ensure_actor()
        override = getattr(self._tl, "timeout", None)
        try:
            return actor.call(fn, override if override is not None
                              else (timeout if timeout is not None else self.call_timeout_s))
        except BucsError as exc:
            self._note(exc)
            raise
        except BaseException as exc:
            if not self._looks_like_com(exc):
                raise                              # a bug in our code: keep its traceback and type
            mapped = map_com_error(exc, phase=phase, path=path)
            self._note(mapped)
            raise mapped from exc

    def _note(self, exc: BucsError) -> None:
        if isinstance(exc, (BucsConnectionLost, BucsNotRunning, BucsTypeLibMissing)):
            self._connected = False

    # -- lifecycle -------------------------------------------------------------------

    def _connect_raw(self) -> None:
        self._run(self._connect_on_com_thread, phase="connect", timeout=self.connect_timeout_s)
        self._connected = True

    def _connect_on_com_thread(self) -> None:
        self._drop_connection()
        if self._bundle is None:
            factory = self._bundle_factory or (lambda: _default_bundle(self._gen_dir))
            self._bundle = factory()
        b = self._bundle
        if not self._com_inited:
            try:
                b.co_initialize()
            except Exception as exc:  # noqa: BLE001
                if hresult_of(exc) != -2147417850:        # RPC_E_CHANGED_MODE: already initialised another way
                    raise
            self._com_inited = True
        try:
            b.get_module((P.TYPELIB_ID,) + tuple(P.TYPELIB_VERSION))
        except Exception as exc:  # noqa: BLE001
            raise map_com_error(exc, phase="typelib") from exc
        try:
            lib = b.import_lib()
        except ImportError as exc:
            raise BucsTypeLibMissing(f"the generated BasicUniversalCOMServerLib wrapper cannot be imported "
                                     f"({exc}). {_BRIDGE_HINT}") from exc
        try:
            core = b.create_object(lib.CBUCSCore, interface=lib.IBUCSCore,
                                   machine=self.machine or socket.gethostname())
        except Exception as exc:  # noqa: BLE001
            raise map_com_error(exc, phase="connect") from exc
        if core is None:
            raise BucsNotRunning("CreateObject(CBUCSCore) returned nothing — WITec Control is not running")
        try:
            access = core.QueryInterface(lib.IBUCSAccess)
        except Exception as exc:  # noqa: BLE001
            raise map_com_error(exc, phase="typelib") from exc
        self._lib, self._core, self._access = lib, core, access

    def _close_raw(self) -> None:
        actor = self._actor
        if actor is None:
            self._connected = False
            return
        busy = actor.running_for()
        if self._connected and actor.alive and (busy is None or busy < 1.0):    # a stuck thread cannot release politely
            try:
                actor.call(self._release_on_com_thread, 2.0)
            except BaseException:  # noqa: BLE001 - best effort
                pass
        self._connected = False
        self._actor = None
        actor.stop(1.5)

    def _release_on_com_thread(self) -> None:
        if self._access is None:
            return
        try:
            if self._access.HasWriteAccess():
                self._access.RequestWriteAccess(False)
        except Exception:  # noqa: BLE001 - the connection may already be gone
            pass

    # -- access ----------------------------------------------------------------------

    def _has_write_access_raw(self) -> bool:
        return bool(self._run(lambda: self._access.HasWriteAccess(), phase="access"))

    def _has_read_access_raw(self) -> bool:
        return bool(self._run(lambda: self._access.HasReadAccess(), phase="access"))

    def _request_write_access_raw(self, on: bool) -> None:
        self._run(lambda: self._access.RequestWriteAccess(bool(on)), phase="access")

    # -- parameter table -----------------------------------------------------------------

    def _load_table(self) -> dict[str, ParamInfo]:
        return self._run(self._load_table_on_com_thread, phase="table", timeout=self.connect_timeout_s)

    def _load_table_on_com_thread(self) -> dict[str, ParamInfo]:
        lst = self._core.GetSubSystemsList(None, 5)           # None = from the root, depth 5, "unsorted"
        out: dict[str, ParamInfo] = {}
        for i in range(int(lst.GetNumberOfSystems())):
            name, iid = lst.GetSubSystemNameAndIId(i)
            iid_n = _norm_iid(iid)
            if iid_n.startswith("000"):                       # a pure grouping node, not an object
                continue
            kind = P.IID_KIND.get(iid_n)
            if kind is None:
                continue
            type_str, _, path = str(name).partition(":")
            out[path] = ParamInfo(path, kind, type_str.strip().casefold(), iid_n)
        return out

    def _bind(self, info: ParamInfo) -> _Bound:               # COM thread
        b = self._bound.get(info.path)
        if b is not None:
            return b
        lib = self._lib
        try:
            unk = self._core.GetSubSystemDefaultInterface(info.path)
        except Exception as exc:  # noqa: BLE001
            raise map_com_error(exc, phase="table", path=info.path) from exc
        iface = {"int": "IBUCSInt", "float": "IBUCSFloat", "bool": "IBUCSBool", "string": "IBUCSString",
                 "enum": "IBUCSEnum", "trigger": "IBUCSTrigger",
                 "status": "IBUCSStatusContainer"}[info.kind]
        mod = unk.QueryInterface(getattr(lib, iface))
        sub = mod.QueryInterface(lib.IBUCSSubSystemInfo)
        lim = mm = None
        if info.kind in ("int", "float"):
            lim = mod.QueryInterface(lib.IBUCSSingleValueHasLimits)
            mm = mod.QueryInterface(getattr(lib, "IBUCSIntMinMaxValues" if info.kind == "int"
                                            else "IBUCSFloatMinMaxValues"))
        b = self._bound[info.path] = _Bound(mod, sub, lim, mm)
        return b

    # -- reads ------------------------------------------------------------------------------

    def _get_raw(self, info: ParamInfo) -> Any:
        def work() -> Any:
            b = self._bind(info)
            if info.kind == "status":
                b.mod.Update()
                return self._status_value(b.mod, info)
            if info.kind == "enum":
                return int(b.mod.GetValue()[1])               # (label, index)
            return b.mod.GetValue()

        return self._run(work, phase="status" if info.kind == "status" else "read", path=info.path)

    @staticmethod
    def _status_value(mod: Any, info: ParamInfo) -> Any:
        d = info.dtype
        fn = {"bool": "GetSingleValueAsInt", "int": "GetSingleValueAsInt",
              "float": "GetSingleValueAsDouble", "string": "GetSingleValueAsString"}.get(d)
        if fn is None:
            raise BucsValueError(f"{info.path}: status data type {d!r} is not readable by this client")
        res = getattr(mod, fn)()
        if isinstance(res, tuple):
            *vals, ok = res
            value = vals[0] if vals else None
        else:
            value, ok = res, True
        if not ok:
            raise BucsError(f"reading status {info.path} was not successful")
        return bool(value) if d == "bool" else value

    def _get_enum_raw(self, info: ParamInfo) -> tuple[int, str]:
        def work() -> tuple[int, str]:
            label, idx = self._bind(info).mod.GetValue()
            return int(idx), str(label)

        return self._run(work, phase="read", path=info.path)

    def _enum_values_raw(self, info: ParamInfo) -> dict[int, str]:
        def work() -> dict[int, str]:
            index, strings, n = self._bind(info).mod.GetAvailableValues()
            if index is None or strings is None:
                return {}
            return {int(index[i]): str(strings[i]) for i in range(int(n))}

        return self._run(work, phase="read", path=info.path)

    def _range_of(self, info: ParamInfo) -> tuple[float | None, float | None]:
        if info.kind not in ("int", "float"):
            return (None, None)

        def work() -> tuple[float | None, float | None]:
            b = self._bind(info)
            lo = b.mm.GetMinimum() if b.lim.HasMinimum() else None
            hi = b.mm.GetMaximum() if b.lim.HasMaximum() else None
            return lo, hi

        return self._run(work, phase="read", path=info.path)

    def _is_enabled(self, info: ParamInfo) -> bool:
        return bool(self._run(lambda: self._bind(info).sub.GetEnabled(), phase="read", path=info.path))

    # -- writes (already guarded by the base class) ---------------------------------------------

    def _set_raw(self, info: ParamInfo, value: Any) -> None:
        def work() -> None:
            mod = self._bind(info).mod
            if info.kind == "enum":
                res = mod.SetValueString(value) if isinstance(value, str) else mod.SetValueNumeric(int(value))
            else:
                res = mod.SetValue(value)
            if res is False:
                raise BucsError(f"WITec Control refused to set {info.path} to {value!r}")

        self._run(work, phase="write", path=info.path)

    def _trigger_raw(self, info: ParamInfo) -> None:
        def work() -> None:
            if self._bind(info).mod.OperateTrigger() is False:
                raise BucsError(f"WITec Control did not start the action {info.path}")

        self._run(work, phase="trigger", path=info.path)
