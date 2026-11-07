"""手动控制面板里的卫星设备（契约 §13.1）：按卫星上报的 ``caps`` 生成 ManualDevice。

* 只为 ``caps`` 里有的卫星动作生成按钮；卫星没上报的、驱动是占位的，照实标出来。
* 偏振片、Picomotor、XYZ、光谱仪设置这几类技能按 ``config/optical_instruments.json`` 里的
  设备 id 找设备（``device_id``），所以要先在那里找到「这颗卫星的这台设备」对应的登记；
  没登记的设备只给 stop / identify / 读数（这几个技能直接按卫星 id + 设备 id 发）。
* 离线、链路降级、人工接管（operator_hold）、设备报错的，``available=false`` 并写明原因；
  stop 照样列出（卫星在人工接管时也收 stop）。
* 这里给的 ``safety`` / ``bypass_lock`` / 参数细节只是初值：端点按技能注册的元数据逐字覆盖。

返回 ``(devices, notes)``，devices 是 ManualDevice 形状的 dict。
"""

from __future__ import annotations

from typing import Any

from mast.i18n import tr

# 轴读数的 LiveHub 流：就是 live_bridge 那张表，不另抄一份
from mast.satellite.live_bridge import _AXIS_STREAM

#: 卫星设备 kind → 面板 kind；不在这里的（pmt、file_watcher）不进面板（PMT 高压不放，§13.1）。
_KINDS = {"rotation_stage": "rotation_stage", "openloop_stepper": "openloop_stepper",
          "xyz_stage": "xyz_stage", "spectrometer": "spectrometer", "aotf": "aotf"}
#: 卫星设备 kind → optical_instruments.json 的 device_kind。
_REG_KIND = {"rotation_stage": "rotation", "openloop_stepper": "openloop",
             "xyz_stage": "motion", "spectrometer": "spectrometer"}
_VENDOR = {"kinesis_kdc101": "Thorlabs", "picomotor8742": "Newport",
           "motion_manipulator": ("UIRobot（Motion_Manipulator）", "UIRobot (Motion_Manipulator)"),
           "lightfield": "Teledyne Princeton Instruments",
           "ds1": "Brimrose"}
_ROLE_LABEL = {"polarizer": ("偏振片", "Polarizer"), "beam_steer": ("光束微调", "Beam steering"),
               "focus_xyz": ("聚焦 XYZ", "Focus XYZ"), "raman_ccd": ("拉曼 CCD", "Raman CCD")}
_KIND_LABEL = {"rotation_stage": ("旋转台", "Rotation stage"), "openloop_stepper": ("Picomotor", "Picomotor"),
               "xyz_stage": ("XYZ 台", "XYZ stage"), "spectrometer": ("光谱仪", "Spectrometer"),
               "aotf": ("AOTF", "AOTF")}

#: 按住点动的节拍（ms）：上一次返回之后至少隔这么久才发下一次。
JOG_REPEAT_MS = 250


def _p(name: str, **kw: Any) -> dict:
    return {"name": name, **kw}


def _action(aid: str, label: str, skill: str, *, fixed: dict | None = None,
            params: list[dict] | None = None, per_axis: bool = False, signed: str | None = None,
            safety: str = "confirm", bypass: bool = False, hold: int | None = None,
            stop_on_release: bool = False, hint: str | None = None) -> dict:
    return {"id": aid, "label": label, "skill": skill, "fixed": dict(fixed or {}),
            "params": list(params or []), "per_axis": per_axis,
            "direction": "signed" if signed else "none", "direction_param": signed,
            "safety": safety, "bypass_lock": bypass, "hold_repeat_ms": hold,
            "stop_on_release": stop_on_release, "hint": hint}


def _registry_index(registry: Any) -> dict[tuple[str, str], dict]:
    """``(satellite, device) → optical_instruments.json 的那一条``。"""
    out: dict[tuple[str, str], dict] = {}
    if registry is None:
        return out
    for e in registry.find_devices() or []:
        if isinstance(e, dict) and e.get("satellite") and e.get("device"):
            out[(str(e["satellite"]), str(e["device"]))] = e
    return out


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _axes(kind: str, d: dict, entry: dict | None, fresh: bool) -> list[dict]:
    ax_state = (d.get("axis") or {}) if fresh else {}
    latest = (d.get("latest") or {}) if fresh else {}
    info = d.get("info") or {}
    cfg_axes = [a for a in (entry or {}).get("axes") or [] if isinstance(a, dict)]
    if kind == "rotation_stage":
        a = cfg_axes[0] if cfg_axes else {}
        homed = latest.get("homed", ax_state.get("homed"))
        return [{"name": str(a.get("name") or "angle"), "unit": "deg",
                 "min": _num(a.get("min_pos", info.get("min_deg"))),
                 "max": _num(a.get("max_pos", info.get("max_deg"))),
                 "position": _num(ax_state.get("position_deg")),
                 "moving": ax_state.get("moving") if isinstance(ax_state.get("moving"), bool) else None,
                 "homed": homed if isinstance(homed, bool) else None}]
    if kind == "openloop_stepper":
        live = ax_state.get("axes") or {}
        if cfg_axes:
            rows = [(str(a.get("name")), str(a.get("channel", a.get("name"))), a) for a in cfg_axes]
        else:
            rows = [(str(n), str(n), {}) for n in info.get("axes") or []]
        out = []
        for name, chan, a in rows:
            st = live.get(chan) or {}
            out.append({"name": name, "unit": "steps",
                        "min": _num(a.get("min_pos")), "max": _num(a.get("max_pos")),
                        "position": _num(st.get("steps")),
                        "moving": st.get("moving") if isinstance(st.get("moving"), bool) else None,
                        "extra": {"channel": chan, "feedback": False,
                                  "max_steps_per_cmd": (entry or {}).get(
                                      "max_steps_per_cmd", info.get("max_steps_per_cmd"))}})
        return out
    if kind == "xyz_stage":
        limits = (d.get("hub_config") or {}).get("limits_mm") or info.get("limits_mm") or {}
        by_name = {str(a.get("channel", a.get("name"))): a for a in cfg_axes}
        out = []
        for key in ("x", "y", "z"):
            a = by_name.get(key, {})
            lo_hi = limits.get(key) if isinstance(limits.get(key), list) else [None, None]
            out.append({"name": str(a.get("name") or key), "unit": "mm",
                        "min": _num(a.get("min_pos", lo_hi[0])), "max": _num(a.get("max_pos", lo_hi[1])),
                        "position": _num(ax_state.get(f"{key}_mm")),
                        "moving": ax_state.get("moving") if isinstance(ax_state.get("moving"), bool) else None})
        return out
    return []


def _availability(sat_id: str, link: str, d: dict, kind: str, entry: dict | None,
                  caps: set[str]) -> tuple[bool, str | None, str | None]:
    """``(available, unavailable_reason, occupied_by)``。"""
    if link != "connected":
        why = (tr("离线", "offline") if link == "lost"
               else tr(f"链路 {link}，新命令会被拒", f"link {link}, new commands will be rejected"))
        return (False, tr(f"卫星 {sat_id} {why}（stop 照样可以按，卫星断线时靠自己的死人开关停）",
                          f"Satellite {sat_id} {why} (stop still works; a disconnected satellite "
                          f"stops itself with its own dead-man switch)"), None)
    state = str(d.get("state") or "")
    if d.get("operator_hold") or state == "operator_hold":
        return (False, tr("人工接管中：卫星上有人在操作这台设备，只放行 stop 和读数",
                          "Manual takeover: someone on the satellite is operating this device; "
                          "only stop and readouts are allowed"),
                tr("卫星上的人工操作（operator_hold）", "Manual operation on the satellite (operator_hold)"))
    if state in ("offline", "not_started", "error", "disabled"):
        why = (d.get("info") or {}).get("why") or (d.get("latest") or {}).get("error") or ""
        return (False, tr(f"设备状态 {state}", f"Device state {state}")
                + (tr(f"：{why}", f": {why}") if why else ""), None)
    if kind == "xyz_stage" and not caps & {"move_abs", "move_rel"}:
        if str(d.get("driver") or "") == "motion_manipulator":
            return False, tr("等厂家协议（Motion_Manipulator）",
                             "Waiting for the vendor protocol (Motion_Manipulator)"), None
        return False, tr("卫星驱动不支持移动（caps 里没有 move_abs / move_rel）",
                         "The satellite driver does not support moving (caps has neither move_abs nor move_rel)"), None
    if kind in _REG_KIND and entry is None:
        return (False, tr(f"没有登记在 config/optical_instruments.json（satellite={sat_id}, "
                          f"device={d.get('id')}）：技能找不到它，面板只能 stop 和读数",
                          f"Not registered in config/optical_instruments.json (satellite={sat_id}, "
                          f"device={d.get('id')}): skills cannot find it, so the panel offers only stop "
                          f"and readouts"), None)
    if entry is not None:
        try:
            _tip_requirement(entry)
        except ValueError as exc:
            return False, tr(f"{exc} —— 运动技能会一律拒绝，先改 optical_instruments.json",
                             f"{exc} -- motion skills will all be refused; fix optical_instruments.json first"), None
    return True, None, None


def _tip_requirement(entry: dict | None) -> str | None:
    """面板显示的针尖要求 = 运动技能实际判的那一个：optical_instruments.json 那一条的
    ``tip_requirement``（没写就是 none）。没登记的设备没有运动按钮，返回 None。"""
    if entry is None:
        return None
    from mast.instruments.config import device_tip_requirement

    return device_tip_requirement(entry)


def _actions(sat_id: str, dev_id: str, kind: str, entry: dict | None, caps: set[str],
             info: dict) -> list[dict]:
    target = {"satellite": sat_id, "device": dev_id}
    reg = {"device_id": str(entry["id"])} if entry else None
    acts: list[dict] = []
    if kind == "rotation_stage" and reg:
        if "move_rel" in caps:
            acts.append(_action("jog", tr("点动", "Jog"), "SetPolarizerAngle", fixed={**reg, "relative": True},
                                params=[_p("angle_deg", default=1.0)], signed="angle_deg",
                                hold=JOG_REPEAT_MS, stop_on_release=True,
                                hint=tr("每次相对转 angle_deg 度；按住连续转，松开补发 stop",
                                        "Rotates by angle_deg degrees each time; hold to keep rotating, "
                                        "a stop is sent on release")))
            acts.append(_action("move_rel", tr("相对转动", "Rotate relative"), "SetPolarizerAngle",
                                fixed={**reg, "relative": True}, params=[_p("angle_deg")],
                                hint=tr("从当前角度转过 angle_deg 度",
                                        "Rotate by angle_deg degrees from the current angle")))
        if "move_abs" in caps:
            acts.append(_action("move_abs", tr("转到角度", "Rotate to angle"), "SetPolarizerAngle",
                                fixed={**reg, "relative": False}, params=[_p("angle_deg")],
                                hint=tr("转到绝对角度；没回零时拒绝",
                                        "Rotate to an absolute angle; refused if not homed")))
        if "home" in caps:
            acts.append(_action("home", tr("回零", "Home"), "HomePolarizer", fixed=reg,
                                hint=tr("回零：会转过参考位（最长约 36 s），之后角度才有意义",
                                        "Homing: passes through the reference position (up to about 36 s); "
                                        "angles only mean something afterwards")))
        if "set_velocity" in caps:
            acts.append(_action("set_velocity", tr("速度", "Speed"), "SetPolarizerVelocity", fixed=reg,
                                params=[_p("deg_s"), _p("accel_deg_s2")],
                                hint=tr("设最大速度与加速度，下一次转动起生效；台子不动",
                                        "Sets the maximum velocity and acceleration, effective from the "
                                        "next rotation; the stage does not move")))
    if kind == "openloop_stepper" and reg:
        if "jog" in caps:
            acts.append(_action("jog", tr("点动", "Jog"), "PicomotorJog", fixed=reg,
                                params=[_p("steps", default=10)], per_axis=True, signed="steps",
                                hold=JOG_REPEAT_MS, stop_on_release=True,
                                hint=tr("走 steps 步（开环：计数是命令过的步数，不是位置反馈）",
                                        "Moves by steps steps (open loop: the count is the number of "
                                        "steps commanded, not position feedback)")))
            # PicomotorMoveAbs 走设备层的开环轴（目标与卫星此刻计数之差），用的也是卫星的 jog
            acts.append(_action("move_abs", tr("走到计数", "Go to count"), "PicomotorMoveAbs", fixed=reg,
                                params=[_p("steps")], per_axis=True,
                                hint=tr("走到累计计数（相对上次清零），差值按卫星此刻的计数算",
                                        "Goes to the cumulative count (relative to the last zeroing); the "
                                        "difference is computed from the satellite's current count")))
        if "set_velocity" in caps:
            acts.append(_action("set_velocity", tr("步速", "Step rate"), "SetPicomotorVelocity", fixed=reg,
                                params=[_p("steps_s")], per_axis=True,
                                hint=tr("设这一轴的步速，下一次走步起生效；电机不动",
                                        "Sets this axis's step rate, effective from the next move; "
                                        "the motor does not move")))
        if "set_accel" in caps:
            acts.append(_action("set_accel", tr("加速度", "Acceleration"), "SetPicomotorAccel", fixed=reg,
                                params=[_p("steps_s2")], per_axis=True,
                                hint=tr("设这一轴的加速度，下一次走步起生效；电机不动",
                                        "Sets this axis's acceleration, effective from the next move; "
                                        "the motor does not move")))
        if "set_zero" in caps:
            acts.append(_action("set_zero", tr("计数清零", "Zero count"), "PicomotorSetZero", fixed=reg,
                                per_axis=True,
                                hint=tr("当前位置记为 0 步（不移动）",
                                        "Records the current position as 0 steps (no movement)")))
        if "abort" in caps:
            acts.append(_action("abort", tr("立即停（不减速）", "Stop now (no deceleration)"),
                                "PicomotorAbort", fixed=reg, safety="auto", bypass=True,
                                hint=tr("立即停所有轴，不减速",
                                        "Stops all axes immediately, without deceleration")))
    if kind == "xyz_stage" and reg:
        if "move_abs" in caps:
            acts.append(_action("move_abs", tr("移到坐标", "Move to position"), "MoveXYZStage",
                                fixed={**reg, "relative": False}, params=[_p("x"), _p("y"), _p("z")],
                                hint=tr("移到坐标（毫米）；给哪个轴动哪个轴，依次 x、y、z",
                                        "Moves to the coordinates (mm); only the axes you give are "
                                        "moved, in the order x, y, z")))
        if "move_rel" in caps:
            acts.append(_action("move_rel", tr("相对移动", "Move relative"), "MoveXYZStage",
                                fixed={**reg, "relative": True}, params=[_p("x"), _p("y"), _p("z")],
                                hint=tr("相对当前位置移动（毫米）",
                                        "Moves relative to the current position (mm)")))
    if kind == "spectrometer" and reg:
        if "set_grating" in caps:
            gratings = [str(g) for g in info.get("gratings") or []]
            acts.append(_action("set_grating", tr("光栅", "Grating"), "SetSpectrometerGrating", fixed=reg,
                                params=[_p("grating", allowed_values=gratings or None)],
                                hint=tr("换光栅：转塔转动约 10 到 30 s，波长轴随之改变；采集中拒绝",
                                        "Changes the grating: the turret takes about 10 to 30 s to turn "
                                        "and the wavelength axis changes with it; refused during acquisition")))
        if "set_center_wavelength" in caps:
            acts.append(_action("set_center", tr("中心波长", "Center wavelength"), "SetSpectrometerCenter",
                                fixed=reg, params=[_p("center_nm")],
                                hint=tr("设中心波长（nm）；采集中拒绝",
                                        "Sets the center wavelength (nm); refused during acquisition")))
        if "set_exit_port" in caps:
            acts.append(_action("set_exit_port", tr("出口", "Exit port"), "SetSpectrometerExitPort",
                                fixed=reg, params=[_p("port")],
                                hint=tr("切换出口：front 接 CCD，side 是侧出口（PMT）",
                                        "Switches the exit port: front goes to the CCD, side is the "
                                        "side exit (PMT)")))
    if "identify" in caps:
        acts.append(_action("identify", tr("识别（闪灯）", "Identify (blink)"), "IdentifySatelliteDevice",
                            fixed=target, safety="auto",
                            hint=tr("控制器面板灯闪几下，确认是哪一台；不动轴",
                                    "Blinks the controller panel light a few times to show which unit "
                                    "this is; no axis moves")))
    if "stop" in caps:
        acts.append(_action("stop", tr("停", "Stop"), "StopSatelliteDevice", fixed=target, safety="auto",
                            bypass=True,
                            hint=tr("停这台设备（停轴、停采集）；不需要仪器令牌",
                                    "Stops this device (axes and acquisition); no instrument token needed")))
    return acts


def _readout(sat_id: str, dev_id: str, kind: str) -> dict:
    """卫星设备的读数一律走 LiveHub（不走技能执行入口）：运动类看轴流，其余看
    device_state 的数值流 ``<sat>/<dev>/state``（live_bridge 发）。"""
    return {"kind": "live", "stream": f"{sat_id}/{dev_id}/{_AXIS_STREAM.get(kind, 'state')}"}


def describe_device(sat_id: str, link: str, d: dict, entry: dict | None) -> dict | None:
    """一台卫星设备的 ManualDevice；不进面板的 kind 返回 None。"""
    kind = _KINDS.get(str(d.get("kind") or ""))
    if kind is None:
        return None
    dev_id = str(d.get("id") or "")
    caps = {str(c) for c in d.get("caps") or []}
    fresh = link == "connected" and not d.get("stale", True)
    available, why, occupied = _availability(sat_id, link, d, kind, entry, caps)
    role = (entry or {}).get("role") or d.get("role") or next(
        (a.get("role") for a in (entry or {}).get("axes") or [] if isinstance(a, dict) and a.get("role")),
        None)
    label_pair = _ROLE_LABEL.get(str(role)) or _KIND_LABEL.get(kind) or (kind, kind)
    label = f"{tr(*label_pair)} {dev_id}"
    vendor = _VENDOR.get(str(d.get("driver") or ""))
    if isinstance(vendor, tuple):
        vendor = tr(*vendor)
    try:
        tip = _tip_requirement(entry)
    except ValueError:
        tip = str((entry or {}).get("tip_requirement"))    # 显示原样；_availability 已经标了不可用
    return {
        "id": f"{sat_id}/{dev_id}", "machine": sat_id, "kind": kind, "label": label,
        "vendor": tr("模拟", "Simulated") if d.get("sim") else vendor,
        "model": d.get("model") or None, "serial": d.get("serial") or None,
        "available": available, "unavailable_reason": why, "occupied_by": occupied,
        "tip_requirement": tip,
        "axes": _axes(kind, d, entry, fresh),
        "actions": _actions(sat_id, dev_id, kind, entry, caps, d.get("info") or {}),
        "readout": _readout(sat_id, dev_id, kind),
    }


def _hub_and_index(hub: Any, registry: Any) -> tuple[Any, dict[tuple[str, str], dict]]:
    if hub is None:
        from mast.satellite.hub import get_satellite_hub

        hub = get_satellite_hub()
    if registry is None:
        try:
            from mast.instruments.registry import get_instrument_registry

            registry = get_instrument_registry()
        except Exception:  # noqa: BLE001 — 没有本机登记：卫星设备照列，只是动作少
            registry = None
    return hub, _registry_index(registry)


def satellite_readouts(ids: list[str], hub: Any = None, registry: Any = None) -> dict[str, dict]:
    """``GET /api/manual/readouts`` 里卫星设备那几项：只回枢纽缓存的最近读数，不向卫星发
    任何命令。链路不是 connected、或卫星重启后还没来新状态时位置给 null，并在 error 里说明。"""
    import time

    hub, index = _hub_and_index(hub, registry)
    views = {str(s.get("id")): s for s in hub.list_satellites() or []}
    now = time.time()
    out: dict[str, dict] = {}
    for rid in ids:
        sat_id, _, dev_id = rid.partition("/")
        sat = views.get(sat_id)
        d = next((x for x in (sat or {}).get("devices") or [] if str(x.get("id")) == dev_id), None)
        if sat is None or d is None:
            out[rid] = {"t": now, "axes": [],
                        "error": tr(f"枢纽不知道这台设备：{rid!r}", f"The hub does not know this device: {rid!r}")}
            continue
        kind = _KINDS.get(str(d.get("kind") or ""))
        link = str(sat.get("state") or "lost")
        fresh = link == "connected" and not d.get("stale", True)
        axes = _axes(kind or "", d, index.get((sat_id, dev_id)), fresh) if kind else []
        ts = d.get("latest_ts_ms")
        out[rid] = {"t": float(ts) / 1000.0 if isinstance(ts, (int, float)) else now,
                    "axes": [{k: a.get(k) for k in ("name", "position", "moving", "homed", "extra")}
                             for a in axes],
                    "error": None if fresh else tr(f"卫星 {sat_id} {link}：没有新读数",
                                                   f"Satellite {sat_id} {link}: no new readings")}
    return out


def satellite_manual_devices(hub: Any = None, registry: Any = None) -> tuple[list[dict], list[str]]:
    """所有已知卫星设备的 ManualDevice（含离线卫星最后已知的设备）与说明。"""
    hub, index = _hub_and_index(hub, registry)
    devices: list[dict] = []
    notes: list[str] = []
    for sat in hub.list_satellites() or []:
        sat_id, link = str(sat.get("id")), str(sat.get("state") or "lost")
        listed = sat.get("devices") or []
        if not listed:
            notes.append(tr(f"卫星 {sat_id}（{link}）还没报过设备：连上之后才知道有哪些",
                            f"Satellite {sat_id} ({link}) has not reported any devices yet: "
                            f"they are known only once it connects"))
            continue
        for d in listed:
            item = describe_device(sat_id, link, d, index.get((sat_id, str(d.get("id") or ""))))
            if item is not None:
                devices.append(item)
    return devices, notes


__all__ = ["satellite_manual_devices", "satellite_readouts", "describe_device", "JOG_REPEAT_MS"]
