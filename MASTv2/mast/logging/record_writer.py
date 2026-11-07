"""动作记录的后台写入器 —— 让「入账」不再卡住已经发生的动作的回执。

## 为什么有这个模块

技能直调 API（``POST /api/skills/{name}/execute``）每跑完一个技能，都在**请求线程上**
同步写两行动作记录：v1 ``mast_experiments.db``（WAL）与 v2
``mast_experiments_v2.db``。当一个外部看守脚本高频轮询（约 1.6 次/秒）
``GetSignalValues``（进针时盯 qPlus 振幅判撞针），环境监测又在往同一个
库写传感器值时，写者在 SQLite 锁上排队，服务日志里会出现::

    [WARNING] mast.core.runtime: V1 action record failed for GetAutoApproachStatus
    sqlite3.OperationalError: database is locked
    [WARNING] mast.environment.monitor: Failed to log sensor 'tunnel_current': database is locked

每一次失败都先等满 busy timeout，于是 HTTP 回执会卡几十秒到几分钟 —— **而仪器命令早就执行
完了**：一次 ``StopAutoApproach`` 当场停住了进针，回执却十几秒后才到；看守因此可能判「失明」
而反复停进针。``/api/health``、``live-readings`` 一直是几十毫秒 —— 卡的只是入账。

## 它做什么

* **一个进程级的有界队列 + 一个写入线程**。生产者（请求线程）只做 ``put_nowait``，
  永不阻塞；写入线程按提交顺序逐条写，同一时刻只有它一个写者，请求线程之间不再互相抢锁。
* **只对「库被锁」重试**（指数退避，有总预算），其余异常照旧吞掉、照旧记日志 ——
  记录失败绝不能反噬已经发生的动作，这条与原来的语义一致。
* **队列满就丢、计数、限频告警**，绝不让一个卡住的库把请求线程拖住。
* **关停时有界冲刷**（``CoreRuntime.shutdown`` 显式调一次，atexit 兜底）。
* 每次提交返回一张 :class:`Ticket`：调用方可以**有界地**等结果（要 ``v2_action_id``
  的调用方等一小会儿；不要的 —— 例如技能直调 API —— 一刻都不等）。

## 刻意不做的事

* 不改记录的内容与归属：写什么、记在哪个实验 / 会话上，仍由 ``CoreRuntime`` 的那几个
  方法决定（归属在**调用时**快照，见 ``CoreRuntime._direct_record_attribution``）。
* 不重试「已经写了一半」的记录：重试只发生在第一条 INSERT 之前就撞锁的那一种，
  否则会写出重复行（见 ``CoreRuntime._record_v2_action`` 的 ``raise_locked``）。
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: 队列上限。约 1.6 条/秒时，库被完全锁住约 10 分钟才会满 —— 长于已观察到的最长卡顿
#: （数分钟）。不取更大：每条载荷带着技能返回的 data，被锁的库不该吃掉无界的内存。
DEFAULT_MAXSIZE = 1000

#: 单条记录撞锁时最多重试多久（秒）。超过就放弃这一条（记日志），不让队列无限积压。
DEFAULT_LOCK_RETRY_S = 60.0

#: 关停冲刷的默认上限（秒）。
DEFAULT_FLUSH_S = 5.0

#: 丢弃告警的最小间隔（秒）—— 库卡住时每秒可能丢好几条，日志不能被它刷屏。
_DROP_WARN_EVERY_S = 30.0


def is_sqlite_locked(exc: BaseException | None) -> bool:
    """这个异常是不是「库被别的写者锁着」（可以等一会儿再写的那一种）。

    只认 ``sqlite3.OperationalError`` 里的 ``database is locked`` / ``database table is
    locked`` / ``database is busy`` —— 结构性错误（缺表、外键、磁盘满）重试多少次都一样，
    不在此列。沿 ``__cause__`` / ``__context__`` 往上找两层：包装过的异常同样算。
    """
    seen = 0
    while exc is not None and seen < 3:
        if isinstance(exc, sqlite3.OperationalError):
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def call_with_lock_retry(fn: Callable[[], Any], *, budget_s: float,
                         first_wait_s: float = 0.25, max_wait_s: float = 5.0,
                         sleep: Callable[[float], None] = time.sleep,
                         clock: Callable[[], float] = time.monotonic) -> Any:
    """调 ``fn()``；它因 :func:`is_sqlite_locked` 失败就退避重试，直到 ``budget_s`` 用完。

    预算用完仍被锁 ⇒ 把**最后一次**的异常原样抛出去，交给调用方按原来的方式记日志、吞掉。
    其它异常一律不重试、原样抛出。``budget_s <= 0`` ⇒ 只调一次（与旧行为相同）。
    """
    t0 = clock()
    wait = max(0.0, float(first_wait_s))
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — 只拦「库被锁」，其余原样抛
            if not is_sqlite_locked(exc):
                raise
            left = float(budget_s) - (clock() - t0)
            if left <= 0:
                raise
            sleep(min(wait, left))
            wait = min(max(wait * 2.0, 0.05), float(max_wait_s))


class Ticket:
    """一次提交的回执。``wait(timeout)`` 拿到写入结果，或在超时时返回 ``None``。"""

    __slots__ = ("_event", "result", "label", "submitted_at")

    def __init__(self, label: str = "") -> None:
        self._event = threading.Event()
        self.result: dict | None = None
        self.label = label
        self.submitted_at = time.monotonic()

    def _set(self, result: dict) -> None:
        self.result = result
        self._event.set()

    @property
    def done(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout_s: float | None) -> dict | None:
        """等最多 ``timeout_s`` 秒；``0`` / 负数 ⇒ 只看一眼，不等。超时返回 ``None``。"""
        if timeout_s is None:
            self._event.wait()
        elif timeout_s > 0:
            self._event.wait(timeout_s)
        return self.result if self._event.is_set() else None


_STOP = object()


class RecordWriter:
    """进程级动作记录写入器。线程安全；所有公开方法**永不抛**。"""

    def __init__(self, *, maxsize: int = DEFAULT_MAXSIZE, name: str = "mast-record-writer") -> None:
        self._q: "queue.Queue[Any]" = queue.Queue(maxsize=max(1, int(maxsize)))
        self._name = name
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._closed = False
        self._last_drop_warn = 0.0
        self.submitted = 0
        self.written = 0
        self.errors = 0
        self.dropped = 0

    # ── 生产者一侧 ──────────────────────────────────────────────────
    def submit(self, fn: Callable[[], dict], *, label: str = "") -> Ticket:
        """排进队列，立刻返回。``fn`` 在写入线程上被调用，返回记录回执 dict。

        队列满或写入器已关 ⇒ 不排队，回执当场给出 ``dropped`` 结果；**绝不阻塞调用方**。
        """
        ticket = Ticket(label)
        if self._closed:
            ticket._set(_dropped_result("closed"))
            self._count("dropped")
            return ticket
        self._ensure_thread()
        try:
            self._q.put_nowait((fn, ticket))
        except queue.Full:
            ticket._set(_dropped_result("queue_full"))
            self._count("dropped")
            self._warn_drop(label)
            return ticket
        self._count("submitted")
        return ticket

    def flush(self, timeout_s: float = DEFAULT_FLUSH_S) -> bool:
        """等到此刻之前提交的都写完（或超时）。True = 队列已空、写入线程空闲。"""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            # unfinished_tasks 在 task_done() 之后才减 —— 「出队了但还在写」也算没完。
            if self._q.unfinished_tasks == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def close(self, timeout_s: float = DEFAULT_FLUSH_S) -> bool:
        """有界冲刷后停掉写入线程。之后的提交一律 ``dropped``（``closed``）。"""
        ok = self.flush(timeout_s)
        self._closed = True
        th = self._thread
        if th is not None and th.is_alive():
            try:
                self._q.put_nowait(_STOP)
            except queue.Full:
                pass
        return ok

    def stats(self) -> dict:
        with self._stats_lock:
            return {"submitted": self.submitted, "written": self.written,
                    "errors": self.errors, "dropped": self.dropped,
                    "queued": self._q.qsize(), "closed": self._closed,
                    "alive": bool(self._thread is not None and self._thread.is_alive())}

    # ── 写入线程 ────────────────────────────────────────────────────
    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            th = threading.Thread(target=self._run, name=self._name, daemon=True)
            th.start()
            self._thread = th

    def _run(self) -> None:
        while True:
            item = self._q.get()
            try:
                if item is _STOP:
                    return
                fn, ticket = item
                try:
                    res = fn()
                    res = dict(res or {})
                    self._count("written")
                except Exception as exc:  # noqa: BLE001 — 记账失败绝不能拖死写入线程
                    logger.warning("record-writer: %s 入账失败: %s", ticket.label or "?", exc)
                    res = {"v1": False, "v2_action_id": None, "error": str(exc)[:200]}
                    self._count("errors")
                ticket._set(res)
            finally:
                self._q.task_done()

    def _count(self, key: str) -> None:
        with self._stats_lock:
            setattr(self, key, getattr(self, key) + 1)

    def _warn_drop(self, label: str) -> None:
        now = time.monotonic()
        if now - self._last_drop_warn < _DROP_WARN_EVERY_S:
            return
        self._last_drop_warn = now
        logger.warning(
            "record-writer: 动作记录队列已满（%d 条），丢弃 %s 的记录（累计丢弃 %d 条）—— "
            "实验库很可能被长时间锁住", self._q.maxsize, label or "?", self.dropped)


def _dropped_result(why: str) -> dict:
    return {"v1": False, "v2_action_id": None, "dropped": True,
            "error": f"record not written: {why}"}


# ── 进程级单例 ────────────────────────────────────────────────────────

_default: RecordWriter | None = None
_default_lock = threading.Lock()


def get_writer() -> RecordWriter:
    """进程级写入器（第一次用时建；atexit 时有界冲刷）。"""
    global _default
    w = _default
    if w is not None:
        return w
    with _default_lock:
        if _default is None:
            _default = RecordWriter()
            try:
                import atexit

                atexit.register(_flush_default_at_exit)
            except Exception:  # noqa: BLE001
                pass
        return _default


def flush_default_writer(timeout_s: float = DEFAULT_FLUSH_S) -> bool:
    """有界冲刷进程级写入器；从未建过 ⇒ True（没有要写的）。永不抛。"""
    w = _default
    if w is None:
        return True
    try:
        return w.flush(timeout_s)
    except Exception:  # noqa: BLE001
        return False


def _flush_default_at_exit() -> None:
    try:
        if not flush_default_writer(DEFAULT_FLUSH_S):
            w = _default
            logger.warning("record-writer: 退出时仍有 %s 条动作记录没写完",
                           w.stats().get("queued") if w is not None else "?")
    except Exception:  # noqa: BLE001
        pass


def set_writer_for_test(writer: RecordWriter | None) -> RecordWriter | None:
    """测试用：换掉进程级写入器，返回旧的。"""
    global _default
    with _default_lock:
        old, _default = _default, writer
    return old


__all__ = [
    "DEFAULT_FLUSH_S",
    "DEFAULT_LOCK_RETRY_S",
    "DEFAULT_MAXSIZE",
    "RecordWriter",
    "Ticket",
    "call_with_lock_retry",
    "flush_default_writer",
    "get_writer",
    "is_sqlite_locked",
    "set_writer_for_test",
]
