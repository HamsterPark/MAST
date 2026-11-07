"""笔记 / 向操作员发问 / 交接报告 —— 外部 agent 与 MAST 其余部分的双向通道。

* **笔记**写进 MAST 自己的记忆库（``CognitionContext.remember``，与实验记录同一个库）。
  内部 agent 每个用户回合会经 ``MemoryRecallMiddleware`` 自动召回相关记忆 —— 所以外部
  agent 在这里留下的结论，下一个接手的内部 agent 不用问就能看到；反过来，内部 agent
  写的记忆也能在这里搜到。命名空间与内部一致：有当前实验记 ``experiment:<id>``，
  否则 ``global``；路径 ``ext/<名字>/<内容哈希>`` ⇒ 同内容重发是幂等的。
* **发问**进心愿单的「agent → 操作员」请求（与内部 agent 的 ``request_user_action``
  同一张表），操作员在界面上答；答复里 ``path`` 是一等字段（最常见的问题是「文件在哪」）。
  ``kind`` 是 confirm / choice / number / text / attest 之一时改发**结构化操作员提示**
  （``mast.operator_hitl``）：网页、启动器弹窗、声音同时提醒，只有操作员能答（agent 能发、
  能读、能在 ``/wait`` 里等，不能答）。自由文本那条路不变。
* **交接报告**把这个调用方的作业、动作与笔记汇成一份 markdown，存进文档库
  （``kind=experiment_report``，``created_by=ext:<名字>``），操作员在「报告」页能看到。
"""

from __future__ import annotations

from mast.i18n import tr
import hashlib
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request

from mast.api import direct_exec
from mast.api.ext.common import (
    ExtError,
    caller_of,
    cognition_of,
    memory_store_of,
    scope_ids,
    storage_of,
)
from mast.api.ext.schemas import STRUCTURED_FIELDS, HandoverBody, NoteBody, RequestBody
from mast.operator_hitl import prompts as op_prompts
from mast.operator_hitl import service as op_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["collab"])

_NOTE_KINDS = ("note", "insight", "summary", "hypothesis", "protocol")


def _entry(r: dict, *, content_cap: int = 2000) -> dict:
    c = str(r.get("content") or "")
    return {"namespace": r.get("namespace"), "path": r.get("path"), "title": r.get("title"),
            "kind": r.get("kind"), "author": r.get("author"), "tags": r.get("tags") or [],
            "experiment_id": r.get("experiment_id"), "updated_at": r.get("updated_at"),
            "pinned": bool(r.get("pinned")),
            "content": c if len(c) <= content_cap else c[:content_cap] + tr("…(截断)", "...(truncated)")}


# ─────────────────────────────────────────────────────────────────────
# 笔记
# ─────────────────────────────────────────────────────────────────────

@router.post("/notes")
def write_note(body: NoteBody, request: Request):
    """写一条笔记进 MAST 记忆库（内部 agent 会自动召回）。同标题同内容重发不产生重复。"""
    from mast.agents._shared.cognition import _namespace_for

    caller = caller_of(request)
    eid, _ = scope_ids()
    warnings: list[str] = []
    if body.scope == "experiment" and not eid:
        warnings.append(tr("没有当前实验 —— 这条笔记存进了 global 命名空间", "No current experiment - this note was saved in the global namespace"))
    ns = _namespace_for(eid if body.scope == "experiment" else None)
    kind = body.kind if body.kind in _NOTE_KINDS else "note"
    digest = hashlib.sha1(f"{body.title}\n{body.content}".encode("utf-8")).hexdigest()[:12]
    path = f"ext/{caller.actor}/{digest}"
    tags = [str(t)[:40] for t in (body.tags or [])][:12]
    cog = cognition_of(request)
    try:
        if cog is not None and hasattr(cog, "remember"):
            res = cog.remember(ns, path, body.content, title=body.title, kind=kind, tags=tags,
                               experiment_id=eid if body.scope == "experiment" else None,
                               author=caller.agent_id)
        else:
            store = memory_store_of(request)
            if store is None:
                raise ExtError(503, "not_wired", tr("记忆库未接线", "Memory store not wired"), missing=["cognition"])
            res = store.write(ns, path, body.content, title=body.title, kind=kind, tags=tags,
                              experiment_id=eid if body.scope == "experiment" else None,
                              author=caller.agent_id)
            warnings.append(tr("语义索引未接线：这条笔记只能被子串检索到", "Semantic index not wired: this note can only be found by substring search"))
    except ExtError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ExtError(503, "write_failed", tr(f"笔记写不进去：{exc}", f"Could not write the note: {exc}")) from None
    return {"ok": True, "namespace": res.get("namespace", ns), "path": res.get("path", path),
            "id": res.get("id"), "author": caller.agent_id, "warnings": warnings}


@router.get("/notes")
def search_notes(request: Request, q: str = Query("", max_length=500),
                 scope: str = Query("both", description="both | experiment | global"),
                 limit: int = Query(10, ge=1, le=50)):
    """检索笔记：``q`` 非空时语义召回（当前实验 ∪ global；语义索引不可用时退回子串），
    ``q`` 为空时列最近的。内部 agent 写的记忆也在这里。每条带 ``match``（semantic /
    substring / recent）与 ``score``（语义召回时是查询与笔记的余弦相似度，-1..1，只在同一个
    嵌入模型内可比；其余为 null）：语义召回总会凑满 k 条，分数明显低的那几条多半与查询无关。"""
    from mast.agents._shared.cognition import _namespace_for

    eid, _ = scope_ids()
    store = memory_store_of(request)
    cog = cognition_of(request)
    if store is None:
        raise ExtError(503, "not_wired", tr("记忆库未接线", "Memory store not wired"), missing=["cognition"])
    scope = scope if scope in ("both", "experiment", "global") else "both"
    namespaces = {"both": [_namespace_for(eid), "global"] if eid else ["global"],
                  "experiment": [_namespace_for(eid)] if eid else [],
                  "global": ["global"]}[scope]
    rows: list[tuple[dict, float | None, str]] = []
    try:
        if q and scope == "both" and cog is not None and hasattr(cog, "recall_scored"):
            rows = list(cog.recall_scored(q, experiment_id=eid, k=limit) or [])
        elif q and scope == "both" and cog is not None and hasattr(cog, "recall"):
            # 只有 recall 的召回器：说不清是语义还是子串，就不替它说
            rows = [(r, None, "recall") for r in (cog.recall(q, experiment_id=eid, k=limit) or [])]
        elif q:
            for ns in namespaces:
                rows += [(r, None, "substring") for r in (store.search(q, namespace=ns, limit=limit) or [])]
        else:
            for ns in namespaces:
                rows += [(r, None, "recent") for r in (store.list(ns, limit=limit) or [])]
    except Exception as exc:  # noqa: BLE001
        raise ExtError(503, "read_failed", tr(f"记忆库读不了：{exc}", f"Could not read the memory store: {exc}")) from None
    seen: set[tuple] = set()
    out = []
    for r, score, how in rows:
        key = (r.get("namespace"), r.get("path"))
        if key in seen:
            continue
        seen.add(key)
        out.append({**_entry(r), "match": how, "score": score})
    return {"query": q, "scope": scope, "namespaces": namespaces, "count": len(out[:limit]),
            "notes": out[:limit]}


# ─────────────────────────────────────────────────────────────────────
# 向操作员发问
# ─────────────────────────────────────────────────────────────────────

@router.post("/requests")
def ask_operator(body: RequestBody, request: Request):
    """向操作员发一个请求 / 问题（界面上会亮起来）。同一个未答的问题重发不产生重复。
    **不要等在这里** —— 答复是异步的，之后用 GET /requests/{id} 或简报的
    ``operator_requests`` 段看，或用 ``/wait`` 的 operator_reply 条件等。

    ``kind`` 是 question / action / info（或别的自由词）：心愿单里的自由文本请求，操作员回一段文字。
    ``kind`` 是 confirm / choice / number / text / attest：**结构化提示**，可带 ``options``、
    ``number_min`` / ``number_max`` / ``unit``、``attest_slots``、``severity``、``timeout_s``；
    操作员在网页或启动器弹窗里答，答复是结构化的（choice / value）。attest = 请操作员签署
    一个手动光学状态（``attest_slots`` 里的槽位）；只有操作员能答，agent 不能。操作员看到的选项来自槽位
    定义，不来自你：是非槽位（如 laser_area_safe）不能带 ``options``（422 ``invalid_prompt``），
    choice 槽位只能从它自己的选项里选一部分、显示的是槽位定义的名字，number 槽位的单位和范围取槽位定义。
    没给 ``timeout_s`` 的提示 24 小时后按超时关闭；没答的提示太多回 429 ``too_many_open_prompts``。

    结构化提示**只有光学站有**。STM 站上每个请求都是文字请求，不管 ``kind`` 写什么（原样存），
    结构化字段被忽略、不报错 —— 和操作员提示出现之前一样。"""
    caller = caller_of(request)
    eid, _ = scope_ids()
    kind = (body.kind or "question").strip()
    structured_here = _structured_prompts_here()
    if structured_here and kind.lower() in op_prompts.PROMPT_KINDS:
        kind = kind.lower()
    given = _structured_fields_given(body)       # always empty on an STM station: RequestBody dropped them
    if not structured_here or kind not in op_prompts.PROMPT_KINDS:
        if given:
            raise ExtError(422, "structured_fields_need_kind",
                           tr(f"字段 {given} 只用于结构化提示：kind 要是 "
                              f"{' | '.join(op_prompts.PROMPT_KINDS)} 之一（现在是 {kind!r}）",
                              f"The fields {given} apply to structured prompts only: kind must be one of "
                              f"{' | '.join(op_prompts.PROMPT_KINDS)} (it is {kind!r})"),
                           kinds=list(op_prompts.PROMPT_KINDS))
        from mast.wishlist import post_agent_request

        rec = post_agent_request(caller.agent_id, body.message, kind=(body.kind or "question"),
                                 experiment_id=eid)
        if not isinstance(rec, dict) or rec.get("error"):
            raise ExtError(422, "request_rejected", str((rec or {}).get("error") or tr("请求没被接受", "Request not accepted")))
        return {"ok": True, "request": direct_exec.jsonable(rec)}
    return _post_structured(body, kind, caller, eid)


_STRUCTURED_FIELDS = STRUCTURED_FIELDS


def _structured_prompts_here() -> bool:
    """Structured operator prompts exist on the optical station only (``mast.station``). An STM station keeps what it always had: every request is a text request.
    Unreadable profile = STM."""
    try:
        from mast import station

        return bool(station.is_optical())
    except Exception:  # noqa: BLE001
        return False

#: open (unanswered) structured prompts one external agent may have at a time
MAX_OPEN_PROMPTS_PER_AGENT = 25


def _structured_fields_given(body: RequestBody) -> list[str]:
    return [f for f in _STRUCTURED_FIELDS if getattr(body, f) is not None]


def _first_line(text: str, limit: int = 120) -> str:
    line = next((ln.strip() for ln in str(text).splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[:limit - 1] + "…"


def _same_question(a: op_prompts.OperatorPrompt, b: op_prompts.OperatorPrompt) -> bool:
    def shape(p: op_prompts.OperatorPrompt) -> tuple:
        return (p.agent_id, p.kind, p.title, p.message, [(o.id, o.label) for o in p.options],
                p.number_min, p.number_max, p.unit, list(p.attest_slots), p.severity)

    return shape(a) == shape(b)


def _post_structured(body: RequestBody, kind: str, caller: Any, eid: str | None) -> dict:
    prompt = op_prompts.OperatorPrompt(
        kind=kind,
        title=(body.title or "").strip() or _first_line(body.message),
        message=body.message.strip(),
        options=[op_prompts.PromptOption(o.id, o.label) for o in (body.options or [])],
        number_min=body.number_min, number_max=body.number_max, unit=body.unit or "",
        attest_slots=list(body.attest_slots or []),
        severity=body.severity or "action",
        agent_id=caller.agent_id, timeout_s=body.timeout_s,
        context={"source": "ext", "actor": caller.actor, "session": caller.session,
                 "experiment_id": eid})
    try:
        # validate and make the prompt say only what the optical-state registry allows (an attest
        # prompt's options and labels come from the slot definitions, not from the agent): before
        # the comparison below, so a retry of the same question is still recognised as the same
        op_prompts.vet(prompt)
        # same agent asking the same open question again (a retry after a lost reply): the
        # original, as for free-text requests
        mine = 0
        for existing in op_prompts.list_open():
            if _same_question(existing, prompt):
                return {"ok": True, "request": direct_exec.jsonable(op_service.request_view(existing))}
            mine += existing.agent_id == prompt.agent_id
        if mine >= MAX_OPEN_PROMPTS_PER_AGENT:
            # a loop that keeps asking would bury the operator's list under its own questions
            raise ExtError(429, "too_many_open_prompts",
                           tr(f"你已经有 {mine} 个操作员还没答的提示（上限 {MAX_OPEN_PROMPTS_PER_AGENT}）："
                              "等操作员答复，再问新的。",
                              f"You already have {mine} prompts the operator has not answered (the limit is "
                              f"{MAX_OPEN_PROMPTS_PER_AGENT}): wait for the operator, then ask again."),
                           open=mine)
        op_prompts.post(prompt)
    except op_prompts.TooManyOpenPrompts as exc:
        # the per-agent limit above counts by the name the agent gave itself; this one does not
        raise ExtError(429, "too_many_open_prompts",
                       tr(f"操作员那里已经有太多没答的提示（{exc.scope}：{exc.open} 个，上限 {exc.limit}）："
                          "等操作员答复，再问新的。",
                          f"The operator already has too many unanswered prompts ({exc.scope}: {exc.open}, "
                          f"the limit is {exc.limit}): wait for the operator, then ask again."),
                       open=exc.open, limit=exc.limit, scope=exc.scope) from None
    except op_prompts.PromptError as exc:
        raise ExtError(422, "invalid_prompt", str(exc)) from None
    return {"ok": True, "request": direct_exec.jsonable(op_service.request_view(prompt))}


#: 请求状态的对外名字 → 心愿单里的存储名。未答的在心愿单里叫 ``pending``；``open`` 是同义词。
_REQUEST_STATUS_ALIASES = {"pending": "pending", "open": "pending", "done": "done",
                           "dismissed": "dismissed"}


@router.get("/requests")
def my_requests(request: Request,
                status: str = Query("", description="pending (= open) | done | dismissed；空 = 全部")):
    """本调用方发过的请求（未答的在前）与答复。"""
    from mast.wishlist import list_agent_requests

    caller = caller_of(request)
    wanted = None
    if status.strip():
        wanted = _REQUEST_STATUS_ALIASES.get(status.strip().lower())
        if wanted is None:           # 未知状态回 422，不回一个永远为空的列表
            raise ExtError(422, "unknown_status", tr(f"没有状态 {status!r}", f"No such status {status!r}"),
                           available=sorted(_REQUEST_STATUS_ALIASES))
    rows = [r for r in list_agent_requests(wanted)
            if r.get("agent_id") == caller.agent_id]
    # structured prompts this caller posted, in the same shape (status: pending | done | dismissed)
    rows += [r for r in op_service.prompt_rows()
             if r.get("agent_id") == caller.agent_id and (wanted is None or r.get("status") == wanted)]
    rows.sort(key=lambda r: (r.get("status") != "pending", r.get("created_at") or ""))
    return {"count": len(rows), "requests": direct_exec.jsonable(rows)}


@router.get("/requests/{request_id}")
def get_request(request_id: str, request: Request):
    """单条请求与答复（``note`` 是文字答复，``path`` 是操作员给的文件/目录路径）。结构化提示
    （id 形如 ``op-0001-…``）多带 ``prompt_status`` 与 ``answer``（choice / value / answered_by）。"""
    from mast.wishlist import get_request as _get

    prompt = op_prompts.get(request_id)
    if prompt is not None:
        return {"request": direct_exec.jsonable(op_service.request_view(
            prompt, op_prompts.get_backend().get_answer(request_id)))}
    rec = _get(request_id)
    if rec is None:
        raise ExtError(404, "unknown_request", tr(f"没有请求 {request_id!r}", f"No such request {request_id!r}"))
    return {"request": direct_exec.jsonable(rec)}


# ─────────────────────────────────────────────────────────────────────
# 交接报告
# ─────────────────────────────────────────────────────────────────────

def _since_ok(ts: str | None, since: str | None) -> bool:
    if not since:
        return True
    return str(ts or "") >= str(since)


def _signed_by(context: Any, agent_id: str) -> bool:
    """v1 ``actions.context`` 是 ``ext:<actor>`` 或 ``ext:<actor>/<session>``。
    不能用裸前缀：``ext:tester`` 是 ``ext:tester2`` 的前缀。"""
    c = str(context or "")
    return c == agent_id or c.startswith(agent_id + "/")


@router.post("/handover")
def handover(body: HandoverBody, request: Request):
    """生成交接报告存进文档库。汇总：你写的总结与下一步、你这次提交的作业及结局、
    记在当前实验里署你名字的动作、你写的笔记。操作员在「报告」页能看到。"""
    caller = caller_of(request)
    eid, sid = scope_ids()
    st = storage_of(request)
    jm = getattr(request.app.state, "jobs", None)

    jobs = [j for j in (jm.list(actor=caller.actor, limit=200) if jm else [])
            if _since_ok(j.created_at, body.since)]
    actions: list[dict] = []
    if st is not None and eid and hasattr(st, "recent_actions"):
        try:
            actions = [r for r in st.recent_actions(eid, 500)
                       if _signed_by(r.get("context"), caller.agent_id)
                       and _since_ok(r.get("timestamp"), body.since)]
        except Exception as exc:  # noqa: BLE001
            logger.debug("handover: 读动作失败(%s)", exc)
    notes: list[dict] = []
    store = memory_store_of(request)
    if store is not None:
        try:
            from mast.agents._shared.cognition import _namespace_for

            for ns in ([_namespace_for(eid)] if eid else []) + ["global"]:
                notes += [r for r in (store.list(ns, limit=200) or [])
                          if r.get("author") == caller.agent_id
                          and _since_ok(r.get("updated_at"), body.since)]
        except Exception as exc:  # noqa: BLE001
            logger.debug("handover: 读笔记失败(%s)", exc)

    steps = body.next_steps if isinstance(body.next_steps, list) else (
        [body.next_steps] if str(body.next_steps or "").strip() else [])
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = body.title.strip() or tr(f"外部 agent 交接 · {caller.actor} · {now}",
                                     f"External agent handover · {caller.actor} · {now}")
    _none = tr("无", "none")
    lines = [f"# {title}", "",
             tr(f"- 署名：`{caller.agent_id}`（会话 `{caller.thread_id}`）",
                f"- Signed: `{caller.agent_id}` (session `{caller.thread_id}`)"),
             tr(f"- 实验 / 样品：`{eid or _none}` / `{sid or _none}`",
                f"- Experiment / sample: `{eid or _none}` / `{sid or _none}`"),
             tr(f"- 生成时间：{now}", f"- Generated: {now}"), "", tr("## 总结", "## Summary"), "",
             body.summary.strip(), ""]
    if steps:
        lines += [tr("## 下一步", "## Next steps"), ""] + [f"- {s}" for s in steps if str(s).strip()] + [""]
    lines += [tr("## 作业", "## Jobs"), ""]
    if jobs:
        lines += [tr("| 时间 | 技能 | 结局 | 说明 |", "| Time | Skill | Outcome | Note |"), "|---|---|---|---|"]
        for j in reversed(jobs):
            res = j.result or {}
            why = (res.get("error") or res.get("summary") or "").replace("|", "/").replace("\n", " ")
            lines.append(f"| {j.created_at[:19]} | {j.skill} | {j.state}"
                         f"{tr('（', ' (') + j.refused_by + tr('）', ')') if j.refused_by else ''} | {why[:160]} |")
    else:
        lines.append(tr("（本进程里没有这个调用方的作业记录）", "(No job records for this caller in this process)"))
    lines += ["", tr("## 实验记录里署这个名字的动作", "## Actions signed with this name in the experiment record"), ""]
    if actions:
        for r in reversed(actions):
            mark = "✓" if r.get("success") else "✗"
            lines.append(f"- {str(r.get('timestamp') or '')[:19]} {r.get('skill_name')} {mark}"
                         + (f" — {str(r.get('error'))[:160]}" if r.get("error") else ""))
    else:
        lines.append(tr("（没有 —— 没有当前实验时动作不进记录）", "(None - actions are not recorded when there is no current experiment)") if not eid
                     else tr("（没有）", "(None)"))
    lines += ["", tr("## 笔记", "## Notes"), ""]
    if notes:
        for r in notes:
            _body = ' '.join(str(r.get('content') or '').split())[:300]
            _head = r.get('title') or r.get('path')
            lines.append(tr(f"- **{_head}**（{r.get('kind')}）：{_body}",
                            f"- **{_head}** ({r.get('kind')}): {_body}"))
    else:
        lines.append(tr("（没有）", "(None)"))
    md = "\n".join(lines) + "\n"
    try:
        from mast.documents.store import store as doc_store

        res = doc_store().save(text=md, kind="experiment_report", title=title,
                               experiment_id=eid, sample_id=sid,
                               created_by=caller.agent_id, note=tr("外部 agent 交接报告", "External agent handover report"),
                               use_turn_context=False)
    except Exception as exc:  # noqa: BLE001
        raise ExtError(503, "save_failed", tr(f"交接报告存不进文档库：{exc}", f"Could not save the handover report to the document library: {exc}"), markdown=md) from None
    if not getattr(res, "ok", False):
        raise ExtError(503, "save_failed", str(getattr(res, "error", "") or tr("文档库拒绝了保存", "The document library refused the save")),
                       markdown=md)
    return {"ok": True, "doc_id": res.doc_id, "version": res.version, "path": res.path,
            "title": title, "jobs": len(jobs), "actions": len(actions), "notes": len(notes)}


__all__ = ["router"]
