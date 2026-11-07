"""Optical layouts for **people**: the web editor's endpoints.

* ``GET  /api/optical-layouts``                               — every layout (id, title, revision, ...)
* ``GET  /api/optical-layouts/{id}``                          — the layout + its ray trace + the state of its links
* ``PUT  /api/optical-layouts/{id}``                          — save (``{layout, base_revision, by}``; 409 if stale)
* ``POST /api/optical-layouts/{id}/delete``                   — archive (``{by, base_revision}``; never a real delete)
* ``GET  /api/optical-layouts/{id}/history?limit=``           — the saved revisions, newest first (at most 200 kept)
* ``GET  /api/optical-layouts/{id}/revisions/{rev}``          — one of them, with its trace
* ``GET  /api/optical-layouts/{id}/render.png?width=&fit=``   — a PNG (matplotlib Agg, no display needed)
* ``GET  /api/optical-layouts/{id}/comments``                 — the discussion
* ``POST /api/optical-layouts/{id}/comments``                 — add a comment (``{by, text, component_id?}``)
* ``POST /api/optical-layouts/{id}/comments/{cid}/resolve``   — resolve it (``{by}``)

The drawing describes the *physical* bench; what is on it is the operator's word. An external agent
reads it and may comment (``api/ext/optical_layout.py``) but cannot change the geometry or the
annotations and cannot resolve a comment: those writes exist only here.
``tests/v2/unit/optical_layout/test_layout_ext_readonly.py`` keeps it that way.

Write requests are guarded exactly like ``/api/operator``'s (``routes/operator.py``): the access
gate first, then :func:`mast.api.routes.operator.write_guard` - ``Content-Type: application/json`` and a
body (415), ``Sec-Fetch-Site`` same-origin, ``Origin`` equal to the service, no foreign Host name (403)
- and the body is mandatory with unknown fields refused (422). A write body must say how long it is
(``Content-Length``: a chunked one is a 411) and be at most 2 MB (413); both are decided before the body
is read (``mast/api/body_limit.py``). Every write records ``client_record``: what the server saw of the
request, in the layout's history and with a comment, and one log line.

Reads never open a device: ``links_state`` reads the registry's configuration, the devices the
process already has open, and the optical-state snapshot (``optical_layout/links.py``).
"""
from __future__ import annotations

import contextlib
import json
import logging
from typing import Any, Iterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from mast.api.body_limit import sized_route_class
from mast.api.routes.operator import client_record, write_guard
from mast.api.schemas_optical_layout import (
    ArchiveResponse,
    CommentBody,
    CommentOut,
    CommentsResponse,
    ConflictResponse,
    HistoryResponse,
    LayoutArchiveBody,
    LayoutListResponse,
    LayoutViewResponse,
    LayoutWriteBody,
    ResolveBody,
    RevisionResponse,
)
from mast.i18n import tr
from mast.optical_layout import store as layout_store
from mast.optical_layout.links import links_state
from mast.optical_layout.model import Layout
from mast.optical_layout.trace import trace_layout

logger = logging.getLogger(__name__)

#: a write body above this is refused (413) without being read (the document itself is bounded at
#: 1 MB; the rest is the envelope)
MAX_REQUEST_BYTES = 2_000_000


def _refuse_body(status: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


# A body must declare its length (411 when it does not, e.g. chunked) and not be too long (413). The
# check runs in front of the handler, before FastAPI reads the body: a dependency would run after the
# whole body is in memory (``mast/api/body_limit.py``).
router = APIRouter(tags=["optical-layout"], route_class=sized_route_class(MAX_REQUEST_BYTES, _refuse_body))

_WRITE = [Depends(write_guard)]


class _Audit:
    """Lets a handler that *returns* a refusal (a 409 with a body) say so for the log line."""

    outcome: str | None = None


@contextlib.contextmanager
def _audited(action: str, target: str, by: str, client: dict[str, Any]) -> Iterator[_Audit]:
    """One log line per write that reaches a handler: action, layout id, the operator's name, the
    outcome and the ``client`` record (values JSON-encoded: a header cannot start a fake line)."""
    audit = _Audit()
    outcome = "error"
    try:
        yield audit
        outcome = audit.outcome or "ok"
    except HTTPException as exc:
        outcome = f"refused_{exc.status_code}" if exc.status_code < 500 else f"failed_{exc.status_code}"
        raise
    finally:
        level = logging.INFO if outcome == "ok" or outcome.startswith("refused_") else logging.WARNING
        logger.log(level, "optical layout write: action=%s target=%s by=%s outcome=%s client=%s", action,
                   json.dumps(target, ensure_ascii=False), json.dumps(by, ensure_ascii=False), outcome,
                   json.dumps(client, ensure_ascii=False, sort_keys=True))


def _conflict(exc: layout_store.Conflict) -> JSONResponse:
    """409 with what the editor needs to offer "load the latest": the stored revision and who saved it."""
    return JSONResponse(status_code=409, content={
        "detail": str(exc), "current_revision": exc.current_revision,
        "updated_by": exc.updated_by, "updated_at": exc.updated_at})


def _http(exc: layout_store.StoreError) -> HTTPException:
    """The HTTP answer for what the store refused."""
    if isinstance(exc, layout_store.NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, layout_store.Conflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, layout_store.Invalid):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, layout_store.LimitReached):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, layout_store.Corrupt):
        return HTTPException(status_code=500, detail=str(exc))
    if isinstance(exc, layout_store.SaveFailed):
        logger.error("optical layout: could not save: %s", exc.cause, exc_info=exc.cause)
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _view(lay: Layout) -> dict[str, Any]:
    return {"layout": lay.to_dict(), "trace": trace_layout(lay), "links_state": links_state(lay)}


# ── reading ────────────────────────────────────────────────────────────────────────────────────

@router.get("/optical-layouts", response_model=LayoutListResponse)
def list_layouts() -> dict:
    """Every layout, most recently saved first. A file that cannot be read is listed with an
    ``error``."""
    return {"layouts": layout_store.get_store().list_layouts()}


@router.get("/optical-layouts/{layout_id}", response_model=LayoutViewResponse)
def get_layout(layout_id: str) -> dict:
    """The layout (as stored: ``layout``), its ray trace (``trace``, the contract's section 4) and
    ``links_state`` (the state behind each device / slot link: read from the registry's
    configuration and the devices already open, never by opening one). 404 unknown, 500 the file
    cannot be read (it is left untouched)."""
    try:
        return _view(layout_store.get_store().load(layout_id))
    except layout_store.StoreError as exc:
        raise _http(exc) from None


@router.get("/optical-layouts/{layout_id}/history", response_model=HistoryResponse)
def get_history(layout_id: str, limit: int = Query(50, ge=1, le=layout_store.MAX_HISTORY)) -> dict:
    """The saved revisions, newest first (the newest 200 are kept)."""
    try:
        return {"revisions": layout_store.get_store().history(layout_id, limit)}
    except layout_store.StoreError as exc:
        raise _http(exc) from None


@router.get("/optical-layouts/{layout_id}/revisions/{revision}", response_model=RevisionResponse)
def get_revision(layout_id: str, revision: int) -> dict:
    """One saved revision, with its trace. To go back to it, save its ``layout`` as a new revision."""
    store = layout_store.get_store()
    try:
        lay, saved_at, saved_by = store.revision_record(layout_id, revision)
    except layout_store.StoreError as exc:
        raise _http(exc) from None
    return {"revision": revision, "updated_at": saved_at, "updated_by": saved_by,
            "layout": lay.to_dict(), "trace": trace_layout(lay)}


@router.get("/optical-layouts/{layout_id}/render.png",
            response_class=Response, responses={200: {"content": {"image/png": {}}, "description": "the table as a PNG"}})
def get_render(layout_id: str, width: int = Query(1200, ge=200, le=3000, description="pixels wide"),
               fit: Literal["table", "content"] = Query("table", description="the whole table, or zoomed to the work")) -> Response:
    """A PNG of the layout: components as simple shapes, beams coloured by wavelength, marks where a
    beam hits far off center, on a back side or just misses. Needs no display."""
    from mast.optical_layout.render import render_png

    try:
        lay = layout_store.get_store().load(layout_id)
    except layout_store.StoreError as exc:
        raise _http(exc) from None
    try:
        png = render_png(lay, width_px=width, fit=fit)
    except Exception as exc:  # noqa: BLE001
        logger.exception("optical layout: render of %s failed", layout_id)
        raise HTTPException(status_code=500, detail=tr(f"画图失败（{type(exc).__name__}: {exc}）",
                                                       f"Rendering failed ({type(exc).__name__}: {exc})")) from None
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/optical-layouts/{layout_id}/comments", response_model=CommentsResponse)
def get_comments(layout_id: str, status: Literal["all", "open"] = Query("all", description="all | open")) -> dict:
    """The comments, oldest first. ``author`` says whether the operator or an agent wrote one."""
    store = layout_store.get_store()
    try:
        rows = store.comments(layout_id)
    except layout_store.StoreError as exc:
        raise _http(exc) from None
    open_count = sum(1 for c in rows if not c.get("resolved"))
    shown = [c for c in rows if status == "all" or not c.get("resolved")]
    return {"comments": [layout_store.public_comment(c) for c in shown], "open_count": open_count}


# ── writing ────────────────────────────────────────────────────────────────────────────────────

@router.put("/optical-layouts/{layout_id}", response_model=LayoutViewResponse, dependencies=_WRITE,
            responses={409: {"model": ConflictResponse, "description": "base_revision is not the stored revision"}})
def put_layout(layout_id: str, body: LayoutWriteBody, request: Request) -> Any:
    """Save a layout as the next revision. ``layout.id`` must be the id in the path. 409 when
    ``base_revision`` is not the stored revision (someone saved in between: load the latest, or
    save a copy under another id; the body says which revision is stored and who saved it); a new
    layout is saved with ``base_revision`` 0. 422 the layout is not valid, 500 the stored file
    cannot be read (nothing is overwritten), 503 the disk refused (nothing changed).
    ``revision``, ``updated_at`` and ``updated_by`` are written by the server."""
    client = client_record(request)
    with _audited("save", layout_id, body.by, client) as audit:
        if body.layout.id != layout_id:
            raise HTTPException(status_code=422, detail=tr(
                f"路径里的 id（{layout_id}）与光路图自己的 id（{body.layout.id}）不一致",
                f"The id in the path ({layout_id}) is not the layout's own id ({body.layout.id})"))
        try:
            saved = layout_store.get_store().save(body.layout, base_revision=body.base_revision, by=body.by,
                                                  client=client)
        except layout_store.Conflict as exc:
            audit.outcome = "refused_409"
            return _conflict(exc)
        except layout_store.StoreError as exc:
            raise _http(exc) from None
    return _view(saved)


@router.post("/optical-layouts/{layout_id}/delete", response_model=ArchiveResponse, dependencies=_WRITE,
             responses={409: {"model": ConflictResponse, "description": "base_revision is not the stored revision"}})
def archive_layout(layout_id: str, body: LayoutArchiveBody, request: Request) -> Any:
    """Archive a layout (moved to ``archive/`` with its history and comments, not deleted).
    409 when ``base_revision`` is stale (same body as a stale save)."""
    client = client_record(request)
    with _audited("archive", layout_id, body.by, client) as audit:
        try:
            layout_store.get_store().archive(layout_id, base_revision=body.base_revision, by=body.by, client=client)
        except layout_store.Conflict as exc:
            audit.outcome = "refused_409"
            return _conflict(exc)
        except layout_store.StoreError as exc:
            raise _http(exc) from None
    return {"id": layout_id, "archived": True}


@router.post("/optical-layouts/{layout_id}/comments", response_model=CommentOut, dependencies=_WRITE)
def post_comment(layout_id: str, body: CommentBody, request: Request) -> dict:
    """Add a comment as the operator ``by`` (``author: "operator"``). ``component_id`` must be a
    component of the layout (422). 429 when the layout's comments are full of unresolved ones."""
    client = client_record(request)
    with _audited("comment", layout_id, body.by, client):
        try:
            c = layout_store.get_store().add_comment(layout_id, text=body.text, by=body.by, author="operator",
                                                     component_id=body.component_id, client=client)
        except layout_store.StoreError as exc:
            raise _http(exc) from None
    return layout_store.public_comment(c)


@router.post("/optical-layouts/{layout_id}/comments/{comment_id}/resolve", response_model=CommentOut,
             dependencies=_WRITE)
def resolve_comment(layout_id: str, comment_id: str, body: ResolveBody, request: Request) -> dict:
    """Resolve a comment (idempotent: an already resolved one is returned as it is). Only here:
    an external agent cannot resolve."""
    client = client_record(request)
    with _audited("resolve", f"{layout_id}/{comment_id}", body.by, client):
        try:
            c = layout_store.get_store().resolve_comment(layout_id, comment_id, by=body.by, client=client)
        except layout_store.StoreError as exc:
            raise _http(exc) from None
    return layout_store.public_comment(c)


__all__ = ["MAX_REQUEST_BYTES", "router"]
