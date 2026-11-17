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


def _stokes() -> Any:
    """``[S1/S0, S2/S0, S3/S0]``: the normalised Stokes vector (contract v2 §4.1-§4.2)."""
    return Field(min_length=3, max_length=3,
                 description="[S1/S0, S2/S0, S3/S0], the normalised Stokes vector in the beam's own frame "
                             "(e_h = z x d in the table plane, left of the beam; e_v = z, up; S1 = I_h - I_v, "
                             "S2 = I_+45 - I_-45, angles from e_h towards e_v); null when no light (S0 = 0)")


class SegmentBeam(BaseModel):
    """The beam at the start of a segment (contract v2 §4.7)."""

    p: float = Field(description="power relative to the tree's source (S0; 1 at the source)")
    s: Optional[list[float]] = _stokes()
    q: Optional[list[float]] = Field(min_length=2, max_length=2,
                                     description="[z, zR] in mm: the complex beam parameter q = z + i zR of the "
                                                 "Gaussian beam (z > 0 past the waist); null without one")


class EventBeam(BaseModel):
    """The beam as it arrives at a component, before the component acts (contract v2 §4.7)."""

    p: float = Field(description="power relative to the tree's source")
    mw: Optional[float] = Field(description="absolute power in mW (the tree's source_mw times p); null when "
                                            "the source's power is not known")
    d_mm: Optional[float] = Field(description="1/e^2 diameter of the Gaussian beam here, mm; null without one")
    s: Optional[list[float]] = _stokes()


class TreeBeam(BaseModel):
    """What the beam of a whole tree has in common (contract v2 §4.7)."""

    source_mw: Optional[float] = Field(description="the source's power in mW (every wavelength of a source has "
                                                   "all of power_mw; a fibre continuation what reached its "
                                                   "coupler times the coupling); null when not known")
    m2: Optional[float] = Field(description="beam quality factor M^2 of the tree; null when it has no Gaussian beam")


class TracePass(BaseModel):
    """A component a segment passes over or under (contract 3D §3.5): the ray meets its working surface in the
    plane, inside the aperture, but more than half the aperture above or below its axis, and goes on."""

    component: str
    x: float = Field(description="where the ray passes it, in the plane, mm")
    y: float
    dz_mm: float = Field(description="height of the beam minus that of the component's axis (a periscope: of its "
                                     "entrance for this direction), mm; above 0 the beam passes over it")


class TraceSegment(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float
    z_mm: float = Field(description="height of the beam along the segment above the table top, mm (contract 3D §4)")
    beam: SegmentBeam
    passes: list[TracePass] = Field(description="the components the segment passes over or under, in the order "
                                                "along it (contract 3D §3.5)")


class TraceEvent(BaseModel):
    component: str
    action: Literal["reflect", "transmit", "split", "stop", "blocked", "back_hit"]
    x: float
    y: float
    z_mm: float = Field(description="height of the beam arriving here, mm (contract 3D §4)")
    offset_mm: float
    dz_mm: float = Field(description="height of the beam minus that of the component's axis (a periscope: of its "
                                     "entrance for this direction), mm; above 0 the beam is above the axis")
    incidence_deg: float
    path_mm: float = Field(description="path from the source, mm, with the vertical runs inside periscopes")
    beam: EventBeam


class TraceBranch(BaseModel):
    branch: str
    fraction: float
    segments: list[TraceSegment]
    events: list[TraceEvent]
    end: Literal["stop", "blocked", "back_hit", "split", "left_table", "max_events", "budget"]


class TraceTree(BaseModel):
    source: str
    wavelength_nm: Optional[float] = None
    beam: TreeBeam
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
    "EventBeam", "HistoryResponse",
    "LayoutArchiveBody", "LayoutListResponse", "LayoutRowOut", "LayoutViewResponse",
    "LayoutWriteBody", "ResolveBody", "RevisionResponse", "RevisionRowOut", "SegmentBeam", "TraceBranch",
    "TraceEvent", "TracePass", "TraceSegment", "TraceTree", "TreeBeam",
]
