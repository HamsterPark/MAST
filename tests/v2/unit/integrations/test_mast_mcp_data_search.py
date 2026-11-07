"""``mast_search_data``: the filters reach ``GET /data/search`` as given, and the summary says
what the model needs before it reads the JSON (how many, which frames, and when the search was
incomplete). The server side is tested in ``tests/v2/unit/api_ext/test_ext_data_search.py``."""
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

import threading

from mast_mcp.config import load_config
from mast_mcp.stdio_rpc import CallSignal
from mast_mcp.tools import Toolbox


class _Client:
    def __init__(self, answer: dict):
        self.answer = answer
        self.calls: list[tuple] = []

    def call(self, method, path, *, query=None, body=None, timeout=30.0):
        self.calls.append((method, path, dict(query or {})))
        return self.answer


def _run(args: dict, answer: dict):
    client = _Client(answer)
    box = Toolbox(load_config({"MAST_URL": "http://127.0.0.1:1"}), client=client)
    out = box.call_tool("mast_search_data", args, CallSignal(threading.Event()))
    return out, client.calls


def test_only_the_filters_given_are_sent():
    answer = {"items": [{"name": "trial_0002.sxm", "frame": "t0003.s0", "path": "D:/x/trial_0002.sxm"},
                        {"name": "Bias-Spectroscopy00018.dat", "path": "D:/x/b.dat"}],
              "total": 5, "index": {"available": True}, "live": {"truncated": False}}
    out, calls = _run({"ext": ["SXM", ".dat"], "experiment": "aca3d174", "bias_min_v": -2.5,
                       "size_max_nm": 10, "min_rating": 1, "since": "2026-10-15", "live": False},
                      answer)
    assert not out.is_error, out.text
    method, path, query = calls[0]
    assert (method, path) == ("GET", "/data/search")
    assert query == {"ext": ["sxm", "dat"], "experiment": "aca3d174", "bias_min_v": -2.5,
                     "size_max_nm": 10.0, "min_rating": 1, "since": "2026-10-15", "live": False,
                     "n": 50, "offset": 0, "order": "newest"}
    assert out.text.startswith("2 of 5 data record(s): trial_0002.sxm (t0003.s0), "
                               "Bias-Spectroscopy00018.dat. Download one with mast_fetch")


def test_an_incomplete_search_says_so_in_the_summary():
    out, _ = _run({}, {"items": [], "total": 0, "index": {"available": False},
                       "live": {"truncated": True}})
    assert not out.is_error
    assert "no index yet" in out.text and "stopped at its limit" in out.text


def test_a_bad_number_is_an_argument_error_and_sends_nothing():
    out, calls = _run({"bias_min_v": "minus two"}, {"items": []})
    assert out.is_error and "bias_min_v" in out.text and calls == []
