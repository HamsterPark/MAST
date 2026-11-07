"""外部面的请求体模型。响应体是普通 JSON（字段在各端点的 docstring 与设计稿第 3 节）。"""

from __future__ import annotations

from mast.i18n import tr
import json
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


#: 一次作业参数序列化后的上限（字节）。技能参数是几个数与短字符串；更大的东西不该塞进参数
#: （它会原样进 journal、作业视图与实验记录）。
PARAMS_MAX_BYTES = 64_000


class JobSubmit(BaseModel):
    skill: str = Field(..., min_length=1, max_length=120, description="技能名（注册表里的名字）")
    params: dict[str, Any] = Field(default_factory=dict,
                                   description="技能参数；带量纲的可写 '5n' 这类带 SI 前缀的字符串")
    request_id: str | None = Field(None, max_length=128,
                                   description="幂等键（同一调用方内）：同 id 同内容 ⇒ 回原作业")
    note: str = Field("", max_length=500, description="给人看的备注，进作业视图")

    @field_validator("params")
    @classmethod
    def _params_are_small(cls, v: dict[str, Any]) -> dict[str, Any]:
        size = len(json.dumps(v, ensure_ascii=False, default=str).encode("utf-8"))
        if size > PARAMS_MAX_BYTES:
            raise ValueError(tr(f"params 序列化后 {size} 字节，超过上限 {PARAMS_MAX_BYTES}", f"params is {size} bytes when serialised, over the limit of {PARAMS_MAX_BYTES}"))
        return v


class CancelBody(BaseModel):
    reason: str = Field("", max_length=300)


class EstopBody(BaseModel):
    reason: str = Field("", max_length=300)


class ScopeRef(BaseModel):
    id: str | None = Field(None, max_length=120, description="已有实验/样品的 id（切换）")
    name: str | None = Field(None, max_length=200, description="名字（按名字新建或复用）")
    goal: str = Field("", max_length=2000)
    description: str = Field("", max_length=2000)
    sample_type: str = Field("", max_length=120)


class ScopeBody(BaseModel):
    experiment: ScopeRef | None = None
    sample: ScopeRef | None = None
    force: bool = Field(False, description="仪器正被占用时仍然切换（在跑的动作会记到新作用域下）")
    reason: str = Field("", max_length=300)


class NoteBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=20000)
    kind: str = Field("note", max_length=40,
                      description="note | insight | summary | hypothesis | protocol")
    tags: list[str] = Field(default_factory=list)
    scope: Literal["experiment", "global"] = "experiment"


class RequestOption(BaseModel):
    """One answer the operator can pick in a structured request."""

    id: str = Field(..., min_length=1, max_length=64,
                    description="returned verbatim as the answer's choice")
    label: str = Field(..., min_length=1, max_length=200, description="what the operator reads")


#: the fields only a structured prompt has (``RequestBody``)
STRUCTURED_FIELDS = ("title", "options", "number_min", "number_max", "unit", "attest_slots",
                     "severity", "timeout_s")


def _optical_station() -> bool:
    """``mast.station``: this process serves the optical station. Never true on an STM station,
    and unreadable = STM."""
    try:
        from mast import station

        return bool(station.is_optical())
    except Exception:  # noqa: BLE001
        return False


class RequestBody(BaseModel):
    """A question or request for the operator.

    Free text (``kind`` question | action | info, or any other word up to 40 characters) goes
    to the operator-request list exactly as before. On the **optical station** a ``kind`` that
    names a structured prompt (``confirm`` | ``choice`` | ``number`` | ``text`` | ``attest``)
    makes an operator prompt with the fields below; only the operator can answer it. The
    structured fields are refused (422) when ``kind`` is not a structured one.

    On an **STM station** there are no structured prompts: every request is a free-text
    request, whatever its ``kind`` (the text is stored as given) and the structured fields are
    ignored, as an unknown field always was.
    """

    @model_validator(mode="before")
    @classmethod
    def _stm_ignores_the_structured_fields(cls, data: Any) -> Any:
        """On an STM station the body is what it was before structured prompts existed: only
        ``message`` and ``kind`` count, and a structured field is dropped *before* it is
        validated, so even a malformed one is ignored without an error."""
        if isinstance(data, dict) and not _optical_station():
            return {k: v for k, v in data.items() if k not in STRUCTURED_FIELDS}
        return data

    message: str = Field(..., min_length=1, max_length=4000)
    kind: str = Field("question", max_length=40,
                      description="question | action | info (free text), or - on the optical "
                                  "station only - a structured prompt: confirm | choice | number | "
                                  "text | attest. On an STM station every kind is a text request")
    title: str | None = Field(None, max_length=200,
                              description="structured: short heading (default: the first line of message)")
    options: list[RequestOption] | None = Field(
        None, max_length=24,
        description="choice, attest: the answers to pick from (choice needs 2 or more). An attest's "
                    "options must be the slot's own and are shown under the slot's labels; a yes/no "
                    "slot takes none")
    number_min: float | None = Field(None, allow_inf_nan=False,
                                     description="number, attest with a unit: lowest accepted value")
    number_max: float | None = Field(None, allow_inf_nan=False,
                                     description="number, attest with a unit: highest accepted value")
    unit: str | None = Field(None, max_length=20, description="number: the unit shown next to the field")
    attest_slots: list[str] | None = Field(
        None, max_length=20,
        description="attest: ids of the optical-state slots the operator's answer sets "
                    "(required for attest)")
    severity: str | None = Field(None, max_length=20, description="info | action (default) | safety")
    timeout_s: float | None = Field(None, gt=0, le=604800, allow_inf_nan=False,
                                    description="structured: seconds until an unanswered prompt "
                                                "expires (at most 7 days); default: 24 hours")


class HandoverBody(BaseModel):
    summary: str = Field(..., min_length=1, max_length=20000)
    next_steps: Union[str, list[str]] = Field(default_factory=list)
    title: str = Field("", max_length=200)
    since: str | None = Field(None, max_length=40,
                              description="ISO 时间；只汇总此后的作业与动作（缺省 = 本进程里的全部）")


class LayoutCommentBody(BaseModel):
    """A comment on an optical layout. The author is the caller (``X-MAST-Actor``), never a field of
    the body; unknown fields are refused, so there is nothing to smuggle a name, a resolution or
    an edit in."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=2000, description="the comment")
    component_id: str | None = Field(None, max_length=40, pattern=r"^[A-Za-z0-9_-]{1,40}$",
                                     description="the component it is about (must be in the layout)")


class ProposalBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    code: str = Field(..., min_length=1, max_length=200_000)
    rationale: str = Field(..., min_length=1, max_length=4000)


class CompositeBody(BaseModel):
    spec: Union[dict[str, Any], str] = Field(..., description="CompositeSpec JSON 对象；'?' 返回格式说明")
    base_version: int = Field(-1, description="覆盖已存在的同名技能时必须给（乐观锁）")


__all__ = ["CancelBody", "CompositeBody", "EstopBody", "HandoverBody", "JobSubmit",
           "LayoutCommentBody", "NoteBody", "ProposalBody", "RequestBody", "RequestOption", "ScopeBody",
           "ScopeRef"]
