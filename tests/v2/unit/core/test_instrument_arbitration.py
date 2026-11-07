"""One instrument, three entry points — the arbitration they never had.

Dispatch design audit 2026-07-28 致命一: the group-chat IC agent, the private
chat and the signals routes each build their own graph/tools/middleware over the
SAME ConnectionPool, and an exhaustive search of the tree found no global
instrument lock, busy flag, experiment lock, serialising queue or single-worker
executor anywhere. The only lock was ConnectionPool's per-role one, whose
critical section is a single TCP command round-trip — byte-interleaving
protection, not run arbitration.

Also covers the three connected defects:
  (a) the private chat borrowing the orchestrator's run_id (sidecar collision),
  (b) a new group run's abort.clear() unlocking someone else's E-STOP,
  (c) the Layer-0d approach refusal being clearable by a different chain.

Run from repo root:
    .venv-v2-py313/Scripts/python.exe -m pytest \
        tests/v2/unit/core/test_instrument_arbitration.py -x -v
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports ──
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

import ast
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _fresh_token():
    from mast.core.instrument_lock import instrument_lock

    instrument_lock()._reset_for_tests()
    yield
    instrument_lock()._reset_for_tests()


class _Meta:
    def __init__(self, name="ScanArea", category="write", tags=()):
        self.name = name
        self.category = type("C", (), {"value": category})()
        self.tags = list(tags)


# ─────────────────────────────────────────────────────────────────────
# The token itself
# ─────────────────────────────────────────────────────────────────────

def test_second_chain_is_refused_not_queued():
    from mast.core.instrument_lock import InstrumentBusy, instrument_lock

    lock = instrument_lock()
    held = threading.Event()
    release = threading.Event()

    def _holder():
        with lock.hold(owner="群聊任务 abc", skill="AutoApproach"):
            held.set()
            release.wait(timeout=10)

    th = threading.Thread(target=_holder, daemon=True)
    th.start()
    assert held.wait(timeout=5)
    try:
        t0 = time.perf_counter()
        with pytest.raises(InstrumentBusy) as ei:
            with lock.hold(owner="主聊天/私聊", skill="SetBias", timeout_s=0.3):
                pytest.fail("two chains must not drive the instrument at once")
        waited = time.perf_counter() - t0
        assert waited < 3.0
        # The message must name the holder — "busy" alone tells the operator
        # (and the LLM) nothing actionable.
        msg = ei.value.message()
        assert "群聊任务 abc" in msg and "AutoApproach" in msg
        assert "Do NOT retry" in msg
    finally:
        release.set()
        th.join(timeout=5)


def test_token_is_reentrant_for_composite_substeps():
    """A composite holds the token and its sub-steps re-take it on the same
    thread. Without re-entrancy every composite would deadlock on step 1."""
    from mast.core.instrument_lock import instrument_lock

    lock = instrument_lock()
    with lock.hold(owner="群聊", skill="AutoApproach"):
        with lock.hold(owner="群聊", skill="TryEngageController"):
            with lock.hold(owner="群聊", skill="SetBias"):
                assert lock.snapshot()["depth"] == 3
        assert lock.snapshot()["depth"] == 1
    assert lock.snapshot() is None


def test_reads_never_take_the_token():
    """The dashboard polls every 2 s. Making it queue behind a 10-minute scan
    would trade one bug for a frozen UI."""
    from mast.core.instrument_lock import instrument_lock, needs_token

    assert needs_token(_Meta("GetBias", category="read")) is False
    assert needs_token(_Meta("FitPeaks", category="analysis")) is False
    assert needs_token(_Meta("SetBias", category="write")) is True

    lock = instrument_lock()
    held = threading.Event()
    release = threading.Event()

    def _holder():
        with lock.hold(owner="群聊", skill="ScanArea"):
            held.set()
            release.wait(timeout=10)

    th = threading.Thread(target=_holder, daemon=True)
    th.start()
    assert held.wait(timeout=5)
    try:
        from mast.core.instrument_lock import hold_for_skill

        t0 = time.perf_counter()
        with hold_for_skill(_Meta("GetBias", category="read"), "GetBias", "API"):
            pass
        assert time.perf_counter() - t0 < 0.2, "a read waited on the token"
    finally:
        release.set()
        th.join(timeout=5)


def test_remedies_never_take_the_token():
    """A retract must run WHILE another chain holds the instrument — that is
    what an emergency is. Gating the fix behind the thing that needs fixing is
    a deadlock with a broken tip at the end of it."""
    from mast.core.instrument_lock import hold_for_skill, instrument_lock, needs_token

    for name in ("SafeRetract", "EmergencyRetract", "WithdrawTip",
                 "StopScan", "StopMotor", "StopAutoApproach"):
        assert needs_token(_Meta(name)) is False, name
    # …and by tag, so a retract added under a new name is still recognised.
    assert needs_token(_Meta("PullBackProbe", tags=["retract"])) is False

    lock = instrument_lock()
    held = threading.Event()
    release = threading.Event()

    def _holder():
        with lock.hold(owner="群聊", skill="AutoApproach"):
            held.set()
            release.wait(timeout=10)

    th = threading.Thread(target=_holder, daemon=True)
    th.start()
    assert held.wait(timeout=5)
    try:
        t0 = time.perf_counter()
        with hold_for_skill(_Meta("EmergencyRetract"), "EmergencyRetract", "急停"):
            pass
        assert time.perf_counter() - t0 < 0.2
    finally:
        release.set()
        th.join(timeout=5)


def test_unclassifiable_skill_takes_the_token():
    """Fail-CLOSED: the wrong direction here is two chains writing at once."""
    from mast.core.instrument_lock import needs_token

    assert needs_token(None, "SomethingNew") is True
    assert needs_token(object()) is True


def test_token_is_released_even_when_the_skill_raises():
    from mast.core.instrument_lock import instrument_lock

    lock = instrument_lock()
    with pytest.raises(ValueError):
        with lock.hold(owner="群聊", skill="ScanArea"):
            raise ValueError("boom")
    assert lock.snapshot() is None


# ─────────────────────────────────────────────────────────────────────
# All three entry points are actually wired to it
# ─────────────────────────────────────────────────────────────────────

def test_all_three_skill_entry_points_hold_the_token():
    """The token is worth nothing if only one of the three chains takes it."""
    root = Path(_MASTV2_ROOT) / "mast"
    for rel in ("core/execution_context.py",          # signals + composite substeps
                "core/executor.py",                   # manual / GUI
                "agents/_shared/skill_adapter.py"):   # group chat + private chat
        src = (root / rel).read_text(encoding="utf-8", errors="replace")
        assert "hold_for_skill" in src, f"{rel} does not arbitrate"


# The three source pins below (this test, the private-chat run_id one and the
# E-STOP latch one) read the syntax tree rather than the text. An `owner=` label
# is now usually `tr(zh, en)` and the refusal messages are bilingual, so a text
# match or a window of characters breaks whenever a message is reworded.

def _parse(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    return tree, parents


def _owner_label(node):
    """The Chinese label of an ``owner=`` argument: a plain string, the leading text
    of an f-string, or either of those wrapped in ``tr(zh, en)``. ``None`` otherwise."""
    if isinstance(node, ast.Call) and node.args:
        return _owner_label(node.args[0])
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        head = node.values[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return None


def _owner_calls(tree):
    """``(call, label)`` for every call that passes ``owner=<label>``."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            for kw in n.keywords:
                label = _owner_label(kw.value) if kw.arg == "owner" else None
                if label is not None:
                    yield n, label


def _enclosing_def(parents, node):
    fn = parents.get(node)
    while fn is not None and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        fn = parents.get(fn)
    return fn


def _own_nodes(fn):
    """The nodes of ``fn``'s own body, not those of functions nested inside it."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(n))


def _before(a, b):
    return (a.lineno, a.col_offset) < (b.lineno, b.col_offset)


def test_each_entry_point_names_itself():
    """A refusal must say WHICH chain has the instrument."""
    root = Path(_MASTV2_ROOT) / "mast"
    rt = [label for _, label in _owner_calls(_parse(root / "core/runtime.py")[0])]
    sig = [label for _, label in _owner_calls(_parse(root / "api/routes/signals.py")[0])]
    # The label may be wrapped in tr(zh, en) for the UI language; the Chinese
    # text is what is pinned.
    assert any(label.startswith("群聊任务") for label in rt)
    assert "主聊天/私聊" in rt
    assert "信号采集 API" in sig


# ─────────────────────────────────────────────────────────────────────
# (a) the private chat must not borrow the orchestrator's run_id
# ─────────────────────────────────────────────────────────────────────

def test_chat_engine_mints_its_own_run_id():
    from mast.chat.engine import ConversationEngine

    eng = ConversationEngine(graph_factory=lambda _a: None,
                             checkpointer=None, store=None)
    # No live turn → no run id.
    assert eng.active_run_id() == ""
    a = eng._new_run_id("thread-1")
    b = eng._new_run_id("thread-1")
    assert a and b and a != b, "sidecar keys must not collide across turns"
    assert a.startswith("chat-")


def test_private_chat_context_no_longer_reads_orch_run_id():
    """The literal defect: `_rid = getattr(self, "_orch_run_id", "")` inside the
    private-chat context provider, with no clear point anywhere in the tree."""
    tree, parents = _parse(Path(_MASTV2_ROOT) / "mast" / "core" / "runtime.py")
    # Find the private-chat provider (the call that names itself 主聊天/私聊) and
    # check that, in the same function, it reads the engine's own run id first.
    calls = [call for call, label in _owner_calls(tree) if label == "主聊天/私聊"]
    assert calls, "the private chat's ExecutionContext no longer names itself"
    call = calls[0]
    fn = _enclosing_def(parents, call)
    assert fn is not None
    reads = [n for n in _own_nodes(fn)
             if isinstance(n, ast.Attribute) and n.attr == "active_run_id"
             and _before(n, call)]
    assert reads, "private chat is still borrowing another chain's run_id"


# ─────────────────────────────────────────────────────────────────────
# (b) a new run must not clear a latched emergency
# ─────────────────────────────────────────────────────────────────────

def test_run_task_refuses_to_clear_a_latched_emergency():
    tree, parents = _parse(
        Path(_MASTV2_ROOT) / "mast" / "api" / "routes" / "orchestrator.py")
    clears = sorted(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
         and n.func.attr == "clear" and isinstance(n.func.value, ast.Name)
         and n.func.value.id == "abort"),
        key=lambda n: (n.lineno, n.col_offset))
    assert clears, "no abort.clear() left in orchestrator.py"
    clear = clears[0]
    fn = _enclosing_def(parents, clear)
    assert fn is not None
    # The latch flag must be read in the same function, before the clear. Anchoring
    # on the function rather than on a window of characters keeps this true when the
    # refusal text between the check and the clear is reworded or translated.
    reads = [n for n in _own_nodes(fn)
             if ((isinstance(n, ast.Constant) and n.value == "_orch_abort_emergency")
                 or (isinstance(n, ast.Attribute) and n.attr == "_orch_abort_emergency"))
             and _before(n, clear)]
    assert reads, (
        "starting a new task still clears an E-STOP out from under another chain")


def test_any_abort_union_semantics():
    from mast.api.routes.orchestrator import _AnyAbort

    glob = threading.Event()
    run = threading.Event()
    u = _AnyAbort(global_event=glob, run_event=run)

    assert u.is_set() is False
    run.set()
    assert u.is_set() is True          # this run stopped
    run.clear()
    glob.set()
    assert u.is_set() is True          # the emergency stopped everything
    glob.clear()

    # set() means "stop THIS run" — it must never latch the global emergency,
    # or 中止 would masquerade as an E-STOP and freeze the other chains.
    u.set()
    assert run.is_set() is True
    assert glob.is_set() is False


def test_any_abort_degrades_without_a_run_event():
    from mast.api.routes.orchestrator import _AnyAbort

    glob = threading.Event()
    u = _AnyAbort(global_event=glob, run_event=None)
    u.set()
    assert glob.is_set() is True, "with no run Event, fall back rather than no-op"


# ─────────────────────────────────────────────────────────────────────
# (c) one chain must not clear another chain's approach refusal
# ─────────────────────────────────────────────────────────────────────

def test_approach_refusal_clear_is_scoped_to_the_recording_chain():
    from mast.core import safety_escalation as esc

    esc.clear_approach_refusal(owner=None)
    esc.record_approach_refusal("engage phase failed", owner="群聊任务 abc#abc")
    assert esc.active_approach_refusal() is not None

    # The private chat succeeding does NOT license the group chat's gate to open.
    assert esc.clear_approach_refusal("engaged", owner="主聊天/私聊#chat-1") is False
    assert esc.active_approach_refusal() is not None

    # The chain that recorded it can supersede its own verdict.
    assert esc.clear_approach_refusal("escalating", owner="群聊任务 abc#abc") is True
    assert esc.active_approach_refusal() is None


def test_approach_refusal_administrative_clear_still_works():
    from mast.core import safety_escalation as esc

    esc.record_approach_refusal("x", owner="群聊#1")
    assert esc.clear_approach_refusal("admin", owner=None) is True
    assert esc.active_approach_refusal() is None


def test_unscoped_refusal_is_clearable_by_anyone():
    """Legacy shape (no owner recorded) must not become permanently sticky."""
    from mast.core import safety_escalation as esc

    esc.clear_approach_refusal(owner=None)
    esc.record_approach_refusal("x")
    assert esc.clear_approach_refusal("y", owner="anybody") is True


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-v"])
