"""Pydantic models of the optical-layout API (``/api/optical-layouts*``).

The document itself (``Layout``) is the model in ``mast.optical_layout.model``; what is here is
the envelope around it: the request bodies of the write endpoints (a mandatory JSON object, unknown
fields refused, ``by`` the operator's name as everywhere in the operator-facing API) and the
responses (the layout with its ray trace and the state of its links; listings; comments;
history).

Provenance of a write is not in the body: the server records what it saw of the request
(``routes/operator.py`` ``client_record``) next to the operator's name, in the history of the layout
and with a comment.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, BeforeValidator, Field

from mast.api.schemas_operator import _OperatorBody
from mast.optical_layout.model import ID_PATTERN, Layout


def _strip(v: Any) -> Any:
    return v.strip() if isinstance(v, str) else v


# ── requests ───────────────────────────────────────────────────────────────────────────────────

class LayoutWriteBody(_OperatorBody):
    """Save a layout. ``base_revision`` is the revision the client started from (0 for a layout
    that does not exist yet); anything else than the stored one is a 409."""

    layout: Layout
    base_revision: int = Field(..., ge=0, description="the revision the edit started from; 0 = a new layout")


class LayoutArchiveBody(_OperatorBody):
    base_revision: int = Field(..., ge=0, description="the revision the client last saw")


class CommentBody(_OperatorBody):
    text: Annotated[str, BeforeValidator(_strip), Field(min_length=1, max_length=2000)]
    component_id: Optional[Annotated[str, Field(pattern=ID_PATTERN)]] = Field(
        None, description="the component the comment is about (must be in the layout)")


class ResolveBody(_OperatorBody):
    """Resolve a comment. Only a person does this (an external agent cannot)."""


# ── responses ──────────────────────────────────────────────────────────────────────────────────

class LayoutRowOut(BaseModel):
    id: str
    title: str = ""
    revision: int = 0
    updated_at: str = ""
    updated_by: str = ""
    components: int = Field(0, description="number of components")
    open_comments: int = 0
    error: Optional[str] = Field(None, description="set when the file cannot be read; the other "
                                                   "fields are then empty")


class LayoutListResponse(BaseModel):
    layouts: list[LayoutRowOut] = Field(default_factory=list)


class TraceSegment(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class TraceEvent(BaseModel):
    component: str
    action: Literal["reflect", "transmit", "split", "stop", "blocked", "back_hit"]
    x: float
    y: float
    offset_mm: float
    incidence_deg: float
    path_mm: float


class TraceBranch(BaseModel):
    branch: str
    fraction: float
    segments: list[TraceSegment]
    events: list[TraceEvent]
    end: Literal["stop", "blocked", "back_hit", "split", "left_table", "max_events", "budget"]


class TraceTree(BaseModel):
    source: str
    wavelength_nm: Optional[float] = None
    branches: list[TraceBranch]


class LayoutViewResponse(BaseModel):
    """A layout as it is stored, with its ray trace and what its links say now."""

    layout: Layout
    trace: list[TraceTree]
    links_state: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="component id -> {device?, slot?}: the current state behind each link; only "
                    "components with a link appear")


class RevisionRowOut(BaseModel):
    revision: int
    updated_at: str = ""
    updated_by: str = ""
    title: str = ""
    components: int = 0


class HistoryResponse(BaseModel):
    revisions: list[RevisionRowOut] = Field(default_factory=list, description="newest first")


class RevisionResponse(BaseModel):
    revision: int
    updated_at: str = ""
    updated_by: str = ""
    layout: Layout
    trace: list[TraceTree]


class ArchiveResponse(BaseModel):
    id: str
    archived: bool = True


class ConflictResponse(BaseModel):
    """The body of a 409: a stale ``base_revision``."""

    detail: str
    current_revision: int = Field(description="the revision that is stored now (0 = the layout does not exist)")
    updated_by: str = ""
    updated_at: str = ""


class CommentOut(BaseModel):
    id: str
    author: Literal["operator", "agent"]
    by: str
    text: str
    component_id: Optional[str] = None
    created_at: str
    resolved: bool = False
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None


class CommentsResponse(BaseModel):
    comments: list[CommentOut] = Field(default_factory=list, description="oldest first")
    open_count: int = 0


__all__ = [
    "ArchiveResponse", "CommentBody", "CommentOut", "CommentsResponse", "ConflictResponse",
    "HistoryResponse",
    "LayoutArchiveBody", "LayoutListResponse", "LayoutRowOut", "LayoutViewResponse",
    "LayoutWriteBody", "ResolveBody", "RevisionResponse", "RevisionRowOut", "TraceBranch",
    "TraceEvent", "TraceSegment", "TraceTree",
]
