"""Only the operator can answer a prompt or write the optical state — pinned structurally.

An agent that could answer its own question, or register "laser area safe", would turn every
safeguard built on those words into decoration (``api/ext/agent_tools.py`` OPERATOR_ONLY is the
precedent: an agent must never attest a physical event it cannot see). So this file does not
test a behaviour that could be right today and wrong after the next refactor; it pins *where the
capability lives*:

1. the external surface (``/api/ext/v1``) has no route that answers, dismisses or writes optical
   state, and the only request routes are the ones that ask, read and wait;
2. the MCP tool table has no such tool and no tool talks to ``/operator``;
3. no skill writes the optical state or answers a prompt;
4. nothing in the source tree calls ``set_by_operator`` / ``clear_by_operator`` /
   ``prompts.answer`` / ``prompts.dismiss`` except the operator routes and the package itself
   (an AST scan, with positive controls so a scanner gone blind turns red).
"""
from __future__ import annotations

import ast
import re
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[4]
_SERVER_DIR = _REPO / "integrations" / "claude-code" / "server"
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from mast.operator_hitl import optical_state, prompts  # noqa: E402

#: the only places allowed to change what an operator attested
_ALLOWED = ("MASTv2/mast/operator_hitl/", "MASTv2/mast/api/routes/operator.py")
_SCAN_ROOTS = ("MASTv2/mast", "integrations", "contrib", "scripts", "mast2_launcher.py")
_STORE_WRITES = {"set_by_operator", "clear_by_operator"}
_PROMPT_MODULES = {"prompts", "op_prompts", "_prompts", "operator_prompts"}
_ANSWERING = {"answer", "dismiss"}


# ── the scanner ─────────────────────────────────────────────────────────────


def violations(source: str) -> list[str]:
    """What in *source* changes an operator's attestation (see the module docstring)."""
    found: list[str] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")           # other files' escape sequences are not our business
        tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            fn = node.func
            if fn.attr in _STORE_WRITES:
                found.append(f"line {node.lineno}: .{fn.attr}(...)")
            elif (fn.attr in _ANSWERING and isinstance(fn.value, ast.Name)
                  and fn.value.id in _PROMPT_MODULES):
                found.append(f"line {node.lineno}: {fn.value.id}.{fn.attr}(...)")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mast.operator_hitl"):
            for alias in node.names:
                if alias.name in _ANSWERING | {"FakeOperator"}:
                    found.append(f"line {node.lineno}: imports {alias.name}")
    return found


def test_the_scanner_sees_what_it_is_looking_for():
    """A guard that cannot fail guards nothing: each shape must be flagged."""
    assert violations("store.set_by_operator('laser_area_safe', True, by='x', source='y')")
    assert violations("get_store().clear_by_operator('objective', by='x', source='y')")
    assert violations("prompts.answer(pid, by='x', source='y', choice='yes')")
    assert violations("op_prompts.dismiss(pid, by='x', source='y')")
    assert violations("from mast.operator_hitl.prompts import answer")
    assert violations("from mast.operator_hitl import prompts\nfrom mast.operator_hitl.prompts import FakeOperator")
    assert not violations("prompts.post(p)\nprompts.ask(p)\nprompts.get(pid)\nstore.get('x')\nstore.snapshot()")
    assert not violations("reply.answer(1)\nsession.dismiss()")      # other objects' methods


def _sources():
    for root in _SCAN_ROOTS:
        base = _REPO / root
        if base.is_file():
            yield base
        elif base.is_dir():
            yield from (p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def test_only_the_operator_routes_and_the_package_change_attestations():
    scanned, bad = 0, []
    for path in _sources():
        rel = path.relative_to(_REPO).as_posix()
        scanned += 1
        if rel.startswith(_ALLOWED):
            continue
        try:
            src = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        try:
            found = violations(src)
        except SyntaxError:
            continue
        bad += [f"{rel}: {v}" for v in found]
    assert scanned > 500, f"the scan saw only {scanned} files — the roots moved?"
    assert not bad, "code outside the operator routes changes what an operator attested:\n  " + "\n  ".join(bad)


def test_the_allowed_places_really_do_use_those_calls():
    """And the allow-list is not stale: each allowed place is where the capability lives."""
    routes = (_REPO / "MASTv2/mast/api/routes/operator.py").read_text(encoding="utf-8")
    assert violations(routes), "the operator routes no longer answer / write? update the allow-list"
    pkg = (_REPO / "MASTv2/mast/operator_hitl/prompts.py").read_text(encoding="utf-8")
    assert violations(pkg)


# ── the external surface ────────────────────────────────────────────────────


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


FORBIDDEN_WORDS = ("answer", "dismiss", "optical", "attest", "operator", "slot", "sign")
#: the optical-layout viewer (docs/v2/design/optical_layout.md) is "optical" by name only: an agent reads the
#: drawing of the table and may comment; it cannot change the drawing or resolve a comment, which
#: tests/v2/unit/optical_layout/test_layout_ext_readonly.py pins (routes, source and behaviour)
LAYOUT_PATH = "/optical-layouts"
LAYOUT_TOOLS = ("mast_optical_layout", "mast_optical_layout_comment")


def test_the_ext_surface_has_no_route_that_answers_or_writes_optical_state(ext_app):
    seen = []
    for r in _walk(ext_app.routes):
        path = getattr(r, "path", None)
        if path:
            seen.append((sorted(getattr(r, "methods", None) or ()), path))
    assert len(seen) > 30, "the route walk saw too little"
    offenders = [(m, p) for m, p in seen if any(w in p.lower() for w in FORBIDDEN_WORDS)
                 and not p.startswith(LAYOUT_PATH)]
    assert not offenders, offenders
    spec = ext_app.openapi()["paths"]
    assert not [p for p in spec if any(w in p.lower() for w in FORBIDDEN_WORDS)
                and not p.startswith(LAYOUT_PATH)]


def test_the_ext_request_routes_ask_read_and_nothing_else(ext_app):
    spec = ext_app.openapi()["paths"]
    mine = sorted((m.upper(), p) for p, ops in spec.items() for m in ops if p.startswith("/requests"))
    assert mine == [("GET", "/requests"), ("GET", "/requests/{request_id}"), ("POST", "/requests")]


def test_an_agent_cannot_reach_an_answer_or_a_slot_through_the_ext_app(ext_app, memory_backends):
    from fastapi.testclient import TestClient

    pid = prompts.post(prompts.OperatorPrompt(kind="confirm", title="Is it safe?", agent_id="ext:tester"))
    c = TestClient(ext_app)
    h = {"X-MAST-Actor": "tester"}
    body = {"by": "tester", "choice": "yes", "value": True}
    for method, path in (("post", f"/requests/{pid}/answer"), ("put", f"/requests/{pid}"),
                         ("post", f"/requests/{pid}/dismiss"), ("put", "/optical-state/laser_area_safe"),
                         ("post", "/operator/prompts/%s/answer" % pid),
                         ("put", "/api/operator/optical-state/laser_area_safe")):
        r = getattr(c, method)(path, json=body, headers=h)
        assert r.status_code in (404, 405), (method, path, r.status_code, r.text)
    assert prompts.get(pid).status == "open"
    assert optical_state.get_store().get("laser_area_safe") is None
    assert optical_state.get_store().history == []


# ── the MCP tools ───────────────────────────────────────────────────────────


def test_no_mcp_tool_answers_or_writes_optical_state():
    from mast_mcp.tools import TOOLS

    bad = [n for n in TOOLS if re.search(r"answer|dismiss|attest|optical|slot|sign", n) and n not in LAYOUT_TOOLS]
    assert bad == []
    assert {n for n in TOOLS if "operator" in n} == {"mast_ask_operator", "mast_operator_reply"}
    assert TOOLS["mast_operator_reply"].annotations["readOnlyHint"] is True
    assert TOOLS["mast_ask_operator"].annotations["readOnlyHint"] is False
    # no tool takes the operator's name or an answer as an argument
    for name, tool in TOOLS.items():
        props = set(tool.input_schema.get("properties", {}))
        assert not props & {"by", "answered_by", "choice", "answer"}, (name, props)


def test_no_mcp_tool_talks_to_the_operator_api():
    src = (_SERVER_DIR / "mast_mcp" / "tools.py").read_text(encoding="utf-8")
    paths = []
    for node in ast.walk(ast.parse(src)):
        # ctx.get("/requests") ... : the call context is what every tool talks to MAST through
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "ctx"
                and node.func.attr in {"get", "post", "put", "delete", "request"}
                and node.args and isinstance(node.args[0], (ast.Constant, ast.JoinedStr))):
            first = node.args[0]
            paths.append(first.value if isinstance(first, ast.Constant) else ast.unparse(first))
    paths = [p for p in paths if isinstance(p, str) and "/" in p]
    assert len(paths) > 15, f"the scan found too few HTTP calls in tools.py: {paths}"
    # an f-string path is read back with its quote and its ``f`` ( f'/optical-layouts/{seg}' )
    assert not [p for p in paths if re.search(r"operator|optical|answer|dismiss", p)
                and not re.match(r"""f?['"]?""" + re.escape(LAYOUT_PATH) + r"""(?:/|['"]|$)""", p)], paths
    for module in (_SERVER_DIR / "mast_mcp").glob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert "/api/operator" not in text, module.name


# ── skills ──────────────────────────────────────────────────────────────────


def test_no_skill_writes_the_optical_state_or_answers():
    from mast.core.registry import SkillRegistry

    reg = SkillRegistry()
    reg.discover("mast.skills.builtins")
    names = {m.name for m in reg.list_skills()}
    assert {"AskOperator", "ReadOpticalState"} <= names
    suspicious = [n for n in names
                  if re.search(r"(Set|Write|Register|Attest|Sign|Answer|Confirm|Clear|Dismiss).*"
                               r"(OpticalState|Attestation|Operator|LaserArea|Slot)", n)]
    assert suspicious == [], suspicious
    meta = {m.name: m for m in reg.list_skills()}
    assert meta["ReadOpticalState"].safety_level.value == "auto"
    assert meta["AskOperator"].safety_level.value == "confirm"
