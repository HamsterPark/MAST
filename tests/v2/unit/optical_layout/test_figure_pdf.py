"""``POST /api/optical-layouts/figure.pdf``: the editor's figure SVG as a vector PDF (docs/v2/design/optical_layout_exports.md).

Pinned from both sides, like the layout's write routes (``test_layout_api.py``):

* what the editor sends comes back as a one-page vector PDF: the Chinese labels as text (extractable, in an
  embedded font subset), the page at 0.75 pt per pixel (96 px to the inch), and - rendered by MuPDF - the same
  picture as the SVG it came from;
* a body that is too large is refused before it is read (413 / 411, also over a real socket);
* a page from elsewhere cannot use it (foreign Origin, cross-site Sec-Fetch-Site without an Origin, a foreign Host
  name: 403; not JSON: 415); a program that sends no browser header is let through, as on every write route;
* what is not a figure SVG is a 422 that says why, and MuPDF is never reached for it (MuPDF itself would draw an
  HTML page as an SVG);
* without PyMuPDF the answer is 501 with how to install it; the module does not import it before it is needed;
* MuPDF is not thread-safe: the conversions run one at a time.
"""
from __future__ import annotations

import ast
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mast.api.access import AccessGate
from mast.api.routes import optical_layout_figure as routes
from mast.optical_layout import figure_pdf as F

pymupdf = pytest.importorskip("pymupdf")
np = pytest.importorskip("numpy")

PATH = "/api/optical-layouts/figure.pdf"
PAGE = "http://127.0.0.1:7862"
JSON = {"Content-Type": "application/json"}
FONT = "Helvetica, Arial, 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif"
LABEL = "反射镜 M1 · 532 nm"
NOTE = "偏振片 P2：线偏 30°（p）"


def figure_svg(w: int = 400, h: int = 200, *, label: str = LABEL, extra: str = "") -> str:
    """A small figure in the shape the editor makes: white ground, a beam, a dashed beam drawn as segments, a label
    with its halo as a separate stroked copy, a note; every text names its font."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
<rect x="0" y="0" width="{w}" height="{h}" fill="#ffffff"/>
<path d="M10 100L390 100" stroke="#00a000" stroke-width="3" fill="none"/>
<path d="M10 120L20 120M26 120L36 120M42 120L52 120" stroke="#c00000" stroke-width="2" fill="none"/>
<g><text x="20" y="40" font-family="{FONT}" font-size="16" fill="#ffffff" stroke="#ffffff" stroke-width="3" stroke-linejoin="round" aria-hidden="true">{label}</text>
<text x="20" y="40" font-family="{FONT}" font-size="16" fill="#000000">{label}</text></g>
<text x="20" y="170" font-family="{FONT}" font-size="14" fill="#000000">{NOTE}</text>
{extra}</svg>
"""


def _app(*, gate: bool = False, lan: bool = False) -> FastAPI:
    app = FastAPI()
    if gate:
        app.add_middleware(AccessGate, credentials=("op", "pw") if lan else None, dev_origins=())
    app.include_router(routes.router, prefix="/api")
    return app


@pytest.fixture
def client():
    return TestClient(_app(), base_url=PAGE)


def post(client, svg: str | None = None, *, title: str = "SHG 光路", headers: dict | None = None, body=None):
    payload = body if body is not None else {"svg": figure_svg() if svg is None else svg, "title": title}
    return client.post(PATH, json=payload, headers=headers or {})


def _gray(pix) -> "np.ndarray":
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(np.int16)
    return a.mean(axis=2)


def _worst_tile(a, b, tile: int = 48) -> float:
    """How far apart two renders are where they are furthest apart: per pixel the smallest |a - b| within one pixel
    of misregistration (both ways: text drawn through the PDF's 0.75 scale lands a fraction of a pixel elsewhere),
    averaged over tiles of *tile* px, the worst tile. A label whose glyphs are not drawn is a tile of tens of grey
    levels; antialiasing is a few."""
    def nearest(x, y):
        p = np.pad(y, 1, mode="edge")
        return np.min([np.abs(x - p[dy:dy + x.shape[0], dx:dx + x.shape[1]]) for dy in range(3) for dx in range(3)], axis=0)
    m = np.maximum(nearest(a, b), nearest(b, a))
    h, w = m.shape[0] // tile * tile, m.shape[1] // tile * tile
    return float(m[:h, :w].reshape(h // tile, tile, w // tile, tile).mean(axis=(1, 3)).max())


# ── the PDF ──────────────────────────────────────────────────────────────────────────────────


def test_the_figure_comes_back_as_a_one_page_vector_pdf_with_its_chinese_text(client):
    r = post(client)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["cache-control"] == "no-store"
    assert r.content[:5] == b"%PDF-"
    doc = pymupdf.open("pdf", r.content)
    assert doc.page_count == 1
    page = doc[0]
    # 400 x 200 px at 96 px to the inch: 300 x 150 pt (MuPDF alone would make it 400 x 200)
    assert (round(page.rect.width, 3), round(page.rect.height, 3)) == (300.0, 150.0)
    text = page.get_text()
    assert LABEL in text and NOTE in text, text
    assert not page.get_images(), "the text and the lines are vectors, not a picture"
    assert doc.metadata["title"] == "SHG 光路" and doc.metadata["creator"] == "MAST optical layout"


def test_the_fonts_are_embedded_and_cut_down_to_what_is_used(client):
    r = post(client)
    doc = pymupdf.open("pdf", r.content)
    fonts = doc[0].get_fonts(full=True)
    names = [f[3] for f in fonts]
    assert fonts and all(f[1] != "n/a" for f in fonts), fonts                     # every font is in the file
    assert all(len(n) > 7 and n[6] == "+" and n[:6].isupper() for n in names), names   # "ABCDEF+Name": a subset
    assert any("Fallback" in n or "CJK" in n for n in names), names              # the Chinese glyphs have their font
    # the whole CJK font is about 1.8 MB; cut down, the file is a few tens of KB
    assert len(r.content) < 200_000, len(r.content)


def test_rendered_by_mupdf_the_pdf_is_the_picture_the_svg_is(client):
    svg = figure_svg()
    r = post(client, svg)
    pdf_page = pymupdf.open("pdf", r.content)[0]
    svg_page = pymupdf.open(stream=svg.encode("utf-8"), filetype="svg")[0]
    # the same pixels: the SVG at 1.5 (MuPDF reads a px as a pt), the PDF (0.75 pt per px) at 2
    a = _gray(svg_page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False))
    b = _gray(pdf_page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False))
    assert a.shape == b.shape == (300, 600)
    assert float(np.abs(a - b).mean()) < 1.0, float(np.abs(a - b).mean())
    assert _worst_tile(a, b) < 15, _worst_tile(a, b)
    # the measure sees what matters: the same figure with its Chinese characters not drawn (a CJK font lost on the
    # way) is far off where the label is
    blank = svg.replace(LABEL, LABEL.replace("反射镜", "   ")).replace(NOTE, "".join(" " if ord(ch) > 0x2fff else ch for ch in NOTE))
    c = _gray(pymupdf.open(stream=blank.encode("utf-8"), filetype="svg")[0].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False))
    assert _worst_tile(a, c) > 25, _worst_tile(a, c)
    # and the labels are drawn, Chinese included: ink where the label and the note are, in both
    for top, bottom in ((30, 66), (232, 262)):                                    # px rows of the label and the note at 1.5x
        ink_a = int((a[top:bottom] < 128).sum())
        ink_b = int((b[top:bottom] < 128).sum())
        assert ink_a > 400 and abs(ink_a - ink_b) <= 0.05 * ink_a, (top, ink_a, ink_b)


def test_a_figure_of_the_size_of_the_worst_layout_converts_in_seconds(client):
    """400 components with a halo'd Chinese label each and 600 beam paths, a third of them dashes as segments: an SVG
    of a few MB, as the editor makes for the 400-component stress layout."""
    import random

    rnd = random.Random(1)
    parts = []
    for i in range(400):
        x, y = rnd.uniform(20, 1000), rnd.uniform(20, 720)                 # its label (about 60 px) stays on the page
        parts.append(f'<g transform="translate({x:.2f} {y:.2f}) rotate({rnd.uniform(0, 360):.1f})">'
                     + "".join(f'<path d="M{k - 6} -8L{k - 6} 8L{k - 4} 8L{k - 4} -8Z" fill="#f3f3f3" stroke="#000000" stroke-width="0.8"/>' for k in range(8))
                     + "</g>")
        for halo in (True, False):
            paint = 'fill="#ffffff" stroke="#ffffff" stroke-width="3"' if halo else 'fill="#000000"'
            parts.append(f'<text x="{x + 8:.2f}" y="{y - 6:.2f}" font-family="{FONT}" font-size="10" {paint}>反射镜 M{i}</text>')
    for b in range(600):
        pts = [(rnd.uniform(0, 1100), rnd.uniform(0, 760)) for _ in range(11)]
        d = "M" + "L".join(f"{px:.2f} {py:.2f}" for px, py in pts)
        if b % 3 == 0:
            d = "".join(f"M{x0 + (x1 - x0) * j / 40:.2f} {y0 + (y1 - y0) * j / 40:.2f}L{x0 + (x1 - x0) * (j + 0.6) / 40:.2f} {y0 + (y1 - y0) * (j + 0.6) / 40:.2f}"
                        for (x0, y0), (x1, y1) in zip(pts, pts[1:]) for j in range(40))
        parts.append(f'<path d="{d}" fill="none" stroke="#{rnd.randrange(1 << 24):06x}" stroke-width="1.4"/>')
    svg = figure_svg(1100, 760, extra="\n".join(parts))
    assert len(svg.encode("utf-8")) > 2_000_000
    started = time.perf_counter()
    r = post(client, svg)
    assert r.status_code == 200, r.text[:300]
    assert time.perf_counter() - started < 30
    doc = pymupdf.open("pdf", r.content)
    text = doc[0].get_text()
    # every label is text
    import re

    assert doc.page_count == 1
    assert set(re.findall(r"反射镜 M(\d+)", text)) == {str(i) for i in range(400)}
    assert len(r.content) < len(svg.encode("utf-8"))


# ── the size of the body: decided before it is read ──────────────────────────────────────────


def test_the_limit_is_16_mb_and_a_larger_body_is_refused_before_it_is_read(client):
    assert routes.MAX_FIGURE_REQUEST_BYTES == 16_000_000
    raw = b'{"svg": "<svg/>"}'
    for declared, status in (("16000001", 413), ("999999999999", 413), ("abc", 400), ("-1", 400)):
        r = client.post(PATH, content=raw, headers={**JSON, "Content-Length": declared})
        assert r.status_code == status, (declared, r.status_code, r.text)
    seen: list[int] = []

    def chunks():
        for part in (raw[:5], raw[5:]):
            seen.append(len(part))
            yield part

    r = client.post(PATH, content=chunks(), headers=JSON)
    assert r.status_code == 411 and seen == [], "a chunked body is refused without being read"
    r = client.post(PATH, content=raw, headers={**JSON, "Transfer-Encoding": "chunked"})
    assert r.status_code == 411


def test_over_a_real_socket_the_413_comes_before_any_body_is_sent():
    import socket

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(_app(), host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        head = [f"POST {PATH} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Content-Type: application/json", "Content-Length: 20000000"]
        with socket.create_connection(("127.0.0.1", port), timeout=6) as s:
            s.sendall(("\r\n".join(head) + "\r\n\r\n").encode("ascii"))     # and then nothing
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        assert data.split(b" ", 2)[1] == b"413", data[:200]
    finally:
        server.should_exit = True
        t.join(timeout=20)


# ── who may ask ──────────────────────────────────────────────────────────────────────────────


def _browser(client, *, origin: str | None = PAGE, site: str | None = "same-origin", host: str | None = None):
    headers = {**JSON}
    if origin is not None:
        headers["Origin"] = origin
    if site is not None:
        headers["Sec-Fetch-Site"] = site
    if host is not None:
        headers["Host"] = host
    return post(client, headers=headers)


def test_the_operators_own_page_gets_its_pdf(client):
    assert _browser(client).status_code == 200


@pytest.mark.parametrize("kw", [
    dict(origin="http://evil.example"),
    dict(origin="http://127.0.0.1:9999"),                                   # another local page
    dict(origin="null"),
    dict(origin=None, site="cross-site"),                                   # no Origin, but the browser says cross-site
    dict(origin=None, site="same-site"),
    dict(host="evil.example:7862", origin="http://evil.example:7862"),      # DNS rebinding
], ids=["evil", "other-port", "null", "no-origin-cross-site", "no-origin-same-site", "rebinding"])
def test_a_page_from_elsewhere_gets_a_403(client, kw):
    r = _browser(client, **kw)
    assert r.status_code == 403, (kw, r.status_code, r.text)
    assert r.content[:5] != b"%PDF-"


def test_a_program_that_sends_no_browser_header_is_let_through_as_on_every_write_route(client):
    r = post(client)                                                        # no Origin, no Sec-Fetch-*
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"


def test_a_body_that_is_not_json_is_a_415(client):
    raw = ('{"svg": %s}' % __import__("json").dumps(figure_svg())).encode()
    for headers in ({"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"}, {}):
        assert client.post(PATH, content=raw, headers=headers).status_code == 415, headers


def test_behind_the_real_gate_too():
    gated = TestClient(_app(gate=True), base_url=PAGE)
    assert _browser(gated, origin="http://evil.example").status_code == 403
    assert _browser(gated).status_code == 200


# ── what is a figure ─────────────────────────────────────────────────────────────────────────

NS = 'xmlns="http://www.w3.org/2000/svg"'
PNG_1PX = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
NOT_FIGURES = {
    "text": "hello world",
    "html": "<html><body><p>hi</p></body></html>",
    "pdf": "%PDF-1.7\n1 0 obj\n<<>>\nendobj\n",
    "no-namespace": '<svg width="10" height="10"><rect width="5" height="5"/></svg>',
    "broken-xml": f'<svg {NS} width="10" height="10"><g></svg>',
    "junk-after": f'<svg {NS} width="10" height="10"></svg><svg {NS}/>',
    "doctype": f'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><svg {NS} width="10" height="10"><text>&b;</text></svg>',
    "doctype-public": f'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd"><svg {NS} width="10" height="10"/>',
    "stylesheet-pi": f'<?xml-stylesheet href="http://evil.example/x.css"?><svg {NS} width="10" height="10"/>',
    "latin1": f'<?xml version="1.0" encoding="ISO-8859-1"?><svg {NS} width="10" height="10"/>',
    "file-image": f'<svg {NS} width="10" height="10"><image href="file:///C:/Windows/win.ini" width="5" height="5"/></svg>',
    "relative-image": f'<svg {NS} xmlns:xlink="http://www.w3.org/1999/xlink" width="10" height="10"><image xlink:href="secret.png" width="5" height="5"/></svg>',
    "http-image": f'<svg {NS} width="10" height="10"><image href="http://evil.example/x.png" width="5" height="5"/></svg>',
    "svg-data-image": f'<svg {NS} width="10" height="10"><image href="data:image/svg+xml;base64,PHN2Zy8+" width="5" height="5"/></svg>',
    # the figure has no pictures: not even an embedded PNG reaches MuPDF's decoders, by the element or by its href
    "png-data-image": f'<svg {NS} width="10" height="10"><image href="{PNG_1PX}" width="5" height="5"/></svg>',
    "internal-image": f'<svg {NS} width="10" height="10"><defs><g id="a"/></defs><image href="#a" width="5" height="5"/></svg>',
    "data-href-on-use": f'<svg {NS} width="10" height="10"><use href="{PNG_1PX}"/></svg>',
    "foreign-use": f'<svg {NS} width="10" height="10"><use href="other.svg#a"/></svg>',
    "css-url": f'<svg {NS} width="10" height="10"><rect width="5" height="5" style="fill: url(http://evil.example/p)"/></svg>',
    "css-import": f'<svg {NS} width="10" height="10"><style>@import "http://evil.example/x.css";</style></svg>',
    "style-url": f'<svg {NS} width="10" height="10"><style>rect {{ fill: url( "x.svg#p" ) }}</style></svg>',
    "script": f'<svg {NS} width="10" height="10"><script>alert(1)</script></svg>',
    "foreign-object": f'<svg {NS} width="10" height="10"><foreignObject width="5" height="5"><p>hi</p></foreignObject></svg>',
    "mm": f'<svg {NS} width="100mm" height="50mm"/>',
    "percent": f'<svg {NS} width="100%" height="100%" viewBox="0 0 10 10"/>',
    "no-size": f"<svg {NS}/>",
    "zero": f'<svg {NS} width="0" height="10"/>',
    "too-wide": f'<svg {NS} width="19201" height="10"/>',
    "infinite": f'<svg {NS} width="1e999" height="10"/>',
}


@pytest.mark.parametrize("name", list(NOT_FIGURES), ids=list(NOT_FIGURES))
def test_what_is_not_a_figure_svg_is_a_422_that_says_why_and_never_reaches_mupdf(client, monkeypatch, name):
    reached: list[str] = []

    def no_mupdf():
        reached.append(name)
        raise AssertionError("MuPDF must not see this")

    monkeypatch.setattr(F, "_pymupdf", no_mupdf)
    r = post(client, NOT_FIGURES[name])
    assert r.status_code == 422, (name, r.status_code, r.text)
    assert isinstance(r.json()["detail"], str) and r.json()["detail"], r.text
    assert reached == []


def test_an_svg_with_too_many_elements_is_refused(client, monkeypatch):
    monkeypatch.setattr(F, "MAX_ELEMENTS", 5)
    many = f'<svg {NS} width="10" height="10">' + "<g/>" * 4 + "</svg>"           # 5 with the root: allowed
    assert F.check_svg(many).elements == 5
    r = post(client, many.replace("<g/>", "<g/><g/>", 1))                          # 6
    assert r.status_code == 422 and "5" in r.json()["detail"], r.text


def test_what_a_figure_may_have(client):
    """Internal references, a viewBox for a size, "px", a pattern by url(#...)."""
    svg = (f'<?xml version="1.0" encoding="utf-8"?><svg {NS} xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 120 80">'
           '<defs><pattern id="p" width="4" height="4" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="1"/></pattern>'
           '<path id="tick" d="M0 0L5 0" stroke="#000"/></defs>'
           '<rect width="10" height="10" fill="url(#p)" style="stroke: url( \'#p\' )"/><use xlink:href="#tick"/>'
           '<style>rect { fill: url(#p) }</style></svg>')
    info = F.check_svg(svg)
    assert (info.width_px, info.height_px) == (120.0, 80.0)
    assert F.check_svg(f'<svg {NS} width="300px" height="20.5"/>').width_px == 300.0
    assert post(client, svg).status_code == 200


@pytest.mark.parametrize("body", [
    None, {}, {"title": "x"}, {"svg": ""}, {"svg": 12}, {"svg": figure_svg(), "title": "x" * 201},
    {"svg": figure_svg(), "approved": True}, {"svg": figure_svg(), "client": {"peer": "forged"}},
], ids=["no-body", "empty", "no-svg", "empty-svg", "svg-not-text", "long-title", "unknown-field", "client-field"])
def test_the_body_is_mandatory_and_unknown_fields_are_refused(client, body):
    r = client.post(PATH, headers=JSON) if body is None else client.post(PATH, json=body)
    assert r.status_code == 422, (body if body is None else list(body), r.status_code, r.text[:200])


def test_a_title_with_control_characters_is_cleaned_in_the_pdf(client):
    r = post(client, title="光路\x00\x1b[31m 图")
    assert pymupdf.open("pdf", r.content).metadata["title"] == "光路[31m 图"


# ── without PyMuPDF, when MuPDF fails, and one at a time ─────────────────────────────────────


def test_without_pymupdf_the_answer_is_501_and_says_how_to_install_it(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "pymupdf", None)                       # import pymupdf -> ImportError
    monkeypatch.setitem(sys.modules, "fitz", None)
    r = post(client)
    assert r.status_code == 501, r.text
    assert "PyMuPDF" in r.json()["detail"] and "requirements-pdf.txt" in r.json()["detail"]


def test_pymupdf_is_imported_only_when_a_pdf_is_asked_for():
    """The app must boot without it (it is optional in the public snapshot): no module-level import."""
    for mod in (F, routes):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        names = {a.name.split(".")[0] for n in top for a in n.names} | {(n.module or "").split(".")[0] for n in top if isinstance(n, ast.ImportFrom)}
        assert not names & {"pymupdf", "fitz"}, (mod.__name__, names)


def test_when_mupdf_fails_the_answer_is_a_500_that_says_so(client, monkeypatch):
    class Broken:
        @staticmethod
        def open(*_a, **_k):
            raise RuntimeError("MuPDF said no")

    monkeypatch.setattr(F, "_pymupdf", lambda: Broken)
    r = post(client)
    assert r.status_code == 500 and "MuPDF said no" in r.json()["detail"]


def test_the_conversions_run_one_at_a_time(monkeypatch):
    """PyMuPDF is not thread-safe and FastAPI runs a sync route in a pool of threads."""
    real = F._pymupdf()
    inside = 0
    most = 0
    guard = threading.Lock()

    class Slow:
        get_pdf_now = staticmethod(real.get_pdf_now)
        VersionBind = "test"

        @staticmethod
        def open(*a, **k):
            doc = real.open(*a, **k)
            if k.get("filetype") != "svg":
                return doc

            class Watched:
                def convert_to_pdf(self):
                    nonlocal inside, most
                    with guard:
                        inside += 1
                        most = max(most, inside)
                    time.sleep(0.2)
                    with guard:
                        inside -= 1
                    return doc.convert_to_pdf()

                def close(self):
                    doc.close()

            return Watched()

    monkeypatch.setattr(F, "_pymupdf", lambda: Slow)
    out: list[bytes] = []
    threads = [threading.Thread(target=lambda: out.append(F.svg_to_pdf(figure_svg()))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(out) == 3 and all(p[:5] == b"%PDF-" for p in out)
    assert most == 1, most


def test_a_conversion_in_progress_does_not_hold_up_the_server(monkeypatch):
    """The route is a plain ``def``: FastAPI runs it in its thread pool, so a conversion of seconds leaves the event
    loop free. While one PDF is being made (held here for 2 s inside the conversion), a refusal of the same route and
    a read of another route (answered on the event loop itself) come back at once."""
    import http.client
    import json as _json

    import uvicorn

    real_check = F.check_svg
    started = threading.Event()

    def slow(svg, *, title=""):
        real_check(svg)
        started.set()
        time.sleep(2.0)                                                  # what MuPDF takes on a large figure, and more
        return b"%PDF-1.7\n%%EOF\n"

    monkeypatch.setattr(F, "svg_to_pdf", slow)
    app = _app()

    @app.get("/api/ping")
    async def ping():                                                    # runs on the loop: answers only if it is free
        return {"ok": True}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]

    def call(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        data = None if body is None else _json.dumps(body).encode()
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data is not None else {})
        r = conn.getresponse()
        out = (r.status, r.read())
        conn.close()
        return out

    first: dict = {}
    try:
        worker = threading.Thread(target=lambda: first.update(r=call("POST", PATH, {"svg": figure_svg()})))
        t0 = time.perf_counter()
        worker.start()
        assert started.wait(15), "the conversion did not start"
        t1 = time.perf_counter()
        refused = call("POST", PATH, {"svg": "not an svg"})
        refused_in = time.perf_counter() - t1
        t2 = time.perf_counter()
        pong = call("GET", "/api/ping")
        pong_in = time.perf_counter() - t2
        worker.join(30)
        total = time.perf_counter() - t0
    finally:
        server.should_exit = True
        t.join(timeout=20)
    assert refused[0] == 422 and refused_in < 1.0, (refused[0], refused_in)
    assert pong[0] == 200 and pong_in < 1.0, (pong[0], pong_in)
    assert first["r"][0] == 200 and first["r"][1].startswith(b"%PDF-") and total >= 2.0, (first, total)


# ── in the real app ──────────────────────────────────────────────────────────────────────────


def test_the_route_is_part_of_the_main_app_and_gated_like_every_write():
    from mast.api.app import create_app

    app = create_app(dev_cors=False)
    spec = app.openapi()
    assert "post" in spec["paths"]["/api/optical-layouts/figure.pdf"]
    assert "FigurePdfBody" in spec["components"]["schemas"]
    assert not [p for p in spec["paths"] if p.startswith("/api/ext")]
    c = TestClient(app, base_url=PAGE)
    raw = __import__("json").dumps({"svg": figure_svg()}).encode()
    assert c.post(PATH, content=raw, headers={"Content-Type": "text/plain"}).status_code == 415
    assert c.post(PATH, json={"svg": figure_svg()}, headers={"Origin": "http://evil.example"}).status_code == 403
    r = c.post(PATH, json={"svg": figure_svg()})
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
