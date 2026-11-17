"""The optical layout figure as a PDF, for **people**: ``POST /api/optical-layouts/figure.pdf``.

docs/v2/design/optical_layout_exports.md. The web editor draws the paper-style figure of a layout itself and sends
it here as an SVG; the answer is the same figure as a vector PDF (``mast/optical_layout/figure_pdf.py``: Chinese
labels in an embedded font subset, text still text, the page the size the SVG has on paper). Nothing is stored and
no layout is read: the figure is what the editor shows, saved or not.

It writes nothing, but it is a POST with a body, and it is guarded like every write of the editor
(``routes/optical_layout.py``): the access gate first, then :func:`mast.api.routes.operator.write_guard` -
``Content-Type: application/json`` (415), for a browser ``Sec-Fetch-Site`` same-origin, ``Origin`` equal to the
service and no foreign Host name (403); a program that sends none of the browser headers (a script, the launcher)
passes, as on every write route. The body is mandatory (``svg`` is required) and unknown fields are refused (422).
A body must say how long it is (``Content-Length``; a chunked one is a 411) and be at most
:data:`MAX_FIGURE_REQUEST_BYTES` (413); both are decided before the body is read (``mast/api/body_limit.py``),
which is why this route has a router of its own: the layout routes take 2 MB, a figure of a crowded table more.
An SVG that is not a figure (another document, a DTD, a reference to a file or a URL, a script) is a 422; a MAST
without PyMuPDF answers 501 and says how to install it.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from mast.api.body_limit import sized_route_class
from mast.api.routes.operator import client_record, write_guard
from mast.i18n import tr
from mast.optical_layout import figure_pdf

logger = logging.getLogger(__name__)

#: a request body above this is refused (413) without being read. The figure of the worst layouts (400 components;
#: 5000 events of beams) is measured in docs/v2/design/optical_layout_exports.md; this is several times that.
MAX_FIGURE_REQUEST_BYTES = 16_000_000


def _refuse_body(status: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


router = APIRouter(tags=["optical-layout"], route_class=sized_route_class(MAX_FIGURE_REQUEST_BYTES, _refuse_body))


class FigurePdfBody(BaseModel):
    """The figure to convert: the SVG file the editor's "Figure SVG" export saves, and a title for the PDF."""

    model_config = ConfigDict(extra="forbid")

    svg: str = Field(..., min_length=1, max_length=MAX_FIGURE_REQUEST_BYTES,
                     description="the figure as a standalone SVG file (root svg in the SVG namespace, sized in px)")
    title: str = Field("", max_length=200, description="written into the PDF's properties")


@router.post("/optical-layouts/figure.pdf", response_class=Response, dependencies=[Depends(write_guard)],
             responses={200: {"content": {"application/pdf": {}}, "description": "the figure as a vector PDF"},
                        413: {"description": "the request body is too large (refused before it is read)"},
                        422: {"description": "the body is not a figure SVG"},
                        501: {"description": "PyMuPDF is not installed"}})
def figure_pdf_route(body: FigurePdfBody, request: Request) -> Response:
    """The figure SVG as a one-page vector PDF: text kept as text (Chinese in an embedded subset of MuPDF's CJK
    font), the page 0.75 pt per pixel of the SVG (its size on paper at 96 px to the inch). 422 when ``svg`` is not
    a figure SVG (the detail says why), 501 without PyMuPDF, 500 when MuPDF fails."""
    client = client_record(request)
    try:
        pdf = figure_pdf.svg_to_pdf(body.svg, title=body.title)
    except figure_pdf.FigureRefused as exc:
        logger.info("optical layout figure: refused (%s) client=%s", exc, json.dumps(client, ensure_ascii=False, sort_keys=True))
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except figure_pdf.PdfUnavailable as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        logger.exception("optical layout figure: the PDF conversion failed")
        raise HTTPException(status_code=500, detail=tr(f"PDF 转换失败（{type(exc).__name__}: {exc}）",
                                                       f"The PDF conversion failed ({type(exc).__name__}: {exc})")) from None
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                             "Content-Disposition": 'attachment; filename="figure.pdf"'})


__all__ = ["FigurePdfBody", "MAX_FIGURE_REQUEST_BYTES", "router"]
