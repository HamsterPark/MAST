"""Fixtures of the operator-in-the-loop tests: a throw-away data root, and backends that are put
back afterwards (``prompts`` / ``optical_state`` hold process-wide singletons)."""
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


@pytest.fixture
def op_root(tmp_path, monkeypatch):
    """A data root for ``project_root()``: nothing here ever touches the operator's real one."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(root))
    return root


@pytest.fixture
def memory_backends():
    """Fresh in-memory backends for the test, the previous ones restored afterwards."""
    from mast.operator_hitl import optical_state, prompts

    old_backend = prompts.set_backend(prompts.InMemoryPromptBackend())
    old_store = optical_state.set_store(optical_state.OpticalStateStore())
    yield
    prompts.set_backend(old_backend)
    optical_state.set_store(old_store)


@pytest.fixture
def installed(op_root):
    """The persistent backends installed under ``op_root`` (the service's start-up), removed again."""
    from mast.operator_hitl import service

    inst = service.install(op_root)
    yield inst
    service.uninstall()
