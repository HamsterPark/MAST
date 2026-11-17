"""The figure of an optical layout as a PDF: the editor's figure SVG turned into a vector PDF with PyMuPDF.

docs/v2/design/optical_layout_exports.md. The web editor draws the paper-style figure itself (its canvas component
in the "paper" variant, ``frontend/src/lib/opticalLayout/exportStyle.ts``) and saves it as an SVG; for a PDF it
sends that SVG here (``POST /api/optical-layouts/figure.pdf``, ``mast/api/routes/optical_layout_figure.py``).

:func:`svg_to_pdf` does three things:

1. **checks** the SVG (:func:`check_svg`): a well-formed XML document whose root is ``svg`` in the SVG namespace,
   sized in CSS pixels, self-contained, without a DTD. MuPDF itself is lenient - it draws an HTML page as if it
   were an SVG - so this is what decides that the body is a figure;
2. **converts** it with MuPDF (``convert_to_pdf``). MuPDF draws the text as text: Latin in its built-in Helvetica,
   Chinese in its built-in CJK font (Droid Sans Fallback), each embedded with a ToUnicode map, so the labels can be
   selected and searched and do not depend on the fonts of the machine that opens the file. The figure SVG is made
   for MuPDF: dashes are geometry (``dash.ts``), a halo is its own text element (LayoutSvg ``HaloText``), every text
   names its font. MuPDF draws an SVG pixel as one PDF point; the page is put on one 0.75 points per pixel (96
   CSS pixels to the inch), the size the SVG has on paper in a browser, Word or Inkscape and the size the figure PNG
   says it has;
3. **makes it small**: the fonts are cut down to the glyphs used (the CJK font is 1.8 MB whole, a few tens of KB
   cut), unused objects dropped, streams compressed.

PyMuPDF is imported when a PDF is asked for, not before: it is AGPL-licensed and optional in the public snapshot
(``requirements-pdf.txt``); without it the route answers 501 and says how to install it. It is not thread-safe, so
the conversions run one at a time (``_LOCK``).
"""
from __future__ import annotations

import logging
import re
import threading
import time
import xml.parsers.expat
from dataclasses import dataclass
from typing import Any, Optional

from mast.i18n import tr

logger = logging.getLogger(__name__)

SVG_NS = "http://www.w3.org/2000/svg"
#: CSS pixels per inch: what a pixel of the figure measures on paper
CSS_PX_PER_INCH = 96
#: PDF points per pixel of the figure
PT_PER_PX = 72 / CSS_PX_PER_INCH
#: the largest side of a figure, pixels: 14 400 points, the largest page a PDF reader must show (PDF 1.7 annex C)
MAX_SIDE_PX = 19_200
#: the most elements a figure may have (the worst layout's figure has a few tens of thousands)
MAX_ELEMENTS = 500_000
#: elements a figure has no use for, and that could fetch or run something elsewhere
#: ``image`` among them: the figure draws no picture, so MuPDF's image decoders are never reached through it (the
#: route is open to the LAN when MAST is); allow an embedded picture only when the figure comes to need one
FORBIDDEN_ELEMENTS = frozenset({"script", "foreignObject", "iframe", "object", "embed", "audio", "video", "animation",
                                "image"})
#: the only reference a figure makes: to an element of its own (``#id``). Not even ``data:``: nothing in the figure is
#: a picture, and a data URI would be decoded by MuPDF
_OK_HREF = re.compile(r"\s*#")
#: a CSS ``url(`` that does not point into the document (the blanks inside the look-ahead: an outer ``\s*`` could give
#: one back and let ``url( '#p' )`` look foreign)
_FOREIGN_URL = re.compile(r"url\((?!\s*['\"]?\s*#)", re.IGNORECASE)
#: a length in CSS pixels: a number, optionally with "px"
_PX_LENGTH = re.compile(r"\s*([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*(px)?\s*")
_LOCK = threading.Lock()


class FigureRefused(ValueError):
    """The body is not a figure SVG this converts (the route answers 422 with the message)."""


class PdfUnavailable(RuntimeError):
    """PyMuPDF is not installed (the route answers 501 with the message)."""


@dataclass(frozen=True)
class SvgInfo:
    """What :func:`check_svg` read: the size of the figure in CSS pixels and how many elements it has."""

    width_px: float
    height_px: float
    elements: int


def _px(value: Optional[str]) -> Optional[float]:
    """A length in CSS pixels ("1100", "1100px"), or None for anything else (a unit, a percentage, nothing)."""
    if value is None:
        return None
    m = _PX_LENGTH.fullmatch(value)
    return float(m.group(1)) if m else None


def _local(name: str) -> str:
    """``"http://www.w3.org/2000/svg svg"`` -> ``"svg"`` (expat writes namespace and name with a blank between)."""
    return name.rsplit(" ", 1)[-1]


def check_svg(svg: str) -> SvgInfo:
    """The size of a figure SVG, or :class:`FigureRefused` saying why it is not one.

    It must be well-formed XML (UTF-8 if it declares an encoding) with no DTD (so no entity either) and no
    processing instruction; its root element must be ``svg`` in the SVG namespace, sized in CSS pixels (``width`` / ``height``,
    or the ``viewBox`` without them), each side above 0 and at most :data:`MAX_SIDE_PX`; it must be self-contained:
    every ``href`` points into the document (``#id``; no ``data:`` either), every CSS ``url(`` points into the
    document, no ``@import``; and it must not hold an ``image``, a script, a ``foreignObject`` or the like. At most
    :data:`MAX_ELEMENTS` elements. Read with expat as a stream: nothing is built, nothing is fetched.
    """
    if not isinstance(svg, str) or not svg.strip():
        raise FigureRefused(tr("没有 SVG 内容", "There is no SVG"))
    state: dict[str, Any] = {"root": None, "elements": 0, "style": 0, "style_text": []}

    def refuse(zh: str, en: str) -> None:
        raise FigureRefused(tr(zh, en))

    def on_decl(version: str, encoding: Optional[str], standalone: int) -> None:
        if encoding is not None and encoding.strip().lower().replace("_", "-") not in ("utf-8", "utf8"):
            refuse(f"SVG 必须是 UTF-8 编码（声明的是 {encoding[:40]}）", f"The SVG must be UTF-8 (it declares {encoding[:40]})")

    def on_doctype(*_a: Any) -> None:
        refuse("SVG 里不许有 DOCTYPE（DTD）", "A DOCTYPE (DTD) is not allowed in the SVG")

    def on_pi(target: str, _data: str) -> None:
        refuse(f"SVG 里不许有处理指令（<?{target[:40]} …?>）", f"A processing instruction (<?{target[:40]} ...?>) is not allowed in the SVG")

    def check_style_text(text: str) -> None:
        if "@import" in text.lower() or _FOREIGN_URL.search(text):
            refuse("SVG 的样式引用了外部资源（@import 或 url(…)）", "The SVG's style refers to something outside it (@import or url(...))")

    def on_start(name: str, attrs: dict[str, str]) -> None:
        local = _local(name)
        state["elements"] += 1
        if state["elements"] > MAX_ELEMENTS:
            refuse(f"SVG 的元素超过 {MAX_ELEMENTS} 个", f"The SVG has more than {MAX_ELEMENTS} elements")
        if state["root"] is None:
            state["root"] = (name, attrs)
            if name != f"{SVG_NS} svg":
                refuse(f"根元素不是 SVG 命名空间里的 svg（是 {name[-80:]}）",
                       f"The root element is not svg in the SVG namespace (it is {name[-80:]})")
        if local in FORBIDDEN_ELEMENTS:
            refuse(f"SVG 里不许有 <{local}>", f"<{local}> is not allowed in the SVG")
        if local == "style":
            state["style"] += 1
        for key, value in attrs.items():
            akey = _local(key)
            if akey == "href" and not _OK_HREF.match(value):
                refuse(f"SVG 引用了外部资源（{akey}={value[:80]}）；图要自成一体",
                       f"The SVG refers to something outside it ({akey}={value[:80]}); the figure must be self-contained")
            if "url(" in value.lower() and _FOREIGN_URL.search(value):
                refuse(f"SVG 引用了外部资源（{akey}={value[:80]}）；图要自成一体",
                       f"The SVG refers to something outside it ({akey}={value[:80]}); the figure must be self-contained")

    def on_end(name: str) -> None:
        if _local(name) == "style" and state["style"] > 0:
            state["style"] -= 1
            if state["style"] == 0:
                check_style_text("".join(state["style_text"]))
                state["style_text"] = []

    def on_text(text: str) -> None:
        if state["style"] > 0:
            state["style_text"].append(text)

    parser = xml.parsers.expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.XmlDeclHandler = on_decl
    # no DTD at all: an entity can only be declared in one, and an external one would be fetched by a reader that does
    parser.StartDoctypeDeclHandler = on_doctype
    parser.ProcessingInstructionHandler = on_pi
    parser.StartElementHandler = on_start
    parser.EndElementHandler = on_end
    parser.CharacterDataHandler = on_text
    try:
        parser.Parse(svg.encode("utf-8"), True)
    except xml.parsers.expat.ExpatError as exc:
        raise FigureRefused(tr(f"不是一个完整的 SVG（XML 有错：{exc}）", f"Not a whole SVG (the XML is broken: {exc})")) from None
    if state["root"] is None:
        raise FigureRefused(tr("没有 SVG 内容", "There is no SVG"))
    _name, attrs = state["root"]
    # the size MuPDF gives the page: width / height, each in pixels; a side without one takes the viewBox's
    box = (attrs.get("viewBox") or "").replace(",", " ").split()
    vb: Optional[tuple[float, float]] = None
    if len(box) == 4:
        try:
            vb = (float(box[2]), float(box[3]))
        except ValueError:
            vb = None
    width = _px(attrs["width"]) if "width" in attrs else (vb[0] if vb else None)
    height = _px(attrs["height"]) if "height" in attrs else (vb[1] if vb else None)
    if width is None or height is None:
        raise FigureRefused(tr("SVG 的宽和高要以像素给出（width / height，或 viewBox）",
                               "The SVG's width and height must be given in pixels (width / height, or a viewBox)"))
    if not (0 < width <= MAX_SIDE_PX and 0 < height <= MAX_SIDE_PX):
        raise FigureRefused(tr(f"SVG 的尺寸 {width:g} × {height:g} 像素超出范围（每边 0 到 {MAX_SIDE_PX}）",
                               f"The SVG's size {width:g} × {height:g} px is out of range (0 to {MAX_SIDE_PX} a side)"))
    return SvgInfo(width_px=width, height_px=height, elements=state["elements"])


def _pymupdf() -> Any:
    """The PyMuPDF module, or :class:`PdfUnavailable`."""
    try:
        import pymupdf  # PyMuPDF >= 1.24 (AGPL; optional in the public snapshot)

        return pymupdf
    except ImportError:
        pass
    try:
        import fitz  # the older name of the same package

        if hasattr(fitz, "open") and hasattr(fitz, "Document"):
            return fitz
    except ImportError:
        pass
    raise PdfUnavailable(tr("这个 MAST 没有装 PyMuPDF，导不了 PDF（pip install -r MASTv2/requirements-pdf.txt）",
                            "This MAST has no PyMuPDF and cannot make a PDF (pip install -r MASTv2/requirements-pdf.txt)"))


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def svg_to_pdf(svg: str, *, title: str = "") -> bytes:
    """The figure SVG *svg* as a one-page vector PDF (bytes). :class:`FigureRefused` when it is not a figure SVG
    (:func:`check_svg`), :class:`PdfUnavailable` without PyMuPDF; anything MuPDF raises is passed on."""
    info = check_svg(svg)
    fitz = _pymupdf()
    started = time.perf_counter()
    with _LOCK:
        src = fitz.open(stream=svg.encode("utf-8"), filetype="svg")
        try:
            drawn = src.convert_to_pdf()
        finally:
            src.close()
        mid = fitz.open("pdf", drawn)
        out = fitz.open()
        try:
            rect = mid[0].rect
            page = out.new_page(width=rect.width * PT_PER_PX, height=rect.height * PT_PER_PX)
            page.show_pdf_page(page.rect, mid, 0)
            try:
                out.subset_fonts()
            except Exception as exc:  # noqa: BLE001 - a larger file is still a right one
                logger.warning("optical layout figure: fonts not subset (%s: %s)", type(exc).__name__, exc)
            now = fitz.get_pdf_now()
            out.set_metadata({
                "title": _CONTROL.sub("", title or "")[:200],
                "creator": "MAST optical layout",
                "producer": f"PyMuPDF {getattr(fitz, 'VersionBind', '')}".strip(),
                "creationDate": now,
                "modDate": now,
            })
            pdf = out.tobytes(garbage=3, deflate=True)
        finally:
            out.close()
            mid.close()
    logger.info("optical layout figure: %d x %d px, %d elements, SVG %d bytes -> PDF %d bytes in %.2f s",
                round(info.width_px), round(info.height_px), info.elements, len(svg.encode("utf-8")), len(pdf),
                time.perf_counter() - started)
    return pdf


__all__ = ["CSS_PX_PER_INCH", "FigureRefused", "MAX_ELEMENTS", "MAX_SIDE_PX", "PT_PER_PX", "PdfUnavailable",
           "SvgInfo", "check_svg", "svg_to_pdf"]
