"""Laser / TunableLaser — light sources that MAST may switch or tune.

Design contract: ``docs/v2/design/optical_station_witec.md`` §6.1 and decision D7.

Template methods check :class:`LaserLimits` and call ``_*_raw`` primitives;
out-of-range is refused (:class:`LaserLimitError`), never clamped.

Emission safety is two gates, and **both are enforced where the light is switched**, so a
protocol that calls the driver directly cannot walk around them:

1. **Config gate:** :meth:`Laser.emission_on` refuses unless the device entry sets
   ``allow_emission_control: true`` (default false), and — like every setting — unless
   ``write_enabled`` is on.
2. **Operator gate:** the operator's attestation ``laser_area_safe``
   (:mod:`mast.operator_hitl.optical_state`), checked by an injectable
   :data:`EmissionGuard`. The real drivers install :class:`LaserAttestationGuard` by default
   (entry key ``require_laser_attestation``, default true; ``false`` installs none); the
   simulators install none unless the entry asks. The ``LaserEmissionOn`` skill checks the
   same attestation first, because it can tell the agent who must do what.

The order in front of *every* action that **adds light on the sample** — ``emission_on`` and whatever
else a driver offers that makes more light than there is now (:meth:`Laser._check_emission_gates`): dark
to lit, and also a lit channel or the output level made higher while the source is, or may be, emitting —
is SUPPORTS → ``allow_emission_control`` → ``write_enabled`` → guard → the ``_*_raw`` primitive. So
``allow_emission_control=false`` means "MAST never adds light": in that manual mode the operator lights the
source and MAST may still tune the colour, turn the light down and switch it off. Not knowing the current
value counts as "adds" (fail closed); the same level, a lower level, 0, the wavelength and everything in
the off direction are never gated.

Two more rules close the gap between "checked" and "acted". The gates are checked **again once the
operation holds the device lock** - the first check only refuses early, so nothing queues behind another operation
for nothing; whatever changed while it queued (the attestation withdrawn, a config flag turned off) cancels it.
And an ``emission_off()`` asked for after an operation that adds light has started **cancels it**: every
``emission_off()`` call counts one "shutdown generation" (:attr:`Laser.off_epoch`, never reset); the operation
reads it before it can wait, and compares it when it gets the device and while it switches on
(:class:`EmissionPreempted`, and a switch-on already under way is switched back off). Dimming and everything in
the off direction are never cancelled.

Turning emission *off* is always attempted, passes no gate and never raises. It does not wait forever
either: the device lock and the port are waited for at most
:data:`~mast.instruments.safe_direction.SAFE_LOCK_TIMEOUT_S` in total, then it answers ``None`` ("not
confirmed"), logs, and leaves the reason in :attr:`Laser.last_off_report`.

``write_enabled`` (default true for sims, false for real drivers whose register map
is not yet verified on the bench) gates every *setting* write except emission-off.
"""

from __future__ import annotations

import contextlib
import logging
import math
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from mast.i18n import tr
from mast.instruments.base import InstrumentError
from mast.instruments.external_stop import ExternalStopMixin
from mast.instruments.safe_direction import LockTimeout, held, safe_direction

logger = logging.getLogger(__name__)

_EPOCH_LOCK = threading.Lock()   # the shutdown generation changes rarely and instantly: one tiny lock for every laser

__all__ = ["LaserLimitError", "EmissionControlDisabled", "EmissionPreempted", "LaserAttestationRequired",
           "LaserAttestationGuard", "EmissionGuard", "default_emission_guard", "LaserLimits", "Laser",
           "TunableLaser", "SimLaser", "SimTunableLaser"]


class LaserLimitError(InstrumentError):
    """A requested laser setting lies outside the configured limits."""


class EmissionControlDisabled(InstrumentError):
    """Software emission control is not enabled for this device (config)."""


class EmissionPreempted(InstrumentError):
    """An operation that adds light was cancelled because an emission-off was asked for after it started (while it
    waited for the device, or while it was switching on). The off wins."""


#: ``guard(what)``: returns if software may start laser emission now, raises an :class:`InstrumentError` (with a
#: message the operator can act on) if not. *what* says which action is about to happen.
EmissionGuard = Callable[[str], None]


class LaserAttestationRequired(InstrumentError):
    """MAST was about to start (or may start) laser emission with no valid operator attestation ``laser_area_safe``
    (docs D7, gate 2). Only the operator can attest."""


class LaserAttestationGuard:
    """The default emission guard: the operator's current, **positive** ``laser_area_safe`` attestation.

    Only a person can set that slot (the optical-state panel in the MAST UI, or the pop-up); no skill can, so this
    is not a switch an agent can flip. ``guard(what)`` raises :class:`LaserAttestationRequired` unless the
    attestation is in force; :meth:`status` says why not. ``mast.operator_hitl.optical_state`` is imported when
    the guard is used, not when this module is.

    Same semantics as the WITec driver's guard (one shared implementation): only ``is_valid(slot, expect=True)``
    counts, and a slot the operator recorded as *not* safe that has not expired is its own state (``"unsafe"``)."""

    SLOT = "laser_area_safe"

    def status(self) -> str:
        """``"valid"``; ``"unsafe"`` (the operator recorded the area as NOT safe, and that is still in force);
        ``"missing"`` (never signed, cleared, or expired); ``"no_slot"`` (the optical state has no such slot)."""
        from mast.operator_hitl import optical_state

        try:
            store = optical_state.get_store()
            if store.is_valid(self.SLOT, expect=True):
                return "valid"
            return "unsafe" if store.is_valid(self.SLOT, expect=False) else "missing"
        except optical_state.OpticalStateError:
            return "no_slot"

    def _validity(self) -> tuple[str, str]:
        from mast.operator_hitl import optical_state

        try:
            secs = optical_state.get_store().slot(self.SLOT).valid_for_s
        except optical_state.OpticalStateError:
            secs = None
        if not secs:
            return "签署后一直有效，直到被改动", "it stays valid until it is changed"
        hours = float(secs) / 3600.0
        return f"签署后 {hours:g} 小时内有效", f"it is valid for {hours:g} h after signing"

    def problem(self) -> str | None:
        """Why software may not start emission right now (in the UI language), or ``None``."""
        status = self.status()
        if status == "valid":
            return None
        if status == "no_slot":
            return tr("光学状态里没有定义 laser_area_safe 槽位，无法确认激光区安全",
                      "the optical state defines no 'laser_area_safe' slot, so laser-area safety cannot be "
                      "confirmed")
        if status == "unsafe":
            return tr("操作员把「激光区安全」（laser_area_safe）记成了「不安全」：软件不得让激光出光；"
                      "等操作员确认安全后，在 MAST 界面（光学状态面板）或弹窗里重新签署",
                      "the operator has recorded 'laser area safe' (laser_area_safe) as NOT safe: software may "
                      "not start laser emission; once the operator has checked, they sign it again in the MAST UI "
                      "(optical-state panel) or in the pop-up")
        valid_zh, valid_en = self._validity()
        return tr("没有有效的「激光区安全」签署（laser_area_safe）：软件让激光出光之前，请操作员在 MAST 界面"
                  f"（光学状态面板）或弹窗里签署「激光区安全」（护目镜、光路封闭、光路上无人），{valid_zh}；"
                  "这个签署只有操作员能做",
                  "no valid 'laser area safe' attestation (laser_area_safe): before software starts laser "
                  "emission, ask the operator to sign 'laser area safe' in the MAST UI (optical-state panel) or "
                  f"in the pop-up (eyewear on, beam path enclosed, nobody in the beam); {valid_en}; "
                  "only the operator can do that")

    def __call__(self, what: str) -> None:
        why = self.problem()
        if why:
            raise LaserAttestationRequired(f"{what} - {why}")


def default_emission_guard(require: bool) -> EmissionGuard | None:
    """What a real driver's ``build()`` installs: the attestation guard, unless the entry says
    ``require_laser_attestation: false``. (The simulators install a guard only when the entry asks for one.)"""
    return LaserAttestationGuard() if require else None


def _num(value: Any, what: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise LaserLimitError(f"{what} must be a number, got {value!r}") from exc
    if not math.isfinite(v):
        raise LaserLimitError(f"{what} must be finite")
    return v


@dataclass(frozen=True)
class LaserLimits:
    """``limits`` in the device entry. ``None`` = that control is refused."""

    min_nm: float | None = None
    max_nm: float | None = None
    max_power: float | None = None          # in ``power_unit``
    power_unit: str = "pct"                 # "pct" | "mW"
    max_amplitude_pct: float | None = None  # tunable filter channel amplitude

    @classmethod
    def from_dict(cls, raw: dict | None) -> "LaserLimits":
        raw = dict(raw or {})
        kw: dict[str, Any] = {}
        for key in ("min_nm", "max_nm", "max_power", "max_amplitude_pct"):
            if raw.get(key) is not None:
                kw[key] = float(raw[key])
        if raw.get("power_unit"):
            kw["power_unit"] = str(raw["power_unit"])
        lim = cls(**kw)
        if (lim.min_nm is None) != (lim.max_nm is None):
            raise ValueError("laser limits: give both min_nm and max_nm or neither")
        if lim.min_nm is not None and not lim.min_nm < lim.max_nm:
            raise ValueError("laser limits: need min_nm < max_nm")
        return lim

    def as_dict(self) -> dict:
        return {"min_nm": self.min_nm, "max_nm": self.max_nm, "max_power": self.max_power,
                "power_unit": self.power_unit, "max_amplitude_pct": self.max_amplitude_pct}


class Laser(ExternalStopMixin, ABC):
    """A switchable light source. Drivers implement ``_*_raw``."""

    #: what this driver can do (subset of {"emission", "power", "wavelength", "amplitude"})
    SUPPORTS: frozenset[str] = frozenset({"emission"})

    def __init__(self, *, device_id: str = "", name: str = "",
                 limits: LaserLimits | None = None,
                 allow_emission_control: bool = False, write_enabled: bool = True,
                 emission_guard: EmissionGuard | None = None):
        self.device_id = device_id
        self.name = name or device_id
        self.limits = limits or LaserLimits()
        self.allow_emission_control = bool(allow_emission_control)
        self.write_enabled = bool(write_enabled)
        #: the operator gate (module docstring); ``None`` = no operator check in the driver
        self.emission_guard: EmissionGuard | None = emission_guard
        #: what the last ``emission_off()`` did, for the skill's report (drivers add detail; an off that could
        #: not even start - the device was busy - says so here)
        self.last_off_report: dict[str, Any] = {}
        #: the shutdown generation (see :attr:`off_epoch`) and, per thread, the one the running operation started under
        self._off_epoch = 0
        self._op = threading.local()
        self._lock = threading.RLock()

    # -- the second check and the shutdown generation -----------------------------------------------------

    @property
    def off_epoch(self) -> int:
        """The shutdown generation: how many ``emission_off()`` calls were asked for since this driver was built.
        Never reset. An operation that adds light reads it when it starts - before it can wait for the device - and is
        cancelled (:class:`EmissionPreempted`) if it has moved by the time the operation gets to act or while it is
        switching on: an off asked for after the on started wins, whoever asked and whenever the lock came free."""
        return int(getattr(self, "_off_epoch", 0) or 0)

    def _bump_off_epoch(self) -> int:
        with _EPOCH_LOCK:
            n = int(getattr(self, "_off_epoch", 0) or 0) + 1
            self._off_epoch = n
            return n

    def _op_state(self) -> threading.local:
        st = getattr(self, "_op", None)
        if st is None:                       # a subclass that skipped __init__ (test double)
            with _EPOCH_LOCK:
                st = getattr(self, "_op", None)
                if st is None:
                    st = self._op = threading.local()
        return st

    def _check_not_preempted(self, what: str) -> None:
        """Inside an operation that adds light: raise :class:`EmissionPreempted` if an emission-off was asked for since
        it started. Outside one (an early check, a read) it does nothing."""
        entry = getattr(self._op_state(), "epoch", None)
        if entry is not None and entry != self.off_epoch:
            raise EmissionPreempted(tr(f"{self.device_id}: {what}被关出光指令打断",
                                       f"{self.device_id}: {what} was pre-empted by an emission-off"))

    @contextlib.contextmanager
    def _light_scope(self, epoch: int | None = None):
        """Hold the device lock for an operation that may add light. The shutdown generation is the one read before
        the lock was waited for (``epoch``, else now); every gate check made while the scope is open also fails if an
        emission-off was asked for since. Nested in an operation of this thread (the lock is already ours) it adds
        nothing: the outermost operation's generation stays."""
        st = self._op_state()
        if getattr(st, "epoch", None) is not None:
            yield
            return
        entry = self.off_epoch if epoch is None else int(epoch)
        with self._lock:
            st.epoch = entry
            try:
                yield
            finally:
                st.epoch = None

    @contextlib.contextmanager
    def _adds_light(self, action: str):
        """What every operation that adds light does, in this order: read the shutdown generation (before anything
        can make it wait); check the gates, to refuse EARLY instead of queueing behind another operation; take the
        device lock; check the gates AGAIN now that it holds the device (an attestation withdrawn, a config flag turned
        off, or an emission-off asked for while it queued, cancels it); run the body."""
        nested = getattr(self._op_state(), "epoch", None) is not None
        epoch = self.off_epoch
        if not nested:
            self._check_emission_gates(action)
        with self._light_scope(epoch):
            self._check_emission_gates(action)
            yield

    def _require_writes(self, what: str) -> None:
        if not self.write_enabled:
            raise EmissionControlDisabled(
                f"{self.device_id}: {what} refused — write_enabled is false in the device "
                "config (enable it only after the bench self-test)")

    def _check_emission_gates(self, action: str) -> None:
        """Every gate in front of an action that adds light on the sample, in this order: the driver supports
        software emission → ``allow_emission_control`` → ``write_enabled`` → the operator guard (→ not pre-empted by an
        emission-off, when called inside an operation that holds the device). Raises; never switches anything.
        Drivers call it from *every* path that makes more light than there is now (``emission_on`` here; e.g. the
        NKT's RF power on, or a channel made brighter) and never from a path that turns light down or off."""
        if "emission" not in self.SUPPORTS:
            raise EmissionControlDisabled(f"{self.device_id}: no software emission control")
        if not self.allow_emission_control:
            raise EmissionControlDisabled(
                f"{self.device_id}: {action} refused - emission control is disabled in the device config "
                "(allow_emission_control=false)")
        self._require_writes(action)
        if self.emission_guard is not None:
            self.emission_guard(f"{self.device_id}: {action}")
        self._check_not_preempted(action)

    def status(self) -> dict:
        st = dict(self._status_raw() or {})
        st.setdefault("device_id", self.device_id)
        st.setdefault("allow_emission_control", self.allow_emission_control)
        st.setdefault("write_enabled", self.write_enabled)
        st.setdefault("operator_guard", self.emission_guard is not None)      # does the driver check the attestation?
        return st

    def emission_state(self) -> bool | None:
        v = self._emission_state_raw()
        return None if v is None else bool(v)

    def emission_on(self) -> bool:
        with self._adds_light("emission on"):          # gates early, device lock, gates again, then the primitive
            return bool(self._emission_raw(True))

    def emission_off(self) -> bool | None:
        """Always attempted, never raises; returns the read-back state if known, ``None`` if it is NOT confirmed
        (the command or its read-back failed, or the device was busy: see :attr:`last_off_report`). It does not
        wait forever: the device lock and the port are waited for at most ``SAFE_LOCK_TIMEOUT_S`` in total. Asking for
        it counts one shutdown generation FIRST, so an operation that adds light and started earlier is cancelled
        even if this off cannot get the device."""
        self._bump_off_epoch()
        self.last_off_report = {}
        try:
            with safe_direction():
                with held(self._lock, f"{self.device_id}: device lock"):
                    self._emission_raw(False)
                return self.emission_state()
        except LockTimeout as exc:
            logger.warning("%s: emission off NOT confirmed - %s", self.device_id, exc)
            self.last_off_report = {"lock_timeout": True, "error": str(exc)}
            return None
        except Exception as exc:  # noqa: BLE001 - the safe direction must not throw
            logger.warning("%s: emission off NOT confirmed - %s: %s", self.device_id, type(exc).__name__, exc)
            return None

    def set_power(self, value: Any) -> float:
        epoch = self.off_epoch
        if "power" not in self.SUPPORTS:
            raise LaserLimitError(f"{self.device_id}: power is not software-controllable")
        v = _num(value, "laser power")
        cap = self.limits.max_power
        if cap is None:
            raise LaserLimitError(f"{self.device_id}: no max_power configured — refused")
        if not 0.0 <= v <= cap:
            raise LaserLimitError(f"{self.device_id}: power {v} {self.limits.power_unit} "
                                  f"outside [0, {cap}]")
        self._require_writes("set power")
        with self._light_scope(epoch):
            return float(self._set_power_raw(v))

    def get_power(self) -> float | None:
        v = self._get_power_raw()
        return None if v is None else float(v)

    def close(self) -> None:  # pragma: no cover - drivers override
        pass

    @abstractmethod
    def _status_raw(self) -> dict: ...
    @abstractmethod
    def _emission_state_raw(self) -> bool | None: ...
    @abstractmethod
    def _emission_raw(self, on: bool) -> bool: ...

    def _set_power_raw(self, value: float) -> float:  # pragma: no cover
        raise LaserLimitError(f"{self.device_id}: power control not implemented")

    def _get_power_raw(self) -> float | None:
        return None


class TunableLaser(Laser):
    """A source with selectable wavelength channels (e.g. supercontinuum + AOTF)."""

    SUPPORTS = frozenset({"emission", "wavelength", "amplitude"})

    def channels(self) -> list[dict]:
        """``[{channel, wavelength_nm, amplitude_pct, enabled}]``."""
        return [dict(c) for c in self._channels_raw()]

    def _check_nm(self, nm: Any) -> float:
        v = _num(nm, "wavelength")
        lo, hi = self.limits.min_nm, self.limits.max_nm
        if lo is None or hi is None:
            raise LaserLimitError(f"{self.device_id}: no wavelength range configured — refused")
        if not lo <= v <= hi:
            raise LaserLimitError(f"{self.device_id}: wavelength {v} nm outside [{lo}, {hi}] nm")
        return v

    def _check_channel(self, channel: Any) -> int:
        ch = int(_num(channel, "channel"))
        n = len(self._channels_raw())
        if not 0 <= ch < n:
            raise LaserLimitError(f"{self.device_id}: channel {ch} outside [0, {n - 1}]")
        return ch

    def set_wavelength_nm(self, nm: Any, *, channel: Any = 0) -> float:
        v, ch = self._check_nm(nm), self._check_channel(channel)
        self._require_writes("set wavelength")
        with self._lock:
            return float(self._set_wavelength_raw(ch, v))

    def get_wavelength_nm(self, *, channel: Any = 0) -> float | None:
        ch = self._check_channel(channel)
        for c in self.channels():
            if int(c.get("channel", -1)) == ch:
                w = c.get("wavelength_nm")
                return None if w is None else float(w)
        return None

    def set_amplitude_pct(self, pct: Any, *, channel: Any = 0) -> float:
        epoch = self.off_epoch
        v, ch = _num(pct, "amplitude"), self._check_channel(channel)
        cap = self.limits.max_amplitude_pct
        if cap is None:
            raise LaserLimitError(f"{self.device_id}: no max_amplitude_pct configured — refused")
        if not 0.0 <= v <= cap:
            raise LaserLimitError(f"{self.device_id}: amplitude {v}% outside [0, {cap}]%")
        self._require_writes("set amplitude")
        with self._light_scope(epoch):
            return float(self._set_amplitude_raw(ch, v))

    @abstractmethod
    def _channels_raw(self) -> list[dict]: ...
    @abstractmethod
    def _set_wavelength_raw(self, channel: int, nm: float) -> float: ...
    @abstractmethod
    def _set_amplitude_raw(self, channel: int, pct: float) -> float: ...


class SimLaser(Laser):
    """Fixed-wavelength laser with emission and (optionally) power in %."""

    SUPPORTS = frozenset({"emission", "power"})

    def __init__(self, *, wavelength_nm: float = 1064.0, max_output_w: float = 0.2, **kw):
        kw.setdefault("limits", LaserLimits(max_power=100.0, power_unit="pct"))
        kw.setdefault("allow_emission_control", True)
        super().__init__(**kw)
        self.wavelength_nm = float(wavelength_nm)
        self.max_output_w = float(max_output_w)
        self.emitting = False
        self.power_pct = 50.0

    def output_w(self) -> float:
        """What a power meter would see (sim only)."""
        return self.max_output_w * self.power_pct / 100.0 if self.emitting else 0.0

    def _status_raw(self) -> dict:
        return {"sim": True, "emission": self.emitting, "power_pct": self.power_pct,
                "wavelength_nm": self.wavelength_nm}

    def _emission_state_raw(self) -> bool | None:
        return self.emitting

    def _emission_raw(self, on: bool) -> bool:
        self.emitting = bool(on)
        return self.emitting

    def _set_power_raw(self, value: float) -> float:
        # like a real source: while emitting, ANY raise of the power is more light on the sample, so it passes
        # the emission gates (the NKT driver does the same for its output level); lowering it never does
        if self.emitting and value > self.power_pct:
            self._check_emission_gates(f"power {value:g} % (adds light)")
        self.power_pct = float(value)
        return self.power_pct

    def _get_power_raw(self) -> float | None:
        return self.power_pct


class SimTunableLaser(TunableLaser):
    """Supercontinuum + 8-channel AOTF filter (SuperK SELECT-like) in memory."""

    def __init__(self, *, n_channels: int = 8, max_output_w: float = 0.05, **kw):
        kw.setdefault("limits", LaserLimits(min_nm=430.0, max_nm=1400.0, max_amplitude_pct=100.0))
        kw.setdefault("allow_emission_control", True)
        super().__init__(**kw)
        self.max_output_w = float(max_output_w)
        self.emitting = False
        self._ch = [{"channel": i, "wavelength_nm": 600.0 + 10 * i, "amplitude_pct": 0.0,
                     "enabled": False} for i in range(int(n_channels))]

    def output(self) -> tuple[float, float] | None:
        """(wavelength_nm, power_w) of the strongest enabled channel, sim only."""
        if not self.emitting:
            return None
        live = [c for c in self._ch if c["amplitude_pct"] > 0]
        if not live:
            return None
        c = max(live, key=lambda c: c["amplitude_pct"])
        # a mild spectral roll-off so power normalisation matters in tests
        roll = math.exp(-((c["wavelength_nm"] - 800.0) / 600.0) ** 2)
        return (float(c["wavelength_nm"]), self.max_output_w * c["amplitude_pct"] / 100.0 * roll)

    def _status_raw(self) -> dict:
        return {"sim": True, "emission": self.emitting, "channels": self.channels()}

    def _emission_state_raw(self) -> bool | None:
        return self.emitting

    def _emission_raw(self, on: bool) -> bool:
        self.emitting = bool(on)
        return self.emitting

    def _channels_raw(self) -> list[dict]:
        return [dict(c) for c in self._ch]

    def _set_wavelength_raw(self, channel: int, nm: float) -> float:
        self._ch[channel]["wavelength_nm"] = float(nm)
        return float(nm)

    def _set_amplitude_raw(self, channel: int, pct: float) -> float:
        # like the NKT SELECT: with the source emitting, ANY raise of a channel (opening a dark one or making a
        # lit one brighter) is more light on the sample, so it passes the emission gates; dimming one, setting
        # the same level or switching it to 0 does not
        if self.emitting and pct > float(self._ch[channel]["amplitude_pct"]):
            self._check_emission_gates(f"channel {channel} amplitude {pct:g} % (adds light)")
        self._ch[channel]["amplitude_pct"] = float(pct)
        self._ch[channel]["enabled"] = pct > 0
        return float(pct)
