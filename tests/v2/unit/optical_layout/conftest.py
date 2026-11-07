"""Fixtures of the optical-layout tests: a throw-away data root, and the English / Chinese switch.

The layout store lives under ``project_root()/config/optical_layouts``; every test here runs with
``MAST2_PROJECT_ROOT`` pointing at an empty temporary folder, so nothing ever touches the
operator's real layouts.
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports (same bootstrap as test_wrap_skill_minimal) ──
import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[4] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def layout_root(tmp_path, monkeypatch):
    """The data root of this test (``project_root()``); layouts go to ``<root>/config/optical_layouts``."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(root))
    return root
