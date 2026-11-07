"""``OpticalStateStore.add_listener``: the hook for "a slot was just written".

The integration wires "the laser-area attestation was recorded as anything but True, or cleared:
switch every external source's emission off and close the WITec shutter" onto it. So what is
pinned here is what such a safety reaction needs to be able to rely on:

* it is called for every committed write — a set (with the record, whatever value the operator
  recorded) and a clear (``None``) — and for nothing that did not happen;
* the new state is already in force when it is called, and the store's own lock is released;
* a listener that fails never undoes or blocks the write, nor starves the other listeners —
  and the failure is not silent;
* adding, removing and being notified are thread-safe;
* an attest answer, which writes its slots while the prompt backend holds its lock, notifies only
  after that lock is released;
* the way it is meant to be wired — ``service.install()``, then ``add_listener`` on the active
  store — hears what the operator API writes, with the ``client`` of the request.
"""
from __future__ import annotations

import logging
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mast.api.routes import operator as operator_routes
from mast.operator_hitl import optical_state, prompts
from mast.operator_hitl.store import PersistentOpticalStateStore


@pytest.fixture(autouse=True)
def _memory(memory_backends):
    yield


@pytest.fixture
def store():
    return optical_state.get_store()


class Recorder:
    """A listener that remembers what it was told, and what the store looked like by then."""

    def __init__(self, store):
        self.store = store
        self.calls = []

    def __call__(self, slot_id, record):
        self.calls.append((slot_id, record, self.store.get(slot_id), self.store.seq))


# ── what is delivered ────────────────────────────────────────────────────────


def test_a_set_delivers_the_slot_and_the_record(store):
    rec = Recorder(store)
    store.add_listener(rec)
    sv = store.set_by_operator("objective", "50x", by="alice", source="web", note="turned in",
                               client={"peer": "127.0.0.1", "auth": "none"})
    ((slot, record, seen_value, seen_seq),) = rec.calls
    assert slot == "objective"
    assert record == {"slot": "objective", "value": "50x", "set_by": "alice", "set_at": sv.set_at,
                      "source": "web", "valid_until": None, "note": "turned in",
                      "client": {"peer": "127.0.0.1", "auth": "none"}}
    assert seen_value.value == "50x", "the new value is already in force when the listener runs"
    assert seen_seq == 1 and store.history[-1]["slot"] == "objective"


def test_a_clear_delivers_none_even_for_a_slot_that_was_not_set(store):
    store.set_by_operator("laser_area_safe", True, by="alice", source="web")
    rec = Recorder(store)
    store.add_listener(rec)
    store.clear_by_operator("laser_area_safe", by="alice", source="web", client={"peer": "x"})
    store.clear_by_operator("objective", by="alice", source="web")
    assert [(s, r) for s, r, _, _ in rec.calls] == [("laser_area_safe", None), ("objective", None)]
    assert rec.calls[0][2] is None, "the value is already gone when the listener runs"
    assert store.history[-2]["client"] == {"peer": "x"}, "a clear's history record carries the client"


def test_a_flag_recorded_false_is_reported_as_false_not_as_unset(store):
    """The case the emission interlock is for: the operator records "laser area safe" as NO."""
    store.set_by_operator("laser_area_safe", True, by="alice", source="web")
    rec = Recorder(store)
    store.add_listener(rec)
    store.set_by_operator("laser_area_safe", False, by="alice", source="web")
    ((_, record, now, _),) = rec.calls
    assert record["value"] is False and now.value is False
    # the gotcha, stated where the listener's author will read it: a False is a *set, unexpired*
    # value, so a bare is_valid() still says yes; only expect=True answers "is it safe"
    assert store.is_valid("laser_area_safe") is True
    assert store.is_valid("laser_area_safe", expect=True) is False


def test_every_write_is_delivered_in_order_with_its_own_record(store):
    rec = Recorder(store)
    store.add_listener(rec)
    store.set_by_operator("objective", "10x", by="a", source="web")
    store.set_by_operator("objective", "100x", by="a", source="web")
    store.clear_by_operator("objective", by="a", source="web")
    assert [(s, None if r is None else r["value"]) for s, r, _, _ in rec.calls] == [
        ("objective", "10x"), ("objective", "100x"), ("objective", None)]


def test_a_write_that_failed_calls_nobody_and_leaves_no_trace(op_root):
    s = PersistentOpticalStateStore(op_root)
    rec = Recorder(s)
    s.add_listener(rec)
    s.set_by_operator("laser_area_safe", True, by="alice", source="web")
    rec.calls.clear()

    def boom():
        raise PermissionError("read-only file system")

    s._write_values = boom
    with pytest.raises(PermissionError):
        s.set_by_operator("laser_area_safe", False, by="alice", source="web")
    with pytest.raises(PermissionError):
        s.clear_by_operator("laser_area_safe", by="alice", source="web")
    with pytest.raises(optical_state.OpticalStateError):
        s.set_by_operator("incident_polarizer_deg", 999, by="alice", source="web")
    with pytest.raises(optical_state.OpticalStateError):
        s.set_by_operator("laser_area_safe", True, by="  ", source="web")
    assert rec.calls == [], "a write that did not happen is not announced"
    assert s.get("laser_area_safe").value is True


def test_a_lapse_by_time_is_not_a_write(store):
    store.set_by_operator("laser_area_safe", True, by="alice", source="web")
    rec = Recorder(store)
    store.add_listener(rec)
    store._values["laser_area_safe"].valid_until = "2000-01-01T00:00:00+00:00"
    assert store.is_valid("laser_area_safe") is False
    assert store.snapshot()["laser_area_safe"]["valid"] is False
    assert rec.calls == [], "expiry calls nobody (the interlock must also check at use time)"


def test_the_listener_gets_its_own_copy_of_the_record(store):
    mine = []
    store.add_listener(lambda slot, record: (mine.append(record), record.update(value="tampered"),
                                             record["client"].update(peer="forged")))
    seen_later = Recorder(store)
    store.add_listener(seen_later)
    store.set_by_operator("objective", "10x", by="alice", source="web", client={"peer": "127.0.0.1"})
    assert store.get("objective").value == "10x" and store.get("objective").client == {"peer": "127.0.0.1"}
    assert store.history[-1]["value"] == "10x" and store.history[-1]["client"] == {"peer": "127.0.0.1"}
    assert seen_later.calls[0][1]["value"] == "10x", "one listener cannot change what the next one sees"
    assert seen_later.calls[0][1]["client"] == {"peer": "127.0.0.1"}


# ── a listener that misbehaves ────────────────────────────────────────────────


def test_a_listener_that_raises_is_logged_and_skipped_and_the_write_stands(store, caplog):
    before, after = Recorder(store), Recorder(store)

    def explode(slot, record):
        raise ZeroDivisionError("the interlock itself crashed")

    store.add_listener(before)
    store.add_listener(explode)
    store.add_listener(after)
    with caplog.at_level(logging.ERROR, logger="mast.operator_hitl.optical_state"):
        sv = store.set_by_operator("laser_area_safe", False, by="alice", source="web")
    assert sv.value is False and store.get("laser_area_safe").value is False, "the write stands"
    assert len(before.calls) == 1 and len(after.calls) == 1, "the other listeners still run"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, "not silent: one ERROR with the traceback"
    assert "explode" in errors[0].getMessage() and "'laser_area_safe'" in errors[0].getMessage()
    assert errors[0].exc_info and "the interlock itself crashed" in str(errors[0].exc_info[1])


def test_a_listener_that_raises_on_a_clear_does_not_undo_the_clear(store, caplog):
    store.set_by_operator("laser_area_safe", True, by="alice", source="web")
    store.add_listener(lambda s, r: 1 / 0)
    with caplog.at_level(logging.ERROR, logger="mast.operator_hitl.optical_state"):
        store.clear_by_operator("laser_area_safe", by="alice", source="web")
    assert store.get("laser_area_safe") is None
    assert any("failed after a write" in r.getMessage() for r in caplog.records)


def test_the_store_lock_is_not_held_while_a_listener_runs(store):
    """A listener may read the store and may write it from another thread: with the lock held by
    the notifying thread, the join below would never return."""
    done = []

    def listener(slot, record):
        if slot != "objective":
            return

        def other_writer():
            store.set_by_operator("edge_filter", "LP532", by="bob", source="web")
            done.append(store.get("objective").value)

        t = threading.Thread(target=other_writer, daemon=True)
        t.start()
        t.join(5)
        assert not t.is_alive(), "deadlock: the store's lock is held while listeners run"

    store.add_listener(listener)
    store.set_by_operator("objective", "50x", by="alice", source="web")
    assert done == ["50x"] and store.get("edge_filter").value == "LP532"


# ── adding and removing ───────────────────────────────────────────────────────


def test_add_is_idempotent_remove_says_whether_it_removed_and_both_are_safe(store):
    rec = Recorder(store)
    store.add_listener(rec)
    store.add_listener(rec)
    assert store.listeners == (rec,)
    store.set_by_operator("objective", "10x", by="a", source="web")
    assert len(rec.calls) == 1, "the same callable registered twice is called once"
    assert store.remove_listener(rec) is True
    assert store.remove_listener(rec) is False, "not registered: no error"
    store.set_by_operator("objective", "50x", by="a", source="web")
    assert len(rec.calls) == 1
    with pytest.raises(TypeError):
        store.add_listener("not callable")


def test_a_listener_may_remove_itself_and_others_while_being_called(store):
    calls = []

    def once(slot, record):
        calls.append("once")
        store.remove_listener(once)
        store.remove_listener(other)

    def other(slot, record):
        calls.append("other")

    store.add_listener(once)
    store.add_listener(other)
    store.set_by_operator("objective", "10x", by="a", source="web")
    assert calls == ["once", "other"], "this round's snapshot of the listeners is still delivered"
    store.set_by_operator("objective", "50x", by="a", source="web")
    assert calls == ["once", "other"], "and neither is called again"


def test_adding_removing_and_being_notified_are_thread_safe(store):
    stable_calls = []
    lock = threading.Lock()

    def stable(slot, record):
        with lock:
            stable_calls.append(slot)

    store.add_listener(stable)
    errors = []
    stop = threading.Event()

    def churn():
        def temp(slot, record):
            pass

        try:
            while not stop.is_set():
                store.add_listener(temp)
                store.remove_listener(temp)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def writer(i):
        try:
            for j in range(25):
                if j % 5 == 4:
                    store.clear_by_operator("objective", by=f"w{i}", source="web")
                else:
                    store.set_by_operator("objective", "10x" if j % 2 else "50x", by=f"w{i}", source="web")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    churners = [threading.Thread(target=churn) for _ in range(3)]
    writers = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in (*churners, *writers):
        t.start()
    for t in writers:
        t.join(30)
    stop.set()
    for t in churners:
        t.join(10)
    assert not errors, errors
    assert len(stable_calls) == 8 * 25, "every committed write was delivered to the stable listener once"
    assert store.listeners == (stable,), "no churn listener was left behind"


# ── deferral (used by attest answers) ─────────────────────────────────────────


def test_deferred_notifications_wait_for_the_end_of_the_block_and_keep_their_order(store):
    rec = Recorder(store)
    store.add_listener(rec)
    with store.deferred_notifications():
        store.set_by_operator("objective", "10x", by="a", source="web")
        store.clear_by_operator("objective", by="a", source="web")
        assert rec.calls == [], "held back inside the block"
        with store.deferred_notifications():            # nested: the outer block delivers
            store.set_by_operator("objective", "50x", by="a", source="web")
        assert rec.calls == []
    assert [(s, None if r is None else r["value"]) for s, r, _, _ in rec.calls] == [
        ("objective", "10x"), ("objective", None), ("objective", "50x")]
    store.set_by_operator("objective", "100x", by="a", source="web")
    assert len(rec.calls) == 4, "outside a block it is immediate again"


def test_deferred_notifications_are_delivered_even_when_the_block_raises(store):
    rec = Recorder(store)
    store.add_listener(rec)
    with pytest.raises(RuntimeError):
        with store.deferred_notifications():
            store.set_by_operator("laser_area_safe", False, by="a", source="web")
            raise RuntimeError("something after the write failed")
    assert [(s, r["value"]) for s, r, _, _ in rec.calls] == [("laser_area_safe", False)], \
        "the write was committed, so it is announced"


def test_another_threads_writes_are_not_held_back_by_my_block(store):
    rec = Recorder(store)
    store.add_listener(rec)
    with store.deferred_notifications():
        t = threading.Thread(target=lambda: store.set_by_operator("edge_filter", "LP", by="b", source="web"))
        t.start()
        t.join(5)
        assert [c[0] for c in rec.calls] == ["edge_filter"], "deferral is per thread"


def test_an_attest_answer_notifies_after_the_prompt_lock_is_released(store):
    """The slots of an attest answer are written while the prompt backend holds its lock. A slow
    listener (it closes a laser: serial I/O) must not hold up everything that reads the prompt
    list, so the notification waits until that lock is released — and the prompt is then already
    answered."""
    pid = prompts.post(prompts.OperatorPrompt(kind="attest", title="Area safe?", attest_slots=["laser_area_safe"]))
    seen = {}

    def listener(slot, record):
        t = threading.Thread(target=lambda: seen.update(open_ids=[p.id for p in prompts.list_open()],
                                                       status=prompts.get(pid).status), daemon=True)
        t.start()
        t.join(3)
        seen["blocked"] = t.is_alive()
        seen["record"] = record

    store.add_listener(listener)
    prompts.answer(pid, by="alice", source="web", value=False, client={"peer": "127.0.0.1"})
    assert seen["blocked"] is False, "the prompt list was readable from the listener: its lock was released"
    assert seen["status"] == "answered" and seen["open_ids"] == []
    assert seen["record"]["value"] is False and seen["record"]["source"] == f"prompt:{pid}"
    assert seen["record"]["client"] == {"peer": "127.0.0.1"}, "the answering request's client reaches the hook"


def test_a_listener_error_during_an_attest_answer_does_not_fail_the_answer(store, caplog):
    pid = prompts.post(prompts.OperatorPrompt(kind="attest", title="Area safe?", attest_slots=["laser_area_safe"]))
    store.add_listener(lambda s, r: 1 / 0)
    with caplog.at_level(logging.ERROR, logger="mast.operator_hitl.optical_state"):
        ans = prompts.answer(pid, by="alice", source="web", value=True)
    assert ans.status == "answered" and store.is_valid("laser_area_safe", expect=True)
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_the_persistent_store_notifies_like_the_memory_one(op_root):
    s = PersistentOpticalStateStore(op_root)
    rec = Recorder(s)
    s.add_listener(rec)
    s.set_by_operator("laser_area_safe", True, by="alice", source="launcher")
    s.clear_by_operator("laser_area_safe", by="alice", source="launcher")
    assert [(c[0], c[1] is None) for c in rec.calls] == [("laser_area_safe", False), ("laser_area_safe", True)]
    # and both writes were on disk before anybody heard of them
    assert s.history_path.read_text(encoding="utf-8").count("\n") == 2


# ── the wiring the integration uses ───────────────────────────────────────────


def test_a_listener_added_after_install_hears_the_operator_api_with_the_clients_record(installed):
    """``service.install()`` first (it swaps in the persistent store), then ``add_listener`` on the
    active store: what a person does in the web UI or the launcher reaches the hook, and the
    record says where the request came from."""
    active = optical_state.get_store()
    assert active is installed.store
    rec = Recorder(active)
    active.add_listener(rec)
    assert len(active.listeners) == 2, "next to the service's own pointer-event listener"

    app = FastAPI()
    app.include_router(operator_routes.router, prefix="/api")
    c = TestClient(app, base_url="http://127.0.0.1:7862")
    headers = {"Content-Type": "application/json", "User-Agent": "Python-urllib/3.13"}

    r = c.put("/api/operator/optical-state/laser_area_safe", json={"by": "alice", "value": False}, headers=headers)
    assert r.status_code == 200, r.text
    r = c.post("/api/operator/optical-state/laser_area_safe/clear", json={"by": "alice"}, headers=headers)
    assert r.status_code == 200, r.text
    pid = prompts.post(prompts.OperatorPrompt(kind="attest", title="Area safe?", attest_slots=["laser_area_safe"]))
    r = c.post(f"/api/operator/prompts/{pid}/answer", json={"by": "alice", "value": True}, headers=headers)
    assert r.status_code == 200, r.text

    assert [(slot, None if record is None else record["value"]) for slot, record, _, _ in rec.calls] == [
        ("laser_area_safe", False), ("laser_area_safe", None), ("laser_area_safe", True)]
    set_false, _, set_true = (record for _, record, _, _ in rec.calls)
    assert set_false["client"]["user_agent"] == "Python-urllib/3.13" and set_false["client"]["browser"] is False
    assert set_true["source"] == f"prompt:{pid}" and set_true["client"]["peer"] == "testclient"
