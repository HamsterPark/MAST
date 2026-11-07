"""健康 / 铁律 / 状态 / 简报 / 作用域 / 急停。

## 简报为什么是这个形状

内部 agent 每一轮都由中间件白拿一批上下文块（针尖、仪器档案、live 读数、操作员偏好、
续工、记忆召回），外部 agent 只拿到裸的技能返回 —— 它不知道针尖是钨还是 PtIr、不知道
这台仪器 Z 朝哪边是退针、不知道十分钟前另一个 agent 在同一块样品上做了什么。简报把
那些块原样渲染出来，再加上「在我之前谁做了什么」。

三条纪律：

* **逐段独立降级**：任何一段读不到只让那一段 ``ok:false`` 并进 ``degraded``，其余照给。
  读不到 ≠ 一个值 —— 一段缺席的针尖块不能被读成「没有针尖」。
* **零硬件 I/O**：只读缓存快照与内存状态，绝不 ``refresh()``、绝不发 TCP（不走
  ``records_export._scan_search_dirs``，它会去问 Nanonis 会话目录）。
* **有界**：每段文字截断；续工块会全量加载实验动作，缓存 10 秒。
"""

from __future__ import annotations

from mast.i18n import tr
import logging
import re
import time
from typing import Any, Callable

from fastapi import APIRouter, Query, Request

from mast import station, station_view
from mast.api import direct_exec
from mast.api.ext import guide as _guide
from mast.api.ext.common import (
    API_VERSION,
    Caller,
    ExtError,
    active_log,
    caller_of,
    cognition_of,
    ctx_of,
    memory_store_of,
    operating_mode,
    registry_of,
    runtime_of,
    scope_ids,
    storage_of,
)
from mast.api.ext.schemas import EstopBody, ScopeBody

logger = logging.getLogger(__name__)

router = APIRouter(tags=["overview"])

#: 单段文字上限 / 整份简报文字上限（字符）。
SECTION_TEXT_CAP = 2400
BRIEFING_TEXT_CAP = 12000

_LIVE_FIELDS = ("bias_v", "current_a", "setpoint_a", "z_pos_m", "x_pos_m", "y_pos_m",
                "z_controller_on", "z_controller_status", "withdrawn", "scan_running", "stale")


def _cap(text: str, n: int = SECTION_TEXT_CAP) -> str:
    t = str(text or "").strip()
    return t if len(t) <= n else t[:n] + tr("\n…(截断)", "\n...(truncated)")


# ─────────────────────────────────────────────────────────────────────
# 健康 / 铁律
# ─────────────────────────────────────────────────────────────────────

@router.get("/health")
def health(request: Request):
    """外部面自己的健康：各子系统接没接线。``missing`` 非空时相关端点会 503。"""
    ctx, rt = ctx_of(request), runtime_of(request)
    wired = {
        "runtime": rt is not None,
        "registry": registry_of(request) is not None,
        "pool": getattr(ctx, "connection_pool", None) is not None,
        "state": (getattr(ctx, "state", None) or getattr(rt, "_state", None)) is not None,
        "storage": storage_of(request) is not None,
        "cognition": cognition_of(request) is not None,
        "jobs": getattr(request.app.state, "jobs", None) is not None,
    }
    try:
        from mast.api.version import get_version

        ver = get_version()
    except Exception:  # noqa: BLE001
        ver = "unknown"
    return {"ok": True, "api_version": API_VERSION, "mast_version": ver,
            "wired": wired, "missing": [k for k, v in wired.items() if not v]}


@router.get("/guide")
def guide(lang: str = Query("en", description="zh | en")):
    """面向 agent 的铁律清单。完整指南在仓库的 docs/external/。"""
    lang = "zh" if str(lang).lower().startswith("zh") else "en"
    return {"lang": lang, "rules": _guide.rules(lang), "docs_hint": _guide.DOCS_HINT[lang]}


# ─────────────────────────────────────────────────────────────────────
# 状态
# ─────────────────────────────────────────────────────────────────────

def _live(request: Request) -> dict | None:
    ctx, rt = ctx_of(request), runtime_of(request)
    state = getattr(ctx, "state", None) or getattr(rt, "_state", None)
    snap = getattr(state, "snapshot", None)
    if not callable(snap):
        return None
    hw = snap()
    return {k: direct_exec.jsonable(getattr(hw, k, None)) for k in _LIVE_FIELDS}


def _connection(request: Request) -> dict:
    pool = getattr(ctx_of(request), "connection_pool", None)
    roles: dict[str, bool] = {}
    if pool is not None:
        for role in ("main", "monitor", "data", "emergency"):
            try:
                pool.get(role)          # 没连上就抛；不重连、不发命令
                roles[role] = True
            except Exception:  # noqa: BLE001
                roles[role] = False
    return {"wired": pool is not None, "roles": roles}


def _abort(request: Request) -> dict | None:
    fn = getattr(runtime_of(request), "emergency_latch_state", None)
    if not callable(fn):
        return None
    st = fn() or {}
    return {"set": bool(st.get("abort_set")), "emergency": bool(st.get("latched")),
            "why": str(st.get("why") or "")}


def _lock() -> dict:
    from mast.core.instrument_lock import instrument_lock

    snap = instrument_lock().snapshot()
    if not snap:
        return {"held": False}
    return {"held": True, "owner": snap.get("owner"), "skill": snap.get("skill"),
            "held_s": round(float(snap.get("held_s") or 0.0), 1)}


def _scope(request: Request) -> dict:
    eid, sid = scope_ids()
    st = storage_of(request)
    exp = st.get_experiment(eid) if (st is not None and eid) else None
    smp = st.get_sample(sid) if (st is not None and sid) else None
    rt = runtime_of(request)
    return {
        "experiment": ({"id": eid, "name": (exp or {}).get("name") or "",
                        "goal": (exp or {}).get("goal_text") or ""} if eid else None),
        "sample": ({"id": sid, "name": (smp or {}).get("name") or "",
                    "sample_type": (smp or {}).get("sample_type") or ""} if sid else None),
        "recording": {
            "v1": bool(st is not None and eid),
            "v2": bool(getattr(rt, "_v2_repos", None) is not None
                       and getattr(rt, "_v2_eid", None)),
            "note": ("" if eid else tr("没有当前实验：你的动作不会进实验记录（v1 外键拒绝空实验）。"
                                       "先用 POST /scope 选择或新建实验。",
                                       "No current experiment: your actions will not be written to the "
                                       "experiment record (the v1 foreign key rejects an empty experiment). "
                                       "Use POST /scope first to select or create an experiment.")),
        },
    }


def _is_optical() -> bool:
    """Optical station profile (``mast.station``). Never true on an STM station."""
    try:
        return station.is_optical()
    except Exception:  # noqa: BLE001 - no profile information: STM, as before
        return False


def _status(request: Request) -> tuple[dict, list[str]]:
    degraded: list[str] = []
    out: dict[str, Any] = {"mode": operating_mode()}
    probes: list[tuple[str, Callable[[], Any]]] = [("abort", lambda: _abort(request)), ("lock", _lock)]
    if _is_optical():
        # No Nanonis on an optical station: its four-port connection row and the live
        # bias / current / Z readings are not "unreadable", they do not exist.
        out["station"] = station.PROFILE_OPTICAL
    else:
        probes += [("connection", lambda: _connection(request)), ("live", lambda: _live(request))]
    probes.append(("scope", lambda: _scope(request)))
    for key, fn in probes:
        try:
            out[key] = fn()
            if out[key] is None:
                degraded.append(key)
        except Exception as exc:  # noqa: BLE001
            out[key] = None
            degraded.append(key)
            logger.debug("ext status %s failed: %s", key, exc)
    jm = getattr(request.app.state, "jobs", None)
    out["jobs"] = ({"running": jm.running_count(), "max": jm.max_concurrent}
                   if jm is not None else None)
    out["degraded"] = degraded
    return out, degraded


@router.get("/status")
def status(request: Request):
    """快速状态：运行模式、中止/急停闩、仪器锁持有者、连接、live 读数、作用域、作业数。
    只读内存与缓存快照，零硬件 I/O。"""
    out, _ = _status(request)
    return out


def _status_text(s: dict) -> str:
    optical = s.get("station") == station.PROFILE_OPTICAL
    lines = [tr(f"- 运行模式 mode: {s.get('mode')}", f"- Operating mode: {s.get('mode')}")]
    if optical:
        lines.append(tr("- 站点画像: optical（光学站，没有 STM / Nanonis）",
                        "- Station profile: optical (an optical station, no STM / Nanonis)"))
    ab = s.get("abort")
    if ab is None:
        lines.append(tr("- 中止/急停: 读不到", "- Abort / e-stop: unreadable"))
    elif ab.get("set") or ab.get("emergency"):
        lines.append(tr(f"- ⚠ 中止事件已置位（急停闩={ab.get('emergency')}）：{ab.get('why') or '未留原因'}"
                        " —— 此时一切写动作都会被拒；急停闩由操作员解。",
                        f"- ⚠ Abort event set (e-stop latch={ab.get('emergency')}): "
                        f"{ab.get('why') or 'no reason given'} - all write actions are refused now; "
                        "only the operator clears the e-stop latch."))
    else:
        lines.append(tr("- 中止/急停: 未置位", "- Abort / e-stop: not set"))
    lk = s.get("lock") or {}
    if lk.get("held"):
        lines.append(tr(f"- 仪器锁: 被 {lk.get('owner')} 持有（{lk.get('skill')}，{lk.get('held_s')} s）",
                        f"- Instrument lock: held by {lk.get('owner')} ({lk.get('skill')}, {lk.get('held_s')} s)"))
    else:
        lines.append(tr("- 仪器锁: 空闲", "- Instrument lock: free"))
    conn = s.get("connection") or {}
    roles = conn.get("roles") or {}
    if optical:
        pass                          # no Nanonis connection to report on this station
    elif not conn.get("wired"):
        lines.append(tr("- Nanonis 连接: 未接线", "- Nanonis connection: not wired"))
    else:
        marks = ", ".join(f"{k}={'✓' if v else '✗'}" for k, v in roles.items())
        lines.append(tr(f"- Nanonis 连接: {marks}", f"- Nanonis connection: {marks}"))
    lv = s.get("live") or {}
    if lv and not optical:
        lines.append(f"- live: bias={lv.get('bias_v')} V, I={lv.get('current_a')} A, setpoint={lv.get('setpoint_a')} A, "
                     f"Z={lv.get('z_pos_m')} m, "
                     + tr(f"Z控制={lv.get('z_controller_status') or lv.get('z_controller_on')}, "
                          f"扫描中={lv.get('scan_running')}, 退针={lv.get('withdrawn')}, stale={lv.get('stale')}",
                          f"Z controller={lv.get('z_controller_status') or lv.get('z_controller_on')}, "
                          f"scanning={lv.get('scan_running')}, withdrawn={lv.get('withdrawn')}, "
                          f"stale={lv.get('stale')}"))
    jb = s.get("jobs") or {}
    if jb:
        lines.append(tr(f"- 外部作业: 在跑 {jb.get('running')} / 上限 {jb.get('max')}",
                        f"- External jobs: {jb.get('running')} running / limit {jb.get('max')}"))
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# 简报
# ─────────────────────────────────────────────────────────────────────

_RESUME_CACHE: dict[str, Any] = {"key": None, "at": 0.0, "text": None}

#: 续工块是内部 agent 的上下文块（``resume_context.build_experiment_resume_block``），末尾那句
#: 「提示：…路由到相应 agent…不要从头新建实验/样品」是给 supervisor 的路由指令。外部 agent
#: 读到它会以为「不许新建实验」—— 而外部 agent 接手时激活的可能是一个无关的旧实验（例如
#: 工作只是在模拟器上试用），正确的做法恰恰是新开实验。所以简报换成下面这句；内部 agent 看到的
#: 原文不动。
_EXT_RESUME_HINT = tr(
    "提示（给外部 agent）：要接着做这个实验，就保持当前作用域，从上面停下的地方续做；"
    "你要做的事与它无关时（例如在模拟器上试用、换了样品或换了目标），先用 mast_scope"
    "（POST /scope）新开一个实验，免得把你的动作记进别人的实验。",
    "Hint (for external agents): to carry on with this experiment, keep the current scope and continue "
    "from where it stopped above; if what you are doing is unrelated to it (for example trying things on "
    "the simulator, or a different sample or goal), first open a new experiment with mast_scope "
    "(POST /scope) so your actions are not recorded into someone else's experiment.")


def _for_external_agents(text: str | None) -> str | None:
    """续工块去掉给内部 agent 的「提示：」行，换成 :data:`_EXT_RESUME_HINT`。"""
    if not text:
        return text
    lines = [ln for ln in str(text).splitlines() if not ln.lstrip().startswith("提示：")]
    return "\n".join(lines + [_EXT_RESUME_HINT])


#: 反引号里的一个标识符（「调 `get_coarse_map`」的那种写法）。
_BACKTICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


def _ext_tool_calls(text: str | None) -> str | None:
    """仪器档案 / 针尖块（与内部 agent 共用的文字）里提到的仪器 agent 工具，改写成外部 agent 能照做的
    ``mast_run("<名字>")``。此前外部面上没有它们，而这两段写着「一律调 `get_map_analysis`」
    「要登记请调 `register_tip`」；7.0.1 起开放的那些经 ``mast_run`` 可用（:mod:`mast.api.ext.agent_tools`）。
    只改开放的那些名字；技能名（如 `RelocateCoarseXY`）原样留着 —— 那本来就是 mast_run 的写法。

    操作员才能做的那几个（``OPERATOR_ONLY``：登记针尖、补记手动粗动）原名留着，紧跟一句「外部面不开放，
    请操作员做」—— 加注而不是改句子，原文怎么措辞都对得上。内部 agent 读到的原文不动。"""
    if not text:
        return text
    try:
        from mast.api.ext.agent_tools import OFFERED, OPERATOR_ONLY
    except Exception:  # noqa: BLE001 — 目录坏了就照原文给
        return text

    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name in OFFERED:
            return f'`mast_run("{name}")`'
        if name in OPERATOR_ONLY:
            return f"`{name}`" + tr("（外部面不开放：请操作员在 MAST 界面上做，用 `mast_ask_operator` 请他）",
                                  " (not offered on the external surface: ask the operator to do it in "
                                  "the MAST UI, using `mast_ask_operator`)")
        return m.group(0)

    return _BACKTICKED.sub(sub, str(text))


#: 简报列多久以内、最多几条告警（与 mast_wait 的 alert 条件同源）。
ALERT_WINDOW_S = 3600.0
ALERT_LIMIT = 8
#: 简报的作业段列几个（其余用 GET /jobs）。
JOBS_SHOWN = 6

_STORE_LEVELS = {"warn": "warning", "crit": "critical"}


def _recent_alerts(request) -> tuple[list[dict], list[str]]:
    """近 :data:`ALERT_WINDOW_S` 的告警，最新在前：``(rows, sources)``。

    与 ``mast_wait`` 的 ``alert`` 条件同源：VISTA 等待中枢记下的看门狗 / 电流监测 / conduct
    告警（本次服务运行期间），再加电流监测库里同一时间窗的告警 —— 服务重启之后中枢是空的，
    库里的还在（按 ``alert_id`` 去重）。只读内存与本地库，零硬件 I/O；监测库**只在已经存在时**
    读（``get_store_if_exists``，被动读者不建库）。两个来源都没有 ⇒ 抛（这一段降级，不是
    「没有告警」）。"""
    rows: list[dict] = []
    sources: list[str] = []
    svc = getattr(request.app.state, "vista", None)
    recent = getattr(getattr(svc, "hub", None), "recent", None)
    if callable(recent):
        sources.append("wait_hub")
        for r in recent("alert", limit=ALERT_LIMIT * 4, within_s=ALERT_WINDOW_S):
            ev = dict(r.get("event") or {})
            ev["t"] = ev.get("t") or r.get("t")
            ev["age_s"] = r.get("age_s")
            rows.append(ev)
    store = None
    try:
        from mast.monitoring.store import get_store_if_exists

        store = get_store_if_exists()
    except Exception as exc:  # noqa: BLE001
        logger.debug("ext briefing: 电流监测库取不到(%s)", exc)
    if store is not None:
        try:
            now = time.time()
            got, _total = store.alerts_query(since=now - ALERT_WINDOW_S, limit=ALERT_LIMIT * 4)
            sources.append("monitor_store")
            seen = {r.get("alert_id") for r in rows
                    if r.get("source") == "current_monitor" and r.get("alert_id")}
            for a in got:
                if a.get("id") in seen:
                    continue
                ts = float(a.get("ts") or now)
                rows.append({"level": _STORE_LEVELS.get(str(a.get("level") or ""), a.get("level")),
                             "source": "current_monitor", "code": a.get("rule") or "",
                             "message": a.get("summary_zh") or "", "alert_id": a.get("id"),
                             "segment_id": a.get("segment_id"), "t": ts,
                             "age_s": round(max(0.0, now - ts), 3)})
        except Exception as exc:  # noqa: BLE001 — 库读失败只让这一个来源缺席
            logger.debug("ext briefing: 电流监测库读失败(%s)", exc)
    if not sources:
        raise RuntimeError(tr("告警来源未接线（没有 VISTA 等待中枢，也没有电流监测库）", "Alert sources not wired (no VISTA wait hub and no current-monitor store)"))
    rows.sort(key=lambda r: float(r.get("t") or 0.0), reverse=True)
    return rows, sources


def _ago(seconds: Any) -> str:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if s < 90:
        return tr(f"{s:.0f} s 前", f"{s:.0f} s ago")
    if s < 5400:
        return tr(f"{s / 60:.0f} 分钟前", f"{s / 60:.0f} min ago")
    return tr(f"{s / 3600:.1f} 小时前", f"{s / 3600:.1f} h ago")


def _alert_line(a: dict) -> str:
    try:
        when = time.strftime("%H:%M:%S", time.localtime(float(a.get("t"))))
    except (TypeError, ValueError, OverflowError, OSError):
        when = "?"
    _msg = _cap(a.get('message') or '', 120)
    line = tr(f"{when}（{_ago(a.get('age_s'))}）[{a.get('level')}] {a.get('source')}/{a.get('code')}：{_msg}",
              f"{when} ({_ago(a.get('age_s'))}) [{a.get('level')}] {a.get('source')}/{a.get('code')}: {_msg}")
    ctx = a.get("context") if isinstance(a.get("context"), dict) else {}
    bits = []
    if "scanning" in ctx:
        bits.append(tr("扫描中", "scanning") if ctx["scanning"] else tr("不在扫描", "not scanning"))
    if ctx.get("scanning_s_ago") is not None and not ctx.get("scanning"):
        bits.append(tr(f"距上一段扫描 {ctx['scanning_s_ago']} s", f"{ctx['scanning_s_ago']} s since the last scan"))
    if ctx.get("skill"):
        bits.append(tr(f"技能 {ctx['skill']}", f"skill {ctx['skill']}"))
    elif ctx.get("last_skill"):
        bits.append(tr(f"最近的技能 {ctx['last_skill']}（{ctx.get('last_skill_s_ago')} s 前）",
                       f"last skill {ctx['last_skill']} ({ctx.get('last_skill_s_ago')} s ago)"))
    return line + (tr(f"〔{'；'.join(bits)}〕", f" [{'; '.join(bits)}]") if bits else "")


def _sec_status(request, caller):
    s, _ = _status(request)
    return {"text": _status_text(s), "data": s}


def _sec_scope(request, caller):
    sc = _scope(request)
    e, smp = sc.get("experiment"), sc.get("sample")
    lines = [tr(f"- 实验: {e['name']} (id {e['id']})", f"- Experiment: {e['name']} (id {e['id']})") if e
             else tr("- 实验: 无 —— 先 POST /scope", "- Experiment: none - POST /scope first"),
             tr(f"- 样品: {smp['name']} (id {smp['id']}, {smp.get('sample_type') or '类型未填'})",
                f"- Sample: {smp['name']} (id {smp['id']}, {smp.get('sample_type') or 'type not set'})")
             if smp else tr("- 样品: 无 —— 产数据的技能（扫描/谱学）会在门口被拒",
                            "- Sample: none - skills that produce data (scan / spectroscopy) are refused at the door")]
    if e and e.get("goal"):
        lines.append(tr(f"- 目标: {_cap(e['goal'], 300)}", f"- Goal: {_cap(e['goal'], 300)}"))
    if sc["recording"]["note"]:
        lines.append(f"- ⚠ {sc['recording']['note']}")
    return {"text": "\n".join(lines), "data": sc}


def _sec_resume(request, caller):
    from mast.agents._shared.resume_context import build_experiment_resume_block

    eid, sid = scope_ids()
    key = f"{eid}/{sid}"
    now = time.monotonic()
    if _RESUME_CACHE["key"] == key and now - _RESUME_CACHE["at"] < 10.0:
        text = _RESUME_CACHE["text"]
    else:
        text = build_experiment_resume_block(
            active_log(), storage_of(request),
            plan_store=getattr(runtime_of(request), "_plan_store", None))
        _RESUME_CACHE.update(key=key, at=now, text=text)
    return {"text": _cap(_for_external_agents(text) or tr("（没有当前实验，无可续工的上下文）", "(No current experiment; no context to resume from)"))}


def _sec_tip(request, caller):
    from mast.core.instrument_profile import get_profile
    from mast.core.tip_state import current_tip_facts, format_tip_block, get_current_tip

    text = format_tip_block(get_current_tip(), get_profile())
    return {"text": _cap(_ext_tool_calls(text) or tr("（针尖未登记 —— 修针类技能会退回通用保守参数）", "(No tip registered - tip-conditioning skills fall back to generic conservative parameters)")),
            "data": direct_exec.jsonable(current_tip_facts())}


def _sec_instrument(request, caller):
    from mast.core.instrument_profile import format_profile_block, get_profile

    return {"text": _cap(_ext_tool_calls(format_profile_block(get_profile())) or tr("（仪器档案未设置）", "(Instrument profile not set)"))}


def _sec_live(request, caller):
    from mast.agents._shared.live_state_mw import format_live_state_block

    ctx, rt = ctx_of(request), runtime_of(request)
    state = getattr(ctx, "state", None) or getattr(rt, "_state", None)
    if state is None:
        raise RuntimeError(tr("仪器状态未接线", "Instrument state not wired"))
    return {"text": _cap(format_live_state_block(state.snapshot()) or tr("（live 读数为空）", "(live readings empty)"))}


def _sec_prefs(request, caller):
    from mast.agents._shared.experiment_prefs import format_prefs_block, get_prefs

    return {"text": _cap(format_prefs_block(get_prefs()) or tr("（操作员没有设置默认参数偏好）", "(The operator has not set default-parameter preferences)"))}


def _sec_safety(request, caller):
    from mast.api.routes.safety import get_latches
    from mast.api.safety_view import in_force_safety_limits

    optical = _is_optical()
    if optical:
        # The envelope is made of STM quantities (bias, setpoint, tip height). The optical
        # station's limits live in each device's own configuration (Layer-0); only the
        # latches below mean the same thing here.
        source, keep = "optical station profile", {}
    else:
        limits, source = in_force_safety_limits(ctx_of(request))
        lim = limits.model_dump() if hasattr(limits, "model_dump") else dict(limits)
        keep = {k: v for k, v in lim.items()
                if any(s in k for s in ("bias", "setpoint", "current", "xy", "scan_size", "z_"))}
    lat = get_latches(request)
    latched = [tr(f"{s.name}（{s.why or '未留原因'}；解除：{s.release or '无'}）",
                  f"{s.name} ({s.why or 'no reason given'}; release: {s.release or 'none'})")
               for s in (lat.latches or []) if s.latched]
    unreadable = list(lat.unreadable or [])
    lines = ([] if optical else
             [tr(f"- 生效包络（来源 {source}）: ", f"- Envelope in force (source {source}): ") + ", ".join(f"{k}={v}" for k, v in sorted(keep.items()))])
    lines.append(tr("- 挂着的闩: ", "- Latches set: ") + (tr("；", "; ").join(latched) if latched else tr("无", "none")))
    if unreadable:
        lines.append(tr(f"- 读不到的闩: {unreadable}", f"- Unreadable latches: {unreadable}"))
    return {"text": _cap("\n".join(lines)),
            "data": {"limits_source": source, "limits": direct_exec.jsonable(keep),
                     "latched": latched, "unreadable": unreadable}}


def _sec_recent_actions(request, caller):
    eid, _ = scope_ids()
    st = storage_of(request)
    if not eid:
        return {"text": tr("（没有当前实验）", "(No current experiment)"), "data": []}
    if st is None or not hasattr(st, "recent_actions"):
        raise RuntimeError(tr("实验记录未接线", "Experiment record not wired"))
    rows = st.recent_actions(eid, 12)
    lines = []
    for r in rows:
        who = r.get("context") or "MAST agent"
        mark = "✓" if r.get("success") else ("✗" if r.get("success") is False else "?")
        err = f" — {r.get('error')[:120]}" if r.get("error") else ""
        lines.append(f"- {str(r.get('timestamp') or '')[:19]} [{who}] {r.get('skill_name')} {mark}{err}")
    return {"text": "\n".join(lines) or tr("（本实验还没有动作）", "(No actions in this experiment yet)"), "data": rows}


def _static_roots(request) -> list[str]:
    """数据根目录 —— **不**去问 Nanonis 会话目录（那会发 TCP）。"""
    rt = runtime_of(request)
    dirs: list[str] = []
    for d in (getattr(rt, "_scan_search_dirs", None) or ()):
        if d:
            dirs.append(str(d))
    cfg = getattr(ctx_of(request), "config", None)
    if getattr(cfg, "experiments_dir", None):
        dirs.append(str(cfg.experiments_dir))
    try:
        from mast._runtime_paths import project_root

        dirs += [str(project_root() / "working-sessions"), str(project_root() / "experiments")]
    except Exception:  # noqa: BLE001
        pass
    try:
        from mast.core.experiment_paths import experiment_root

        dirs.append(str(experiment_root()))
    except Exception:  # noqa: BLE001
        pass
    seen: set[str] = set()
    return [d for d in dirs if not (d in seen or seen.add(d))]


def _sec_recent_files(request, caller):
    from mast.webui.scan_preview import collect_scan_stats

    stats = collect_scan_stats(*_static_roots(request))[:8]
    rows = [{"path": str(s.path), "name": s.path.name, "mtime_ns": s.mtime_ns,
             "size_bytes": s.size_bytes} for s in stats]
    text = "\n".join(f"- {r['name']}  ({r['size_bytes']} B)  {r['path']}" for r in rows)
    return {"text": text or tr("（数据目录里还没有扫描/谱文件）", "(No scan / spectrum files in the data directories yet)"), "data": rows}


def _sec_alarms(request, caller):
    """环境告警 + 近期的仪器告警（看门狗 / 电流监测 / conduct，与 ``mast_wait`` 同源）。

    从前这一段只有环境：电流监测告警一条也不在简报里，重启后读简报的
    agent 根本看不到它们。两部分各自降级；都读不到这一段才算坏。"""
    rt = runtime_of(request)
    log = getattr(rt, "_env_alarm_log", None)
    env_rows = [dict(r) for r in log[-5:] if isinstance(r, dict)] if isinstance(log, list) else None
    mon = getattr(ctx_of(request), "environment_monitor", None)
    overall = "unknown"
    if mon is not None:
        try:
            overall = str(mon.overall_status() or "ok")
        except Exception:  # noqa: BLE001
            overall = "unknown"
    alerts: list[dict] = []
    sources: list[str] = []
    alerts_error = ""
    n_info = 0
    try:
        every, sources = _recent_alerts(request)
        # mast_wait 的 alert 条件默认也是 warning 起：info（监测服务的状态迁移之类）只报个数
        alerts = [a for a in every if a.get("level") in ("warning", "critical")]
        n_info = len(every) - len(alerts)
    except Exception as exc:  # noqa: BLE001
        alerts_error = f"{type(exc).__name__}: {exc}"[:200]
    if env_rows is None and alerts_error:
        raise RuntimeError(tr(f"环境监控未接线；{alerts_error}", f"Environment monitoring not wired; {alerts_error}"))
    if env_rows is None:
        lines = [tr("- 环境监控: 未接线", "- Environment monitoring: not wired")]
    else:
        lines = [tr(f"- 环境总体: {overall}", f"- Environment overall: {overall}")] + [
            f"- {r.get('t')} {r.get('sensor')}: {r.get('status')} ({r.get('value')} {r.get('unit') or ''})"
            for r in env_rows]
    minutes = int(ALERT_WINDOW_S // 60)
    shown = alerts[:ALERT_LIMIT]
    info_note = tr(f"；另有 {n_info} 条 info 级未列出", f"; {n_info} more info-level not listed") if n_info else ""
    if alerts_error:
        lines.append(tr(f"- 仪器告警: 读不到（{alerts_error}）", f"- Instrument alerts: unreadable ({alerts_error})"))
    elif not alerts:
        lines.append(tr(f"- 仪器告警: 近 {minutes} 分钟没有 warning 及以上的（看门狗 / 电流监测 / conduct，"
                        f"与 mast_wait 的 alert 条件同源{info_note}）",
                        f"- Instrument alerts: none at warning level or above in the last {minutes} min "
                        f"(watchdog / current monitor / conduct, same source as the mast_wait alert "
                        f"condition{info_note})"))
    else:
        more = (tr(f"，只列最新 {len(shown)} 条", f", only the latest {len(shown)} listed")
                if len(alerts) > len(shown) else "")
        lines.append(tr(f"- 仪器告警: 近 {minutes} 分钟 warning 及以上 {len(alerts)} 条（最新在前{more}；"
                        f"与 mast_wait 的 alert 条件同源，〔〕里是告警时仪器在做什么{info_note}）:",
                        f"- Instrument alerts: {len(alerts)} at warning level or above in the last "
                        f"{minutes} min (newest first{more}; same source as the mast_wait alert "
                        f"condition, the [...] shows what the instrument was doing{info_note}):"))
        lines += [f"  - {_alert_line(a)}" for a in shown]
    data: dict[str, Any] = {"overall": overall, "recent": direct_exec.jsonable(env_rows or []),
                            "alerts": direct_exec.jsonable(shown), "alerts_total": len(alerts),
                            "alerts_info_omitted": n_info, "alerts_window_s": ALERT_WINDOW_S,
                            "alerts_sources": sources}
    if alerts_error:
        data["alerts_error"] = alerts_error
    return {"text": "\n".join(lines), "data": data}


def _sec_notes(request, caller):
    from mast.agents._shared.cognition import _namespace_for

    store = memory_store_of(request)
    if store is None:
        raise RuntimeError(tr("记忆库未接线", "Memory store not wired"))
    eid, _ = scope_ids()
    rows = store.list(_namespace_for(eid), limit=6) if eid else []
    lines = [tr(f"- [{r.get('kind')}] {r.get('title') or r.get('path')} —— {r.get('author') or '?'}"
                f"：{_cap(' '.join(str(r.get('content') or '').split()), 160)}",
                f"- [{r.get('kind')}] {r.get('title') or r.get('path')} - {r.get('author') or '?'}"
                f": {_cap(' '.join(str(r.get('content') or '').split()), 160)}") for r in rows]
    return {"text": "\n".join(lines) or tr("（本实验还没有笔记）", "(No notes in this experiment yet)"),
            "data": [{k: r.get(k) for k in ("namespace", "path", "title", "kind", "author",
                                            "updated_at", "pinned")} for r in rows]}


def _sec_recording(request, caller):
    sc = _scope(request)
    rec = sc["recording"]
    _will, _wont = tr("会写入", "will be written"), tr("不会写入", "will not be written")
    text = (tr(f"- 这次会话的动作署名: {caller.thread_id}\n", f"- Actions of this session are signed: {caller.thread_id}\n")
            + tr(f"- v1 实验记录: {_will if rec['v1'] else _wont}；v2: {_will if rec['v2'] else _wont}",
                 f"- v1 experiment record: {_will if rec['v1'] else _wont}; v2: {_will if rec['v2'] else _wont}"))
    if rec["note"]:
        text += f"\n- ⚠ {rec['note']}"
    return {"text": text, "data": {**rec, "agent_id": caller.agent_id, "thread_id": caller.thread_id}}


def _sec_jobs(request, caller):
    from mast.api.ext.jobs import KEEP

    jm = getattr(request.app.state, "jobs", None)
    if jm is None:
        raise RuntimeError(tr("作业管理器未接线", "Job manager not wired"))
    every = jm.list(actor=caller.actor, limit=KEEP)
    rows = every[:JOBS_SHOWN]
    lines = [f"- {j.job_id} {j.skill} → {j.state}" + (tr(f"（{j.refused_by}）", f" ({j.refused_by})") if j.refused_by else "")
             for j in rows]
    if len(every) > len(rows):
        total = tr(f"至少 {len(every)}", f"at least {len(every)}") if len(every) >= KEEP else str(len(every))
        lines.append(tr(f"- （只列最近 {len(rows)} 个，共 {total} 个；mast_jobs（GET /jobs）列出全部）",
                        f"- (only the latest {len(rows)} listed, {total} in total; mast_jobs (GET /jobs) lists all)"))
    return {"text": "\n".join(lines) or tr("（你还没有提交过作业）", "(You have not submitted any jobs yet)"),
            "data": [{"job_id": j.job_id, "skill": j.skill, "state": j.state,
                      "created_at": j.created_at} for j in rows],
            "shown": len(rows), "total": len(every)}


def _sec_operator_requests(request, caller):
    from mast.wishlist import list_agent_requests

    rows = [r for r in list_agent_requests() if r.get("agent_id") == caller.agent_id][:6]
    lines = []
    for r in rows:
        # 说明与路径两个都给：操作员常常只在路径里作答（「在这个目录」）。
        ans = tr("；", "; ").join(str(x) for x in (r.get("note"), r.get("path")) if x)
        lines.append(f"- {r.get('id')} [{r.get('status')}] {_cap(r.get('message') or '', 120)}"
                     + (tr(f" → 答复：{_cap(ans, 200)}", f" -> reply: {_cap(ans, 200)}") if ans else ""))
    return {"text": "\n".join(lines) or tr("（你没有向操作员发过请求）", "(You have not sent any requests to the operator)"),
            "data": direct_exec.jsonable(rows)}


#: 段文字为空时整段不进顶层 ``text``（不占篇幅）；``sections`` 里仍有它（``ok: true``、``text: ""``）。
#: 目前只有光路图：用户没画过图就没有可说的。
_OMIT_WHEN_EMPTY = frozenset({"optical_layouts"})

#: 简报里最多列几张光路图
_LAYOUTS_SHOWN = 8


def _sec_optical_layouts(request, caller):
    """用户画过的光路图（两种站点画像都有）：几张、各自器件数、未解决评论数。**没有图时为空**。"""
    from mast.optical_layout import store as layout_store

    rows = layout_store.get_store().list_layouts()
    lines = []
    for r in rows[:_LAYOUTS_SHOWN]:
        if r.get("error"):
            lines.append(tr(f"- {r['id']}：文件读不了（{_cap(r['error'], 160)}）",
                            f"- {r['id']}: the file cannot be read ({_cap(r['error'], 160)})"))
            continue
        title = f" \"{r['title']}\"" if r.get("title") else ""
        lines.append(tr(f"- {r['id']}{title}：{r['components']} 个器件，未解决评论 {r['open_comments']} 条"
                        f"（第 {r['revision']} 版，{r['updated_by']} {str(r['updated_at'])[:16].replace('T', ' ')}）",
                        f"- {r['id']}{title}: {r['components']} components, {r['open_comments']} open comment(s) "
                        f"(revision {r['revision']}, {r['updated_by']} {str(r['updated_at'])[:16].replace('T', ' ')})"))
    if rows:
        if len(rows) > len(lines):
            lines.append(tr(f"- …另有 {len(rows) - len(lines)} 张", f"- ... {len(rows) - len(lines)} more"))
        lines.append(tr("读法：mast_optical_layout(layout_id=…)（沿光束的叙述、问题清单、未解决评论；image=true 附图）；"
                        "你不能改图，只能用 mast_optical_layout_comment 评论。",
                        "Read one with mast_optical_layout(layout_id=...) (the beam paths in order, the problems, "
                        "the open comments; image=true adds a picture). You cannot change a layout; comment with "
                        "mast_optical_layout_comment."))
    return {"text": "\n".join(lines), "data": {"count": len(rows), "layouts": rows[:30]}}


# ─────────────────────────────────────────────────────────────────────
# 光学站画像的段落（mast.station；设计 optical_station_witec.md）
# ─────────────────────────────────────────────────────────────────────
#
# 光学站上 tip / instrument / live / prefs / recent_files 这几段不是「读不到」，是不存在：
# 没有针尖、没有 Nanonis 读数、没有 STM 的扫描默认值、没有扫描文件。换成下面这几段。
# 同样三条纪律：逐段独立降级；不为简报去打开任何设备（只读配置，和进程里已经打开的
# 设备 —— station_view.inventory）；有界。

#: 光学站简报的默认段序（只含存在的段）。STM 的段序见 SECTIONS（顺序即其键序，原样不动）。
OPTICAL_ORDER = ("station", "status", "scope", "resume", "optical_state", "operator_prompts",
                 "safety", "recent_actions", "recent_spectra", "alarms", "notes", "recording",
                 "jobs", "operator_requests", "optical_layouts")

_OPTICAL_RULES = (
    tr("规则（光学站）：",
       "Rules (optical station):"),
    tr("  1. 用 mast_run 跑光学技能（mast_find_skills 按动作找、mast_skill_card 读卡）；"
       "mast_act 的 STM 动作词表在这里不存在。",
       "  1. Run optical skills with mast_run (find them with mast_find_skills, read the card with "
       "mast_skill_card); mast_act's STM action vocabulary does not exist here."),
    tr("  2. WITec 的写权限要人交出：操作员在 WITec Control 里点「COM Automation」，技能才写得进去；"
       "没有写权限时技能会明确失败，不要重试刷屏 —— 用 mast_ask_operator 请他点。",
       "  2. WITec write access is handed over by a person: the operator presses \"COM Automation\" in "
       "WITec Control before any skill can write. Without it skills fail plainly - do not retry in a "
       "loop; ask with mast_ask_operator."),
    tr("  3. 需要人动手的步骤（换 λ/2、转检偏、换物镜、关厂商软件）走 mast_ask_operator，"
       "再 mast_wait 等答复；你不能替操作员作答，也不能替他签署。",
       "  3. Steps that need a person's hands (change a λ/2 plate, turn the analyzer, change the "
       "objective, close vendor software) go through mast_ask_operator, then mast_wait for the "
       "reply. You cannot answer or attest for the operator."),
    tr("  4. 开出光（LaserEmissionOn）需要设备配置允许，并且操作员已签署 laser_area_safe（8 小时有效）；"
       "关出光（LaserEmissionOff）永远允许。",
       "  4. Switching laser emission on (LaserEmissionOn) needs the device configuration to allow it "
       "and the operator's current laser_area_safe attestation (valid 8 h); switching it off "
       "(LaserEmissionOff) is always allowed."),
    tr("  5. 手动光学状态（装的是哪片 λ/2、物镜、检偏角……）只有操作员能写，会自动记进每条谱的 "
       "sidecar；用 ReadOpticalState 读。",
       "  5. The manual optical state (which λ/2 plate is in, the objective, the analyzer angle ...) "
       "can only be written by the operator and is recorded in every spectrum's sidecar; read it with "
       "ReadOpticalState."),
    tr("  6. 串口是独占的：被厂商软件占着的口打不开时，请操作员关掉对应软件。",
       "  6. Serial ports are exclusive: when a port is held by vendor software, ask the operator to "
       "close that software."),
)


def _status_brief(status: Any) -> str:
    """A device status as one readable line: its scalar fields as ``key=value``, nested
    lists / dicts as their size (the full status is in the section's ``data``). A status
    cut in the middle of a JSON object is worse than a short one."""
    if not isinstance(status, dict):
        return _cap(str(status), 200)
    bits = []
    for k, v in status.items():
        if v is None or isinstance(v, (str, int, float, bool)):
            bits.append(f"{k}={v}")
        elif isinstance(v, (list, tuple)):
            bits.append(f"{k}=[{len(v)} items]")
        elif isinstance(v, dict):
            bits.append(f"{k}={{{', '.join(f'{a}={b}' for a, b in v.items() if not isinstance(b, (dict, list, tuple)))}}}"
                        if len(v) <= 4 else f"{k}={{{len(v)} fields}}")
    return _cap(", ".join(bits), 400)


def _device_line(d: dict) -> str:
    bits = [f"{d.get('id')}"]
    if d.get("name") and d.get("name") != d.get("id"):
        bits.append(f"({d.get('name')})")
    bits.append(f"[{d.get('kind')}/{d.get('type')}]")
    if d.get("enabled") is False:
        bits.append(tr("未启用", "disabled"))
    elif d.get("connected"):
        bits.append(tr("已打开", "open"))
    else:
        bits.append(tr("未打开", "not open"))
    line = "  - " + " ".join(bits)
    if "status" in d:
        line += " → " + _status_brief(d["status"])
    elif d.get("status_error"):
        line += tr(f" → 状态读不到：{d['status_error']}", f" -> status unreadable: {d['status_error']}")
    return line


def _sec_station(request, caller):
    inv = station_view.inventory()
    devs = inv.get("devices") or []
    lines = [tr("- 站点画像 station profile: optical（光学站：没有 STM / Nanonis；"
                "技能面只有光学与通用技能）",
                "- Station profile: optical (an optical station: no STM / Nanonis; the skill face "
                "shows optical and generic skills only)")]
    lines += list(_OPTICAL_RULES)
    if inv.get("error"):
        lines.append(tr(f"- 光学设备：读不到配置（{inv['error']}）",
                        f"- Optical devices: configuration unreadable ({inv['error']})"))
    elif not devs:
        lines.append(tr("- 光学设备：没有配置（config/optical_instruments.json 为空或不存在）",
                        "- Optical devices: none configured (config/optical_instruments.json is empty "
                        "or missing)"))
    else:
        n_open = sum(1 for d in devs if d.get("connected"))
        lines.append(tr(f"- 光学设备 {len(devs)} 台，进程里已打开 {n_open} 台"
                        "（来自配置；简报不会为了读状态去打开任何设备）：",
                        f"- Optical devices: {len(devs)} configured, {n_open} open in this process "
                        "(from the configuration; the briefing never opens a device to read it):"))
        lines += [_device_line(d) for d in devs]
    last_off = _last_safe_off()
    if last_off and not last_off.get("all_confirmed", True):
        bad = "; ".join(f"{d.get('device_id')}: {d.get('cause') or d.get('action')}"
                        for d in last_off.get("devices") or [] if not d.get("confirmed"))
        why = last_off.get("reason") or "-"
        at = str(last_off.get("at") or "")[:19]
        lines.append(tr(f"- ⚠ 上一次全部关光（{why}，{at}）没有全部确认：{bad}。请操作员到仪器上确认已经关掉。",
                        f"- ⚠ The last switch-everything-off ({why}, {at}) was NOT confirmed for: {bad}. "
                        "Ask the operator to check at the instrument that it is off."))
    return {"text": _cap("\n".join(lines)),
            "data": {"profile": station.PROFILE_OPTICAL, **inv, "last_safe_off": last_off}}


def _last_safe_off() -> dict | None:
    """The last emission_safety.safe_off_all result (E-STOP, withdrawn laser_area_safe), or None."""
    try:
        from mast.instruments import emission_safety

        return emission_safety.last_result()
    except Exception:  # noqa: BLE001 - the briefing never fails on this
        return None


def _slot_line(s: dict) -> str:
    if s["value"] is None:
        return f"- {s['id']}: " + tr("（未签署）", "(not signed)")
    state = tr("有效", "valid") if s["valid"] else tr("已失效", "EXPIRED")
    who = f"{s.get('set_by') or '?'} {str(s.get('set_at') or '')[:19]}"
    until = tr(f"，有效至 {s['valid_until'][:19]}", f", valid until {s['valid_until'][:19]}") if s.get("valid_until") else ""
    unit = f" {s['unit']}" if s.get("unit") else ""
    return f"- {s['id']}: {s['value']}{unit}  [{state}; {who}{until}]"


def _sec_optical_state(request, caller):
    ms = station_view.manual_state()
    if ms.get("error"):
        raise RuntimeError(ms["error"])
    slots = ms["slots"]
    lines = [_slot_line(s) for s in slots]
    head = tr("手动光学状态（只有操作员能写；每条谱的 sidecar 自动带上这份快照）：",
              "Manual optical state (only the operator writes it; every spectrum's sidecar carries this "
              "snapshot):")
    return {"text": _cap(head + "\n" + "\n".join(lines)) if lines
            else tr("（没有定义任何手动光学状态槽位）", "(no manual optical-state slots are defined)"),
            "data": ms}


def _sec_operator_prompts(request, caller):
    pr = station_view.open_prompts(limit=8)
    if pr.get("error"):
        raise RuntimeError(pr["error"])
    n = pr["count"]
    if not n:
        return {"text": tr("（没有未答的操作员提示）", "(no operator prompts are waiting)"), "data": pr}
    lines = [tr(f"- 有 {n} 条操作员提示在等人答（你只能发和等，不能答；mast_wait 的 operator_reply 条件等答复）：",
                f"- {n} operator prompt(s) are waiting for a person (you can post and wait, you cannot "
                "answer; the mast_wait operator_reply condition waits for the reply):")]
    for it in pr["items"]:
        who = it.get("skill") or it.get("agent_id") or "?"
        lines.append(f"  - {it['id']} [{it['severity']}/{it['kind']}] {it['title']} ({who})")
    if n > len(pr["items"]):
        lines.append(tr(f"  - …另有 {n - len(pr['items'])} 条未列出", f"  - ... {n - len(pr['items'])} more not listed"))
    return {"text": _cap("\n".join(lines)), "data": pr}


def _sec_recent_spectra(request, caller):
    from mast.vista import optical as optical_view

    rows = optical_view.list_spectra(6)
    data = [{"file": s.path.name, "path": str(s.path), "size_bytes": s.size,
             "age_s": round(max(0.0, time.time() - s.mtime), 1)} for s in rows]
    text = "\n".join(f"- {r['file']}  ({r['size_bytes']} B, {_ago(r['age_s'])})  {r['path']}" for r in data)
    return {"text": text or tr("（实验文件夹里还没有谱）", "(no spectra in the experiment folder yet)"),
            "data": data}


OPTICAL_SECTIONS: dict[str, Callable[[Request, Caller], dict]] = {
    "station": _sec_station,
    "optical_state": _sec_optical_state,
    "operator_prompts": _sec_operator_prompts,
    "recent_spectra": _sec_recent_spectra,
}


def _section_table() -> dict[str, Callable[[Request, Caller], dict]]:
    """The sections this station's briefing offers, looked up per request.

    STM: ``SECTIONS`` itself (its keys, its order, entries monkeypatched in a test still
    win). Optical: the sections that exist on an optical station, in :data:`OPTICAL_ORDER` -
    the shared ones taken from ``SECTIONS`` at request time, the optical ones from
    :data:`OPTICAL_SECTIONS`. Asking an optical station for ``tip`` / ``instrument`` /
    ``live`` is a 422 that lists what does exist."""
    if not _is_optical():
        return SECTIONS
    return {name: (SECTIONS.get(name) or OPTICAL_SECTIONS[name]) for name in OPTICAL_ORDER}


SECTIONS: dict[str, Callable[[Request, Caller], dict]] = {
    "status": _sec_status,
    "scope": _sec_scope,
    "resume": _sec_resume,
    "tip": _sec_tip,
    "instrument": _sec_instrument,
    "live": _sec_live,
    "prefs": _sec_prefs,
    "safety": _sec_safety,
    "recent_actions": _sec_recent_actions,
    "recent_files": _sec_recent_files,
    "alarms": _sec_alarms,
    "notes": _sec_notes,
    "recording": _sec_recording,
    "jobs": _sec_jobs,
    "operator_requests": _sec_operator_requests,
    "optical_layouts": _sec_optical_layouts,
}


@router.get("/briefing")
def briefing(request: Request,
             sections: str = Query("", description="逗号分隔的段名；空 = 全部")):
    """分段简报。每段 ``{ok, text?, data?, error?}``；读不到的段名进 ``degraded``。
    顶层 ``text`` 是各段文字拼成的一份 markdown（给模型直接读）。零硬件 I/O。"""
    caller = caller_of(request)
    table = _section_table()
    wanted = [s.strip() for s in (sections or "").split(",") if s.strip()] or list(table)
    unknown = [s for s in wanted if s not in table]
    if unknown:
        raise ExtError(422, "unknown_section", tr(f"没有这些段：{unknown}", f"No such sections: {unknown}"), available=list(table))
    out: dict[str, dict] = {}
    degraded: list[dict] = []
    for name in wanted:
        try:
            sec = table[name](request, caller) or {}
            out[name] = {"ok": True, **sec}
        except Exception as exc:  # noqa: BLE001 — 一段坏了不许拖垮其余
            reason = f"{type(exc).__name__}: {exc}"[:300]
            out[name] = {"ok": False, "error": reason}
            degraded.append({"section": name, "reason": reason})
    parts = []
    for name in wanted:
        sec = out[name]
        if sec.get("ok") and name in _OMIT_WHEN_EMPTY and not str(sec.get("text") or "").strip():
            continue
        body = sec.get("text") if sec.get("ok") else tr(f"（读不到：{sec.get('error')}）", f"(unreadable: {sec.get('error')})")
        parts.append(f"## {name}\n{body}")
    text = "\n\n".join(parts)
    if len(text) > BRIEFING_TEXT_CAP:
        text = text[:BRIEFING_TEXT_CAP] + tr("\n…(简报截断；用 ?sections= 单独取某几段)", "\n...(briefing truncated; fetch single sections with ?sections=)")
    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "actor": caller.agent_id,
            "sections": out, "degraded": degraded, "text": text}


# ─────────────────────────────────────────────────────────────────────
# 作用域
# ─────────────────────────────────────────────────────────────────────

@router.get("/scope")
def get_scope(request: Request):
    """当前实验与样品，以及「这次的动作会不会进记录」。"""
    return _scope(request)


@router.post("/scope")
def set_scope(body: ScopeBody, request: Request):
    """切换或新建实验 / 样品。给 ``id`` 就切换到已有的；给 ``name`` 就按名字复用或新建。

    仪器正被占用时默认拒绝（在跑的动作会记到新作用域下）；``force: true`` 才切。
    """
    caller = caller_of(request)
    log, st = active_log(), storage_of(request)
    if log is None or st is None:
        raise ExtError(503, "not_wired", tr("记录系统未接线", "Record system not wired"),
                       missing=[n for n, v in (("experiment_log", log), ("storage", st)) if v is None])
    if body.experiment is None and body.sample is None:
        raise ExtError(422, "empty_request", tr("experiment 与 sample 至少给一个", "Give at least one of experiment and sample"))
    holder = _lock()
    warnings: list[str] = []
    if holder.get("held"):
        if not body.force:
            raise ExtError(409, "instrument_busy",
                           tr(f"仪器正被 {holder.get('owner')} 占用（{holder.get('skill')}）——"
                              "现在切作用域，它接下来的动作会记到新的实验/样品下。等它结束，或传 force:true。",
                              f"The instrument is in use by {holder.get('owner')} ({holder.get('skill')}) - "
                              "switching scope now would record its next actions under the new "
                              "experiment / sample. Wait for it to finish, or pass force:true."),
                           holder=holder)
        warnings.append(tr(f"仪器正被 {holder.get('owner')} 占用，已按 force 切换", f"The instrument is in use by {holder.get('owner')}; switched because of force"))
    reason = (body.reason or tr(f"外部 agent {caller.agent_id}", f"external agent {caller.agent_id}"))[:200]
    done: list[str] = []
    e = body.experiment
    if e is not None:
        if e.id:
            sc = log.switch_experiment(e.id, source="ext", reason=reason)
            if not sc.ok:
                raise ExtError(404 if sc.block_code == "unknown_experiment" else 409,
                               sc.block_code or "scope_refused", sc.error or tr("切换被拒", "Switch refused"))
            done.append(f"experiment→{e.id}")
        elif e.name:
            eid = log.start_experiment(e.name.strip(), e.goal or "", reuse_open=True)
            reused = bool(getattr(log, "_last_start_reused", False))
            done.append(f"experiment {'reused' if reused else 'created'}→{eid}")
        else:
            raise ExtError(422, "bad_experiment", tr("experiment 要给 id 或 name", "experiment needs an id or a name"))
    s = body.sample
    if s is not None:
        if s.id:
            sc = log.switch_sample(s.id, source="ext", reason=reason)
            if not sc.ok:
                raise ExtError(404 if sc.block_code == "unknown_sample" else 409,
                               sc.block_code or "scope_refused", sc.error or tr("切换被拒", "Switch refused"))
            done.append(f"sample→{s.id}")
        elif s.name:
            if not log.current_experiment_id:
                raise ExtError(409, "no_experiment", tr("没有当前实验 —— 先定实验再建样品", "No current experiment - set the experiment before creating a sample"))
            sid = log.start_sample(s.name.strip(), s.description or "",
                                   sample_type=s.sample_type or "", reuse_active=True)
            reused = bool(getattr(log, "_last_sample_reused", False))
            done.append(f"sample {'reused' if reused else 'created'}→{sid}")
        else:
            raise ExtError(422, "bad_sample", tr("sample 要给 id 或 name", "sample needs an id or a name"))
    logger.info("ext-gateway: %s 改了作用域：%s", caller.agent_id, done)
    return {**_scope(request), "changed": done, "warnings": warnings}


# ─────────────────────────────────────────────────────────────────────
# 急停
# ─────────────────────────────────────────────────────────────────────

@router.post("/estop")
def estop(body: EstopBody, request: Request):
    """硬件急停：与 ``/api/safety/emergency-stop`` 同一个动作（中止、停运动、退针、
    E_STOP），闩上的原因如实写成这个外部 agent；再取消所有外部作业。
    **解闩是操作员的事**（``/api/safety/clear-emergency``）。"""
    caller = caller_of(request)
    rt = runtime_of(request)
    fn = getattr(rt, "emergency_stop", None)
    if not callable(fn):
        raise ExtError(503, "not_wired", tr("没有活的 runtime / 急停钩子", "No live runtime / e-stop hook"))
    reason = body.reason or tr("未说明", "no reason given")
    why = tr(f"外部 agent {caller.agent_id} 触发急停：{reason}", f"External agent {caller.agent_id} triggered the e-stop: {reason}")[:200]
    logger.critical("ext-gateway E-STOP: %s", why)
    try:
        res = fn(why=why)
    except TypeError:                       # 旧签名（没有 why 参数）
        res = fn()
    jm = getattr(request.app.state, "jobs", None)
    cancelled = jm.cancel_all(by=caller.agent_id, reason=f"estop: {reason}") if jm else []
    return {"ok": True, **direct_exec.jsonable(res or {}), "why": why,
            "cancelled_jobs": cancelled,
            "note": tr("急停闩已挂；此后一切写动作被拒，直到操作员解闩。", "E-stop latch set; all write actions are refused until the operator clears the latch.")}


__all__ = ["OPTICAL_ORDER", "OPTICAL_SECTIONS", "SECTIONS", "router"]
