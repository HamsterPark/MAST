"""Two CoreRuntime methods that callers used and nobody defined.

Both lived on the Gradio app class and went with ``gui/app.py``;
their callers in ``core/runtime.py`` stayed:

* ``_chat_experiment_id`` — ``_chat_agent_middleware`` raised AttributeError on
  every build and ``orchestrator.graph`` swallowed it into an EMPTY middleware
  list (startup log: ``agent_extra_middleware(...) failed: 'CoreRuntime' object has
  no attribute '_chat_experiment_id'`` ×6). The private-chat memory provider and
  the request-tool provider raised on every use; the latter's wrapper turned that
  into ``{}``, so requests went out without agent id or experiment.
* ``_state_to_dict`` — every training-trace step raised inside a swallowing except.

The one existing test of that wiring (test_request_readback_mw) passed a stub that
CARRIED ``_chat_experiment_id`` itself, so it could not see the gap. Everything
here goes through the class, with nothing of the sort on the instance.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mast.core.runtime import CoreRuntime
from mast.core.types import HardwareState

_RUNTIME_PY = Path(__file__).resolve().parents[4] / "MASTv2" / "mast" / "core" / "runtime.py"


def _bare(**attrs) -> CoreRuntime:
    """A CoreRuntime without __init__ (as other tests build it), plus *attrs* —
    never a method."""
    rt = CoreRuntime.__new__(CoreRuntime)
    rt._settings = None
    rt._cognition = None
    rt._experiment_log = None
    for k, v in attrs.items():
        setattr(rt, k, v)
    return rt


def test_chat_experiment_id_is_defined_on_the_class():
    assert callable(CoreRuntime.__dict__.get("_chat_experiment_id"))
    assert _bare()._chat_experiment_id() is None
    log = SimpleNamespace(current_experiment_id="exp-1")
    assert _bare(_experiment_log=log)._chat_experiment_id() == "exp-1"


def test_chat_middleware_builds_on_the_real_class():
    mws = _bare()._chat_agent_middleware("instrument_control")
    assert mws, "an empty list is exactly what the swallowed AttributeError produced"
    assert any(type(m).__name__ == "RequestReplyReadbackMiddleware" for m in mws)


def test_every_group_agent_gets_its_stack():
    factory = _bare()._group_agent_middleware()
    for agent_id in ("research_director", "literature", "experiment_design",
                     "instrument_control", "data_processing", "paper_writing",
                     "paper_review"):
        assert factory(agent_id), agent_id


def test_private_chat_providers_resolve_the_experiment(monkeypatch):
    import mast.agents._shared.memory_tools as memory_tools
    import mast.agents._shared.request_tools as request_tools

    seen: dict = {}
    monkeypatch.setattr(memory_tools, "make_memory_tools",
                        lambda provider: seen.setdefault("memory", provider) and [])
    monkeypatch.setattr(request_tools, "make_request_tools",
                        lambda provider: seen.setdefault("request", provider) and [])

    asked: dict = {}

    class _Cognition:
        def memory_provider(self, experiment_id=None, author=None):
            asked.update(experiment_id=experiment_id, author=author)
            return lambda: "provider"

    rt = _bare(_cognition=_Cognition(),
               _experiment_log=SimpleNamespace(current_experiment_id="exp-7"))
    rt._chat_agent_extra_tools("instrument_control")

    assert seen["memory"]() == "provider"
    assert asked == {"experiment_id": "exp-7", "author": "user"}
    assert seen["request"]() == {"agent_id": "instrument_control", "experiment_id": "exp-7"}


def test_state_to_dict_is_defined_on_the_class():
    assert isinstance(CoreRuntime.__dict__.get("_state_to_dict"), staticmethod)
    d = CoreRuntime._state_to_dict(HardwareState(bias_v=2.0, current_a=5e-11))
    assert d["bias_v"] == 2.0 and d["current_a"] == 5e-11
    assert CoreRuntime._state_to_dict(None) is None
    assert CoreRuntime._state_to_dict({"a": 1}) == {"a": 1}


def test_a_skill_step_reaches_the_active_trajectory():
    sink = MagicMock()
    rt = _bare(_trace_sink=sink, _active_traj="traj-1")
    # the recorders that run before the trace step have their own tests
    rt._record_map_marker = lambda p: None
    rt._record_v1_action = lambda p: None
    rt._note_run_skill = lambda p: None
    rt._record_v2_action = lambda p: "act-1"

    rt._skill_trace_recorder({"skill": "GetBias", "params": {}, "success": True,
                              "state_before": HardwareState(bias_v=2.0)})

    sink.record_step.assert_called_once()
    kwargs = sink.record_step.call_args.kwargs
    assert kwargs["trajectory_id"] == "traj-1" and kwargs["action_id"] == "act-1"
    assert kwargs["input"]["state_before"]["bias_v"] == 2.0


# ── the gate: no self.<name> is read that the class never defines ───────────

def _undefined_self_reads(path: Path, class_name: str) -> dict[str, list[int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    assert not cls.bases, "the check below is complete only for a class without bases"
    defined: set[str] = set()
    for item in cls.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(item.name)
        elif isinstance(item, ast.Assign):
            defined.update(t.id for t in item.targets if isinstance(t, ast.Name))
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            defined.add(item.target.id)
    reads: dict[str, list[int]] = {}
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "self":
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                defined.add(node.attr)
            else:
                reads.setdefault(node.attr, []).append(node.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "setattr" and len(node.args) >= 2 \
                and isinstance(node.args[0], ast.Name) and node.args[0].id == "self" \
                and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
            defined.add(node.args[1].value)
    return {k: v for k, v in reads.items() if k not in defined}


def test_core_runtime_reads_no_self_attribute_it_never_defines():
    """A read of ``self.X`` with no ``def X`` / class attribute / ``self.X = …`` /
    ``setattr(self, "X", …)`` anywhere in the class is that bug's shape: it raises
    the first time it runs, and every caller here wraps in a swallowing except.
    ``getattr(self, "X", default)`` is a deliberate optional read and is not
    counted."""
    missing = _undefined_self_reads(_RUNTIME_PY, "CoreRuntime")
    assert not missing, f"read but never defined on CoreRuntime: {missing}"


def test_the_gate_sees_the_shape_it_guards(tmp_path):
    """The gate must fail on exactly what it exists for, or a green says nothing."""
    probe = tmp_path / "probe.py"
    probe.write_text("class CoreRuntime:\n"
                     "    def a(self):\n"
                     "        self._defined = 1\n"
                     "        return self._ghost() + self._defined + self.a()\n",
                     encoding="utf-8")
    assert _undefined_self_reads(probe, "CoreRuntime") == {"_ghost": [4]}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
