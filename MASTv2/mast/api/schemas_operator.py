"""Pydantic models of the operator API (``/api/operator/*``).

The operator API is for **people** — the web UI and the launcher popup. It lets an operator
answer structured prompts and write the manual optical state. It is deliberately not part of
``/api/ext/v1`` and not reachable from MCP: an agent must never attest a physical event it
cannot see (see ``docs/v2/design/optical_station_witec.md``).

Request bodies are mandatory JSON objects with unknown fields refused. ``by`` is the
operator's name: it goes into the answer, the optical-state record and its history.

Provenance: ``source`` ("web" / "launcher") is what the client *says*. Every write also stores
``client`` — what the server *saw* of the request (``routes/operator.py`` ``client_record``):
the peer address, the client identification, whether a web page sent it, how it authenticated.
A client cannot supply or change it (a ``client`` field in a body is refused).
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mast.operator_hitl.optical_state import MAX_SLOT_NOTE_CHARS

#: where a write came from (recorded with it)
OperatorSource = Literal["web", "launcher"]


def _name(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError("by (the operator's name) must not be empty")
    return value


class _OperatorBody(BaseModel):
    """Common to every write: a mandatory JSON object, unknown fields refused, and ``by`` —
    the operator's name — present and not blank."""

    model_config = ConfigDict(extra="forbid")

    by: str = Field(..., min_length=1, max_length=80, description="the operator's name")

    @field_validator("by")
    @classmethod
    def _by_not_blank(cls, v: str) -> str:
        return _name(v)


class ClientOut(BaseModel):
    """What the server saw of the request behind a write: the record an audit goes by.
    Credentials are never recorded, only which kind of credential came with the request."""

    peer: Optional[str] = Field(None, description="the address of the TCP connection, never read "
                                                  "from a header; null when the server had none it "
                                                  "could trust")
    user_agent: Optional[str] = Field(None, description="the User-Agent header, cut to 200 "
                                                        "characters (the client's own claim)")
    browser: bool = Field(False, description="the request carried an Origin or a Sec-Fetch-* header, "
                                             "i.e. a web page sent it")
    origin: Optional[str] = Field(None, description="the Origin header, cut to 120 characters")
    sec_fetch_site: Optional[str] = None
    sec_fetch_mode: Optional[str] = None
    referer: bool = Field(False, description="whether a Referer header was present (its value is "
                                             "not recorded)")
    auth: str = Field("none", description="basic | session_cookie | none: the credential the "
                                          "request carried (not the credential itself)")
    auth_enforced: bool = Field(False, description="whether the service requires LAN credentials "
                                                   "at all; only when true was `auth` verified")
    host: Optional[str] = Field(None, description="the Host header, cut to 120 characters")
    claimed_forwarding: Optional[dict[str, str]] = Field(
        None, description="the forwarding headers the client sent (X-Forwarded-For, Forwarded, ...), "
                          "verbatim and cut to 200 characters: what it claims, recorded next to the "
                          "peer and never used instead of it; null when there were none")


# ── prompts ────────────────────────────────────────────────────────────────────

class PromptOptionOut(BaseModel):
    id: str
    label: str


class AnswerOut(BaseModel):
    """An operator's answer — or the way a prompt ended without one (``status`` is then
    ``cancelled`` / ``timeout`` / ``dismissed``, ``source`` is ``system`` or the dismisser's)."""

    prompt_id: str
    status: str
    choice: Optional[str] = None
    value: Any = None
    note: str = ""
    answered_by: str = ""
    source: str = Field("", description="self-reported by the client (web / launcher), or set by "
                                        "MAST itself (system / test)")
    answered_at: str = ""
    client: Optional[ClientOut] = Field(
        None, description="what the server saw of the request that gave this answer; null for an "
                          "answer that came from MAST itself (a timeout, a cancellation)")


class PromptOut(BaseModel):
    """One operator prompt. ``answer`` is ``null`` while the prompt is open."""

    id: str
    kind: str = Field(description="confirm | choice | number | text | attest")
    title: str
    message: str = ""
    options: list[PromptOptionOut] = Field(default_factory=list)
    number_min: Optional[float] = None
    number_max: Optional[float] = None
    unit: str = ""
    attest_slots: list[str] = Field(
        default_factory=list, description="attest: the optical-state slots this answer sets")
    severity: str = Field("action", description="info | action | safety")
    job_id: Optional[str] = None
    skill: Optional[str] = None
    agent_id: Optional[str] = None
    timeout_s: Optional[float] = None
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    status: str = Field("open", description="open | answered | cancelled | timeout | dismissed")
    answer: Optional[AnswerOut] = None


class PromptsResponse(BaseModel):
    seq: int = Field(description="changes on every post / answer / close; compare it for "
                                 "(in)equality to skip a refetch. Monotonic across restarts "
                                 "when persistent.")
    prompts: list[PromptOut] = Field(default_factory=list)
    open_count: int = 0
    persistent: bool = Field(False, description="false: prompts live in memory only")
    persist_error: Optional[str] = Field(
        None, description="the last error saving prompts.json, if the latest save failed")


class AnswerBody(_OperatorBody):
    """Answer a prompt: ``choice`` for confirm (``yes`` / ``no``), choice and attest-with-options
    prompts (an option id); ``value`` for number, text and plain attest prompts."""

    choice: Optional[str] = Field(None, max_length=200)
    value: Any = None
    note: str = Field("", max_length=2000)
    source: OperatorSource = Field(
        "web", description="where the client says the request came from. Self-reported: an audit "
                           "goes by `client`, which the server fills in from the request itself")


class DismissBody(_OperatorBody):
    """Close a prompt without answering it (nothing is attested)."""

    reason: str = Field("", max_length=2000)
    source: OperatorSource = Field(
        "web", description="where the client says the request came from. Self-reported: an audit "
                           "goes by `client`, which the server fills in from the request itself")


# ── optical state ──────────────────────────────────────────────────────────────

class SlotOut(BaseModel):
    """A slot definition: what can be registered, not its current value."""

    id: str
    label: str
    kind: str = Field(description="choice | number | text | flag")
    options: list[dict[str, Any]] = Field(
        default_factory=list, description="choice slots: [{id, label, meta}]; meta may hold valid_nm")
    unit: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    valid_for_s: Optional[float] = Field(None, description="null = valid until changed")
    description: str = ""


class OpticalStateResponse(BaseModel):
    seq: int = Field(description="changes on every slot write")
    slots: list[SlotOut] = Field(default_factory=list)
    values: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="slot id -> {value, set_by, set_at, source, valid_until, valid}; an unset "
                    "slot is {value: null, valid: false}")
    persistent: bool = False


class SlotValueOut(BaseModel):
    value: Any = None
    set_by: str
    set_at: str
    source: str = Field(..., description="self-reported by the client (web / launcher / "
                                         "prompt:<id>); the audit goes by `client`")
    valid_until: Optional[str] = None
    note: str = ""
    client: Optional[ClientOut] = Field(None, description="what the server saw of the request "
                                                          "that wrote this value")


class SetSlotBody(_OperatorBody):
    """Register what is physically in the beam path. Only an operator may do this."""

    value: Any = Field(..., description="a choice option id, a number, text, or true/false")
    note: str = Field("", max_length=MAX_SLOT_NOTE_CHARS)
    source: OperatorSource = Field(
        "web", description="where the client says the request came from. Self-reported: an audit "
                           "goes by `client`, which the server fills in from the request itself")

    @field_validator("value")
    @classmethod
    def _value_present(cls, v: Any) -> Any:
        if v is None:
            raise ValueError("value must not be null (use the clear endpoint to forget a slot)")
        return v


class ClearSlotBody(_OperatorBody):
    source: OperatorSource = Field(
        "web", description="where the client says the request came from. Self-reported: an audit "
                           "goes by `client`, which the server fills in from the request itself")


class ClearSlotResponse(BaseModel):
    slot: str
    cleared: bool = True
    seq: int = 0


class OpticalHistoryResponse(BaseModel):
    records: list[dict[str, Any]] = Field(
        default_factory=list,
        description="oldest first; each {slot, value, set_by, set_at, source, valid_until, note, "
                    "client}; `client` is what the server saw of the writing request (null for "
                    "records written before this was recorded, and for MAST's own voiding lines)")


__all__ = [
    "AnswerBody", "AnswerOut", "ClearSlotBody", "ClearSlotResponse", "ClientOut", "DismissBody",
    "OpticalHistoryResponse", "OpticalStateResponse", "OperatorSource", "PromptOptionOut",
    "PromptOut", "PromptsResponse", "SetSlotBody", "SlotOut", "SlotValueOut",
]
