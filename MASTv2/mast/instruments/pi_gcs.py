"""Physik Instrumente GCS controllers: E-816 (piezo) and E-861 (NEXACT).

Speaks the text-based **GCS command set** directly over a serial link
(RS-232 / USB-VCP) — no PIPython dependency, no new pins in
requirements-v2.txt. Replaces the vendor ``GCS_LabVIEW`` VI stacks found
in the lab's LabVIEW project.

Command subset used (GCS 1/2 common ground, verified against the E-816
and E-861 vendor SDKs shipped with the LabVIEW project):

- ``*IDN?``            identify
- ``SVO <ax> <0|1>``   servo (closed-loop) off/on
- ``MOV <ax> <pos>``   closed-loop absolute move
- ``POS? <ax>``        current position       → ``<ax>=<float>``
- ``ONT? <ax>``        on-target flag         → ``<ax>=<0|1>``
- ``ERR?``             last error code        → ``<int>``
- ``STP``              smooth stop (sets error 10, which we swallow)
- ``FRF <ax>`` / ``FRF? <ax>``  reference move + referenced query (E-861)
- ``VEL <ax> <v>`` / ``VEL? <ax>``  closed-loop velocity, physical unit / s
- ``SVO? <ax>``        servo state
- ``TMN? <ax>`` / ``TMX? <ax>``  the controller's own travel range
- ``LIM? <ax>`` / ``TRS? <ax>``  does the stage HAVE limit / reference switches
  (presence, not their state) — E-861 only

The manual-panel additions (status, velocity, servo) use only commands the
vendor's per-controller LabVIEW driver sets ship a VI for (E-861 set 6.0.2:
VEL/VEL?, SVO/SVO?, TMN?/TMX?, LIM?, TRS?, FRF/FRF?; the E-816 set has no
FRF/LIM?/TRS?). A query the firmware does not answer leaves GCS error 2 in the
queue; the next command's ERR? would be misattributed that error, so a failed status
query is followed by one draining ERR?.

Model differences handled here:

- **E-816**: axes are letters (``A``-``D``), no homing concept (piezo with
  absolute sensor) — ``home()`` raises. Servo must be ON for MOV.
- **E-861**: axis is usually ``"1"``; a closed-loop absolute MOV requires
  the axis to be referenced once (FRF). ``home()`` = FRF: the stage MOVES to its
  reference switch and the position is then set to the reference value.
"""

from __future__ import annotations

import logging

from mast.environment.serial_transport import (
    SerialLike,
    SerialSettings,
    SerialUnavailable,
    open_transport,
)
from mast.instruments.base import (
    AxisConfig,
    AxisStatus,
    InstrumentError,
    InstrumentUnavailable,
    MotionAxis,
    MotionController,
    serial_open_hint,
)

logger = logging.getLogger(__name__)

__all__ = ["PiGcsAxis", "PiGcsController", "parse_gcs_value"]

#: GCS error codes we intentionally swallow: 10 = "controller was stopped
#: by command" — the documented, expected side-effect of STP.
_BENIGN_ERRORS = {10}


def parse_gcs_value(line: str, axis: str) -> str:
    """Extract the value from a GCS ``<axis>=<value>`` reply line.

    Accepts replies with or without the axis prefix (older E-816 firmware
    replies to single-axis queries with a bare value)."""
    text = line.strip()
    if not text:
        raise InstrumentError(f"empty GCS reply for axis {axis!r}")
    for part in text.splitlines():
        part = part.strip()
        if "=" in part:
            key, _, value = part.partition("=")
            if key.strip().lstrip("\x00").upper() == str(axis).upper():
                return value.strip()
    # bare-value fallback (single-axis query, prefix-less firmware)
    if "=" not in text:
        return text
    raise InstrumentError(f"GCS reply {line!r} has no value for axis {axis!r}")


#: what a velocity readback may differ from the request (the controller rounds)
_VEL_REL_TOL = 0.01


def _gcs_bool(value: str) -> bool:
    return value.strip() in ("1", "True", "true")


class PiGcsAxis(MotionAxis):
    """One GCS axis (letter for E-816, digit for E-861)."""

    def __init__(self, config: AxisConfig, controller: "PiGcsController"):
        super().__init__(config, controller)
        self._pi: PiGcsController = controller
        self._ax = str(config.channel)
        self.SUPPORTS = frozenset({"velocity", "servo"} | (
            {"home"} if controller.model_needs_reference else set()))

    # -- primitives (controller lock already held by the base class) --------

    def _move_abs_raw(self, target: float) -> None:
        self._pi._command(f"MOV {self._ax} {target:.6f}")

    def _get_position_raw(self) -> float:
        reply = self._pi._query(f"POS? {self._ax}")
        try:
            return float(parse_gcs_value(reply, self._ax))
        except ValueError as exc:
            raise InstrumentError(f"bad POS? reply {reply!r}") from exc

    def _get_status_raw(self) -> AxisStatus:
        pos = self._get_position_raw()
        on_target: bool | None = None
        try:
            ont = parse_gcs_value(self._pi._query(f"ONT? {self._ax}"), self._ax)
            on_target = ont.strip() in ("1", "True")
        except InstrumentError:
            pass  # older firmware without ONT? — fall back to not-moving
        homed: bool | None = None
        if self._pi.model_needs_reference:
            try:
                frf = parse_gcs_value(self._pi._query(f"FRF? {self._ax}"), self._ax)
                homed = frf.strip() == "1"
            except InstrumentError:
                homed = None
        return AxisStatus(
            position=pos,
            # GCS has no universal "moving" query at this subset level; the
            # on-target flag is authoritative for settledness.
            moving=(on_target is False),
            on_target=on_target,
            homed=homed,
        )

    def _stop_raw(self) -> None:
        self._pi._stop_all_raw()

    def _home_raw(self) -> None:
        if not self._pi.model_needs_reference:
            raise InstrumentError(
                f"PI {self._pi.model}: axis {self._ax} has an absolute sensor, "
                "no homing procedure exists"
            )
        self._pi._command(f"FRF {self._ax}")

    def _ask(self, cmd: str) -> str | None:
        """``<cmd> <axis>`` → the value, or None when the controller does not answer."""
        try:
            return parse_gcs_value(self._pi._query(f"{cmd} {self._ax}"), self._ax)
        except InstrumentError:
            return None

    def _status_details_raw(self) -> dict:
        out: dict = {}
        failed = False
        queries = [("servo", "SVO?", _gcs_bool), ("velocity", "VEL?", float),
                   ("travel_min_controller", "TMN?", float),
                   ("travel_max_controller", "TMX?", float)]
        if self._pi.model_needs_reference:
            queries += [("has_limit_switches", "LIM?", _gcs_bool),
                        ("has_reference_switch", "TRS?", _gcs_bool)]
        for key, cmd, conv in queries:
            raw = self._ask(cmd)
            try:
                if raw is not None:
                    out[key] = conv(raw)
                    continue
            except ValueError:
                pass
            failed = True
        if "velocity" in out:
            out["velocity_unit"] = f"{self.config.unit}/s"
        if failed:
            # an unanswered query leaves GCS error 2 queued: read it here, or the
            # next set command's ERR? reports it as its own failure
            try:
                self._pi._query("ERR?")
            except InstrumentError:
                pass
        return out

    def _set_velocity_raw(self, velocity: float) -> float | None:
        self._pi._command(f"VEL {self._ax} {velocity:.6f}")
        raw = self._ask("VEL?")
        if raw is None:
            return None
        try:
            back = float(raw)
        except ValueError:
            return None
        if abs(back - velocity) > _VEL_REL_TOL * velocity:
            raise InstrumentError(
                f"PI {self._pi.model}: VEL {self._ax} {velocity:g} reads back {back:g}")
        return back

    def _set_servo_raw(self, on: bool) -> bool | None:
        self._pi._command(f"SVO {self._ax} {1 if on else 0}")
        raw = self._ask("SVO?")
        if raw is None:
            return None
        back = _gcs_bool(raw)
        if back != on:
            raise InstrumentError(
                f"PI {self._pi.model}: SVO {self._ax} {int(on)} reads back {int(back)}")
        return back


class PiGcsController(MotionController):
    """One PI GCS controller on a serial link.

    ``model`` selects the small behavioural differences ("E-816" vs
    "E-861"); anything else defaults to E-861-like behaviour (referenced
    stepper). ``transport`` may be injected for tests.
    """

    IO_TIMEOUT_S = 0.5
    TERMINATOR = b"\n"

    def __init__(
        self,
        *,
        port: str | None = None,
        baudrate: int = 115200,
        model: str = "E-861",
        axes: list[AxisConfig] | None = None,
        servo_on_connect: bool = True,
        transport: SerialLike | None = None,
    ):
        super().__init__()
        if transport is None and not port:
            raise ValueError("PiGcsController needs either port or transport")
        self._port = port
        self._baudrate = int(baudrate)
        self.model = model.upper().strip()
        self._axis_configs = list(axes or [])
        self._servo_on_connect = bool(servo_on_connect)
        self._transport: SerialLike | None = transport
        self._owns_transport = transport is None

    @property
    def model_needs_reference(self) -> bool:
        """E-861 (and other steppers) must be referenced before MOV;
        the E-816 piezo has an absolute sensor."""
        return self.model != "E-816"

    # -- lifecycle -----------------------------------------------------------

    def _connect_raw(self) -> None:
        if self._transport is None:
            settings = SerialSettings(
                port=self._port or "",
                baudrate=self._baudrate,
                timeout=self.IO_TIMEOUT_S,
                write_timeout=self.IO_TIMEOUT_S,
            )
            try:
                t = open_transport(settings)
                t.open()
            except SerialUnavailable as exc:
                raise InstrumentUnavailable(
                    f"PI {self.model} on {self._port}: {exc}{serial_open_hint(self._port)}"
                ) from exc
            self._transport = t
        if self._servo_on_connect:
            for cfg in self._axis_configs:
                try:
                    self._command(f"SVO {cfg.channel} 1")
                except InstrumentError as exc:
                    logger.warning(
                        "PI %s: servo-on for axis %s failed: %s",
                        self.model, cfg.channel, exc,
                    )

    def _close_raw(self) -> None:
        t = self._transport
        if t is not None and self._owns_transport:
            try:
                t.close()
            except Exception:  # noqa: BLE001
                pass
            self._transport = None

    def _build_axes(self) -> dict[str, MotionAxis]:
        return {cfg.name: PiGcsAxis(cfg, self) for cfg in self._axis_configs}

    # -- GCS I/O ---------------------------------------------------------------

    def _raw_transact(self, line: str, *, expect_reply: bool) -> str:
        t = self._transport
        if t is None:
            raise InstrumentUnavailable(f"PI {self.model} transport not connected")
        payload = line.strip().encode("ascii") + self.TERMINATOR
        try:
            if expect_reply:
                raw = t.transact(
                    payload, read_until=self.TERMINATOR, timeout=self.IO_TIMEOUT_S
                )
                return raw.decode("ascii", errors="replace")
            # command with no reply — write, then drain nothing
            t.transact(payload, read_size=0, timeout=0.05)
            return ""
        except SerialUnavailable as exc:
            raise InstrumentUnavailable(f"PI {self.model} I/O failed: {exc}") from exc

    def _query(self, cmd: str) -> str:
        reply = self._raw_transact(cmd, expect_reply=True)
        if not reply.strip():
            raise InstrumentError(f"PI {self.model}: no reply to {cmd!r}")
        return reply

    def _command(self, cmd: str) -> None:
        """Set-command + mandatory ERR? check (GCS set commands are silent;
        the error queue is the only acknowledgement)."""
        self._raw_transact(cmd, expect_reply=False)
        self._check_error(context=cmd)

    def _check_error(self, *, context: str) -> None:
        reply = self._query("ERR?")
        try:
            code = int(reply.strip().split()[-1])
        except (ValueError, IndexError) as exc:
            raise InstrumentError(
                f"PI {self.model}: unparseable ERR? reply {reply!r} after {context!r}"
            ) from exc
        if code and code not in _BENIGN_ERRORS:
            raise InstrumentError(
                f"PI {self.model}: GCS error {code} after {context!r}"
            )

    def _stop_all_raw(self) -> None:
        """STP stops all axes and by design queues error 10 — swallow it."""
        self._raw_transact("STP", expect_reply=False)
        try:
            self._check_error(context="STP")
        except InstrumentError:
            # A stop must never raise on the panic path; the error queue is
            # cleared by the ERR? read itself.
            pass
