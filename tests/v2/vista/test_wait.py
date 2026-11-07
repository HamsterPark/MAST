"""WaitHub (``mast.vista.wait``): conditions, cursors and gaps, alerts from the EventBus,
operator replies from the wishlist, bounded and cancellable waits."""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

import mast.vista.wait as W
from mast.core.events import Event, EventBus, EventType
from mast.vista.wait import (
    ConditionError,
    CursorError,
    WaitHub,
    alert_from_bus_event,
    parse_conditions,
)


def run(coro):
    return asyncio.run(coro)


def later(delay: float, fn, *args) -> threading.Timer:
    t = threading.Timer(delay, fn, args=args)
    t.start()
    return t


class FakeJob:
    def __init__(self, job_id: str, terminal: bool = False):
        self.job_id, self.terminal = job_id, terminal

    def view(self) -> dict:
        return {"job_id": self.job_id, "terminal": self.terminal,
                "state": "succeeded" if self.terminal else "running"}


def types_of(ans: dict) -> list[tuple]:
    return [(f["index"], f["type"]) for f in ans["fired"]]


# ── events, baseline, cursor ─────────────────────────────────────────────────
def test_events_before_the_call_do_not_fire_without_since():
    hub = WaitHub()
    hub.publish("frame", {"fid": "t0001.s0"})
    ans = run(hub.wait([{"type": "frame"}], timeout_s=0))
    assert ans["fired"] == [] and ans["timed_out"] is True


def test_an_event_during_the_wait_wakes_it_at_once():
    hub = WaitHub()
    later(0.2, hub.publish, "frame", {"fid": "t0002.s0"})
    t0 = time.monotonic()
    ans = run(hub.wait([{"type": "frame"}], timeout_s=10))
    assert time.monotonic() - t0 < 2.0
    assert ans["timed_out"] is False
    assert [f["event"]["fid"] for f in ans["fired"]] == ["t0002.s0"]
    assert ans["fired"][0]["index"] == 0 and isinstance(ans["fired"][0]["seq"], int)


def test_the_cursor_never_misses_an_event_between_two_waits():
    hub = WaitHub()
    c0 = hub.cursor()
    hub.publish("frame", {"fid": "t0003.s0"})          # between two waits
    t0 = time.monotonic()
    ans = run(hub.wait([{"type": "frame"}], timeout_s=10, since=c0))
    assert time.monotonic() - t0 < 1.0
    assert [f["event"]["fid"] for f in ans["fired"]] == ["t0003.s0"]
    again = run(hub.wait([{"type": "frame"}], timeout_s=0, since=ans["cursor"]))
    assert again["fired"] == [] and again["timed_out"] is True


def test_publish_rejects_an_unknown_kind_and_normalises_alert_levels():
    hub = WaitHub()
    with pytest.raises(ValueError):
        hub.publish("job", {})
    c = hub.cursor()
    hub.publish("alert", {"level": "warn", "message": "x"})
    ans = run(hub.wait([{"type": "alert", "min_level": "warning"}], timeout_s=0, since=c))
    assert ans["fired"][0]["event"]["level"] == "warning"


# ── job ──────────────────────────────────────────────────────────────────────
def test_job_conditions_unknown_terminal_and_finishing():
    jobs = {"j_done": FakeJob("j_done", True), "j_run": FakeJob("j_run", False)}
    hub = WaitHub(job_lookup=jobs.get)

    ans = run(hub.wait([{"type": "job", "job_id": "j_nope"}], timeout_s=10))
    assert ans["timed_out"] is False and ans["fired"][0]["event"]["error"] == "unknown_job"

    ans = run(hub.wait([{"type": "job", "job_id": "j_done"}], timeout_s=10))
    assert ans["fired"][0]["event"] == jobs["j_done"].view()

    def finish():
        jobs["j_run"].terminal = True

    later(0.3, finish)
    t0 = time.monotonic()
    ans = run(hub.wait([{"type": "job", "job_id": "j_run"}], timeout_s=10))
    assert 0.2 < time.monotonic() - t0 < 3.0
    assert ans["fired"][0]["event"]["state"] == "succeeded"


def test_a_failing_or_missing_job_lookup_fires_an_error_instead_of_hanging():
    def boom(job_id):
        raise RuntimeError("journal unreadable")

    ans = run(WaitHub(job_lookup=boom).wait([{"type": "job", "job_id": "j_1"}], timeout_s=10))
    assert ans["fired"][0]["event"]["error"] == "lookup_failed"
    ans = run(WaitHub().wait([{"type": "job", "job_id": "j_1"}], timeout_s=10))
    assert ans["fired"][0]["event"]["error"] == "jobs_unavailable"


# ── several conditions, filters, timer, timeout ──────────────────────────────
def test_everything_that_fired_in_one_check_comes_back_together():
    hub = WaitHub(job_lookup={"j_1": FakeJob("j_1", True)}.get)
    c = hub.cursor()
    hub.publish("frame", {"fid": "t0004.s0"})
    hub.publish("detector", {"detector_id": "det-a", "fid": "t0004.s0"})
    hub.publish("detector", {"detector_id": "det-b", "fid": "t0004.s0"})
    hub.publish("alert", {"level": "critical", "message": "rail"})
    hub.publish("alert", {"level": "info", "message": "fyi"})
    conds = [{"type": "frame"}, {"type": "detector", "detector_id": "det-b"},
             {"type": "alert"}, {"type": "job", "job_id": "j_1"}, {"type": "timer", "seconds": 0}]
    ans = run(hub.wait(conds, timeout_s=10, since=c))
    assert types_of(ans) == [(0, "frame"), (1, "detector"), (2, "alert"), (3, "job"), (4, "timer")]
    assert ans["fired"][1]["event"]["detector_id"] == "det-b"
    assert ans["fired"][2]["event"]["level"] == "critical"     # default min_level = warning


@pytest.mark.parametrize("min_level, expected", [
    ("info", ["info", "warning", "critical"]), ("warning", ["warning", "critical"]),
    ("critical", ["critical"]), ("crit", ["critical"])])
def test_alert_min_level(min_level, expected):
    hub = WaitHub()
    c = hub.cursor()
    for level in ("info", "warning", "critical"):
        hub.publish("alert", {"level": level})
    ans = run(hub.wait([{"type": "alert", "min_level": min_level}], timeout_s=0, since=c))
    assert [f["event"]["level"] for f in ans["fired"]] == expected


def test_timer_fires_and_timeout_is_honoured():
    hub = WaitHub()
    t0 = time.monotonic()
    ans = run(hub.wait([{"type": "timer", "seconds": 0.3}], timeout_s=10))
    assert 0.25 < time.monotonic() - t0 < 2.0
    assert ans["fired"][0]["type"] == "timer" and ans["timed_out"] is False
    t0 = time.monotonic()
    ans = run(hub.wait([{"type": "frame"}], timeout_s=0.3))
    assert ans["timed_out"] is True and ans["fired"] == []
    assert 0.25 < time.monotonic() - t0 < 2.0 and ans["waited_s"] >= 0.29
    assert ans["cursor"].startswith("c1.")


def test_many_matches_are_cut_and_counted():
    hub = WaitHub()
    c = hub.cursor()
    for i in range(30):
        hub.publish("frame", {"fid": f"t{i:04d}.s0"})
    ans = run(hub.wait([{"type": "frame"}], timeout_s=0, since=c))
    assert len(ans["fired"]) == W.MAX_FIRED_PER_CONDITION and ans["omitted"] == 10
    assert ans["fired"][-1]["event"]["fid"] == "t0029.s0"           # the newest are kept


# ── validation ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("conds", [
    [], "frame", [{"type": "nope"}], [{"type": ["job"]}], [{"type": "job"}],
    [{"type": "job", "job_id": ""}],
    [{"type": "job", "job_id": "j", "jobid": "j"}], [{"type": "alert", "min_level": "loud"}],
    [{"type": "operator_reply", "requestid": "r-1"}], [{"type": "timer"}],
    [{"type": "timer", "seconds": -1}], [{"type": "timer", "seconds": True}],
    [{"type": "frame"}] * (W.MAX_CONDITIONS + 1), ["frame"]])
def test_invalid_conditions_are_refused_before_waiting(conds):
    with pytest.raises(ConditionError):
        parse_conditions(conds)
    with pytest.raises(ConditionError):
        run(WaitHub().wait(conds, timeout_s=5))


def test_invalid_timeout_and_cursors_are_refused():
    hub = WaitHub()
    with pytest.raises(ConditionError):
        run(hub.wait([{"type": "frame"}], timeout_s=-1))
    with pytest.raises(CursorError):
        run(hub.wait([{"type": "frame"}], timeout_s=0, since="yesterday"))
    epoch = hub.cursor().split(".")[1]
    with pytest.raises(CursorError):                     # ahead of this hub's sequence
        run(hub.wait([{"type": "frame"}], timeout_s=0, since=f"c1.{epoch}.99.0"))


# ── gaps ─────────────────────────────────────────────────────────────────────
def test_an_evicted_event_is_reported_as_a_gap_not_silently_missed():
    hub = WaitHub(ring_size=3)
    c = hub.cursor()
    for i in range(5):
        hub.publish("frame", {"fid": f"t{i:04d}.s0"})
    t0 = time.monotonic()
    ans = run(hub.wait([{"type": "frame"}], timeout_s=10, since=c))
    assert time.monotonic() - t0 < 1.0
    frames = [f["event"]["fid"] for f in ans["fired"] if f["type"] == "frame"]
    assert frames == ["t0002.s0", "t0003.s0", "t0004.s0"]
    gap = ans["fired"][-1]
    assert gap["type"] == "gap" and gap["index"] is None
    assert gap["event"]["kinds"] == ["frame"] and gap["event"]["reason"] == "evicted"
    # a kind the agent does not wait on is not a gap for it
    assert run(hub.wait([{"type": "alert"}], timeout_s=0, since=c))["fired"] == []


def test_a_cursor_from_another_server_run_is_a_gap():
    old = WaitHub()
    c = old.cursor()
    time.sleep(0.01)
    new = WaitHub()
    new.publish("frame", {"fid": "t0001.s0"})
    ans = run(new.wait([{"type": "frame"}], timeout_s=10, since=c))
    assert [f["type"] for f in ans["fired"]] == ["frame", "gap"]
    assert ans["fired"][-1]["event"]["reason"] == "cursor_from_another_run"
    assert ans["cursor"].split(".")[1] == new.cursor().split(".")[1]


# ── alerts from the EventBus ─────────────────────────────────────────────────
@pytest.mark.parametrize("etype, data, level, source", [
    (EventType.ANOMALY, {"threshold_a": 1e-8, "readings": [2e-8, -3e-8]}, "critical", "watchdog"),
    (EventType.CURRENT_MONITOR, {"kind": "alert", "level": "critical", "rule": "saturation",
                                 "summary_zh": "贴轨"}, "critical", "current_monitor"),
    (EventType.CURRENT_MONITOR, {"kind": "alert", "level": "warn", "rule": "rtn"}, "warning",
     "current_monitor"),
    (EventType.CURRENT_MONITOR, {"kind": "status", "state": "comms_down"}, "warning",
     "current_monitor"),
    (EventType.CURRENT_MONITOR, {"kind": "status", "state": "running"}, "info", "current_monitor"),
    (EventType.CONDUCT_ALERT, {"conduct_id": "c1", "severity": "crit", "code": "estop"}, "critical",
     "conduct"),
    (EventType.CONDUCT_ALERT, {"conduct_id": "c1", "severity": "warn", "code": "safe_mode"}, "warning",
     "conduct"),
    (EventType.CONDUCT_ALERT, {"conduct_id": "c1", "severity": "info", "code": "wait"}, "info",
     "conduct"),
])
def test_bus_events_map_to_alert_levels(etype, data, level, source):
    a = alert_from_bus_event(Event(type=etype, data=data))
    assert a["level"] == level and a["source"] == source
    if etype is EventType.ANOMALY:
        assert a["max_reading_a"] == pytest.approx(3e-8) and a["threshold_a"] == 1e-8


@pytest.mark.parametrize("etype, data", [
    (EventType.CURRENT_MONITOR, {"kind": "segment", "level": "critical"}),
    (EventType.CURRENT_MONITOR, {"kind": "aux", "level": "warn"}),
    (EventType.SCAN_COMPLETE, {"path": "x.sxm"}),
    (EventType.HARDWARE_STATE, {}),
])
def test_telemetry_and_other_bus_events_are_not_alerts(etype, data):
    assert alert_from_bus_event(Event(type=etype, data=data)) is None


def test_the_hub_subscribes_maps_and_detaches():
    bus = EventBus()                     # a private bus, not the process singleton
    hub = WaitHub()
    hub.attach_event_bus(bus)
    hub.attach_event_bus(bus)            # idempotent: one subscription
    c = hub.cursor()
    bus.publish(Event(type=EventType.ANOMALY, data={"threshold_a": 1e-8, "readings": [2e-8]}))
    bus.publish(Event(type=EventType.CURRENT_MONITOR, data={"kind": "segment", "level": "critical"}))
    ans = run(hub.wait([{"type": "alert", "min_level": "critical"}], timeout_s=0, since=c))
    assert [f["event"]["source"] for f in ans["fired"]] == ["watchdog"]
    hub.detach_event_bus()
    before = hub.cursor().split(".")[2]
    bus.publish(Event(type=EventType.ANOMALY, data={"threshold_a": 1e-8, "readings": [2e-8]}))
    assert hub.cursor().split(".")[2] == before


def test_an_alert_wakes_a_running_wait():
    bus = EventBus()
    hub = WaitHub()
    hub.attach_event_bus(bus)
    later(0.2, bus.publish, Event(type=EventType.CONDUCT_ALERT,
                                  data={"conduct_id": "c9", "severity": "crit", "code": "stall"}))
    t0 = time.monotonic()
    ans = run(hub.wait([{"type": "alert"}], timeout_s=10))
    assert time.monotonic() - t0 < 2.0
    assert ans["fired"][0]["event"]["conduct_id"] == "c9"
    hub.detach_event_bus()


# ── operator replies ─────────────────────────────────────────────────────────
def _board():
    rows = [{"id": "r-1", "agent_id": "ext:t", "message": "load a new sample?", "status": "pending",
             "resolved_at": None, "note": ""},
            {"id": "r-2", "agent_id": "ext:t", "message": "where is the file?", "status": "done",
             "resolved_at": "2026-10-14T01:00:00+00:00", "note": "D:/data", "path": "D:/data/a.sxm"}]
    return rows, (lambda: [dict(r) for r in rows])


def _answer(row: dict, note: str) -> None:
    row.update(status="done", note=note, resolved_at="2026-10-14T02:00:00+00:00")


def test_the_first_snapshot_publishes_nothing_and_an_answer_between_waits_fires():
    rows, lookup = _board()
    hub = WaitHub(wishlist_lookup=lookup)
    assert hub.poll_wishlist() == 0                     # r-2 was answered before we looked
    c = hub.cursor()
    _answer(rows[0], "yes, Au(111)")
    ans = run(hub.wait([{"type": "operator_reply"}], timeout_s=10, since=c))
    ev = ans["fired"][0]["event"]
    assert ev["request_id"] == "r-1" and ev["note"] == "yes, Au(111)" and ev["status"] == "done"
    assert hub.poll_wishlist() == 0                     # reported once


def test_a_named_request_that_is_already_answered_fires_at_once():
    rows, lookup = _board()
    hub = WaitHub(wishlist_lookup=lookup)
    ans = run(hub.wait([{"type": "operator_reply", "request_id": "r-2"}], timeout_s=10))
    assert ans["fired"][0]["event"]["path"] == "D:/data/a.sxm"
    ans = run(hub.wait([{"type": "operator_reply", "request_id": "r-404"}], timeout_s=10))
    assert ans["fired"][0]["event"]["error"] == "unknown_request"


def test_an_answer_during_the_wait_is_noticed_by_polling(monkeypatch):
    monkeypatch.setattr(W, "WISHLIST_POLL_S", 0.1)
    rows, lookup = _board()
    hub = WaitHub(wishlist_lookup=lookup)
    hub.poll_wishlist()
    later(0.3, _answer, rows[0], "done")
    t0 = time.monotonic()
    ans = run(hub.wait([{"type": "operator_reply", "request_id": "r-1"}], timeout_s=10))
    assert 0.2 < time.monotonic() - t0 < 3.0
    assert ans["fired"][0]["event"]["request_id"] == "r-1"


def test_operator_replies_without_a_board_still_come_from_published_events():
    hub = WaitHub()
    c = hub.cursor()
    hub.publish("operator_reply", {"request_id": "r-7", "status": "done"})
    ans = run(hub.wait([{"type": "operator_reply", "request_id": "r-7"}], timeout_s=0, since=c))
    assert ans["fired"][0]["event"]["request_id"] == "r-7"


# ── cancellation ─────────────────────────────────────────────────────────────
def test_a_disconnected_client_ends_the_wait(monkeypatch):
    monkeypatch.setattr(W, "DISCONNECT_POLL_S", 0.05)

    async def gone():
        return True

    t0 = time.monotonic()
    ans = run(WaitHub().wait([{"type": "frame"}], timeout_s=10, disconnected=gone))
    assert time.monotonic() - t0 < 1.5
    assert ans["cancelled"] == "client_disconnected" and ans["fired"] == []


def test_waits_leave_no_waiter_behind():
    hub = WaitHub()
    run(hub.wait([{"type": "timer", "seconds": 0}], timeout_s=1))
    with pytest.raises(ConditionError):
        run(hub.wait([{"type": "nope"}], timeout_s=1))
    assert hub._waiters == set()


# ── age and backlog ──────────────────────────────────────────────────────────
def test_an_event_from_before_the_wait_says_it_is_backlog_and_how_old():
    """A jump_burst alert from the end of the previous scan woke the next wait
    "after 0 s", and nothing in it said it was minutes old."""
    hub = WaitHub()
    c = hub.cursor()
    hub.publish("alert", {"level": "warning", "message": "old news"})
    time.sleep(0.3)
    ans = run(hub.wait([{"type": "alert"}], timeout_s=5, since=c))
    hit = ans["fired"][0]
    assert ans["waited_s"] < 0.25                       # it returned at once ...
    assert hit["backlog"] is True and hit["age_s"] >= 0.25  # ... and says the event is not new
    assert "age_s" not in hit["event"] and "backlog" not in hit["event"]


def test_an_event_during_the_wait_is_not_backlog():
    hub = WaitHub()
    later(0.2, hub.publish, "alert", {"level": "critical", "message": "now"})
    ans = run(hub.wait([{"type": "alert"}], timeout_s=10))
    hit = ans["fired"][0]
    assert hit["backlog"] is False and 0.0 <= hit["age_s"] < 1.0


def test_a_job_that_finished_before_the_wait_is_backlog_dated_by_finished_at():
    class Done(FakeJob):
        def view(self) -> dict:
            return {**super().view(), "finished_at": "2026-01-01T00:00:00+00:00"}

    hub = WaitHub(job_lookup={"j_old": Done("j_old", True)}.get)
    hit = run(hub.wait([{"type": "job", "job_id": "j_old"}], timeout_s=5))["fired"][0]
    assert hit["backlog"] is True
    assert hit["age_s"] == pytest.approx(time.time() - 1767225600.0, abs=5.0)
    no_date = run(WaitHub(job_lookup={"j": FakeJob("j", True)}.get).wait(
        [{"type": "job", "job_id": "j"}], timeout_s=5))["fired"][0]
    assert "age_s" not in no_date and "backlog" not in no_date     # unknown time: no guess


def test_an_answer_given_before_the_wait_is_backlog_dated_by_resolved_at():
    rows, lookup = _board()
    hub = WaitHub(wishlist_lookup=lookup)
    hit = run(hub.wait([{"type": "operator_reply", "request_id": "r-2"}], timeout_s=5))["fired"][0]
    assert hit["backlog"] is True
    assert hit["age_s"] == pytest.approx(time.time() - _epoch("2026-10-14T01:00:00+00:00"), abs=5.0)
    unknown = run(hub.wait([{"type": "operator_reply", "request_id": "r-404"}],
                           timeout_s=5))["fired"][0]
    assert "backlog" not in unknown and "age_s" not in unknown


def _epoch(iso: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(iso).timestamp()


# ── current-monitor alert context ────────────────────────────────────────────
def test_a_monitor_alert_carries_its_segment_measurements_and_context():
    data = {"kind": "alert", "level": "warn", "rule": "jump_burst", "summary_zh": "电流频繁突跳",
            "alert_id": 2, "seg_id": 589, "seg_t_start": 1792054618.6643846,
            "seg_t_end": 1792054619.6644027, "jump_rate_hz": 451.85586761216524,
            "spike_sigma": 40.62484840810818, "max_step_pa": 64.857, "mean_pa": 50.978,
            "ctx_scanning": False, "scanning_s_ago": 0.031, "ctx_zctrl_on": True, "ctx_skill": "",
            "last_skill": "ScanAt", "last_skill_s_ago": 1.03, "ctx_bias_v": 2.0,
            "ctx_setpoint_pa": 50.0}
    a = alert_from_bus_event(Event(type=EventType.CURRENT_MONITOR, data=data))
    assert a["level"] == "warning" and a["code"] == "jump_burst" and a["segment_id"] == 589
    assert a["segment_start"] == "2026-10-15T08:56:58.664+00:00"
    assert a["segment_end"] == "2026-10-15T08:56:59.664+00:00"
    assert a["measured"] == {"spike_sigma": 40.625, "jump_rate_hz": 451.856, "max_step_pa": 64.857,
                             "mean_pa": 50.978}
    assert a["context"] == {"scanning": False, "scanning_s_ago": 0.031, "zctrl_on": True,
                            "last_skill": "ScanAt", "last_skill_s_ago": 1.03, "bias_v": 2.0,
                            "setpoint_pa": 50.0}         # an empty skill is left out, not ""


def test_an_older_monitor_alert_without_context_maps_as_before():
    a = alert_from_bus_event(Event(type=EventType.CURRENT_MONITOR,
                                   data={"kind": "alert", "level": "warn", "rule": "rtn"}))
    assert not {"segment_start", "segment_end", "measured", "context"} & set(a)


# ── recent (the briefing's view) ─────────────────────────────────────────────
def test_recent_lists_the_newest_events_first_without_moving_any_cursor():
    hub = WaitHub()
    for n in range(5):
        hub.publish("alert", {"level": "info", "message": f"m{n}"})
    c = hub.cursor()
    got = hub.recent("alert", limit=3)
    assert [r["event"]["message"] for r in got] == ["m4", "m3", "m2"]
    assert all(r["age_s"] >= 0 for r in got) and got[0]["seq"] > got[1]["seq"]
    assert hub.cursor().split(".")[2] == c.split(".")[2]
    assert hub.recent("frame") == []
    with pytest.raises(ValueError):
        hub.recent("job")


def test_recent_keeps_only_the_window():
    hub = WaitHub()
    hub.publish("alert", {"level": "info", "message": "old"})
    with hub._lock:                                   # age the first event by an hour
        hub._rings["alert"][0]["t"] -= 3600
    hub.publish("alert", {"level": "info", "message": "new"})
    assert [r["event"]["message"] for r in hub.recent("alert", within_s=600)] == ["new"]
    assert len(hub.recent("alert")) == 2
