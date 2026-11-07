"""``mast_optical_layout`` and ``mast_optical_layout_comment``: what a model reads and sends.

The MCP server is stdlib-only (``test_mast_mcp_stdlib_only``); the handlers are driven with a
stand-in call context that records what would be sent to MAST (the real server end is covered by
``tests/v2/unit/api_ext/test_ext_optical_layout.py`` and the end-to-end test next to it).
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports (same bootstrap as test_wrap_skill_minimal) ──
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
_MASTV2_ROOT = str(_REPO / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]
_SERVER_DIR = _REPO / "integrations" / "claude-code" / "server"
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import base64
import json

import pytest

from mast_mcp.client import MastError, Response
from mast_mcp.tools import ArgError, TOOLS, as_output

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


class FakeClient:
    def __init__(self, png=PNG, status=200, content_type="image/png"):
        self.requests: list[tuple] = []
        self._resp = (png, status, content_type)

    def request(self, method, path, *, query=None, body=None, timeout=30.0):
        self.requests.append((method, path, query))
        png, status, ctype = self._resp
        return Response(status, {"Content-Type": ctype}, png)

    def http_error(self, method, path, resp):
        return MastError("not_found", f"{method} {path} -> {resp.status}", status=resp.status)


class FakeCtx:
    def __init__(self, get=None, post=None, client=None):
        self.fetched: list[tuple] = []
        self.posted: list[tuple] = []
        self._get, self._post = get, post
        self.client = client or FakeClient()

    def get(self, path, query=None, **kw):
        self.fetched.append((path, query))
        value = self._get(path) if callable(self._get) else self._get
        if isinstance(value, Exception):
            raise value
        return value

    def post(self, path, body, **kw):
        self.posted.append((path, body))
        return self._post if self._post is not None else {"ok": True, "comment": {"id": "c3", "author": "agent"}}

    def timeout(self, preferred=30.0):
        return preferred


def call(name, ctx, **args):
    return as_output(TOOLS[name].handler(ctx, args))


LISTING = {"layouts": [
    {"id": "shg", "title": "SHG excitation", "revision": 12, "updated_at": "2026-11-03T12:00:00+00:00", "updated_by": "ana",
     "components": 17, "open_comments": 2},
    {"id": "old", "title": "", "revision": 0, "updated_at": "", "updated_by": "", "components": 0, "open_comments": 0, "error": "bad json"}]}
FULL = {"layout": {"id": "shg", "revision": 12, "updated_by": "ana", "updated_at": "2026-11-03T12:00:00+00:00",
                   "table": {"width_mm": 1200.0}, "components": [
                       {"id": "m1", "type": "mirror", "label": "M1", "x_mm": 112.5, "y_mm": 62.5, "angle_deg": 135.0, "aperture_mm": 25.4,
                        "annotations": {"model": "BB1-E02"}, "props": {}, "links": {}, "locked": False}]},
        "trace": [{"source": "las1", "wavelength_nm": 532.0, "branches": []}],
        "summary": "Optical layout \"SHG\" ...\n1) L1 laser (532 nm)\n   -> M1 mirror\n",
        "links_state": {}, "comments": [{"id": "c1"}]}


# ── the schemas and descriptions ─────────────────────────────────────────────────────────────


def test_the_two_tools_exist_with_the_right_shape():
    read, comment = TOOLS["mast_optical_layout"], TOOLS["mast_optical_layout_comment"]
    assert read.input_schema["properties"].keys() == {"layout_id", "image"} and "required" not in read.input_schema
    assert read.input_schema["properties"]["image"]["type"] == "boolean"
    assert comment.input_schema["required"] == ["layout_id", "text"]
    assert set(comment.input_schema["properties"]) == {"layout_id", "text", "component_id"}
    for tool in (read, comment):
        for name, spec in tool.input_schema["properties"].items():
            assert spec.get("description"), (tool.name, name)
    assert read.large and read.annotations["readOnlyHint"] is True and read.annotations["idempotentHint"] is True
    assert comment.annotations["readOnlyHint"] is False and comment.annotations["destructiveHint"] is False
    assert comment.annotations["openWorldHint"] is False


def test_the_descriptions_say_what_the_model_cannot_do():
    read, comment = TOOLS["mast_optical_layout"].description, TOOLS["mast_optical_layout_comment"].description
    assert "you cannot change it" in read and "mast_optical_layout_comment" in read and "image=true" in read
    assert "lower left" in read and "counter-clockwise" in read and "opens no device" in read
    assert "cannot edit the layout or resolve comments" in comment and "operator" in comment and "50 unresolved" in comment


def test_the_briefing_tool_offers_the_new_section():
    from mast_mcp import tools

    assert "optical_layouts" in tools.BRIEFING_SECTIONS
    assert "optical_layouts" in tools.TOOLS["mast_briefing"].input_schema["properties"]["sections"]["description"]


# ── reading ──────────────────────────────────────────────────────────────────────────────────


def test_without_an_id_the_layouts_are_listed():
    ctx = FakeCtx(get=LISTING)
    out = call("mast_optical_layout", ctx)
    assert ctx.fetched == [("/optical-layouts", None)] and not out.is_error and not out.images
    assert out.text.splitlines()[0] == "2 optical layout(s). Read one with layout_id."
    assert '- shg "SHG excitation": 17 components, 2 open comment(s), revision 12' in out.text
    assert "- old: unreadable (bad json)" in out.text


def test_an_empty_installation_says_so():
    out = call("mast_optical_layout", FakeCtx(get={"layouts": []}))
    assert out.text.startswith("No optical layouts have been drawn on this MAST yet.") and not out.is_error


def test_with_an_id_the_description_comes_first_and_then_the_geometry():
    ctx = FakeCtx(get=FULL)
    out = call("mast_optical_layout", ctx, layout_id="shg")
    assert ctx.fetched == [("/optical-layouts/shg", None)] and not out.images and not out.is_error
    lines = out.text.splitlines()
    assert lines[0].startswith("Optical layout shg: 1 components, revision 12, 1 open comment(s). You cannot change it;")
    assert any(ln.startswith("{") and '"layout_id":"shg"' in ln for ln in lines)
    payload = json.loads(next(ln for ln in lines if ln.startswith("{")))
    assert payload["components"] == [{"id": "m1", "type": "mirror", "label": "M1", "x_mm": 112.5, "y_mm": 62.5,
                                      "angle_deg": 135.0, "aperture_mm": 25.4}]
    assert payload["open_comments"] == 1 and payload["table"] == {"width_mm": 1200.0}
    assert "L1 laser (532 nm)" in out.text and "-> M1 mirror" in out.text, "the narrative is there in full"
    assert "annotations" not in out.text, "the raw annotations stay out; the description carries them"


def test_the_id_is_quoted_in_the_url():
    ctx = FakeCtx(get=FULL)
    call("mast_optical_layout", ctx, layout_id="a/b c")
    assert ctx.fetched[0][0] == "/optical-layouts/a%2Fb%20c"


def test_the_picture_is_attached_when_asked_for():
    ctx = FakeCtx(get=FULL)
    out = call("mast_optical_layout", ctx, layout_id="shg", image=True)
    assert ctx.client.requests == [("GET", "/optical-layouts/shg/render.png", {"width": 1000, "fit": "content"})]
    assert not out.is_error and len(out.images) == 1
    img = out.images[0]
    assert img["mime"] == "image/png" and base64.b64decode(img["data_b64"]) == PNG and "shg" in img["label"] and "revision 12" in img["label"]
    assert "The picture follows" in out.text and "yellow ring" in out.text and "red cross" in out.text
    for flag in ("true", "1", "yes", True):
        c = FakeCtx(get=FULL)
        assert len(call("mast_optical_layout", c, layout_id="shg", image=flag).images) == 1
    for flag in (False, "false", None, ""):
        c = FakeCtx(get=FULL)
        assert not call("mast_optical_layout", c, layout_id="shg", image=flag).images and not c.client.requests


def test_a_picture_that_cannot_be_fetched_does_not_lose_the_text():
    out = call("mast_optical_layout", FakeCtx(get=FULL, client=FakeClient(png=b"<html>", content_type="text/html")), layout_id="shg", image=True)
    assert not out.images and not out.is_error and "could not be fetched" in out.text and "L1 laser" in out.text
    with pytest.raises(MastError):
        call("mast_optical_layout", FakeCtx(get=FULL, client=FakeClient(png=b"", status=404, content_type="application/json")), layout_id="shg", image=True)


def test_a_reply_that_is_not_a_layout_is_shown_as_it_is():
    out = call("mast_optical_layout", FakeCtx(get={"unexpected": True}), layout_id="shg")
    assert out.text.startswith("Optical layout shg") and "unexpected" in out.text


def test_a_bad_argument_is_refused_before_any_request():
    ctx = FakeCtx(get=FULL)
    with pytest.raises(ArgError):
        call("mast_optical_layout", ctx, layout_id="shg", image="maybe")
    assert ctx.fetched == [] and not ctx.client.requests


# ── commenting ───────────────────────────────────────────────────────────────────────────────


def test_a_comment_is_posted_to_the_layout_with_nothing_else():
    ctx = FakeCtx()
    out = call("mast_optical_layout_comment", ctx, layout_id="shg", text="M2 looks misaligned.\nCan you check?", component_id="m2")
    assert ctx.posted == [("/optical-layouts/shg/comments", {"text": "M2 looks misaligned.\nCan you check?", "component_id": "m2"})]
    assert not out.is_error and "Comment c3 added to optical layout shg (about m2)." in out.text and "you cannot" in out.text
    ctx = FakeCtx()
    call("mast_optical_layout_comment", ctx, layout_id="shg", text="plain")
    assert ctx.posted == [("/optical-layouts/shg/comments", {"text": "plain"})]


def test_a_comment_needs_a_layout_and_a_text():
    for args in ({"text": "x"}, {"layout_id": "shg"}, {"layout_id": "shg", "text": "   "}, {"layout_id": "  ", "text": "x"}):
        ctx = FakeCtx()
        with pytest.raises(ArgError):
            call("mast_optical_layout_comment", ctx, **args)
        assert ctx.posted == []


def test_the_tools_cannot_be_given_a_name_a_resolution_or_an_edit():
    """Unknown arguments are not sent: only the documented ones reach the request."""
    ctx = FakeCtx()
    call("mast_optical_layout_comment", ctx, layout_id="shg", text="x", by="ana", resolved=True, layout={"id": "shg"})
    assert ctx.posted == [("/optical-layouts/shg/comments", {"text": "x"})]
