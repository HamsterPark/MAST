"""A composite ``human`` node — and the ``AskOperator`` skill — waiting for a real operator.

On the agent path (LangGraph) a ``human`` node pauses the graph with ``interrupt()`` and the
private chat's approval pane takes the decision. On the **direct / external path** — a job
submitted through ``/api/ext/v1``, a VISTA act, the skill-run endpoints — there is no graph to
pause: until now ``direct_exec`` turned that interrupt into ``refused_by="needs_human_node"`` and
the external agent could do nothing but give up.

Now ``direct_exec.run_and_record`` opens a :func:`direct_scope` around the skill, and
``skills.composite.interpreter.human_channel`` — which sees the scope through a context
variable — maps the node onto a structured operator prompt (:func:`decide`): the node's
``routes`` become the options of a choice (a node with one route becomes a yes/no confirm), the
call blocks in :func:`mast.operator_hitl.prompts.ask` with the run's ``check_abort`` and a
bounded timeout, and the chosen route comes back as the node's decision. A timeout, a dismissal
or a cancelled prompt ends the run with an explicit :class:`HumanNodeFailed`; an operator stop
ends the wait at once and aborts the composite the way any other abort does.

The LangGraph path is untouched: no scope is set there.

Only the optical station waits for a person here: on an STM station a
``human`` node on the direct path ends the job at once with ``needs_human_node``, exactly as
before the operator prompts existed - no wait, no popup, no instrument token held for an
operator who may not be there. ``direct_exec`` sets :attr:`HumanScope.human_nodes` from the
station profile; the scope itself is set either way, so what ``AskOperator`` records about the
run it belongs to does not depend on the profile.

While the call blocks, the job that runs it can show ``waiting_operator`` to the external agent:
``api/ext/jobs.py`` installs a :func:`wait_sink` around the job body and this module reports
``{"prompt_id", "kind", "title", "since"}`` to it when the prompt is posted and ``None`` when
the wait ends.

A context variable does not cross into threads the skill starts itself; a ``human`` node asked
from such a thread falls back to the old behaviour (no channel).
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from mast.i18n import tr
from mast.operator_hitl import prompts

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TIMEOUT_S", "MAX_TIMEOUT_S", "HumanScope", "HumanNodeFailed",
    "direct_scope", "current_scope", "wait_sink", "clamp_timeout", "ask_blocking", "decide",
]

#: how long a ``human`` node / ``AskOperator`` waits for a person unless told otherwise
DEFAULT_TIMEOUT_S = 1800.0
#: the longest any single wait for a person may be (4 h)
MAX_TIMEOUT_S = 4 * 3600.0


class HumanNodeFailed(RuntimeError):
    """The operator did not decide: no answer in time, the prompt was dismissed or cancelled,
    or the operator declined a confirmation. ``status`` ∈ timeout | dismissed | cancelled |
    declined; ``prompt_id`` names the prompt in the operator's history."""

    def __init__(self, message: str, *, status: str, prompt_id: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.prompt_id = prompt_id


@dataclass(frozen=True)
class HumanScope:
    """What a ``human`` node needs from the run it sits in.

    ``human_nodes``: whether a ``human`` node of this run asks a real operator (the optical
    station) or keeps its old behaviour (an STM station: ``needs_human_node``). The rest of the
    scope - which job, which agent - is there either way."""

    check_abort: Callable[[], object] | None
    skill: str = ""
    agent_id: str | None = None
    job_id: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    human_nodes: bool = True


_SCOPE: contextvars.ContextVar[HumanScope | None] = contextvars.ContextVar(
    "mast_operator_human_scope", default=None)
_SINK: contextvars.ContextVar[Callable[[dict | None], None] | None] = contextvars.ContextVar(
    "mast_operator_wait_sink", default=None)


@contextlib.contextmanager
def direct_scope(*, check_abort: Callable[[], object] | None, skill: str = "",
                 agent_id: str | None = None, job_id: str | None = None,
                 timeout_s: float | None = None, human_nodes: bool = True) -> Iterator[HumanScope]:
    """Mark this thread as running a skill on the direct / external path
    (``direct_exec.run_and_record`` opens it around ``ec.run``). With ``human_nodes`` (default)
    its ``human`` nodes ask a real operator; without, they keep their old behaviour."""
    scope = HumanScope(check_abort=check_abort, skill=str(skill or ""),
                       agent_id=agent_id or None, job_id=job_id or None,
                       timeout_s=clamp_timeout(timeout_s, DEFAULT_TIMEOUT_S),
                       human_nodes=bool(human_nodes))
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def current_scope() -> HumanScope | None:
    return _SCOPE.get()


@contextlib.contextmanager
def wait_sink(fn: Callable[[dict | None], None] | None) -> Iterator[None]:
    """Report "waiting for the operator" to *fn* while code inside the block waits.
    ``fn(info)`` gets a dict when a prompt is posted and ``None`` when the wait is over."""
    token = _SINK.set(fn)
    try:
        yield
    finally:
        _SINK.reset(token)


def clamp_timeout(value: Any, default: float = DEFAULT_TIMEOUT_S) -> float:
    """*value* seconds as a float in ``(0, MAX_TIMEOUT_S]``; *default* when it is not a
    positive finite number."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return min(float(default), MAX_TIMEOUT_S)
    if not (v > 0) or v != v or v == float("inf"):
        return min(float(default), MAX_TIMEOUT_S)
    return min(v, MAX_TIMEOUT_S)


def _report(sink: Callable[[dict | None], None] | None, info: dict | None) -> None:
    if sink is None:
        return
    try:
        sink(info)
    except Exception:  # noqa: BLE001 - reporting must never break the wait
        logger.debug("operator wait sink failed", exc_info=True)


def ask_blocking(prompt: prompts.OperatorPrompt, *, check_abort: Callable[[], object] | None,
                 timeout_s: float) -> prompts.OperatorAnswer:
    """:func:`prompts.ask`, reporting the wait to the job sink (if any). The prompt is
    cancelled when the wait ends for any reason other than an answer."""
    sink = _SINK.get()

    def _posted(p: prompts.OperatorPrompt) -> None:
        _report(sink, {"prompt_id": p.id, "kind": p.kind, "title": p.title,
                       "since": datetime.now(timezone.utc).isoformat(timespec="seconds")})

    try:
        return prompts.ask(prompt, check_abort=check_abort, timeout_s=timeout_s, on_posted=_posted)
    finally:
        _report(sink, None)


# ── a ``human`` node ───────────────────────────────────────────────────────────

#: friendly labels for route names the shipped composites use; any other route is shown as
#: its own name (the route id is what comes back either way)
_ROUTE_LABELS = {
    "cancel": ("取消", "Cancel"), "skip": ("跳过", "Skip"), "resolved": ("已处理", "Done"),
    "home": ("回零", "Home it"), "go": ("继续", "Go on"), "stop": ("停止", "Stop"),
    "laser_on": ("激光已打开", "The laser is on"), "laser_off": ("激光已关闭", "The laser is off"),
    "laser_blocked": ("激光已遮挡", "The laser is blocked"),
}
_SAFETY_WORDS = ("laser", "attest", "safety", "interlock")


def _route_label(route: str) -> str:
    zh_en = _ROUTE_LABELS.get(route)
    return tr(*zh_en) if zh_en else route


def _raise_if_aborted(check_abort: Callable[[], object] | None, what: str) -> None:
    if check_abort is None:
        return
    if check_abort():
        from mast.skills.composite._base import AbortRequested

        raise AbortRequested(what)


def decide(payload: dict, scope: HumanScope) -> dict:
    """Ask the operator what a composite ``human`` node asks; returns the node's decision
    ``{"route", "note", "by"}`` or raises (see the module docstring)."""
    # one option per distinct route name (a repeated name would make the prompt invalid), and
    # bounded like any prompt
    routes = list(dict.fromkeys(str(r) for r in (payload.get("routes") or [])))[:prompts.MAX_OPTIONS] \
        or ["resolved"]
    workflow = str(payload.get("workflow") or scope.skill or "").strip()[:100]
    node_id = str(payload.get("node_id") or "").strip()[:100]
    message = str(payload.get("message") or "").strip()[:prompts.MAX_MESSAGE_CHARS]
    where = f"{workflow} · {node_id}" if workflow and node_id else (workflow or node_id)
    _raise_if_aborted(scope.check_abort, where or "human node")     # a stopped run asks nobody

    kind = "choice" if len(routes) >= 2 else "confirm"
    options = ([prompts.PromptOption(r, _route_label(r)) for r in routes]
               if kind == "choice" else [])
    haystack = " ".join([node_id, *routes]).lower()
    severity = payload.get("severity")
    if severity not in prompts.SEVERITIES:
        severity = "safety" if any(w in haystack for w in _SAFETY_WORDS) else "action"
    timeout_s = clamp_timeout(payload.get("timeout_s"), scope.timeout_s)
    try:
        inputs = _plain(payload.get("inputs"))
        if len(json.dumps(inputs)) > 8000:           # the prompt's context is bounded (20 kB)
            inputs = {"_truncated": True, "keys": list(inputs)[:30] if isinstance(inputs, dict) else []}
    except Exception:  # noqa: BLE001
        inputs = {}
    prompt = prompts.OperatorPrompt(
        kind=kind,
        title=(tr(f"{workflow}：需要你决定", f"{workflow}: your decision is needed") if workflow
               else tr("工作流需要你决定", "A workflow needs your decision")),
        message=message or tr("请决定下一步。", "Please decide what happens next."),
        options=options, severity=severity,
        job_id=scope.job_id, skill=scope.skill or None, agent_id=scope.agent_id,
        context={"source": "composite_human_node", "workflow": workflow, "node_id": node_id,
                 "decision_key": str(payload.get("decision_key") or ""), "inputs": inputs,
                 "routes": routes})
    try:
        ans = ask_blocking(prompt, check_abort=scope.check_abort, timeout_s=timeout_s)
    except prompts.PromptAborted:
        from mast.skills.composite._base import AbortRequested

        raise AbortRequested(where or "human node") from None

    label = where or tr("human 节点", "human node")
    if ans.status == "answered":
        if kind == "confirm":
            if ans.choice != "yes":
                raise HumanNodeFailed(
                    tr(f"{label}：操作员选择了「否」（提示 {prompt.id}）。这个节点之后的步骤都没有执行。",
                       f"{label}: the operator answered no (prompt {prompt.id}). Nothing after "
                       "this node ran."),
                    status="declined", prompt_id=prompt.id)
            route = routes[0]
        else:
            route = str(ans.choice)
        return {"route": route, "note": ans.note, "by": ans.answered_by}
    why = f" ({ans.note})" if ans.note else ""
    if ans.status == "timeout":
        raise HumanNodeFailed(
            tr(f"{label}：操作员在 {timeout_s:g} 秒内没有答复（提示 {prompt.id} 已按超时关闭）。"
               "这个节点之后的步骤都没有执行。",
               f"{label}: the operator did not answer within {timeout_s:g} s (prompt {prompt.id} "
               "was closed as timed out). Nothing after this node ran."),
            status="timeout", prompt_id=prompt.id)
    if ans.status == "dismissed":
        raise HumanNodeFailed(
            tr(f"{label}：操作员关闭了提示而没有回答{why}（提示 {prompt.id}）。这个节点之后的步骤都没有执行。",
               f"{label}: the operator dismissed the prompt without answering{why} (prompt "
               f"{prompt.id}). Nothing after this node ran."),
            status="dismissed", prompt_id=prompt.id)
    raise HumanNodeFailed(
        tr(f"{label}：提示被取消{why}（提示 {prompt.id}）。这个节点之后的步骤都没有执行。",
           f"{label}: the prompt was cancelled{why} (prompt {prompt.id}). Nothing after this "
           "node ran."),
        status="cancelled", prompt_id=prompt.id)


def _plain(obj: Any, _depth: int = 0) -> Any:
    """Node inputs as plain JSON data for the prompt's ``context`` (bounded)."""
    if _depth > 4:
        return None
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj if not isinstance(obj, str) else obj[:500]
    if isinstance(obj, float):
        return obj if obj == obj and abs(obj) != float("inf") else None
    if isinstance(obj, dict):
        return {str(k)[:80]: _plain(v, _depth + 1) for k, v in list(obj.items())[:40]}
    if isinstance(obj, (list, tuple)):
        return [_plain(v, _depth + 1) for v in list(obj)[:40]]
    return str(obj)[:200]
