"""直调动作记录的后台写入器 ``mast.logging.record_writer``。

## 钉住的事

1. **生产者永不阻塞**：库被锁、写入很慢、队列满 —— ``submit`` 都立刻返回。
2. **按提交顺序写**：一个写入线程，FIFO。
3. **队列满就丢、计数**，回执当场说 ``dropped``；写入线程不会因此卡死。
4. **冲刷有界**：``flush`` 等到在途的那一条也写完；等不到就如实返回 False。
5. **只对「库被锁」重试**：结构性错误（缺表、外键）重试多少次都一样，不重试。
6. 一条记录抛异常不会带走写入线程。
7. 消费回执的地方（VISTA 的步骤汇总）把 ``pending`` 当「还不知道」，不当「没记上」。

Run:
    .venv-v2-py313/Scripts/python.exe -m pytest tests/v2/unit/logging/test_record_writer.py -q
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from mast.logging import record_writer as rw


@pytest.fixture()
def writer():
    w = rw.RecordWriter(maxsize=50, name="test-record-writer")
    yield w
    w.close(2.0)


# ── 1/2. 不阻塞、按序 ─────────────────────────────────────────────────────────

def test_submit_returns_at_once_even_when_each_write_is_slow(writer):
    done: list[int] = []

    def slow(i):
        def _fn():
            time.sleep(0.05)
            done.append(i)
            return {"v1": True, "i": i}
        return _fn

    t0 = time.monotonic()
    tickets = [writer.submit(slow(i), label=f"s{i}") for i in range(10)]
    submit_s = time.monotonic() - t0
    assert submit_s < 0.2, f"提交 10 条花了 {submit_s:.3f} s —— 生产者被写入拖住了"
    assert writer.flush(5.0)
    assert done == list(range(10)), "写入顺序必须是提交顺序"
    assert [t.wait(0) for t in tickets] == [{"v1": True, "i": i} for i in range(10)]
    st = writer.stats()
    assert st["written"] == 10 and st["dropped"] == 0 and st["errors"] == 0


def test_ticket_wait_is_bounded_and_says_none_while_pending(writer):
    gate = threading.Event()
    t = writer.submit(lambda: (gate.wait(5), {"v1": True})[1], label="blocked")
    t0 = time.monotonic()
    assert t.wait(0.1) is None, "没写完时回执必须是 None（pending），不是一个猜出来的结果"
    assert time.monotonic() - t0 < 0.5
    assert t.wait(0) is None and not t.done
    gate.set()
    assert t.wait(5.0) == {"v1": True}


# ── 3. 队列满 ─────────────────────────────────────────────────────────────────

def test_a_full_queue_drops_counts_and_never_blocks():
    w = rw.RecordWriter(maxsize=2, name="test-record-writer-full")
    gate = threading.Event()
    started = threading.Event()

    def blocker():
        started.set()
        gate.wait(5)
        return {"v1": True}

    try:
        w.submit(blocker, label="first")
        assert started.wait(2.0)                 # 第一条已在写入线程上（不占队列）
        t0 = time.monotonic()
        tickets = [w.submit(lambda: {"v1": True}, label=f"n{i}") for i in range(5)]
        assert time.monotonic() - t0 < 0.2, "队列满时生产者被阻塞了"
        dropped = [t for t in tickets if t.done and (t.result or {}).get("dropped")]
        assert len(dropped) == 3, [t.result for t in tickets]
        assert all(d.result["v1"] is False and d.result["v2_action_id"] is None
                   for d in dropped)
        assert w.stats()["dropped"] == 3
        gate.set()
        assert w.flush(5.0)
        assert w.stats()["written"] == 3          # blocker + 队列里的 2 条
    finally:
        gate.set()
        w.close(2.0)


# ── 4. 冲刷 ───────────────────────────────────────────────────────────────────

def test_flush_waits_for_the_record_in_flight(writer):
    wrote = threading.Event()

    def fn():
        time.sleep(0.3)
        wrote.set()
        return {}

    writer.submit(fn, label="in-flight")
    time.sleep(0.05)                              # 已出队、正在写
    assert writer.flush(3.0)
    assert wrote.is_set(), "flush 在在途的那一条写完之前就返回了"


def test_flush_times_out_honestly(writer):
    gate = threading.Event()
    writer.submit(lambda: (gate.wait(5), {})[1], label="stuck")
    try:
        t0 = time.monotonic()
        assert writer.flush(0.2) is False
        assert time.monotonic() - t0 < 1.0
    finally:
        gate.set()


def test_after_close_submissions_are_dropped_not_lost_silently():
    w = rw.RecordWriter(maxsize=5, name="test-record-writer-close")
    assert w.close(1.0)
    t = w.submit(lambda: {"v1": True}, label="late")
    assert t.done and t.result["dropped"] is True and "closed" in t.result["error"]


# ── 6. 一条坏记录不带走写入线程 ───────────────────────────────────────────────

def test_a_raising_record_is_reported_and_the_writer_keeps_going(writer):
    def boom():
        raise RuntimeError("disk on fire")

    t1 = writer.submit(boom, label="boom")
    t2 = writer.submit(lambda: {"v1": True}, label="after")
    assert writer.flush(3.0)
    assert t1.result["v1"] is False and "disk on fire" in t1.result["error"]
    assert t2.result == {"v1": True}
    assert writer.stats()["errors"] == 1 and writer.stats()["alive"]


# ── 5. 只对「库被锁」重试 ─────────────────────────────────────────────────────

def test_is_sqlite_locked_recognises_locked_and_busy_and_follows_the_cause():
    assert rw.is_sqlite_locked(sqlite3.OperationalError("database is locked"))
    assert rw.is_sqlite_locked(sqlite3.OperationalError("database table is locked"))
    assert rw.is_sqlite_locked(sqlite3.OperationalError("database is busy"))
    assert not rw.is_sqlite_locked(sqlite3.OperationalError("no such table: actions"))
    assert not rw.is_sqlite_locked(sqlite3.IntegrityError("FOREIGN KEY constraint failed"))
    assert not rw.is_sqlite_locked(None)
    try:
        try:
            raise sqlite3.OperationalError("database is locked")
        except sqlite3.OperationalError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert rw.is_sqlite_locked(outer)


def test_lock_retry_backs_off_and_then_succeeds():
    calls = {"n": 0}
    sleeps: list[float] = []

    def fn():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert rw.call_with_lock_retry(fn, budget_s=10.0, sleep=sleeps.append) == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0], f"没有退避：{sleeps}"


def test_lock_retry_does_not_retry_a_structural_error():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: actions")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        rw.call_with_lock_retry(fn, budget_s=10.0, sleep=lambda s: None)
    assert calls["n"] == 1


def test_lock_retry_gives_up_when_the_budget_is_spent():
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s

    def fn():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        rw.call_with_lock_retry(fn, budget_s=3.0, sleep=sleep, clock=lambda: clock["t"])
    assert clock["t"] <= 3.0 + 1e-9


def test_zero_budget_means_one_attempt_like_before():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        rw.call_with_lock_retry(fn, budget_s=0.0, sleep=lambda s: None)
    assert calls["n"] == 1


# ── 进程级单例 ────────────────────────────────────────────────────────────────

def test_flush_default_writer_is_true_when_nothing_was_ever_written():
    old = rw.set_writer_for_test(None)
    try:
        assert rw.flush_default_writer(0.1) is True
    finally:
        rw.set_writer_for_test(old)


# ── 7. 回执的消费者：pending 是「还不知道」 ──────────────────────────────────

def _step_ctx():
    from mast.vista.service import _StepContext

    return _StepContext(ec=None, runtime=None, caller=None, job_id="j1")


class _Run:
    def __init__(self, recorded):
        self.recorded = recorded
        self.success = True
        self.nanonis_calls = 0
        self.params = {}
        self.elapsed_s = 0.0


def test_vista_step_summary_says_unknown_not_false_while_a_record_is_pending():
    steps = _step_ctx()
    steps.note("ScanAt", _Run({"v1": True, "v2_action_id": "a1"}))
    steps.note("SaveScan", _Run({"v1": None, "v2_action_id": None, "pending": True}))
    out = steps.recorded()
    assert out["v1"] is None, "还在写的那一步被汇总成了「没记上」"
    assert out["pending"] == 1
    assert out["v2_action_ids"] == ["a1"]


def test_vista_step_summary_still_says_false_when_a_finished_step_failed():
    steps = _step_ctx()
    steps.note("ScanAt", _Run({"v1": False, "v2_action_id": None}))
    steps.note("SaveScan", _Run({"v1": None, "v2_action_id": None, "pending": True}))
    assert steps.recorded()["v1"] is False


def test_vista_step_summary_is_unchanged_without_pending_steps():
    steps = _step_ctx()
    steps.note("ScanAt", _Run({"v1": True, "v2_action_id": "a1"}))
    steps.note("SaveScan", _Run({"v1": True, "v2_action_id": "a2"}))
    out = steps.recorded()
    assert out["v1"] is True and "pending" not in out
    assert out["v2_action_id"] == "a2" and out["v2_action_ids"] == ["a1", "a2"]
