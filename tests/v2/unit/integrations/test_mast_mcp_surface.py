"""The model-facing surface of the MAST MCP server changes only on purpose.

``SURFACE`` pins everything a model reads before its first call: the tool list exactly as
``tools/list`` sends it (names, order, titles, descriptions, schemas, annotations,
``_meta``) and the server instructions. For an agent driving the instrument these words
are part of the experiment, as in VISTA-SPM (its ``tests/test_surface.py``).

If this test fails after a change you made on purpose, put the new value from the failure
message into ``SURFACE`` and say in the commit message what the model now reads
differently. If you did not mean to change what the model reads, find out what did.
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

import hashlib
import json

from mast_mcp.config import load_config
from mast_mcp.tools import INSTRUCTIONS, Toolbox

#: sha256 (first 16 hex digits) of the surface. Update it only on purpose (see above).
SURFACE = "260ccdc0cbbd2362"


def surface() -> str:
    tools = Toolbox(load_config({"MAST_URL": "http://127.0.0.1:9"})).list_tools()
    text = json.dumps(tools, sort_keys=True, ensure_ascii=False) + INSTRUCTIONS
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def test_the_model_facing_surface_is_pinned():
    got = surface()
    assert got == SURFACE, (
        f"The tools or instructions the model reads changed (surface {got}, pinned {SURFACE}). "
        "If that was on purpose, set SURFACE to the new value and describe the change in the "
        "commit message.")
