"""Operator-prompt and optical-state skills (AskOperator, ReadOpticalState).

Owner: hitl-core agent. Contract: docs/v2/design/optical_station_witec.md §6.3.
This module is imported by ``skills/builtins/__init__.py`` so the frozen build
discovers it (core/registry.py frozen fallback).

=================  ========  =====================================================
skill              safety    what
=================  ========  =====================================================
AskOperator        CONFIRM   post a structured prompt (confirm / choice / number /
                             text / attest) and **block** until the operator
                             answers; bounded, cancellable
ReadOpticalState   AUTO      the manual optical state: slot definitions, what is
                             registered, what is expired or unset
=================  ========  =====================================================

There is deliberately **no skill that writes the optical state** and none that answers a
prompt: what is physically in the beam path, and whether a person confirmed something, is the
operator's word alone (web UI or launcher popup; ``mast/api/routes/operator.py``). An
``attest`` prompt asked here is answered there, and only there.

Both are ``READ`` skills for the instrument arbitration: ``AskOperator`` may wait for hours
and must not hold the instrument token meanwhile (the token policy keys off the category,
``core/instrument_lock.needs_token``). Neither carries a data tag, so the sample gate lets them
through.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.i18n import tr
from mast.operator_hitl import human_node, optical_state, prompts
from mast.skills.base import BaseSkill

logger = logging.getLogger(__name__)

__all__ = ["AskOperator", "ReadOpticalState"]

_TAGS = ["operator", "human", "optics"]


def _abort_predicate(context: Any) -> Callable[[], bool] | None:
    fn = getattr(context, "check_abort", None)
    return (lambda: bool(fn())) if callable(fn) else None


def _parse_options(raw: Any) -> list[prompts.PromptOption]:
    """``options``: a JSON list of ``{"id", "label"}`` objects (or of strings), or a comma
    separated text of ``id`` and ``id=label`` items."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items: list[Any] = list(raw)
    else:
        text = str(raw).strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                items = json.loads(text)
            except ValueError as exc:
                raise ValueError(f"options is not valid JSON: {exc}") from None
            if not isinstance(items, list):
                raise ValueError("options must be a JSON list")
        else:
            items = [part for part in (p.strip() for p in text.split(",")) if part]
    out: list[prompts.PromptOption] = []
    for item in items:
        if isinstance(item, dict):
            ident = str(item.get("id") or "").strip()
            label = str(item.get("label") or ident).strip()
        else:
            ident, _, label = str(item).partition("=")
            ident, label = ident.strip(), (label.strip() or ident.strip())
        if not ident:
            raise ValueError("every option needs an id")
        out.append(prompts.PromptOption(ident, label))
    return out


def _parse_slots(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(s).strip() for s in raw if str(s).strip()]
    text = str(raw).strip()
    if text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"attest_slots is not valid JSON: {exc}") from None
        return [str(s).strip() for s in data if str(s).strip()] if isinstance(data, list) else []
    return [part for part in (p.strip() for p in text.split(",")) if part]


class AskOperator(BaseSkill):
    """Ask the operator something and wait for the answer."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="AskOperator",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.CONFIRM,
            description=tr(
                "向操作员提一个结构化问题并**阻塞等答复**：网页上亮起提示卡、启动器弹窗并响铃，只有操作员能答。"
                "kind：confirm（是 / 否）、choice（从 options 里选一个）、number（给一个数，可限 number_min / "
                "number_max，带 unit）、text（一段文字）、attest（请操作员签署手动光学状态，答复会写进 "
                "attest_slots 里的槽位，例如换了哪片 λ/2）。等待有上限（timeout_s，缺省 1800 s，至多 4 h），"
                "作业被取消或操作员中止时立刻结束并撤回提示。返回答复：status、choice、value、note、answered_by；"
                "操作员答了就 success（confirm 答「否」也是答复，看 data.ok / data.choice）；超时、被关闭、被取消"
                "则失败并说明原因。attest 的答复里 data.attested 是 {槽位: 操作员记下的值}，操作员把它记成 False"
                "（例如激光区「不安全」）时 data.ok 是 false、summary 会明说 —— 那是一个答复，不是批准。"
                "attest 提示里选项来自槽位定义（是非槽位不能带 options）。不取仪器令牌，等待期间不占仪器。",
                "Ask the operator a structured question and **block until they answer**: a prompt card lights up in "
                "the web UI and the launcher pops up with a sound; only the operator can answer. kind: confirm "
                "(yes / no), choice (pick one of options), number (a value, optionally bounded by number_min / "
                "number_max, with unit), text (free text), attest (ask the operator to sign off the manual "
                "optical state; the answer is written to the slots named in attest_slots, e.g. which λ/2 is "
                "installed). The wait is bounded (timeout_s, default 1800 s, at most 4 h) and ends at once, "
                "withdrawing the prompt, when the job is cancelled or the operator aborts. Returns the answer: "
                "status, choice, value, note, answered_by. success means the operator answered (a confirm "
                "answered no is still an answer: read data.ok / data.choice); a timeout, a dismissed or a "
                "cancelled prompt fails with the reason. For an attest, data.attested is {slot: the value the "
                "operator recorded}; when the operator recorded False (say the laser area is NOT safe) data.ok is "
                "false and the summary says so - that is an answer, not an approval. The options of an attest "
                "prompt come from the slot definitions (a yes/no slot takes no options). Takes no instrument "
                "token and does not hold the instrument while waiting."),
            parameters=[
                ParameterSpec(name="title", type="str", required=True,
                              description=tr("提示的标题（一句话）", "The prompt's heading (one sentence)")),
                ParameterSpec(name="message", type="str", required=False, default="",
                              description=tr("要操作员做 / 决定什么，写完整", "What the operator is to do or decide, in full")),
                ParameterSpec(name="kind", type="str", required=False, default="confirm",
                              allowed_values=list(prompts.PROMPT_KINDS),
                              description=tr("confirm | choice | number | text | attest",
                                             "confirm | choice | number | text | attest")),
                ParameterSpec(name="options", type="str", required=False, default="",
                              description=tr("choice / attest：可选答案。JSON 列表 [{\"id\":…,\"label\":…}]，"
                                             "或逗号分隔的 id（可写 id=显示名）；choice 至少两个",
                                             "choice / attest: the answers to pick from. A JSON list "
                                             "[{\"id\":…,\"label\":…}] or comma-separated ids (id=label allowed); "
                                             "a choice needs two or more")),
                ParameterSpec(name="number_min", type="float", required=False, default=None,
                              description=tr("number：允许的最小值", "number: the lowest accepted value")),
                ParameterSpec(name="number_max", type="float", required=False, default=None,
                              description=tr("number：允许的最大值", "number: the highest accepted value")),
                ParameterSpec(name="unit", type="str", required=False, default="",
                              description=tr("number：数值旁显示的单位，例如 deg", "number: the unit shown beside the value, e.g. deg")),
                ParameterSpec(name="attest_slots", type="str", required=False, default="",
                              description=tr("attest：答复要写进的光学状态槽位 id，逗号分隔或 JSON 列表",
                                             "attest: ids of the optical-state slots the answer sets, comma-separated or a JSON list")),
                ParameterSpec(name="severity", type="str", required=False, default="action",
                              allowed_values=list(prompts.SEVERITIES),
                              description=tr("info | action | safety：提醒的响亮程度", "info | action | safety: how loudly the operator is alerted")),
                ParameterSpec(name="timeout_s", type="float", unit="s", required=False,
                              default=human_node.DEFAULT_TIMEOUT_S, min_value=1.0,
                              max_value=human_node.MAX_TIMEOUT_S,
                              description=tr("最长等多久（秒，缺省 1800，至多 4 小时）",
                                             "How long to wait at most (seconds, default 1800, at most 4 hours)")),
            ],
            estimated_duration_s=300.0,
            composition_level=0,
            tags=[*_TAGS, "hitl", "ask"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "AskOperator"
        try:
            prompt = prompts.OperatorPrompt(
                kind=str(params.get("kind") or "confirm"),
                title=str(params.get("title") or "").strip(),
                message=str(params.get("message") or "").strip(),
                options=_parse_options(params.get("options")),
                number_min=params.get("number_min"), number_max=params.get("number_max"),
                unit=str(params.get("unit") or ""),
                attest_slots=_parse_slots(params.get("attest_slots")),
                severity=str(params.get("severity") or "action"),
                context={"source": "AskOperator"})
            prompt.validate()
        except (prompts.PromptError, ValueError) as exc:
            return SkillResult(skill_name=name, success=False, error=f"invalid prompt: {exc}")
        scope = human_node.current_scope()
        if scope is not None:
            prompt.job_id, prompt.skill, prompt.agent_id = scope.job_id, scope.skill or None, scope.agent_id
        timeout_s = human_node.clamp_timeout(params.get("timeout_s"), human_node.DEFAULT_TIMEOUT_S)
        abort = _abort_predicate(context)
        if abort is not None and abort():
            return SkillResult(skill_name=name, success=False,
                               error="aborted by operator — AskOperator not started")
        try:
            ans = human_node.ask_blocking(prompt, check_abort=abort, timeout_s=timeout_s)
        except prompts.TooManyOpenPrompts as exc:    # not a mistake in the prompt
            return SkillResult(skill_name=name, success=False, error=str(exc))
        except prompts.PromptError as exc:           # e.g. an attest naming an undefined slot
            return SkillResult(skill_name=name, success=False, error=f"invalid prompt: {exc}")
        except prompts.PromptAborted:
            return SkillResult(
                skill_name=name, success=False,
                error=tr("被中止：提示已撤回，没有等到答复", "aborted: the prompt was withdrawn, no answer was awaited"),
                data={"prompt_id": prompt.id, "status": "cancelled"})
        data = {"prompt_id": prompt.id, "status": ans.status, "ok": ans.ok, "choice": ans.choice,
                "value": ans.value, "note": ans.note, "answered_by": ans.answered_by,
                "answered_at": ans.answered_at, "source": ans.source}
        if prompt.kind == "attest" and ans.status == "answered":
            # the value each slot now holds: an attest answered False is an answer, not a sign-off
            data["attested"] = {slot: ans.value for slot in prompt.attest_slots}
        if ans.status == "answered":
            said = (f"choice {ans.choice!r}" if ans.choice is not None else "")
            if ans.value not in (None, "") and ans.value != ans.choice:
                said += (", " if said else "") + f"value {ans.value!r}"
            summary = tr(f"操作员（{ans.answered_by}）已答复：{said or '已确认'}",
                         f"The operator ({ans.answered_by}) answered: {said or 'acknowledged'}")
            if prompt.kind == "attest":
                recorded = ", ".join(f"{slot} = {ans.value!r}" for slot in prompt.attest_slots)
                summary = tr(f"操作员（{ans.answered_by}）记下了：{recorded}",
                             f"The operator ({ans.answered_by}) recorded: {recorded}")
                if ans.value is False:
                    summary += tr(
                        " —— 操作员记成了 False（没有确认 / 不安全）：这不是批准，不要当成已签署。",
                        " - the operator recorded False (NOT confirmed / not safe): this is not an "
                        "approval, do not treat it as signed off.")
            return SkillResult(skill_name=name, success=True, data=data, summary=summary)
        if ans.status == "timeout":
            error = tr(f"操作员在 {timeout_s:g} 秒内没有答复（提示 {prompt.id} 已按超时关闭）",
                       f"The operator did not answer within {timeout_s:g} s (prompt {prompt.id} was closed as "
                       "timed out)")
        elif ans.status == "dismissed":
            error = tr(f"操作员关闭了提示而没有回答（提示 {prompt.id}）" + (f"：{ans.note}" if ans.note else ""),
                       f"The operator dismissed the prompt without answering (prompt {prompt.id})"
                       + (f": {ans.note}" if ans.note else ""))
        else:
            error = tr(f"提示被取消（提示 {prompt.id}）" + (f"：{ans.note}" if ans.note else ""),
                       f"The prompt was cancelled (prompt {prompt.id})" + (f": {ans.note}" if ans.note else ""))
        return SkillResult(skill_name=name, success=False, error=error, data=data)


class ReadOpticalState(BaseSkill):
    """Read the manual optical state — what an operator registered about the beam path."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ReadOpticalState",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr(
                "读手动光学状态：软件读不到、只有人知道的那些量（装的是哪片 λ/2、Analyzer 座里的片、手动偏振角、"
                "物镜、滤光片、NPI 泵浦电流 %、激光区安全签署）。返回槽位定义 slots、当前登记 values"
                "（谁、何时、从哪里登记，何时失效，是否仍有效）以及 unset / expired 清单。只读：这个状态只有"
                "操作员能写（网页或启动器），没有技能能改它；每条谱的 sidecar 自动带一份快照。",
                "Read the manual optical state: the quantities no software can read back and only a person knows "
                "(which λ/2 is installed, the optic in the analyzer wheel, the manual polarizer angle, the "
                "objective, the edge filter, the NPI pump-current %, the laser-area-safe attestation). Returns "
                "the slot definitions (slots), what is registered (values: who, when, from where, when it "
                "expires, whether it is still valid) and the unset / expired lists. Read-only: only the "
                "operator can write this state (web UI or launcher), no skill can change it; every spectrum "
                "sidecar carries a snapshot automatically."),
            parameters=[
                ParameterSpec(name="slot", type="str", required=False, default="",
                              description=tr("只看这一个槽位的 id；不给 = 全部",
                                             "Only this slot (its id); not given = all")),
            ],
            estimated_duration_s=0.1,
            composition_level=0,
            tags=[*_TAGS, "read", "status"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "ReadOpticalState"
        store = optical_state.get_store()
        want = str(params.get("slot") or "").strip()
        try:
            slots = [store.slot(want)] if want else store.slots()
        except optical_state.OpticalStateError as exc:
            return SkillResult(
                skill_name=name, success=False,
                error=f"{exc}; defined slots: {[s.id for s in store.slots()]}")
        snapshot = store.snapshot()
        values = {s.id: snapshot.get(s.id, {"value": None, "valid": False}) for s in slots}
        unset = [sid for sid, v in values.items() if v.get("value") is None]
        expired = [sid for sid, v in values.items() if v.get("value") is not None and not v.get("valid")]
        valid = [sid for sid, v in values.items() if v.get("valid")]
        bits = [tr(f"{len(valid)}/{len(values)} 个槽位有效", f"{len(valid)}/{len(values)} slots valid")]
        if unset:
            bits.append(tr("未登记：", "unset: ") + ", ".join(unset))
        if expired:
            bits.append(tr("已过期：", "expired: ") + ", ".join(expired))
        return SkillResult(
            skill_name=name, success=True,
            data={"slots": [s.to_dict() for s in slots], "values": values, "valid": valid,
                  "unset": unset, "expired": expired},
            summary="; ".join(bits))
