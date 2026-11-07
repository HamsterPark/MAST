"""Thorlabs PDXC2 piezo-inertia stage controller — via the Kinesis C DLL (ctypes).

This stage is a **PDXC2** (``BenchtopPDXC2Control``), NOT a Benchtop Piezo (BPC) — a different
Kinesis device family with a different control model (piezo inertia "slip-stick"
motor, open-loop jog steps + closed-loop position). This driver calls the
documented C API in ``Thorlabs.MotionControl.Benchtop.Piezo.dll`` via ctypes
(no pythonnet), which is deterministic from the vendor header signatures
(``Thorlabs.MotionControl.Benchtop.Piezo.PDXC2.h``).

Closed-loop position flow (from the header)::

    TLI_BuildDeviceList → PDXC2_Open(serial) → PDXC2_StartPolling →
    PDXC2_Enable → PDXC2_SetPositionControlMode(PZ_CloseLoop) →
    PDXC2_SetExternalTriggerConfig(Manual)
    per move:  PDXC2_SetExternalTriggerConfig(Manual) →
               PDXC2_SetClosedLoopTarget(target) → PDXC2_MoveStart →
               poll PDXC2_GetPosition

The trigger mode is put back to Manual before every move: a stage left in a
trigger mode may move on a stray edge.

VERIFY ON HARDWARE — the points that need the real stage (this driver cannot
be unit-tested end-to-end without it; everything else degrades gracefully):

  1. **Position unit.** ``PDXC2_SetClosedLoopTarget`` / ``GetPosition`` use
     int32 device units (the header: nm in closed loop, steps in open loop).
     ``counts_per_unit`` converts the axis native unit → device units;
     ``OpticalStageWiggle``'s ``scale_ratio`` confirms it in one nudge.
  2. **On-target.** A move is settled when the read-back
     position is within ``position_tolerance`` device units of the target.
     Without it ``_get_status_raw`` reported "not moving" the instant MoveStart
     returned, so a move "settled" before the stage had gone anywhere.
     ``position_tolerance=None`` selects that behaviour (settle on
     ``AxisConfig.settle_s`` alone). The status bits the header documents
     (moving forward/backward, homed, closed loop, errors) are reported by
     ``status_details`` but do not decide settling — not yet seen on the stage.

``PDXC2_GetPosition`` is ``short (char const*, __int32* position)`` — header and
C API help of Kinesis 1.14.53. Binding it as ``int (char const*)`` is wrong: the
DLL then writes the position through whatever the second argument register
holds, and the "position" read is the error code.

``PDXC2_Home`` "sets the current position to the Home position (Position = 0)"
(header): it does not move the stage — it re-zeroes the counter, so the soft
limits shift with it. MAST calls it only on request.

Lazy like the rest of ``mast.instruments``: no ctypes load at import; a missing
Kinesis DLL / absent device surfaces as :class:`InstrumentUnavailable`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from mast.instruments.base import (
    AxisConfig,
    AxisStatus,
    InstrumentError,
    InstrumentUnavailable,
    MotionAxis,
    MotionController,
)

logger = logging.getLogger(__name__)

__all__ = ["ThorlabsPDXC2Controller", "ThorlabsPDXC2Axis", "DEFAULT_KINESIS_DIR"]

DEFAULT_KINESIS_DIR = Path(r"C:\Program Files\Thorlabs\Kinesis")
_DLL_NAME = "Thorlabs.MotionControl.Benchtop.Piezo.dll"

# PZ_ControlModeTypes (Kinesis): Undefined=0, OpenLoop=1, CloseLoop=2,
# OpenLoopSmooth=3, CloseLoopSmooth=4.
_PZ_CLOSE_LOOP = 2

# PDXC2_TriggerModes (Benchtop.Piezo.PDXC2.h): Manual=0 ("no external
# triggering"), AnalogRising=1 … TwoPositionFalling=6.
_TRIGGER_MANUAL = 0

# PDXC2_GetStatusBits (header table). Reported by status_details only.
_STATUS_FLAGS = (
    (0x00000010, "moving_forward"),
    (0x00000020, "moving_backward"),
    (0x00000200, "homing"),
    (0x00000400, "homed"),
    (0x00000800, "closed_loop"),
    (0x00200000, "acquiring_pulse_params"),
    (0x00400000, "pulse_params_acquired"),
)
_STATUS_ERRORS = (
    (0x00800000, "device in wrong mode"),
    (0x01000000, "excessive current"),
    (0x02000000, "excessive temperature"),
    (0x04000000, "abnormal stage movement"),
    (0x08000000, "wrong stage detected"),
    (0x40000000, "error"),
)
_CONTROL_MODES = {1: "open_loop", 2: "closed_loop", 3: "open_loop_smooth",
                  4: "closed_loop_smooth"}


def decode_status_bits(bits: int) -> dict:
    """The header's PDXC2 status word → named flags + error list."""
    bits = int(bits) & 0xFFFFFFFF
    out: dict = {"status_bits": f"0x{bits:08X}"}
    for mask, name in _STATUS_FLAGS:
        out[name] = bool(bits & mask)
    out["errors"] = [name for mask, name in _STATUS_ERRORS if bits & mask]
    return out


def _load_pdxc2_dll(dll_dir: Path):
    """Load the Kinesis Benchtop.Piezo DLL and set PDXC2 prototypes.

    Raises :class:`InstrumentUnavailable` if ctypes, the directory, or the DLL
    is missing — never a bare OSError."""
    try:
        import ctypes
    except Exception as exc:  # pragma: no cover - ctypes is stdlib
        raise InstrumentUnavailable(f"ctypes unavailable: {exc}") from exc

    if not dll_dir.is_dir():
        raise InstrumentUnavailable(
            f"Kinesis directory not found: {dll_dir} — install Thorlabs Kinesis "
            "or set dll_dir in the device config"
        )
    dll_path = dll_dir / _DLL_NAME
    if not dll_path.is_file():
        raise InstrumentUnavailable(f"Kinesis DLL missing: {dll_path}")

    # let the loader resolve sibling Kinesis DLLs (DeviceManager, etc.)
    try:
        os.add_dll_directory(str(dll_dir))  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - non-Windows / already added
        pass

    try:
        lib = ctypes.CDLL(str(dll_path))
    except OSError as exc:
        raise InstrumentUnavailable(f"failed to load {dll_path.name}: {exc}") from exc

    c = ctypes
    proto = {
        "TLI_BuildDeviceList": ([], c.c_short),
        "PDXC2_Open": ([c.c_char_p], c.c_short),
        "PDXC2_Close": ([c.c_char_p], None),
        "PDXC2_Enable": ([c.c_char_p], c.c_short),
        "PDXC2_Disable": ([c.c_char_p], c.c_short),
        "PDXC2_StartPolling": ([c.c_char_p, c.c_int], c.c_bool),
        "PDXC2_StopPolling": ([c.c_char_p], None),
        "PDXC2_SetPositionControlMode": ([c.c_char_p, c.c_short], c.c_short),
        "PDXC2_SetClosedLoopTarget": ([c.c_char_p, c.c_int], c.c_short),
        "PDXC2_MoveStart": ([c.c_char_p], c.c_short),
        "PDXC2_MoveStop": ([c.c_char_p], c.c_short),
        "PDXC2_RequestPosition": ([c.c_char_p], c.c_short),
        "PDXC2_GetPosition": ([c.c_char_p, c.POINTER(c.c_int32)], c.c_short),
        "PDXC2_GetStatusBits": ([c.c_char_p], c.c_uint),
        "PDXC2_Home": ([c.c_char_p], c.c_short),
    }
    try:
        for name, (argtypes, restype) in proto.items():
            fn = getattr(lib, name)
            fn.argtypes = argtypes
            fn.restype = restype
    except AttributeError as exc:
        raise InstrumentUnavailable(
            f"{dll_path.name} is missing a PDXC2 export ({exc}); wrong Kinesis "
            "version?"
        ) from exc
    # Exports a stage can be driven without (an older DLL may lack them).
    optional = {
        "PDXC2_SetExternalTriggerConfig": ([c.c_char_p, c.c_ushort], c.c_short),
        "PDXC2_RequestExternalTriggerConfig": ([c.c_char_p], c.c_short),
        "PDXC2_GetExternalTriggerConfig": ([c.c_char_p], c.c_ushort),
        "PDXC2_GetPositionControlMode": ([c.c_char_p], c.c_short),
    }
    for name, (argtypes, restype) in optional.items():
        fn = getattr(lib, name, None)
        if fn is not None:
            fn.argtypes = argtypes
            fn.restype = restype
    return lib


class ThorlabsPDXC2Axis(MotionAxis):
    """The single PDXC2 stage axis. Native unit set by ``counts_per_unit``
    (device int32 units per axis unit).

    No velocity: ``PDXC2_ClosedLoopParameters.RefSpeed`` exists, but neither the
    header nor the C API help gives its unit. No servo switch: the open/closed
    loop mode also changes what a position means (steps vs nm)."""

    #: "home" here is PDXC2_Home — re-zero the counter, no motion
    SUPPORTS = frozenset({"home"})

    def __init__(self, config: AxisConfig, controller: "ThorlabsPDXC2Controller",
                 *, counts_per_unit: float = 1.0, position_tolerance: float | None = None):
        super().__init__(config, controller)
        if counts_per_unit <= 0:
            raise ValueError("counts_per_unit must be > 0")
        self._pdxc: ThorlabsPDXC2Controller = controller
        self._cpu = float(counts_per_unit)
        self._tol = float(position_tolerance) if position_tolerance is not None else None
        self._target_counts: int | None = None

    def _move_abs_raw(self, target: float) -> None:
        lib, s = self._pdxc._lib_serial()
        counts = int(round(target * self._cpu))
        self._pdxc._ensure_manual_trigger(lib)
        self._pdxc._check(lib.PDXC2_SetClosedLoopTarget(s, counts), "SetClosedLoopTarget")
        self._pdxc._check(lib.PDXC2_MoveStart(s), "MoveStart")
        self._target_counts = counts

    def _read_counts(self) -> int:
        import ctypes

        lib, s = self._pdxc._lib_serial()
        lib.PDXC2_RequestPosition(s)
        pos = ctypes.c_int32(0)
        self._pdxc._check(lib.PDXC2_GetPosition(s, ctypes.byref(pos)), "GetPosition")
        return int(pos.value)

    def _get_position_raw(self) -> float:
        return self._read_counts() / self._cpu

    def _get_status_raw(self) -> AxisStatus:
        counts = self._read_counts()
        if self._tol is None or self._target_counts is None:
            # Legacy / before any move: no on-target knowledge; the base wait
            # loop treats "not moving" as settled and applies settle_s.
            return AxisStatus(position=counts / self._cpu, moving=False, on_target=None,
                              raw={"counts": counts})
        on = abs(counts - self._target_counts) <= self._tol
        return AxisStatus(position=counts / self._cpu, moving=not on, on_target=on,
                          raw={"counts": counts, "target_counts": self._target_counts})

    def _stop_raw(self) -> None:
        self._target_counts = None      # a stopped move has no target to settle on
        try:
            lib, s = self._pdxc._lib_serial()
            lib.PDXC2_MoveStop(s)
        except Exception:  # noqa: BLE001 - panic path must not raise
            pass

    def _home_raw(self) -> None:
        lib, s = self._pdxc._lib_serial()
        self._pdxc._check(lib.PDXC2_Home(s), "Home")
        # the counter is now 0: a target from before would never be "reached"
        # (home()'s wait loop timed out after any earlier move)
        self._target_counts = None

    def _status_details_raw(self) -> dict:
        lib, s = self._pdxc._lib_serial()
        out: dict = {}
        try:
            out.update(decode_status_bits(lib.PDXC2_GetStatusBits(s)))
        except Exception:  # noqa: BLE001 - the position read already stands
            pass
        get_mode = getattr(lib, "PDXC2_GetPositionControlMode", None)
        if get_mode is not None:
            try:
                mode = int(get_mode(s))
                out["control_mode"] = _CONTROL_MODES.get(mode, str(mode))
            except Exception:  # noqa: BLE001
                pass
        out["external_trigger"] = "manual" if self._pdxc._trigger_manual else "unchanged"
        return out


class ThorlabsPDXC2Controller(MotionController):
    """One PDXC2 controller, addressed by serial number (single stage)."""

    POLL_MS = 200

    def __init__(
        self,
        *,
        serial_no: str,
        axes: list[AxisConfig] | None = None,
        dll_dir: str | Path | None = None,
        counts_per_unit: float = 1.0,
        closed_loop: bool = True,
        position_tolerance: float | None = None,
        external_trigger: str | None = "manual",
    ):
        super().__init__()
        if not serial_no:
            raise ValueError("ThorlabsPDXC2Controller needs the device serial_no")
        self._serial_str = str(serial_no)
        self._serial = self._serial_str.encode("ascii")
        self._axis_configs = list(axes or [])
        self._dll_dir = Path(dll_dir) if dll_dir else DEFAULT_KINESIS_DIR
        self._counts_per_unit = float(counts_per_unit)
        self._closed_loop = bool(closed_loop)
        #: device units (nm on the PDXC2); None = legacy settle-on-dwell
        self._position_tolerance = position_tolerance
        #: ``"manual"`` (default): external trigger to Manual ("no external
        #: triggering") at connect, verified by readback, and again before every
        #: move — a device may be left holding a "rising edge → step 1 mm"
        #: setting: a stray edge would move the stage by itself, and a stage left in a trigger mode may
        #: not answer MoveStart. ``None`` / ``"unchanged"`` leaves the mode alone.
        mode = (str(external_trigger).strip().lower() if external_trigger is not None
                else "unchanged")
        if mode not in ("manual", "unchanged"):
            raise ValueError(f"external_trigger={external_trigger!r}: only 'manual' or "
                             "'unchanged' — MAST does not set up trigger modes")
        self._trigger_manual = mode == "manual"
        self._lib = None

    # -- lifecycle -----------------------------------------------------------

    def _connect_raw(self) -> None:
        lib = _load_pdxc2_dll(self._dll_dir)
        if lib.TLI_BuildDeviceList() != 0:
            raise InstrumentUnavailable("TLI_BuildDeviceList failed")
        if lib.PDXC2_Open(self._serial) != 0:
            raise InstrumentUnavailable(
                f"PDXC2_Open({self._serial_str}) failed — device off / cable / "
                "another program holding it?"
            )
        try:
            lib.PDXC2_StartPolling(self._serial, self.POLL_MS)
            lib.PDXC2_Enable(self._serial)
            if self._closed_loop:
                lib.PDXC2_SetPositionControlMode(self._serial, _PZ_CLOSE_LOOP)
            if self._trigger_manual and getattr(lib, "PDXC2_SetExternalTriggerConfig",
                                                None) is None:
                logger.warning("PDXC2 %s: this Kinesis DLL has no PDXC2_SetExternalTriggerConfig "
                               "— the trigger mode is left as the device has it",
                               self._serial_str)
            self._ensure_manual_trigger(lib)
            self._verify_manual_trigger(lib)
        except Exception as exc:
            try:
                lib.PDXC2_Close(self._serial)
            except Exception:  # noqa: BLE001
                pass
            raise InstrumentUnavailable(
                f"PDXC2 {self._serial_str}: init failed: {exc}"
            ) from exc
        self._lib = lib

    def _close_raw(self) -> None:
        lib = self._lib
        self._lib = None
        if lib is not None:
            for call in (
                lambda: lib.PDXC2_StopPolling(self._serial),
                lambda: lib.PDXC2_Disable(self._serial),
                lambda: lib.PDXC2_Close(self._serial),
            ):
                try:
                    call()
                except Exception:  # noqa: BLE001
                    pass

    def _build_axes(self) -> dict[str, MotionAxis]:
        return {
            cfg.name: ThorlabsPDXC2Axis(
                cfg, self, counts_per_unit=self._counts_per_unit,
                position_tolerance=self._position_tolerance,
            )
            for cfg in self._axis_configs
        }

    # -- helpers -------------------------------------------------------------

    def _lib_serial(self):
        if self._lib is None:
            raise InstrumentUnavailable(f"PDXC2 {self._serial_str} not connected")
        return self._lib, self._serial

    def _check(self, code: int, op: str) -> None:
        if code != 0:
            raise InstrumentError(
                f"PDXC2 {self._serial_str}: {op} returned error code {code}"
            )

    def _ensure_manual_trigger(self, lib) -> None:
        """``PDXC2_SetExternalTriggerConfig(serial, Manual)``; an error refuses
        the move (or the connect) — a stage in an unknown trigger mode is not
        one MAST commands."""
        if not self._trigger_manual:
            return
        fn = getattr(lib, "PDXC2_SetExternalTriggerConfig", None)
        if fn is None:
            return            # older DLL: warned once at connect
        self._check(fn(self._serial, _TRIGGER_MANUAL), "SetExternalTriggerConfig(Manual)")

    #: how long the connect-time readback waits for the DLL's cached value
    TRIGGER_READBACK_S = 1.0

    def _verify_manual_trigger(self, lib) -> None:
        """Connect-time readback: ``PDXC2_RequestExternalTriggerConfig`` then
        ``PDXC2_GetExternalTriggerConfig`` (the value the DLL cached from the
        device's reply) must be Manual within :attr:`TRIGGER_READBACK_S`, or
        the connect fails. A DLL without the two exports is logged as
        unverified — the write itself was still checked."""
        if not self._trigger_manual or getattr(lib, "PDXC2_SetExternalTriggerConfig",
                                               None) is None:
            return
        req = getattr(lib, "PDXC2_RequestExternalTriggerConfig", None)
        get = getattr(lib, "PDXC2_GetExternalTriggerConfig", None)
        if req is None or get is None:
            logger.warning("PDXC2 %s: Manual trigger written but not read back (this "
                           "Kinesis DLL has no Request/GetExternalTriggerConfig)",
                           self._serial_str)
            return
        import time as _time

        self._check(req(self._serial), "RequestExternalTriggerConfig")
        deadline = _time.monotonic() + self.TRIGGER_READBACK_S
        mode = int(get(self._serial))
        while mode != _TRIGGER_MANUAL and _time.monotonic() < deadline:
            _time.sleep(0.05)
            mode = int(get(self._serial))
        if mode != _TRIGGER_MANUAL:
            raise InstrumentError(
                f"PDXC2 {self._serial_str}: external trigger reads back mode {mode} after "
                "SetExternalTriggerConfig(Manual) — refusing to drive a stage that may also "
                "move on its trigger input")
