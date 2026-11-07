"""NPI Rainbow 1064 OEM — ps fibre laser, ASCII serial control.

Contract: ``docs/v2/design/optical_station_witec.md`` §3 / §6.1 / D6 / D7.

What may be sent — and where each command comes from
====================================================
Every command below is in **"Rainbow 1064 OEM User Manual V2.0.0" (NPI Lasers),
§3.3.2 "Commands for Remote Control", Table 1 (manual p.9)**. The syntax "must
include all shown characters and symbols (``\\r``)": a command is ASCII text
ended by a carriage return.

=================  ====  =============================================  =====================
command            mode  manual text                                    reply (manual)
=================  ====  =============================================  =====================
sEmission_flag1    W     "Switch ON/OFF laser (1: ON, 0: OFF)"          Emission ON Finished
sEmission_flag0    W                                                    Emission OFF Finished
sRemote_flag0      W     "Enable remote control (1: local, 0: remote)"  Remote/Local Set Finished
sRemote_flag1      W
gstatus            R     "LED status (four numbers correspond to four   ``LED Status: 1100``
                         LED states)", e.g. 1100 = Power + Ready lit
gProduct           R     "Product series"                               ``1064/1550``
gSN                R     "Product's serial number"                      e.g. ``XZ00000000``
g_case_temp        R     hex, 16-bit two's complement, "25°C = 0C80h,   e.g. ``0C80``
                         1°C = 0080h, 0.25°C = 0020h"
=================  ====  =============================================  =====================

LED order (manual §3.1.3-3.1.6): Power, Ready, Laser, Error. The Laser LED "indicates
that the ON/OFF Switch is in state ON, which means the laser is ON and the laser beam
is emitted" — so it is the emission read-back used here. The case temperature is
``signed16(value) / 128`` °C: that scale is the one the manual's three examples share.

Baud rate: the manual only says "open a terminal program and select a serial port".
115200 8N1 is the setting this driver uses.
The reply terminator is not documented either: replies are read up to LF (or the I/O
timeout) and parsed tolerantly; the raw reply text is always returned next to the parsed
value so the bench self-test can show what the laser really says.

What is NEVER sent
==================
``sDB9_ON_OFF_EN*`` (documented, but it changes who may switch the laser — left to the
operator), and everything beyond the manual (factory / service-level commands). :meth:`NpiRainbow._command`
sends only the exact byte strings in :data:`ALLOWED_COMMANDS`; anything else raises.

Power is not software-controllable
==================================
The manual documents no power command (the GUI's "pump current 10-100 %, Set/Save" has no
documented serial syntax). ``SUPPORTS`` is therefore ``{"emission"}``; the pump-current
percentage is a *state the operator declares* (``npi_power_pct`` slot, see
:mod:`mast.operator_hitl.optical_state`), not something MAST reads or writes.

Remote vs. Local mode — read before touching ``emission_on``
============================================================
"Remote" (``sRemote_flag0``) hands emission control to software. **In Remote mode the front
panel ON/OFF switch and the D-Sub 9 control are not active** (manual §4.2.1) — nobody can
switch the laser off at the panel any more until Local mode is restored. The vendor GUI
puts the laser into Remote at start-up. This driver therefore:

* never touches the mode on connect, :meth:`status` or :meth:`identify` (read-only);
* switches to Remote **only inside** :meth:`emission_on` (needed for software emission
  control) and, as a fallback, inside :meth:`emission_off` when the Laser LED stays lit
  after a plain ``sEmission_flag0`` (the front-panel switch may be what holds it on);
* never returns to Local by itself: leaving Remote with the front switch at ON would make
  the laser emit. :meth:`release_to_front_panel` exists for the operator's hand-over and
  refuses unless the caller attests the switch is physically OFF and emission is off.

Gates (D7), in this order in front of every path that can start light: ``allow_emission_control``
(device config, default false), ``write_enabled`` (default false: the command syntax is documented,
but the replies and the Remote-mode behaviour have not been seen on the real laser - the bench
self-test comes first) and the operator's ``laser_area_safe`` attestation, checked by
:class:`~mast.instruments.laser.LaserAttestationGuard` which ``build()`` installs unless the entry says
``require_laser_attestation: false`` (the ``LaserEmissionOn`` skill checks it first, with a better
message). The paths are ``emission_on`` and ``release_to_front_panel`` (Local mode with the panel switch
at ON would emit). Switching emission *off* needs none of them and never raises.

The bench self-test (first step on the real laser, no light involved): ``identify()`` —
``gProduct``, ``gSN``, ``gstatus``, ``g_case_temp``. Stop the vendor GUI first: it opens
the port (AutoStart) and the port is exclusive.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

from mast.environment.serial_transport import (
    SerialLike,
    SerialSettings,
    SerialUnavailable,
    open_transport,
)
from mast.i18n import tr
from mast.instruments.base import InstrumentError, InstrumentUnavailable, serial_open_hint
from mast.instruments.laser import EmissionGuard, Laser, LaserLimits, default_emission_guard
from mast.instruments.safe_direction import LockTimeout, held, safe_direction

logger = logging.getLogger(__name__)

__all__ = ["NpiRainbow", "NpiProtocolError", "ALLOWED_COMMANDS", "parse_led_status",
           "parse_case_temp_c", "build"]

#: tests replace these two so that polling loops run instantly
_sleep = time.sleep
_monotonic = time.monotonic

# ── the command table (manual Table 1) ──────────────────────────────────────

CMD_EMISSION_ON = b"sEmission_flag1\r"
CMD_EMISSION_OFF = b"sEmission_flag0\r"
CMD_REMOTE = b"sRemote_flag0\r"        # 0 = remote (software) control
CMD_LOCAL = b"sRemote_flag1\r"         # 1 = local (front-panel switch) control
CMD_STATUS = b"gstatus\r"
CMD_PRODUCT = b"gProduct\r"
CMD_SERIAL = b"gSN\r"
CMD_CASE_TEMP = b"g_case_temp\r"

#: The only byte strings :meth:`NpiRainbow._command` will ever write.
ALLOWED_COMMANDS: frozenset[bytes] = frozenset({
    CMD_EMISSION_ON, CMD_EMISSION_OFF, CMD_REMOTE, CMD_LOCAL,
    CMD_STATUS, CMD_PRODUCT, CMD_SERIAL, CMD_CASE_TEMP,
})

_LED_NAMES = ("power", "ready", "laser", "error")
#: three-quarter second per read command; writes get longer (the laser may need a moment)
READ_TIMEOUT_S = 0.8
WRITE_TIMEOUT_S = 3.0


class NpiProtocolError(InstrumentError):
    """The laser answered, but not in a way the manual describes."""


# ── parsing (pure functions, unit-tested against the manual's examples) ──────


def parse_led_status(reply: str) -> dict[str, bool] | None:
    """``"LED Status: 1100"`` → ``{power: True, ready: True, laser: False, error: False}``.

    Manual Table 1: "Four Numbers correspond to four LED states" (Power, Ready, Laser,
    Error). The last run of exactly four 0/1 characters in the reply is taken, so a
    prefix text does not matter. ``None`` when there is no such run."""
    runs = re.findall(r"(?<![01])[01]{4}(?![01])", reply or "")
    if not runs:
        return None
    bits = runs[-1]
    return {name: bits[i] == "1" for i, name in enumerate(_LED_NAMES)}


def parse_case_temp_c(reply: str) -> float | None:
    """``"0C80"`` → 25.0 (manual Table 1: 16-bit two's complement, 0C80h = 25 °C, 0080h =
    1 °C, 0020h = 0.25 °C, i.e. ``signed16 / 128``). ``None`` if no hex word is found."""
    toks = re.findall(r"(?<![0-9a-zA-Z])(?:0[xX])?([0-9a-fA-F]{1,4})[hH]?(?![0-9a-zA-Z])",
                      (reply or "").strip())
    if not toks:
        return None
    raw = int(toks[-1], 16) & 0xFFFF
    if raw & 0x8000:
        raw -= 0x10000
    return raw / 128.0


def _led_bits(leds: dict[str, bool] | None) -> str:
    if not leds:
        return ""
    return "".join("1" if leds[n] else "0" for n in _LED_NAMES)


# ── driver ───────────────────────────────────────────────────────────────────


class NpiRainbow(Laser):
    """The 1064 nm picosecond laser. Emission on/off only (see the module docstring)."""

    SUPPORTS = frozenset({"emission"})

    def __init__(self, *, port: str | None = None, baudrate: int = 115200,
                 transport: SerialLike | None = None,
                 device_id: str = "", name: str = "",
                 limits: LaserLimits | None = None,
                 allow_emission_control: bool = False, write_enabled: bool = False,
                 emission_confirm_s: float = 5.0, emission_guard: EmissionGuard | None = None):
        super().__init__(device_id=device_id, name=name, limits=limits,
                         allow_emission_control=allow_emission_control,
                         write_enabled=write_enabled, emission_guard=emission_guard)
        if transport is None and not port:
            raise InstrumentError(f"npi_rainbow {device_id or ''}: a COM port is required")
        self._port = port
        self._baudrate = int(baudrate)
        self._transport: SerialLike | None = transport
        self._owns_transport = transport is None
        self._io_lock = threading.RLock()
        self.emission_confirm_s = float(emission_confirm_s)
        self._identity: dict[str, str] = {}
        #: what MAST last sent about the mode (the laser cannot be asked): True = remote
        self._remote_set_by_mast: bool | None = None
        #: what the last emission_off() did (for the skill's report)
        self.last_off_report: dict[str, Any] = {}

    @property
    def emission_on_warning(self) -> str:
        """Shown with a successful switch-on: Remote mode took the panel switch away."""
        return tr("激光现在处于 Remote 模式：前面板 ON/OFF 开关和 DB9 控制失效，只能由软件关出光（LaserEmissionOff）。",
                  "The laser is now in Remote mode: the front-panel ON/OFF switch and the DB9 control are inactive; "
                  "only software (LaserEmissionOff) can switch emission off.")

    # -- transport --------------------------------------------------------------

    def _ensure_open(self) -> SerialLike:
        t = self._transport
        if t is not None:
            return t
        settings = SerialSettings(port=self._port or "", baudrate=self._baudrate,
                                  timeout=READ_TIMEOUT_S, write_timeout=READ_TIMEOUT_S)
        t = open_transport(settings)
        try:
            t.open()
        except SerialUnavailable as exc:
            raise InstrumentUnavailable(
                f"NPI Rainbow on {self._port}: {exc}"
                + tr(" —— 串口是独占的：Rainbow OEM 程序（启动即自动连接）开着就打不开，请先在那边断开或退出。",
                     " -- the serial port is exclusive: it cannot be opened while the Rainbow OEM program "
                     "(auto-connects at start-up) is running; disconnect or quit it first.")
                + serial_open_hint(self._port)) from exc
        self._transport = t
        return t

    def _drop_transport(self) -> None:
        t, self._transport = self._transport, None
        if t is not None and self._owns_transport:
            try:
                t.close()
            except Exception:  # noqa: BLE001
                pass
        self._remote_set_by_mast = None

    def _command(self, cmd: bytes, *, timeout: float = READ_TIMEOUT_S) -> str:
        """Send one allow-listed command and return the reply text (stripped)."""
        if cmd not in ALLOWED_COMMANDS:
            # a factory/service command or a typo:
            # never written, whatever the caller meant
            raise InstrumentError(f"NPI command {cmd!r} is not in the manual's command table - refused")
        with held(self._io_lock, f"{self.device_id or 'NPI Rainbow'}: serial port"):
            t = self._ensure_open()
            try:
                raw = t.transact(cmd, read_until=b"\n", timeout=timeout)
            except SerialUnavailable as exc:
                self._drop_transport()
                raise InstrumentUnavailable(f"NPI Rainbow on {self._port}: I/O failed: {exc}") from exc
        text = bytes(raw or b"").decode("ascii", errors="replace").strip()
        if not text:
            raise InstrumentUnavailable(
                f"NPI Rainbow on {self._port}: no reply to {cmd.strip().decode()!r} within {timeout:g}s "
                + tr("（是否上电、是否接对串口？Rainbow OEM 开着会占住串口。）",
                     "(is it powered, is this the right port? A running Rainbow OEM holds the port.)"))
        return text

    # -- reads --------------------------------------------------------------------

    def led_status(self) -> dict[str, Any]:
        """``{leds: {power, ready, laser, error}, raw}`` from ``gstatus``."""
        text = self._command(CMD_STATUS)
        leds = parse_led_status(text)
        if leds is None:
            raise NpiProtocolError(f"gstatus reply has no four-LED field: {text!r}")
        return {"leds": leds, "raw": text}

    def _cached(self, key: str, cmd: bytes) -> str:
        if key not in self._identity:
            self._identity[key] = self._command(cmd)
        return self._identity[key]

    def identify(self) -> dict:
        """Read-only self-check: ``gProduct``, ``gSN``, ``gstatus``, ``g_case_temp`` (all in the
        manual). Sends nothing that changes the laser; replies are returned verbatim too."""
        out: dict[str, Any] = {"device_id": self.device_id, "port": self._port,
                               "commands_used": [c.strip().decode() for c in
                                                 (CMD_PRODUCT, CMD_SERIAL, CMD_STATUS, CMD_CASE_TEMP)]}
        replies: dict[str, str] = {}
        for key, cmd in (("product", CMD_PRODUCT), ("serial_number", CMD_SERIAL)):
            try:
                self._identity.pop(key, None)
                replies[key] = out[key] = self._cached(key, cmd)
            except InstrumentError as exc:
                out[key] = None
                out.setdefault("errors", {})[key] = str(exc)
        try:
            st = self.led_status()
            out["leds"], replies["gstatus"] = st["leds"], st["raw"]
        except InstrumentError as exc:
            out["leds"] = None
            out.setdefault("errors", {})["gstatus"] = str(exc)
        try:
            text = self._command(CMD_CASE_TEMP)
            replies["g_case_temp"] = text
            out["case_temp_c"] = parse_case_temp_c(text)
        except InstrumentError as exc:
            out["case_temp_c"] = None
            out.setdefault("errors", {})["g_case_temp"] = str(exc)
        out["replies"] = replies
        out["ok"] = not out.get("errors")
        return out

    def _status_raw(self) -> dict:
        out: dict[str, Any] = {"port": self._port, "model": "NPI Rainbow 1064 OEM",
                               "remote_set_by_mast": self._remote_set_by_mast,
                               "power_control": "operator-declared only (no documented command)"}
        errors: dict[str, str] = {}
        try:
            st = self.led_status()
            out["leds"], out["led_raw"] = st["leds"], _led_bits(st["leds"])
            out["emission"] = bool(st["leds"]["laser"])
            out["ready"] = bool(st["leds"]["ready"])
            out["error_led"] = bool(st["leds"]["error"])
        except InstrumentUnavailable:
            raise
        except InstrumentError as exc:
            errors["gstatus"] = str(exc)
            out["emission"] = None
        for key, cmd in (("product", CMD_PRODUCT), ("serial_number", CMD_SERIAL)):
            try:
                out[key] = self._cached(key, cmd)
            except InstrumentUnavailable:
                raise
            except InstrumentError as exc:
                errors[key] = str(exc)
        try:
            text = self._command(CMD_CASE_TEMP)
            out["case_temp_c"], out["case_temp_raw"] = parse_case_temp_c(text), text
        except InstrumentUnavailable:
            raise
        except InstrumentError as exc:
            errors["g_case_temp"] = str(exc)
        if errors:
            out["errors"] = errors
        return out

    def _emission_state_raw(self) -> bool | None:
        try:
            return bool(self.led_status()["leds"]["laser"])
        except InstrumentError:
            return None

    # -- emission -------------------------------------------------------------------

    def _emission_raw(self, on: bool) -> bool:
        return self._switch_on() if on else self._switch_off()

    def _switch_on(self) -> bool:
        st = self.led_status()["leds"]
        if st["error"]:
            raise InstrumentError(tr("NPI 激光的 Error 灯亮着，不开出光（先看激光本体，重启后仍亮就联系 NPI）。",
                                     "The NPI laser's Error LED is lit - emission not switched on "
                                     "(check the laser; contact NPI if it persists after a restart)."))
        if not (st["power"] and st["ready"]):
            raise InstrumentError(tr(f"NPI 激光还没就绪（LED {_led_bits(st)}，需要 Power 与 Ready 亮）——不开出光。",
                                     f"The NPI laser is not ready (LEDs {_led_bits(st)}; Power and Ready must be lit) "
                                     "- emission not switched on."))
        if st["laser"]:
            return True                              # already emitting; nothing to send
        try:
            # Remote mode: needed for software emission control, and it disables the front-panel
            # switch (module docstring). Idempotent, so it is sent every time instead of trusted.
            self._command(CMD_REMOTE, timeout=WRITE_TIMEOUT_S)
            self._remote_set_by_mast = True
            self._command(CMD_EMISSION_ON, timeout=WRITE_TIMEOUT_S)
            deadline = _monotonic() + self.emission_confirm_s
            while True:
                leds = self.led_status()["leds"]
                if leds["error"]:
                    raise InstrumentError(tr("开出光后 Error 灯亮了，已发关出光命令。",
                                             "The Error LED lit after the emission-on command; emission-off was sent."))
                if leds["laser"]:
                    return True
                self._check_not_preempted("emission on")            # an emission-off asked for since the switch-on began
                if _monotonic() >= deadline:
                    raise InstrumentError(
                        tr(f"{self.emission_confirm_s:g} s 内 Laser 灯没亮（LED {_led_bits(leds)}），已发关出光命令。",
                           f"The Laser LED did not light within {self.emission_confirm_s:g} s "
                           f"(LEDs {_led_bits(leds)}); emission-off was sent."))
                _sleep(0.25)
        except BaseException:
            # whatever went wrong after the first write, the laser's state is not known to be
            # "off" any more: go to the safe side before the error propagates
            self._safe_off()
            raise

    def _safe_off(self) -> None:
        try:
            with safe_direction():                   # a port that is stuck must not hold the failing switch-on hostage
                self._command(CMD_EMISSION_OFF, timeout=WRITE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - the safe direction must not throw
            logger.warning("NPI %s: emission-off after a failed switch-on also failed: %s", self.device_id, exc)

    def _switch_off(self) -> bool:
        """Best effort, every step independent; returns the read-back (the base class's
        ``emission_off`` swallows anything that still escapes)."""
        self.last_off_report = {"remote_mode_entered_for_off": False}
        try:
            self._command(CMD_EMISSION_OFF, timeout=WRITE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            self._note_off_failure(exc)
            logger.warning("NPI %s: sEmission_flag0 failed: %s", self.device_id, exc)
        state: bool | None = None
        try:
            state = bool(self.led_status()["leds"]["laser"])
        except Exception as exc:  # noqa: BLE001
            self._note_off_failure(exc)
            logger.warning("NPI %s: could not read the Laser LED after emission-off: %s", self.device_id, exc)
        if state:
            # Still lit: the front-panel switch (Local mode) may be what holds it on. Remote
            # mode puts software in charge (manual §4.2.1), then the off is repeated.
            try:
                self._command(CMD_REMOTE, timeout=WRITE_TIMEOUT_S)
                self._remote_set_by_mast = True
                self.last_off_report["remote_mode_entered_for_off"] = True
                self._command(CMD_EMISSION_OFF, timeout=WRITE_TIMEOUT_S)
                state = bool(self.led_status()["leds"]["laser"])
            except Exception as exc:  # noqa: BLE001
                self._note_off_failure(exc)
                logger.warning("NPI %s: remote + emission-off retry failed: %s", self.device_id, exc)
        self.last_off_report["laser_led"] = state
        if state is None:
            raise InstrumentError("NPI emission state unknown after emission-off")
        return state

    def _note_off_failure(self, exc: BaseException) -> None:
        """A step of the off path that ran into a busy port says so in the report (the skill shows it)."""
        if isinstance(exc, LockTimeout):
            self.last_off_report["lock_timeout"] = True
            self.last_off_report["error"] = str(exc)

    def release_to_front_panel(self, *, front_switch_confirmed_off: bool = False) -> bool:
        """``sRemote_flag1``: give control back to the front-panel ON/OFF switch.

        Dangerous in exactly one way: if that switch is physically at ON, the laser starts
        emitting the moment Local mode is restored. MAST cannot read the switch, so the caller
        must **attest** that a person has seen it at OFF (``front_switch_confirmed_off=True``);
        emission must also read as off. No skill calls this; it is for the operator's hand-over. It can end
        in light, so it passes the same gates as ``emission_on`` (config, write_enabled, operator guard)."""
        epoch = self.off_epoch
        self._check_emission_gates("return to local mode")            # early: refuse before waiting for the device
        if front_switch_confirmed_off is not True:
            raise InstrumentError(tr("交还前面板控制前，必须由人确认前面板开关在 OFF 位（front_switch_confirmed_off=True）。",
                                     "A person must confirm the front-panel switch is at OFF before local mode is "
                                     "restored (front_switch_confirmed_off=True)."))
        with self._light_scope(epoch):
            self._check_emission_gates("return to local mode")        # again, now that this holds the device
            if self.led_status()["leds"]["laser"]:
                raise InstrumentError("the Laser LED is lit - switch emission off first")
            self._command(CMD_LOCAL, timeout=WRITE_TIMEOUT_S)
            self._remote_set_by_mast = False
            return True

    def close(self) -> None:
        self._drop_transport()


def build(entry: dict) -> NpiRainbow:
    """Registry factory (``registry._STATION_DRIVERS['npi_rainbow']``). Opens nothing."""
    dev_id = str(entry.get("id", ""))
    port = str(entry.get("port") or "").strip()
    if not port:
        raise InstrumentError(f"optical device {dev_id!r} (npi_rainbow): 'port' is required")
    return NpiRainbow(
        port=port, baudrate=int(entry.get("baudrate", 115200)),
        device_id=dev_id, name=str(entry.get("name") or dev_id),
        limits=LaserLimits.from_dict(entry.get("limits")),
        allow_emission_control=bool(entry.get("allow_emission_control", False)),
        write_enabled=bool(entry.get("write_enabled", False)),
        emission_confirm_s=float(entry.get("emission_confirm_s", 5.0)),
        emission_guard=default_emission_guard(bool(entry.get("require_laser_attestation", True))))
