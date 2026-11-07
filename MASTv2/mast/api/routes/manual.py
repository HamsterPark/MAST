"""手动控制面板的两个只读端点：

* ``GET /api/manual/devices`` —— 设备描述（轴、按钮、读数来源、能不能用及原因）；
* ``GET /api/manual/readouts?ids=a,b`` —— 批量读数。读数不走技能执行入口（每执行一次就入账一条，
  1 s 轮询就是每台每小时 3600 条）：本机台子直接读已经打开的设备，卫星设备只回枢纽缓存；不取令牌、
  不改任何东西、不入账。实时曲线照旧看 LiveHub，相机走 ``/api/camera/{id}/stats``。

这里只描述按钮，不执行任何东西：每个按钮在前端走 ``POST /api/skills/{skill}/execute``，令牌、
联锁、中止、记账全部照旧。描述来自三类提供者（:mod:`mast.manual`）；某一类读不到时那一类记进
``degraded_sources``，端点照常返回别的类。

每个动作都按技能注册表核对后才给出去 —— 技能没注册，或者 ``fixed`` / ``params`` /
``direction_param`` 里有技能没有的参数，那个动作就不给，并写进 ``notes``（不让面板上出现
按了才报错的幽灵按钮）。参数的类型、单位、范围、是否必填原样取自技能的 ParameterSpec；
``safety`` 取技能**生效的** safety_level（含管理员覆写）；``bypass_lock`` 只给免令牌的 stop 类。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import ValidationError

from mast.api.schemas_manual import (
    ManualAction,
    ManualDevice,
    ManualDevicesResponse,
    ManualParamSpec,
    ManualReadoutsResponse,
    ManualReadoutValue,
)
from mast.i18n import tr

logger = logging.getLogger(__name__)

router = APIRouter(tags=["manual"])


def _source_of(raw: dict) -> str:
    rid = str(raw.get("id") or "")
    if rid.startswith("local/camera/"):
        return "camera"
    if rid.startswith("local/"):
        return "local"
    return "satellite"


def _skill_meta(registry: Any, name: str) -> Any:
    """技能**生效的**元数据（含管理员覆写）；没注册返回 None。"""
    try:
        cls = registry.get(name)
    except KeyError:
        return None
    getter = getattr(registry, "_get_metadata", None)
    return getter(cls) if callable(getter) else cls().metadata()


def _param_from_spec(spec: Any, given: ManualParamSpec) -> ManualParamSpec:
    """名字、类型、单位、范围、必填取技能的；默认值与说明提供者给了就用提供者的，
    允许值技能没有时可以由提供者收窄（例如这台光谱仪实际装的光栅）。"""
    allowed = list(spec.allowed_values) if spec.allowed_values else given.allowed_values
    return ManualParamSpec(
        name=spec.name, type=str(spec.type), unit=str(spec.unit or ""),
        required=bool(spec.required),
        default=given.default if given.default is not None else spec.default,
        min_value=spec.min_value, max_value=spec.max_value, allowed_values=allowed,
        description=given.description or str(spec.description or ""))


def _bypasses_lock(meta: Any, name: str) -> bool:
    from mast.core.instrument_lock import BYPASS_NAMES, BYPASS_TAGS

    tags = {str(t).lower() for t in getattr(meta, "tags", None) or ()}
    return name in BYPASS_NAMES or bool(tags & BYPASS_TAGS)


def _checked_action(dev_id: str, action: ManualAction, registry: Any,
                    notes: list[str]) -> ManualAction | None:
    meta = _skill_meta(registry, action.skill)
    if meta is None:
        notes.append(tr(f"{dev_id}：动作 {action.id} 的技能 {action.skill} 没有注册，不显示",
                        f"{dev_id}: skill {action.skill} of action {action.id} is not registered, not shown"))
        return None
    specs = {p.name: p for p in meta.parameters}
    named = list(action.fixed) + [p.name for p in action.params]
    if action.direction_param:
        named.append(action.direction_param)
    if action.per_axis:
        named.append("axis")
    bad = sorted({n for n in named if n not in specs})
    if bad:
        notes.append(tr(f"{dev_id}：动作 {action.id}（{action.skill}）用了技能没有的参数 {bad}，不显示",
                        f"{dev_id}: action {action.id} ({action.skill}) uses parameters the skill does not have {bad}, not shown"))
        return None
    return action.model_copy(update={
        "params": [_param_from_spec(specs[p.name], p) for p in action.params],
        "safety": str(getattr(meta.safety_level, "value", meta.safety_level)),
        "bypass_lock": _bypasses_lock(meta, action.skill),
    })


@router.get("/manual/devices", response_model=ManualDevicesResponse)
def manual_devices(request: Request) -> ManualDevicesResponse:
    """所有电动装置与对光相机的描述：轴、按钮（各对应一个技能）、读数来源、能不能用及原因。"""
    from mast.manual import SOURCE_LABEL, collect_manual_devices

    raw_devices, degraded, notes = collect_manual_devices()
    ctx = getattr(request.app.state, "ctx", None)
    registry = getattr(ctx, "skill_registry", None) if ctx is not None else None
    if registry is None:
        notes.append(tr("技能注册表不可用：动作没有按技能核对",
                        "Skill registry unavailable: actions were not checked against skills"))
    out: list[ManualDevice] = []
    for raw in raw_devices:
        source = _source_of(raw)
        try:
            dev = ManualDevice.model_validate(raw)
        except ValidationError as exc:
            if source not in degraded:
                degraded.append(source)
            first = exc.errors()[0] if exc.errors() else {}
            notes.append(tr(f"{SOURCE_LABEL.get(source, source)}的设备 {raw.get('id')!r} 描述不合契约，"
                            f"已丢弃：{first.get('loc')} {first.get('msg')}",
                            f"{SOURCE_LABEL.get(source, source)} device {raw.get('id')!r}: description violates "
                            f"the contract, discarded: {first.get('loc')} {first.get('msg')}"))
            continue
        if registry is not None:
            actions = [a for a in (_checked_action(dev.id, a, registry, notes) for a in dev.actions)
                       if a is not None]
            dev = dev.model_copy(update={"actions": actions})
        out.append(dev)
    return ManualDevicesResponse(devices=out, degraded=bool(degraded),
                                 degraded_sources=[s for s in ("local", "camera", "satellite")
                                                   if s in degraded],
                                 notes=notes)


#: 一次最多读多少台（面板一屏的卡片数远小于它；防止一条请求把读数线程堆满）。
MAX_READOUT_IDS = 64


@router.get("/manual/readouts", response_model=ManualReadoutsResponse)
def manual_readouts(ids: str = Query("", description="逗号分隔的设备 id：local/stage/…、"
                                                    "local/nanonis_group/…、<satellite_id>/<device_id>")
                    ) -> ManualReadoutsResponse:
    """本机台子、Nanonis 光学组与卫星设备的读数。只读：不执行技能、不取令牌、不入账；没打开的
    台子不去打开，卫星设备只回枢纽缓存。相机与认不出的 id 各回一条 error；读不到时每台各回一条
    error，端点照样 200。"""
    from mast.manual import collect_readouts

    wanted = list(dict.fromkeys(i.strip() for i in ids.split(",") if i.strip()))[:MAX_READOUT_IDS]
    out: dict[str, ManualReadoutValue] = {}
    for rid, raw in collect_readouts(wanted).items():
        try:
            out[rid] = ManualReadoutValue.model_validate(raw)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            out[rid] = ManualReadoutValue(error=tr(f"读数格式不对：{first.get('loc')} {first.get('msg')}",
                                                   f"Malformed readout: {first.get('loc')} {first.get('msg')}"))
    return ManualReadoutsResponse(readouts=out)
