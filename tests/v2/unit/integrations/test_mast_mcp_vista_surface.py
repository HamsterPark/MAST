"""The VISTA tool text the bridge offers is a faithful copy of ``mast.vista``.

``mast_mcp`` must not import MASTv2 (it runs on a bare embedded Python), so
``mast_mcp/vista_surface.py`` copies the descriptions and input schemas of the VISTA
tools. These tests compare the copy with its source, so that a change on either side
shows up here, not as a model that reads one thing while the server checks another:

* input schemas are equal, apart from the argument descriptions the bridge adds (where
  VISTA has none) and the tool renames (``act`` is ``mast_act`` and so on);
* descriptions are equal after the same renames (inspect also says how Codex shows images);
* every action line of ``mast_act`` is ``ActionSpec.describe()`` of that action.

The notes descriptions are rewritten for MAST's workspaces on purpose and are not compared.
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

import copy

import pytest

pytest.importorskip("numpy")

from mast.vista import actions, frames, measure, notes, protocol, viewtools
from mast_mcp import vista_surface as vs
from mast_mcp.tools import TOOLS

RENAMES = (("the coordinates act accepts", "the coordinates mast_act accepts"),
           ("so read_values and inspect work on it",
            "so mast_read_values and mast_inspect work on it"))
#: Arguments the bridge describes where VISTA does not (MCP clients show every argument).
ADDED_DESCRIPTIONS = {"mast_inspect": ("views",), "mast_read_values": ("views",),
                      "mast_write_guide": ("content",), "mast_write_working": ("content",)}
NOTES = {"mast_read_guide", "mast_write_guide", "mast_read_working", "mast_write_working"}


def _vista_tools() -> dict:
    tools = {"mast_inspect": viewtools.make_inspect_tool(vs.DISPLAY_MAX, vs.GRID),
             "mast_read_values": viewtools.make_read_values_tool(vs.DISPLAY_MAX)}
    tools.update({f"mast_{name}": tdef
                  for name, (tdef, _) in measure.measure_tools(vs.DISPLAY_MAX).items()})
    tools.update({f"mast_{name}": tdef for name, tdef in notes.NOTES_TOOLS.items()})
    return tools


VISTA_TOOLS = _vista_tools()


def _renamed(obj):
    if isinstance(obj, str):
        for old, new in RENAMES:
            obj = obj.replace(old, new)
        return obj
    if isinstance(obj, dict):
        return {k: _renamed(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_renamed(v) for v in obj]
    return obj


def _bridge_schema(name: str) -> dict:
    schema = copy.deepcopy(TOOLS[name].input_schema)
    for prop in ADDED_DESCRIPTIONS.get(name, ()):
        del schema["properties"][prop]["description"]
    return schema


@pytest.mark.parametrize("name", sorted(VISTA_TOOLS))
def test_input_schemas_match_mast_vista(name):
    assert _bridge_schema(name) == _renamed(VISTA_TOOLS[name].input_schema)


@pytest.mark.parametrize("name", sorted(set(VISTA_TOOLS) - NOTES))
def test_descriptions_match_mast_vista(name):
    expected = _renamed(VISTA_TOOLS[name].description)
    if name == "mast_inspect":
        expected += " " + vs.CODEX_IMAGE
    assert TOOLS[name].description == expected


@pytest.mark.parametrize("name", sorted(VISTA_TOOLS))
def test_titles_and_hints_match_mast_vista(name):
    tdef, hints = VISTA_TOOLS[name], TOOLS[name].annotations
    assert TOOLS[name].title == tdef.title
    assert (hints["readOnlyHint"], hints["destructiveHint"], hints["idempotentHint"]) == (
        tdef.read_only, tdef.destructive, tdef.idempotent)


def test_every_action_is_described_as_mast_vista_describes_it():
    assert vs.ACTION_NAMES == tuple(actions.ACTIONS)
    assert [line for _, line in vs.ACTION_LINES] == [s.describe() for s in actions.ACTIONS.values()]
    assert vs.GRID_PHRASE == actions.GRID_PHRASE
    description = TOOLS["mast_act"].description
    assert all(line in description for _, line in vs.ACTION_LINES)
    assert (actions.GRID_PHRASE in description) is vs.GRID


def test_mast_act_keeps_vistas_arguments():
    vista = actions.act_schema(list(actions.ACTIONS))
    ours = TOOLS["mast_act"].input_schema
    for key in ("action", "params", "note"):
        assert ours["properties"][key] == vista["properties"][key]
    assert set(vista["required"]) < set(ours["required"])
    assert ours["additionalProperties"] is vista["additionalProperties"] is False


def test_limits_and_vocabulary_match_mast_vista():
    assert vs.DISPLAY_MAX == protocol.DISPLAY_MAX_DEFAULT
    assert (vs.MAX_INSPECT_VIEWS, vs.MAX_READ_VIEWS, vs.MAX_READ_SAMPLES, vs.MAX_QUESTION_CHARS,
            vs.MAX_LABEL_CHARS, vs.MAX_GUIDE_CHARS, vs.MAX_WORKING_CHARS) == (
        protocol.MAX_INSPECT_VIEWS, protocol.MAX_READ_VIEWS, protocol.MAX_READ_SAMPLES,
        protocol.MAX_QUESTION_CHARS, protocol.MAX_LABEL_CHARS, protocol.MAX_GUIDE_CHARS,
        protocol.MAX_WORKING_CHARS)
    assert vs.DIRECTIONS == frames.DIRECTIONS and vs.FLATTEN_MODES == frames.FLATTEN_MODES
    assert vs.GRID_COLOUR_NAME == frames.GRID_COLOUR_NAME
    assert vs.FRAME_ID_PATTERN == viewtools.FRAME_ID_PATTERN and vs.MAX_BLOBS == measure.MAX_BLOBS
