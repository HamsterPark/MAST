"""Shared plumbing for the optics / TERS skills (no skills defined here).

* :class:`LiveStream` — a never-raising adapter over the LiveHub contract (§7).
  Without ``mast.live`` (an older build, a test) every call is a no-op, so
  live plotting can never be the reason an acquisition fails.
* :func:`abort_probe` — the context's abort predicate in the shape the
  instrument layer accepts (returns True to abort).
* :func:`fail` — one translation from the instrument error family to a
  failed ``SkillResult``.
* :func:`resolve_device` — "which configured device plays this role".
* :func:`stm_snapshot` / :class:`CurrentSampler` — STM state before/after an
  exposure and the tunnel current during it (contract §11.3).
* :func:`resolve_scope_dirs` / :func:`store_in_experiment` — put files into the
  active sample's folder through ``ExperimentFileStore``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import secrets
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from mast.core.types import SkillResult
from mast.i18n import tr
from mast.instruments.base import (
    InstrumentError,
    InstrumentUnavailable,
    MotionAborted,
    MotionTimeout,
    TravelLimitError,
)

logger = logging.getLogger(__name__)

__all__ = [
    "LiveStream",
    "set_live_hub_provider",
    "abort_probe",
    "fail",
    "resolve_device",
    "no_stm",
    "stm_snapshot",
    "tip_requirement_refusal",
    "stage_tip_refusal",
    "stage_tip_result",
    "CurrentSampler",
    "scope_ids",
    "resolve_scope_dirs",
    "store_in_experiment",
    "staging_dir",
    "atomic_write_bytes",
    "dump_json_bytes",
    "optics_positions",
    "json_safe",
    "new_run_id",
    "hlc_now",
    "tip_work_refusal",
]


# ── LiveHub ─────────────────────────────────────────────────────────────────

_LIVE_PROVIDER: Callable[[], Any] | None = None


def set_live_hub_provider(provider: Callable[[], Any] | None) -> None:
    """Tests inject a LiveHub double; ``None`` restores ``mast.live.get_live_hub``."""
    global _LIVE_PROVIDER
    _LIVE_PROVIDER = provider


def _live_hub() -> Any:
    if _LIVE_PROVIDER is not None:
        return _LIVE_PROVIDER()
    try:
        from mast.live import get_live_hub
    except Exception:  # noqa: BLE001 - live plotting is optional
        return None
    try:
        return get_live_hub()
    except Exception as exc:  # noqa: BLE001
        logger.debug("LiveHub unavailable: %s", exc)
        return None


def json_safe(obj: Any, _depth: int = 0) -> Any:
    """numpy / tuples / non-finite floats → plain JSON-able Python."""
    if _depth > 8:
        return None
    if obj is None or isinstance(obj, (bool, str, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): json_safe(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [json_safe(v, _depth + 1) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    try:
        import numpy as np

        if isinstance(obj, np.ndarray):
            return json_safe(obj.tolist(), _depth + 1)
        if isinstance(obj, np.generic):
            return json_safe(obj.item(), _depth + 1)
    except Exception:  # noqa: BLE001
        pass
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return json_safe(dataclasses.asdict(obj), _depth + 1)
    return str(obj)


class LiveStream:
    """One LiveHub stream. Every method swallows its own failures."""

    def __init__(self, stream_id: str, kind: str, *, title: str, source: str,
                 x_label: str | None = None, x_unit: str | None = None,
                 y_label: str | None = None, y_unit: str | None = None,
                 meta: dict | None = None, retain: int | None = None):
        self.stream_id = str(stream_id)
        self.kind = str(kind)
        self._hub = _live_hub()
        self.published = 0
        if self._hub is None:
            return
        try:
            self._hub.declare(self.stream_id, self.kind, title=title, source=source,
                              x_label=x_label, x_unit=x_unit, y_label=y_label,
                              y_unit=y_unit, meta=json_safe(meta) if meta else None,
                              retain=retain)
        except Exception as exc:  # noqa: BLE001
            logger.debug("LiveHub declare %s failed: %s", self.stream_id, exc)
            self._hub = None

    @property
    def active(self) -> bool:
        return self._hub is not None

    def set_axis(self, x: Any, *, unit: str) -> int | None:
        if self._hub is None or x is None:
            return None
        try:
            return int(self._hub.set_axis(self.stream_id, json_safe(list(x)), unit=unit))
        except Exception as exc:  # noqa: BLE001
            logger.debug("LiveHub set_axis %s failed: %s", self.stream_id, exc)
            return None

    def publish(self, frame: dict) -> None:
        if self._hub is None:
            return
        body = dict(frame)
        body.setdefault("t", time.time())
        try:
            self._hub.publish(self.stream_id, json_safe(body))
            self.published += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("LiveHub publish %s failed: %s", self.stream_id, exc)

    def end(self) -> None:
        if self._hub is None:
            return
        try:
            self._hub.end(self.stream_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("LiveHub end %s failed: %s", self.stream_id, exc)


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"


# ── abort / errors ───────────────────────────────────────────────────────────


def abort_probe(context: Any) -> Callable[[], bool] | None:
    """``context.check_abort`` as a bool predicate, or None without a context."""
    fn = getattr(context, "check_abort", None)
    if not callable(fn):
        return None

    def _probe() -> bool:
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001 - a broken probe must not abort a run
            return False

    return _probe


def fail(skill: str, exc: BaseException, **data: Any) -> SkillResult:
    """Uniform failure translation for the instrument / hub error family."""
    code = getattr(exc, "remote_code", None) or getattr(exc, "code", None)
    from mast.instruments.spectrometer import SpectrometerLimitError

    try:
        from mast.instruments.motor_groups import MotorGroupConfigError
    except Exception:  # noqa: BLE001
        MotorGroupConfigError = ()  # type: ignore[assignment]  # noqa: N806
    if isinstance(exc, MotionAborted):
        msg = f"aborted: {exc}. Stop and report; do not retry."
    elif isinstance(exc, InstrumentUnavailable):
        msg = f"optical hardware unavailable: {exc}"
    elif isinstance(exc, TravelLimitError):
        msg = f"refused by soft limit: {exc}"
    elif isinstance(exc, SpectrometerLimitError):
        msg = f"refused by spectrometer limit: {exc}"
    elif isinstance(exc, MotionTimeout):
        msg = f"timeout: {exc}"
    elif MotorGroupConfigError and isinstance(exc, MotorGroupConfigError):
        msg = f"motor-group config: {exc}"
    elif isinstance(exc, InstrumentError):
        msg = str(exc)
    else:
        msg = f"{type(exc).__name__}: {exc}"
    if code == "not_supported":
        msg += tr(" —— 卫星驱动尚不支持这个动作（例如 DS1 协议待厂家），这不是参数问题，不要重试。",
                  " - the satellite driver does not support this action yet (e.g. the DS1 protocol is waiting on the vendor); this is not a parameter problem, do not retry.")
    out = dict(data)
    if code:
        out["remote_code"] = str(code)
    return SkillResult(skill_name=skill, success=False, error=msg, data=out)


# ── device resolution ───────────────────────────────────────────────────────


def resolve_device(registry: Any, *, kind: str, role: str | None,
                   device_id: str | None) -> str:
    """The configured device id to use: the explicit one, or the single enabled
    device of *kind* carrying *role* (then the single device of *kind*).

    A microscope is also a spectrometer (``registry.spectrometer(id)`` returns the
    microscope object), so ``kind="spectrometer"`` falls back to microscopes —
    only when no spectrometer is configured at all, so a TERS station that has
    one never changes its answer."""
    if device_id:
        return str(device_id)
    hits = registry.find_devices(kind=kind, role=role) if role else []
    if not hits:
        hits = registry.find_devices(kind=kind)
    if not hits and kind == "spectrometer":
        hits = (registry.find_devices(kind="microscope", role=role) if role else []) \
            or registry.find_devices(kind="microscope")
    if len(hits) == 1:
        return str(hits[0].get("id"))
    if not hits:
        raise InstrumentUnavailable(
            f"no enabled {kind} device{f' with role {role!r}' if role else ''} in "
            "config/optical_instruments.json — configure one (see the TERS template under config/) "
            "or pass device_id")
    raise InstrumentError(
        f"{len(hits)} {kind} devices match ({[h.get('id') for h in hits]}) — pass device_id")


# ── STM state around an exposure ──────────────────────────────────────────────


def no_stm() -> bool:
    """True on an optical station (``mast.station``): there is no STM and no Nanonis, so
    nothing may try to read one. ``AcquireSpectrum`` calls :func:`stm_snapshot` twice and
    samples the tunnelling current through the whole exposure; without this each of those
    reads is a failed Nanonis round-trip that logs a warning and leaves a sidecar section
    that looks like data. Never true on an STM station."""
    try:
        from mast import station

        return station.is_optical()
    except Exception:  # noqa: BLE001 - no profile information: STM, as before
        return False


def stm_snapshot(context: Any, *, fresh: bool = True) -> dict:
    """The STM state as a JSON-able dict. ``fresh`` reads the hardware
    (``state.refresh()``) when the context offers it; otherwise the 1 Hz cache.
    Never raises: ``{"available": False, "why": ...}`` instead."""
    if no_stm():
        return {"available": False, "why": "optical station profile: there is no STM"}
    st = getattr(context, "state", None)
    snap = None
    source = ""
    if st is None:
        return {"available": False, "why": "no instrument state on this context"}
    if fresh and callable(getattr(st, "refresh", None)):
        try:
            snap, source = st.refresh(), "refresh"
        except Exception as exc:  # noqa: BLE001
            logger.debug("state refresh failed: %s", exc)
    if snap is None and callable(getattr(st, "snapshot", None)):
        try:
            snap, source = st.snapshot(), "snapshot"
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "why": f"snapshot failed: {exc}"}
    if snap is None:
        return {"available": False, "why": "state returned nothing"}
    d = json_safe(snap) if dataclasses.is_dataclass(snap) else json_safe(dict(vars(snap)))
    d = d if isinstance(d, dict) else {"value": d}
    d["available"] = True
    d["source"] = source
    from mast.instruments.spectrometer import utc_now_iso

    d["read_utc"] = utc_now_iso()
    return d


def tip_requirement_refusal(requires: str, snap: dict, *, what: str = tr("该组", "this group")) -> str:
    """Refusal text for a tip-state requirement (``none`` / ``tip_withdrawn`` /
    ``z_feedback_on``) against an :func:`stm_snapshot`, or ``""``. An unreadable
    state refuses: 读不到 ≠ 没问题."""
    if requires == "none":
        return ""
    if not snap.get("available"):
        return (tr(f"读不到 STM 状态（{snap.get('why', '?')}）—— {what}要求 {requires}，"
                "状态未知就不动（读不到 ≠ 没问题）",
                   f"cannot read STM state ({snap.get('why', '?')}) - {what} requires {requires}, "
                   "so nothing moves while the state is unknown (unreadable != fine)"))
    if snap.get("scan_running"):
        return tr("扫描进行中 —— 这时动光学件会毁掉这张图，先停扫描", "scan in progress - moving optics now would ruin this image; stop the scan first")
    if requires == "tip_withdrawn" and snap.get("withdrawn") is not True:
        return (tr(f"针尖未退（withdrawn={snap.get('withdrawn')!r}）—— {what}要求 tip_withdrawn："
                "运动有冲击振动，先 WithdrawTip 再动",
                   f"tip not withdrawn (withdrawn={snap.get('withdrawn')!r}) - {what} requires tip_withdrawn: "
                   "motion causes shock and vibration; run WithdrawTip first"))
    if requires == "z_feedback_on" and snap.get("z_controller_on") is not True:
        return (tr(f"Z 反馈未开（z_controller_on={snap.get('z_controller_on')!r}）—— "
                f"{what}要求 z_feedback_on",
                   f"Z feedback is off (z_controller_on={snap.get('z_controller_on')!r}) - "
                   f"{what} requires z_feedback_on"))
    return ""


def stage_tip_refusal(context: Any, device_ids, *, registry: Any = None) -> tuple[str, dict]:
    """The per-device tip requirement of local optical devices
    (``optical_instruments.json`` ``tip_requirement``, contract §13.1).

    ``("", {...})`` when every device may move. Devices without the field (or
    ``"none"``) need nothing and cost no STM read — the behaviour before the
    field existed. An unknown value refuses."""
    from mast.instruments.config import device_tip_requirement

    if registry is None:
        from mast.instruments.registry import get_instrument_registry

        registry = get_instrument_registry()
    lookup = getattr(registry, "device_config", None)
    needed: list[tuple[str, str]] = []
    for dev in dict.fromkeys(str(d) for d in device_ids if d):
        entry = lookup(dev) if callable(lookup) else None
        try:
            req = device_tip_requirement(entry)
        except ValueError as exc:
            return tr(f"{exc} —— 不认识的针尖要求，拒绝运动", f"{exc} - unrecognised tip requirement, motion refused"), {"device_id": dev}
        if req != "none":
            needed.append((dev, req))
    if not needed:
        return "", {}
    snap = stm_snapshot(context, fresh=True)
    info = {"requirements": dict(needed),
            "stm": {k: snap.get(k) for k in ("available", "withdrawn", "z_controller_on",
                                             "scan_running")}}
    for dev, req in needed:
        why = tip_requirement_refusal(req, snap, what=tr(f"设备 {dev} ", f"device {dev}"))
        if why:
            return why, info
    return "", info


def stage_tip_result(skill: str, context: Any, *device_ids: str) -> SkillResult | None:
    """:func:`stage_tip_refusal` as the failed SkillResult a moving skill returns
    (None = may move). A check that itself fails refuses."""
    try:
        why, info = stage_tip_refusal(context, device_ids)
    except Exception as exc:  # noqa: BLE001 - an unanswerable check does not pass
        why, info = tr(f"针尖要求查不了（{type(exc).__name__}: {exc}）—— 拒绝运动",
                       f"cannot check the tip requirement ({type(exc).__name__}: {exc}) - motion refused"), {}
    if not why:
        return None
    return SkillResult(skill_name=skill, success=False, error=why,
                       data={"tip_requirement": info})


class CurrentSampler:
    """Polls ``Current_Get`` at most every ``interval_s`` while an exposure runs.

    ``tick()`` is cheap to call often (the hub calls the wait hook every
    ~0.1 s); a failed read is counted, never raised."""

    def __init__(self, context: Any, *, interval_s: float = 0.25, max_samples: int = 20000):
        self._ctx = context
        self.interval_s = max(0.02, float(interval_s))
        self._next = 0.0
        self.values: list[float] = []
        self.errors = 0
        self._max = int(max_samples)
        # an optical station has no tunnelling current to sample (see no_stm)
        self.enabled = callable(getattr(context, "safe_call", None)) and not no_stm()

    def tick(self) -> None:
        if not self.enabled or len(self.values) >= self._max:
            return
        now = time.monotonic()
        if now < self._next:
            return
        self._next = now + self.interval_s
        try:
            from mast.skills.builtins.optics_acquire import nanonis_scalar

            rec = self._ctx.safe_call("Current_Get")
            if rec.error:
                self.errors += 1
                return
            self.values.append(nanonis_scalar(rec))
        except Exception:  # noqa: BLE001
            self.errors += 1

    def stats(self) -> dict:
        v = self.values
        if not v:
            return {"n": 0, "errors": self.errors, "interval_s": self.interval_s}
        mean = sum(v) / len(v)
        std = (sum((x - mean) ** 2 for x in v) / (len(v) - 1)) ** 0.5 if len(v) > 1 else 0.0
        return {"n": len(v), "mean_a": mean, "std_a": std, "min_a": min(v), "max_a": max(v),
                "errors": self.errors, "interval_s": self.interval_s}


#: The approach skills themselves. Named exactly: the token's holder is the
#: OUTERMOST skill, so a substring test would also refuse a TERS composite that
#: merely has "Approach" in its name while it runs its spectrum sub-step.
APPROACH_SKILLS = frozenset({"AutoApproach", "ApproachTip"})


def tip_work_refusal() -> str:
    """Contract §10.2: no spectrum while tip work / an approach owns the instrument.

    Called after the caller's own token is taken, so the holder seen here is the
    outermost skill: AcquireSpectrum itself at top level (another entry point's
    tip work is already kept out by the token), or the composite it runs in."""
    try:
        from mast.core.instrument_lock import instrument_lock
        from mast.core.tip_intent import is_tip_work

        holder = instrument_lock().snapshot() or {}
        skill = str(holder.get("skill") or "")
    except Exception:  # noqa: BLE001 - unreadable holder: do not block
        return ""
    if skill and is_tip_work(skill):
        return (tr(f"修针进行中（{skill}）——契约 §10.2：修针期间禁止开始光谱采集。"
                "等修针结束后再采；激光本身 MAST 关不掉，请确认现场光路状态。",
                   f"tip conditioning in progress ({skill}) - contract §10.2: starting spectrum acquisition is forbidden during tip conditioning. "
                   "Acquire after conditioning finishes; MAST cannot turn the laser off, so check the optical path on site."))
    if skill in APPROACH_SKILLS:
        return tr(f"进针进行中（{skill}）——契约 §10.2：进针期间禁止开始光谱采集。",
                  f"approach in progress ({skill}) - contract §10.2: starting spectrum acquisition is forbidden during approach.")
    return ""


# ── HLC ─────────────────────────────────────────────────────────────────────

_hlc_lock = threading.Lock()
_hlc_clock: Any = None


def hlc_now() -> str:
    """An HLC stamp from this process's optics node (``OPT``); "" if unavailable."""
    global _hlc_clock
    try:
        with _hlc_lock:
            if _hlc_clock is None:
                from mast.logging.v2.hlc import HLCClock

                _hlc_clock = HLCClock(node_id="OPT")
            return _hlc_clock.now().encode()
    except Exception:  # noqa: BLE001
        return ""


# ── experiment folder ─────────────────────────────────────────────────────────


def scope_ids() -> dict:
    """``{experiment_id, sample_id}`` of the active scope (None when unset)."""
    try:
        from mast.logging.experiment_log import get_active_log

        log = get_active_log()
    except Exception:  # noqa: BLE001
        log = None
    return {"experiment_id": getattr(log, "current_experiment_id", None) if log else None,
            "sample_id": getattr(log, "current_sample_id", None) if log else None}


def resolve_scope_dirs(*, create: bool = True) -> tuple[Path, str] | None:
    """``(experiment_dir, sample_dir_name)`` of the active sample, or None.

    Mirrors the read side of ``CoreRuntime._ensure_scope_dirs`` — same name
    functions, same fallbacks — but never writes the database: when a directory
    name is not stored yet it is derived exactly as the runtime will derive and
    store it. ``tests/v2/unit/skills/builtins/test_optics_spectro.py`` pins the
    two against each other."""
    try:
        from mast.core import experiment_paths as ep
        from mast.logging.experiment_log import get_active_log

        log = get_active_log()
        st = getattr(log, "_storage", None) if log is not None else None
        if log is None or st is None:
            return None
        eid, sid = log.current_experiment_id, log.current_sample_id
        if not eid or not sid:
            return None
        exp = st.get_experiment(eid)
        smp = st.get_sample(sid)
        if not exp or not smp:
            return None
        dir_name = (exp.get("dir_name") or "").strip() or ep.experiment_dir_name(
            eid, exp.get("name") or "", str(exp.get("start_time") or ""))
        sdir = (smp.get("dir_name") or "").strip()
        if not sdir:
            idx = smp.get("sample_index") or st.sample_ordinal(sid)
            sdir = ep.sample_dir_name(int(idx), smp.get("name") or "", sid)
        exp_dir = ep.experiment_dir(dir_name, create=create)
        if create:
            ep.sample_dir(exp_dir, sdir, create=True)
        return exp_dir, sdir
    except Exception as exc:  # noqa: BLE001 - no scope is not an error
        logger.debug("scope dirs unresolved: %s", exc)
        return None


_stores: dict[str, Any] = {}
_stores_lock = threading.Lock()


def _store_for(exp_dir: Path) -> Any:
    from mast.logging.v2.filestore import ExperimentFileStore

    with _stores_lock:
        st = _stores.get(str(exp_dir))
        if st is None:
            st = _stores[str(exp_dir)] = ExperimentFileStore(exp_dir)
        return st


def staging_dir(kind: str) -> Path:
    """Where a skill writes before ingesting: ``<project_root>/artifacts/<kind>/<date>``."""
    from mast._runtime_paths import project_root

    d = project_root() / "artifacts" / kind / datetime.now().strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".part", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def store_in_experiment(paths: list[Path], *, skill: str) -> dict:
    """Ingest *paths* into the active sample's ``raw/`` via ExperimentFileStore.

    On success the staging copies are removed and the in-folder paths returned;
    without an active sample (or on failure) the staging files stay where they
    are and are returned instead — a measurement is never thrown away."""
    scope = resolve_scope_dirs()
    if scope is None:
        return {"stored": False, "reason": "no active experiment/sample",
                "paths": [str(p) for p in paths], "files": []}
    exp_dir, sdir = scope
    store = _store_for(exp_dir)
    files: list[dict] = []
    final: list[str] = []
    all_ok = True
    for p in paths:
        r = store.ingest(p, sample_dir_name=sdir, source="skill", skill=skill, settle_s=0.0)
        files.append({"origin": str(p), "disposition": r.disposition,
                      "dest": str(r.dest) if r.dest else None, "rel_path": r.rel_path,
                      "sha256": r.sha256, "size": r.size_bytes, "reason": r.reason})
        if r.ok and r.dest is not None:
            final.append(str(r.dest))
            if Path(r.dest) != Path(p):
                try:
                    Path(p).unlink()
                except OSError:
                    pass
        else:
            all_ok = False
            final.append(str(p))
    return {"stored": all_ok, "experiment_dir": str(exp_dir), "sample_dir": sdir,
            "paths": final, "files": files}


def dump_json_bytes(obj: Any) -> bytes:
    return json.dumps(json_safe(obj), ensure_ascii=False, indent=1).encode("utf-8")


def optics_positions(registry: Any) -> dict:
    """Positions of the optical axes for a spectrum's metadata. Best effort:

    * the polariser angle is read (one call) when exactly one polariser is
      configured — it is the one number every TERS spectrum needs; a polariser on
      THIS machine is read only if already open (``connected: false`` otherwise);
    * remote devices already in use report their latest ``axis_state`` (no
      network); local stages are not polled here (a serial round-trip per axis
      per spectrum is not worth it — composites record what they moved)."""
    out: dict[str, Any] = {}
    try:
        pols = registry.find_devices(kind="rotation", role="polarizer")
    except Exception:  # noqa: BLE001
        pols = []
    if len(pols) == 1:
        dev = str(pols[0].get("id"))
        try:
            from mast.instruments.config import SATELLITE_TYPES

            if str(pols[0].get("type")) in SATELLITE_TYPES:
                # the hub-side proxy: "opening" it takes nothing from this machine
                ax = next(iter(registry.controller(dev).axes().values()))
            else:
                # a local device is read only if already open — metadata must not
                # take a device back from the vendor software it was released to
                built = getattr(registry, "built_controllers", None)
                ctrl = built().get(dev) if callable(built) else None
                ax = next(iter((ctrl.open_axes() if ctrl is not None else {}).values()), None)
            out["polarizer"] = ({"device_id": dev, "angle_deg": ax.get_position()}
                                if ax is not None else {"device_id": dev, "connected": False})
        except Exception as exc:  # noqa: BLE001
            out["polarizer"] = {"device_id": dev, "error": f"{type(exc).__name__}: {exc}"}
    built = getattr(registry, "built_controllers", None)
    if callable(built):
        for dev, ctrl in built().items():
            live = getattr(ctrl, "live_status", None)
            if callable(live):
                try:
                    out.setdefault("remote", {})[dev] = json_safe(live())
                except Exception:  # noqa: BLE001
                    pass
    return out
