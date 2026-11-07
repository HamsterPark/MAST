"""The internal LLM is off while ``legacy_features_enabled`` is off.

MAST is meant to be driven from an external agent; the built-in LLM stack belongs
to the legacy feature set (default OFF). Previously only the UI and the
vision model honoured that switch — the backend kept calling the model on its own:
the 30-min dreaming pass could hit the provider (HTTP 429) during an unattended
tip approach.
``mast/legacy_gate.py`` now gates every path that can reach the internal LLM.

This file pins the core side (the API routes and the CLI are in
``tests/v2/unit/api/test_legacy_llm_routes.py``):

① the switch: read LIVE from the process settings store; absent / false /
  unreadable ⇒ OFF; reachable over HTTP (all three whitelists + the OpenAPI);
② dreaming: a vetoed cycle does NOTHING (no DB read, no model call, no memory
  write); turning the switch on takes effect on the next cycle;
③ waking: off ⇒ nothing is asked or spawned (no goal check either), but overdue
  parks still expire and escalate (no model call);
④ the runtime hands the switch to both loops, builds no agent stack / cognition
  LLM at start-up while off, never (re)builds the orchestrator while off, and does
  not auto-resume conversations while off;
⑤ the choke points: ``make_chat_model`` refuses to build AND every model it built
  refuses at call time (before the provider) once the switch is off; the legacy
  ``ClaudeClient``, the OCR transport and the voice transports refuse before any
  request; the vision summarizer, the conduct seat, and the composite llm / agent
  nodes take their designed "no model" exits.

The suite runs with the gate forced open (``tests/v2/conftest.py``); the ``gate``
fixture here switches it back to reading the real setting from a fake store.

Run::

    $env:PYTHONPATH = "$PWD\\MASTv2"
    .venv-v2-py313\\Scripts\\python.exe -m pytest tests/v2/unit/core/test_legacy_llm_gate.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path


def _find_mastv2_root() -> str:
    p = Path(__file__).resolve()
    while p.parent != p:
        candidate = p / "MASTv2"
        if candidate.is_dir():
            return str(candidate)
        p = p.parent
    raise RuntimeError("MASTv2 dir not found")


_MASTV2_ROOT = _find_mastv2_root()
if sys.path and sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)

import ast  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from mast import legacy_gate  # noqa: E402
from mast.core import park_board as pb  # noqa: E402
from mast.core import wake_scheduler as ws  # noqa: E402
from mast.core.runtime import CoreRuntime  # noqa: E402
from mast.legacy_gate import (  # noqa: E402
    REFUSAL_CODE,
    LegacyFeaturesDisabled,
    internal_llm_enabled,
)
from mast.memory import dreaming as dreaming_mod  # noqa: E402
from mast.memory.dreaming import DreamingService  # noqa: E402
from mast.webui import settings_store  # noqa: E402
from tests.v2.srcref import source_of  # noqa: E402

KEY = "legacy_features_enabled"


# ── fakes + the gate fixture ──────────────────────────────────────────────────

class _Settings:
    """The slice of SettingsStore the gate reads: ``get(key, default=None)``."""

    def __init__(self, **kv):
        self.kv = dict(kv)

    def get(self, key, default=None):
        return self.kv.get(key, default)


class _Unreadable:
    def get(self, key, default=None):
        raise RuntimeError("ui_settings.json is locked")


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    """Read the REAL setting from a fake process store (starts with the key absent)."""
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path))
    settings_store.reset_process_store()
    st = _Settings()
    settings_store.set_process_store(st)
    legacy_gate.set_override_for_test(None)
    try:
        yield st
    finally:
        legacy_gate.set_override_for_test(True)
        settings_store.reset_process_store()


def _rt(*, busy=False) -> CoreRuntime:
    rt = CoreRuntime.__new__(CoreRuntime)      # skip the heavy __init__/setup
    rt._orch_running = busy
    return rt


# ══════════════════════════════════════════════════════════════════════════════
# ① the switch
# ══════════════════════════════════════════════════════════════════════════════

class TestTheSwitch:
    def test_no_store_and_no_settings_file_means_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path))
        settings_store.reset_process_store()
        legacy_gate.set_override_for_test(None)
        try:
            assert internal_llm_enabled() is False
        finally:
            legacy_gate.set_override_for_test(True)
            settings_store.reset_process_store()

    def test_an_absent_key_means_off(self, gate):
        """An existing install has no such key until someone turns it on: after
        the upgrade the background calls stop with no settings change."""
        assert internal_llm_enabled() is False

    def test_an_unreadable_store_means_off(self, gate):
        settings_store.set_process_store(_Unreadable())
        assert internal_llm_enabled() is False

    def test_false_is_off_and_true_is_on(self, gate):
        gate.kv[KEY] = False
        assert internal_llm_enabled() is False
        gate.kv[KEY] = True
        assert internal_llm_enabled() is True

    def test_it_is_read_live(self, gate):
        """No restart: the next read sees the flip, in both directions."""
        assert internal_llm_enabled() is False
        gate.kv[KEY] = True
        assert internal_llm_enabled() is True
        gate.kv[KEY] = False
        assert internal_llm_enabled() is False


class TestTheSwitchIsReachableOverHttp:
    """The one switch must be writable and readable over HTTP — a key missing from
    any of these fails silently (see test_settings_write_reachability.py)."""

    def test_the_store_persists_it(self):
        assert KEY in settings_store.KNOWN_KEYS

    def test_the_read_schema_exposes_it(self):
        from mast.api.schemas import SettingsResponse

        assert KEY in SettingsResponse.model_fields

    def test_the_write_schema_accepts_it(self):
        from mast.api.schemas_settings_admin_write import SettingsWriteRequest

        assert KEY in SettingsWriteRequest.model_fields

    def test_the_committed_openapi_carries_it(self):
        root = Path(_MASTV2_ROOT).parent
        spec = json.loads((root / "frontend" / "openapi.json").read_text(encoding="utf-8"))
        schemas = spec["components"]["schemas"]
        assert KEY in schemas["SettingsResponse"]["properties"]
        assert KEY in schemas["SettingsWriteRequest"]["properties"]


# ══════════════════════════════════════════════════════════════════════════════
# ② dreaming
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def dream_svc(tmp_path, monkeypatch):
    """A DreamingService over a real temp DB, with spies on the DB read and the
    consolidator (the model call in production)."""
    from mast.logging.storage import ExperimentStorage
    from mast.memory.store import MemoryStore

    db = str(tmp_path / "exp.db")
    st = ExperimentStorage(db)
    st.create_experiment("Run A", "approach")
    mem = MemoryStore(db)

    calls = {"gather": 0, "model": 0}
    real_gather = dreaming_mod.gather_context

    def _gather(path):
        calls["gather"] += 1
        return real_gather(path)

    def _model(ctx):
        calls["model"] += 1
        return [{"path": "dreams/spy.md", "title": "spy", "kind": "dream",
                 "content": "a dream"}]

    monkeypatch.setattr(dreaming_mod, "gather_context", _gather)
    svc = DreamingService(db, mem, consolidator=_model)
    return svc, mem, calls


class TestDreaming:
    def test_by_default_a_scheduled_cycle_does_nothing(self, gate, dream_svc):
        svc, mem, calls = dream_svc
        svc.set_should_dream(_rt()._dream_allowed)
        assert svc.run_cycle() is False
        assert calls == {"gather": 0, "model": 0}, \
            "a vetoed cycle still read the experiment DB or called the model"
        assert mem.read("global", "dreams/spy.md") is None

    def test_turning_it_on_takes_effect_on_the_next_cycle(self, gate, dream_svc):
        svc, mem, calls = dream_svc
        svc.set_should_dream(_rt()._dream_allowed)
        assert svc.run_cycle() is False
        gate.kv[KEY] = True
        assert svc.run_cycle() is True
        assert calls["model"] == 1
        assert mem.read("global", "dreams/spy.md") is not None

    def test_on_but_busy_still_skips(self, gate, dream_svc):
        svc, _mem, calls = dream_svc
        gate.kv[KEY] = True
        svc.set_should_dream(_rt(busy=True)._dream_allowed)
        assert svc.run_cycle() is False
        assert calls["model"] == 0

    def test_unreadable_settings_skip(self, gate, dream_svc):
        svc, _mem, calls = dream_svc
        settings_store.set_process_store(_Unreadable())
        svc.set_should_dream(_rt()._dream_allowed)
        assert svc.run_cycle() is False
        assert calls["model"] == 0

    def test_a_raising_predicate_is_a_veto(self, dream_svc):
        svc, _mem, calls = dream_svc

        def _boom():
            raise RuntimeError("no runtime")

        svc.set_should_dream(_boom)
        assert svc.run_cycle() is False
        assert calls == {"gather": 0, "model": 0}

    def test_dream_once_itself_is_not_the_gate(self, gate, dream_svc):
        """The service method stays ungated; on-demand dreaming is refused at the
        API routes instead (test_legacy_llm_routes.py), and the LLM consolidator is
        only built while the switch is on."""
        svc, _mem, calls = dream_svc
        svc.set_should_dream(_rt()._dream_allowed)
        assert svc.dream_once()
        assert calls["model"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# ③ waking
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST_PARK_BOARD_DIR", str(tmp_path))
    b = pb.ParkBoard(tmp_path / "pb.json")
    pb.set_board_for_test(b)
    yield b
    pb.set_board_for_test(None)


@pytest.fixture()
def arrived(monkeypatch):
    """Make 'the awaited draft has arrived' deterministic."""
    state = {"draft": (1, 111.0)}
    monkeypatch.setattr(ws, "_artifact_versions", lambda: dict(state))
    return state


class _Spy:
    def __init__(self):
        self.asked: list = []
        self.spawned: list = []
        self.escalated: list = []

    def ask(self, park, arrived, versions):
        self.asked.append(park["park_id"])
        return {"action": "start", "waiting_for": [], "reason": "test"}

    def spawn(self, park):
        self.spawned.append(park["park_id"])
        return f"run-{len(self.spawned)}"

    def escalate(self, kind, park, detail):
        self.escalated.append(kind)


def _sched(spy, enabled, **kw):
    return ws.WakeScheduler(should_wake=lambda: True, enabled=enabled,
                            ask=spy.ask, spawn=spy.spawn, escalate=spy.escalate,
                            daily_budget_usd=0.0, ask_cooldown_s=0.0, **kw)


def _park(board):
    return board.park("paper_review", waiting_for=["draft"], experiment_id="exp-A",
                      instruction="finish it")


class TestWaking:
    def test_by_default_nothing_is_asked_or_spawned(self, gate, board, arrived):
        spy = _Spy()
        _park(board)
        out = _sched(spy, internal_llm_enabled).tick()
        assert spy.asked == [] and spy.spawned == []
        assert out["skipped"] == "legacy features disabled"

    def test_deadlines_still_expire_while_off(self, gate, board, arrived):
        """Expiry costs no model call and is how the operator learns a wait went
        stale — the switch must not hide it."""
        rec = _park(board)
        board._items[rec["park_id"]]["deadline_at"] = time.time() - 1
        spy = _Spy()
        out = _sched(spy, internal_llm_enabled).tick()
        assert out["expired"] == 1
        assert "expired" in spy.escalated
        assert spy.asked == [] and spy.spawned == []

    def test_turning_it_on_wakes_on_the_next_tick(self, gate, board, arrived):
        spy = _Spy()
        s = _sched(spy, internal_llm_enabled)
        _park(board)
        s.tick()
        assert spy.spawned == []
        gate.kv[KEY] = True
        s.tick()
        assert len(spy.asked) == 1 and len(spy.spawned) == 1

    def test_an_unreadable_switch_stops_rather_than_guesses(self, board, arrived):
        def _boom():
            raise RuntimeError("settings gone")

        spy = _Spy()
        _park(board)
        out = _sched(spy, _boom).tick()
        assert spy.asked == [] and spy.spawned == []
        assert "enable switch unreadable" in out["skipped"]

    def test_the_off_switch_also_skips_the_goal_gate(self, gate, board, arrived):
        """While off, a tick is deadlines only — no goal check either."""
        seen = []
        spy = _Spy()
        _park(board)
        _sched(spy, internal_llm_enabled,
               goal_check=lambda park: seen.append(park) or None).tick()
        assert seen == []

    def test_a_scheduler_built_without_the_switch_behaves_as_before(self, board,
                                                                    arrived):
        spy = _Spy()
        _park(board)
        ws.WakeScheduler(should_wake=lambda: True, ask=spy.ask, spawn=spy.spawn,
                         escalate=spy.escalate, daily_budget_usd=0.0,
                         ask_cooldown_s=0.0).tick()
        assert len(spy.spawned) == 1


# ══════════════════════════════════════════════════════════════════════════════
# ④ the runtime
# ══════════════════════════════════════════════════════════════════════════════

def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    """Calls ``…name(...)`` / ``name(...)`` in ``tree`` (source via srcref, not
    inspect.getsource — see tests/v2/srcref.py for why)."""
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and (getattr(n.func, "attr", None) == name
                 or getattr(n.func, "id", None) == name)]


def _kw(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _guards_of(fn, inner_call: str) -> set[str]:
    """Plain names called in the test of every ``if`` whose body calls
    ``inner_call`` (directly or nested)."""
    tree = ast.parse(source_of(fn))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and any(
                _calls_named(stmt, inner_call) for stmt in node.body):
            names |= {n.func.id for n in ast.walk(node.test)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    return names


class TestTheRuntimeWiring:
    def test_setup_starts_dreaming_with_the_gated_predicate(self):
        calls = _calls_named(ast.parse(source_of(CoreRuntime.setup)), "start_dreaming")
        assert len(calls) == 1, "setup() no longer starts dreaming exactly once"
        val = _kw(calls[0], "should_dream")
        assert isinstance(val, ast.Attribute) and val.attr == "_dream_allowed"

    def test_the_wake_scheduler_gets_the_switch(self):
        calls = _calls_named(ast.parse(source_of(CoreRuntime._ensure_wake_scheduler)),
                             "WakeScheduler")
        assert len(calls) == 1
        val = _kw(calls[0], "enabled")
        assert isinstance(val, ast.Name) and val.id == "internal_llm_enabled"

    def test_the_dream_predicate_needs_both_the_switch_and_idle(self, gate):
        gate.kv[KEY] = True
        assert _rt()._dream_allowed() is True
        assert _rt(busy=True)._dream_allowed() is False
        gate.kv[KEY] = False
        assert _rt()._dream_allowed() is False

    def test_setup_builds_the_agent_stack_only_with_the_switch_on(self):
        """Orchestrator, chat engine, query assistant: no client while off."""
        assert "_llm_allowed" in _guards_of(CoreRuntime.setup, "_build_orchestrator")
        assert "_llm_allowed" in _guards_of(CoreRuntime.setup, "_build_chat_engine")

    def test_setup_builds_the_cognition_llm_only_with_the_switch_on(self):
        assert "_llm_on" in _guards_of(CoreRuntime.setup, "set_consolidator")

    def test_no_orchestrator_rebuild_while_off_and_an_existing_graph_is_kept(self, gate):
        rt = _rt()
        sentinel = object()
        rt._orchestrator = sentinel
        rt._orch_build_lock = threading.Lock()
        assert rt._build_orchestrator() is False
        assert rt._orchestrator is sentinel

    def test_no_auto_resume_of_a_conversation_while_off(self, gate, monkeypatch):
        rt = _rt()
        touched = []
        monkeypatch.setattr(CoreRuntime, "_resume_private",
                            lambda self, *a, **k: touched.append(a) or (True, ""))
        out = rt._resume_after_fetch_fulfilled(
            "w1", [{"origin_conversation_id": "c1", "request_id": "r1"}])
        assert out["resumed"] == 0 and touched == []
        assert "legacy features disabled" in out["skipped"][0]["reason"]


# ══════════════════════════════════════════════════════════════════════════════
# ⑤ the choke points
# ══════════════════════════════════════════════════════════════════════════════

class TestModelFactory:
    def test_building_a_model_is_refused_while_off(self, gate):
        from mast.agents._shared.models import make_chat_model

        with pytest.raises(LegacyFeaturesDisabled) as ei:
            make_chat_model("orchestrator")
        assert isinstance(ei.value, RuntimeError), \
            "callers read RuntimeError from make_chat_model as 'no model configured'"
        assert str(ei.value).startswith(REFUSAL_CODE)

    def test_every_built_model_carries_the_gate_first(self, monkeypatch):
        from mast.agents._shared.models import MOONSHOT_128K, make_chat_model

        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test-not-a-key")
        m = make_chat_model(model_id=MOONSHOT_128K)
        first = m.callbacks[0]
        assert type(first).__name__ == "InternalLLMGateCallback"
        assert first.raise_error is True

    def test_a_model_built_while_on_is_refused_after_switching_off(self, gate,
                                                                   monkeypatch):
        """The call-time half: switching off needs no restart and no rebuild."""
        from mast.agents._shared.models import MOONSHOT_128K, make_chat_model

        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test-not-a-key")
        gate.kv[KEY] = True
        m = make_chat_model(model_id=MOONSHOT_128K)
        sent = []
        monkeypatch.setattr(type(m), "_generate",
                            lambda self, *a, **k: sent.append(1))
        gate.kv[KEY] = False
        with pytest.raises(LegacyFeaturesDisabled):
            m.invoke("hello")
        assert sent == [], "the request reached the provider layer"

    @pytest.mark.parametrize("how", ["invoke", "ainvoke", "stream"])
    def test_the_gate_callback_refuses_before_the_provider(self, gate, how):
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        calls = []

        class _Fake(FakeListChatModel):
            def _call(self, *a, **k):
                calls.append(1)
                return super()._call(*a, **k)

        m = _Fake(responses=["hi"], callbacks=[legacy_gate.make_gate_callback("t")])
        with pytest.raises(LegacyFeaturesDisabled):
            if how == "invoke":
                m.invoke("x")
            elif how == "ainvoke":
                asyncio.run(m.ainvoke("x"))
            else:
                list(m.stream("x"))
        assert calls == []
        gate.kv[KEY] = True
        assert m.invoke("x").content == "hi"


class TestOtherTransports:
    def test_the_legacy_client_refuses_before_any_request(self, gate, monkeypatch):
        from mast.llm.client import ClaudeClient

        sent = []
        c = ClaudeClient.__new__(ClaudeClient)
        c._provider = "moonshot"
        monkeypatch.setattr(ClaudeClient, "_chat_openai_compat",
                            lambda self, *a, **k: sent.append(1) or {})
        monkeypatch.setattr(ClaudeClient, "_chat_anthropic",
                            lambda self, *a, **k: sent.append(1) or {})
        with pytest.raises(LegacyFeaturesDisabled):
            c.single_turn("hello")
        assert sent == []

    def test_ocr_is_unavailable_and_renders_nothing_while_off(self, gate, monkeypatch,
                                                             tmp_path):
        from mast.knowledge import ocr

        rendered = []
        monkeypatch.setattr(ocr, "_render_pages",
                            lambda *a, **k: rendered.append(1) or [b"png"])
        assert ocr.ocr_available() is False
        assert ocr.ocr_pdf_pages(tmp_path / "scan.pdf") == []
        assert ocr.ocr_pdf(tmp_path / "scan.pdf") == ""
        assert rendered == []

    def test_voice_refuses_before_any_request(self, gate):
        from mast.voice.dashscope import DashScopeVoiceClient, VoiceProviderError

        c = DashScopeVoiceClient.__new__(DashScopeVoiceClient)   # no HTTP client
        for call in (lambda: c.synthesize("hello"), lambda: c.transcribe(b"RIFF")):
            with pytest.raises(VoiceProviderError) as ei:
                call()
            assert str(ei.value).startswith(REFUSAL_CODE)

    def test_realtime_voice_refuses_before_connecting(self, gate):
        from mast.voice.realtime import dashscope_rt

        with pytest.raises(dashscope_rt.RealtimeVoiceError) as ei:
            asyncio.run(dashscope_rt._connect("wss://127.0.0.1:9/never", "k"))
        assert str(ei.value).startswith(REFUSAL_CODE)


class TestAutomaticCallers:
    def test_the_vision_summarizer_uses_its_template_and_caches_nothing(self, gate,
                                                                        monkeypatch):
        from mast.agents.buffer_summarizer import node

        built = []
        monkeypatch.setattr(node, "_get_llm", lambda *a, **k: built.append(1))
        payload = {"label": "good", "confidence": 0.9}
        before = dict(node._CACHE)
        text = node._summarize_generic("tip_coarse", payload)
        assert text == node._fallback_summary("tip_coarse", payload)
        assert built == [] and node._CACHE == before

    def test_the_conduct_seat_has_no_model_while_off(self, gate):
        """RuntimeError from the factory is the seat's designed 'no seat' shape."""
        from mast.conduct import llm_seat

        with pytest.raises(RuntimeError) as ei:
            llm_seat._default_model_factory({})
        assert isinstance(ei.value, LegacyFeaturesDisabled)

    def test_a_workflow_llm_node_takes_its_escape(self, gate):
        from mast.skills.composite import llm_node

        node = {"id": "judge", "routes": {"ok": "a", "retry": "b"}, "escape": "retry"}
        out = llm_node.decide_route(node, {"q": 1})
        assert out["route"] == "retry" and out["escaped"] is True
        assert REFUSAL_CODE in out["escape_reason"]

    def test_a_workflow_agent_node_refuses_without_building_a_graph(self, gate,
                                                                    monkeypatch):
        from mast.skills.composite import agent_node

        built = []
        monkeypatch.setattr(agent_node, "_build_agent_graph",
                            lambda *a, **k: built.append(1))
        agent = sorted(agent_node.DELEGATABLE_AGENTS)[0]
        out = agent_node.run_agent_task({"agent": agent}, "summarise")
        assert out["ok"] is False and REFUSAL_CODE in out["note"]
        assert built == []
