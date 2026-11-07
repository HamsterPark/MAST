"""BucsClient — the replaceable layer between MAST and WITec Control's COM Automation server.

Design contract: ``docs/v2/design/optical_station_witec.md`` §1 (traps), D2, D3, D5.

WITec Control exposes one tree of named parameters (``UserParameters|…``, ``MultiComm|MicroscopeControl|…``,
``Status|…``, ``ApplicationControl|…``) through the "Basic Universal COM Server" (BUCS). This module is the
abstraction every upper layer talks to:

* :class:`BucsClient` — ``get / set / trigger / exists / list_parameters / has_write_access /
  request_write_access / close``. :class:`~mast.instruments.witec.comtypes_client.ComtypesBucsClient`
  speaks real COM; :class:`~mast.instruments.witec.fake_bucs.FakeBucsClient` is an in-memory WITec Control
  (tests, offline demos). If 64-bit comtypes cannot reach the 32-bit-only registration (docs trap 6), a
  bridge-process client slots in here without touching anything above.
* The **write guard** lives in the *concrete template methods* ``set`` / ``trigger`` of the base class, so every
  client — real, fake, future bridge — inherits it and none can forget it. ``ApplicationControl|NewProject``
  (deletes the current project), ``ApplicationControl|ExitApplication`` (closes WITec Control, which COM cannot
  start again) and ``ApplicationControl|LoadConfiguration`` (swaps the whole configuration) are refused for
  ``set`` and ``trigger`` unconditionally — see :data:`~mast.instruments.witec.params.DANGEROUS_PATHS`. The
  guard goes further than the contract in three places, each argued in ``params.py``: the whole
  ``ApplicationControl`` group, the ``COMInterop`` group (the "COM Automation" button itself — write access
  must be *handed over by a person*, docs D5) and ``UserParameters|HardwareConstraints`` (the software Z
  travel limit), plus the stage coordinate re-zero triggers. Reads are never guarded.
* Range, type and write-access checks happen here too (the vendor server "will not check for it"): a value
  outside the range the parameter itself declares is **refused, never clamped**.

Errors are :class:`BucsError` (an :class:`~mast.instruments.base.InstrumentError`). The "WITec is not there"
family also derives from :class:`~mast.instruments.base.InstrumentUnavailable`, which the skill layer already
reports as "optical hardware unavailable".
"""

from __future__ import annotations

import contextlib
import math
import numbers
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterator

from mast.instruments.base import InstrumentError, InstrumentUnavailable
from mast.instruments.microscope import WriteAccessDenied
from mast.instruments.witec import params as P

__all__ = [  # noqa: F822 - FakeBucsClient / VirtualClock are served lazily by the module __getattr__ below
    "BucsError", "BucsNotRunning", "BucsConnectionLost", "BucsTypeLibMissing",
    "BucsDependencyMissing", "BucsTimeout", "BucsParameterMissing", "BucsParameterDisabled",
    "BucsNoWriteAccess", "BucsForbiddenPath", "BucsValueError", "BucsNoValue",
    "KINDS", "ParamInfo", "BucsClient", "ReadOnlyBucs",
    "forbidden_reason", "check_write_path", "check_path_syntax", "FakeBucsClient", "VirtualClock",
]

KINDS = ("int", "float", "bool", "string", "enum", "trigger", "status")


# ── errors ────────────────────────────────────────────────────────────────────────


class BucsError(InstrumentError):
    """Anything that went wrong talking to WITec Control's COM server."""


class BucsNotRunning(BucsError, InstrumentUnavailable):
    """WITec Control is not running (or not reachable). COM cannot start it: a person must open it
    (docs trap 2)."""


class BucsConnectionLost(BucsError, InstrumentUnavailable):
    """WITec Control exited or crashed after we connected. Reconnect with a fresh ``connect()``; a person
    has to press "COM Automation" again before there is write access."""


class BucsTypeLibMissing(BucsError, InstrumentUnavailable):
    """The BUCS type library / interface is not registered for this process (the registration is 32-bit
    only, docs trap 6). Do NOT patch the registry; the fallback is a 32-bit bridge process."""


class BucsDependencyMissing(BucsError, InstrumentUnavailable):
    """The COM binding (``comtypes``) is not importable on this machine."""


class BucsTimeout(BucsError):
    """WITec Control did not answer within the call timeout. The operation may or may not have been applied."""


class BucsParameterMissing(BucsError):
    """No such parameter on this WITec Control configuration."""


class BucsParameterDisabled(BucsError):
    """The parameter exists but is currently disabled in WITec Control."""


class BucsNoWriteAccess(BucsError, WriteAccessDenied):
    """This client does not hold write access (a person must press "COM Automation" in WITec Control)."""


class BucsForbiddenPath(BucsError):
    """The write guard refused this path for ``set`` / ``trigger`` (see :func:`forbidden_reason`)."""


class BucsValueError(BucsError):
    """Wrong type for the parameter, out of the range the parameter declares, or a malformed path."""


class BucsNoValue(BucsError):
    """The parameter exists but has no value right now (``E_ACCESSDENIED`` while no sequencer is active)."""


# ── paths and the write guard ──────────────────────────────────────────────────────

_MAX_PATH_LEN = 512


def check_path_syntax(path: Any) -> str:
    """Return *path* if it is a plausible WITec parameter path, else raise :class:`BucsValueError`.

    ASCII only, no control characters, non-empty ``|``-separated segments. (The server would reject anything
    else anyway; checking up front keeps look-alike spellings out of the write guard's reach.)"""
    if not isinstance(path, str) or not path.strip():
        raise BucsValueError(f"parameter path must be a non-empty string, got {path!r}")
    if len(path) > _MAX_PATH_LEN:
        raise BucsValueError(f"parameter path longer than {_MAX_PATH_LEN} characters")
    if not path.isascii() or any(ord(c) < 32 for c in path):
        raise BucsValueError(f"parameter path {path!r} contains non-ASCII or control characters")
    if any(not seg.strip() for seg in path.split(P.SEP)):
        raise BucsValueError(f"parameter path {path!r} has an empty segment")
    return path


def _folded(path: str) -> list[str]:
    return [seg.strip().casefold() for seg in path.split(P.SEP)]


def forbidden_reason(path: str) -> str | None:
    """Why ``set`` / ``trigger`` on *path* is refused, or ``None``. Case- and whitespace-insensitive."""
    segs = _folded(path)
    for group in P.DANGEROUS_GROUPS:
        g = _folded(group)
        if len(g) == 1:
            hit = g[0] in segs
        else:
            hit = segs[: len(g)] == g
        if hit:
            return f"group {group!r} is never written or triggered by automation"
    if "motorizedxyz" in segs and segs[-1] in {leaf.casefold() for leaf in P.COORDINATE_REZERO_LEAVES}:
        return "re-defining the stage coordinate origin / calibration move is never done by automation"
    return None


def check_write_path(path: str) -> str:
    """Raise :class:`BucsForbiddenPath` (or :class:`BucsValueError` for a malformed path) unless writing
    *path* is allowed. Runs before anything touches the server — also when disconnected."""
    check_path_syntax(path)
    why = forbidden_reason(path)
    if why is not None:
        raise BucsForbiddenPath(f"{path!r} is refused by the WITec write guard: {why}")
    return path


# ── parameter table ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParamInfo:
    """One entry of the server's parameter table."""

    path: str
    kind: str                 # one of KINDS
    dtype: str = ""           # the server's own type string, lower-cased ("float", "uint[2]" …)
    iid: str = ""

    @property
    def type(self) -> str:
        """Normalised type string used by :meth:`BucsClient.list_parameters` — ``status`` carries its data type."""
        return f"status:{self.dtype}" if self.kind == "status" and self.dtype else self.kind


def _is_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return True
    dtype = getattr(v, "dtype", None)          # numpy bool_
    return getattr(dtype, "kind", "") == "b"


def coerce_value(info: ParamInfo, value: Any) -> Any:
    """Check *value* against the parameter's kind and return what to send. Never converts silently
    between kinds (a ``bool`` is not an ``int``, a ``str`` is not a ``float``)."""
    kind = info.kind
    where = f"{info.path}: "
    if kind in ("trigger", "status"):
        raise BucsValueError(where + ("a trigger cannot be set — use trigger()" if kind == "trigger"
                                      else "a status container is read-only"))
    if kind == "bool":
        if not _is_bool(value):
            raise BucsValueError(where + f"needs a bool, got {value!r}")
        return bool(value)
    if kind == "string":
        if not isinstance(value, str):
            raise BucsValueError(where + f"needs a str, got {value!r}")
        return value
    if kind == "enum":
        if isinstance(value, str):
            return value
        if _is_bool(value) or not isinstance(value, numbers.Integral):
            raise BucsValueError(where + f"needs an enum index (int) or label (str), got {value!r}")
        return int(value)
    # numbers
    if _is_bool(value) or not isinstance(value, numbers.Real):
        raise BucsValueError(where + f"needs a number, got {value!r}")
    v = float(value)
    if not math.isfinite(v):
        raise BucsValueError(where + f"needs a finite number, got {value!r}")
    if kind == "int":
        if v != int(v):
            raise BucsValueError(where + f"needs a whole number, got {value!r}")
        return int(v)
    return v


# ── the abstraction ────────────────────────────────────────────────────────────────


class BucsClient(ABC):
    """Abstract WITec BUCS client. Concrete clients implement the ``_*_raw`` primitives; the public methods
    here are the template: write guard → table lookup → type check → range check → enabled → write access.

    Thread-safety: public methods may be called from any thread; subclasses decide how they serialise
    (the comtypes client funnels every COM call through one dedicated thread).
    """

    def __init__(self) -> None:
        self._tbl: dict[str, ParamInfo] | None = None
        self._tlock = threading.RLock()

    # -- lifecycle ----------------------------------------------------------------

    @property
    @abstractmethod
    def connected(self) -> bool:
        """Whether a live connection to WITec Control is held. No I/O."""

    @abstractmethod
    def _connect_raw(self) -> None: ...

    @abstractmethod
    def _close_raw(self) -> None: ...

    def _lock_wait_s(self) -> float:
        """How long a caller may wait for another thread's connect / table load before giving up with
        :class:`BucsTimeout` (subclasses with their own timeouts override). Never wait unboundedly: a panic stop
        must not queue forever behind a hung connect."""
        return 30.0

    @contextlib.contextmanager
    def _locked(self, what: str) -> Iterator[None]:
        wait = self._lock_wait_s()
        if not self._tlock.acquire(timeout=wait):
            raise BucsTimeout(f"{what}: another thread has been talking to WITec Control for more than "
                              f"{wait:.0f} s")
        try:
            yield
        finally:
            self._tlock.release()

    def connect(self) -> None:
        """Connect (idempotent). Raises :class:`BucsNotRunning` / :class:`BucsTypeLibMissing` / … — never retries."""
        if self.connected:
            return
        with self._locked("connect"):
            if self.connected:
                return
            self._tbl = None
            self._connect_raw()

    def close(self) -> None:
        """Give up write access if held, drop the connection. Idempotent, never raises, never waits long (if
        another thread is stuck in a call, closing goes ahead without the lock after two seconds)."""
        got = self._tlock.acquire(timeout=2.0)
        try:
            try:
                self._close_raw()
            except Exception:  # noqa: BLE001 - closing must not throw
                pass
            self._tbl = None
        finally:
            if got:
                self._tlock.release()

    @contextlib.contextmanager
    def call_timeout(self, seconds: float) -> Iterator[None]:
        """Within the block, calls made *by this thread* give up after *seconds* (stop paths use a short one so a
        wedged WITec Control cannot hold a panic stop for the normal 30 s). The base implementation has no
        timeouts to shorten."""
        yield

    def __enter__(self) -> "BucsClient":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- access -------------------------------------------------------------------

    @abstractmethod
    def _has_write_access_raw(self) -> bool: ...

    @abstractmethod
    def _has_read_access_raw(self) -> bool: ...

    @abstractmethod
    def _request_write_access_raw(self, on: bool) -> None: ...

    def has_read_access(self) -> bool:
        self.connect()
        return bool(self._has_read_access_raw())

    def has_write_access(self) -> bool:
        self.connect()
        return bool(self._has_write_access_raw())

    def request_write_access(self, on: bool = True) -> bool:
        """Ask WITec Control for write access (``on=True``) or give it back (``on=False``).

        Returns whether the *requested state* now holds. The server only grants write access while a person
        has pressed "COM Automation" (docs trap 1): without that this returns ``False`` — it never loops,
        never retries, and never raises for a plain refusal.
        """
        self.connect()
        want = bool(on)
        if self._has_write_access_raw() != want:
            self._request_write_access_raw(want)
        return self._has_write_access_raw() == want

    # -- parameter table ----------------------------------------------------------

    @abstractmethod
    def _load_table(self) -> dict[str, ParamInfo]: ...

    def _table(self) -> dict[str, ParamInfo]:
        tbl = self._tbl
        if tbl is not None and self.connected:           # fast path: no lock, so a stop is never queued behind a load
            return tbl
        with self._locked("parameter table"):
            self.connect()
            if self._tbl is None:
                self._tbl = self._load_table()
            return self._tbl

    def reload_parameters(self) -> None:
        """Forget the cached parameter table (it changes when WITec Control loads another configuration)."""
        self._tbl = None

    def list_parameters(self) -> dict[str, str]:
        """``{path: type}`` for every parameter on this configuration, sorted by path. ``type`` is one of
        ``int float bool string enum trigger`` or ``status:<datatype>``."""
        return {p: i.type for p, i in sorted(self._table().items())}

    def describe(self, path: str) -> ParamInfo:
        check_path_syntax(path)
        info = self._table().get(path)
        if info is None:
            raise BucsParameterMissing(f"no parameter {path!r} on this WITec Control configuration")
        return info

    def exists(self, path: str) -> bool:
        check_path_syntax(path)
        return path in self._table()

    # -- reads --------------------------------------------------------------------

    @abstractmethod
    def _get_raw(self, info: ParamInfo) -> Any: ...

    def get(self, path: str) -> Any:
        """Current value: ``int / float / bool / str``; an enum reads as its numeric index (see :meth:`get_enum`);
        a status container as its single value."""
        info = self.describe(path)
        if info.kind == "trigger":
            raise BucsValueError(f"{path}: a trigger has no value")
        return self._get_raw(info)

    def get_enum(self, path: str) -> tuple[int, str]:
        """``(index, label)`` of an enum parameter."""
        info = self.describe(path)
        if info.kind != "enum":
            raise BucsValueError(f"{path}: not an enum parameter (it is {info.type})")
        return self._get_enum_raw(info)

    def enum_values(self, path: str) -> dict[int, str]:
        """``{index: label}`` of an enum parameter."""
        info = self.describe(path)
        if info.kind != "enum":
            raise BucsValueError(f"{path}: not an enum parameter (it is {info.type})")
        return dict(self._enum_values_raw(info))

    def _get_enum_raw(self, info: ParamInfo) -> tuple[int, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _enum_values_raw(self, info: ParamInfo) -> dict[int, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- writes (guarded) ---------------------------------------------------------

    @abstractmethod
    def _set_raw(self, info: ParamInfo, value: Any) -> None: ...

    @abstractmethod
    def _trigger_raw(self, info: ParamInfo) -> None: ...

    def _range_of(self, info: ParamInfo) -> tuple[float | None, float | None]:
        """The range the *parameter itself* declares (``None`` = unbounded). Default: none declared."""
        return (None, None)

    def _is_enabled(self, info: ParamInfo) -> bool:
        return True

    def _require_ready_to_write(self, info: ParamInfo) -> None:
        if not self._is_enabled(info):
            raise BucsParameterDisabled(f"{info.path} is currently disabled in WITec Control")
        if not self._has_write_access_raw():
            raise BucsNoWriteAccess(
                f"no write access to WITec Control ({info.path}); a person must press 'COM Automation' "
                "in the WITec Control tree first")

    def set(self, path: str, value: Any) -> None:
        """Write a parameter. Guard → type → range → enabled → write access → send. Never clamps."""
        check_write_path(path)
        info = self.describe(path)
        v = coerce_value(info, value)
        if info.kind in ("int", "float"):
            lo, hi = self._range_of(info)
            if lo is not None and v < lo:
                raise BucsValueError(f"{path}: {v} is below the minimum {lo} the parameter declares")
            if hi is not None and v > hi:
                raise BucsValueError(f"{path}: {v} is above the maximum {hi} the parameter declares")
        self._require_ready_to_write(info)
        self._set_raw(info, v)

    def trigger(self, path: str) -> None:
        """Press a button. The return only means "the action started" — poll
        :data:`~mast.instruments.witec.params.IS_SEQUENCER_ACTIVE` for completion."""
        check_write_path(path)
        info = self.describe(path)
        if info.kind != "trigger":
            raise BucsValueError(f"{path}: not a trigger (it is {info.type})")
        self._require_ready_to_write(info)
        self._trigger_raw(info)


class ReadOnlyBucs:
    """A view of a client that *has no write methods at all*: ``set``, ``trigger`` and
    ``request_write_access`` do not exist on it. The read-only probe talks to WITec through this, so
    "the probe never writes" is a structural fact rather than a promise."""

    _ALLOWED = frozenset({
        "connect", "close", "connected", "exists", "list_parameters", "describe", "get", "get_enum",
        "enum_values", "has_read_access", "has_write_access", "reload_parameters", "call_timeout",
    })

    def __init__(self, client: BucsClient):
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name: str) -> Any:
        if name not in self._ALLOWED:
            raise AttributeError(f"ReadOnlyBucs has no {name!r}: this view cannot write")
        return getattr(self._client, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ReadOnlyBucs is read-only")


# ── lazy re-exports (the fake lives in its own module) ────────────────────────────


def __getattr__(name: str) -> Any:
    if name in ("FakeBucsClient", "VirtualClock"):
        from mast.instruments.witec import fake_bucs

        return getattr(fake_bucs, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
