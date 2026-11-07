"""mast_ask_operator / mast_operator_reply / mast_wait with structured operator prompts.

The handlers are driven with a stand-in call context that records what would be sent to MAST
(the real server end is covered by ``tests/v2/unit/api_ext/test_ext_operator_mcp_e2e.py``). The
MCP server is stdlib-only (``test_mast_mcp_stdlib_only``): nothing here may pull in a package.
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

import json

import pytest

from mast_mcp import tools
from mast_mcp.tools import ArgError, TOOLS, as_output


class FakeCtx:
    def __init__(self, post=None, get=None):
        self.posted: list[tuple[str, dict]] = []
        self.fetched: list[str] = []
        self._post, self._get = post, get

    def post(self, path, body, **kw):
        self.posted.append((path, body))
        return self._post if self._post is not None else {"ok": True, "request": {
            "id": "op-0001-abc123", "status": "pending", "structured": True}}

    def get(self, path, query=None, **kw):
        self.fetched.append(path)
        return self._get


def call(name, ctx, **args):
    return as_output(TOOLS[name].handler(ctx, args))


# ── the schema ──────────────────────────────────────────────────────────────


def test_ask_operator_keeps_its_old_required_field_and_adds_optional_ones():
    schema = TOOLS["mast_ask_operator"].input_schema
    assert schema["required"] == ["message"]
    props = schema["properties"]
    assert {"message", "kind", "title", "options", "number_min", "number_max", "unit", "attest_slots",
            "severity", "timeout_s"} <= set(props)
    assert props["options"]["type"] == "array" and props["options"]["items"]["required"] == ["id"]
    assert props["severity"]["enum"] == ["info", "action", "safety"]
    assert props["attest_slots"]["items"] == {"type": "string"}
    for name, spec in props.items():
        assert spec.get("description"), name


def test_the_description_says_what_the_agent_cannot_do():
    d = TOOLS["mast_ask_operator"].description
    assert "only the operator can" in d and "operator_reply" in d
    assert "confirm" in d and "choice" in d and "attest" in d


# ── asking ──────────────────────────────────────────────────────────────────


def test_a_free_text_request_is_sent_as_it_always_was():
    ctx = FakeCtx(post={"ok": True, "request": {"id": "r-1", "status": "pending"}})
    out = call("mast_ask_operator", ctx, message="Please change the sample.")
    assert ctx.posted == [("/requests", {"message": "Please change the sample."})]
    assert not out.is_error and 'mast_operator_reply(request_id="r-1")' in out.text
    assert "operator_reply" not in out.text.split("mast_operator_reply")[1], "no wait hint for free text"
    ctx = FakeCtx(post={"ok": True, "request": {"id": "r-2", "status": "pending"}})
    call("mast_ask_operator", ctx, message="x", kind="action")
    assert ctx.posted[0][1] == {"message": "x", "kind": "action"}


def test_a_structured_prompt_carries_every_field():
    ctx = FakeCtx()
    out = call("mast_ask_operator", ctx, message="Which plate is in?", kind="choice", title="Plate",
               options=[{"id": "hwp_532", "label": "532 nm"}, {"id": "hwp_1064"}],
               severity="safety", timeout_s=900)
    path, body = ctx.posted[0]
    assert path == "/requests"
    assert body == {"message": "Which plate is in?", "kind": "choice", "title": "Plate",
                    "options": [{"id": "hwp_532", "label": "532 nm"}, {"id": "hwp_1064", "label": "hwp_1064"}],
                    "severity": "safety", "timeout_s": 900.0}
    assert not out.is_error
    assert 'mast_operator_reply(request_id="op-0001-abc123")' in out.text
    assert '"type": "operator_reply", "request_id": "op-0001-abc123"' in out.text
    assert "Only the operator can answer it" in out.text


def test_numbers_slots_and_units():
    ctx = FakeCtx()
    call("mast_ask_operator", ctx, message="Analyzer angle?", kind="number", number_min=0,
         number_max="360", unit="deg")
    assert ctx.posted[0][1] == {"message": "Analyzer angle?", "kind": "number", "number_min": 0.0,
                                "number_max": 360.0, "unit": "deg"}
    ctx = FakeCtx()
    call("mast_ask_operator", ctx, message="Sign off", kind="attest",
         attest_slots="excitation_waveplate, analyzer_optic", options='["hwp_532", "hwp_1064"]')
    body = ctx.posted[0][1]
    assert body["attest_slots"] == ["excitation_waveplate", "analyzer_optic"]
    assert body["options"] == [{"id": "hwp_532", "label": "hwp_532"}, {"id": "hwp_1064", "label": "hwp_1064"}]
    ctx = FakeCtx()
    call("mast_ask_operator", ctx, message="x", kind="number", number_min=0, number_max=0)
    assert ctx.posted[0][1]["number_min"] == 0.0 and ctx.posted[0][1]["number_max"] == 0.0, "zero is a value"


@pytest.mark.parametrize("args", [
    {"options": "not json"}, {"options": {"id": "a"}}, {"options": [{"label": "no id"}]},
    {"options": [3]}, {"severity": "loud"}, {"number_min": "low"}, {"timeout_s": True},
])
def test_bad_arguments_are_refused_before_anything_is_sent(args):
    ctx = FakeCtx()
    with pytest.raises(ArgError):
        call("mast_ask_operator", ctx, message="x", kind="choice", **args)
    assert ctx.posted == []


def test_the_servers_refusal_is_shown_as_an_error():
    ctx = FakeCtx(post={"error": "invalid_prompt", "detail": "a choice prompt needs at least two options"})
    out = call("mast_ask_operator", ctx, message="x", kind="choice")
    assert out.is_error and "at least two options" in out.text


# ── reading ─────────────────────────────────────────────────────────────────

ANSWERED = {"request": {"id": "op-0001-abc123", "status": "done", "prompt_status": "answered",
                        "note": "the thick one", "path": "", "structured": True,
                        "answer": {"status": "answered", "choice": "hwp_1064", "value": "hwp_1064",
                                   "note": "the thick one", "answered_by": "alice", "ok": True}}}


def test_a_pending_prompt_reads_as_pending():
    out = call("mast_operator_reply", FakeCtx(get={"request": {"id": "op-1", "status": "pending"}}),
               request_id="op-1")
    assert "still pending" in out.text and not out.is_error


def test_the_structured_answer_is_read_back_in_words_and_in_full():
    ctx = FakeCtx(get=ANSWERED)
    out = call("mast_operator_reply", ctx, request_id="op-0001-abc123")
    assert ctx.fetched == ["/requests/op-0001-abc123"]
    first = out.text.splitlines()[0]
    assert "done" in first and "answered" not in first.split("done")[0]
    assert "choice 'hwp_1064'" in first and "by alice" in first and "the thick one" in first
    assert "value" not in first, "a value equal to the choice is not said twice"
    assert json.dumps("hwp_1064") in out.text and '"answered_by": "alice"' in out.text or "alice" in out.text


def test_a_number_answer_and_the_other_endings():
    rec = {"request": {"id": "op-2", "status": "done", "prompt_status": "answered",
                       "answer": {"choice": None, "value": 47.5, "answered_by": "bob"}}}
    assert "value 47.5" in call("mast_operator_reply", FakeCtx(get=rec), request_id="op-2").text.splitlines()[0]
    for ps in ("timeout", "cancelled", "dismissed"):
        rec = {"request": {"id": "op-3", "status": "dismissed", "prompt_status": ps, "note": "why",
                           "answer": {"status": ps, "choice": None, "value": None, "answered_by": ""}}}
        line = call("mast_operator_reply", FakeCtx(get=rec), request_id="op-3").text.splitlines()[0]
        assert "dismissed" in line and ps in line and "operator's note: why" in line


def test_a_wishlist_reply_reads_as_before():
    rec = {"request": {"id": "r-1", "status": "done", "note": "here", "path": "D:/data/au111"}}
    line = call("mast_operator_reply", FakeCtx(get=rec), request_id="r-1").text.splitlines()[0]
    assert line == "Request r-1: done; operator's note: here; attached: D:/data/au111"


# ── waiting and jobs ────────────────────────────────────────────────────────


def test_a_fired_operator_reply_wait_says_what_the_operator_answered():
    answer = {"fired": [{"index": 0, "type": "operator_reply", "event": {
        "request_id": "op-0001-abc123", "status": "done", "structured": True, "prompt_status": "answered",
        "choice": "hwp_1064", "value": "hwp_1064", "answered_by": "alice"}}], "timed_out": False,
        "waited_s": 12.0, "cursor": "c1.aaaaaaaa.1.1"}
    text = tools.wait_output(answer).text.splitlines()[0]
    assert "operator_reply (condition 0, request op-0001-abc123: answered, choice 'hwp_1064')" in text
    number = {"fired": [{"index": 1, "type": "operator_reply", "event": {
        "request_id": "op-2", "structured": True, "prompt_status": "answered", "choice": None, "value": 47.5}}],
        "waited_s": 3.0}
    assert "request op-2: answered, value 47.5" in tools.wait_output(number).text
    timed_out = {"fired": [{"index": 0, "type": "operator_reply", "event": {
        "request_id": "op-3", "structured": True, "prompt_status": "timeout", "status": "dismissed"}}]}
    assert "request op-3: timeout" in tools.wait_output(timed_out).text


def test_the_wait_text_for_other_conditions_is_unchanged():
    out = tools.wait_output({"fired": [{"index": 0, "type": "job", "event": {"job_id": "j_9"}}],
                             "waited_s": 32.0})
    assert out.text.startswith("mast_wait: job (condition 0) fired after 32 s.")
    wishlist = {"fired": [{"index": 0, "type": "operator_reply", "event": {"request_id": "r-1", "status": "done"}}]}
    assert "operator_reply (condition 0, request r-1)" in tools.wait_output(wishlist).text


def test_a_running_job_says_when_it_waits_for_the_operator():
    view = {"job_id": "j_000000000001", "skill": "SHGExcitationScan", "state": "running", "elapsed_s": 40.0,
            "waiting_operator": {"prompt_id": "op-0004-ffeedd", "kind": "choice",
                                 "title": "SwapPlate: your decision is needed", "since": "2026-11-03T05:00:00+00:00"}}
    summary, failed = tools.job_summary(view)
    assert not failed and "waiting for the operator (prompt op-0004-ffeedd" in summary
    assert "SwapPlate: your decision is needed" in summary and "only the operator can answer" in summary
    plain, _ = tools.job_summary({**view, "waiting_operator": None})
    assert "waiting for the operator" not in plain
    done, _ = tools.job_summary({**view, "state": "succeeded", "result": {"summary": "ok"}})
    assert "waiting for the operator" not in done


def test_the_server_is_still_standard_library_only():
    """The structured arguments are parsed with json and re only."""
    import ast

    src = (_SERVER_DIR / "mast_mcp" / "tools.py").read_text(encoding="utf-8")
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import)}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.ImportFrom) and not n.level}
    assert imported <= set(sys.stdlib_module_names) | {"mast_mcp", ""}, imported - set(sys.stdlib_module_names)
