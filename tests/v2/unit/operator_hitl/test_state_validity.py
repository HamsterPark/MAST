"""How long a persisted attestation is trusted - and what happens to a time that cannot be read.

``optical_state.json`` is a file a person (or a program with a shell) can edit. Reading a value
back used to take its ``valid_until`` as written, so "laser area safe, valid until 2999" kept an
8-hour attestation alive for ever; and a ``valid_until`` without a time zone made ``is_valid``
raise ``TypeError``, taking ``snapshot()`` - and with it every spectrum
sidecar and the operator's panel - down. Now:

* a value read back is valid until the *earlier* of its stored ``valid_until`` and
  ``set_at + valid_for_s`` (the slot's definition as it is now);
* a time that cannot be trusted - ``set_at`` missing / unreadable / without a time zone / in the
  future, a stored ``valid_until`` that is not a time with a time zone - makes the value count
  as expired (it stays visible: the operator sees it lapsed and signs again), with a WARNING
  that names the slot;
* ``is_valid`` and ``snapshot`` never raise on any of it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mast.api.routes import operator as routes
from mast.operator_hitl import optical_state, service
from mast.operator_hitl.optical_state import OpticalSlot, SlotValue, parse_aware, restored_valid_until
from mast.operator_hitl.store import STATE_FILE, PersistentOpticalStateStore, operator_dir
from mast.skills.builtins.operator_skills import ReadOpticalState

SAFE = "laser_area_safe"          # flag, valid for 8 hours
LOGGER = "mast.operator_hitl.store"


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def hours_ago(h: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=h)


def write_state(root, values: dict) -> None:
    d = operator_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / STATE_FILE).write_text(json.dumps({"version": 1, "seq": 3, "values": values}), encoding="utf-8")


def entry(value, *, set_at, valid_until=None, **extra) -> dict:
    return {"value": value, "set_by": "alice", "set_at": set_at, "source": "web",
            "valid_until": valid_until, "note": "", **extra}


def load(root, values) -> PersistentOpticalStateStore:
    write_state(root, values)
    return PersistentOpticalStateStore(root)


# ── the helper ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("ts, aware", [
    ("2026-11-03T10:00:00+00:00", True), ("2026-11-03T10:00:00Z", True), ("2026-11-03T18:00:00+08:00", True),
    ("2026-11-03T10:00:00", False), ("2026-11-03", False), ("", False), ("not a time", False),
    (None, False), (12345, False), ([], False), ({}, False), (True, False),
])
def test_only_a_string_time_with_a_zone_is_a_time(ts, aware):
    assert (parse_aware(ts) is not None) is aware


def test_restored_valid_until_is_the_earlier_of_the_two():
    slot = next(s for s in optical_state.DEFAULT_SLOTS if s.id == SAFE)
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=1)
    limit = start + timedelta(hours=8)
    end, problem = restored_valid_until(slot, iso(start), iso(now + timedelta(days=9999)), now=now)
    assert end == iso(limit) and "later than set_at + valid_for_s" in problem
    earlier = iso(now + timedelta(hours=2))
    assert restored_valid_until(slot, iso(start), earlier, now=now) == (earlier, "")
    assert restored_valid_until(slot, iso(start), None, now=now) == (iso(limit), "")


# ── reading a file back ─────────────────────────────────────────────────────────────────────


def test_an_honest_file_is_restored_as_it_was(op_root):
    start = hours_ago(1)
    until = iso(start + timedelta(hours=8))
    store = load(op_root, {SAFE: entry(True, set_at=iso(start), valid_until=until)})
    sv = store.get(SAFE)
    assert sv.valid_until == until and sv.value is True
    assert store.is_valid(SAFE, expect=True)


def test_a_valid_until_far_in_the_future_cannot_lengthen_an_attestation(op_root, caplog):
    start = hours_ago(10)                                            # signed 10 h ago, valid for 8
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        store = load(op_root, {SAFE: entry(True, set_at=iso(start), valid_until="2999-01-01T00:00:00+00:00")})
    sv = store.get(SAFE)
    assert sv.value is True, "the value stays visible ..."
    assert sv.valid_until == iso(start + timedelta(hours=8))
    assert not store.is_valid(SAFE) and not store.is_valid(SAFE, expect=True), "... but it has lapsed"
    assert store.snapshot()[SAFE]["valid"] is False
    assert any(SAFE in r.getMessage() and "later than set_at + valid_for_s" in r.getMessage()
               for r in caplog.records), "and the file's claim is not silently ignored"


def test_a_still_young_attestation_is_shortened_to_its_real_end(op_root):
    start = hours_ago(1)
    store = load(op_root, {SAFE: entry(True, set_at=iso(start), valid_until="2999-01-01T00:00:00+00:00")})
    assert store.get(SAFE).valid_until == iso(start + timedelta(hours=8))
    assert store.is_valid(SAFE, expect=True)


def test_an_earlier_stored_end_is_kept(op_root):
    start = hours_ago(1)
    soon = iso(datetime.now(timezone.utc) + timedelta(minutes=30))
    store = load(op_root, {SAFE: entry(True, set_at=iso(start), valid_until=soon)})
    assert store.get(SAFE).valid_until == soon


def test_a_missing_end_is_worked_out_from_the_slots_definition(op_root):
    start = hours_ago(9)
    store = load(op_root, {SAFE: entry(True, set_at=iso(start), valid_until=None)})
    assert store.get(SAFE).valid_until == iso(start + timedelta(hours=8)) and not store.is_valid(SAFE)


def test_a_slot_without_an_expiry_is_valid_until_it_is_changed(op_root):
    store = load(op_root, {"objective": entry("50x", set_at=iso(hours_ago(24 * 90)))})
    assert store.get("objective").valid_until is None and store.is_valid("objective", expect="50x")


def test_the_slots_definition_as_it_is_now_decides(op_root):
    """The slot used to be valid for 8 hours and is now defined as 1: a 2-hour-old signature has lapsed."""
    start = hours_ago(2)
    slots = [OpticalSlot(SAFE, "area", kind="flag", valid_for_s=3600)]
    write_state(op_root, {SAFE: entry(True, set_at=iso(start), valid_until=iso(start + timedelta(hours=8)))})
    store = PersistentOpticalStateStore(op_root, slots=slots)
    assert not store.is_valid(SAFE) and store.get(SAFE).valid_until == iso(start + timedelta(hours=1))


# ── times that cannot be trusted ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["2999-01-01T00:00:00", "tomorrow", "", 12345, [1], {"a": 1}, True])
def test_an_end_that_is_not_a_time_with_a_zone_is_expired_not_an_error(op_root, caplog, bad):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        store = load(op_root, {SAFE: entry(True, set_at=iso(hours_ago(1)), valid_until=bad)})
    sv = store.get(SAFE)
    assert sv is not None and sv.value is True
    assert store.is_valid(SAFE) is False and store.is_valid(SAFE, expect=True) is False
    snap = store.snapshot()                                       # does not raise
    assert snap[SAFE]["valid"] is False and snap[SAFE]["value"] is True
    assert parse_aware(sv.valid_until) is not None, "what is kept is a readable time (the moment it lapsed)"
    assert any(SAFE in r.getMessage() and "valid_until" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("bad", [None, "", "yesterday", "2026-11-03T10:00:00", 12345])
def test_a_start_that_is_not_a_time_with_a_zone_is_expired_too(op_root, caplog, bad):
    values = {"objective": entry("50x", set_at=bad)}
    if bad is None:
        del values["objective"]["set_at"]
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        store = load(op_root, values)
    assert store.get("objective").value == "50x"
    assert store.is_valid("objective") is False, "even a slot that never expires: nothing can be said about its age"
    assert store.snapshot()["objective"]["valid"] is False
    assert any("objective" in r.getMessage() and "set_at" in r.getMessage() for r in caplog.records)


def test_a_start_in_the_future_is_not_believed(op_root, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        store = load(op_root, {SAFE: entry(True, set_at="2999-01-01T00:00:00+00:00")})
    assert not store.is_valid(SAFE)
    assert any("in the future" in r.getMessage() for r in caplog.records)


def test_a_clock_that_ran_a_few_minutes_ahead_is_forgiven(op_root):
    start = datetime.now(timezone.utc) + timedelta(minutes=5)
    store = load(op_root, {SAFE: entry(True, set_at=iso(start))})
    assert store.is_valid(SAFE, expect=True) and store.get(SAFE).valid_until == iso(start + timedelta(hours=8))


def test_a_time_with_another_zone_is_compared_as_the_instant_it_is(op_root):
    start = hours_ago(1).astimezone(timezone(timedelta(hours=8)))
    store = load(op_root, {SAFE: entry(True, set_at=iso(start))})
    assert store.is_valid(SAFE, expect=True)


# ── nothing that reads the state can be brought down by it ─────────────────────────────────


def test_is_valid_and_snapshot_survive_a_value_with_a_naive_end_in_memory():
    store = optical_state.OpticalStateStore()
    for bad in ("2999-01-01T00:00:00", "garbage", 12345):
        store._values["objective"] = SlotValue(value="10x", set_by="a", set_at="x", source="web", valid_until=bad)
        assert store.is_valid("objective") is False
        assert store.snapshot()["objective"]["valid"] is False
    store._values["objective"] = SlotValue(value="10x", set_by="a", set_at="x", source="web",
                                           valid_until=iso(datetime.now(timezone.utc) + timedelta(hours=1)))
    assert store.is_valid("objective", expect="10x") is True


def test_the_operator_api_and_the_read_skill_report_a_lapsed_value_instead_of_failing(op_root):
    write_state(op_root, {
        SAFE: entry(True, set_at=iso(hours_ago(1)), valid_until="2999-01-01T00:00:00"),
        "objective": entry("50x", set_at="yesterday"),
        "edge_filter": entry("LP532", set_at=iso(hours_ago(1))),
    })
    inst = service.install(op_root)
    try:
        app = FastAPI()
        app.include_router(routes.router, prefix="/api")
        c = TestClient(app, base_url="http://127.0.0.1:7862")
        r = c.get("/api/operator/optical-state")
        assert r.status_code == 200, r.text
        values = r.json()["values"]
        assert values[SAFE]["valid"] is False and values[SAFE]["value"] is True
        assert values["objective"]["valid"] is False
        assert values["edge_filter"]["valid"] is True
        data = ReadOpticalState().execute(None, {}).data
        assert set(data["expired"]) == {SAFE, "objective"} and data["valid"] == ["edge_filter"]
        assert optical_state.get_store() is inst.store
    finally:
        service.uninstall()


def test_a_new_attestation_replaces_a_lapsed_one(op_root):
    store = load(op_root, {SAFE: entry(True, set_at=iso(hours_ago(1)), valid_until="2999-01-01T00:00:00")})
    assert not store.is_valid(SAFE)
    store.set_by_operator(SAFE, True, by="alice", source="web")
    assert store.is_valid(SAFE, expect=True)
    again = PersistentOpticalStateStore(op_root)                  # and survives the next restart as it is
    assert again.is_valid(SAFE, expect=True) and again.get(SAFE).valid_until == store.get(SAFE).valid_until
