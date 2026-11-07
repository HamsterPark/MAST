"""FakeBucsClient — an in-memory WITec Control (alpha300 + UHTS600S) for tests and offline runs.

It models the *semantics* the driver has to survive, not WITec's internals:

* the parameter tree: types (int/float/bool/string/enum/trigger/status), enum labels, the ranges a
  parameter declares, ``Enabled``;
* **write access is handed over by a person**: ``request_write_access(True)`` succeeds only after
  :meth:`operator_press_com_automation`; :meth:`operator_revoke` takes it away mid-run; the GUI is
  "locked" while it is held;
* **WITec Control may not be running** (:meth:`witec_exit`, ``witec_running=False``) → ``BucsNotRunning`` /
  ``BucsConnectionLost``; after a restart the person has to press the button again;
* the sequencer busy flag (``IsASequencerActive``) and ``CurrentActivity`` (which has no value while idle);
* the **silent spectrum** protocol: parameter string → ``StartSilentSpectrumAcquisition`` → ``SequenceDone`` →
  ``SilentSpectrumAsText`` (a real WITEC_TRUEMATCH_ASCII text) while the sequencer stays busy for a clean-up
  phase;
* stage moves with ``State`` strings and travel limits, autofocus, analyzer series, video image, project
  save, state presets, laser selection (the shutter belongs to the selected laser), beam path;
* time: pass ``clock=VirtualClock().now`` and sleep on it, and minute-long operations cost nothing.

Everything a test may want to poke is public: ``log`` (every get/set/trigger), ``hang_sequencer``,
``inject(path, exc)``, ``human_sets(path, value)``, ``laser_table``, ``presets`` …
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from mast.instruments.witec import params as P
from mast.instruments.witec.bucs import (BucsClient, BucsConnectionLost, BucsError, BucsNoValue,
                                          BucsNotRunning, ParamInfo)
from mast.instruments.witec.truematch import format_truematch

__all__ = ["FakeBucsClient", "VirtualClock", "DEFAULT_LASERS", "DEFAULT_PRESETS", "PNG_1X1"]

#: a valid 1×1 PNG, what the fake "video camera" writes
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8ffff3f0005fe02fea7d6a1b50000000049454e44ae426082")

#: ASSUMED order of ``SelectLaser0..4`` (COM lists the five entries but not their index order).
DEFAULT_LASERS: tuple[dict, ...] = (
    {"name": "355 nm", "wavelength_nm": 354.855, "has_shutter": False, "power_readable": False},
    {"name": "532 nm", "wavelength_nm": 532.138, "has_shutter": True, "power_readable": True,
     "power_mw": 1.0},
    {"name": "633 nm", "wavelength_nm": 633.0, "has_shutter": True, "power_readable": True,
     "power_mw": 1.0},
    {"name": "1064 nm", "wavelength_nm": 1064.0, "has_shutter": True, "power_readable": False},
    {"name": "white light", "wavelength_nm": 2600.0, "has_shutter": False, "power_readable": False},
)

DEFAULT_PRESETS: dict[str, dict] = {
    "RayShield 532 - G300": {"laser": 1, "grating": 0, "center_nm": 547.0},
    "RayShield 532 - G600": {"laser": 1, "grating": 1, "center_nm": 547.0},
    "RayShield 532 - G1800": {"laser": 1, "grating": 2, "center_nm": 547.0},
    "Raman 633 - G300": {"laser": 2, "grating": 0, "center_nm": 664.55},
    "Raman 355 - G300": {"laser": 0, "grating": 0, "center_nm": 380.0},
    "Raman 355 - G600": {"laser": 0, "grating": 1, "center_nm": 380.0},
    "SHG": {"laser": 3, "grating": 0, "center_nm": 532.0},
    "variable wavelength mode": {"laser": 3, "grating": 0, "center_nm": 600.0},
    "Unnamed State": {"laser": 1, "grating": 0, "center_nm": 547.0},
}

_GRATINGS = {0: "G1: 300 g/mm BLZ=500nm", 1: "G2: 600 g/mm BLZ=500nm", 2: "G3: 1800 g/mm BLZ=500nm"}
_DISPERSION_NM_PX = {0: 0.15, 1: 0.075, 2: 0.025}
_UNITS = {0: "nm", 1: "µm", 2: "1/cm", 3: "rel. 1/cm", 4: "eV", 5: "meV", 6: "rel. eV", 7: "rel. meV"}
_BEAM_STATE_TEXT = {"raman": "Raman", "video": "Video", "all_off": "AllOff", "confocal": "Confocal",
                    "white_light": "WhiteLightMeasurement"}
_XKIND = {0: "Wavelength [nm]", 1: "Wavelength [µm]", 2: "Wavenumber [1/cm]",
          3: "Raman Shift [rel. 1/cm]", 4: "Energy [eV]", 5: "Energy [meV]",
          6: "Raman Shift [rel. eV]", 7: "Raman Shift [rel. meV]"}


class VirtualClock:
    """``now()`` / ``sleep()`` on a number that only moves when somebody sleeps on it."""

    def __init__(self, start: float = 1000.0):
        self._t = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._t += max(0.0, float(seconds))

    advance = sleep


@dataclass
class _FP:
    path: str
    kind: str
    dtype: str
    value: Any = None
    lo: float | None = None
    hi: float | None = None
    enum: dict[int, str] | None = None
    enabled: bool = True
    getter: Callable[[], Any] | None = None
    on_set: Callable[[Any], None] | None = None
    on_trigger: Callable[[], None] | None = None


@dataclass
class _Seq:
    name: str
    t0: float
    busy_s: float
    cleanup_s: float
    on_data: Callable[[], None] | None = None
    on_end: Callable[[], None] | None = None
    data_done: bool = False


class FakeBucsClient(BucsClient):
    """See the module docstring."""

    def __init__(self, *, clock: Callable[[], float] | None = None, witec_running: bool = True,
                 operator_allows_write: bool = False,
                 lasers: Iterable[dict] = DEFAULT_LASERS,
                 presets: dict[str, dict] | None = None,
                 version: str = "WITec Control, 7.0 Release, Build: 7.0.5.153",
                 omit: Iterable[str] = (), work_dir: str | Path | None = None,
                 silent_acquire_s: float = 0.05, silent_cleanup_s: float = 0.02,
                 stage_move_s: float = 0.05, focus_s: float = 0.05, series_point_s: float = 0.02,
                 beam_switch_s: float = 0.0, pixels: int = 1340, seed: int = 0,
                 reject_unknown_state: bool = False):
        super().__init__()
        self._clock = clock or time.monotonic
        self._lk = threading.RLock()
        self._rng = np.random.default_rng(seed)
        # world state --------------------------------------------------------------
        self.running = bool(witec_running)
        self._allowed = bool(operator_allows_write)        # a person pressed "COM Automation"
        self._connected = False
        self._holds_write = False
        self.version = version
        self.laser_table = [dict(x) for x in lasers]
        self.presets = {k: dict(v) for k, v in (presets or DEFAULT_PRESETS).items()}
        self.reject_unknown_state = bool(reject_unknown_state)
        self.work_dir = Path(work_dir) if work_dir is not None else None
        self.silent_acquire_s, self.silent_cleanup_s = float(silent_acquire_s), float(silent_cleanup_s)
        self.stage_move_s, self.focus_s = float(stage_move_s), float(focus_s)
        self.series_point_s, self.beam_switch_s = float(series_point_s), float(beam_switch_s)
        self.pixels = int(pixels)
        # behaviour switches tests flip
        self.hang_sequencer = False
        self.select_closes_previous_shutter = False   # unknown on real hardware; the driver must work both ways
        self.min_integration_s = 0.0           # WITec silently raises a too-short integration time to this
        self.truematch_trailer: str | None = None
        self.focus_ok = True
        self.focus_z_um = 12.5
        self.xy_limit_um = 25000.0
        self.store_error = ""
        # bookkeeping tests read
        self.log: list[tuple[str, str, Any]] = []
        self.series_runs: list[dict] = []
        self.silent_requests: list[dict] = []
        self.saved_projects: list[str] = []
        self.video_files: list[str] = []
        self.connects = 0
        self.connect_attempts = 0
        self._inject: dict[tuple[str, str], list[Exception]] = {}
        # dynamic state ------------------------------------------------------------
        self._seq: _Seq | None = None
        self._last_result = "Finished"
        self._silent_text = ""
        self._silent_done = False
        self._silent_error = "Ok"
        self._selected = 1
        self._shutter = [False] * len(self.laser_table)
        self._power = [float(x.get("power_mw", 0.0)) for x in self.laser_table]
        self._state_name = "RayShield 532 - G300"
        self._beam = "Video"
        self._beam_pending: tuple[float, str] | None = None
        self._xy = {"cur": [0.0, 0.0], "state": "", "move": None}
        self._z = {"cur": 0.0, "state": "", "move": None}
        self._focus: dict | None = None
        self._focus_status = 1
        self._current_file = ""
        self._tree = self._build_tree()
        for token in omit:
            self.remove(token)

    # ── test controls ──────────────────────────────────────────────────────────────────

    def operator_press_com_automation(self) -> None:
        """A person presses the "COM Automation" button: write access may now be granted."""
        with self._lk:
            self._allowed = True

    def operator_revoke(self) -> None:
        """A person presses "Revoke": write access is gone, mid-run if need be."""
        with self._lk:
            self._allowed = False
            self._holds_write = False

    def witec_exit(self) -> None:
        """WITec Control exits or crashes. Consent does not survive a restart."""
        with self._lk:
            self.running = False
            self._allowed = False
            self._holds_write = False
            self._seq = None

    def witec_start(self) -> None:
        with self._lk:
            self.running = True

    @property
    def gui_locked(self) -> bool:
        return self._allowed

    def human_sets(self, path: str, value: Any) -> None:
        """Somebody changes a value in the WITec GUI (no guard, no log). For a value that lives elsewhere (a
        getter plus a setter hook: laser shutter, power, state name) the hook applies it, so what the getter
        reports really changes; a getter without a hook keeps ignoring the stored value, as before."""
        with self._lk:
            f = self._tree[path]
            if f.getter is not None and f.on_set is not None:
                f.on_set(value)
            else:
                f.value = value

    def peek(self, path: str) -> Any:
        """The stored value, without logging (assertions)."""
        with self._lk:
            p = self._tree[path]
            return p.getter() if p.getter else p.value

    def poke(self, path: str, value: Any) -> None:
        self.human_sets(path, value)

    def inject(self, path: str, exc: Exception, ops: Iterable[str] = ("get", "set", "trigger"),
               times: int = 1) -> None:
        """Make the next ``times`` matching operations raise *exc*."""
        for op in ops:
            self._inject.setdefault((op, path), []).extend([exc] * times)

    def disable(self, path: str, enabled: bool = False) -> None:
        self._tree[path].enabled = enabled

    def remove(self, token: str) -> None:
        """Remove a path, or a whole feature (a key of :data:`params.FEATURE_PATHS`), from the tree."""
        names = P.FEATURE_PATHS.get(token, (token,))
        for n in names:
            self._tree.pop(n, None)
        self._tbl = None

    @property
    def writes(self) -> list[tuple[str, str, Any]]:
        return [e for e in self.log if e[0] in ("set", "trigger")]

    def triggers(self) -> list[str]:
        return [e[1] for e in self.log if e[0] == "trigger"]

    # ── BucsClient primitives ─────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    def _connect_raw(self) -> None:
        with self._lk:
            self.connect_attempts += 1
            if not self.running:
                raise BucsNotRunning("WITec Control is not running (fake)")
            self._connected = True
            self.connects += 1

    def _close_raw(self) -> None:
        with self._lk:
            self._holds_write = False
            self._connected = False

    def _alive(self) -> None:
        if not self.running:
            self._connected = False
            self._holds_write = False
            raise BucsConnectionLost("connection to WITec Control lost (fake)")
        if not self._connected:
            raise BucsError("fake: not connected")

    def _has_read_access_raw(self) -> bool:
        with self._lk:
            self._alive()
            return True

    def _has_write_access_raw(self) -> bool:
        with self._lk:
            self._alive()
            return self._holds_write and self._allowed

    def _request_write_access_raw(self, on: bool) -> None:
        with self._lk:
            self._alive()
            self.log.append(("request_write", "", bool(on)))
            self._holds_write = bool(on) and self._allowed

    def _load_table(self) -> dict[str, ParamInfo]:
        with self._lk:
            self._alive()
            return {p: ParamInfo(p, f.kind, f.dtype) for p, f in self._tree.items()}

    def _fire(self, op: str, path: str) -> None:
        queue = self._inject.get((op, path))
        if queue:
            raise queue.pop(0)

    def _get_raw(self, info: ParamInfo) -> Any:
        with self._lk:
            self._alive()
            self._fire("get", info.path)
            f = self._tree[info.path]
            v = f.getter() if f.getter else f.value
            self.log.append(("get", info.path, v))
            return v

    def _get_enum_raw(self, info: ParamInfo) -> tuple[int, str]:
        with self._lk:
            self._alive()
            f = self._tree[info.path]
            idx = int(f.getter() if f.getter else f.value)
            return idx, f.enum[idx]

    def _enum_values_raw(self, info: ParamInfo) -> dict[int, str]:
        with self._lk:
            self._alive()
            return dict(self._tree[info.path].enum or {})

    def _range_of(self, info: ParamInfo):
        f = self._tree[info.path]
        return (f.lo, f.hi)

    def _is_enabled(self, info: ParamInfo) -> bool:
        f = self._tree[info.path]
        if info.path == P.LASER_SHUTTER:
            return f.enabled and bool(self.laser_table[self._selected].get("has_shutter"))
        return f.enabled

    def _set_raw(self, info: ParamInfo, value: Any) -> None:
        with self._lk:
            self._alive()
            self._fire("set", info.path)
            f = self._tree[info.path]
            if f.kind == "enum" and isinstance(value, str):
                rev = {v: k for k, v in (f.enum or {}).items()}
                if value not in rev:
                    raise BucsError(f"{info.path}: no enum label {value!r}")
                value = rev[value]
            if f.kind == "enum" and f.enum is not None and int(value) not in f.enum:
                raise BucsError(f"{info.path}: no enum index {value}")
            self.log.append(("set", info.path, value))
            if f.on_set is not None:
                f.on_set(value)
            else:
                f.value = value

    def _trigger_raw(self, info: ParamInfo) -> None:
        with self._lk:
            self._alive()
            self._fire("trigger", info.path)
            self.log.append(("trigger", info.path, None))
            f = self._tree[info.path]
            if f.on_trigger is not None:
                f.on_trigger()

    # ── building the tree ───────────────────────────────────────────────────────────────

    def _add(self, path: str, kind: str, value: Any = None, *, dtype: str | None = None,
             lo: float | None = None, hi: float | None = None, enum: dict[int, str] | None = None,
             getter: Callable[[], Any] | None = None, on_set: Callable[[Any], None] | None = None,
             on_trigger: Callable[[], None] | None = None) -> None:
        if dtype is None:
            dtype = kind if kind != "status" else type(value).__name__.replace("str", "string")
        self._tree[path] = _FP(path, kind, dtype, value, lo, hi, enum, True, getter, on_set, on_trigger)

    def _build_tree(self) -> dict[str, _FP]:
        self._tree = {}
        add = self._add
        sel = lambda: self.laser_table[self._selected]       # noqa: E731

        # -- status ---------------------------------------------------------------------------
        add(P.PROGRAM_VERSION, "status", self.version, dtype="string", getter=lambda: self.version)
        add(P.COM_ACCEPTS_REMOTE, "status", True, dtype="bool", getter=lambda: self._allowed)
        add(P.CURRENT_FILE_NAME, "status", "", dtype="string", getter=lambda: self._current_file)
        add(P.IS_SEQUENCER_ACTIVE, "status", False, dtype="bool",
            getter=lambda: self._seq_phase() != "idle")
        add(P.ACTIVE_SEQUENCER_NAME, "status", "", dtype="string", getter=self._seq_name)
        add(P.ACTIVE_SEQUENCER_ACTIVITY, "status", "", dtype="string", getter=self._seq_activity)
        add(P.SEQUENCER_STATUS_OR_RESULT, "status", "", dtype="string", getter=self._seq_result)

        # -- control tree ----------------------------------------------------------------------
        add(P.STOP_SEQUENCER, "trigger", on_trigger=self._stop_sequencer)
        add(P.SINGLE_SPECTRUM_INTEGRATION, "float", 1.0, lo=0.001, hi=3600.0)
        add(P.SINGLE_SPECTRUM_ACCUMULATIONS, "int", 1, lo=1, hi=100000)
        add(P.SINGLE_SPECTRUM_START, "trigger", on_trigger=lambda: self._start_seq("SequencerSingleSpectrum", 1.0, 0.0))
        add(P.MAX_Z_STEPPER_TRAVEL, "float", 200.0, lo=0.0, hi=2000.0)
        add(P.spectrograph_path(1, "Grating"), "enum", 0, enum=dict(_GRATINGS))
        add(P.spectrograph_path(1, "CenterWavelength"), "float", 547.0, lo=200.0, hi=1700.0)
        add(P.spectrograph_path(1, "SpectralCenter"), "float", 547.0, lo=-1e9, hi=1e9)
        add(P.spectrograph_path(1, "SpectralUnit"), "enum", 0, enum=dict(_UNITS))
        add(P.SERIES_INTEGRATION, "float", 1.0, lo=0.001, hi=3600.0)
        add(P.SERIES_ACCUMULATIONS, "int", 1, lo=1, hi=100000)
        add(P.ANALYZER_SERIES_STEPS, "int", 73, lo=2, hi=3600)
        add(P.ANALYZER_SERIES_START, "trigger", on_trigger=self._start_analyzer_series)
        add(P.AUTOSAVE_START_DIR, "string", "")
        add(P.AUTOSAVE_FILE_NAME, "string", "")
        add(P.AUTOSAVE_FILE_NUMBER, "string", "")
        add(P.AUTOSAVE_PREFIX + "ExtraDirectory", "string", "")
        add(P.AUTOSAVE_DIR_MODE, "enum", 2, enum={0: "none", 1: "extra", 2: "date", 3: "extra+date", 4: "date+extra"})
        add(P.AUTOSAVE_STORE_MODE, "enum", 0, enum={0: "keep", 1: "clear after store"})
        add(P.AUTOSAVE_OVERWRITE_MODE, "enum", 0, enum={0: "no", 1: "yes"})
        add(P.AUTOSAVE_STORE, "trigger", on_trigger=self._store_project)
        # groups the write guard must refuse (present so the tests can prove the guard, not the absence)
        for leaf, kind in (("NewProject", "trigger"), ("ExitApplication", "trigger"),
                           ("UpdateHardware", "trigger"), ("LoadConfiguration", "string"),
                           ("FileNameToAppendToProject", "string")):
            add("ApplicationControl|" + leaf, kind, "" if kind == "string" else None)
        for leaf, kind in (("LocalLockoutLicensed", "trigger"), ("LocalLockout", "bool"),
                           ("AllowReadAccess", "bool"), ("RevokeWriteAccess", "trigger")):
            add("UserParameters|COMInterop|" + leaf, kind, False if kind == "bool" else None)
        add("UserParameters|HardwareConstraints|ExtendedZStepperTravel", "float", 400.0, lo=0.0, hi=4000.0)
        add("UserParameters|HardwareConstraints|SetExtendedZStepperTravel", "trigger")

        # -- silent spectrum -------------------------------------------------------------------
        add(P.SILENT_PARAMETER, "string", "")
        add(P.SILENT_INFORMATION, "string", "")
        add(P.SILENT_DONE, "bool", False, on_set=self._set_silent_done, getter=self._get_silent_done)
        add(P.SILENT_TEXT, "string", "", getter=lambda: self._silent_text)
        add(P.SILENT_ERROR, "string", "Ok", getter=lambda: self._silent_error)
        add(P.SILENT_START, "trigger", on_trigger=self._start_silent)

        # -- optics ------------------------------------------------------------------------------
        add(P.BEAM_STATE, "string", "", getter=self._beam_state)
        for key, trig in P.BEAM_TRIGGERS.items():
            add(trig, "trigger", on_trigger=(lambda k=key: self._set_beam(k)))
        add(P.ANALYZER, "float", 0.0, lo=-720.0, hi=720.0)
        add(P.ANALYZER_COUPLER, "bool", True)
        add(P.FIELD_STOP, "float", 100.0, lo=0.0, hi=100.0)
        add(P.APERTURE_STOP, "float", 100.0, lo=0.0, hi=100.0)

        # -- lasers ------------------------------------------------------------------------------
        add(P.LASER_COUNT, "int", len(self.laser_table), getter=lambda: len(self.laser_table))
        add(P.LASER_SELECT_NONE, "trigger", on_trigger=lambda: None)
        for i in range(len(self.laser_table)):
            add(P.laser_select_path(i), "trigger", on_trigger=(lambda i=i: self._select_laser(i)))
        add(P.LASER_WAVELENGTH, "float", 0.0, lo=0.0, hi=1e5, getter=lambda: float(sel()["wavelength_nm"]))
        add(P.LASER_POWER, "float", 0.0, lo=0.0, hi=50.0, getter=self._get_power, on_set=self._set_power)
        add(P.LASER_SHUTTER, "bool", False, getter=lambda: bool(self._shutter[self._selected]),
            on_set=self._set_shutter)
        add(P.LASER_HAS_SHUTTER, "bool", True, getter=lambda: bool(sel().get("has_shutter")))
        add(P.LASER_POWER_CORRECTION, "float", 1.092, getter=lambda: 1.092)

        # -- stage -------------------------------------------------------------------------------
        add(P.STAGE_IS_VIRTUAL, "bool", False)
        add(P.XY_STATE, "string", "", getter=lambda: self._axis_state(self._xy, "xy"))
        add(P.XY_DESIRED_X, "float", 0.0, lo=-self.xy_limit_um * 2, hi=self.xy_limit_um * 2)
        add(P.XY_DESIRED_Y, "float", 0.0, lo=-self.xy_limit_um * 2, hi=self.xy_limit_um * 2)
        add(P.XY_CURRENT_X, "float", 0.0, getter=lambda: self._axis_pos(self._xy, "xy")[0])
        add(P.XY_CURRENT_Y, "float", 0.0, getter=lambda: self._axis_pos(self._xy, "xy")[1])
        add(P.XY_MOVE, "trigger", on_trigger=lambda: self._start_move("xy"))
        add(P.XY_STOP, "trigger", on_trigger=lambda: self._stop_move("xy"))
        add(P.XY_SPEED, "float", 1000.0, lo=1.0, hi=5000.0)
        add(P.Z_STATE, "string", "", getter=lambda: self._axis_state(self._z, "z"))
        add(P.Z_DESIRED, "float", 0.0, lo=-1000.0, hi=1000.0)
        add(P.Z_CURRENT, "float", 0.0, getter=lambda: self._axis_pos(self._z, "z"))
        add(P.Z_MOVE, "trigger", on_trigger=lambda: self._start_move("z"))
        add(P.Z_STOP, "trigger", on_trigger=lambda: self._stop_move("z"))
        add(P.Z_SPEED, "float", 100.0, lo=1.0, hi=1000.0)
        add(P.Z_IN_USER_SPACE, "bool", False)
        for rezero in ("XYAxes|SetSamplePositionToZero", "XYAxes|ResetCoordinateSystem",
                       "XYAxes|MoveToCalibrationPosition", "ZAxis|SetSamplePositionZToZero",
                       "ZAxis|SetHardwarePositionZToZero"):
            add(P.ROOT_MICROSCOPE + "MotorizedXYZ|" + rezero, "trigger")

        # -- video / focus / objective ----------------------------------------------------------------
        add(P.VIDEO_FILE_NAME, "string", "")
        add(P.VIDEO_TO_FILE, "trigger", on_trigger=self._video_to_file)
        add(P.VIDEO_ACQUIRE, "trigger")
        add(P.AUTOFOCUS_EXECUTE, "trigger", on_trigger=self._start_focus)
        add(P.AUTOFOCUS_STATUS, "enum", 1, enum={0: "Running", 1: "LastSucceeded", 2: "LastFailed"},
            getter=self._get_focus_status)
        add(P.VIDEO_CAL_WIDTH_UM, "float", 120.0)
        add(P.VIDEO_CAL_HEIGHT_UM, "float", 90.0)
        add(P.VIDEO_CAL_ROTATION_DEG, "float", 0.0)
        add(P.VIDEO_PROBE_X, "float", 0.5)
        add(P.VIDEO_PROBE_Y, "float", 0.5)
        add(P.OBJECTIVE_INFORMATION, "string", "Zeiss EC Epiplan 50x/0.75")
        add(P.OBJECTIVE_MAGNIFICATION, "float", 50.0)
        add(P.OBJECTIVE_FOCUS_DEPTH, "float", 1.4)
        add(P.STATE_NAME, "string", "", getter=lambda: self._state_name, on_set=self._set_state_name)
        add(P.DETECTOR_OUTPUT_CCD1, "trigger")
        return self._tree

    # ── sequencer ───────────────────────────────────────────────────────────────────────────

    def _start_seq(self, name: str, busy_s: float, cleanup_s: float, *, on_data=None, on_end=None) -> None:
        self._seq = _Seq(name, self._clock(), busy_s, cleanup_s, on_data, on_end)

    def _seq_phase(self) -> str:
        with self._lk:
            s = self._seq
            if s is None:
                return "idle"
            el = self._clock() - s.t0
            busy = float("inf") if self.hang_sequencer else s.busy_s
            if el < busy:
                return "busy"
            if not s.data_done:
                s.data_done = True
                if s.on_data:
                    s.on_data()
            if el < busy + s.cleanup_s:
                return "cleanup"
            if s.on_end:
                s.on_end()
            self._seq = None
            self._last_result = "Finished"
            return "idle"

    def _seq_name(self) -> str:
        if self._seq_phase() == "idle":
            raise BucsNoValue("no sequencer is active (E_ACCESSDENIED)")
        return self._seq.name

    def _seq_activity(self) -> str:
        ph = self._seq_phase()
        if ph == "idle":
            raise BucsNoValue("no sequencer is active (E_ACCESSDENIED)")
        return "Sequence Busy" if ph == "busy" else "Cleaning up after Sequence End"

    def _seq_result(self) -> str:
        return "Running" if self._seq_phase() != "idle" else self._last_result

    def _stop_sequencer(self) -> None:
        if self._seq is not None:
            self._seq = None
            self._last_result = "StoppedByUser"
        self._focus = None

    # ── silent spectrum ──────────────────────────────────────────────────────────────────────

    def _get_silent_done(self) -> bool:
        self._seq_phase()
        return self._silent_done

    def _set_silent_done(self, value: Any) -> None:
        self._silent_done = bool(value)

    def _start_silent(self) -> None:
        if self._seq_phase() != "idle":
            self.log.append(("ignored", P.SILENT_START, "sequencer busy"))
            return
        text = self._tree[P.SILENT_PARAMETER].value or ""
        req = {"integration_s": 1.0, "accumulations": 1, "raw": text}
        for line in text.replace("\r", "").split("\n"):
            tok = line.split()
            if len(tok) == 2 and tok[0] == "IntegrationTime":
                req["integration_s"] = float(tok[1])
            elif len(tok) == 2 and tok[0] == "NumberOfAccumulations":
                req["accumulations"] = int(tok[1])
        self.silent_requests.append(req)
        self._silent_done = False
        self._silent_error = "Ok"
        self._silent_text = ""
        integ = max(req["integration_s"], self.min_integration_s)
        n = req["accumulations"]
        busy = max(self.silent_acquire_s, integ * n)
        self._start_seq("SequencerSingleSpectrum", busy, self.silent_cleanup_s,
                        on_data=lambda: self._finish_silent(integ, n))

    def _axis_nm(self) -> np.ndarray:
        cw = self._tree.get(P.spectrograph_path(1, "CenterWavelength"))
        center = float(cw.value) if cw is not None else 547.0
        grating = int(self._tree[P.spectrograph_path(1, "Grating")].value) \
            if P.spectrograph_path(1, "Grating") in self._tree else 0
        disp = _DISPERSION_NM_PX.get(grating, 0.075)
        return center + (np.arange(self.pixels, dtype=np.float64) - (self.pixels - 1) / 2.0) * disp

    def _finish_silent(self, integration_s: float, n: int) -> None:
        nm = self._axis_nm()
        las = self.laser_table[self._selected]
        exc = float(las["wavelength_nm"])
        lit = self._shutter[self._selected] and self._beam == "Raman"
        shift = 1e7 / exc - 1e7 / nm
        base = 300.0 + 0.05 * (nm - nm[0])
        if lit:
            for pos, h in ((520.7, 1.0), (303.0, 0.08), (950.0, 0.05)):
                base = base + 4000.0 * h / (1.0 + ((shift - pos) / 4.0) ** 2)
        frames = [self._rng.poisson(np.clip(base * integration_s, 0, None)).astype(np.float64)
                  for _ in range(n)]
        unit = int(self._tree[P.spectrograph_path(1, "SpectralUnit")].value) \
            if P.spectrograph_path(1, "SpectralUnit") in self._tree else 0
        x = _to_unit(nm, unit, exc)
        self._silent_text = format_truematch(
            x, frames, excitation_nm=exc, integration_time_s=integration_s, x_kind=_XKIND[unit],
            trailer=self.truematch_trailer)
        self._silent_done = True

    # ── beam path / lasers / analyzer ────────────────────────────────────────────────────────

    def _set_beam(self, key: str) -> None:
        text = _BEAM_STATE_TEXT[key]
        if self.beam_switch_s > 0:
            self._beam_pending = (self._clock() + self.beam_switch_s, text)
        else:
            self._beam = text

    def _beam_state(self) -> str:
        if self._beam_pending is not None and self._clock() >= self._beam_pending[0]:
            self._beam, self._beam_pending = self._beam_pending[1], None
        return "Changing" if self._beam_pending is not None else self._beam

    def _select_laser(self, i: int) -> None:
        if self.select_closes_previous_shutter and int(i) != self._selected:
            self._shutter[self._selected] = False
        self._selected = int(i)

    def _get_power(self) -> float:
        if not self.laser_table[self._selected].get("power_readable"):
            raise BucsError("Laser|Selected|Power not available for this laser line (fake)")
        return float(self._power[self._selected])

    def _set_power(self, value: float) -> None:
        if not self.laser_table[self._selected].get("power_readable"):
            raise BucsError("Laser|Selected|Power not available for this laser line (fake)")
        self._power[self._selected] = float(value)

    def _set_shutter(self, value: bool) -> None:
        self._shutter[self._selected] = bool(value)

    def _set_state_name(self, name: str) -> None:
        preset = self.presets.get(name)
        if preset is None:
            if self.reject_unknown_state:
                raise BucsError(f"unknown microscope state {name!r}")
            return                                    # WITec ignores it; the driver must read back
        self._state_name = name
        self._selected = int(preset.get("laser", self._selected))
        if "grating" in preset and P.spectrograph_path(1, "Grating") in self._tree:
            self._tree[P.spectrograph_path(1, "Grating")].value = int(preset["grating"])
        if "center_nm" in preset:
            self._tree[P.spectrograph_path(1, "CenterWavelength")].value = float(preset["center_nm"])
        if preset.get("power_mw") is not None and self.laser_table[self._selected].get("power_readable"):
            self._power[self._selected] = float(preset["power_mw"])
        if preset.get("shutter_open") is not None and self.laser_table[self._selected].get("has_shutter"):
            self._shutter[self._selected] = bool(preset["shutter_open"])

    # ── stage ────────────────────────────────────────────────────────────────────────────────

    def _z_limit(self) -> float:
        p = self._tree.get(P.MAX_Z_STEPPER_TRAVEL)
        return float(p.value) / 2.0 if p is not None else 100.0

    def _axis_state(self, ax: dict, which: str) -> str:
        self._advance_axis(ax, which)
        return ax["state"]

    def _axis_pos(self, ax: dict, which: str):
        self._advance_axis(ax, which)
        m = ax["move"]
        if m is not None:
            frac = min(1.0, max(0.0, (self._clock() - m["t0"]) / max(m["dur"], 1e-9)))
            cur = ax["cur"]
            if which == "xy":
                return [cur[0] + (m["end"][0] - cur[0]) * frac, cur[1] + (m["end"][1] - cur[1]) * frac]
            return cur + (m["end"] - cur) * frac
        return list(ax["cur"]) if which == "xy" else ax["cur"]

    def _advance_axis(self, ax: dict, which: str) -> None:
        m = ax["move"]
        if m is not None and self._clock() - m["t0"] >= m["dur"]:
            ax["cur"] = list(m["end"]) if which == "xy" else m["end"]
            ax["state"] = P.STAGE_STATE_FAILED if m["clipped"] else P.STAGE_STATE_REACHED
            ax["move"] = None

    def _start_move(self, which: str) -> None:
        ax = self._xy if which == "xy" else self._z
        self._advance_axis(ax, which)
        if ax["move"] is not None:
            ax["state"] = P.STAGE_STATE_IN_USE
            return
        if which == "xy":
            want = [float(self._tree[P.XY_DESIRED_X].value), float(self._tree[P.XY_DESIRED_Y].value)]
            lim = self.xy_limit_um
            end = [max(-lim, min(lim, want[0])), max(-lim, min(lim, want[1]))]
            clipped = end != want
        else:
            want = float(self._tree[P.Z_DESIRED].value)
            lim = self._z_limit()
            end = max(-lim, min(lim, want))
            clipped = end != want
        ax["move"] = {"t0": self._clock(), "dur": self.stage_move_s, "end": end, "clipped": clipped}
        ax["state"] = "Moving"

    def _stop_move(self, which: str) -> None:
        ax = self._xy if which == "xy" else self._z
        self._advance_axis(ax, which)          # a move that already finished stays finished
        if ax["move"] is not None:
            ax["cur"] = self._axis_pos(ax, which)
            ax["move"] = None
            ax["state"] = P.STAGE_STATE_STOPPED

    # ── autofocus / video / series / project ──────────────────────────────────────────────────

    def _start_focus(self) -> None:
        self._focus = {"t0": self._clock()}
        self._focus_status = 0

    def _get_focus_status(self) -> int:
        f = self._focus
        if f is not None and self._clock() - f["t0"] >= self.focus_s:
            self._focus = None
            self._focus_status = 1 if self.focus_ok else 2
            if self.focus_ok:
                self._z["cur"] = float(self.focus_z_um)
        return self._focus_status

    def _video_to_file(self) -> None:
        path = self._tree[P.VIDEO_FILE_NAME].value
        if not path:
            return
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(PNG_1X1)
        self.video_files.append(str(p))

    def _start_analyzer_series(self) -> None:
        if self._seq_phase() != "idle":
            self.log.append(("ignored", P.ANALYZER_SERIES_START, "sequencer busy"))
            return
        n = int(self._tree[P.ANALYZER_SERIES_STEPS].value)
        run = {"points": n, "integration_s": float(self._tree[P.SERIES_INTEGRATION].value),
               "accumulations": int(self._tree[P.SERIES_ACCUMULATIONS].value),
               "analyzer_coupled": bool(self._tree[P.ANALYZER_COUPLER].value)}
        self._start_seq("SequencerTimeSeriesSlow", max(self.series_point_s * n, run["integration_s"] * n * run["accumulations"]),
                        self.silent_cleanup_s, on_end=lambda: self.series_runs.append(run))

    def _store_project(self) -> None:
        d = self._tree[P.AUTOSAVE_START_DIR].value
        name = self._tree[P.AUTOSAVE_FILE_NAME].value
        if not d or not name:
            self.store_error = "no directory or file name"
            return
        target = Path(d) / f"{name}.wip"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and int(self._tree[P.AUTOSAVE_OVERWRITE_MODE].value) == 0:
            self.store_error = f"{target} exists and overwrite is off"
            return
        target.write_bytes(b"WIT-FAKE-PROJECT\n")
        self.saved_projects.append(str(target))
        self._current_file = str(target)
        self.store_error = ""


def _to_unit(nm: np.ndarray, unit: int, exc: float) -> np.ndarray:
    """Wavelength axis → the numbers WITec writes for its *Spectral Unit* enum index."""
    if unit == 0:
        return nm
    if unit == 1:
        return nm / 1e3
    if unit == 2:
        return 1e7 / nm
    if unit == 3:
        return 1e7 / exc - 1e7 / nm
    ev = 1239.841984 / nm
    if unit == 4:
        return ev
    if unit == 5:
        return ev * 1e3
    rel = 1239.841984 / exc - ev
    return rel if unit == 6 else rel * 1e3
