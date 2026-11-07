"""An agent reads the drawing of the bench and may comment; it cannot change it. Pinned structurally.

The layout records what is physically on the table - which only the operator knows (the principle of
``api/ext/agent_tools.py`` OPERATOR_ONLY and of the optical-state attestation). An agent that could
move a mirror in the drawing, rewrite a model number or resolve its own comment would turn every
question the drawing answers into decoration. So this file does not test one behaviour that could be
right today and wrong after the next refactor; it pins *where the capability lives*:

1. the external surface has exactly four layout routes - three GETs and one POST that adds a comment;
2. an agent that tries every other way to write (other methods, other paths, extra body fields,
   a name, a resolution) changes nothing: the files stay byte for byte as they were;
3. the source of the external surface does not call the store's ``save`` / ``archive`` /
   ``resolve_comment`` (an AST scan, with positive controls so a scanner gone blind turns red);
4. the MCP tools talk to exactly three layout URLs and none of them writes the drawing.

Also here, through the real app: the language of the description follows the request, and a browser
cannot press the external comment route.
"""
from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mast.optical_layout import store as S
from mast.optical_layout.model import Layout

from ._layouts import det, laser, layout, mirror

_REPO = Path(__file__).resolve().parents[4]
_SERVER_DIR = _REPO / "integrations" / "claude-code" / "server"
_EXT_DIR = _REPO / "MASTv2" / "mast" / "api" / "ext"
EXT = "/api/ext/v1"
AGENT = {"X-MAST-Actor": "claude", "X-MAST-Session": "s1"}
JSON = {"Content-Type": "application/json"}
FORBIDDEN_STORE_CALLS = {"save", "archive", "resolve_comment", "atomic_write"}


@pytest.fixture
def app(layout_root, tmp_path):
    from mast.api.app import create_app

    return create_app(dev_cors=False)


@pytest.fixture
def client(app):
    return TestClient(app, base_url="http://127.0.0.1:7862")


@pytest.fixture
def seeded(layout_root):
    store = S.get_store()
    store.save(Layout.model_validate(layout(
        [laser("las1", 50, 62.5, label="L1"), mirror("m1", 112.5, 62.5, 135, label="M1", model="BB1-E02"), det("d", 112.5, 300, 270)],
        lid="shg", title="SHG")), base_revision=0, by="ana")
    store.add_comment("shg", text="Please check M1.", by="ana", author="operator", component_id="m1")
    return store


def snapshot(root: Path) -> dict[str, str]:
    base = root / "config" / "optical_layouts"
    return {str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(base.rglob("*")) if p.is_file()}


# ── 1. the routes ────────────────────────────────────────────────────────────────────────────


def _walk(routes):
    for r in routes:
        yield r
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _walk(getattr(inner, "routes", []))
        sub = getattr(r, "app", None)
        if sub is not None and hasattr(sub, "routes") and not hasattr(r, "original_router"):
            yield from _walk(sub.routes)


@pytest.fixture(scope="module")
def ext_app():
    from mast.api.ext.app import create_ext_app
    from mast.api.ext.jobs import JobManager

    return create_ext_app(SimpleNamespace(), job_manager=JobManager("unused"))


def test_the_external_surface_has_exactly_these_layout_routes(ext_app):
    seen = sorted((m, r.path) for r in _walk(ext_app.routes) for m in (getattr(r, "methods", None) or ())
                  if "optical-layouts" in getattr(r, "path", ""))
    assert seen == [("GET", "/optical-layouts"), ("GET", "/optical-layouts/{layout_id}"),
                    ("GET", "/optical-layouts/{layout_id}/render.png"), ("POST", "/optical-layouts/{layout_id}/comments")]
    spec = ext_app.openapi()["paths"]
    mine = sorted((m.upper(), p) for p, ops in spec.items() if "optical" in p for m in ops)
    assert mine == [("GET", "/optical-layouts"), ("GET", "/optical-layouts/{layout_id}"),
                    ("GET", "/optical-layouts/{layout_id}/render.png"), ("POST", "/optical-layouts/{layout_id}/comments")]
    body = ext_app.openapi()["components"]["schemas"]["LayoutCommentBody"]
    assert sorted(body["properties"]) == ["component_id", "text"] and body.get("additionalProperties") is False


def test_the_writing_routes_exist_only_in_the_main_app_for_people():
    from mast.api.app import create_app

    spec = create_app(dev_cors=False).openapi()["paths"]
    writes = sorted((m.upper(), p) for p, ops in spec.items() if p.startswith("/api/optical-layouts") for m in ops if m != "get")
    assert ("PUT", "/api/optical-layouts/{layout_id}") in writes and ("POST", "/api/optical-layouts/{layout_id}/delete") in writes
    assert ("POST", "/api/optical-layouts/{layout_id}/comments/{comment_id}/resolve") in writes
    assert not [p for p in spec if p.startswith("/api/ext")], "the ext surface stays off the main spec"


# ── 2. an agent that tries ───────────────────────────────────────────────────────────────────


def test_every_other_way_an_agent_could_write_changes_nothing(client, seeded, layout_root):
    before = snapshot(layout_root)
    doc = Layout.model_validate(layout([laser("las1", 0, 0)], lid="shg", title="HACKED")).to_dict()
    attempts = [
        ("put", f"{EXT}/optical-layouts/shg", {"layout": doc, "base_revision": 1, "by": "claude"}),
        ("put", f"{EXT}/optical-layouts/shg", doc),
        ("patch", f"{EXT}/optical-layouts/shg", {"title": "HACKED"}),
        ("post", f"{EXT}/optical-layouts/shg", {"layout": doc}),
        ("delete", f"{EXT}/optical-layouts/shg", {}),
        ("post", f"{EXT}/optical-layouts/shg/delete", {"by": "claude", "base_revision": 1}),
        ("post", f"{EXT}/optical-layouts/shg/comments/c1/resolve", {"by": "claude"}),
        ("post", f"{EXT}/optical-layouts/shg/comments/c1", {"resolved": True}),
        ("put", f"{EXT}/optical-layouts/shg/comments/c1", {"resolved": True}),
        ("patch", f"{EXT}/optical-layouts/shg/comments/c1", {"resolved": True}),
        ("delete", f"{EXT}/optical-layouts/shg/comments/c1", {}),
        ("put", f"{EXT}/optical-layouts/shg/comments", {"text": "x"}),
        ("get", f"{EXT}/optical-layouts/shg/history", None),
        ("get", f"{EXT}/optical-layouts/shg/revisions/1", None),
        ("put", f"{EXT}/optical-layouts/shg/components/m1", {"angle_deg": 0}),
        ("post", f"{EXT}/optical-layouts", {"layout": doc, "base_revision": 0, "by": "claude"}),
        ("put", f"{EXT}/optical-layouts", {"layout": doc}),
    ]
    for method, path, body in attempts:
        kw = {"json": body} if body is not None else {}
        r = client.request(method.upper(), path, headers=AGENT, **kw)
        assert r.status_code in (404, 405), (method, path, r.status_code, r.text[:200])
    # the main app's own writing routes are not under the external prefix and are not reachable from it
    for method, path in (("PUT", f"{EXT}/api/optical-layouts/shg"), ("POST", f"{EXT}/api/optical-layouts/shg/comments/c1/resolve")):
        assert client.request(method, path, json={"by": "claude"}, headers=AGENT).status_code in (404, 405)
    assert snapshot(layout_root) == before, "an attempt changed a file"
    assert S.get_store().comments("shg")[0]["resolved"] is False


def test_nothing_in_a_comment_body_can_resolve_edit_or_sign_for_someone_else(client, seeded, layout_root):
    before = snapshot(layout_root)
    for extra in ({"resolved": True}, {"resolve": "c1"}, {"by": "ana"}, {"author": "operator"}, {"resolved_by": "ana"},
                  {"layout": {"id": "shg"}}, {"revision": 9}, {"annotations": {"model": "x"}}, {"x_mm": 5}):
        r = client.post(f"{EXT}/optical-layouts/shg/comments", json={"text": "hi", **extra}, headers=AGENT)
        assert r.status_code == 422 and r.json()["error"] == "invalid_request", (extra, r.status_code)
    assert snapshot(layout_root) == before
    ok = client.post(f"{EXT}/optical-layouts/shg/comments", json={"text": "hi"}, headers=AGENT)
    assert ok.status_code == 200 and ok.json()["comment"]["by"] == "ext:claude" and ok.json()["comment"]["author"] == "agent"
    after = snapshot(layout_root)
    changed = {k for k in after if before.get(k) != after[k]}
    assert changed == {"shg.comments.json"}, f"a comment touched more than the comments file: {changed}"
    layout_doc = json.loads((layout_root / "config" / "optical_layouts" / "shg.json").read_text(encoding="utf-8"))
    assert layout_doc["revision"] == 1 and layout_doc["title"] == "SHG" and layout_doc["components"][1]["annotations"]["model"] == "BB1-E02"


def test_a_read_changes_nothing_either(client, seeded, layout_root):
    before = snapshot(layout_root)
    for path in ("/optical-layouts", "/optical-layouts/shg", "/optical-layouts/shg/render.png", "/briefing"):
        assert client.get(EXT + path, headers=AGENT).status_code == 200, path
    assert snapshot(layout_root) == before


# ── the browser guard of the external surface covers the comment route ───────────────────────


def test_a_web_page_cannot_press_the_comment_route_or_read_the_drawing(client, seeded, layout_root):
    before = snapshot(layout_root)
    evil = {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}
    assert client.post(f"{EXT}/optical-layouts/shg/comments", headers=evil).status_code in (403, 415)
    r = client.post(f"{EXT}/optical-layouts/shg/comments", content=b'{"text": "x"}', headers={**evil, "Content-Type": "text/plain"})
    assert r.status_code in (403, 415)
    r = client.post(f"{EXT}/optical-layouts/shg/comments", json={"text": "x"}, headers={"Origin": "http://evil.example", "Host": "127.0.0.1:7862"})
    assert r.status_code == 403 and r.json()["error"] == "cross_origin_refused"
    assert client.post(f"{EXT}/optical-layouts/shg/comments", json={"text": "x"}).status_code == 200, "a program is fine"
    assert client.get(f"{EXT}/optical-layouts/shg", headers={"Host": "127.0.0.1:7862", "Sec-Fetch-Site": "cross-site",
                                                             "Sec-Fetch-Mode": "no-cors"}).status_code == 403
    assert client.post(f"{EXT}/optical-layouts/shg/comments", content=b"").status_code == 415
    assert len(S.get_store().comments("shg")) == 2 and snapshot(layout_root) != before


# ── language, through the real app ───────────────────────────────────────────────────────────


def test_the_description_is_in_the_language_of_the_request(client, seeded):
    en = client.get(f"{EXT}/optical-layouts/shg", headers={**AGENT, "X-MAST-Lang": "en"}).json()["summary"]
    zh = client.get(f"{EXT}/optical-layouts/shg", headers={**AGENT, "X-MAST-Lang": "zh"}).json()["summary"]
    assert en.startswith('Optical layout "SHG"') and "Beam paths (in the order along the beam):" in en and "Unresolved comments (1):" in en
    assert zh.startswith("光路图「SHG」") and "光路（沿光束的顺序）：" in zh and "未解决的评论（1 条）：" in zh
    assert "Please check M1." in en and "Please check M1." in zh


def test_error_texts_follow_the_language_too(client, layout_root):
    en = client.get(f"{EXT}/optical-layouts/nope", headers={**AGENT, "X-MAST-Lang": "en"}).json()
    zh = client.get(f"{EXT}/optical-layouts/nope", headers={**AGENT, "X-MAST-Lang": "zh"}).json()
    assert en["error"] == zh["error"] == "unknown_layout"
    assert "No such optical layout" in en["detail"] and "没有这张光路图" in zh["detail"]


# ── 3. the source ────────────────────────────────────────────────────────────────────────────


def violations(source: str) -> list[str]:
    """Calls that write the layout store: ``<x>.save(...)``, ``.archive(...)``, ``.resolve_comment(...)``,
    ``atomic_write(...)`` in a module that imports the layout store."""
    tree = ast.parse(source)
    imports_store = any(
        (isinstance(n, ast.ImportFrom) and ((n.module or "").startswith("mast.optical_layout") or any(a.name == "store" for a in n.names) and "optical_layout" in (n.module or "")))
        or (isinstance(n, ast.Import) and any("optical_layout" in a.name for a in n.names)) for n in ast.walk(tree))
    if not imports_store:
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
            if name in FORBIDDEN_STORE_CALLS:
                found.append(f"line {node.lineno}: {name}(...)")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mast.api.routes.optical_layout"):
            found.append(f"line {node.lineno}: imports the human routes")
    return found


def test_the_scanner_sees_what_it_is_looking_for():
    assert violations("from mast.optical_layout import store as layout_store\nlayout_store.get_store().save(lay, base_revision=0, by='x')")
    assert violations("from mast.optical_layout.store import get_store\nget_store().archive('a', base_revision=1, by='x')")
    assert violations("from mast.optical_layout import store\nstore.get_store().resolve_comment('a', 'c1', by='x')")
    assert violations("from mast.optical_layout.store import atomic_write\natomic_write(p, b'')")
    assert violations("from mast.api.routes.optical_layout import put_layout\nimport mast.optical_layout")
    assert not violations("from mast.optical_layout import store\nstore.get_store().load('a')\nstore.get_store().add_comment('a', text='x', by='b', author='agent')")
    assert not violations("doc_store().save(text='x')")                         # another store, in a module that does not import this one


def test_nothing_in_the_external_surface_writes_the_layout_store():
    files = sorted(_EXT_DIR.glob("*.py"))
    assert len(files) >= 12
    bad = {f.name: violations(f.read_text(encoding="utf-8")) for f in files}
    assert not {k: v for k, v in bad.items() if v}, bad
    assert "optical_layout" in (_EXT_DIR / "optical_layout.py").read_text(encoding="utf-8"), "the scan looked at the right module"
    importers = [f.name for f in files if "mast.optical_layout" in f.read_text(encoding="utf-8")]
    assert {"optical_layout.py", "overview.py"} <= set(importers), importers


def test_the_comment_route_hands_the_store_the_callers_name_and_the_agent_author():
    tree = ast.parse((_EXT_DIR / "optical_layout.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add_comment"]
    assert len(calls) == 1
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert kw["by"] == "caller.agent_id" and kw["author"] == "'agent'" and kw["max_open_for_author"] == "MAX_OPEN_COMMENTS_PER_AGENT"


# ── 4. the MCP tools ─────────────────────────────────────────────────────────────────────────


def _layout_strings_and_calls() -> tuple[set[str], list[tuple[str, str]]]:
    """Every string in tools.py that names a layout URL, and every ``ctx.<verb>(<url>)`` call on one."""
    tree = ast.parse((_SERVER_DIR / "mast_mcp" / "tools.py").read_text(encoding="utf-8"))
    strings: set[str] = set()
    calls: list[tuple[str, str]] = []
    parts = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}   # pieces of an f-string
    for node in ast.walk(tree):
        if isinstance(node, (ast.Constant, ast.JoinedStr)) and id(node) not in parts:
            text = ast.unparse(node)
            if "optical-layouts" in text and text.startswith(("'/", "f'/", '"/', 'f"/')):
                strings.add(text)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ctx" and node.args and "optical-layouts" in ast.unparse(node.args[0])):
            calls.append((node.func.attr, ast.unparse(node.args[0])))
    return strings, calls


def test_the_mcp_tools_read_and_add_a_comment_and_nothing_else():
    strings, calls = _layout_strings_and_calls()
    assert strings == {"'/optical-layouts'", "f'/optical-layouts/{seg}'", "f'/optical-layouts/{seg}/render.png'",
                       "f'/optical-layouts/{_seg(layout_id)}/comments'"}, strings
    assert sorted(calls) == sorted([("get", "'/optical-layouts'"), ("get", "f'/optical-layouts/{seg}'"),
                                    ("post", "f'/optical-layouts/{_seg(layout_id)}/comments'")]), calls
    src = (_SERVER_DIR / "mast_mcp" / "tools.py").read_text(encoding="utf-8")
    for word in ("/resolve", "optical-layouts/{seg}/delete", "history", "revisions"):
        assert not [ln for ln in src.splitlines() if "optical-layouts" in ln and word in ln], word


def test_the_mcp_tools_have_no_argument_that_names_a_person_or_a_resolution():
    sys.path.insert(0, str(_SERVER_DIR))
    try:
        from mast_mcp.tools import TOOLS
    finally:
        sys.path.pop(0)
    for name in ("mast_optical_layout", "mast_optical_layout_comment"):
        props = set(TOOLS[name].input_schema["properties"])
        assert not props & {"by", "author", "resolved", "resolve", "answer", "choice", "layout", "annotations", "base_revision"}, (name, props)
    assert TOOLS["mast_optical_layout"].annotations["readOnlyHint"] is True
    assert TOOLS["mast_optical_layout_comment"].annotations["readOnlyHint"] is False
    assert TOOLS["mast_optical_layout_comment"].annotations["destructiveHint"] is False
    assert not [n for n in TOOLS if "layout" in n and n not in ("mast_optical_layout", "mast_optical_layout_comment")]
