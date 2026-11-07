"""WITecMicroscope — the WITec alpha300 (Raman/SHG) as a :class:`Spectrometer` *and* a :class:`Microscope`.

Design contract: ``docs/v2/design/optical_station_witec.md`` §1 (traps), §6.1, D2–D5.
``registry.spectrometer(id)`` and ``registry.microscope(id)`` return the same object for a ``witec_com`` entry,
so ``AcquireSpectrum`` and the ``Set Spectrometer*`` skills work unchanged next to the WITec skills.

Every primitive maps onto parameters of WITec Control's COM server through a
:class:`~mast.instruments.witec.bucs.BucsClient` (:mod:`~mast.instruments.witec.params` lists the paths and
where each comes from). The limit checks are the base classes' (Layer-0, refuse never clamp); this class adds
what only a driver can know: waiting for WITec to finish, verifying by read-back, refusing when a parameter
does not exist on this configuration.

How a MAST spectrum maps onto WITec's silent spectrum
-----------------------------------------------------
* ``exposure_ms``  ↔ WITec **integration time per accumulation** (``IntegrationTime``, seconds = ms / 1000);
* ``frames``       ↔ WITec **NumberOfAccumulations**. WITec returns every accumulation as its own
  ``[SpectrumData]`` block in the TrueMatch text (that is how the vendor SDK reads it), so ``frames=N`` gives N
  frames of ``exposure_ms`` each. If a WITec Control returns fewer blocks than requested the result says so
  (``meta['frames_returned']``) instead of pretending.
* the X axis is converted to nm using the excitation wavelength WITec writes into the text.

Laser emission (D7)
-------------------
Software may put light on the sample only through two gates and a cap. Gate 1: the device entry's
``allow_emission_control`` (default **false**). Gate 2: the *emission guard*, ``emission_guard(what)``, which raises
unless emission is allowed — by default the operator's positive, unexpired ``laser_area_safe`` attestation
(:class:`LaserAttestationGuard`; ``build()`` installs it unless the entry says ``require_laser_attestation: false``,
and a test or another station can inject its own). The cap: the selected laser's current power within its
``max_laser_power_mw`` (no cap configured, power unreadable, or a selected laser that matches no entry of the
``lasers`` table: refused).

The rule is "anything that makes MORE light on the sample goes through the check; anything that makes it darker is
never refused; a state that cannot be read counts as *may emit*". So the check guards: opening the shutter; the Raman
and confocal beam paths unless the shutter is CONFIRMED closed (then WITec's own laser adds nothing — a protocol with
an external source needs no WITec emission control); an acquisition unless it is a dark spectrum in the Raman path
with the shutter closed; an analyzer series; a power RAISE while the shutter is open or unreadable (a lowering, or
any change with the shutter confirmed closed, is not gated); and a laser selection or a state load that brings an
open shutter along. Where WITec might light the sample by itself — a beam-path switch, a state load, a laser
selection, a "dark" spectrum — the shutter is looked at every poll (never less often than every 0.2 s): if it has
opened and the gates or the cap say no, it is closed at once and the refusal raised. With the gates not met, a shutter
that is open is closed BEFORE a state is loaded; after the load the laser the state selected is the one judged.

The close direction always works: ``set_shutter(False)``, ``close_all_shutters()``, ``stop_all()``, ``release_write_access()``
and ``close()`` take no instrument lock (an exposure holds it for its whole run), ask for write access again silently
(RequestWriteAccess shows a person nothing, and the person's COM Automation grant stands until Revoke or a restart),
and say "NOT closed" — never nothing — when they cannot confirm it. Because WITec exposes only the SELECTED laser's
shutter, a stop, a release and a close visit every other laser (``SelectLaser<i>``), close its shutter and put the
selection back (``close_every_laser``); whether selecting a laser closes the previous one's shutter is not documented,
so no shutter is assumed closed. ``release_write_access()`` and ``close()`` make the station safe first: after the
release MAST could neither close nor stop. A stop that lands while an open is already on its way wins.

While a stop runs the selection moves from laser to laser, so from its first moment to its end (the *stop window*) an
open, a laser selection, a power change, a state load, an acquisition, an analyzer series and the Raman/confocal path
are refused with :class:`StopInProgress` — checked INSIDE the shutter guard, and the open judges and writes under that
one guard, so the laser that was judged is the laser that is opened. Closing the selected laser and visiting the others
take a *turn* (one at a time, a bounded wait, never the instrument lock), so two stops at once neither restore the
selection onto each other's laser nor name the wrong lasers. The laser that was selected is put back by the table, else
by the wavelength it had, else by the index last selected, and read back; when that fails the selection is *lost*: the
report says so and opening / raising power are refused until somebody chooses a laser explicitly (closing and lowering
never are). Write access a close had to regain only for itself is handed back right after it. ``last_stop_report`` and
``last_release_report`` carry ``shutter_confirmed`` (every shutter the stop looked at reads closed) — also when the stop
could not run at all.

The check is made when an action starts; an attestation that expires or is withdrawn during a long run does not
interrupt it (StopWITec / closing the shutter does).

Honest limits (the first run on a real WITec PC must confirm them):
* Whether ``SetValue`` on the analyzer / ``MoveTo…`` return after the motor stopped is not documented. Moves
  are therefore verified by read-back plus a settle time, never assumed.
* The grating and centre wavelength are "only available on special systems" (CHM): when the parameter is
  absent, ``set_grating`` / ``set_center_wavelength`` raise :class:`OperationNotSupported` and point to
  ``load_state`` (saved states carry the grating).
* WITec's own prompts (analyzer filter change, "Area around objective clear?", objective wizard) are never
  triggered by anything here; if one is on screen, calls time out and the error says so.
* Open questions the first run settles (``probe_witec_com.py --watch`` logs them read-only): does WITec open its own
  shutter during a spectrum or on SetStateRaman; does selecting a laser close the previous laser's shutter; does setting
  StateName block until the state is loaded (then a state that opens a shutter is lit for that long before the
  first look); does RequestWriteAccess(True) succeed again after a release without a person pressing the button.
  The driver is safe either way; the answers only tell which gates can be relaxed.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from mast.i18n import tr
from mast.instruments.base import (AbortCheck, InstrumentError, MotionAborted, MotionTimeout,
                                   OperationNotSupported)
from mast.instruments.laser import (EmissionControlDisabled, EmissionGuard, LaserAttestationGuard,
                                    LaserAttestationRequired)
from mast.instruments.laser import default_emission_guard as _default_laser_guard
from mast.instruments.microscope import Microscope, MicroscopeLimitError, MicroscopeLimits, WriteAccessDenied
from mast.instruments.spectrometer import (AcquisitionResult, FrameCallback, Spectrometer,
                                           SpectrometerLimitError, SpectrometerLimits, utc_now_iso)
from mast.instruments.witec import params as P
from mast.instruments.witec.bucs import (BucsClient, BucsConnectionLost, BucsDependencyMissing, BucsError,
                                          BucsNotRunning, BucsTimeout, BucsTypeLibMissing)
from mast.instruments.witec.paths import check_output_dir
from mast.instruments.witec.truematch import TrueMatchError, parse_truematch

logger = logging.getLogger(__name__)

__all__ = ["WITecConfig", "WITecMicroscope", "LaserAttestationRequired", "LaserAttestationGuard", "EmissionGuard",
           "ShutterNotClosed", "StopInProgress", "default_emission_guard", "build", "set_client_factory",
           "get_client_factory",
           "DEFAULT_LASERS", "DEFAULT_STATES"]

# EmissionGuard, LaserAttestationRequired and LaserAttestationGuard live in mast.instruments.laser (one
# implementation for the WITec and the external light sources); they are re-exported here.


class ShutterNotClosed(WriteAccessDenied):
    """A shutter could not be confirmed closed - usually because WITec Control refuses writes while we hold no write
    access (a person pressed Revoke, or WITec was restarted). The light may still be on: close the shutter by hand in
    WITec Control, or block the beam. ``report`` says which lasers were closed and which were not."""

    def __init__(self, message: str, report: dict | None = None):
        super().__init__(message)
        self.report: dict = dict(report or {})


class StopInProgress(MotionAborted):
    """A stop / close-all is visiting the lasers (WITec exposes only the SELECTED laser's shutter, so it selects each
    laser in turn and puts the selection back) or has arrived since this action began: what was asked for was not sent.
    A MotionAborted, so a protocol ends cleanly on it."""


def default_emission_guard(cfg: "WITecConfig") -> EmissionGuard | None:
    """What ``build()`` installs: the attestation guard, unless the entry says ``require_laser_attestation: false``."""
    return _default_laser_guard(bool(cfg.require_laser_attestation))


#: ASSUMED ``SelectLaser<i>`` order: COM lists five entries (355 passive, three TruePower
#: 532/633/1064, "white light") but not their index order. Every selection is cross-checked against the live
#: ``Laser|Selected|Wavelength``; fix the order with ``lasers`` in the device entry after the first run.
DEFAULT_LASERS: tuple[dict, ...] = (
    {"index": 0, "name": "355 nm", "wavelength_nm": 355.0, "power_controllable": False},
    {"index": 1, "name": "532 nm", "wavelength_nm": 532.0, "power_controllable": True},
    {"index": 2, "name": "633 nm", "wavelength_nm": 633.0, "power_controllable": True},
    {"index": 3, "name": "1064 nm", "wavelength_nm": 1064.0, "power_controllable": False},
    {"index": 4, "name": "white light", "wavelength_nm": 2600.0, "power_controllable": False},
)

#: the saved states of a typical WITec PC. COM can read and set the *current* state name but
#: cannot list the saved ones, so the list is configuration.
DEFAULT_STATES: tuple[str, ...] = (
    "Raman 355 - G300", "Raman 355 - G600", "Raman 633 - G300", "RayShield 532 - G1800",
    "RayShield 532 - G300", "RayShield 532 - G600", "SHG", "Unnamed State", "variable wavelength mode",
)

_DEFAULT_BEAM_MARKERS = {
    "raman": ("raman",), "video": ("video",), "all_off": ("alloff", "all off", "off"),
    "confocal": ("confocal",), "white_light": ("white",),
}

#: connection-class errors: a read that hits one must not be reported as "value unavailable"
_LINK_ERRORS = (BucsConnectionLost, BucsNotRunning, BucsTimeout, BucsTypeLibMissing, BucsDependencyMissing)


def _num(raw: dict, key: str, default: float, *, lo: float | None = None, hi: float | None = None) -> float:
    v = raw.get(key)
    if v is None:
        return float(default)
    try:
        f = float(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"witec_com: {key} must be a number, got {v!r}") from exc
    if not math.isfinite(f) or (lo is not None and f < lo) or (hi is not None and f > hi):
        raise ValueError(f"witec_com: {key}={v!r} outside [{lo}, {hi}]")
    return f


#: keys of a ``witec_com`` entry that are not WITecConfig fields (registry, limits, skill layer)
_ENTRY_KEYS = frozenset({"id", "type", "name", "enabled", "limits", "microscope_limits", "role",
                         "tip_requirement", "jog_step"})


@dataclass(frozen=True)
class WITecConfig:
    """Driver settings from the device entry — every key optional, defaults conservative.

    Example entry (``config/optical_instruments.json``)::

        {"id": "witec", "type": "witec_com", "name": "WITec alpha300 (UHTS600)", "enabled": true,
         "limits": {...}, "microscope_limits": {...},
         "allow_emission_control": false,              # D7 gate 1 (default false): software may start emission at all
         "require_laser_attestation": true,            # D7 gate 2: build() installs the attestation guard (the default)
         "write_access_wait_s": 0,                     # 0 = ask once; >0 = poll once a second for the button
         "lasers": [{"index": 1, "name": "532 nm", "wavelength_nm": 532, "power_controllable": true}, ...],
         "states": ["RayShield 532 - G300", ...],      # COM cannot list saved states
         "beam_markers": {"raman": ["raman"]},         # substrings of BeamPath|State, case-insensitive
         "poll_s": 0.1, "call_timeout_s": 30, "connect_timeout_s": 180, "connect_cooldown_s": 2,
         "stage_timeout_s": 120, "stage_tol_xy_um": 0.5, "stage_tol_z_um": 0.1, "analyzer_tol_deg": 0.5,
         "analyzer_settle_s": 0.5, "analyzer_full_turn_s": 6, "spectrograph_settle_s": 3, "state_settle_s": 3,
         "beam_path_timeout_s": 20, "beam_path_settle_s": 4, "series_start_grace_s": 5,
         "close_shutter_on_stop": true, "close_every_laser": true,
         "raw_dir": "D:/MAST-data/artifacts/witec/truematch",
         "machine": null, "gen_dir": null, "spectrograph": 1}

    The ones that stand for a *guess about WITec's behaviour* — settle times, tolerances, the beam-path
    vocabulary, the laser index order — are the first things to confirm on the machine.
    """

    write_access_wait_s: float = 0.0       # 0 = ask once; >0 = poll once a second for a person to press the button
    call_timeout_s: float = 30.0
    connect_timeout_s: float = 180.0
    connect_cooldown_s: float = 2.0        # after a failed connect, do not call COM again for this long
    poll_s: float = 0.1
    spectrograph: int = 1
    beam_path_timeout_s: float = 20.0
    beam_path_settle_s: float = 4.0        # the vendor's own example sleeps 4 s after SetStateRaman
    beam_markers: dict = field(default_factory=lambda: {k: tuple(v) for k, v in _DEFAULT_BEAM_MARKERS.items()})
    lasers: tuple = DEFAULT_LASERS
    laser_match_nm: float = 3.0
    laser_settle_s: float = 0.5
    power_settle_s: float = 1.0
    close_shutter_on_stop: bool = True
    close_every_laser: bool = True           # a stop / CloseWITecShutter / release also visits the other lasers (SelectLaser<i>)
    allow_emission_control: bool = False     # D7 gate 1: the operator enables software-started emission in the config
    require_laser_attestation: bool = True   # D7 gate 2: build() installs LaserAttestationGuard (the default emission guard)
    states: tuple = DEFAULT_STATES
    state_settle_s: float = 3.0
    stage_poll_s: float = 0.1
    stage_timeout_s: float = 120.0
    stage_grace_s: float = 2.0             # a finished-looking state is not believed before the position agrees
    stage_tol_xy_um: float = 0.5
    stage_tol_z_um: float = 0.1
    analyzer_tol_deg: float = 0.5
    analyzer_settle_s: float = 0.5
    analyzer_full_turn_s: float = 6.0      # extra settle for a full 360° move (scaled by the angle)
    analyzer_timeout_s: float = 60.0
    spectrograph_settle_s: float = 3.0
    spectrograph_timeout_s: float = 60.0
    sequencer_cleanup_timeout_s: float = 30.0
    autofocus_timeout_s: float = 90.0
    series_point_overhead_s: float = 3.0
    series_start_grace_s: float = 5.0
    video_timeout_s: float = 20.0
    project_timeout_s: float = 120.0
    raw_dir: str | None = None
    machine: str | None = None
    gen_dir: str | None = None

    @classmethod
    def from_entry(cls, entry: dict | None) -> "WITecConfig":
        raw = dict(entry or {})
        kw: dict[str, Any] = {}
        for key, default, lo, hi in (
                ("write_access_wait_s", 0.0, 0.0, 600.0), ("call_timeout_s", 30.0, 1.0, 600.0),
                ("connect_timeout_s", 180.0, 5.0, 900.0), ("connect_cooldown_s", 2.0, 0.0, 300.0),
                ("poll_s", 0.1, 0.001, 5.0), ("beam_path_timeout_s", 20.0, 1.0, 300.0),
                ("beam_path_settle_s", 4.0, 0.0, 60.0), ("laser_match_nm", 3.0, 0.1, 50.0),
                ("laser_settle_s", 0.5, 0.0, 30.0), ("power_settle_s", 1.0, 0.0, 60.0),
                ("state_settle_s", 3.0, 0.0, 120.0), ("stage_poll_s", 0.1, 0.01, 5.0),
                ("stage_timeout_s", 120.0, 1.0, 1800.0), ("stage_grace_s", 2.0, 0.0, 60.0),
                ("stage_tol_xy_um", 0.5, 0.001, 100.0), ("stage_tol_z_um", 0.1, 0.001, 100.0),
                ("analyzer_tol_deg", 0.5, 0.001, 45.0), ("analyzer_settle_s", 0.5, 0.0, 60.0),
                ("analyzer_full_turn_s", 6.0, 0.0, 120.0), ("analyzer_timeout_s", 60.0, 1.0, 600.0),
                ("spectrograph_settle_s", 3.0, 0.0, 120.0), ("spectrograph_timeout_s", 60.0, 1.0, 600.0),
                ("sequencer_cleanup_timeout_s", 30.0, 0.0, 600.0),
                ("autofocus_timeout_s", 90.0, 1.0, 900.0), ("series_point_overhead_s", 3.0, 0.0, 600.0),
                ("series_start_grace_s", 5.0, 0.5, 120.0), ("video_timeout_s", 20.0, 1.0, 300.0),
                ("project_timeout_s", 120.0, 1.0, 3600.0)):
            kw[key] = _num(raw, key, default, lo=lo, hi=hi)
        if raw.get("spectrograph") is not None:
            kw["spectrograph"] = int(_num(raw, "spectrograph", 1, lo=1, hi=3))
        for key in ("close_shutter_on_stop", "close_every_laser", "require_laser_attestation",
                    "allow_emission_control"):
            if raw.get(key) is not None:
                kw[key] = bool(raw[key])
        for key in ("raw_dir", "machine", "gen_dir"):
            if raw.get(key):
                kw[key] = str(raw[key])
        if raw.get("states") is not None:
            kw["states"] = tuple(str(s) for s in raw["states"])
        if raw.get("lasers") is not None:
            lasers = []
            for i, item in enumerate(raw["lasers"]):
                if not isinstance(item, dict) or "wavelength_nm" not in item:
                    raise ValueError(f"witec_com: lasers[{i}] needs at least wavelength_nm")
                lasers.append({"index": int(item.get("index", i)),
                               "name": str(item.get("name") or f"{float(item['wavelength_nm']):.0f} nm"),
                               "wavelength_nm": float(item["wavelength_nm"]),
                               "power_controllable": bool(item.get("power_controllable", False))})
            if len({l["index"] for l in lasers}) != len(lasers):
                raise ValueError("witec_com: lasers have duplicate index values")
            kw["lasers"] = tuple(lasers)
        if raw.get("beam_markers"):
            markers = {k: tuple(str(x).casefold() for x in v) for k, v in dict(raw["beam_markers"]).items()}
            merged = {k: tuple(v) for k, v in _DEFAULT_BEAM_MARKERS.items()}
            merged.update(markers)
            kw["beam_markers"] = merged
        return cls(**kw)


class _BoundedLock:
    """The spectrometer's lock for the microscope's writes too — with the same bounded wait: a stage move that
    arrives while an exposure runs is refused as busy, never queued behind it."""

    def __init__(self, lock: threading.RLock, wait_s: float, who: str):
        self._lock, self._wait, self._who = lock, wait_s, who

    def __enter__(self) -> "_BoundedLock":
        if not self._lock.acquire(timeout=self._wait):
            raise InstrumentError(
                f"{self._who}: busy (an acquisition or another WITec action is running) — not sent")
        return self

    def __exit__(self, *exc: Any) -> None:
        self._lock.release()

    acquire = __enter__

    def release(self) -> None:
        self._lock.release()


def _fmt_s(x: float) -> str:
    """Seconds as the vendor SDK writes them (``f"{x}"``: ``1.0``, ``0.2``), but never in scientific notation."""
    s = repr(float(x))
    if "e" in s.lower():
        s = f"{float(x):.9f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


class WITecMicroscope(Spectrometer, Microscope):
    """See the module docstring. Construct without touching hardware; the connection is made on first use."""

    def __init__(self, *, device_id: str = "witec", name: str = "",
                 limits: SpectrometerLimits | None = None,
                 microscope_limits: MicroscopeLimits | None = None,
                 config: WITecConfig | None = None, client: BucsClient | None = None,
                 client_factory: Callable[[], BucsClient] | None = None,
                 clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 emission_guard: EmissionGuard | None = None):
        """*emission_guard*: called before everything that can put laser light on the sample (see the module
        docstring, "Laser emission"); ``None`` = no operator gate (what ``require_laser_attestation: false`` gives).
        ``build()`` installs :class:`LaserAttestationGuard` by default; a direct construction gets what it passes."""
        Spectrometer.__init__(self, device_id=device_id, name=name, limits=limits, laser_line_nm=None)
        Microscope.__init__(self, device_id=device_id, name=name, microscope_limits=microscope_limits)
        self._mic_lock = _BoundedLock(self._lock, self.BUSY_WAIT_S, self.device_id)   # one lock, two roles
        self.cfg = config or WITecConfig()
        self.emission_guard: EmissionGuard | None = emission_guard
        if client is None and client_factory is None:
            raise ValueError("WITecMicroscope needs a client or a client_factory")
        self._cl: BucsClient | None = client
        self._factory = client_factory
        self._now = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._conn_lock = threading.RLock()          # held through a (slow) connect
        self._factory_lock = threading.Lock()        # never held through anything slow
        self._connect_error: BucsError | None = None
        self._connect_failed_at = -1e18
        self._caps: dict[str, bool] | None = None
        self._version = ""
        self._settings: dict[str, Any] = {}
        self._last_axis: np.ndarray | None = None
        self._laser_nm: float | None = None
        self._last_stop: dict = {}
        self._last_release: dict = {}
        self._shutter_lock = threading.RLock()       # one shutter write + read-back at a time; never held through anything slow
        self._stop_epoch = 0                         # bumped by every stop / close: an open already on its way gives up
        self._stopping = 0                           # stops under way: while > 0 no open / selection / power raise / state load
        self._stop_count_lock = threading.Lock()     # a leaf lock around the two counters above (never held through anything)
        self._sweep_serial = threading.Lock()        # one visit of the lasers at a time (a second would "restore" onto the first's laser)
        self._access_lock = threading.Lock()         # bookkeeping of write access regained only for a close (a leaf lock)
        self._temp_write = 0                         # closes that hold write access they regained
        self._write_adopted = False                  # somebody relied on it meanwhile: it is theirs, not handed back
        self._selection_lost = False                 # a stop could not put the laser selection back: nothing emits until one is chosen
        self._last_selected_index: int | None = None  # the laser index last identified or selected (where a lost selection belongs)
        self.last_acquisition: dict = {}

    # ── laser line: follows the selected laser ───────────────────────────────────────────

    @property
    def laser_line_nm(self) -> float | None:        # type: ignore[override]
        return self._laser_nm

    @laser_line_nm.setter
    def laser_line_nm(self, value: float | None) -> None:
        self._laser_nm = float(value) if value else None

    # ── connection ───────────────────────────────────────────────────────────────────────────

    def _client(self) -> BucsClient:
        with self._factory_lock:
            if self._cl is None:
                self._cl = self._factory()       # type: ignore[misc]
            return self._cl

    def _ready(self, wait_s: float | None = None) -> BucsClient:
        """The connected client. A failed connect is remembered for ``connect_cooldown_s`` so status polling
        does not hammer COM (and the log) while WITec Control is simply closed. If another thread is in the
        middle of a (slow) connect, wait for it at most *wait_s* (default: the call timeout) — a stop must not
        queue behind a hung connect."""
        cl = self._client()
        if cl.connected:
            return cl
        wait = self.cfg.call_timeout_s if wait_s is None else float(wait_s)
        if not self._conn_lock.acquire(timeout=wait):
            raise BucsTimeout(f"{self.device_id}: another thread has been connecting to WITec Control for "
                              f"more than {wait:.0f} s")
        try:
            if cl.connected:
                return cl
            now = self._now()
            if self._connect_error is not None and now - self._connect_failed_at < self.cfg.connect_cooldown_s:
                raise self._connect_error
            try:
                cl.connect()
            except BucsError as exc:
                self._connect_error, self._connect_failed_at = exc, now
                raise
            self._connect_error = None
            self._caps = None
            try:
                self._version = str(cl.get(P.PROGRAM_VERSION))
            except BucsError:
                self._version = ""
            logger.info("%s: connected to WITec Control %s", self.device_id, self._version or "(version unknown)")
            return cl
        finally:
            self._conn_lock.release()

    def _try_get(self, cl: BucsClient, path: str) -> tuple[bool, Any]:
        """``(ok, value)``; a parameter that is absent or has no value is ``(False, None)``; a dead link raises."""
        try:
            return True, cl.get(path)
        except _LINK_ERRORS:
            raise
        except BucsError:
            return False, None

    def _opt(self, cl: BucsClient, path: str, default: Any = None) -> Any:
        ok, v = self._try_get(cl, path)
        return v if ok else default

    def capabilities(self) -> dict[str, bool]:
        """Feature → whether *all* its parameters exist on the connected WITec Control (docs trap 10)."""
        cl = self._ready()
        if self._caps is None:
            self._caps = {f: all(cl.exists(p) for p in paths) for f, paths in P.FEATURE_PATHS.items()}
        return dict(self._caps)

    _FEATURE_HINT = {
        "center_wavelength": "load a saved state with the wanted centre (LoadWITecState)",
        "grating": "load a saved state with the wanted grating (LoadWITecState)",
    }

    def _need(self, cl: BucsClient, feature: str) -> None:
        missing = [p for p in P.FEATURE_PATHS[feature] if not cl.exists(p)]
        if missing:
            hint = self._FEATURE_HINT.get(feature, "check the parameter table with probe_witec_com.py")
            raise OperationNotSupported(
                f"{self.device_id}: this WITec Control has no '{feature}' "
                f"({missing[0]}{' …' if len(missing) > 1 else ''}) — {hint}")

    def close(self) -> None:
        """Make the station safe, give write access back and drop the connection (the registry calls this on release
        and shutdown). Shutters are closed and the sequencer stopped FIRST, while we still hold write access: afterwards
        MAST could do neither. A WITec Control that was never connected is not connected just to be closed."""
        cl = self._cl
        if cl is not None:
            try:
                if cl.connected:
                    self.stop_all()                  # never raises
            except Exception:  # noqa: BLE001
                pass
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass

    # ── small helpers ───────────────────────────────────────────────────────────────────────

    def _safe(self, fn: Callable[[], Any], what: str) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - best effort
            logger.warning("%s: %s failed: %s", self.device_id, what, exc)
            return None

    def _stop_sequencer_now(self, cl: BucsClient) -> bool:
        try:
            with cl.call_timeout(5.0):
                cl.trigger(P.STOP_SEQUENCER)
            return True
        except Exception as exc:  # noqa: BLE001 - e.g. write access was revoked: say so, do not raise
            logger.warning("%s: StopSequencer failed: %s", self.device_id, exc)
            return False

    def _aborted(self, check_abort: AbortCheck | None, what: str, stop: Callable[[], Any] | None = None) -> None:
        if check_abort is None:
            return
        try:
            hit = bool(check_abort())
        except BaseException:
            if stop is not None:
                self._safe(stop, f"stop after abort ({what})")
            raise
        if hit:
            if stop is not None:
                self._safe(stop, f"stop after abort ({what})")
            raise MotionAborted(f"{self.device_id}: {what} aborted")

    def _wait(self, seconds: float, check_abort: AbortCheck | None, what: str,
              stop: Callable[[], Any] | None = None) -> None:
        """Sleep *seconds* in poll-sized steps, looking at the abort predicate between steps."""
        end = self._now() + max(0.0, seconds)
        while True:
            self._aborted(check_abort, what, stop)
            left = end - self._now()
            if left <= 0:
                return
            self._sleep(min(self.cfg.poll_s, left))

    def _sequencer_active(self, cl: BucsClient) -> bool:
        return bool(cl.get(P.IS_SEQUENCER_ACTIVE))

    def _activity(self, cl: BucsClient) -> dict:
        out: dict[str, Any] = {}
        for key, path in (("name", P.ACTIVE_SEQUENCER_NAME), ("activity", P.ACTIVE_SEQUENCER_ACTIVITY)):
            ok, v = self._try_get(cl, path)
            if ok:
                out[key] = v
        return out

    def _refuse_if_busy(self, cl: BucsClient, what: str) -> None:
        if self._sequencer_active(cl):
            act = self._activity(cl)
            raise InstrumentError(
                f"{self.device_id}: WITec Control is busy ({act.get('name') or 'a sequencer'}"
                f"{': ' + act['activity'] if act.get('activity') else ''}) — {what} not started. "
                "Someone may be measuring in WITec Control; wait or StopWITec.")

    def _wait_idle(self, cl: BucsClient, *, timeout_s: float, check_abort: AbortCheck | None, what: str,
                   stop_on_trouble: bool = True) -> None:
        """Wait for ``IsASequencerActive`` to fall. Abort / timeout stop the sequencer and raise."""
        stop = (lambda: self._stop_sequencer_now(cl)) if stop_on_trouble else None
        deadline = self._now() + timeout_s
        while True:
            self._aborted(check_abort, what, stop)
            if not self._sequencer_active(cl):
                return
            if self._now() >= deadline:
                if stop is not None:
                    self._safe(stop, "stop after timeout")
                raise MotionTimeout(f"{self.device_id}: {what} did not finish within {timeout_s:.0f} s "
                                    "(WITec Control may be waiting on a dialog)")
            self._sleep(self.cfg.poll_s)

    # ── laser emission gates (docs D7) ──────────────────────────────────────────────────────

    def attestation_status(self) -> str:
        """The operator gate as the status view shows it: the default guard's ``valid`` / ``unsafe`` / ``missing`` /
        ``no_slot``; ``waived`` when no guard is installed (``require_laser_attestation: false``); ``custom`` for an
        injected guard that reports no status."""
        guard = self.emission_guard
        if guard is None:
            return "waived"
        status = getattr(guard, "status", None)
        return str(status()) if callable(status) else "custom"

    def _config_gate_text(self) -> str:
        return tr("设备配置 allow_emission_control=false：软件不得让这台 WITec 的激光出光"
                  "（操作员核对安全后，在配置里改成 true）",
                  "the device entry has allow_emission_control=false: software may not start laser emission "
                  "on this WITec (the operator sets it to true in the config after checking safety)")

    def emission_refusal(self) -> tuple[str, str] | None:
        """``(code, why)`` for the first of the two D7 gates that is not satisfied right now, or ``None``: the device
        entry must say ``allow_emission_control: true`` (default false) -> ``emission_control_disabled``; the emission
        guard (the operator's ``laser_area_safe`` attestation unless the entry waives it) must let it through ->
        ``laser_attestation`` / ``emission_guard``. ``why`` is in the UI language. Closing the shutter is never
        gated. A guard without a ``problem()`` method is called with a probe text, so it has to be cheap and free of
        side effects."""
        if not self.cfg.allow_emission_control:
            return ("emission_control_disabled", self._config_gate_text())
        guard = self.emission_guard
        if guard is None:
            return None
        problem = getattr(guard, "problem", None)
        if callable(problem):                                    # the default guard words its own reason
            why = problem()
            return ("laser_attestation", why) if why else None
        try:
            guard(tr("出光检查", "emission check"))
        except InstrumentError as exc:
            return ("laser_attestation" if isinstance(exc, LaserAttestationRequired) else "emission_guard", str(exc))
        return None

    def emission_problem(self) -> str | None:
        """The ``why`` of :meth:`emission_refusal`, or ``None``."""
        refusal = self.emission_refusal()
        return refusal[1] if refusal else None

    def _check_gates(self, what: str) -> None:
        """Raise unless both D7 gates let software start emission for *what*."""
        if not self.cfg.allow_emission_control:
            raise EmissionControlDisabled(f"{self.device_id}: {what} - {self._config_gate_text()}")
        guard = self.emission_guard
        if guard is not None:
            guard(f"{self.device_id}: {what}")

    def _require_emission(self, cl: BucsClient, what: str) -> None:
        """Everything that must hold before software lets the laser light the sample: both D7 gates, a selected laser
        we can identify, and that laser's current power within its cap (a person may have raised it in the WITec GUI
        since MAST set it; a laser whose power WITec cannot set has no software cap to check)."""
        self._check_gates(what)
        if self._selection_lost:
            raise MicroscopeLimitError(f"{self.device_id}: {what} - {self._selection_lost_text()}")
        las = [l for l in self.list_lasers() if l.get("selected")]
        if not las:
            ok_w, wl = self._try_get(cl, P.LASER_WAVELENGTH)
            seen = f"{float(wl):g} nm" if ok_w and wl else "an unreadable wavelength"
            raise MicroscopeLimitError(
                f"{self.device_id}: {what} - the selected laser cannot be identified (WITec reports {seen}; none of "
                f"the lasers in the device entry is within {self.cfg.laser_match_nm:g} nm of that), so its power cap "
                "cannot be applied - refused; fix the 'lasers' table in the device entry")
        if not las[0].get("power_controllable"):
            return
        cap = self._power_cap(las[0])                       # no cap configured: refused (fail closed)
        ok, power = self._try_get(cl, P.LASER_POWER)
        if not ok or power is None:
            raise MicroscopeLimitError(
                f"{self.device_id}: {what} - the {las[0].get('name')} laser's power cannot be read from WITec, "
                f"so the {cap:g} mW cap cannot be verified - refused")
        if float(power) > cap * 1.02 + 1e-9:
            raise MicroscopeLimitError(
                f"{self.device_id}: {what} - the {las[0].get('name')} laser is set to {float(power):g} mW, above "
                f"the {cap:g} mW cap; lower it with SetWITecLaserPower first")

    def _selection_lost_text(self) -> str:
        return tr("上一次停止没能把激光选择放回原处：它留在了最后访问的那台上，那不是任何人的选择；"
                  "先用 SelectWITecLaser 明确选定一台激光",
                  "the last stop could not put the laser selection back: it left the last laser it visited selected, "
                  "which is nobody's choice; choose a laser explicitly (SelectWITecLaser) first")

    def _emission_view(self) -> dict:
        """What the status shows about the two D7 gates (config and operator), without a connection."""
        return {"allow_emission_control": self.cfg.allow_emission_control,
                "attestation": self.attestation_status(),
                "selection_known": not self._selection_lost,
                "software_may_start_emission": self.emission_refusal() is None}

    # ── shutters: reading them, judging a lit one, closing them, and the stops built on that ────────────────────────────

    def _shutter_state(self, cl: BucsClient) -> str:
        """The selected laser's shutter: ``"open"``, ``"closed"``, ``"none"`` (this laser has no software shutter, so its
        light cannot be shut off from here) or ``"unknown"`` (cannot be read - treated as possibly open)."""
        ok_h, has = self._try_get(cl, P.LASER_HAS_SHUTTER)
        if ok_h and has is False:
            return "none"
        ok, shut = self._try_get(cl, P.LASER_SHUTTER)
        if not ok or shut is None:
            return "unknown"
        return "open" if bool(shut) else "closed"

    def _may_emit(self, cl: BucsClient) -> bool:
        """Could the next action put WITec laser light on the sample? Yes unless the selected laser's shutter is
        confirmed closed AND the beam path is already Raman (switching to it might open the shutter - the first run on
        the machine tells). Unreadable, or a laser with no software shutter, counts as "may emit"."""
        ok_b, state = self._try_get(cl, P.BEAM_STATE)
        on_raman = ok_b and self._beam_matches(str(state), "raman")
        return self._shutter_state(cl) != "closed" or not on_raman

    def _watch_emission(self, cl: BucsClient, what: str, seen: dict) -> None:
        """Call while WITec does something that must not light the sample on its own (a beam-path switch, a state load,
        a laser selection, a dark spectrum). A shutter that has turned open is judged like any emission: both gates and
        the cap. If that says no, the shutter is closed at once and the refusal raised. *seen* remembers a verdict of
        "allowed" so the check is not repeated every poll."""
        if seen.get("allowed") or self._shutter_state(cl) != "open":
            return
        try:
            self._require_emission(cl, what)
        except Exception as exc:
            self._refuse_and_close(cl, exc)
        seen["allowed"] = True

    def _settle_watching(self, cl: BucsClient, seconds: float, what: str | None,
                         check_abort: AbortCheck | None = None) -> None:
        """Wait *seconds*, looking at the shutter every poll (never less often than every 0.2 s) when *what* is given.
        A stop that arrives meanwhile ends the wait (:class:`StopInProgress`): what was being settled is moot."""
        if not what:
            self._wait(seconds, check_abort, "settle")
            return
        seen: dict = {}
        epoch = self._stop_epoch
        step = min(self.cfg.poll_s, 0.2)
        end = self._now() + max(0.0, seconds)
        while True:
            self._watch_emission(cl, what, seen)
            self._aborted(check_abort, what)
            if self._stop_epoch != epoch:
                raise self._stopping_error(what)
            left = end - self._now()
            if left <= 0:
                return
            self._sleep(min(step, left))

    @contextlib.contextmanager
    def _shutter_guard(self, wait_s: float = 3.0):
        """One shutter write (and its read-back) at a time - short, never held through anything slow. Not the
        instrument lock: closing a shutter must work while an exposure holds that."""
        if not self._shutter_lock.acquire(timeout=wait_s):
            raise InstrumentError(f"{self.device_id}: another shutter action has been running for more than "
                                  f"{wait_s:.0f} s")
        try:
            yield
        finally:
            self._shutter_lock.release()

    # -- a stop visits the lasers: nothing that adds light or depends on the selection may run meanwhile ---------------------

    SWEEP_WAIT_S = 5.0

    def _stopping_error(self, what: str) -> StopInProgress:
        return StopInProgress(tr(
            f"{self.device_id}: 正在停止/关快门（它要逐台访问激光，激光选择在变），『{what}』没有发出；停止结束后再试",
            f"{self.device_id}: a stop / close-all is under way (it visits the lasers one by one, so the selection is "
            f"not stable) - '{what}' was not sent; try again when it is over"))

    def _check_not_stopping(self, epoch: int, what: str) -> None:
        """Inside the shutter guard: refuse if a stop is under way or has arrived since *epoch*."""
        if self._stopping or self._stop_epoch != epoch:
            raise self._stopping_error(what)

    @contextlib.contextmanager
    def _exclusive_of_stops(self, epoch: int, what: str):
        """The critical write of an action that adds light or depends on the selection, under the shutter lock with the
        stop flag read under it: either the write completes before a stop starts (the stop then looks at what it did)
        or it sees the stop and is refused. A stop's sweep cannot slide in between a judgement and its write."""
        with self._shutter_guard():
            self._check_not_stopping(epoch, what)
            yield

    def _refuse_while_sweeping(self, what: str) -> None:
        with self._shutter_guard():
            if self._stopping:
                raise self._stopping_error(what)

    def _begin_stop(self) -> None:
        """Raise the stop flag and bump the epoch - under the shutter lock when it can be had (so no guarded write is
        half way through), the counters under their own leaf lock whatever happens."""
        got = self._shutter_lock.acquire(timeout=3.0)
        try:
            with self._stop_count_lock:
                self._stop_epoch += 1
                self._stopping += 1
        finally:
            if got:
                self._shutter_lock.release()

    def _end_stop(self) -> None:
        with self._stop_count_lock:
            self._stopping = max(0, self._stopping - 1)

    @contextlib.contextmanager
    def _stopping_window(self):
        """From the first moment of a stop until it is done, opens, selections, power raises and state loads are refused.
        Never takes the instrument lock: the close direction does not wait behind an exposure."""
        self._begin_stop()
        try:
            yield
        finally:
            self._end_stop()

    def _regain_write(self, cl: BucsClient) -> str:
        """``"held"`` / ``"regained"`` / ``"unavailable"``. The close direction has to work whenever the person's grant
        still stands (nobody pressed Revoke, WITec was not restarted) - also after ReleaseWITecWriteAccess or a
        Disconnect: ask once, silently (RequestWriteAccess shows a person nothing), never wait."""
        try:
            if cl.has_write_access():
                return "held"
            return "regained" if cl.request_write_access(True) else "unavailable"
        except _LINK_ERRORS:
            raise
        except BucsError:
            return "unavailable"

    @contextlib.contextmanager
    def _closing_access(self, cl: BucsClient):
        """Write access for a close or a stop: used as it is when held; regained silently when not, and then handed BACK
        afterwards (D5: write access goes back when the job is done) - unless somebody has started to rely on it
        meanwhile (a request for it, or a write). Closes that overlap share one regain; the last one out hands it back."""
        with self._access_lock:
            try:
                state = self._regain_write(cl)
            except Exception as exc:  # noqa: BLE001 - a dead link shows in the parts that follow
                logger.warning("%s: write access check failed: %s", self.device_id, exc)
                state = "unavailable"
            joined = state == "regained" or (state == "held" and self._temp_write > 0)
            if joined:
                self._temp_write += 1
        try:
            yield state
        finally:
            if joined:
                give_back = False
                with self._access_lock:
                    self._temp_write -= 1
                    if self._temp_write == 0:
                        give_back, self._write_adopted = not self._write_adopted, False
                if give_back:
                    self._safe(lambda: cl.request_write_access(False), "hand write access back after a close")

    def request_write_access(self) -> bool:
        if self._temp_write:
            self._write_adopted = True               # asked for while a close holds access it regained: it is theirs now
        ok = super().request_write_access()
        if ok and self._temp_write:
            self._write_adopted = True
        return ok

    def _require_write(self, what: str) -> None:
        super()._require_write(what)
        if self._temp_write:
            self._write_adopted = True               # a write is about to rely on access a close had regained

    def _write_shutter_closed(self, cl: BucsClient) -> None:
        """Close the selected laser's shutter and read it back. Raises if WITec refuses or it still reads open."""
        with self._shutter_guard():
            cl.set(P.LASER_SHUTTER, False)
            self._sleep(self.cfg.poll_s)
            if bool(cl.get(P.LASER_SHUTTER)):
                raise InstrumentError(f"{self.device_id}: the shutter still reads open after asking for closed")

    def _close_selected_quietly(self, cl: BucsClient) -> bool:
        """Best effort, for the error paths of the emission checks: close the selected laser's shutter, never raise."""
        try:
            with self._closing_access(cl):
                self._write_shutter_closed(cl)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: closing the shutter after a refused emission failed: %s", self.device_id, exc)
            return False

    def _refuse_and_close(self, cl: BucsClient, exc: Exception) -> None:
        """The emission check said no about a shutter that is already open: close it again, then raise the refusal.
        If it cannot be closed (write access gone, WITec refuses) the error says so - the refusal alone would read as
        "nothing happened" while the light is still on the sample. Always raises."""
        if self._close_selected_quietly(cl):
            raise exc
        raise ShutterNotClosed(
            tr(f"{exc}。而且这个快门没能关回去：光可能还在样品上，请在 WITec Control 里手动关快门，或挡住光路。",
               f"{exc} - AND the shutter could not be closed again: the light may be on the sample; close it by "
               "hand in WITec Control, or block the beam."),
            report={"refused": str(exc), "closed": False}) from exc

    def _selected_name(self, cl: BucsClient) -> str:
        sel = self._laser_view(cl)
        if sel is not None:
            return str(sel["name"])
        ok, wl = self._try_get(cl, P.LASER_WAVELENGTH)
        return f"the selected laser ({float(wl):g} nm)" if ok and wl else "the selected laser"

    @staticmethod
    def _new_shutter_report() -> dict:
        return {"closed": [], "already_closed": [], "no_software_shutter": [], "not_present": [], "failed": {},
                "skipped": "", "selection_restored": None, "selection_basis": "", "selection_lost": False,
                "note": "", "scope": "", "selected_without_shutter": ""}

    @staticmethod
    def _handled(shut: dict, name: str) -> bool:
        return (name in shut["closed"] or name in shut["already_closed"] or name in shut["no_software_shutter"]
                or name in shut["failed"])

    @staticmethod
    def _confirmed(shut: dict) -> bool:
        """Every shutter the stop looked at that has a software handle reads closed, and no laser was left unexamined
        (``scope`` says which lasers it was set up to look at)."""
        return not shut["failed"] and not shut["selected_without_shutter"] and not shut["skipped"]

    def _close_selected_into(self, cl: BucsClient, shut: dict) -> None:
        """Close the selected laser's shutter, recording the outcome in *shut*. Never raises (a dead link is a recorded
        failure, not an exception: the report of the stop must still come out)."""
        try:
            name = self._selected_name(cl)
        except Exception:  # noqa: BLE001
            name = "the selected laser"
        try:
            if self._shutter_state(cl) == "none":
                shut["selected_without_shutter"] = name          # not a failure of the close: there is nothing to close
                shut["no_software_shutter"].append(name)
                return
            self._write_shutter_closed(cl)
            shut["closed"].append(name)
        except Exception as exc:  # noqa: BLE001 - recorded, not raised: the other parts of a stop still matter
            shut["failed"][name] = str(exc)

    #: how close two wavelengths must be to be "the same laser" when the table cannot say which one it is
    SELECTION_MATCH_NM = 0.6

    def _nominal_nm(self, index: int) -> float | None:
        return next((float(l["wavelength_nm"]) for l in self.cfg.lasers if int(l["index"]) == index), None)

    @contextlib.contextmanager
    def _lasers_turn(self):
        """Whoever touches WITec's laser selection on purpose (a stop's close of the selected laser, its visit of the
        others) takes its turn: one at a time, so a second stop neither closes a laser the first has selected only on
        its way, nor "restores" the selection onto it, nor names the wrong lasers in its report. A bounded wait
        (:attr:`SWEEP_WAIT_S`): a visit that hangs must not stop the next stop from trying. Never the instrument lock."""
        got = self._sweep_serial.acquire(timeout=self.SWEEP_WAIT_S)
        if not got:
            logger.warning("%s: another visit of the lasers has run for more than %.0f s; going ahead",
                           self.device_id, self.SWEEP_WAIT_S)
        try:
            yield
        finally:
            if got:
                self._sweep_serial.release()

    def _sweep_other_lasers(self, cl: BucsClient, shut: dict) -> None:
        """Visit every other laser (SelectLaser<i>), close its shutter if it is not confirmed closed, then put the
        selection back. WITec's ``Laser|Selected|Shutter`` is the only shutter handle there is, and whether a shutter
        stays as it is after another laser is selected is not documented - so the only way to be sure no shutter is
        left open is to look at each. Never raises. Call inside :meth:`_stopping_window` and :meth:`_lasers_turn`."""
        try:
            self._visit_lasers(cl, shut)
        except Exception as exc:  # noqa: BLE001 - recorded, not raised: the other parts of a stop still matter
            shut["failed"]["visiting the other lasers"] = f"{type(exc).__name__}: {exc}"

    def _visit_lasers(self, cl: BucsClient, shut: dict) -> None:
        sel = self._laser_view(cl)
        ok_w, w0 = self._try_get(cl, P.LASER_WAVELENGTH)
        now_wl = float(w0) if ok_w and w0 else None
        lost_before, remembered = self._selection_lost, self._last_selected_index
        ok_n, n_live = self._try_get(cl, P.LASER_COUNT)
        known = {int(l["index"]): str(l["name"]) for l in self.cfg.lasers}
        if ok_n and n_live is not None:
            live = int(n_live)                       # the machine's own count wins over the table
            known = {i: known.get(i, f"laser {i}") for i in range(live)}
        if sel is not None and not self._handled(shut, str(sel["name"])):
            self._close_selected_into(cl, shut)      # another stop moved the selection since this one closed its first laser
        wl_at: dict[int, float] = {}
        for index in sorted(known):
            if sel is not None and index == int(sel["index"]):
                continue
            name = known[index]
            try:
                path = P.laser_select_path(index)
                if not cl.exists(path):                  # the table lists a laser this machine does not have
                    shut["not_present"].append(name)
                    continue
                cl.trigger(path)
                self._wait(min(self.cfg.laser_settle_s, 0.3), None, "laser selection settle (close)")
                ok_v, wv = self._try_get(cl, P.LASER_WAVELENGTH)
                if ok_v and wv:
                    wl_at[index] = float(wv)
                state = self._shutter_state(cl)
                if state == "none":
                    shut["no_software_shutter"].append(name)
                elif state == "closed":
                    shut["already_closed"].append(name)
                else:
                    self._write_shutter_closed(cl)
                    shut["closed"].append(name)
            except Exception as exc:  # noqa: BLE001
                shut["failed"][name] = str(exc)
        self._put_selection_back(cl, shut, sel=sel, now_wl=now_wl, remembered=remembered, lost_before=lost_before,
                                 wl_at=wl_at, known=known)

    def _put_selection_back(self, cl: BucsClient, shut: dict, *, sel: dict | None, now_wl: float | None,
                            remembered: int | None, lost_before: bool, wl_at: dict[int, float],
                            known: dict[int, str]) -> None:
        """Select again the laser that was selected when the visit began - found by the wavelength table, else by the
        wavelength it had, else by the index this driver last selected or identified - and check that it reads as it
        did. A selection an earlier stop already lost goes back to the index that stop remembered. If none of that
        works the selection is *lost*: nothing emits or raises power until somebody chooses a laser explicitly."""
        home, basis, expect_wl, tol = None, "", None, self.SELECTION_MATCH_NM
        if lost_before:
            if remembered is not None and remembered in known:
                home, basis = remembered, "the selection an earlier stop could not put back"
                expect_wl, tol = self._nominal_nm(remembered), self.cfg.laser_match_nm
        elif sel is not None:
            home, basis, expect_wl = int(sel["index"]), "identified", now_wl
        else:
            if now_wl is not None:
                hits = [i for i, w in wl_at.items() if abs(w - now_wl) <= self.SELECTION_MATCH_NM]
                if len(hits) == 1:
                    home, basis, expect_wl = hits[0], "by wavelength", now_wl
            if home is None and remembered is not None and remembered in known:
                home, basis, expect_wl = remembered, "remembered" + ("" if now_wl is not None else ", unverified"), now_wl
        restored, why = False, ""
        if home is None:
            why = tr("无法判断原来选中的是哪一台（波长读不出，或和表里任何一台都对不上）",
                     "the laser that was selected could not be told (its wavelength was unreadable or matches no laser)")
        else:
            try:
                cl.trigger(P.laser_select_path(home))
                self._wait(min(self.cfg.laser_settle_s, 0.3), None, "laser selection settle (restore)")
                ok_v, wv = self._try_get(cl, P.LASER_WAVELENGTH)
                if expect_wl is not None and ok_v and wv and abs(float(wv) - expect_wl) > tol:
                    why = f"index {home} reads {float(wv):g} nm, the laser should read {expect_wl:g} nm"
                else:
                    restored = True
            except Exception as exc:  # noqa: BLE001
                why = f"the selection could not be put back on index {home}: {exc}"
        shut["selection_restored"], shut["selection_basis"] = restored, basis
        if home is not None:
            self._last_selected_index = home         # where the selection belongs, for the next stop
        if restored:
            self._selection_lost = False
            if lost_before:      # the laser this stop began on was nobody's choice and is not selected any more
                home_name = known.get(home, "")
                shut["selected_without_shutter"] = home_name if home_name in shut["no_software_shutter"] else ""
            return
        self._selection_lost = True
        shut["selection_lost"] = True
        shut["note"] = tr(
            f"激光选择没能放回原处（{why}）：停止把选择留在了最后访问的那台上，那不是任何人的选择。"
            "在有人用 SelectWITecLaser 明确选定一台激光之前，开快门和加功率都会被拒绝（关和降不受影响）。",
            f"the laser selection could not be put back ({why}): the stop left the last laser it visited selected, which "
            "is nobody's choice. Opening a shutter and raising power are refused until somebody chooses a laser "
            "explicitly (SelectWITecLaser); closing and lowering are not affected.")
        logger.warning("%s: %s", self.device_id, shut["note"])

    def _verdict(self, shut: dict) -> str:
        """``"closed"``, or ``"not closed: …"`` naming every laser whose light cannot be shut off from here."""
        parts = [f"{k}: {v}" for k, v in shut["failed"].items()]
        if shut["selected_without_shutter"]:
            parts.append(f"{shut['selected_without_shutter']}: "
                         + tr("没有软件快门：请物理挡光", "no software shutter - block the beam physically"))
        return "closed" if not parts else "not closed: " + "; ".join(parts)

    def _not_closed_text(self, shut: dict) -> str:
        why = "; ".join(f"{k}: {v}" for k, v in shut["failed"].items())
        return tr(f"{self.device_id}: 快门没有确认关上（{why}）。光可能还在样品上：请在 WITec Control 里手动关快门，或挡住光路。",
                  f"{self.device_id}: the shutter was NOT closed ({why}). The light may still be on: close it by hand "
                  "in WITec Control, or block the beam.")

    def set_shutter(self, open_: bool) -> bool:
        """Opening: write access, both gates, the cap (see :meth:`_set_shutter_raw`). Closing the selected laser's
        shutter is never refused by policy, takes no instrument lock (an exposure holds it for its whole run) and gets
        write access back silently when the person's grant still stands; if it cannot be confirmed closed,
        :class:`ShutterNotClosed` says so."""
        if open_:
            return super().set_shutter(True)
        return self._close_selected_shutter()

    def _close_selected_shutter(self) -> bool:
        cl = self._ready(wait_s=3.0)
        self._need(cl, "laser_shutter")
        self._stop_epoch += 1
        shut = self._new_shutter_report()
        shut["scope"] = "selected_laser_only"
        with cl.call_timeout(5.0), self._closing_access(cl), self._lasers_turn():     # (a visit under way has its say first)
            if self._shutter_state(cl) == "none":
                raise OperationNotSupported(
                    f"{self.device_id}: the selected laser has no software shutter - block the beam physically")
            self._close_selected_into(cl, shut)
        if shut["failed"]:
            raise ShutterNotClosed(self._not_closed_text(shut), report=shut)
        return False

    def close_all_shutters(self) -> dict:
        """The safety action behind CloseWITecShutter: every laser's shutter closed, the selected laser's first. Not
        gated, no instrument lock, write access asked for silently (and handed back afterwards when it had to be
        regained). The other lasers are visited only while no sequencer runs (swapping lasers under a running
        measurement would disturb it - StopWITec stops the sequence first); while the visit lasts no shutter is opened
        and no laser selected or changed. Returns what happened (``shutter_confirmed``: every shutter this looked at
        reads closed); raises :class:`ShutterNotClosed` if a shutter could not be confirmed closed."""
        cl = self._ready(wait_s=3.0)
        self._need(cl, "laser_shutter")
        shut = self._new_shutter_report()
        shut["scope"] = "all_lasers" if self.cfg.close_every_laser else "selected_laser_only"
        with self._stopping_window(), cl.call_timeout(5.0), self._closing_access(cl), self._lasers_turn():
            self._close_selected_into(cl, shut)
            if self.cfg.close_every_laser:
                try:
                    busy, unreadable = bool(self._opt(cl, P.IS_SEQUENCER_ACTIVE, False)), ""
                except Exception as exc:  # noqa: BLE001 - an unreadable sequencer may be running: do not swap lasers under it
                    busy, unreadable = True, str(exc)
                if busy:
                    shut["skipped"] = (
                        tr(f"没有去动其他激光：序列器状态读不出（{unreadable}），可能有测量在跑",
                           f"other lasers not visited: the sequencer state could not be read ({unreadable}) - a "
                           "measurement may be running")
                        if unreadable else
                        tr("没有去动其他激光：序列器在跑（换激光会打扰它）——StopWITec 会先停序列再逐台关",
                           "other lasers not visited: a sequencer is running (swapping lasers would disturb it) - "
                           "StopWITec stops it first and then visits them"))
                else:
                    self._sweep_other_lasers(cl, shut)
        shut["shutter_confirmed"] = self._confirmed(shut)
        if shut["failed"]:
            raise ShutterNotClosed(self._not_closed_text(shut), report=shut)
        if shut["selected_without_shutter"]:                    # everything that has a shutter is closed; this one cannot be
            raise OperationNotSupported(
                f"{self.device_id}: the selected laser ({shut['selected_without_shutter']}) has no software shutter - "
                "block the beam physically (the shutters of the other lasers were closed)")
        return shut

    def release_write_access(self) -> None:
        """Hand write access back to WITec Control - after making the station safe: once released, MAST can neither
        close a shutter nor stop a sequence until it is granted again (silently, if the person's grant stands). Every
        laser's shutter is closed and the sequencer stopped first; what could not be done is in
        :attr:`last_release_report` and the log, and the release goes ahead. Never queued behind an exposure."""
        cl = self._cl
        if cl is not None and cl.connected:
            try:
                self._last_release = self._make_safe(cl, stage=False)
            except Exception as exc:  # noqa: BLE001
                self._last_release = {"error": str(exc), "shutter_confirmed": False}
                logger.warning("%s: making the station safe before the release failed: %s", self.device_id, exc)
        self._release_write_access_raw()

    @property
    def last_release_report(self) -> dict:
        return dict(self._last_release)

    def _make_safe(self, cl: BucsClient, *, stage: bool) -> dict:
        """Shutters closed (the selected laser's first), the sequencer stopped, optionally the stage; then - the
        sequencer being stopped - the other lasers' shutters. Each part on its own, nothing raised; returns the report
        (``shutter`` / ``sequencer`` / ``stage_*`` are ``"closed"`` / ``"stop sent"`` or ``"not …: reason"``;
        ``shutter_confirmed`` is the machine-readable "every shutter this stop looked at reads closed" - ``False`` when
        the stop is set up not to touch shutters, or the selected laser has none). Write access regained for this is
        handed back at the end."""
        report: dict[str, Any] = {}
        shut: dict | None = None
        with self._stopping_window(), cl.call_timeout(5.0), self._closing_access(cl) as access:
            report["write_access"] = access
            if self.cfg.close_shutter_on_stop:
                shut = self._new_shutter_report()
                shut["scope"] = "all_lasers" if self.cfg.close_every_laser else "selected_laser_only"
                with self._lasers_turn():            # (a visit under way has its say first; then this closes what is selected)
                    self._close_selected_into(cl, shut)
            try:
                cl.trigger(P.STOP_SEQUENCER)
                report["sequencer"] = "stop sent"
            except Exception as exc:  # noqa: BLE001
                report["sequencer"] = f"not stopped: {exc}"
            if stage:
                for key, path in (("stage_xy", P.XY_STOP), ("stage_z", P.Z_STOP)):
                    try:
                        cl.trigger(path)
                        report[key] = "stop sent"
                    except Exception as exc:  # noqa: BLE001
                        report[key] = f"not stopped: {exc}"
            if shut is not None:
                if self.cfg.close_every_laser:
                    with self._lasers_turn():
                        self._sweep_other_lasers(cl, shut)
                report["shutters"] = shut
                report["shutter"] = self._verdict(shut)
        report["shutter_confirmed"] = bool(shut is not None and self._confirmed(shut))
        bad = {k: v for k, v in report.items() if isinstance(v, str) and "not " in v}
        if bad:
            logger.warning("%s: stop incomplete: %s", self.device_id, bad)
        return report

    def move_stage_um(self, **kw: Any) -> dict:
        cl = self._ready()
        if self._opt(cl, P.STAGE_IS_VIRTUAL, False):          # say so before the generic "position unreadable"
            raise OperationNotSupported(f"{self.device_id}: WITec reports the XYZ stage as virtual (not motorised)")
        return super().move_stage_um(**kw)

    # ── settings snapshot (provenance) ──────────────────────────────────────────────────────

    def _snapshot(self, cl: BucsClient) -> dict:
        """What WITec Control currently says about itself, for the spectrum's sidecar. Best effort: each
        item is read alone, an unreadable one is simply absent."""
        out: dict[str, Any] = {}
        sg = self.cfg.spectrograph
        reads: list[tuple[str, str, Callable[[Any], Any] | None]] = [
            ("laser_wavelength_nm", P.LASER_WAVELENGTH, float), ("laser_power_mw", P.LASER_POWER, float),
            ("shutter_open", P.LASER_SHUTTER, bool), ("beam_path", P.BEAM_STATE, str),
            ("analyzer_deg", P.ANALYZER, float), ("analyzer_in", P.ANALYZER_COUPLER, bool),
            ("center_wavelength_nm", P.spectrograph_path(sg, "CenterWavelength"), float),
            ("objective", P.OBJECTIVE_INFORMATION, str), ("magnification", P.OBJECTIVE_MAGNIFICATION, float),
            ("state_name", P.STATE_NAME, str), ("stage_x_um", P.XY_CURRENT_X, float),
            ("stage_y_um", P.XY_CURRENT_Y, float), ("stage_z_um", P.Z_CURRENT, float),
            ("video_image_width_um", P.VIDEO_CAL_WIDTH_UM, float),
            ("video_image_height_um", P.VIDEO_CAL_HEIGHT_UM, float),
        ]
        for key, path, conv in reads:
            ok, v = self._try_get(cl, path)
            if ok and v is not None:
                out[key] = conv(v) if conv else v
        for key, path in (("grating", P.spectrograph_path(sg, "Grating")),
                          ("spectral_unit", P.spectrograph_path(sg, "SpectralUnit"))):
            if cl.exists(path):
                try:
                    out[key] = cl.get_enum(path)[1]
                except _LINK_ERRORS:
                    raise
                except BucsError:
                    pass
        if self._version:
            out["witec_version"] = self._version
        return out

    def _refresh_laser_line(self, cl: BucsClient) -> float | None:
        ok, v = self._try_get(cl, P.LASER_WAVELENGTH)
        if ok and v:
            self.laser_line_nm = float(v)
        return self.laser_line_nm

    # ═════════════════════════════════════════════════════════════════════════════════════
    #  Spectrometer
    # ═════════════════════════════════════════════════════════════════════════════════════

    def default_timeout_s(self, exposure_ms: float | None, frames: int | None) -> float:
        exp = exposure_ms if exposure_ms is not None else 1000.0
        n = frames if frames is not None else 1
        return exp / 1000.0 * n * 1.5 + 60.0

    def _gui_settings(self, cl: BucsClient) -> tuple[float | None, int | None]:
        """WITec's own single-spectrum integration time (ms) and accumulations, as set in its GUI."""
        ok1, it = self._try_get(cl, P.SINGLE_SPECTRUM_INTEGRATION)
        ok2, acc = self._try_get(cl, P.SINGLE_SPECTRUM_ACCUMULATIONS)
        return (float(it) * 1000.0 if ok1 and it else None), (int(acc) if ok2 and acc else None)

    def _unavailable_status(self, exc: BaseException) -> dict:
        return {"state": "unavailable", "is_running": False, "connected": False, "witec": True,
                "error": str(exc), "exposure_ms": self._settings.get("exposure_ms"),
                "frames": self._settings.get("frames"), "grating": None, "center_wavelength_nm": None,
                "exit_port": None, "temperature_c": None, "temperature_locked": None,
                "operator_hold": False, "laser_line_nm": self.laser_line_nm, "sim": False}

    def _status_raw(self) -> dict:
        try:
            cl = self._ready()
            active = bool(cl.get(P.IS_SEQUENCER_ACTIVE))
            gui_exp, gui_acc = self._gui_settings(cl)
            sg = self.cfg.spectrograph
            grating = center = None
            gp = P.spectrograph_path(sg, "Grating")
            if cl.exists(gp):
                try:
                    grating = cl.get_enum(gp)[1]
                except _LINK_ERRORS:
                    raise
                except BucsError:
                    pass
            center = self._opt(cl, P.spectrograph_path(sg, "CenterWavelength"))
            line = self._refresh_laser_line(cl)
            write = cl.has_write_access()
        except _LINK_ERRORS as exc:
            return self._unavailable_status(exc)
        st = {"state": "busy" if active else "ready", "is_running": active, "connected": True,
              "witec": True, "sim": False, "witec_version": self._version,
              "exposure_ms": self._settings.get("exposure_ms", gui_exp),
              "frames": self._settings.get("frames", gui_acc),
              "witec_gui_exposure_ms": gui_exp, "witec_gui_accumulations": gui_acc,
              "grating": grating, "center_wavelength_nm": float(center) if center is not None else None,
              "exit_port": None, "temperature_c": None, "temperature_locked": None,
              "operator_hold": False, "laser_line_nm": line, "write_access": bool(write)}
        if active:
            st.update({f"sequencer_{k}": v for k, v in self._activity(cl).items()})
        return st

    def _wavelength_axis_raw(self):
        return self._last_axis

    def _configure_raw(self, settings: dict) -> dict:
        unsupported = sorted(set(settings) - {"exposure_ms", "frames"})
        if unsupported:
            raise OperationNotSupported(
                f"{self.device_id}: {unsupported} cannot be configured through WITec's COM interface "
                "(only exposure_ms and frames); camera readout/binning belong to WITec Control's saved states")
        self._settings.update(settings)
        return dict(self._settings)

    def _stop_raw(self) -> None:
        cl = self._ready(wait_s=3.0)
        with cl.call_timeout(5.0):
            cl.trigger(P.STOP_SEQUENCER)

    def _set_exit_port_raw(self, port: str) -> dict:
        raise OperationNotSupported(
            f"{self.device_id}: WITec has no exit-port switch the COM interface exposes "
            "(detector output is part of the saved states)")

    def _set_center_raw(self, nm: float) -> dict:
        cl = self._ready()
        path = P.spectrograph_path(self.cfg.spectrograph, "CenterWavelength")
        self._need(cl, "center_wavelength")
        self._require_write("set centre wavelength")
        with self._mic_lock:
            cl.set(path, float(nm))
            self._wait(self.cfg.spectrograph_settle_s, None, "centre wavelength settle")
            deadline = self._now() + self.cfg.spectrograph_timeout_s
            while True:
                cur = float(cl.get(path))
                if abs(cur - nm) <= 0.05:
                    break
                if self._now() >= deadline:
                    raise MotionTimeout(f"{self.device_id}: centre wavelength reads {cur} nm after "
                                        f"{self.cfg.spectrograph_timeout_s:.0f} s, wanted {nm}")
                self._sleep(self.cfg.poll_s)
        return {"center_wavelength_nm": cur}

    def _match_grating(self, labels: dict[int, str], grating: str) -> int:
        g = str(grating).strip()
        by_label = [i for i, lab in labels.items() if lab.strip().casefold() == g.casefold()]
        if len(by_label) == 1:
            return by_label[0]
        digits = g.lstrip("Gg")
        cands = [i for i, lab in labels.items()
                 if lab.casefold().startswith(f"g{digits}:") or lab.casefold().startswith(f"{digits}:")] \
            if digits.isdigit() else []
        if not cands:
            token = digits.replace("g/mm", "").strip()
            cands = [i for i, lab in labels.items() if token.isdigit() and f"{token} g/mm" in lab.casefold()]
        if len(cands) == 1:
            return cands[0]
        raise SpectrometerLimitError(
            f"{self.device_id}: grating {grating!r} does not identify exactly one of {list(labels.values())}")

    def _set_grating_raw(self, grating: str) -> dict:
        cl = self._ready()
        path = P.spectrograph_path(self.cfg.spectrograph, "Grating")
        self._need(cl, "grating")
        labels = cl.enum_values(path)
        idx = self._match_grating(labels, grating)
        self._require_write("set grating")
        with self._mic_lock:
            cl.set(path, idx)
            self._wait(self.cfg.spectrograph_settle_s, None, "grating settle")
            cur_idx, cur_label = cl.get_enum(path)
            if cur_idx != idx:
                raise InstrumentError(f"{self.device_id}: grating reads {cur_label!r} after the change, "
                                      f"wanted {labels[idx]!r}")
        return {"grating": cur_label}

    # -- the acquisition ---------------------------------------------------------------------

    def _raw_text_path(self, base_name: str | None) -> Path:
        if self.cfg.raw_dir:
            d = Path(self.cfg.raw_dir)
        else:
            from mast._runtime_paths import project_root

            d = project_root() / "artifacts" / "witec" / "truematch" / datetime.now().strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        stem = "".join(c for c in (base_name or "") if c.isalnum() or c in "-_")[:60] \
            or "witec_" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        return d / f"{stem}.truematch.txt"

    @staticmethod
    def _write_text(path: Path, text: str) -> dict:
        data = text.encode("utf-8")
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                "kind": "witec_truematch_ascii"}

    def _acquire_raw(self, *, exposure_ms: float | None, frames: int | None, save: bool,
                     base_name: str | None, tag: str | None, meta: dict, timeout_s: float,
                     check_abort: AbortCheck | None, on_frame: FrameCallback | None) -> AcquisitionResult:
        cl = self._ready()
        self._need(cl, "silent_spectrum")
        self._require_write("acquire a spectrum")
        gui_exp, gui_acc = self._gui_settings(cl)
        exp_ms = exposure_ms if exposure_ms is not None else self._settings.get("exposure_ms", gui_exp)
        n = frames if frames is not None else self._settings.get("frames", gui_acc)
        if exp_ms is None or n is None:
            raise InstrumentError(f"{self.device_id}: no exposure / frames given and WITec's own are unreadable")
        exp_ms = self._check_exposure(exp_ms)          # the template only checked the values the caller gave
        n = self._check_frames(n)
        self._check_total(exp_ms, n)
        if exposure_ms is not None:
            self._settings["exposure_ms"] = exp_ms     # LightField behaviour: stays the setting
        if frames is not None:
            self._settings["frames"] = n
        integ_s = exp_ms / 1000.0
        started = utc_now_iso()
        t_start = self._now()
        with self._mic_lock:
            self._refuse_if_busy(cl, "the acquisition")
            self._refuse_while_sweeping(tr("采谱", "an acquisition"))
            may_emit = self._may_emit(cl)
            if may_emit:
                self._require_emission(cl, tr("这次采谱会让激光照到样品上",
                                              "an acquisition would put laser light on the sample"))
            watch_what = tr("本该是暗谱的采谱中，快门被打开了", "the shutter opened during a spectrum that was meant to be dark")
            watched: dict = {}
            before = self._snapshot(cl)
            beam = self._ensure_beam(cl, "raman", check_abort)
            self._aborted(check_abort, "acquisition")
            cl.set(P.SILENT_PARAMETER, f"IntegrationTime {_fmt_s(integ_s)}\n NumberOfAccumulations {n}\n")
            cl.set(P.SILENT_DONE, False)
            cl.trigger(P.SILENT_START)
            stop = lambda: self._stop_sequencer_now(cl)          # noqa: E731
            deadline = t_start + timeout_s
            t_trigger = self._now()
            saw_active = False
            while True:
                self._aborted(check_abort, "acquisition", stop)
                if not may_emit:                    # a dark spectrum: nothing may light the sample while it runs
                    try:
                        self._watch_emission(cl, watch_what, watched)
                    except Exception:
                        self._safe(stop, "stop after a shutter opened in a dark spectrum")
                        raise
                if bool(cl.get(P.SILENT_DONE)):
                    break
                if self._sequencer_active(cl):
                    saw_active = True
                elif saw_active or self._now() - t_trigger > self.cfg.series_start_grace_s:
                    # the sequencer is idle and no spectrum was delivered: somebody stopped it, or it never started
                    if bool(cl.get(P.SILENT_DONE)):
                        break
                    raise InstrumentError(
                        f"{self.device_id}: WITec's sequencer is idle but delivered no spectrum "
                        f"({'it was stopped — by a person at the WITec PC?' if saw_active else 'it did not start'})")
                if self._now() >= deadline:
                    self._safe(stop, "stop after timeout")
                    raise MotionTimeout(f"{self.device_id}: WITec returned no spectrum within {timeout_s:.0f} s "
                                        "(WITec Control may be waiting on a dialog)")
                self._sleep(self.cfg.poll_s)
            err = str(cl.get(P.SILENT_ERROR))
            if err != "Ok":
                raise InstrumentError(f"{self.device_id}: WITec silent spectrum ended with error {err!r}")
            text = str(cl.get(P.SILENT_TEXT))
            files: list[dict] = []
            if save:
                files.append(self._write_text(self._raw_text_path(base_name), text))
            try:
                tm = parse_truematch(text)
                axis_nm = tm.axis_nm()
            except TrueMatchError as exc:
                keep = files[0]["path"] if files else self._write_text(
                    self._raw_text_path((base_name or "unparsed") + "_unparsed"), text)["path"]
                raise InstrumentError(f"{self.device_id}: WITec's spectrum text cannot be read safely: {exc} "
                                      f"(raw text kept at {keep})") from exc
            cleanup_note = None
            try:
                self._wait_idle(cl, timeout_s=self.cfg.sequencer_cleanup_timeout_s, check_abort=None,
                                what="sequencer clean-up", stop_on_trouble=False)
            except MotionTimeout as exc:
                cleanup_note = str(exc)
            after_state = self._opt(cl, P.BEAM_STATE)
        finished = utc_now_iso()
        self._last_axis = axis_nm
        if tm.excitation_nm:
            self.laser_line_nm = tm.excitation_nm
        warnings: list[str] = list(tm.notes)
        if len(tm.frames) != n:
            warnings.append(f"asked for {n} accumulations, the text carries {len(tm.frames)} spectrum block(s)")
        if tm.integration_time_s is not None and abs(tm.integration_time_s - integ_s) > 0.01 * integ_s:
            warnings.append(f"asked for {integ_s} s per accumulation, WITec reports {tm.integration_time_s} s")
        out_meta = {
            "driver": "witec_com", "witec_version": self._version, "exposure_ms": exp_ms, "frames": n,
            "frames_returned": len(tm.frames), "integration_time_s_reported": tm.integration_time_s,
            "excitation_nm": tm.excitation_nm, "x_unit": tm.x_unit, "x_kind": tm.x_kind,
            "x_unit_assumed": tm.x_unit_assumed, "truematch_version": tm.version, "titles": tm.titles,
            "parser_notes": tm.notes, "warnings": warnings, "settings_before": before,
            "beam_path": beam, "beam_path_after": after_state, "sequencer_cleanup": cleanup_note,
            "elapsed_s": round(self._now() - t_start, 3), "tag": tag or "", "base_name": base_name or "",
            **{k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))},
        }
        self.last_acquisition = out_meta
        if on_frame is not None:
            for i, fr in enumerate(tm.frames):
                on_frame(i, fr, axis_nm)
        return AcquisitionResult(frames=list(tm.frames), axis_nm=axis_nm, files=files, started_utc=started,
                                 finished_utc=finished, meta=out_meta)

    # ═════════════════════════════════════════════════════════════════════════════════════
    #  Microscope
    # ═════════════════════════════════════════════════════════════════════════════════════

    # -- write access -----------------------------------------------------------------------

    def _has_write_access_raw(self) -> bool:
        try:
            return bool(self._ready().has_write_access())
        except BucsError:
            return False

    def _request_write_access_raw(self) -> bool:
        cl = self._ready()                                # a closed WITec Control is an explicit error here
        deadline = self._now() + self.cfg.write_access_wait_s
        while True:
            if cl.request_write_access(True):
                return True
            if self._now() >= deadline:
                return False
            self._sleep(min(1.0, max(0.0, deadline - self._now())))

    def request_write_access_waiting(self, timeout_s: float,
                                     check_abort: AbortCheck | None = None) -> bool:
        """Ask for write access, polling once a second for up to *timeout_s* while a person presses
        "COM Automation"; abortable. (``request_write_access`` asks once.)"""
        end = self._now() + max(0.0, timeout_s)
        while True:
            if self.request_write_access():
                return True
            if self._now() >= end:
                return False
            self._aborted(check_abort, "waiting for write access")
            self._sleep(min(1.0, max(0.0, end - self._now())))

    def _release_write_access_raw(self) -> None:
        cl = self._cl
        if cl is None or not cl.connected:
            return
        self._safe(lambda: cl.request_write_access(False), "release write access")

    # -- status ---------------------------------------------------------------------------------

    def _microscope_status_raw(self) -> dict:
        try:
            cl = self._ready()
            st: dict[str, Any] = {"connected": True, "sim": False, "witec": True,
                                  "witec_version": self._version}
            active = bool(cl.get(P.IS_SEQUENCER_ACTIVE))
            st["sequencer_active"] = active
            if active:
                st.update({f"sequencer_{k}": v for k, v in self._activity(cl).items()})
            snap = self._snapshot(cl)
            st.update({
                "beam_path": snap.get("beam_path"), "analyzer_deg": snap.get("analyzer_deg"),
                "analyzer_in": snap.get("analyzer_in"), "laser_wavelength_nm": snap.get("laser_wavelength_nm"),
                "laser_power_mw": snap.get("laser_power_mw"), "shutter_open": snap.get("shutter_open"),
                "state": snap.get("state_name"), "objective": snap.get("objective"),
                "magnification": snap.get("magnification"), "grating": snap.get("grating"),
                "center_wavelength_nm": snap.get("center_wavelength_nm"),
                "stage_um": {"x": snap.get("stage_x_um"), "y": snap.get("stage_y_um"),
                             "z": snap.get("stage_z_um")},
                "spectral_unit": snap.get("spectral_unit"),
            })
            ok, acc = self._try_get(cl, P.COM_ACCEPTS_REMOTE)
            if ok:
                st["com_accepts_remote_connection"] = bool(acc)
            video: dict[str, float] = {}
            for key, path in (("image_width_um", P.VIDEO_CAL_WIDTH_UM), ("image_height_um", P.VIDEO_CAL_HEIGHT_UM),
                              ("rotation_deg", P.VIDEO_CAL_ROTATION_DEG), ("probe_x", P.VIDEO_PROBE_X),
                              ("probe_y", P.VIDEO_PROBE_Y)):
                okv, v = self._try_get(cl, path)
                if okv and v is not None:
                    video[key] = float(v)
            if video:
                st["video"] = video
            laser = self._laser_view(cl)
            if laser:
                st["laser"] = laser.get("name")
            line = snap.get("laser_wavelength_nm")
            if line:
                self.laser_line_nm = line
            st["write_access"] = bool(cl.has_write_access())
            st["emission"] = self._emission_view()
            st["capabilities"] = self.capabilities()
            return st
        except _LINK_ERRORS as exc:
            return {"connected": False, "sim": False, "witec": True, "error": str(exc),
                    "error_type": type(exc).__name__, "sequencer_active": None, "write_access": False,
                    "emission": self._emission_view(),
                    "hint": tr("WITec Control 没开或连不上：请操作员先打开 WITec Control（COM 不会替你启动它）。",
                               "WITec Control is closed or unreachable: a person has to open WITec Control "
                               "(COM cannot start it).")}

    # -- analyzer --------------------------------------------------------------------------------

    @staticmethod
    def _angle_diff(a: float, b: float) -> float:
        return (a - b + 180.0) % 360.0 - 180.0

    def _get_analyzer_raw(self) -> float:
        cl = self._ready()
        self._need(cl, "analyzer")
        return float(cl.get(P.ANALYZER))

    def _set_analyzer_raw(self, deg: float) -> float:
        cl = self._ready()
        self._need(cl, "analyzer")
        before = float(cl.get(P.ANALYZER))
        cl.set(P.ANALYZER, float(deg))
        self._wait(self.cfg.analyzer_settle_s + self.cfg.analyzer_full_turn_s
                   * abs(self._angle_diff(deg, before)) / 360.0, None, "analyzer settle")
        deadline = self._now() + self.cfg.analyzer_timeout_s
        while True:
            cur = float(cl.get(P.ANALYZER))
            if abs(self._angle_diff(cur, deg)) <= self.cfg.analyzer_tol_deg:
                return cur
            if self._now() >= deadline:
                raise MotionTimeout(f"{self.device_id}: analyzer reads {cur}° after "
                                    f"{self.cfg.analyzer_timeout_s:.0f} s, wanted {deg}°")
            self._sleep(self.cfg.poll_s)

    def _set_analyzer_in_raw(self, inserted: bool) -> bool:
        cl = self._ready()
        self._need(cl, "analyzer_coupler")
        cl.set(P.ANALYZER_COUPLER, bool(inserted))
        self._wait(self.cfg.analyzer_settle_s, None, "analyzer coupler settle")
        cur = bool(cl.get(P.ANALYZER_COUPLER))
        if cur != bool(inserted):
            raise InstrumentError(f"{self.device_id}: analyzer coupler reads {cur}, wanted {bool(inserted)}")
        return cur

    # -- beam path ---------------------------------------------------------------------------------

    def _beam_matches(self, state: str | None, path: str) -> bool:
        if state is None:
            return False
        low = state.casefold()
        return any(m in low for m in self.cfg.beam_markers.get(path, (path,)))

    def _ensure_beam(self, cl: BucsClient, path: str, check_abort: AbortCheck | None) -> str | None:
        """Make the beam path *path*; skip the switch if WITec already reports it. Returns the reported state."""
        ok, state = self._try_get(cl, P.BEAM_STATE)
        if ok and self._beam_matches(str(state), path):
            return str(state)
        return self._switch_beam(cl, path, check_abort)

    def _switch_beam(self, cl: BucsClient, path: str, check_abort: AbortCheck | None,
                     watch: str | None = None) -> str | None:
        """Press the beam-path trigger and wait for WITec to report it. With *watch* (the text of the emission being
        guarded) the shutter is looked at on every poll: a switch that is allowed because the shutter was confirmed
        closed must not leave it open by WITec's own doing unless both gates and the cap say so."""
        trig = P.BEAM_TRIGGERS[path]
        if not cl.exists(trig):
            raise OperationNotSupported(f"{self.device_id}: this WITec Control has no beam-path trigger {trig}")
        epoch = self._stop_epoch
        cl.trigger(trig)
        seen: dict = {}
        if not cl.exists(P.BEAM_STATE):
            self._settle_watching(cl, self.cfg.beam_path_settle_s, watch, check_abort)
            return None
        deadline = self._now() + self.cfg.beam_path_timeout_s
        state: str | None = None
        while True:
            self._aborted(check_abort, "beam path switch")
            if self._stop_epoch != epoch:
                raise self._stopping_error(watch or "beam path switch")
            if watch:
                self._watch_emission(cl, watch, seen)
            ok, state = self._try_get(cl, P.BEAM_STATE)
            state = str(state) if ok else None
            if ok and self._beam_matches(state, path):
                if watch:
                    self._watch_emission(cl, watch, seen)
                return state
            if self._now() >= deadline:
                if not ok:           # the state string is unreadable: fall back to the vendor's fixed settle
                    self._settle_watching(cl, self.cfg.beam_path_settle_s, watch, check_abort)
                    return None
                raise InstrumentError(
                    f"{self.device_id}: beam path reads {state!r} {self.cfg.beam_path_timeout_s:.0f} s after "
                    f"SetState{path}; expected one containing {self.cfg.beam_markers.get(path)} "
                    "(fix beam_markers in the device entry if WITec words it differently)")
            self._sleep(self.cfg.poll_s)

    def _set_beam_path_raw(self, path: str) -> str:
        cl = self._ready()
        self._need(cl, "beam_path")
        lights = path in ("raman", "confocal")
        what = tr(f"{path} 光路会让激光照到样品上", f"the {path} beam path puts the laser on the sample")
        # WITec's own laser lights the sample through these paths only when its shutter is open (or may be: unreadable,
        # or a laser with no software shutter). A shutter CONFIRMED closed makes the switch dark, so a protocol that
        # uses an external source needs no WITec emission gate; whether WITec opens the shutter by itself on the
        # switch is watched for below, and answered by the first run on the machine.
        if lights:
            self._refuse_while_sweeping(what)
        gated = lights and self._shutter_state(cl) != "closed"
        if gated:
            self._require_emission(cl, what)
        with self._mic_lock:
            state = self._switch_beam(cl, path, None, watch=what if lights and not gated else None)
        return state if state is not None else path

    # -- lasers -------------------------------------------------------------------------------------

    def _laser_view(self, cl: BucsClient) -> dict | None:
        """The configured laser the live wavelength identifies, or ``None``."""
        ok, wl = self._try_get(cl, P.LASER_WAVELENGTH)
        if not ok or not wl:
            return None
        near = [l for l in self.cfg.lasers if abs(l["wavelength_nm"] - float(wl)) <= self.cfg.laser_match_nm]
        if len(near) != 1:
            return None
        if not self._stopping and not self._selection_lost:      # a stop selects laser after laser: that is nobody's choice
            self._last_selected_index = int(near[0]["index"])
        return dict(near[0])

    def _list_lasers_raw(self) -> list[dict]:
        cl = self._ready()
        ok, wl = self._try_get(cl, P.LASER_WAVELENGTH)
        ok_n, n_live = self._try_get(cl, P.LASER_COUNT)
        sel = self._laser_view(cl)
        out = []
        for l in self.cfg.lasers:
            item = dict(l)
            item["selected"] = bool(sel is not None and sel["index"] == l["index"])
            item["wavelength_verified"] = item["selected"]
            if item["selected"] and ok and wl:
                item["wavelength_nm"] = float(wl)           # trust the live value over the table
            if ok_n and int(n_live) != len(self.cfg.lasers):
                item["table_mismatch"] = f"config lists {len(self.cfg.lasers)} lasers, WITec reports {n_live}"
            out.append(item)
        return out

    def _select_laser_raw(self, index: int) -> None:
        cl = self._ready()
        self._need(cl, "lasers")
        ok_n, n_live = self._try_get(cl, P.LASER_COUNT)
        if ok_n and int(n_live) != len(self.cfg.lasers):
            raise InstrumentError(
                f"{self.device_id}: the 'lasers' table in the device entry lists {len(self.cfg.lasers)} lasers "
                f"but WITec Control reports {n_live} — fix the table before selecting by index")
        path = P.laser_select_path(index)
        if not cl.exists(path):
            raise OperationNotSupported(f"{self.device_id}: this WITec Control has no {path}")
        nominal = next(l["wavelength_nm"] for l in self.cfg.lasers if l["index"] == index)
        what = tr("新选中的激光，它的快门是开着的", "the newly selected laser comes with its shutter open")
        epoch = self._stop_epoch
        with self._mic_lock:
            with self._exclusive_of_stops(epoch, tr("选激光", "selecting a laser")):
                cl.trigger(path)                      # (never while a stop is moving the selection about)
                self._last_selected_index = index
                self._selection_lost = False          # somebody chose a laser explicitly
            # the laser that is selected now may bring an open shutter along (a person left it so): that is light on the
            # sample, judged like any other emission - and shut again, on that laser, when it is not allowed
            self._settle_watching(cl, self.cfg.laser_settle_s, what)
            live = float(cl.get(P.LASER_WAVELENGTH))
            self.laser_line_nm = live
            if self._shutter_state(cl) in ("open", "unknown"):
                try:
                    self._require_emission(cl, what)
                except Exception as exc:
                    self._refuse_and_close(cl, exc)
        if abs(live - nominal) > self.cfg.laser_match_nm:
            raise InstrumentError(
                f"{self.device_id}: SelectLaser{index} now reports {live:.2f} nm but the 'lasers' table says "
                f"{nominal} nm — the table's index order is wrong; fix it. (Power limits use the live "
                "wavelength, so nothing was weakened.)")

    def _set_laser_power_raw(self, mw: float) -> float:
        cl = self._ready()
        self._need(cl, "laser_power")
        epoch = self._stop_epoch
        with self._mic_lock:
            # more light on the sample while the shutter is open (or may be) is an emission like opening it; the cap on
            # the target was checked by the template, a lowering is never gated, a closed shutter is dark preparation
            ok_p, cur = self._try_get(cl, P.LASER_POWER)
            raises = not ok_p or cur is None or float(mw) > float(cur) + 1e-9
            if raises and self._selection_lost:      # which laser would that be? (a stop left the selection nobody's)
                raise MicroscopeLimitError(f"{self.device_id}: raising the power - {self._selection_lost_text()}")
            if raises and self._shutter_state(cl) != "closed":
                self._check_gates(tr(f"把功率提到 {float(mw):g} mW 会让更多光照到样品上",
                                     f"raising the power to {float(mw):g} mW puts more light on the sample"))
            with self._exclusive_of_stops(epoch, tr("调功率", "setting the laser power")):
                cl.set(P.LASER_POWER, float(mw))
            self._wait(self.cfg.power_settle_s, None, "laser power settle")
            if self._stop_epoch != epoch:            # a stop moved the selection: what is read back now is another laser's
                raise self._stopping_error(tr("读回功率", "reading the power back"))
            ok, actual = self._try_get(cl, P.LASER_POWER)
            if not ok:
                return float(mw)
            actual = float(actual)
            sel = [l for l in self.list_lasers() if l.get("selected")]
            if sel:
                cap = self._power_cap(sel[0])
                if actual > cap * 1.02 + 1e-9:
                    closed = self._close_selected_quietly(cl)
                    raise MicroscopeLimitError(
                        f"{self.device_id}: the laser reports {actual} mW, above the {cap} mW cap, after "
                        f"asking for {mw} mW — "
                        + ("shutter closed" if closed else "AND the shutter could NOT be closed: close it by hand in WITec Control"))
            return actual

    def _get_laser_power_raw(self) -> float | None:
        cl = self._ready()
        ok, v = self._try_get(cl, P.LASER_POWER)
        return float(v) if ok and v is not None else None

    def _set_shutter_raw(self, open_: bool) -> bool:
        if not open_:
            return self._close_selected_shutter()
        cl = self._ready()
        self._need(cl, "laser_shutter")
        epoch = self._stop_epoch
        what = tr("打开激光快门", "opening the laser shutter")
        # judged AND written under one guard: a stop's sweep (it selects laser after laser) either finished before this
        # started - then the laser judged here is the laser opened - or is refused here; it cannot slide in between
        with self._exclusive_of_stops(epoch, what):
            self._require_emission(cl, what)
            ok, has = self._try_get(cl, P.LASER_HAS_SHUTTER)
            if ok and has is False:
                raise OperationNotSupported(
                    f"{self.device_id}: the selected laser has no software shutter — block the beam physically")
            self._check_not_stopping(epoch, what)    # a close that is not a sweep may have arrived while this was judged
            cl.set(P.LASER_SHUTTER, True)
            self._sleep(self.cfg.poll_s)
            now_open = bool(cl.get(P.LASER_SHUTTER))
        if not now_open:
            raise InstrumentError(f"{self.device_id}: the shutter reads closed after asking for open")
        return now_open

    # -- saved states ----------------------------------------------------------------------------------

    def _list_states_raw(self) -> list[str]:
        names = list(self.cfg.states)
        try:
            cl = self._ready()
            ok, cur = self._try_get(cl, P.STATE_NAME)
            if ok and cur and str(cur) not in names:
                names.append(str(cur))
        except BucsError:
            pass
        return names

    def _load_state_raw(self, name: str) -> str:
        cl = self._ready()
        self._need(cl, "states")
        what = tr(f"状态 {name!r} 会让激光照到样品上", f"state {name!r} lights the sample")
        epoch = self._stop_epoch
        with self._mic_lock:
            self._refuse_if_busy(cl, "loading a state")
            with self._exclusive_of_stops(epoch, tr("载入状态", "loading a state")):
                if self.emission_refusal() is not None and self._shutter_state(cl) in ("open", "unknown"):
                    self._close_selected_quietly(cl) # the gates are not met: what is open now goes dark BEFORE the load
            cl.set(P.STATE_NAME, name)               # (outside the guard: SetValue may block for as long as the load)
            # a state may select another laser and open its shutter: look every poll, not after the settle time
            self._settle_watching(cl, self.cfg.state_settle_s, what)
            cur = str(cl.get(P.STATE_NAME))
            if cur != name:
                raise InstrumentError(f"{self.device_id}: WITec Control reports state {cur!r} after loading "
                                      f"{name!r} — it did not take the state")
            self._refresh_laser_line(cl)
            self._selection_lost = False             # a state defines its laser: that is an explicit choice
            # judged on the laser the state selected - not the one before it - and fail closed: no cap configured or
            # the power unreadable is a refusal, exactly as for opening the shutter by hand
            if self._shutter_state(cl) in ("open", "unknown"):
                try:
                    self._require_emission(cl, what)
                except Exception as exc:
                    self._refuse_and_close(cl, exc)
            self._audit_power_after_load(cl, name)
        return cur

    def _audit_power_after_load(self, cl: BucsClient, name: str) -> None:
        """A state that leaves the (now selected) laser above its cap is reported even when the shutter is closed."""
        ok, power = self._try_get(cl, P.LASER_POWER)
        if not ok or power is None:
            return
        sel = [l for l in self.list_lasers() if l.get("selected")]
        if not sel:
            return
        try:
            cap = self._power_cap(sel[0])
        except MicroscopeLimitError:
            return                               # no cap configured: opening the shutter is refused for that reason
        if float(power) > cap * 1.02 + 1e-9:
            closed = self._close_selected_quietly(cl)
            raise MicroscopeLimitError(
                f"{self.device_id}: state {name!r} left the {sel[0].get('name')} laser at "
                f"{float(power)} mW, above the {cap} mW cap — "
                + ("shutter closed" if closed else "AND the shutter could NOT be closed: close it by hand in WITec Control")
                + "; lower the power with SetWITecLaserPower (or fix the saved state in WITec Control)")

    # -- stage ----------------------------------------------------------------------------------------------

    def _get_stage_raw(self) -> dict:
        cl = self._ready()
        virtual = self._opt(cl, P.STAGE_IS_VIRTUAL, False)
        out: dict[str, float | None] = {"x": None, "y": None, "z": None}
        if not virtual:
            for axis, path in (("x", P.XY_CURRENT_X), ("y", P.XY_CURRENT_Y), ("z", P.Z_CURRENT)):
                v = self._opt(cl, path)
                out[axis] = float(v) if v is not None else None
        return out

    def _wait_axis(self, cl: BucsClient, *, what: str, state_path: str, stop_paths: tuple[str, ...],
                   read: Callable[[], tuple[float, ...]], target: tuple[float, ...], tol: float,
                   check_abort: AbortCheck | None) -> tuple[float, ...]:
        def stop() -> None:
            for sp in stop_paths:
                self._safe(lambda sp=sp: cl.trigger(sp), f"stop {what}")

        t0 = self._now()
        deadline = t0 + self.cfg.stage_timeout_s
        finished = {P.STAGE_STATE_REACHED, P.STAGE_STATE_IDLE}
        while True:
            self._aborted(check_abort, what, stop)
            state = str(cl.get(state_path))
            pos = read()
            at_target = all(abs(p - t) <= tol for p, t in zip(pos, target))
            if state == P.STAGE_STATE_IN_USE:
                raise InstrumentError(f"{self.device_id}: {what}: axis in use (another move is running)")
            if state == P.STAGE_STATE_FAILED:
                raise InstrumentError(f"{self.device_id}: {what}: WITec reports 'Position not Reached' "
                                      f"(end switch or travel limit); stopped at {pos}")
            if state == P.STAGE_STATE_STOPPED:
                raise MotionAborted(f"{self.device_id}: {what} was stopped manually, at {pos}")
            if state in finished and at_target:
                return pos
            if state in finished and self._now() - t0 > self.cfg.stage_grace_s:
                raise InstrumentError(f"{self.device_id}: {what}: WITec says the move is done ({state!r}) but "
                                      f"the stage is at {pos}, not {target} (±{tol} µm)")
            if self._now() >= deadline:
                self._safe(stop, f"stop {what} after timeout")
                raise MotionTimeout(f"{self.device_id}: {what} not finished within "
                                    f"{self.cfg.stage_timeout_s:.0f} s (state {state!r}, at {pos})")
            self._sleep(self.cfg.stage_poll_s)

    def _move_stage_raw(self, target: dict, check_abort: AbortCheck | None) -> dict:
        cl = self._ready()
        if self._opt(cl, P.STAGE_IS_VIRTUAL, False):
            raise OperationNotSupported(f"{self.device_id}: WITec reports the XYZ stage as virtual (not motorised)")
        if "z" in target:
            ok, trav = self._try_get(cl, P.MAX_Z_STEPPER_TRAVEL)
            if ok and trav is not None and abs(target["z"]) > float(trav) / 2.0 + 1e-9:
                raise MicroscopeLimitError(
                    f"{self.device_id}: stage z={target['z']} µm is outside WITec's own software Z range "
                    f"±{float(trav) / 2.0:g} µm")
        with self._mic_lock:
            self._refuse_if_busy(cl, "the stage move")
            if "x" in target or "y" in target:
                self._need(cl, "stage_xy")
                cx, cy = float(cl.get(P.XY_CURRENT_X)), float(cl.get(P.XY_CURRENT_Y))
                tx, ty = float(target.get("x", cx)), float(target.get("y", cy))
                cl.set(P.XY_DESIRED_X, tx)
                cl.set(P.XY_DESIRED_Y, ty)
                cl.trigger(P.XY_MOVE)
                self._wait_axis(cl, what="XY move", state_path=P.XY_STATE, stop_paths=(P.XY_STOP,),
                                read=lambda: (float(cl.get(P.XY_CURRENT_X)), float(cl.get(P.XY_CURRENT_Y))),
                                target=(tx, ty), tol=self.cfg.stage_tol_xy_um, check_abort=check_abort)
            if "z" in target:
                self._need(cl, "stage_z")
                tz = float(target["z"])
                cl.set(P.Z_DESIRED, tz)
                cl.trigger(P.Z_MOVE)
                self._wait_axis(cl, what="Z move", state_path=P.Z_STATE, stop_paths=(P.Z_STOP,),
                                read=lambda: (float(cl.get(P.Z_CURRENT)),), target=(tz,),
                                tol=self.cfg.stage_tol_z_um, check_abort=check_abort)
        return self._get_stage_raw()

    def _stop_stage_raw(self) -> None:
        cl = self._ready(wait_s=3.0)
        with cl.call_timeout(5.0):
            for sp in (P.XY_STOP, P.Z_STOP):
                self._safe(lambda sp=sp: cl.trigger(sp), f"stop {sp}")

    # -- video / focus ----------------------------------------------------------------------------------------

    def _acquire_video_raw(self, path: str) -> str:
        cl = self._ready()
        self._need(cl, "video_image")
        p = Path(path)
        if not p.suffix:
            p = p.with_suffix(".png")
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            raise InstrumentError(f"{self.device_id}: {p} already exists — pick a new name")
        with self._mic_lock:
            self._refuse_if_busy(cl, "the video image")
            self._ensure_beam(cl, "video", None)
            cl.set(P.VIDEO_FILE_NAME, str(p))
            cl.trigger(P.VIDEO_TO_FILE)
            deadline = self._now() + self.cfg.video_timeout_s
            last = -1
            while True:
                size = p.stat().st_size if p.exists() else 0
                if size > 0 and size == last:
                    return str(p)
                last = size
                if self._now() >= deadline:
                    raise MotionTimeout(f"{self.device_id}: WITec wrote no video image to {p} within "
                                        f"{self.cfg.video_timeout_s:.0f} s")
                self._sleep(self.cfg.poll_s)

    def _autofocus_raw(self, check_abort: AbortCheck | None) -> dict:
        cl = self._ready()
        self._need(cl, "autofocus")
        with self._mic_lock:
            self._refuse_if_busy(cl, "autofocus")
            z_before = self._opt(cl, P.Z_CURRENT)

            def stop() -> None:
                self._safe(lambda: cl.trigger(P.Z_STOP), "stop Z")
                self._stop_sequencer_now(cl)

            self._ensure_beam(cl, "video", check_abort)               # the autofocus looks at the video image
            cl.trigger(P.AUTOFOCUS_EXECUTE)
            verified = cl.exists(P.AUTOFOCUS_STATUS)
            ok = True
            if verified:
                t0 = self._now()
                deadline = t0 + self.cfg.autofocus_timeout_s
                saw_running = False
                while True:
                    self._aborted(check_abort, "autofocus", stop)
                    status = int(cl.get(P.AUTOFOCUS_STATUS))
                    if status == 0:
                        saw_running = True
                    elif saw_running or self._now() - t0 > 2.0:
                        ok = status == 1
                        break
                    if self._now() >= deadline:
                        stop()
                        raise MotionTimeout(f"{self.device_id}: autofocus still running after "
                                            f"{self.cfg.autofocus_timeout_s:.0f} s")
                    self._sleep(self.cfg.poll_s)
            else:
                self._wait(5.0, check_abort, "autofocus", stop)         # the vendor SDK's own fixed wait
            z_after = self._opt(cl, P.Z_CURRENT)
        res = {"z_um": float(z_after) if z_after is not None else None,
               "z_before_um": float(z_before) if z_before is not None else None,
               "ok": ok, "verified": verified}
        zr = self.microscope_limits.z_um
        if res["z_um"] is not None and zr is not None and not zr[0] <= res["z_um"] <= zr[1]:
            raise MicroscopeLimitError(
                f"{self.device_id}: autofocus ended at z={res['z_um']} µm, outside the configured "
                f"[{zr[0]}, {zr[1]}] µm — the stage is there now; check before measuring")
        return res

    # -- analyzer series ---------------------------------------------------------------------------------------

    def check_analyzer_series(self, *, start_deg: Any, stop_deg: Any, points: Any, integration_s: Any,
                              accumulations: Any = 1) -> dict:
        """Validate a series request exactly as :meth:`run_analyzer_series` would (dry runs) and estimate its
        duration. Raises the same limit errors; touches no hardware."""
        lim = self.microscope_limits
        a0, a1 = float(start_deg), float(stop_deg)
        for a in (a0, a1):
            if not math.isfinite(a) or not lim.analyzer_deg[0] <= a <= lim.analyzer_deg[1]:
                raise MicroscopeLimitError(f"analyzer angle {a}° outside {list(lim.analyzer_deg)}")
        n = int(points)
        if not 2 <= n <= lim.max_series_points:
            raise MicroscopeLimitError(f"points={n} outside [2, {lim.max_series_points}]")
        t = float(integration_s)
        if not 0 < t <= lim.max_integration_s:
            raise MicroscopeLimitError(f"integration_s={t} outside (0, {lim.max_integration_s}]")
        acc = int(accumulations)
        if acc < 1:
            raise MicroscopeLimitError("accumulations must be ≥ 1")
        if abs(a0 - 0.0) > 1e-6 or abs(a1 - 360.0) > 1e-6:
            raise OperationNotSupported(
                "WITec's analyzer series is always one full 0–360° rotation in N steps; for a partial range "
                "use SetAnalyzerAngle + AcquireSpectrum at each angle")
        exposure_total = n * t * acc
        if exposure_total > self.limits.max_total_s:
            raise SpectrometerLimitError(
                f"{self.device_id}: {n} points × {acc} × {t} s = {exposure_total:.0f} s exposure exceeds the "
                f"ceiling of {self.limits.max_total_s:.0f} s")
        est = n * (t * acc + self.cfg.series_point_overhead_s)
        return {"points": n, "integration_s": t, "accumulations": acc, "exposure_total_s": exposure_total,
                "estimated_s": est, "angles_deg": np.linspace(0.0, 360.0, n).tolist()}

    def _run_analyzer_series_raw(self, start_deg: float, stop_deg: float, points: int,
                                 integration_s: float, accumulations: int, name: str,
                                 check_abort: AbortCheck | None) -> dict:
        plan = self.check_analyzer_series(start_deg=start_deg, stop_deg=stop_deg, points=points,
                                          integration_s=integration_s, accumulations=accumulations)
        cl = self._ready()
        self._need(cl, "analyzer_series")
        self._need(cl, "analyzer")
        with self._mic_lock:
            self._refuse_if_busy(cl, "the analyzer series")
            self._refuse_while_sweeping(tr("检偏角系列", "an analyzer series"))
            self._require_emission(cl, tr("检偏角系列要在激光照着样品时测量",
                                          "an analyzer series measures with the laser on the sample"))
            ok, coupled = self._try_get(cl, P.ANALYZER_COUPLER)
            if ok and coupled is False:
                raise InstrumentError(
                    f"{self.device_id}: the analyzer is not in the beam path (AnalyzerCoupler is off) — "
                    "run SetAnalyzerAngle with coupled=true first, or the series rotates nothing useful")
            cl.set(P.SERIES_INTEGRATION, float(integration_s))
            cl.set(P.SERIES_ACCUMULATIONS, int(accumulations))
            cl.set(P.ANALYZER_SERIES_STEPS, int(points))
            t0 = self._now()
            timeout = plan["estimated_s"] * 1.5 + 60.0
            stop = lambda: self._stop_sequencer_now(cl)                  # noqa: E731
            cl.trigger(P.ANALYZER_SERIES_START)
            saw_active = False
            start_unobserved = False
            while True:
                self._aborted(check_abort, "analyzer series", stop)
                active = self._sequencer_active(cl)
                if active:
                    saw_active = True
                elif saw_active:
                    break
                elif self._now() - t0 > self.cfg.series_start_grace_s:
                    # Never saw the sequencer run. A series that cannot finish inside the grace window has not
                    # started. Only a tiny series may legitimately be over already — and even then "Finished" may be
                    # the previous run's result, so the result says the start was not observed.
                    okr, res = self._try_get(cl, P.SEQUENCER_STATUS_OR_RESULT)
                    if plan["exposure_total_s"] <= self.cfg.series_start_grace_s and okr                             and str(res).startswith("Finished"):
                        start_unobserved = True
                        break
                    raise InstrumentError(
                        f"{self.device_id}: the analyzer series did not start within "
                        f"{self.cfg.series_start_grace_s:.0f} s (WITec Control may be showing a dialog)")
                if self._now() - t0 >= timeout:
                    self._safe(stop, "stop after timeout")
                    raise MotionTimeout(f"{self.device_id}: analyzer series not finished within {timeout:.0f} s")
                self._sleep(self.cfg.poll_s)
            okr, res = self._try_get(cl, P.SEQUENCER_STATUS_OR_RESULT)
        status, _, detail = (str(res).partition("|") if okr else ("", "", ""))
        if status == "Error":
            raise InstrumentError(f"{self.device_id}: the analyzer series ended with an error: {detail or res}")
        if status == "StoppedByUser":
            raise MotionAborted(f"{self.device_id}: the analyzer series was stopped (by a person at the WITec PC?)")
        return {"name": name, "points": plan["points"], "angles_deg": plan["angles_deg"],
                "integration_s": plan["integration_s"], "accumulations": plan["accumulations"],
                "status": status or None, "detail": detail or None,
                "elapsed_s": round(self._now() - t0, 3), "saved_in_project": False,
                "start_observed": not start_unobserved,
                "note": "the data are in WITec Control's open project — save it with SaveWITecProject"}

    # -- project ---------------------------------------------------------------------------------------------------

    _BAD_NAME = set('<>:"/\\|?*')

    def save_project(self, directory: str | Path, name: str, *, clear_after_store: bool = False) -> str:
        """Store WITec Control's open project as ``<directory>/<name>.wip`` (WITec never overwrites).
        ``clear_after_store`` also empties the project afterwards (the vendor SDK's default; here off)."""
        nm = str(name or "").strip()
        if nm.lower().endswith(".wip"):
            nm = nm[:-4]
        if not nm:
            raise MicroscopeLimitError("project name must not be empty")
        if any(c in self._BAD_NAME for c in nm) or nm in (".", ".."):
            raise MicroscopeLimitError(f"project name {name!r} contains characters a file name cannot have")
        target_dir = check_output_dir(directory)         # only below the data roots; refused before WITec is asked
        self._require_write("save project")
        with self._mic_lock:
            return self._save_project_raw(str(target_dir), nm, clear_after_store=clear_after_store)

    def _save_project_raw(self, directory: str, name: str, clear_after_store: bool = False) -> str:
        cl = self._ready()
        self._need(cl, "project_save")
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        target = d / f"{name}.wip"
        if target.exists():
            raise InstrumentError(f"{self.device_id}: {target} already exists — WITec would not overwrite it; "
                                  "choose another name")
        keep = {p: self._opt(cl, p) for p in (P.AUTOSAVE_START_DIR, P.AUTOSAVE_FILE_NAME, P.AUTOSAVE_DIR_MODE,
                                              P.AUTOSAVE_STORE_MODE, P.AUTOSAVE_OVERWRITE_MODE)
                if cl.exists(p)}
        try:
            cl.set(P.AUTOSAVE_START_DIR, str(d))
            for path, val in ((P.AUTOSAVE_DIR_MODE, 0), (P.AUTOSAVE_STORE_MODE, 1 if clear_after_store else 0),
                              (P.AUTOSAVE_OVERWRITE_MODE, 0)):
                if cl.exists(path):
                    cl.set(path, val)
            cl.set(P.AUTOSAVE_FILE_NAME, name)
            cl.trigger(P.AUTOSAVE_STORE)
            deadline = self._now() + self.cfg.project_timeout_s
            last = -1
            while True:
                size = target.stat().st_size if target.exists() else 0
                if size > 0 and size == last:
                    break
                last = size
                if self._now() >= deadline:
                    raise MotionTimeout(f"{self.device_id}: WITec wrote no project file {target} within "
                                        f"{self.cfg.project_timeout_s:.0f} s (empty project? folder not writable "
                                        "for WITec Control?)")
                self._sleep(self.cfg.poll_s)
        finally:
            for path, val in keep.items():
                if val is not None:
                    self._safe(lambda path=path, val=val: self._restore(cl, path, val), f"restore {path}")
        return str(target)

    @staticmethod
    def _restore(cl: BucsClient, path: str, value: Any) -> None:
        info = cl.describe(path)
        cl.set(path, bool(value) if info.kind == "bool" else value)

    # -- stop -------------------------------------------------------------------------------------------------------------

    def _stop_all_raw(self) -> None:
        """Shutters closed (the selected laser's first, then every other laser's), sequencer stopped, stage stopped -
        each part attempted whatever the others do. Write access is asked for silently first: the stop has to work
        after a release as long as the person's grant stands. Without write access WITec Control refuses the writes;
        that is recorded in :attr:`last_stop_report` (and logged), not raised: a person at the WITec PC still has the
        GUI, and the report says "not closed"."""
        try:
            cl = self._ready(wait_s=3.0)
        except Exception as exc:  # noqa: BLE001
            self._last_stop = {"connected": False, "error": str(exc), "shutter_confirmed": False}
            raise
        try:
            self._last_stop = self._make_safe(cl, stage=True)
        except Exception as exc:  # noqa: BLE001 - never leave the report of an EARLIER stop standing as this one's verdict
            self._last_stop = {"error": f"{type(exc).__name__}: {exc}", "shutter_confirmed": False,
                               "shutter": f"not closed: the stop failed before it could report ({exc})"}
            raise

    @property
    def last_stop_report(self) -> dict:
        return dict(self._last_stop)


# ── registry entry point ────────────────────────────────────────────────────────────────

_client_factory: Callable[[dict], BucsClient] | None = None


def set_client_factory(factory: Callable[[dict], BucsClient] | None) -> Callable[[dict], BucsClient] | None:
    """Test hook: make :func:`build` create its client with ``factory(entry)`` instead of the comtypes one.
    Returns the previous factory (``None`` restores the default)."""
    global _client_factory
    old, _client_factory = _client_factory, factory
    return old


def get_client_factory() -> Callable[[dict], BucsClient] | None:
    return _client_factory


def _default_client(entry: dict, cfg: WITecConfig) -> BucsClient:
    from mast.instruments.witec.comtypes_client import ComtypesBucsClient

    return ComtypesBucsClient(machine=cfg.machine, call_timeout_s=cfg.call_timeout_s,
                              connect_timeout_s=cfg.connect_timeout_s, gen_dir=cfg.gen_dir,
                              name=str(entry.get("id") or "witec"))


def build(entry: dict) -> WITecMicroscope:
    """``registry`` factory for ``witec_com`` entries. Reads the config only — no hardware, no comtypes import."""
    cfg = WITecConfig.from_entry(entry)
    dev_id = str(entry.get("id") or "witec")
    unknown = sorted(set(entry) - _ENTRY_KEYS - set(WITecConfig.__dataclass_fields__))
    if unknown:
        logger.warning("witec_com %s: unknown key(s) %s in the device entry are ignored (typo?)", dev_id, unknown)
    factory = _client_factory
    make = (lambda: factory(entry)) if factory is not None else (lambda: _default_client(entry, cfg))
    mlim = entry.get("microscope_limits")
    return WITecMicroscope(
        device_id=dev_id, name=str(entry.get("name") or dev_id),
        limits=SpectrometerLimits.from_dict(entry.get("limits")),
        microscope_limits=MicroscopeLimits.from_dict(mlim) if mlim else None,
        config=cfg, client_factory=make, emission_guard=default_emission_guard(cfg))
