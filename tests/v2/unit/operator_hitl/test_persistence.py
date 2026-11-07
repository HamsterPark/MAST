"""Persistent operator prompts and optical state: files, restarts, failures that must not be silent.

What is pinned here (each one a way this kind of store goes wrong quietly):

* the files are written atomically, UTF-8 **without** a BOM, and a BOM in a hand-edited file is
  tolerated on read;
* a file that cannot be read is a WARNING naming it, never a silent fall back; an unreadable
  prompts file is kept aside instead of being overwritten;
* a prompt left open when the service stopped is ``cancelled`` ("service restarted") on the next
  start, ``seq`` keeps growing and ids do not repeat;
* an optical-state write that cannot be saved does not happen (rolled back, error reaches the
  caller, the history says so).
"""
from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from mast.i18n import tr
from mast.operator_hitl import optical_state, prompts, service, store
from mast.operator_hitl.store import (
    HISTORY_FILE,
    PROMPTS_FILE,
    STATE_FILE,
    PersistentOpticalStateStore,
    PersistentPromptBackend,
    load_slot_definitions,
    operator_dir,
    station_config_path,
)

BOM = b"\xef\xbb\xbf"


def _confirm(title="Continue?", **kw):
    return prompts.OperatorPrompt(kind="confirm", title=title, **kw)


def _post(backend, **kw):
    return backend.post(_confirm(**kw))


# ── prompts ─────────────────────────────────────────────────────────────────


def test_a_prompt_and_its_answer_survive_a_restart(op_root):
    b = PersistentPromptBackend(op_root)
    pid = b.post(prompts.OperatorPrompt(kind="number", title="Angle?", number_min=0, number_max=360,
                                        unit="deg", agent_id="ext:tester",
                                        context={"experiment_id": "e1"}))
    b.answer(pid, prompts.OperatorAnswer(prompt_id=pid, status="answered", value=45.0,
                                         answered_by="alice", source="web", answered_at="t"))
    b2 = PersistentPromptBackend(op_root)               # the service restarts
    p = b2.get(pid)
    assert p is not None and p.kind == "number" and p.unit == "deg" and p.agent_id == "ext:tester"
    assert p.status == "answered" and p.context == {"experiment_id": "e1"}
    ans = b2.get_answer(pid)
    assert ans.value == 45.0 and ans.answered_by == "alice" and ans.source == "web"
    assert b2.seq == b.seq


def test_the_files_are_utf8_without_bom_and_atomic(op_root):
    b = PersistentPromptBackend(op_root)
    _post(b, title="换一片 λ/2 吗？", message="532 nm → 1064 nm")
    path = operator_dir(op_root) / PROMPTS_FILE
    raw = path.read_bytes()
    assert not raw.startswith(BOM), "a BOM makes json.loads fail in the repo's other loaders"
    data = json.loads(raw.decode("utf-8"))
    assert data["prompts"][0]["prompt"]["title"] == "换一片 λ/2 吗？"
    assert "换一片" in raw.decode("utf-8"), "ensure_ascii=False: the file is readable by a person"
    leftovers = [p.name for p in operator_dir(op_root).iterdir() if ".tmp-" in p.name]
    assert leftovers == [], "the temp file of an atomic write must not stay behind"


def test_a_bom_in_the_prompts_file_is_tolerated(op_root):
    d = operator_dir(op_root)
    d.mkdir(parents=True)
    body = {"version": 1, "seq": 7, "prompts": []}
    (d / PROMPTS_FILE).write_bytes(BOM + json.dumps(body).encode("utf-8"))
    b = PersistentPromptBackend(op_root)
    assert b.seq == 7 and b.list_open() == []


def test_unanswered_prompts_are_cancelled_when_the_service_restarts(op_root):
    b = PersistentPromptBackend(op_root)
    open_id = _post(b, title="Nobody answered me")
    done_id = _post(b, title="Answered")
    b.answer(done_id, prompts.OperatorAnswer(prompt_id=done_id, status="answered", choice="yes",
                                             answered_by="alice", source="web", answered_at="t"))
    seq_before = b.seq
    b2 = PersistentPromptBackend(op_root)
    assert b2.list_open() == []
    p = b2.get(open_id)
    assert p.status == "cancelled"
    ans = b2.get_answer(open_id)
    assert ans.status == "cancelled" and ans.source == "system"
    assert ans.note == tr("服务重启", "service restarted")
    assert b2.get(done_id).status == "answered"           # an answered one is left alone
    assert b2.seq > seq_before                            # the cancellation is a change
    # the file says so too: a third start does not "cancel" it again
    b3 = PersistentPromptBackend(op_root)
    assert b3.get_answer(open_id).note == tr("服务重启", "service restarted")
    assert b3.seq == b2.seq


def test_a_waiting_ask_is_not_confused_by_the_restart_marking(op_root):
    """The restart marks only what was open in the *file*; a prompt posted afterwards is open."""
    b = PersistentPromptBackend(op_root)
    _post(b, title="old")
    b2 = PersistentPromptBackend(op_root)
    new_id = _post(b2, title="new")
    assert [p.id for p in b2.list_open()] == [new_id]


def test_seq_keeps_growing_across_restarts_and_ids_do_not_repeat(op_root):
    b = PersistentPromptBackend(op_root)
    ids = {_post(b, title=f"q{i}") for i in range(3)}
    seq = b.seq
    b2 = PersistentPromptBackend(op_root)
    assert b2.seq >= seq, "a poller comparing 'greater than' would stall if seq went back"
    new_id = _post(b2, title="after the restart")
    assert new_id not in ids
    numbers = sorted(int(i.split("-")[1]) for i in [*ids, new_id])
    assert numbers == sorted(set(numbers)), "the numeric part of an id continues, it does not restart"


def test_a_corrupt_prompts_file_warns_and_is_set_aside(op_root, caplog):
    d = operator_dir(op_root)
    d.mkdir(parents=True)
    (d / PROMPTS_FILE).write_text("{ this is not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="mast.operator_hitl.store"):
        b = PersistentPromptBackend(op_root)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(PROMPTS_FILE in w and "not valid JSON" in w for w in warnings), warnings
    assert b.list_open() == [] and b.seq == 0
    aside = [p.name for p in d.iterdir() if ".corrupt-" in p.name]
    assert len(aside) == 1, "the unreadable file is kept as evidence, not overwritten"
    pid = _post(b, title="works again")
    assert json.loads((d / PROMPTS_FILE).read_text(encoding="utf-8"))["prompts"][0]["prompt"]["id"] == pid
    assert (d / aside[0]).read_text(encoding="utf-8").startswith("{ this is not")


def test_a_prompts_file_of_the_wrong_shape_is_a_warning_too(op_root, caplog):
    d = operator_dir(op_root)
    d.mkdir(parents=True)
    (d / PROMPTS_FILE).write_text(json.dumps({"prompts": "nope"}), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="mast.operator_hitl.store"):
        PersistentPromptBackend(op_root)
    assert any("no 'prompts' list" in r.getMessage() for r in caplog.records)


def test_an_unreadable_entry_is_skipped_loudly_and_the_rest_loads(op_root, caplog):
    b = PersistentPromptBackend(op_root)
    good = _post(b, title="fine")
    path = operator_dir(op_root) / PROMPTS_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    data["prompts"].insert(0, {"prompt": {"kind": "confirm"}})          # no title, no id
    path.write_text(json.dumps(data), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="mast.operator_hitl.store"):
        b2 = PersistentPromptBackend(op_root)
    assert b2.get(good) is not None
    assert any("skipped an unreadable entry" in r.getMessage() for r in caplog.records)


def test_a_failing_save_never_blocks_the_prompt_and_is_reported(op_root, monkeypatch, caplog):
    b = PersistentPromptBackend(op_root)

    def boom(path, obj):
        raise PermissionError("disk is read-only")

    monkeypatch.setattr(store, "atomic_write_json", boom)
    with caplog.at_level(logging.WARNING, logger="mast.operator_hitl.store"):
        pid = _post(b, title="still works from memory")
    assert b.get(pid).status == "open"
    assert "disk is read-only" in b.last_save_error
    assert any("could not be saved" in r.getMessage() for r in caplog.records)
    prompts_view_backend = prompts.set_backend(b)
    try:
        assert "read-only" in service.prompts_view()["persist_error"]
    finally:
        prompts.set_backend(prompts_view_backend)
    monkeypatch.undo()
    _post(b, title="the disk is back")
    assert b.last_save_error == ""


def test_nothing_is_written_until_something_happens(op_root):
    PersistentPromptBackend(op_root)
    PersistentOpticalStateStore(op_root)
    assert not operator_dir(op_root).exists(), "an STM station that never asks must leave no trace"


def test_a_prompt_with_a_timeout_expires_by_itself(op_root):
    b = PersistentPromptBackend(op_root)
    pid = b.post(_confirm(timeout_s=0.05))
    time.sleep(0.12)
    assert b.get(pid).status == "timeout"
    ans = b.get_answer(pid)
    assert ans.status == "timeout" and ans.source == "system"
    assert b.list_open() == []
    # it was saved as closed: a restart does not call it "service restarted"
    b2 = PersistentPromptBackend(op_root)
    assert b2.get_answer(pid).status == "timeout"


def test_an_expired_prompt_can_no_longer_be_answered(op_root):
    old = prompts.set_backend(PersistentPromptBackend(op_root))
    try:
        pid = prompts.post(_confirm(timeout_s=0.05))
        time.sleep(0.12)
        with pytest.raises(prompts.PromptClosed, match="already timeout"):
            prompts.answer(pid, by="alice", source="web", choice="yes")
    finally:
        prompts.set_backend(old)


def test_wait_wakes_at_the_prompts_expiry_without_anyone_touching_the_backend(op_root):
    b = PersistentPromptBackend(op_root)
    pid = b.post(_confirm(timeout_s=0.1))
    t0 = time.monotonic()
    ans = b.wait(pid, 5.0)
    assert ans is not None and ans.status == "timeout"
    assert time.monotonic() - t0 < 2.0


def test_closed_prompts_are_bounded_open_ones_are_not(op_root):
    b = PersistentPromptBackend(op_root, max_closed=3)
    still_open = _post(b, title="stays")
    for i in range(6):
        pid = _post(b, title=f"q{i}")
        b.close(pid, "dismissed", "x")
    seq, entries = b.snapshot("all", 100)
    closed = [e for e in entries if e["status"] != "open"]
    assert len(closed) == 3 and [e["title"] for e in closed] == ["q5", "q4", "q3"]
    assert [e["id"] for e in entries if e["status"] == "open"] == [still_open]


def test_ask_blocked_in_one_thread_gets_the_answer_given_from_another(installed):
    result = {}

    def asker():
        result["ans"] = prompts.ask(_confirm(title="Swap done?"), timeout_s=10, poll_s=0.05)

    t = threading.Thread(target=asker)
    t.start()
    deadline = time.monotonic() + 5
    while not prompts.list_open() and time.monotonic() < deadline:
        time.sleep(0.01)
    (p,) = prompts.list_open()
    prompts.answer(p.id, by="alice", source="web", choice="yes", note="done")
    t.join(5)
    assert result["ans"].status == "answered" and result["ans"].note == "done"
    assert json.loads((operator_dir(installed.root) / PROMPTS_FILE).read_text("utf-8"))["seq"] == installed.prompts.seq


# ── optical state: slot definitions ─────────────────────────────────────────


def _write_config(root, body, *, bom=False):
    path = station_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    path.write_bytes(BOM + raw if bom else raw)
    return path


SLOT_A = {"id": "sample_holder", "label": "样品座 / sample holder", "kind": "choice",
          "options": [{"id": "cryo", "label": "cryostat"}, {"id": "stage", "label": "stage"}]}


def test_without_a_config_file_the_builtin_slots_apply_quietly(op_root, caplog):
    with caplog.at_level(logging.DEBUG, logger="mast.operator_hitl.store"):
        slots = load_slot_definitions(station_config_path(op_root))
    assert slots == optical_state.DEFAULT_SLOTS
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_slots_come_from_the_config_file(op_root):
    _write_config(op_root, {"slots": [SLOT_A]})
    s = PersistentOpticalStateStore(op_root)
    assert [x.id for x in s.slots()] == ["sample_holder"]
    assert s.slot("sample_holder").option("cryo")["label"] == "cryostat"


def test_a_config_saved_with_a_bom_loads(op_root):
    """Notepad and PowerShell 5.1 write a BOM; the repo's other loaders then fall back to
    defaults silently. This one reads it."""
    _write_config(op_root, {"slots": [SLOT_A]}, bom=True)
    assert [x.id for x in PersistentOpticalStateStore(op_root).slots()] == ["sample_holder"]


def test_extend_defaults_keeps_the_builtin_slots(op_root):
    override = {"id": "objective", "label": "objective", "kind": "choice",
                "options": [{"id": "20x", "label": "20x"}]}
    _write_config(op_root, {"extend_defaults": True, "slots": [SLOT_A, override]})
    s = PersistentOpticalStateStore(op_root)
    ids = [x.id for x in s.slots()]
    assert "sample_holder" in ids and "laser_area_safe" in ids and "excitation_waveplate" in ids
    assert [o["id"] for o in s.slot("objective").options] == ["20x"], "the file overrides by id"


@pytest.mark.parametrize("body, why", [
    ("{ not json", "not valid JSON"),
    ({"slots": []}, "non-empty list"),
    ({"slots": [{"label": "no id"}]}, "KeyError"),
    ({"slots": [{"id": "x", "kind": "weird"}]}, "not one of"),
    ({"slots": [SLOT_A, SLOT_A]}, "duplicate slot ids"),
    ({"slots": [{"id": "x", "kind": "choice"}]}, "has no options"),
    ({"slots": [{"id": "x", "kind": "number", "min": "low"}]}, "ValueError"),
])
def test_a_config_that_cannot_be_used_is_a_warning_and_the_defaults_apply(op_root, caplog, body, why):
    path = station_config_path(op_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="mast.operator_hitl.store"):
        slots = load_slot_definitions(path)
    assert slots == optical_state.DEFAULT_SLOTS, "never a half-applied file"
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert msgs and "optical_station.json" in msgs[0] and why in msgs[0], msgs


# ── optical state: values and history ───────────────────────────────────────


def test_values_and_history_survive_a_restart(op_root):
    s = PersistentOpticalStateStore(op_root)
    s.set_by_operator("objective", "50x", by="alice", source="web", note="turned by hand")
    s.set_by_operator("laser_area_safe", True, by="alice", source="launcher")
    s.clear_by_operator("objective", by="bob", source="web")
    s.set_by_operator("incident_polarizer_deg", 45, by="alice", source="web")

    s2 = PersistentOpticalStateStore(op_root)                 # restart
    assert s2.get("objective") is None, "the clear survived"
    sv = s2.get("incident_polarizer_deg")
    assert sv.value == 45.0 and sv.set_by == "alice" and sv.source == "web"
    assert s2.is_valid("laser_area_safe", expect=True)
    assert s2.get("laser_area_safe").valid_until == s.get("laser_area_safe").valid_until, \
        "a restart must not extend an attestation's validity"
    assert s2.seq >= 4

    lines = (operator_dir(op_root) / HISTORY_FILE).read_text(encoding="utf-8").splitlines()
    recs = [json.loads(x) for x in lines]
    assert [(r["slot"], r["value"]) for r in recs] == [
        ("objective", "50x"), ("laser_area_safe", True), ("objective", None),
        ("incident_polarizer_deg", 45.0)]
    assert recs[0]["note"] == "turned by hand" and recs[2]["note"] == "cleared"
    assert not (operator_dir(op_root) / HISTORY_FILE).read_bytes().startswith(BOM)
    # the history is readable across restarts, oldest first, limited from the end
    assert [r["slot"] for r in s2.history_tail(2)] == ["objective", "incident_polarizer_deg"]
    assert s2.history_tail(100) == recs


def test_an_expired_attestation_stays_expired_after_a_restart(op_root):
    d = operator_dir(op_root)
    d.mkdir(parents=True)
    (d / STATE_FILE).write_text(json.dumps({"version": 1, "seq": 3, "values": {
        "laser_area_safe": {"value": True, "set_by": "alice", "set_at": "2000-01-01T00:00:00+00:00",
                            "source": "web", "valid_until": "2000-01-01T08:00:00+00:00", "note": ""}}}),
        encoding="utf-8")
    s = PersistentOpticalStateStore(op_root)
    assert s.get("laser_area_safe").value is True
    assert not s.is_valid("laser_area_safe"), "eight hours long past: not a valid attestation"
    assert s.snapshot()["laser_area_safe"]["valid"] is False


def test_the_state_file_has_no_bom(op_root):
    PersistentOpticalStateStore(op_root).set_by_operator("objective", "10x", by="a", source="web")
    assert not (operator_dir(op_root) / STATE_FILE).read_bytes().startswith(BOM)


def test_a_write_that_cannot_be_saved_does_not_happen(op_root, monkeypatch):
    s = PersistentOpticalStateStore(op_root)
    s.set_by_operator("objective", "10x", by="alice", source="web")

    def boom():
        raise PermissionError("read-only file system")

    monkeypatch.setattr(s, "_write_values", boom)
    with pytest.raises(PermissionError):
        s.set_by_operator("objective", "100x", by="alice", source="web")
    assert s.get("objective").value == "10x", "the old value is back"
    with pytest.raises(PermissionError):
        s.set_by_operator("laser_area_safe", True, by="alice", source="web")
    assert s.get("laser_area_safe") is None and not s.is_valid("laser_area_safe"), \
        "an attestation the audit trail never saw must not stay in force"
    with pytest.raises(PermissionError):
        s.clear_by_operator("objective", by="alice", source="web")
    assert s.get("objective").value == "10x"
    assert [h["slot"] for h in s.history] == ["objective"], "only the write that happened is remembered"
    # the file notes that its lines were not applied
    recs = [json.loads(x) for x in (operator_dir(op_root) / HISTORY_FILE).read_text("utf-8").splitlines()]
    voids = [r for r in recs if r["source"] == "system:void"]
    assert len(voids) == 3 and all("was not applied" in r["note"] for r in voids)
    monkeypatch.undo()
    s.set_by_operator("laser_area_safe", True, by="alice", source="web")      # the disk is back
    assert PersistentOpticalStateStore(op_root).is_valid("laser_area_safe", expect=True)


def test_a_history_that_cannot_be_appended_fails_the_write_cleanly(op_root, monkeypatch):
    s = PersistentOpticalStateStore(op_root)

    def boom(rec):
        raise OSError("no space left on device")

    monkeypatch.setattr(s, "_append_history", boom)
    with pytest.raises(OSError):
        s.set_by_operator("objective", "10x", by="alice", source="web")
    assert s.get("objective") is None and s.history == []


def test_a_stored_value_whose_slot_is_gone_or_no_longer_fits_is_dropped_with_a_warning(op_root, caplog):
    s = PersistentOpticalStateStore(op_root)
    s.set_by_operator("objective", "50x", by="alice", source="web")
    s.set_by_operator("edge_filter", "LP532", by="alice", source="web")
    _write_config(op_root, {"extend_defaults": False, "slots": [
        {"id": "objective", "label": "obj", "kind": "choice", "options": [{"id": "20x", "label": "20x"}]},
        SLOT_A]})                                                    # edge_filter is gone; 50x no longer fits
    with caplog.at_level(logging.INFO, logger="mast.operator_hitl.store"):
        s2 = PersistentOpticalStateStore(op_root)
    assert s2.get("objective") is None
    msgs = [r.getMessage() for r in caplog.records]
    assert any("'edge_filter' is no longer defined" in m for m in msgs)
    assert any("'objective' no longer fits" in m for m in msgs)


def test_an_unreadable_state_file_is_a_warning_and_is_kept_aside(op_root, caplog):
    d = operator_dir(op_root)
    d.mkdir(parents=True)
    (d / STATE_FILE).write_text("garbage", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="mast.operator_hitl.store"):
        s = PersistentOpticalStateStore(op_root)
    assert any(STATE_FILE in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert s.get("objective") is None
    assert [p.name for p in d.iterdir() if ".corrupt-" in p.name]


def test_a_damaged_history_line_is_skipped_loudly(op_root, caplog):
    s = PersistentOpticalStateStore(op_root)
    s.set_by_operator("objective", "10x", by="a", source="web")
    with open(operator_dir(op_root) / HISTORY_FILE, "a", encoding="utf-8") as fh:
        fh.write("{half a line\n")
    s.set_by_operator("objective", "50x", by="a", source="web")
    with caplog.at_level(logging.WARNING, logger="mast.operator_hitl.store"):
        recs = s.history_tail(10)
    assert [r["value"] for r in recs] == ["10x", "50x"]
    assert any("unreadable line" in r.getMessage() for r in caplog.records)


# ── the in-memory store keeps its old behaviour, plus validation without side effects ──


def test_validate_value_changes_nothing(memory_backends):
    s = optical_state.get_store()
    assert s.validate_value("incident_polarizer_deg", "45") == 45.0
    with pytest.raises(optical_state.OpticalStateError):
        s.validate_value("incident_polarizer_deg", 400)
    with pytest.raises(optical_state.UnknownSlot):
        s.validate_value("nope", 1)
    assert s.get("incident_polarizer_deg") is None and s.history == [] and s.seq == 0


def test_clear_needs_a_name_like_set_does(memory_backends):
    with pytest.raises(optical_state.OpticalStateError, match="operator's name"):
        optical_state.get_store().clear_by_operator("objective", by=" ", source="web")


# ── the service: install / uninstall / notifications ────────────────────────


def test_install_makes_the_persistent_backends_active_and_uninstall_restores(op_root, memory_backends):
    before = (prompts.get_backend(), optical_state.get_store())
    inst = service.install(op_root)
    try:
        assert prompts.get_backend() is inst.prompts and optical_state.get_store() is inst.store
        assert isinstance(inst.prompts, PersistentPromptBackend)
        assert service.install(op_root) is inst, "idempotent for the same root"
    finally:
        service.uninstall()
    assert (prompts.get_backend(), optical_state.get_store()) == before
    assert service.installed() is None
    service.uninstall()                                         # a second call is harmless


def test_install_with_another_root_replaces_but_still_restores_the_original(op_root, tmp_path, memory_backends):
    before = (prompts.get_backend(), optical_state.get_store())
    first = service.install(op_root)
    other = tmp_path / "other"
    second = service.install(other)
    try:
        assert second is not first and prompts.get_backend() is second.prompts
        assert first.prompts.listeners == [] and first.store.listeners == (), "the old one is unhooked"
        assert second.store.listeners and second.prompts.listeners, "the new one is hooked"
    finally:
        service.uninstall()
    assert (prompts.get_backend(), optical_state.get_store()) == before


def test_install_defaults_to_the_projects_data_root(op_root, memory_backends):
    inst = service.install()
    try:
        assert inst.root == op_root
    finally:
        service.uninstall()


class _Bus:
    def __init__(self):
        from mast.core.events import EventBus

        self.bus = EventBus.get()
        self.events = []
        self._cb = lambda ev: self.events.append(ev) if ev.type.value == "operator_prompt" else None
        self.bus.subscribe(self._cb)

    def close(self):
        self.bus.unsubscribe(self._cb)


def test_posting_answering_and_closing_publish_pointer_events(installed):
    bus = _Bus()
    try:
        pid = prompts.post(_confirm(title="Is the area clear?", severity="safety"))
        prompts.answer(pid, by="alice", source="web", choice="yes")
        qid = prompts.post(_confirm(title="Another"))
        prompts.dismiss(qid, by="alice", source="launcher", reason="not needed")
        rid = prompts.post(_confirm(title="Third", timeout_s=0.05))
        time.sleep(0.12)
        prompts.get(rid)                                         # a read notices the expiry
    finally:
        bus.close()
    data = [e.data for e in bus.events if e.data["what"] == "prompt"]
    assert [(d["event"], d["prompt_id"], d["status"]) for d in data] == [
        ("posted", pid, "open"), ("answered", pid, "answered"),
        ("posted", qid, "open"), ("closed", qid, "dismissed"),
        ("posted", rid, "open"), ("closed", rid, "timeout")]
    assert data[0]["kind"] == "confirm" and data[0]["severity"] == "safety"
    seqs = [d["seq"] for d in data]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "every change has its own seq"
    # pointers only: no question text travels on the bus
    assert all("title" not in d and "message" not in d for d in data)


def test_slot_writes_publish_events_and_the_attest_answer_too(installed):
    bus = _Bus()
    try:
        optical_state.get_store().set_by_operator("objective", "10x", by="alice", source="web")
        optical_state.get_store().clear_by_operator("objective", by="alice", source="web")
        pid = prompts.post(prompts.OperatorPrompt(kind="attest", title="Area clear?",
                                                  attest_slots=["laser_area_safe"]))
        prompts.answer(pid, by="alice", source="web", value=True)
    finally:
        bus.close()
    kinds = [(e.data["what"], e.data["event"], e.data.get("slot")) for e in bus.events]
    assert kinds.index(("optical_state", "set", "objective")) < kinds.index(("optical_state", "clear", "objective")), \
        "a clear is announced as a clear, after the set it undoes"
    assert ("optical_state", "set", "laser_area_safe") in kinds
    assert ("prompt", "answered", None) in kinds


def test_the_views_the_api_serves(installed):
    pid = prompts.post(_confirm(title="Open one"))
    done = prompts.post(_confirm(title="Done one"))
    prompts.answer(done, by="alice", source="web", choice="no", note="later")
    v = service.prompts_view("open")
    assert [p["id"] for p in v["prompts"]] == [pid] and v["open_count"] == 1
    assert v["persistent"] is True and v["persist_error"] is None and v["prompts"][0]["answer"] is None
    v = service.prompts_view("all")
    assert [p["id"] for p in v["prompts"]] == [pid, done], "open first, then the closed ones"
    assert v["prompts"][1]["answer"]["choice"] == "no" and v["prompts"][1]["answer"]["note"] == "later"
    ov = service.optical_state_view()
    assert {s["id"] for s in ov["slots"]} >= {"objective", "laser_area_safe"}
    assert ov["values"]["objective"] == {"value": None, "valid": False} and ov["persistent"] is True
    row = next(r for r in service.prompt_rows() if r["id"] == done)
    assert row["status"] == "done" and row["prompt_status"] == "answered" and row["answer"]["ok"] is False
    open_row = next(r for r in service.prompt_rows() if r["id"] == pid)
    assert open_row["status"] == "pending" and open_row["answer"] is None
