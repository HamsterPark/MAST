"""GUIDE.md / WORKING.md store and notes tools (mast.vista.notes)."""
from __future__ import annotations

import json
import threading

import pytest

from mast.vista.notes import (GUIDE_INITIAL, NOTES_TOOL_NAMES, NOTES_TOOLS, Notes, NotesError)
from mast.vista.protocol import MAX_GUIDE_CHARS, MAX_WORKING_CHARS, ToolDef


def _log(notes: Notes) -> list[dict]:
    return [json.loads(line) for line in notes.log_path.read_text(encoding="utf-8").splitlines()]


def test_initial_state(tmp_path):
    notes = Notes(tmp_path / "n")
    assert notes.read_guide() == GUIDE_INITIAL == "No reliable model yet.\n"
    assert notes.read_working() is None
    assert notes.working_saved_at() is None
    reply = notes.call("read_working", {})
    assert not reply.is_error and reply.text == "WORKING.md does not exist yet."
    reply = notes.call("read_guide", {})
    assert not reply.is_error and reply.text == GUIDE_INITIAL and reply.effect == "none"
    assert _log(notes)[0]["op"] == "init"


def test_writes_replace_strip_and_log_provenance(tmp_path):
    state = {"turn": 3}
    notes = Notes(tmp_path, provenance=lambda: dict(state))
    reply = notes.call("write_guide", {"content": "\n  # Tip\nsharp, Au(111) herringbone seen  \n\n"})
    assert not reply.is_error and reply.effect == "notes"
    assert json.loads(reply.text) == {"saved": "GUIDE.md", "chars": len("# Tip\nsharp, Au(111) herringbone seen\n")}
    assert notes.read_guide() == "# Tip\nsharp, Au(111) herringbone seen\n"
    state["turn"] = 7
    notes.call("write_working", {"content": "plan A"})
    notes.call("write_working", {"content": "plan B: 扫描 t0007.s0 again"})
    assert notes.read_working() == "plan B: 扫描 t0007.s0 again\n"
    saved = notes.working_saved_at()
    assert saved["provenance"] == {"turn": 7} and saved["file"] == "WORKING.md"
    ops = [(r["op"], r["file"]) for r in _log(notes)]
    assert ops == [("init", "GUIDE.md"), ("write", "GUIDE.md"), ("write", "WORKING.md"), ("write", "WORKING.md")]
    assert all("provenance" in r for r in _log(notes)[1:])
    snap = notes.snapshot()
    assert snap["working"].startswith("plan B") and snap["guide"].startswith("# Tip")


@pytest.mark.parametrize("name,limit", [("write_guide", MAX_GUIDE_CHARS), ("write_working", MAX_WORKING_CHARS)])
def test_limits_and_invalid_content(tmp_path, name, limit):
    notes = Notes(tmp_path)
    before_guide, before_working = notes.read_guide(), notes.read_working()
    for args, fragment in (({"content": "x" * (limit + 1)}, "limit"),
                           ({"content": "   \n"}, "empty"),
                           ({"content": 5}, "string"),
                           ({}, "needs 'content'"),
                           ({"content": "ok", "extra": 1}, "only 'content'")):
        reply = notes.call(name, args)
        assert reply.is_error, args
        assert fragment in json.loads(reply.text)["error"]
    assert notes.read_guide() == before_guide and notes.read_working() == before_working
    assert not notes.call(name, {"content": "x" * limit}).is_error   # exactly at the limit is fine


def test_direct_api_raises(tmp_path):
    notes = Notes(tmp_path)
    with pytest.raises(NotesError):
        notes.write_working("")
    with pytest.raises(NotesError):
        notes.write_guide("y" * (MAX_GUIDE_CHARS + 1))


def test_read_tools_reject_arguments_and_unknown_tool(tmp_path):
    notes = Notes(tmp_path)
    assert notes.call("read_guide", {"x": 1}).is_error
    assert notes.call("read_working", {"x": 1}).is_error
    assert notes.call("write_notes", {}).is_error
    assert notes.call("read_guide", "not a dict").is_error   # type: ignore[arg-type]


def test_archive_working(tmp_path):
    notes = Notes(tmp_path)
    assert notes.archive_working("nothing") is None
    notes.write_working("level 1 plan")
    target = notes.archive_working("new task phase/1")
    assert target is not None and target.parent == notes.archive_dir
    assert target.read_text(encoding="utf-8") == "level 1 plan\n"
    assert target.with_suffix(".provenance.json").exists()
    assert notes.read_working() is None and notes.working_saved_at() is None
    notes.write_working("level 2 plan")
    second = notes.archive_working("again")
    assert second != target and second.name.startswith("001_")
    assert [r["op"] for r in _log(notes)][-1] == "archive"


def test_reopen_keeps_content(tmp_path):
    Notes(tmp_path).write_guide("durable")
    again = Notes(tmp_path)
    assert again.read_guide() == "durable\n"
    assert sum(1 for r in _log(again) if r["op"] == "init") == 1


def test_tool_definitions():
    assert set(NOTES_TOOLS) == set(NOTES_TOOL_NAMES) == {"read_guide", "write_guide", "read_working", "write_working"}
    for name, tool in NOTES_TOOLS.items():
        assert isinstance(tool, ToolDef) and tool.name == name
        mcp = tool.to_mcp()
        assert mcp["inputSchema"]["additionalProperties"] is False
        assert mcp["annotations"]["openWorldHint"] is False
        assert len(tool.description) < 2048
    assert NOTES_TOOLS["write_guide"].input_schema["properties"]["content"]["maxLength"] == MAX_GUIDE_CHARS
    assert NOTES_TOOLS["write_working"].input_schema["properties"]["content"]["maxLength"] == MAX_WORKING_CHARS
    assert NOTES_TOOLS["write_working"].input_schema["required"] == ["content"]
    assert NOTES_TOOLS["read_guide"].read_only and not NOTES_TOOLS["write_guide"].read_only
    assert "survives context replacement" in NOTES_TOOLS["write_working"].description
    assert "instrument, the sample and the tip" in NOTES_TOOLS["write_guide"].description


def test_concurrent_writes_are_serialized(tmp_path):
    notes = Notes(tmp_path)
    texts = [f"version {i} " + "z" * 500 for i in range(40)]
    threads = [threading.Thread(target=notes.write_working, args=(t,)) for t in texts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert notes.read_working() in {t.strip() + "\n" for t in texts}
    assert sum(1 for r in _log(notes) if r["op"] == "write") == 40


def test_provenance_failure_does_not_block_write(tmp_path):
    def broken() -> dict:
        raise RuntimeError("host busy")
    notes = Notes(tmp_path, provenance=broken)
    assert not notes.call("write_working", {"content": "still saved"}).is_error
    assert notes.read_working() == "still saved\n"
    assert "provenance_error" in _log(notes)[-1]["provenance"]


def test_handle_is_call(tmp_path):
    notes = Notes(tmp_path)
    assert Notes.handle is Notes.call
    assert not notes.handle("write_guide", {"content": "via handle"}).is_error
    assert notes.read_guide() == "via handle\n"
