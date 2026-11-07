"""This machine's motorised devices for the manual control panel (contract §13.1).

:func:`local_manual_devices` describes, from configuration alone, every optical
stage MAST drives itself (``optical_instruments.json``: PZTC, PI, PDXC2, the
simulators) and every Nanonis optical motor group (``nanonis_motor_groups.json``)
as a ManualDevice — the shape the panel renders, served by the hub's
``GET /api/manual/devices``.

* **Every button is a registered skill.** An action names the skill, its
  ``fixed`` arguments and the ``params`` the operator fills in; ``params`` are
  that skill's own ParameterSpec entries, copied, so names, units and ranges
  cannot drift (a test pins every action against the registry). An action whose
  skill is not registered is left out, with a note.
* **Only what the driver documents.** Home / velocity / servo appear only where
  the axis class declares them (:attr:`MotionAxis.SUPPORTS`); the label says
  what the vendor call really does (a PDXC2 "home" re-zeroes the counter and
  does not move).
* **No hardware.** No port is opened, no controller is connected, Nanonis is not
  called. Driver objects are constructed (never connected) only to read what
  their axes support. A stage's live values come from
  :func:`local_manual_readouts` (``GET /api/manual/readouts``, ``readout:
  {kind:"poll", interval_s}``): a read, not a skill execution — nothing in the
  ledger, no token — and only of a device this process already has open, so a
  panel that is merely open never grabs a device from its vendor software. An
  optical group has no position feedback: ``readout`` is ``null`` (contract
  §13.1); its step odometer rides in ``axes[].extra`` and in every jog result.
* **Occupancy is evidence or nothing.** ``occupied_by`` is filled from the port
  guard's last report (an owner its handle scan found, a client on a Nanonis
  port) or from a vendor program that is running; ``None`` means unknown, never
  free.
* **Tip requirement** is the device's own: a stage's ``tip_requirement``
  (absent = ``none``, the stage skills' behaviour before the field existed), an
  optical group's ``requires``. The panel only shows it; the skills judge it.

Satellite devices (the hub, from the satellites' ``caps``) and the alignment
camera (``mast.instruments.camera``) are described elsewhere.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any
from mast.i18n import tr

logger = logging.getLogger(__name__)

__all__ = ["local_manual_devices", "local_manual_readouts", "local_readout", "LOCAL_TYPES",
           "stage_supports"]

#: per driver type: panel kind, vendor/model text, hold-to-repeat cadence, and
#: what HomeOpticalStage really does on it (action id, label, hint) when it can.
#: ``hold_repeat_ms`` is the least gap between two bounded jogs while a button is
#: held (each one also waits for the previous to return); ``None`` = no
#: hold-to-repeat. Open-loop and piezo-inertia stages repeat sooner.
LOCAL_TYPES: dict[str, dict[str, Any]] = {
    "pztc_nm003": {
        "kind": "linear_stage", "vendor": tr("Zeptools（卓聚 PZTC）", "Zeptools (PZTC)"), "model": "PZTC nm003",
        "transport": "serial", "hold_repeat_ms": 300,
        "home": ("home", tr("寻零（会移动）", "Find zero (moves)"),
                 tr("编码器找零（enc_look_zero）：控制器带着台子去找编码器 Z 零位 —— 会移动，方向与"
                    "距离由控制器决定；计数是否清零取决于控制器的 enc_useZ（MAST 不改）",
                    "Encoder zero search (enc_look_zero): the controller drives the stage to find the encoder Z index - it moves, "
                    "direction and distance are decided by the controller; whether the count is cleared depends on the controller's enc_useZ (MAST does not change it)")),
    },
    "pi_gcs": {
        "kind": "linear_stage", "vendor": "PI", "model": "",
        "transport": "serial", "hold_repeat_ms": 300,
        "home": ("home", tr("回参考 FRF（会移动）", "Go to reference FRF (moves)"),
                 tr("FRF：台子走到参考开关，位置置为参考值 —— 会移动，可能扫过大半行程",
                    "FRF: the stage goes to the reference switch and the position is set to the reference value - it moves and may sweep most of the travel")),
    },
    "thorlabs_pdxc2": {
        "kind": "linear_stage", "vendor": "Thorlabs", "model": "PDXC2",
        "transport": "usb", "hold_repeat_ms": 200,
        "home": ("set_zero", tr("当前位置记为 0（不移动）", "Set current position as 0 (no move)"),
                 tr("PDXC2_Home：台子不动，只把当前位置记为 0 —— 软限位跟着平移",
                    "PDXC2_Home: the stage does not move; the current position is just set to 0 - the soft limits shift with it")),
    },
    "thorlabs_benchtop_piezo": {
        "kind": "linear_stage", "vendor": "Thorlabs", "model": "Benchtop Piezo",
        "transport": "usb", "hold_repeat_ms": None, "home": None,
    },
    "sim_rotation": {
        "kind": "rotation_stage", "vendor": "MAST", "model": tr("模拟旋转台", "Simulated rotation stage"),
        "transport": "sim", "hold_repeat_ms": 300,
        "home": ("home", tr("回零（会转动）", "Return to zero (rotates)"), tr("转回 0°（模拟）", "Rotate back to 0° (simulated)")),
    },
    "sim_openloop": {
        "kind": "openloop_stepper", "vendor": "MAST", "model": tr("模拟开环台", "Simulated open-loop stage"),
        "transport": "sim", "hold_repeat_ms": 150, "home": None,
    },
}

#: Nanonis optical groups: open-loop stick-slip, a short bounded jog per repeat
_GROUP_HOLD_REPEAT_MS = 150
#: pre-filled jog step of an optical group (never above the group's own cap)
_GROUP_JOG_STEPS = 10
#: pre-filled jog / wiggle step per axis unit when the device config gives no
#: ``jog_step``: 1 µm, 0.1°, 10 steps — small on every stage we drive
_JOG_DEFAULT_BY_UNIT = {"nm": 1000.0, "um": 1.0, "µm": 1.0, "μm": 1.0, "mm": 0.001,
                        "deg": 0.1, "steps": 10.0}
_STAGE_POLL_S = 1.0
_PROC_CACHE_S = 3.0


# ── skills: the one source for every action's arguments ──────────────────────


def _param_dict(p: Any) -> dict:
    """A ParameterSpec in the hub's ``ManualParamSpec`` shape (schemas_manual)."""
    allowed = getattr(p, "allowed_values", None)
    return {"name": p.name, "type": str(p.type), "unit": str(getattr(p, "unit", "") or ""),
            "required": bool(p.required), "default": p.default,
            "min_value": getattr(p, "min_value", None),
            "max_value": getattr(p, "max_value", None),
            "allowed_values": list(allowed) if allowed else None,
            "description": str(p.description or "")}


class _Skills:
    """Skill metadata by name: the live registry when there is one (admin
    overrides applied), else the builtin classes themselves."""

    def __init__(self, registry: Any = None):
        self._reg = registry if registry is not None else _live_skill_registry()
        self._cache: dict[str, Any] = {}
        self.missing: set[str] = set()
        #: actions dropped because they would send an argument the skill does not take
        self.mismatched: list[str] = []

    def meta(self, name: str) -> Any:
        if name in self._cache:
            return self._cache[name]
        meta = None
        if self._reg is not None:
            try:
                for m in self._reg.list_skills():
                    self._cache.setdefault(m.name, m)
                meta = self._cache.get(name)
            except Exception as exc:  # noqa: BLE001
                logger.debug("manual devices: skill registry unreadable: %s", exc)
        else:
            try:
                from mast.skills import builtins as _b

                cls = getattr(_b, name, None)
                meta = cls().metadata() if cls is not None else None
            except Exception as exc:  # noqa: BLE001
                logger.debug("manual devices: skill %s unreadable: %s", name, exc)
        if meta is None:
            self.missing.add(name)
        self._cache[name] = meta
        return meta


def _live_skill_registry() -> Any:
    try:
        from mast.webui import builder_api

        return getattr(builder_api, "_registry", None)
    except Exception:  # noqa: BLE001
        return None


def _bypass(meta: Any) -> bool:
    try:
        from mast.core.instrument_lock import BYPASS_NAMES, BYPASS_TAGS
    except Exception:  # noqa: BLE001
        return False
    return meta.name in BYPASS_NAMES or bool(set(meta.tags or ()) & BYPASS_TAGS)


def _jog_default(entry: dict) -> float | None:
    """Pre-filled jog step (axis native units): the device's ``jog_step`` when the
    config has one, else by unit (``_JOG_DEFAULT_BY_UNIT``); None when the axes
    disagree on the unit or it is unknown — then the operator types it."""
    raw = entry.get("jog_step")
    try:
        if raw is not None and float(raw) > 0:
            return float(raw)
    except (TypeError, ValueError):
        pass
    units = {str(a.get("unit", "um")).strip() for a in entry.get("axes", []) or []
             if isinstance(a, dict)}
    return _JOG_DEFAULT_BY_UNIT.get(units.pop()) if len(units) == 1 else None


def _action(skills: _Skills, action_id: str, label: str, skill: str, *, fixed: dict,
            params: tuple[str, ...] = (), per_axis: bool = False, direction: str = "none",
            direction_param: str | None = None, hold_repeat_ms: int | None = None,
            stop_on_release: bool = False, hint: str = "",
            defaults: dict | None = None) -> dict | None:
    meta = skills.meta(skill)
    if meta is None:
        return None
    specs = {p.name: p for p in meta.parameters}
    unknown = [k for k in (*fixed, *params, *(["axis"] if per_axis else [])) if k not in specs]
    if unknown:
        # the button would send an argument the skill does not take (a describer
        # bug, or an override that removed it): drop the button, say so
        skills.mismatched.append(tr(f"{skill} 没有参数 {unknown}（按钮 {action_id} 没列出）", f"{skill} has no parameter {unknown} (button {action_id} not listed)"))
        return None
    plist = [_param_dict(specs[n]) for n in params]
    for p in plist:            # a pre-filled value; the skill's limits still decide
        if defaults and defaults.get(p["name"]) is not None:
            p["default"] = defaults[p["name"]]
    return {
        "id": action_id, "label": label, "skill": skill, "fixed": dict(fixed),
        "params": plist,
        "per_axis": per_axis, "axis_param": "axis", "direction": direction,
        "direction_param": direction_param,
        "safety": getattr(meta.safety_level, "value", str(meta.safety_level)),
        "bypass_lock": _bypass(meta),
        "hold_repeat_ms": hold_repeat_ms, "stop_on_release": stop_on_release,
        "hint": hint or None,           # schemas_manual: Optional[str], None = no hint
    }


# ── what a stage's driver supports (constructed, never connected) ────────────


def stage_supports(entry: dict) -> frozenset[str]:
    """``MotionAxis.SUPPORTS`` of the device's axes. Builds the driver object
    without connecting it (no I/O); raises if the entry cannot build one."""
    from mast.instruments.registry import _build_controller

    ctrl = _build_controller(entry)
    axes = ctrl._build_axes()
    if not axes:
        return frozenset()
    out: frozenset[str] | None = None
    for ax in axes.values():
        s = frozenset(getattr(ax, "SUPPORTS", frozenset()))
        out = s if out is None else (out & s)
    return out or frozenset()


# ── occupancy evidence ───────────────────────────────────────────────────────

_proc_lock = threading.Lock()
_proc_cache: tuple[float, list[dict]] | None = None


def _running_processes() -> list[dict]:
    """``[{pid, name}]`` of running processes, cached a few seconds (names only)."""
    global _proc_cache
    now = time.monotonic()
    with _proc_lock:
        if _proc_cache is not None and now - _proc_cache[0] < _PROC_CACHE_S:
            return _proc_cache[1]
    try:
        from mast.core.port_guard import _default_processes

        procs = _default_processes()
    except Exception:  # noqa: BLE001
        procs = []
    with _proc_lock:
        _proc_cache = (now, procs)
    return procs


class _Occupancy:
    def __init__(self, processes: list[dict] | None):
        self._procs = processes
        try:
            from mast.core import port_guard as pg

            self._report = pg.last_report()
            self._gcfg, _err = pg.load_guard_config()
            self._vendors = {str(k).lower(): str(v) for k, v in (
                self._gcfg.get("vendor_processes") or pg.DEFAULT_VENDOR_PROCESSES).items()}
            self._serial = tuple(str(x).lower() for x in (
                self._gcfg.get("serial_vendor_processes") or pg.DEFAULT_SERIAL_VENDOR_PROCESSES))
            self._usb = {k: tuple(v) for k, v in getattr(pg, "_USB_VENDORS", {}).items()}
        except Exception:  # noqa: BLE001
            self._report, self._gcfg, self._vendors = None, {}, {}
            self._serial, self._usb = ("labview.exe", "pimikromove.exe"), {}

    def _processes(self) -> list[dict]:
        if self._procs is None:
            self._procs = _running_processes()
        return self._procs

    def _who(self, procs: list[dict]) -> str:
        return "、".join(f"{self._vendors.get(str(p.get('name') or '').lower(), p.get('name'))}"
                        f"（pid {p.get('pid')}）" for p in procs[:3])

    def stage(self, entry: dict, transport: str) -> str | None:
        dev_id = str(entry.get("id") or "")
        port = str(entry.get("port") or "").strip()
        rep = self._report
        if rep is not None and transport == "serial":
            for e in getattr(rep, "optics", None) or []:
                if e.get("id") == dev_id and e.get("owners"):
                    names = {f.pid: f.process for f in (rep.conflicts + rep.infos + rep.warnings)
                             if f.pid is not None}
                    who = tr("、", ", ").join(tr(f"{names.get(int(p)) or '某进程'}（pid {p}）", f"{names.get(int(p)) or 'a process'} (pid {p})")
                                              for p in e["owners"][:3])
                    return tr(f"{port} 正被 {who}打开着（port_guard 句柄扫描）", f"{port} is open in {who} (port_guard handle scan)")
        if transport == "serial":
            cands = self._serial
        elif transport == "usb":
            cands = tuple(str(x).lower() for x in self._usb.get(str(entry.get("type")), ()))
        else:
            return None
        running = [p for p in self._processes() if str(p.get("name") or "").lower() in cands]
        if not running:
            return None
        what = port or tr("这台 USB 设备", "this USB device")
        return tr(f"可能被占着：{self._who(running)}在运行（查不了确切归属，{what}）", f"Possibly in use: {self._who(running)} running (cannot determine exact ownership, {what})")

    def nanonis(self) -> str | None:
        rep = self._report
        if rep is None:
            return None
        hits = [f for f in rep.conflicts if str(f.kind).startswith("nanonis")]
        if not hits:
            return None
        f = hits[0]
        who = f.process or (tr("远端客户端", "remote client") if f.remote else tr("未知进程", "unknown process"))
        if f.pid is not None:
            who += f"（pid {f.pid}）"
        if f.remote:
            who += tr(f"（来自 {f.remote}）", f" (from {f.remote})")
        return tr(f"Nanonis {f.resource} 被 {who}占用（port_guard）", f"Nanonis {f.resource} is in use by {who} (port_guard)")


# ── devices ─────────────────────────────────────────────────────────────────


def _stage_device(entry: dict, skills: _Skills, occ: _Occupancy, connected: bool) -> dict:
    from mast.instruments.config import device_tip_requirement

    dev_type = str(entry.get("type") or "")
    info = LOCAL_TYPES[dev_type]
    dev_id = str(entry.get("id") or "")
    enabled = bool(entry.get("enabled", True))
    problems: list[str] = []
    try:
        supports = stage_supports(entry)
    except Exception as exc:  # noqa: BLE001 - a broken entry is shown, not hidden
        supports = frozenset()
        problems.append(tr(f"配置有误：{type(exc).__name__}: {exc}", f"Bad configuration: {type(exc).__name__}: {exc}"))
    try:
        tip: str | None = device_tip_requirement(entry)
    except ValueError as exc:
        tip = None
        problems.append(str(exc))
    available = enabled and not problems
    reason = None if available else (tr("未启用", "Disabled") if not enabled else tr("；", "; ").join(problems))

    axes = []
    for a in entry.get("axes", []) or []:
        if not isinstance(a, dict):
            continue
        axes.append({"name": str(a.get("name")), "unit": str(a.get("unit", "um")),
                     "min": a.get("min_pos"), "max": a.get("max_pos"),
                     "position": None, "moving": None, "homed": None,
                     "extra": {"role": a.get("role"), "max_velocity": a.get("max_velocity"),
                               "connected": connected, "max_step": None}})
    hold = info["hold_repeat_ms"]
    jog = _jog_default(entry)
    fixed = {"device_id": dev_id}
    acts = [
        _action(skills, "jog", tr("点动（填步长）", "Jog (enter step size)"), "OpticalStageMove",
                fixed={**fixed, "relative": True, "wait": True}, params=("position",),
                per_axis=True, direction="signed", direction_param="position",
                hold_repeat_ms=hold, stop_on_release=True,
                hint=tr("按钮给方向，填的是步长（轴原生单位）；每次一步，受软限位约束", "The button gives the direction and the field is the step size (axis native units); one step each time, bounded by the soft limits"),
                defaults={"position": jog}),
        _action(skills, "move_abs", tr("移到（绝对位置）", "Move to (absolute position)"), "OpticalStageMove",
                fixed={**fixed, "relative": False, "wait": True}, params=("position",),
                per_axis=True, hint=tr("绝对位置（轴原生单位），超出软限位的目标直接拒绝", "Absolute position (axis native units); targets outside the soft limits are refused")),
    ]
    home = info.get("home")
    if home and "home" in supports:
        acts.append(_action(skills, home[0], home[1], "HomeOpticalStage", fixed=fixed,
                            per_axis=True, hint=home[2]))
    if "velocity" in supports:
        acts.append(_action(skills, "set_velocity", tr("设速度（单位/秒）", "Set velocity (units/s)"), "SetOpticalStageVelocity",
                            fixed=fixed, params=("velocity",), per_axis=True,
                            hint=tr("轴原生单位 / 秒；超过配置的 max_velocity 拒绝", "Axis native units / s; refused above the configured max_velocity")))
    if "servo" in supports:
        acts.append(_action(skills, "servo_on", tr("伺服开", "Servo on"), "OpticalStageServo",
                            fixed={**fixed, "on": True}, per_axis=True))
        acts.append(_action(skills, "servo_off", tr("伺服关（之后闭环移动会被拒）", "Servo off (closed-loop moves are refused afterwards)"), "OpticalStageServo",
                            fixed={**fixed, "on": False}, per_axis=True,
                            hint=tr("伺服关着时闭环移动会被控制器拒绝", "With the servo off the controller refuses closed-loop moves")))
    acts += [
        _action(skills, "wiggle", tr("抖一下自检（走一步再回来）", "Wiggle self-test (one step and back)"), "OpticalStageWiggle",
                fixed={**fixed, "return_to_start": True}, params=("delta",), per_axis=True,
                hint=tr("走一小步再走回来，报告动没动、方向与标度", "Takes a small step and returns, reporting whether it moved, the direction and the scale"),
                defaults={"delta": jog}),
        _action(skills, "connect", tr("连接并读取（打开串口 / USB）", "Connect and read (opens the serial port / USB)"), "GetOpticalStageStatus",
                fixed={**fixed, "connect": True},
                hint=tr("打开串口 / USB 并做连接初始化（不移动台子）；厂家软件占着时会失败", "Opens the serial port / USB and initialises the connection (does not move the stage); fails if the vendor software holds it")),
        _action(skills, "disconnect", tr("断开（先停，再交还厂家软件）", "Disconnect (stop first, then hand back to the vendor software)"), "DisconnectOpticalDevice",
                fixed=fixed, hint=tr("先停，再关掉连接", "Stops first, then closes the connection")),
        _action(skills, "stop", tr("停止（只停这一台）", "Stop (this stage only)"), "StopOpticalStage", fixed=fixed,
                hint=tr("只停这一台；也会中止找零", "Stops only this stage; also aborts a zero search")),
    ]
    model = info["model"] or str(entry.get("model") or "")
    return {
        "id": f"local/stage/{dev_id}", "machine": "local", "kind": info["kind"],
        "label": str(entry.get("name") or dev_id), "vendor": info["vendor"], "model": model,
        "serial": str(entry.get("serial_no") or ""),
        "available": available, "unavailable_reason": reason,
        "occupied_by": None if connected else occ.stage(entry, info["transport"]),
        "tip_requirement": tip, "axes": axes, "actions": [a for a in acts if a],
        "readout": ({"kind": "poll", "interval_s": _STAGE_POLL_S} if available else None),
    }


def _group_device(g: Any, skills: _Skills, occ: _Occupancy, odometer: dict) -> dict:
    axes = []
    for letter, logical in sorted(g.axes.items()):
        axes.append({"name": logical, "unit": "steps", "min": None, "max": None,
                     "position": None, "moving": None, "homed": None,
                     "extra": {"nanonis_axis": letter, "max_steps_per_cmd": g.max_steps_per_cmd,
                               "chunk_steps": g.chunk_steps,
                               "odometer_steps": odometer.get(f"{g.group}/{letter}", 0),
                               "position_feedback": False, "freq_hz": g.freq_hz,
                               "amp_v": g.amp_v, "drive_policy": g.drive_policy}})
    acts = [
        _action(skills, "jog", tr(f"点动（单次 ≤ {g.max_steps_per_cmd} 步）", f"Jog (at most {g.max_steps_per_cmd} steps each)"), "NanonisMotorGroupMove",
                fixed={"group": str(g.group)}, params=("direction", "steps"), per_axis=True,
                defaults={"steps": min(_GROUP_JOG_STEPS, g.max_steps_per_cmd)},
                direction="signed", direction_param="direction",
                hold_repeat_ms=_GROUP_HOLD_REPEAT_MS, stop_on_release=False,
                hint=tr(f"开环 stick-slip，没有位置反馈；超过 {g.max_steps_per_cmd} 步拒绝；"
                        f"驱动 {g.freq_hz:g} Hz / {g.amp_v:g} V（{g.drive_policy}）",
                        f"Open-loop stick-slip, no position feedback; refused above {g.max_steps_per_cmd} steps; "
                        f"drive {g.freq_hz:g} Hz / {g.amp_v:g} V ({g.drive_policy})")),
        _action(skills, "stop", tr("停止（停所有电机组，含 STM 粗动组）", "Stop (all motor groups, including the STM coarse group)"), "StopMotor", fixed={},
                hint=tr("Motor_StopMove 没有组参数：停的是此刻在动的任何一组", "Motor_StopMove has no group parameter: it stops whichever group is moving right now")),
    ]
    return {
        "id": f"local/nanonis_group/{g.group}", "machine": "local",
        "kind": "nanonis_motor_group",
        "label": tr(f"{g.name}（Nanonis 电机组 {g.group}，面板 Group {g.group + 1}）", f"{g.name} (Nanonis motor group {g.group}, panel Group {g.group + 1})"),
        "vendor": "Nanonis", "model": "Motor Control", "serial": "",
        "available": bool(g.enabled), "unavailable_reason": None if g.enabled else tr("未启用", "Disabled"),
        "occupied_by": occ.nanonis(), "tip_requirement": g.requires,
        "axes": axes, "actions": [a for a in acts if a],
        # no position feedback (Motor_PosGet unsupported): nothing to poll
        "readout": None,
    }


def local_manual_devices(*, skill_registry: Any = None, notes: list[str] | None = None,
                         instruments: Any = None, motor_groups_path: Path | None = None,
                         processes: list[dict] | None = None) -> list[dict]:
    """ManualDevice dicts for this machine's stages and Nanonis optical groups.

    All arguments are optional (the hub calls it bare): ``skill_registry`` — the
    live SkillRegistry (default: the one the web layer was given, else the
    builtin classes); ``notes`` — a list that receives one line per thing that
    could not be described; ``instruments`` / ``motor_groups_path`` /
    ``processes`` — substitutes for tests. Never touches hardware. A device whose
    entry cannot be described is listed unavailable with the reason; a button
    that cannot be described is dropped with a note."""
    out_notes = notes if notes is not None else []
    skills = _Skills(skill_registry)
    occ = _Occupancy(processes)
    devices: list[dict] = []

    if instruments is None:
        from mast.instruments.registry import get_instrument_registry

        instruments = get_instrument_registry()
    for entry in instruments.device_configs():
        if str(entry.get("type") or "") not in LOCAL_TYPES:
            continue                    # satellites (hub) and spectrometers
        connected = bool(instruments.is_connected(str(entry.get("id") or "")))
        devices.append(_stage_device(entry, skills, occ, connected))

    from mast.instruments import motor_groups as mg

    cfg = mg.load_motor_groups(motor_groups_path)
    if cfg.fatal:
        out_notes.append(tr(f"{mg.CONFIG_FILENAME} 无法使用（{cfg.fatal}）—— Nanonis 光学组不列出", f"{mg.CONFIG_FILENAME} is unusable ({cfg.fatal}) - Nanonis optical groups are not listed"))
    else:
        odo = mg.odometer()
        for g in cfg.groups:
            devices.append(_group_device(g, skills, occ, odo))
        for e in cfg.errors:
            out_notes.append(tr(f"{mg.CONFIG_FILENAME}：{e}（这一项没列出）", f"{mg.CONFIG_FILENAME}: {e} (this entry is not listed)"))
        latch = mg.drive_latch()
        if latch and cfg.groups:
            out_notes.append(
                tr(f"粗动驱动锁存中（{latch.get('reason')}）：所有粗动（STM 与光学组）都会被拒绝，"
                   "直到 Nanonis 面板上的频率 / 幅度设回去、读回核对通过",
                   f"Coarse drive latched ({latch.get('reason')}): all coarse motion (STM and optical groups) is refused "
                   "until the frequency / amplitude on the Nanonis panel are set back and the read-back check passes"))
    for name in sorted(skills.missing):
        out_notes.append(tr(f"技能 {name} 没有注册：用到它的按钮没列出", f"Skill {name} is not registered: the buttons that use it are not listed"))
    out_notes.extend(dict.fromkeys(skills.mismatched))
    return devices


# ── live readouts (GET /api/manual/readouts) ────────────────────────────────


def _stage_readout(instruments: Any, dev_id: str, now: float) -> dict:
    entry = instruments.device_config(dev_id)
    if entry is None:
        return {"t": now, "axes": [], "error": tr(f"没有这台设备：{dev_id!r}", f"No such device: {dev_id!r}")}
    if not entry.get("enabled", True):
        return {"t": now, "axes": [], "error": tr("未启用", "Disabled")}
    ctrl = instruments.built_controllers().get(dev_id)
    axes_open = ctrl.open_axes() if ctrl is not None else {}
    if not axes_open:
        # not open in this process: say so per axis, and do NOT open it to find out
        return {"t": now, "error": None, "axes": [
            {"name": str(a.get("name")), "position": None, "moving": None, "homed": None,
             "extra": {"connected": False}}
            for a in entry.get("axes", []) or [] if isinstance(a, dict)]}
    axes: list[dict] = []
    for name, ax in axes_open.items():
        try:
            d = dict(ax.status_details())
        except Exception as exc:  # noqa: BLE001 - a dead link is this card's error
            return {"t": now, "axes": axes, "error": f"{name}: {type(exc).__name__}: {exc}"}
        axes.append({"name": name, "position": d.pop("position", None),
                     "moving": d.pop("moving", None), "homed": d.pop("homed", None),
                     "extra": {**d, "connected": True}})
    return {"t": now, "axes": axes, "error": None}


def _group_readout(group: int, cfg: Any, odometer: dict, latched: bool, now: float) -> dict:
    g = next((x for x in cfg.groups if x.group == group), None)
    if g is None:
        return {"t": now, "axes": [], "error": tr(f"配置里没有光学组 {group}", f"No optical group {group} in the configuration")}
    return {"t": now, "error": None, "axes": [
        {"name": logical, "position": None, "moving": None, "homed": None,
         "extra": {"odometer_steps": odometer.get(f"{g.group}/{letter}", 0),
                   "position_feedback": False, "drive_latched": latched}}
        for letter, logical in sorted(g.axes.items())]}


def local_manual_readouts(ids, *, instruments: Any = None,
                          motor_groups_path: Path | None = None) -> dict[str, dict]:
    """``{id: {t, axes: [{name, position, moving, homed, extra}], error}}`` for this
    machine's cards (``local/stage/…``, ``local/nanonis_group/…``); other ids are
    skipped. A read, never an execution: no skill, no ledger, no instrument token.

    A stage is read only if this process already has it open — a closed one
    reports ``connected: false`` and stays closed. A Nanonis group reports the
    commanded-step odometer (there is no position feedback); Nanonis is not
    called."""
    now = time.time()
    out: dict[str, dict] = {}
    wanted = [str(i) for i in ids or ()]
    if any(i.startswith("local/stage/") for i in wanted) and instruments is None:
        from mast.instruments.registry import get_instrument_registry

        instruments = get_instrument_registry()
    cfg = odo = None
    latched = False
    for rid in wanted:
        if rid.startswith("local/stage/"):
            out[rid] = _stage_readout(instruments, rid[len("local/stage/"):], now)
        elif rid.startswith("local/nanonis_group/"):
            if cfg is None:
                from mast.instruments import motor_groups as mg

                cfg = mg.load_motor_groups(motor_groups_path)
                odo = mg.odometer()
                latched = bool(mg.drive_latch())
            if cfg.fatal:
                out[rid] = {"t": now, "axes": [], "error": tr(f"电机组配置无法使用：{cfg.fatal}", f"The motor group configuration is unusable: {cfg.fatal}")}
                continue
            try:
                group = int(rid[len("local/nanonis_group/"):])
            except ValueError:
                out[rid] = {"t": now, "axes": [], "error": tr(f"认不出的组：{rid!r}", f"Unrecognised group: {rid!r}")}
                continue
            out[rid] = _group_readout(group, cfg, odo or {}, latched, now)
    return out


def local_readout(device_id: str, **kw: Any) -> dict:
    """One card's readout, ``{t, axes, error}`` — :func:`local_manual_readouts`
    for a single id. An id that is not this machine's says so in ``error``."""
    rid = str(device_id)
    got = local_manual_readouts([rid], **kw).get(rid)
    if got is None:
        return {"t": time.time(), "axes": [],
                "error": tr(f"不是本机台子或 Nanonis 光学组：{rid!r}", f"Not a local stage or Nanonis optical group: {rid!r}")}
    return got
