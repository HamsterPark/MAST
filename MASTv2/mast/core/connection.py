"""vendored from v1 mast/core/connection.py 2026-04-23.

ConnectionPool — manages 4-port TCP connections to Nanonis V5e
(main 6501, monitor 6502, data 6503, emergency 6504). Auto-reconnect on
TCP error; suppresses nanonis_spm stdout noise; records every call as
NanonisCallRecord for downstream logging / experiment journaling.

This file is K (Keep) per migration plan — zero behavioural changes from v1.
The Nanonis hardware interface is hardened by years of v1 production use.
"""

from __future__ import annotations

import contextlib
from mast.i18n import tr
import io
import logging
import re
import socket
import threading
import time

from nanonis_spm import Nanonis

from mast.config import NanonisConfig
from mast.core.comms_health import CommsCircuitBreaker, format_comms_down
from mast.core.types import NanonisCallRecord

# Ensure monkey-patch is applied before any Nanonis usage
from mast.core import nanonis_patch as _patch  # noqa: F401

logger = logging.getLogger(__name__)

_ROLE_ATTR = {
    "main": "port_main",
    "monitor": "port_monitor",
    "data": "port_data",
    "emergency": "port_emergency",
}


def _occupant_hint(host, port) -> str:
    """连不上 / 没回话时补一句「6501 已被 LabVIEW.exe（pid …）占用」。

    本机上的其它程序（例如 LabVIEW VI）也可能连 Nanonis 6501，而那个口同时只服务一个控制方 ——
    只报「超时」的话，看日志的人猜不到是谁占着。没发现别的客户端就返回空串，原错误原样不变。
    """
    try:
        from mast.core.port_guard import occupant_hint

        return occupant_hint(host, port)
    except Exception:  # noqa: BLE001 — 一句提示，绝不影响连接逻辑
        return ""


# Reconnect throttle: when Nanonis is offline, watchdog (0.5 s) + GUI
# refresh (5 s) would otherwise hammer _reconnect_role hundreds of times
# per minute, blocking the GUI main thread and flooding the log. Exponential
# backoff caps the rate; log throttle silences the spam.
_RECONNECT_BACKOFF_S = (0.0, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 16.0)  # by failure count
_RECONNECT_LOG_THROTTLE_S = 30.0
# Graceful stale-socket retirement (2026-07-10): per-recv timeout
# and total budget for draining a timed-out connection to Nanonis's orderly
# EOF before closing it. Keeps a dead peer from blocking the caller while
# giving a live-but-slow Nanonis the chance to finish the interrupted reply
# instead of having its port wedged by an abrupt RST.
_STALE_DRAIN_RECV_S = 0.5
_STALE_DRAIN_TOTAL_S = 2.0

# ── Role-lock bounds (审计 致命三) ─────────────────────
# The per-role lock spans a whole socket round-trip and used to be acquired
# WITHOUT a timeout. One caller stuck inside recv() therefore parked every other
# caller on that role forever: the sse-pump thread wedges → every role="main"
# caller queues → each holds an anyio threadpool token → the default
# CapacityLimiter(40) drains → all 173 sync endpoints under api/ stall. The
# same reasoning (and the same fix) is written up at vision_hardware.py:208-217,
# which bounds its acquire for exactly this reason.
#
# A bounded acquire converts "the whole service hangs" into "this one call
# reports the link is busy", which every caller already handles: safe_call
# returns a record with .error set, it is not an exception path.
_ROLE_LOCK_TIMEOUT_S = 30.0
# Emergency callers (E-STOP, watchdog SafeRetract) wait a token amount and then
# take the last resort below. An emergency retract that queues behind the very
# stall it is meant to rescue is not an emergency retract.
EMERGENCY_LOCK_TIMEOUT_S = 2.0
# close_all() also takes the role lock (so it never closes a socket out from
# under a live transaction). Bounded, because a teardown that blocks forever on
# a wedged role is how a "graceful shutdown" turns into a TerminateProcess.
_CLOSE_LOCK_TIMEOUT_S = 5.0

#: 单次调用能把 socket recv 超时抬到的上限。必须**低于**
#: ``nanonis_patch._LIB_BOGUS_TIMEOUT_S``（1000 s）：到了那个值，patch 会把它
#: 当成 nanonis_spm 泄漏出来的假超时而换回默认值，抬高就白做了。
_MAX_CALL_RECV_TIMEOUT_S = 900.0

#: Prefix on ``NanonisCallRecord.error`` when the call never touched the socket
#: because the role lock was busy. Machine-checkable so the urgent path can tell
#: "the link is busy" apart from "the link answered with an error".
LOCK_BUSY_PREFIX = "RoleBusy:"

# ── 模块首次加载的宽限（Mimea）────────────────────────────────
#
# ``BiasSpectr_Open`` 立即返回，紧跟的 ``BiasSpectr_LimitsSet`` 却在 5 s 的
# socket 超时内没有回话 → 重连 → 同一条命令在替换连接上被对端重置（10054）→
# ConfigureSTS 失败。0.5 s 后同一动作成功；关掉谱学窗口再 Open 是 0.09 s，
# 不复现。所以推断是「Nanonis 启动后**第一次**加载这个模块」：Open 只是把加载
# 发起，下一条命令要等加载完。**这是推断，没在刚启动的控制器上复现过。**
#
# 因此 ``<Module>_Open`` 本身，以及同一角色上紧跟它的**下一条**命令，recv 超时
# 取 ``max(调用方给的, _MODULE_OPEN_RECV_S)``。只延长等待，不重发任何东西：
# 等住比「超时 → 重连 → 重发」干净 —— 后者在真机上会让一个还在忙的端口挨一次
# 替换连接。代价只是控制器真卡死时多等一会才报错。
_MODULE_OPEN_RECV_S = 30.0
#: 「紧跟 Open 的下一条命令」只在 Open 之后这么久之内认。过期就不算：
#: 隔了一分钟的那条命令和模块加载无关，不该白拿一个长超时。
_MODULE_OPEN_FOLLOWUP_WINDOW_S = 60.0

# ── 「机会型」调用────────────────────────────────
#
# 监测服务的辅助通道轮询带 0.1 / 0.5 s 的锁等待上限：data 通道被扫描占着时它就
# **有意**放弃这一次（拒而不排队），下一轮再来。原来每一次放弃都记一条 ERROR，
# 扫描期间约 30 s 一条，把真正的争用（默认 30 s、急停 2 s 的等待）淹没在噪声里。
# 锁等待上限低于这个值的调用按机会型处理：单条降为 DEBUG，按窗口汇总成 INFO。
_OPPORTUNISTIC_LOCK_TIMEOUT_S = 1.0
_OPPORTUNISTIC_SUMMARY_S = 300.0

#: ``NanonisCallRecord`` 上的标记：这次失败发生在**传输层**（超时、连接被重置、
#: 重连失败、空回包），而不是控制器答了一个错误。供调用方判断「对幂等命令重做
#: 一次是否有意义」—— 应用层错误（参数被锁、越界）重做一万次也一样。
_TRANSPORT_FLAG = "transport_failure"


# ── 可能已经送达的命令不重发──────────────────────────────────
#
# 发送中出错、回包超时、空回包之后，safe_call 重连并把命令**原样重发一次**。对读取、
# 属性设置、停止、退针这是对的；但这种失败多半意味着命令**已经送达**、控制器正在或
# 已经执行了它 —— 对「每到一次就做一次事」的命令，重发就是让仪器做两次：粗动走 2N 步、
# 两发脉冲、两次修针、再起一次扫掠或脚本。这些命令照样重连（下一条命令要能走），但
# **不重发**，错误里说清「可能已经执行过」。连接在发送之前就不在（``get`` 抛
# ConnectionError）的那一支，命令根本没发出去，照旧重连后发送。
_ACTS_EACH_TIME = re.compile(r"(Pulse|Start|_Run)$|_Start[A-Z]")
#: 名字上看不出、但每到一次也做一次事的命令：``Scan_Save`` 再到一次就再存一份
#: 自动编号的文件（同一帧两份）；``AtomTrack_DriftComp`` 把原子追踪测得的漂移施加到漂移补偿上 —— 是不是
#: 叠加没有查实，按会叠加处理（不重发的代价只是一条「先读状态」的错误）。
_ACTS_EACH_TIME_NAMES: frozenset[str] = frozenset({"Scan_Save", "AtomTrack_DriftComp"})
#: 按第一个参数才「做一次事」的命令：命令名 -> 不可重发的取值
_ACTS_EACH_TIME_ARG0: dict[str, frozenset[int]] = {
    "Scan_Action": frozenset({0, 3}),            # 0 Start、3 Resume；Stop / Pause / Freeze 可重发
    "AutoApproach_OnOffSet": frozenset({1}),     # 开 = 再起一次进针；关可重发
}


def resend_is_safe(method_name: str, args: tuple = ()) -> bool:
    """Whether a command that may already have reached the controller can be sent again
    (see ``_ACTS_EACH_TIME``). Reads, property sets, stops and withdrawals can; pulses,
    starts, runs, a scan start / resume and switching the auto-approach on cannot. An
    argument that cannot be read as the code it should be counts as unsafe."""
    if _ACTS_EACH_TIME.search(method_name) or method_name in _ACTS_EACH_TIME_NAMES:
        return False
    codes = _ACTS_EACH_TIME_ARG0.get(method_name)
    if codes is None:
        return True
    try:
        first = int(args[0])
    except (IndexError, TypeError, ValueError):
        return False
    return first not in codes


def is_lock_busy(record) -> bool:
    """True iff *record* failed only because the role lock could not be taken."""
    return str(getattr(record, "error", "") or "").startswith(LOCK_BUSY_PREFIX)


def is_transport_failure(record) -> bool:
    """True iff *record* failed at the TCP level (see ``_TRANSPORT_FLAG``).

    False for an application error string, a busy role lock, an open circuit
    breaker, a closed pool, and for any record that did not come from
    :meth:`ConnectionPool.safe_call` (test doubles, the abort gate). Only a
    caller that KNOWS its command is idempotent may act on True by resending."""
    return bool(getattr(record, _TRANSPORT_FLAG, False))


def _mark_transport(record) -> None:
    setattr(record, _TRANSPORT_FLAG, True)


class ConnectionPool:
    """Manages 4-port TCP connections to Nanonis V5e."""

    def __init__(self, config: NanonisConfig):
        self._config = config
        self._connections: dict[str, tuple[socket.socket, Nanonis]] = {}
        # Per-role reconnect state: failures count, last attempt + last log
        # monotonic timestamps. Used to throttle reconnect attempts + log
        # output when the instrument is offline.
        self._reconnect_state: dict[str, dict[str, float]] = {}
        # Thread-safety the pool is hit concurrently by
        # the SafetyWatchdog (monitor, 0.5 s), InstrumentState background refresh
        # (monitor, 1 s) and the GUI/executor (main). Two threads on the SAME
        # role would otherwise interleave request/response bytes on ONE socket
        # (TCP corruption), and a concurrent _reconnect_role would mutate
        # _connections out from under a reader. Fix: a PER-ROLE lock serialises
        # same-role transactions (different roles still run in parallel — the
        # whole point of 4 ports), and a brief struct lock guards the dicts.
        self._struct_lock = threading.RLock()
        self._role_locks: dict[str, threading.RLock] = {}
        # Once close_all() runs, the pool is dead: safe_call/reconnect must NOT
        # silently resurrect sockets (a lingering watchdog/refresh thread would
        # otherwise re-open + leak connections, fighting a freshly built pool
        # for the single Nanonis TCP port). connect_all() clears this to re-arm.
        self._closed = False
        # Communication health circuit breaker (field trace). After a
        # short run of consecutive TCP failures it short-circuits safe_call so a
        # dead link is judged down ONCE instead of stalling every subsequent tool
        # ~5 s in turn — and it stops us re-hammering the fragile Nanonis port.
        # Only TCP-level outcomes drive it (an app-error string = healthy link).
        self._breaker = CommsCircuitBreaker()
        # Report the OPEN transition to the operator/ledger exactly once per
        # outage (not once per short-circuited call), cleared on recovery.
        self._comms_reported = False
        # role -> monotonic time of its last successful ``<Module>_Open``; the
        # next call on that role takes it (one shot). See _MODULE_OPEN_RECV_S.
        self._open_followup: dict[str, float] = {}
        # Opportunistic calls refused on a busy role since the last summary,
        # keyed (role, method). See _OPPORTUNISTIC_LOCK_TIMEOUT_S.
        self._skipped: dict[tuple[str, str], int] = {}
        self._skipped_since = time.monotonic()
        self._skipped_reported_at: float | None = None

    def _role_lock(self, role: str) -> threading.RLock:
        with self._struct_lock:
            lk = self._role_locks.get(role)
            if lk is None:
                lk = threading.RLock()
                self._role_locks[role] = lk
            return lk

    def connect_all(self) -> dict[str, bool]:
        """Connect to all 4 ports. Returns role->success mapping.

        Uses ``connect_timeout_s`` (default 0.3 s) ONLY for the
        ``socket.connect`` call, then restores the full ``timeout_s``
        (default 5 s) for subsequent recv operations. Without the short
        connect timeout, Windows SYN-retry on a dropped/refused port
        blocks ~2 s per port — 8 s of synchronous main-thread freeze for
        all 4 ports when Nanonis is offline.
        """
        results: dict[str, bool] = {}
        self._closed = False  # re-arm a pool that was previously close_all()'d
        # An explicit (re)connect is a fresh start — clear any tripped comms
        # breaker + outage flag so we don't short-circuit brand-new sockets
        # through a stale cooldown.
        self._breaker.record_success()
        self._comms_reported = False
        connect_to = getattr(self._config, "connect_timeout_s", 0.3)
        recv_to = self._config.timeout_s
        # Tell the send() patch which timeout to restore after every round-trip.
        # Without this the patch falls back to its own 5 s default, which would
        # silently override a config that asked for something else.
        _patch.set_default_recv_timeout(recv_to)
        for role, attr in _ROLE_ATTR.items():
            port = getattr(self._config, attr)
            # 端口设成 0 = **显式停用这个角色**(不是配置写漏了)。
            #
            # 为什么需要「显式」:不设这一条也能腾口 —— 让 MAST 连不上就行 ——
            # 但那样它会一直重试、日志刷屏,而且「急停口挂了」这个错误路径会
            # 长期亮着,把真正的故障盖掉。停用要是一个决定,不是一个故障。
            #
            # 腾出 6504(emergency)给外部只读直连时,代价说清楚:急停口的价值在于它是
            # **独立 socket**,``main`` 正忙时也能立刻发退针;停用后自动退针要
            # 排队等 ``main`` 的角色锁(``emergency_stop`` 本来就有 main 兜底)。
            if not port:
                logger.warning(
                    "Nanonis 角色 '%s' 已**显式停用**(端口=0)。"
                    "%s", role,
                    "急停退针将改走 main 口并需要等它的角色锁 —— "
                    "立即停机请用仪器面板上的硬急停。"
                    if role == "emergency" else "相关功能不可用。")
                results[role] = False
                continue
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(connect_to)
                sock.connect((self._config.host, port))
                sock.settimeout(recv_to)
                nn = Nanonis(sock)
                with self._struct_lock:
                    self._connections[role] = (sock, nn)
                results[role] = True
                logger.info("Connected to %s on port %d", role, port)
            except Exception as e:
                results[role] = False
                logger.error("Failed to connect %s on port %d: %s%s", role, port, e,
                             _occupant_hint(self._config.host, port))
        return results

    def get(self, role: str = "main") -> Nanonis:
        with self._struct_lock:
            if role not in self._connections:
                raise ConnectionError(f"No connection for role '{role}'. Call connect_all() first.")
            return self._connections[role][1]

    @contextlib.contextmanager
    def _raised_recv_timeout(self, role: str, seconds: float | None):
        """临时把角色的 socket recv 超时设为 seconds，退出后恢复原值。
        
        阻塞式谱采集的回包要等整个扫掠完成；普通连接超时可能短于采集时长。
        提前放弃接收会使迟到回包污染后续调用，不能用 TCP 仍连接判断流同步正常。
        退出时恢复调用前读到的超时，不能写死所谓正常值或覆盖用户配置。"""
        if seconds is None:
            yield
            return
        with self._struct_lock:
            entry = self._connections.get(role)
        sock = entry[0] if entry else None
        prev = None
        if sock is not None:
            try:
                prev = sock.gettimeout()
                sock.settimeout(min(float(seconds), _MAX_CALL_RECV_TIMEOUT_S))
            except Exception:  # noqa: BLE001 — 假 socket（测试）也要能过
                prev = None
        try:
            yield
        finally:
            if prev is not None:
                try:
                    sock.settimeout(prev)
                except Exception:  # noqa: BLE001 — 期间重连过，旧 socket 已关
                    pass

    def safe_call(self, method_name: str, *args, role: str = "main",
                  lock_timeout_s: float | None = None,
                  count_health: bool = True,
                  recv_timeout_s: float | None = None) -> NanonisCallRecord:
        """Execute a Nanonis method safely with timing and error capture.

        ``recv_timeout_s`` 只给**在仪器端阻塞很久才回话**的命令用
        （``BiasSpectr_Start``、``ZSpectr_Start`` 之类）。默认 ``None`` ＝
        沿用池子的 ``timeout_s``。见 :meth:`_raised_recv_timeout`。
        ``<Module>_Open`` 与紧跟它的那一条命令另有模块加载的宽限
        （:data:`_MODULE_OPEN_RECV_S`）。
        Suppresses nanonis_spm stdout noise. Returns NanonisCallRecord.
        On socket/connection errors, attempts one reconnect + retry
        (subject to per-role exponential backoff + throttled logging).
        A failure at the TCP level is flagged (:func:`is_transport_failure`), and
        when the resend fails too the error text leads with the FIRST failure —
        the cause — not the second.

        ``lock_timeout_s`` bounds the wait for the per-role lock (default
        :data:`_ROLE_LOCK_TIMEOUT_S`). Emergency callers pass
        :data:`EMERGENCY_LOCK_TIMEOUT_S` and then escalate via
        :meth:`urgent_call`. ``0``/negative means "wait forever" and exists only
        so a caller can opt out deliberately; nothing in the tree does.

        ``count_health=False`` keeps this call out of the circuit breaker's
        statistics entirely — neither success nor failure is recorded. The
        breaker is ONE instance shared by all four roles and
        :meth:`CommsCircuitBreaker.record_success` clears the streak
        unconditionally, so a high-rate background poller on an idle role would
        otherwise reset the failure streak of every other role between their
        retries and the "three consecutive failures" condition could never
        accumulate. Reserved for continuous read-only monitoring (the current
        monitor's ~20 Hz oscilloscope poll); anything that acts on the
        instrument must stay counted."""
        record = NanonisCallRecord(method=method_name, args=args)

        if self._closed:
            record.error = "ConnectionPool is closed"
            return record

        # COMMS CIRCUIT BREAKER (field trace). If the link has been
        # judged down (≥ N consecutive TCP failures), short-circuit here WITHOUT
        # touching the socket: return a clear comms-down error immediately rather
        # than adding another ~5 s timeout, and leave the fragile port alone.
        # The breaker itself releases one probe once the cooldown elapses.
        if not self._breaker.allow():
            record.error = format_comms_down(self._breaker)
            return record

        # Serialise the whole transaction PER ROLE: get + the socket round-trip
        # + any reconnect must be atomic w.r.t. another thread on the same role,
        # or two callers would interleave bytes on one Nanonis socket. Different
        # roles take different locks and proceed concurrently.
        #
        # BOUNDED (2026-07-28). See _ROLE_LOCK_TIMEOUT_S: an unbounded acquire
        # here is what let one wedged socket drain the whole anyio threadpool.
        lock_to = (_ROLE_LOCK_TIMEOUT_S if lock_timeout_s is None
                   else float(lock_timeout_s))
        lk = self._role_lock(role)
        t_lock = time.perf_counter()
        acquired = lk.acquire(timeout=lock_to) if lock_to > 0 else lk.acquire()
        if not acquired:
            waited = time.perf_counter() - t_lock
            record.error = (
                f"{LOCK_BUSY_PREFIX} role '{role}' busy — another caller has held "
                f"the Nanonis connection for more than {waited:.1f}s; "
                f"'{method_name}' was NOT sent")
            if 0 < lock_to < _OPPORTUNISTIC_LOCK_TIMEOUT_S:
                # The caller chose to give up quickly (a monitoring poll that
                # yields to a scan): an expected outcome, summarised, not an error.
                logger.debug(
                    "ConnectionPool: role '%s' busy — opportunistic '%s' "
                    "(lock wait %.1fs) skipped", role, method_name, waited)
                self._note_skipped(role, method_name, waited)
            else:
                logger.error(
                    "ConnectionPool: role '%s' lock not acquired in %.1fs — "
                    "'%s' refused rather than queued", role, waited, method_name)
            return record
        try:
          recv_to = self._module_open_budget(role, method_name, recv_timeout_s)
          # What went wrong on the first try, kept for the error text: when the
          # resend fails too, the SECOND exception is usually a consequence (the
          # controller resetting the replacement connection while it is still
          # busy) and the first one is the cause. Reporting only the second sent
          # an agent hunting a network fault (the cause was a 5 s
          # timeout, the agent saw only WinError 10054).
          first_failure = ""
          for attempt in range(2):
            try:
                nn = self.get(role)
            except ConnectionError:
                if attempt == 0:
                    self._log_reconnect_attempt(role, "connection lost")
                    first_failure = f"role '{role}' had no connection"
                    if self._reconnect_role(role):
                        continue
                record.error = f"Connection lost for role '{role}' and reconnect failed"
                _mark_transport(record)
                if count_health:
                    self._on_comms_failure(f"{role}: {record.error}")
                return record

            if not hasattr(nn, method_name):
                record.error = f"Method '{method_name}' not found on Nanonis instance"
                return record

            method = getattr(nn, method_name)
            t0 = time.perf_counter()
            try:
                with self._raised_recv_timeout(role, recv_to), \
                        contextlib.redirect_stdout(io.StringIO()):
                    ret = method(*args)
                record.elapsed_s = time.perf_counter() - t0
                record.return_value = ret

                if isinstance(ret, (list, tuple)) and len(ret) >= 1 and isinstance(ret[0], str) and ret[0]:
                    record.error = ret[0]
                # 空回包不构成成功往返，不能清除通信失败计数。
                # 否则半开连接会持续返回空值而不触发重连，并掩盖其他通信失败。
                if isinstance(ret, (list, tuple)) and len(ret) == 0:
                    if attempt == 0:
                        self._log_reconnect_attempt(
                            role, f"empty reply from '{method_name}' — 对端没答,按断链处理")
                        first_failure = f"'{method_name}' got an empty reply"
                        if not resend_is_safe(method_name, args):
                            return self._not_resent(record, role, method_name, first_failure,
                                                    count_health)
                        if self._reconnect_role(role):
                            continue
                    record.error = (
                        tr(f"'{method_name}' 回了一个空包 —— 对端没有作答。"
                        "这不是「读到了一个空值」,是这条链路没在应答;"
                        "已按断链记账并尝试重连。", f"'{method_name}' returned an empty packet: the peer did not answer. This is not \"read an empty value\"; this link is not responding. It has been booked as a broken link and a reconnect was attempted."))
                    _mark_transport(record)
                    if count_health:
                        self._on_comms_failure(f"{role} {method_name}: empty reply")
                    return record
                # The socket round-trip COMPLETED — the link is healthy even if
                # Nanonis returned an application-error string. Reset the breaker.
                if count_health:
                    self._on_comms_success()
                if method_name.endswith("_Open") and not record.error:
                    self._arm_open_followup(role)
                return record
            except (socket.error, OSError, ConnectionError) as e:
                record.elapsed_s = time.perf_counter() - t0
                what = self._describe_tcp_failure(method_name, e, recv_to)
                if attempt == 0:
                    # 只在每次失败的第一回补「谁占着这个口」：之后的重试共用这句话。
                    # getattr 兜底：有测试用 __new__ 造池子，那时没有 _config。
                    cfg = getattr(self, "_config", None)
                    port = getattr(cfg, _ROLE_ATTR.get(role, "port_main"), None) if cfg else None
                    if isinstance(port, int) and port > 0:
                        what += _occupant_hint(getattr(cfg, "host", "127.0.0.1"), port)
                if attempt == 0:
                    self._log_reconnect_attempt(role, f"TCP error on '{method_name}': {e}")
                    first_failure = what
                    if not resend_is_safe(method_name, args):
                        return self._not_resent(record, role, method_name, what, count_health)
                    if self._reconnect_role(role):
                        continue
                    record.error = (f"{type(e).__name__}: {e} — {what}; reconnecting "
                                    "failed or was deferred by the backoff, so "
                                    "the command was not resent")
                else:
                    record.error = (f"{first_failure}; MAST re-established the "
                                    "connection and resent the command once, and "
                                    f"the resend failed too: {type(e).__name__}: {e}")
                    logger.warning("ConnectionPool: %s", record.error)
                _mark_transport(record)
                if count_health:
                    self._on_comms_failure(f"{role} {method_name}: {record.error}")
                return record
            except Exception as e:
                record.elapsed_s = time.perf_counter() - t0
                record.error = f"{type(e).__name__}: {e}"
                return record
        finally:
            lk.release()

        return record

    def _not_resent(self, record, role: str, method_name: str, what: str,
                    count_health: bool):
        """A command that may already have reached the controller and acts each time it
        arrives (:func:`resend_is_safe`): reconnect so the next call can go, but do not send
        it again, and say so. Returns *record*."""
        reconnected = self._reconnect_role(role)
        record.error = (
            f"{what}; MAST {'re-established' if reconnected else 'could not re-establish'} "
            f"the connection but did not resend '{method_name}': the controller may already "
            "have received and executed it, and sending it again could make the instrument "
            "do it twice. Read the instrument state before trying again.")
        _mark_transport(record)
        logger.warning("ConnectionPool: %s", record.error)
        if count_health:
            self._on_comms_failure(f"{role} {method_name}: {record.error}")
        return record

    def _module_open_budget(self, role: str, method_name: str,
                            recv_timeout_s: float | None) -> float | None:
        """recv timeout for this call: the caller's, raised to
        :data:`_MODULE_OPEN_RECV_S` for a ``<Module>_Open`` and for the ONE call
        that follows a successful Open on the same role within
        :data:`_MODULE_OPEN_FOLLOWUP_WINDOW_S`. Never lowers a larger budget."""
        grace = method_name.endswith("_Open")
        if not grace:
            with self._struct_lock:
                armed = self._followups().pop(role, None)
            grace = (armed is not None
                     and (time.monotonic() - armed) <= _MODULE_OPEN_FOLLOWUP_WINDOW_S)
        if not grace:
            return recv_timeout_s
        return max(float(recv_timeout_s or 0.0), _MODULE_OPEN_RECV_S)

    def _followups(self) -> dict:
        # Created lazily as well as in __init__: a pool built with __new__ (one
        # test does, to stay off TCP) must not lose every call to AttributeError.
        d = getattr(self, "_open_followup", None)
        if d is None:
            d = self._open_followup = {}
        return d

    def _arm_open_followup(self, role: str) -> None:
        with self._struct_lock:
            self._followups()[role] = time.monotonic()

    def _describe_tcp_failure(self, method_name: str, exc: BaseException,
                              recv_to: float | None) -> str:
        """One clause naming the command and how it failed, with the timeout
        that applied when it is a timeout ("no reply within 5.0 s" tells the
        reader the controller was slow, not that the network was down)."""
        if isinstance(exc, (socket.timeout, TimeoutError)):
            limit = recv_to if recv_to is not None else getattr(self._config, "timeout_s", None)
            within = (f" within {float(limit):.1f} s"
                      if isinstance(limit, (int, float)) and not isinstance(limit, bool) else "")
            return f"'{method_name}' timed out: the controller sent no reply{within}"
        return f"'{method_name}' failed on the socket ({type(exc).__name__}: {exc})"

    def _note_skipped(self, role: str, method_name: str, waited: float) -> None:
        """Count one skipped opportunistic call; log the first one and then one
        summary per :data:`_OPPORTUNISTIC_SUMMARY_S` (at the next skip after the
        window, so a quiet pool logs nothing)."""
        now = time.monotonic()
        with self._struct_lock:
            skipped = getattr(self, "_skipped", None)
            if skipped is None:                      # pool built with __new__
                skipped = self._skipped = {}
            key = (role, method_name)
            skipped[key] = skipped.get(key, 0) + 1
            reported_at = getattr(self, "_skipped_reported_at", None)
            first = reported_at is None
            if not first and (now - reported_at) < _OPPORTUNISTIC_SUMMARY_S:
                return
            counts = dict(skipped)
            since = getattr(self, "_skipped_since", now)
            skipped.clear()
            self._skipped_since = now
            self._skipped_reported_at = now
        if first:
            logger.info(
                "ConnectionPool: role '%s' busy — the opportunistic call '%s' "
                "(lock wait %.1fs) was skipped rather than queued. Callers with a "
                "lock wait under %.1fs give up by design (a monitoring poll yields "
                "to a scan); further skips are summarised every %.0f min.",
                role, method_name, waited, _OPPORTUNISTIC_LOCK_TIMEOUT_S,
                _OPPORTUNISTIC_SUMMARY_S / 60.0)
            return
        detail = ", ".join(f"'{m}' on '{r}' ×{n}" for (r, m), n in sorted(counts.items()))
        logger.info(
            "ConnectionPool: %d opportunistic call(s) skipped in the last %.0f min "
            "because their role was busy: %s",
            sum(counts.values()), (now - since) / 60.0, detail)

    # ── Last-resort unstick + the emergency call path ────────────────────

    def reconnect_role(self, role: str, why: str = "") -> bool:
        """用户触发的优雅重连：退役旧 socket 并重新连接。
        
        走 FIN、排空迟到字节、关闭和新建流程；不使用可能阻塞单客户端端口的强制关闭。
        此入口处理自动恢复没有检测或解决的故障。主动重试会清除该角色的退避计数，
        不应被上一次失败的退避窗口阻止。"""
        if self._closed:
            return False
        if role not in _ROLE_ATTR:
            return False
        try:
            st = self._reconnect_state_for(role)
            st["failures"] = 0.0
            st["last_fail_at"] = 0.0
        except Exception:  # noqa: BLE001
            pass
        logger.warning("ConnectionPool: 用户请求重连角色 '%s'%s",
                       role, f" ({why})" if why else "")
        return bool(self._reconnect_role(role))

    def break_role(self, role: str, why: str = "") -> bool:
        """LAST RESORT: close *role*'s socket from another thread.

        This deliberately breaks the house rule that a socket is never closed
        out from under a live transaction — it takes ``_struct_lock`` but NOT
        the role lock, because the whole point is that the role lock is held by
        a caller that is not coming back. Measured 2026-07-28: a ``close()``
        from a second thread makes the blocked ``recv`` raise
        ``ConnectionAbortedError`` within ~0.5 s (``shutdown(RDWR)`` does not).

        The cost is real and is why this is not called speculatively: an abrupt
        close mid-transaction can leave Nanonis's single-client port wedged
        until Nanonis itself is restarted. It is still the better of the two
        outcomes when the alternative is an emergency retract that never runs.

        Returns True if a socket was actually closed.
        """
        with self._struct_lock:
            stale = self._connections.pop(role, None)
        if stale is None:
            return False
        logger.critical(
            "ConnectionPool: FORCE-CLOSING role '%s' socket to unstick a blocked "
            "caller%s — the Nanonis port may need a restart", role,
            f" ({why})" if why else "")
        sock, nn = stale
        for closer in (nn.close, sock.close):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
        try:
            from mast.core.diagnostics import record as _diag

            _diag("comms_force_close", f"nanonis_{role}",
                  tr("为解救被卡死的调用，强制关闭了该角色的 TCP 连接",
                     "Force-closed this role's TCP connection to rescue a stuck call"),
                  why=why)
        except Exception:  # noqa: BLE001
            pass
        return True

    def urgent_call(self, method_name: str, *args, role: str = "main",
                    lock_timeout_s: float = EMERGENCY_LOCK_TIMEOUT_S,
                    ) -> NanonisCallRecord:
        """``safe_call`` for the emergency path — never queues behind a stall.

        The E-STOP retract and the watchdog's SafeRetract both fall back to
        ``role="main"`` when the dedicated emergency port fails. With an
        unbounded role lock that fallback queued behind the exact wedged caller
        it was rescuing the instrument from (dispatch audit 致命三, "最要命").

        So: wait only :data:`EMERGENCY_LOCK_TIMEOUT_S`; if the lock is still
        held, :meth:`break_role` the socket and try once more. A retract that
        costs a Nanonis restart beats a retract that never happens.
        """
        rec = self.safe_call(method_name, *args, role=role,
                             lock_timeout_s=lock_timeout_s)
        if not is_lock_busy(rec):
            return rec
        if not self.break_role(role, f"urgent {method_name} could not acquire the lock"):
            return rec
        return self.safe_call(method_name, *args, role=role,
                              lock_timeout_s=lock_timeout_s)

    # ── comms circuit breaker plumbing ───────────────────────────────────

    def _on_comms_success(self) -> None:
        """A TCP round-trip completed — clear the breaker + the outage flag."""
        self._breaker.record_success()
        if self._comms_reported:
            self._comms_reported = False
            logger.info("Nanonis TCP comms recovered — circuit breaker reset")

    def _on_comms_failure(self, reason: str) -> None:
        """A TCP-level failure — feed the breaker and, when it OPENs, report the
        outage exactly ONCE (not once per short-circuited call) to log + ledger."""
        self._breaker.record_failure(reason)
        if self._breaker.is_open() and not self._comms_reported:
            self._comms_reported = True
            logger.error(
                "Nanonis TCP comms circuit OPEN after consecutive failures "
                "(%s) — short-circuiting further calls to spare the port", reason)
            try:
                from mast.core.diagnostics import record as _diag
                _diag("comms_down", "nanonis_tcp",
                      tr("连续 TCP 超时/断连,通信熔断——已停止发命令",
                         "Consecutive TCP timeouts/disconnects: the comms circuit breaker opened and commands have stopped"),
                      **self._breaker.snapshot())
            except Exception:  # noqa: BLE001 — diagnostics never breaks the pool
                pass

    def comms_snapshot(self) -> dict:
        """Circuit-breaker telemetry (state / streak / cooldown). JSON-safe."""
        return self._breaker.snapshot()

    def comms_healthy(self) -> bool:
        """False once the breaker has tripped (link judged down)."""
        return not self._breaker.is_open()

    def _reconnect_state_for(self, role: str) -> dict[str, float]:
        with self._struct_lock:
            return self._reconnect_state.setdefault(
                role,
                {"failures": 0.0, "last_fail_at": 0.0, "last_log_at": 0.0},
            )

    def _log_reconnect_attempt(self, role: str, reason: str) -> None:
        """Throttled WARNING on each reconnect attempt — once per throttle
        window per role. Without this every watchdog tick (0.5 s) spams a
        WARNING and the launcher tail-tail mirror doubles every line."""
        st = self._reconnect_state_for(role)
        now = time.monotonic()
        if (now - st["last_log_at"]) > _RECONNECT_LOG_THROTTLE_S or st["failures"] == 0:
            logger.warning("Connection lost for role '%s' (%s), attempting reconnect", role, reason)
            st["last_log_at"] = now
        else:
            logger.debug("Connection lost for role '%s' (suppressed): %s", role, reason)

    def _reconnect_role(self, role: str) -> bool:
        if self._closed:
            return False
        attr = _ROLE_ATTR.get(role)
        if not attr:
            return False
        port = getattr(self._config, attr)
        if not port:
            return False        # 显式停用的角色不重连(见 connect_all)
        st = self._reconnect_state_for(role)
        now = time.monotonic()
        failures = int(st["failures"])
        # Exponential backoff: skip the TCP attempt entirely when we're inside
        # the backoff window for this role. Without this, watchdog (0.5 s) +
        # GUI refresh (5 s) would block the main thread inside socket.connect
        # hundreds of times per minute when Nanonis is offline.
        if failures > 0:
            idx = min(failures, len(_RECONNECT_BACKOFF_S) - 1)
            backoff = _RECONNECT_BACKOFF_S[idx]
            if (now - st["last_fail_at"]) < backoff:
                return False
        # Retire the stale socket GRACEFULLY before attempting a new one. A
        # hard close() right after a mid-request timeout is protocol-level
        # force-kill: Nanonis's single-connection port is left with a wedged
        # half-open peer and stays dead until Nanonis restarts (feedback
        # 2026-07-10 #37 "端口占用不会自动释放"; the 14:45 field trace shows
        # timeout → hard close → WSA10054 → every reconnect timing out).
        # Instead: FIN our side (SHUT_WR), drain any late response bytes until
        # Nanonis's orderly EOF (or a short deadline), THEN close. Bounded to
        # ~_STALE_DRAIN_TOTAL_S so a dead peer can't hang the caller; this
        # runs at most once per disconnect (the stale socket is popped).
        with self._struct_lock:
            stale = self._connections.pop(role, None)
        if stale is not None:
            old_sock, old_nn = stale
            try:
                old_sock.settimeout(_STALE_DRAIN_RECV_S)
                try:
                    old_sock.shutdown(socket.SHUT_WR)  # orderly FIN, keep reading
                except OSError:
                    pass
                deadline = time.monotonic() + _STALE_DRAIN_TOTAL_S
                while time.monotonic() < deadline:
                    try:
                        if not old_sock.recv(65536):
                            break  # peer EOF — the stream ended cleanly
                    except (socket.timeout, OSError):
                        break
            except Exception:
                pass
            try:
                old_nn.close()
            except Exception:
                pass
            try:
                old_sock.close()
            except Exception:
                pass
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Short connect timeout to avoid blocking the caller (GUI tick /
            # watchdog tick) for ~2 s per failed port; restore the full
            # recv timeout once connected.
            connect_to = getattr(self._config, "connect_timeout_s", 0.3)
            sock.settimeout(connect_to)
            sock.connect((self._config.host, port))
            sock.settimeout(self._config.timeout_s)
            nn = Nanonis(sock)
            with self._struct_lock:
                self._connections[role] = (sock, nn)
            if failures > 0:
                logger.info(
                    "Reconnected role '%s' on port %d (after %d failure%s)",
                    role, port, failures, "" if failures == 1 else "s",
                )
            else:
                logger.info("Reconnected role '%s' on port %d", role, port)
            st["failures"] = 0.0
            st["last_fail_at"] = 0.0
            st["last_log_at"] = 0.0
            return True
        except Exception as e:
            st["failures"] = float(failures + 1)
            st["last_fail_at"] = now
            # Throttle ERROR log to once per window per role; first failure
            # always logs so the operator gets immediate feedback.
            if failures == 0 or (now - st["last_log_at"]) > _RECONNECT_LOG_THROTTLE_S:
                logger.error(
                    "Reconnect failed for '%s' on port %d (failure #%d): %s%s",
                    role, port, failures + 1, e,
                    _occupant_hint(getattr(getattr(self, "_config", None), "host", "127.0.0.1"),
                                   port),
                )
                st["last_log_at"] = now
            else:
                logger.debug("Reconnect failed for '%s' (suppressed): %s", role, e)
            return False

    def health_check(self, role: str = "main") -> bool:
        record = self.safe_call("Util_VersionGet", role=role)
        return not record.error

    def close_all(self):
        # Mark dead FIRST so any in-flight/queued safe_call bails out instead of
        # triggering a reconnect that resurrects the pool we're tearing down.
        self._closed = True
        with self._struct_lock:
            items = list(self._connections.items())
        for role, (sock, nn) in items:
            # Hold the role lock so we never close a socket while a concurrent
            # safe_call is mid round-trip on it (TCP half-transaction → port
            # corruption on the fragile Nanonis single-client TCP).
            #
            # BOUNDED (2026-07-28): an unbounded acquire here means a wedged
            # role turns `shutdown()` into a hang, and the launcher's fallback
            # for a hung shutdown is TerminateProcess — which skips every other
            # graceful step and is exactly what corrupts the port. Waiting a few
            # seconds and then closing anyway is the lesser harm.
            lk = self._role_lock(role)
            got = lk.acquire(timeout=_CLOSE_LOCK_TIMEOUT_S)
            if not got:
                logger.warning(
                    "close_all: role '%s' still busy after %.0fs — closing "
                    "without the lock", role, _CLOSE_LOCK_TIMEOUT_S)
            try:
                try:
                    nn.close()
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
            finally:
                if got:
                    lk.release()
            logger.info("Closed connection: %s", role)
        with self._struct_lock:
            self._connections.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close_all()
