"""SatelliteHub —— 枢纽侧的卫星会话、命令、事件与文件（契约 §5、§6）。

线程模型
--------
- **WebSocket 线程**（uvicorn 的事件循环）：``attach`` / ``on_message`` / ``detach``。
  这里只做加锁的内存更新和入队，不做磁盘 I/O（文件消息由端点放到线程池里调
  ``on_file_message``）。
- **调用方线程**（技能、REST）：``submit`` / ``call`` / ``latest`` / ``emergency_stop_all`` …
- **分发线程** ``mast-satellite-dispatch``：把事件交给监听者、LiveHub 和 EventBus。
  监听者在这里同步调用（契约 §6「在枢纽线程上同步调用，只许入队」）—— 慢监听者
  只会拖慢分发线程，不会拖住 WebSocket 循环。
- **监视线程** ``mast-satellite-monitor``：活性状态机、心跳 ping、累计 ``evt_ack``、
  命令到期、配置热加载、状态持久化。

所有发送都是非阻塞入队（``Transport.send``），所以持锁发送不会卡住任何线程。

不持有 MAST 的仪器令牌（契约 §6）：令牌由调用它的技能负责。
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from mast.i18n import tr
from mast.satellite import hub_config as hc
from mast.satellite import protocol as P
from mast.satellite.clock import ClockEstimator
from mast.satellite.errors import (
    SatelliteCommandError,
    SatelliteError,
    SatelliteTimeout,
    SatelliteUnavailable,
)

logger = logging.getLogger(__name__)

# 活性阈值（契约 §5.3）：最近一次 pong 在 3 s 内为 connected，3–10 s 为 degraded，
# 超过 10 s 或 socket 断开为 lost。
DEGRADED_AFTER_S = 3.0
LOST_AFTER_S = 10.0
HELLO_TIMEOUT_S = 10.0
#: 累计确认的最短间隔：高频光谱帧不必每条都回一次 evt_ack。
ACK_INTERVAL_S = 0.1
MONITOR_TICK_S = 0.1
#: 卫星自己按 timeout_s 收尾；枢纽多等一点，让卫星的 timeout 结果先到。
CMD_GRACE_MIN_S = 2.0
CMD_GRACE_MAX_S = 30.0
#: 服务正常关停时等卫星确认 safe 的上限（秒）。确认一般几十毫秒；卫星不回话就不等了，
#: 记日志照常关停，卫星靠自己的死人开关。
SHUTDOWN_SAFE_WAIT_S = 2.0
#: 已完成命令在枢纽侧保留多少条（同一 request_id 再提交时直接拿回结果）。
FINISHED_CMDS_KEEP = 2000
RECENT_EVENTS_KEEP = 500
STATE_FILE = "hub_state.json"

LINK_CONNECTED = "connected"
LINK_DEGRADED = "degraded"
LINK_LOST = "lost"

#: 设备状态里只有这些跳变值得发 EventBus（busy↔ready 每次采集都翻，一张 1000 点的
#: 图就是 2000 条，会把总线只重放 100 条的历史冲光）。
_NOTABLE_DEVICE_STATES = frozenset({"error", "offline", "operator_hold", "not_started"})


def now_ms() -> int:
    return int(time.time() * 1000)


class Transport(Protocol):
    """端点提供的发送通道。所有方法线程安全、非阻塞。"""

    closed: bool
    peer: str

    def send(self, obj: dict) -> bool: ...
    def send_lazy(self, fn: Callable[[], dict | None]) -> bool: ...
    def close(self, code: int, reason: str) -> None: ...


# ── 命令 ────────────────────────────────────────────────────────────────────

class PendingCommand:
    """一条发给卫星的命令（契约 §6 ``hub.submit`` 的返回值）。"""

    def __init__(self, hub: "SatelliteHub", *, cmd_id: str, sat_id: str, device: str,
                 action: str, args: dict, timeout_s: float) -> None:
        self._hub = hub
        self.id = cmd_id
        self.sat_id = sat_id
        self.device = device
        self.action = action
        self.args = dict(args or {})
        self.timeout_s = float(timeout_s)
        grace = min(CMD_GRACE_MAX_S, max(CMD_GRACE_MIN_S, 0.1 * self.timeout_s))
        self.created_ms = now_ms()
        self.created_mono = time.monotonic()
        self.deadline_mono = self.created_mono + self.timeout_s + grace
        self.boot_id: str | None = None        # 发出时卫星的 boot_id
        self.state = "pending"                 # pending → sent → accepted/running → done
        self.ack_state: str | None = None
        self.result: dict | None = None        # 卫星回的 result 原文
        self._value: Any = None
        self._exc: Exception | None = None
        self._event = threading.Event()
        self._callbacks: list[Callable[["PendingCommand"], None]] = []
        self._lock = threading.Lock()
        self.sends = 0

    # ── 结果 ──
    @property
    def done(self) -> bool:
        return self._event.is_set()

    @property
    def ok(self) -> bool | None:
        if not self.done:
            return None
        return self._exc is None

    @property
    def error(self) -> Exception | None:
        return self._exc

    def _finish(self, *, value: Any = None, exc: Exception | None = None,
                result: dict | None = None) -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self._value = value
            self._exc = exc
            if result is not None:
                self.result = result
            self.state = "done" if exc is None else "failed"
            self._event.set()
            cbs, self._callbacks = list(self._callbacks), []
        for cb in cbs:
            try:
                cb(self)
            except Exception:  # noqa: BLE001
                logger.debug("PendingCommand callback failed", exc_info=True)
        return True

    def add_done_callback(self, fn: Callable[["PendingCommand"], None]) -> None:
        """完成时调用 ``fn(cmd)``（在完成它的那个线程上；只许做轻活）。已完成则立即调用。"""
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(fn)
                return
        try:
            fn(self)
        except Exception:  # noqa: BLE001
            logger.debug("PendingCommand callback failed", exc_info=True)

    def wait(self, timeout_s: float | None = None, check_abort: Callable[[], Any] | None = None):
        """阻塞到出结果，返回 ``result.value``。

        - ``check_abort``：每 0.1 s 调一次；它抛异常 → 对卫星发 ``cancel``，原样再抛。
        - ``timeout_s``：这一次最多等多久。等够了而命令自己的时限没到 → 抛
          ``SatelliteTimeout(command_expired=False)``，命令仍在跑。命令自己的时限到了 →
          枢纽发 ``cancel`` 并抛 ``SatelliteTimeout(command_expired=True)``。
        """
        wait_deadline = None if timeout_s is None else time.monotonic() + max(0.0, float(timeout_s))
        while True:
            if check_abort is not None:
                try:
                    check_abort()
                except BaseException:
                    self.cancel()
                    raise
            now = time.monotonic()
            if self.done:
                break
            limit = self.deadline_mono
            if wait_deadline is not None:
                limit = min(limit, wait_deadline)
            if now >= limit:
                if now >= self.deadline_mono:
                    self._hub._expire(self)
                    break
                raise SatelliteTimeout(
                    tr(f"{self.sat_id}/{self.device}.{self.action} 在 {timeout_s} s 内没有结果"
                       "（命令仍在卫星上运行）",
                       f"{self.sat_id}/{self.device}.{self.action} produced no result within "
                       f"{timeout_s} s (the command is still running on the satellite)"),
                    sat_id=self.sat_id, cmd_id=self.id, command_expired=False)
            self._event.wait(min(0.1, max(0.0, limit - now)))
        if self._exc is not None:
            raise self._exc
        return self._value

    def cancel(self) -> None:
        """尽力而为地请卫星取消。能取消的会以 ``cancelled`` 收尾。永不抛。"""
        try:
            self._hub._send_cancel(self)
        except Exception:  # noqa: BLE001
            logger.debug("cancel failed", exc_info=True)

    def summary(self) -> dict:
        return {
            "id": self.id, "sat_id": self.sat_id, "device": self.device, "action": self.action,
            "state": self.state, "ack": self.ack_state, "created_ms": self.created_ms,
            "timeout_s": self.timeout_s, "done": self.done,
            "ok": self.ok, "error": (getattr(self._exc, "code", None) if self._exc else None),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PendingCommand {self.sat_id}/{self.device}.{self.action} {self.id} {self.state}>"


# ── 每颗卫星的状态（跨会话保留） ────────────────────────────────────────────

@dataclass
class _Session:
    sat_id: str
    session_id: str
    transport: Transport
    boot_id: str
    opened_ms: int
    peer: str = ""


@dataclass
class _Record:
    sat_id: str
    boot_id: str | None = None
    landed_upto: int = 0
    acked_upto: int = -1
    last_ack_mono: float = 0.0
    state: str = LINK_LOST
    ever_connected: bool = False
    session: _Session | None = None
    connected_since_ms: int | None = None
    disconnected_ms: int | None = None
    last_alive_mono: float = 0.0
    last_seen_ms: int | None = None
    hello: dict = field(default_factory=dict)
    devices: dict[str, dict] = field(default_factory=dict)
    latest: dict[tuple[str, str], dict] = field(default_factory=dict)
    clock: ClockEstimator = field(default_factory=ClockEstimator)
    pending: dict[str, PendingCommand] = field(default_factory=dict)
    recent: deque = field(default_factory=lambda: deque(maxlen=RECENT_EVENTS_KEEP))
    counters: dict[str, int] = field(default_factory=lambda: {
        "events": 0, "dup_events": 0, "gap_events": 0, "cmds": 0, "cmds_ok": 0,
        "cmds_failed": 0, "connects": 0, "restarts": 0, "protocol_errors": 0,
        "late_results": 0})
    pings: dict[int, int] = field(default_factory=dict)
    ping_n: int = 0
    next_ping_mono: float = 0.0
    last_estop: dict | None = None
    persisted_boot_id: str | None = None
    persisted_evt_ack: int = 0
    rate: dict[str, float] = field(default_factory=dict)


class SatelliteHub:
    """进程级单例由 :func:`get_satellite_hub` 提供；测试可直接构造并用
    :func:`reset_satellite_hub` 装进单例。"""

    def __init__(self, *, data_root: str | os.PathLike | None = None,
                 config_path: str | os.PathLike | None = None,
                 degraded_after_s: float = DEGRADED_AFTER_S,
                 lost_after_s: float = LOST_AFTER_S,
                 hello_timeout_s: float = HELLO_TIMEOUT_S,
                 live=None, bus=None, stm_ring=None,
                 enabled_fn: Callable[[], bool] | None = None) -> None:
        self._data_root_explicit = Path(data_root) if data_root else None
        self._config_path_explicit = Path(config_path) if config_path else None
        self.degraded_after_s = float(degraded_after_s)
        self.lost_after_s = float(lost_after_s)
        self.hello_timeout_s = float(hello_timeout_s)
        self.hub_boot_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._records: dict[str, _Record] = {}
        self._cmds: dict[str, PendingCommand] = {}
        self._finished: deque[str] = deque()
        self._listeners: dict[int, Callable[[str, dict], None]] = {}
        self._listener_ids = itertools.count(1)
        self._dispatch_q: queue.Queue = queue.Queue(maxsize=20000)
        self._dispatch_dropped = 0
        self._started = False
        self._shut_down = False              # shutdown() 之后为真，直到显式 start()
        self._shutdown_safe: dict | None = None       # safe_for_shutdown 的结果（只做一次）
        self._shutdown_safe_lock = threading.Lock()
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self._cfg: dict = hc.empty_config()
        self._cfg_mtime: float | None = None
        self._cfg_error: str | None = None
        self._cfg_checked_mono = 0.0
        self._state_dirty = False
        self._state_saved_mono = 0.0
        self._enabled_fn = enabled_fn
        self._enabled_cache: tuple[float, bool] = (0.0, True)
        self._bus = bus
        self._runtime_ingest: Callable[[list[Path], str], Any] | None = None

        from mast.satellite.files import FileArchive, FileReceiver
        from mast.satellite.live_bridge import LiveBridge
        from mast.satellite.stm_ring import StmStateRing

        self.stm_ring = stm_ring if stm_ring is not None else StmStateRing()
        self._archive = FileArchive(self.data_root)
        self._receiver = FileReceiver(
            self._archive, context_fn=self._file_context, stm_ring=self.stm_ring,
            ingest_fn=self._ingest_files, on_stored=self._on_file_stored)
        self.bridge = LiveBridge(self, live=live)
        self._load_config(force=True)

    # ── 路径与配置 ────────────────────────────────────────────────────────
    def data_root(self) -> Path:
        return hc.data_root(self._data_root_explicit)

    def config_path(self) -> Path:
        return self._config_path_explicit or hc.config_path(self.data_root())

    def _state_path(self) -> Path:
        return self.data_root() / "satellite" / STATE_FILE

    def _load_config(self, *, force: bool = False) -> None:
        p = self.config_path()
        try:
            mtime = p.stat().st_mtime if p.exists() else None
        except OSError:
            mtime = None
        if not force and mtime == self._cfg_mtime:
            return
        try:
            cfg = hc.load(p)
            self._cfg_error = None
        except hc.HubConfigError as exc:
            # 坏配置：沿用上一次读到的好配置，并大声说出来。
            if self._cfg_error != str(exc):
                logger.error("卫星配置读不了，沿用上一次的配置：%s", exc)
            self._cfg_error = str(exc)
            self._cfg_mtime = mtime
            return
        with self._lock:
            self._cfg = cfg
            self._cfg_mtime = mtime
            for e in cfg.get("satellites") or []:
                if isinstance(e, dict) and e.get("id"):
                    self._record(str(e["id"]))
        logger.info("卫星配置已加载：%s（%d 颗）", p, len(cfg.get("satellites") or []))

    def _spec(self, sat_id: str) -> dict | None:
        e = hc.find(self._cfg, sat_id)
        return hc.normalized_entry(e) if e is not None else None

    def configured_ids(self) -> list[str]:
        return [str(e.get("id")) for e in (self._cfg.get("satellites") or [])
                if isinstance(e, dict) and e.get("id")]

    def hub_enabled(self) -> bool:
        """总开关（设置键 ``satellite_hub_enabled``，默认开）。读不到当作开。"""
        fn = self._enabled_fn
        if fn is not None:
            try:
                return bool(fn())
            except Exception:  # noqa: BLE001
                return True
        ts, val = self._enabled_cache
        if time.monotonic() - ts < 1.0:
            return val
        val = True
        try:
            from mast.webui.settings_store import settings_store_for_runtime

            v = settings_store_for_runtime().get("satellite_hub_enabled", None)
            val = True if v is None else bool(v)
        except Exception:  # noqa: BLE001
            val = True
        self._enabled_cache = (time.monotonic(), val)
        return val

    # ── 生命周期 ──────────────────────────────────────────────────────────
    def start(self, runtime: Any = None) -> "SatelliteHub":
        """启动分发与监视线程、订阅 STM 状态。重复调用无副作用。"""
        if runtime is not None:
            self.attach_runtime(runtime)
        with self._lock:
            if self._started:
                return self
            self._started = True
            self._shut_down = False
            self._shutdown_safe = None
            self._stopping.clear()
        self._load_persisted_state()
        try:
            self.stm_ring.start(self._bus)
        except Exception:  # noqa: BLE001
            logger.debug("stm ring start failed", exc_info=True)
        for name, target in (("mast-satellite-dispatch", self._dispatch_loop),
                             ("mast-satellite-monitor", self._monitor_loop)):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        logger.info("SatelliteHub 已启动（hub_boot_id=%s，配置 %s）", self.hub_boot_id,
                    self.config_path())
        return self

    def ensure_started(self) -> "SatelliteHub":
        """端点在每条连接上调：还没启动就启动（枢纽在第一颗卫星连上时自动启动）；
        **关停过就不再拉起来** —— 关停钩子关掉会话之后，卫星会立刻重连，那时服务还在退出途中，
        重新拉起来的枢纽会又开始收发命令。显式的 :meth:`start` 照常能重启。"""
        if self._shut_down:
            return self
        return self.start()

    def attach_runtime(self, runtime: Any) -> None:
        """有活动实验时，卫星文件经 runtime 的 ingest 队列登记进实验文件夹。"""
        submit = getattr(runtime, "_submit_ingest", None)
        if callable(submit):
            def _ingest(paths: list[Path], sat_id: str, _s=submit) -> str:
                # file_locations.source 有 CHECK 约束（skill/manual/migrated/import/inplace/
                # stray），这里用 "import"：从另一台机器导入。卫星来源写在 skill 与 .meta.json 里。
                _s(None, [str(p) for p in paths], source="import", skill=f"satellite:{sat_id}")
                return "submitted"
            self._runtime_ingest = _ingest

    @property
    def started(self) -> bool:
        return self._started

    def shutdown(self, *, close_sessions: bool = True, wait_s: float = 0.5) -> None:
        """停线程、存状态、关会话（码 1001）。永不抛。之后的连接一律 503，命令一律不发。"""
        try:
            self._shut_down = True
            self._stopping.set()
            if close_sessions:
                with self._lock:
                    sessions = [r.session for r in self._records.values() if r.session]
                for s in sessions:
                    try:
                        s.transport.close(P.CLOSE_GOING_AWAY, "hub shutting down")
                    except Exception:  # noqa: BLE001
                        pass
            self._save_state(force=True)
            try:
                self._dispatch_q.put_nowait(None)
            except queue.Full:
                pass
            for t in self._threads:
                t.join(timeout=wait_s)
            self._threads.clear()
            try:
                self.stm_ring.stop()
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                self._started = False
        except Exception:  # noqa: BLE001
            logger.warning("SatelliteHub.shutdown 出错", exc_info=True)

    # ── 记录 ──────────────────────────────────────────────────────────────
    def _record(self, sat_id: str) -> _Record:
        rec = self._records.get(sat_id)
        if rec is None:
            rec = _Record(sat_id=sat_id)
            self._records[sat_id] = rec
        return rec

    # ── 认证（端点在 accept 之前调） ──────────────────────────────────────
    def authenticate(self, sat_id: str, token: str | None) -> tuple[int, str]:
        """返回 ``(HTTP 状态, 原因)``：200 通过；401 凭据不对；403 被禁用；503 枢纽在关停。"""
        if self._shut_down:
            return 503, "satellite hub is shutting down"
        self._load_config()
        spec = self._spec(sat_id) if sat_id else None
        ok = hc.verify_token(spec, token)
        if not sat_id or spec is None or not ok:
            return 401, "invalid satellite credentials"
        if not spec.get("enabled", True):
            return 403, "satellite disabled in satellites.json"
        if not self.hub_enabled():
            return 403, "satellite hub disabled (setting satellite_hub_enabled)"
        return 200, "ok"

    # ── 会话 ──────────────────────────────────────────────────────────────
    def attach(self, sat_id: str, transport: Transport, hello: P.Hello) -> _Session:
        """卫星 hello 之后调用：建立会话，发 welcome（以及 cmd_status）。"""
        if hello.satellite_id != sat_id:
            raise SatelliteError(f"hello.satellite_id={hello.satellite_id!r} 与认证的 id 不符")
        spec = self._spec(sat_id) or hc.normalized_entry({"id": sat_id})
        events: list[tuple] = []
        with self._lock:
            rec = self._record(sat_id)
            old = rec.session
            if old is not None:
                try:
                    old.transport.close(P.CLOSE_REPLACED, "replaced by a newer connection")
                except Exception:  # noqa: BLE001
                    pass
            prev_boot = rec.boot_id or rec.persisted_boot_id
            restarted = prev_boot is not None and prev_boot != hello.boot_id
            if restarted:
                events.append(self._handle_restart_locked(rec, prev_boot, hello.boot_id))
            elif rec.boot_id is None and prev_boot == hello.boot_id:
                # 枢纽重启过、卫星没有：用持久化的游标，免得重收已落地的事件。
                rec.landed_upto = max(rec.landed_upto, rec.persisted_evt_ack)
            if hello.evt_seq < rec.landed_upto and not restarted:
                logger.warning("卫星 %s 的 evt_seq=%d 小于已落地 %d（同一 boot_id）—— 以卫星为准",
                               sat_id, hello.evt_seq, rec.landed_upto)
                rec.landed_upto = hello.evt_seq
            rec.boot_id = hello.boot_id
            rec.hello = {"sw": hello.sw, "host": hello.host, "proto": hello.proto,
                         "time_ms": hello.time_ms, "evt_seq": hello.evt_seq}
            new_devices: dict[str, dict] = {}
            for d in hello.devices:
                desc = d.model_dump(mode="json")
                new_devices[d.id] = desc
            rec.devices = new_devices
            session = _Session(sat_id=sat_id, session_id=uuid.uuid4().hex[:12],
                               transport=transport, boot_id=hello.boot_id, opened_ms=now_ms(),
                               peer=getattr(transport, "peer", ""))
            rec.session = session
            rec.pings.clear()
            rec.last_alive_mono = time.monotonic()
            rec.last_seen_ms = now_ms()
            rec.next_ping_mono = time.monotonic()      # 立刻 ping 一次，尽快有对时样本
            rec.counters["connects"] += 1
            rec.ever_connected = True
            rec.connected_since_ms = now_ms()
            rec.acked_upto = rec.landed_upto
            welcome = {
                "t": "welcome", "proto": P.PROTO_VERSION, "hub_boot_id": self.hub_boot_id,
                "session": session.session_id, "time_ms": now_ms(),
                "evt_ack": int(rec.landed_upto),
                "config": {"heartbeat_s": spec["heartbeat_s"], "deadman_s": spec["deadman_s"],
                           "max_upload_bytes_per_s": spec["max_upload_bytes_per_s"]},
            }
            transport.send(welcome)
            inflight = [c.id for c in rec.pending.values() if not c.done]
            if inflight:
                transport.send({"t": "cmd_status", "ids": inflight})
            prev_state = rec.state
            rec.state = LINK_CONNECTED
            self._state_dirty = True
        logger.info("卫星 %s 已连接（%s，boot_id=%s，%d 个设备，在途命令 %d 条）%s", sat_id,
                    session.peer, hello.boot_id, len(hello.devices), len(inflight),
                    "【检测到重启】" if restarted else "")
        for ev in events:
            self._enqueue(ev)
        self._emit_link(sat_id, prev_state, LINK_CONNECTED, "hello")
        return session

    def _handle_restart_locked(self, rec: _Record, old_boot: str, new_boot: str) -> tuple:
        """卫星重启（包括整机崩溃重启）：在途命令判 lost_on_restart，绝不自动重放；
        设备状态全部标陈旧；事件游标归零；对时样本作废。"""
        lost = [c for c in rec.pending.values() if not c.done]
        rec.pending.clear()
        for c in lost:
            self._finish_cmd_locked(c, exc=SatelliteCommandError(
                "lost_on_restart",
                tr("卫星重启了（boot_id 变化），这条命令的结局未知；枢纽不会自动重发。"
                   "位置、快门、相机设置一律当作不可信，重新查询后再继续。",
                   "The satellite restarted (boot_id changed), so the outcome of this command is "
                   "unknown; the hub will not resend it automatically. Treat positions, shutter and "
                   "camera settings as untrusted and query them again before continuing."),
                sat_id=rec.sat_id, cmd_id=c.id))
        for v in rec.latest.values():
            v["stale"] = True
        rec.landed_upto = 0
        rec.acked_upto = -1
        rec.clock.reset()
        rec.counters["restarts"] += 1
        logger.warning("卫星 %s 重启了：boot_id %s → %s；%d 条在途命令判为 lost_on_restart",
                       rec.sat_id, old_boot, new_boot, len(lost))
        return ("hub_evt", rec.sat_id, {
            "kind": "restart", "old_boot_id": old_boot, "new_boot_id": new_boot,
            "lost_commands": [c.id for c in lost]})

    def detach(self, session: _Session | None, reason: str = "") -> None:
        if session is None:
            return
        with self._lock:
            rec = self._records.get(session.sat_id)
            if rec is None or rec.session is not session:
                return                      # 已被新连接顶替
            rec.session = None
            rec.disconnected_ms = now_ms()
            prev = rec.state
            rec.state = LINK_LOST
            self._state_dirty = True
        logger.info("卫星 %s 断开（%s）", session.sat_id, reason or "closed")
        self._emit_link(session.sat_id, prev, LINK_LOST, reason or "closed")

    # ── 收消息（WebSocket 线程） ──────────────────────────────────────────
    def on_message(self, session: _Session, msg: Any, rx_ms: int | None = None) -> None:
        rx_ms = rx_ms if rx_ms is not None else now_ms()
        with self._lock:
            rec = self._records.get(session.sat_id)
            if rec is None or rec.session is not session:
                return
            rec.last_seen_ms = rx_ms
        if isinstance(msg, P.Evt):
            self._on_evt(rec, session, msg, rx_ms)
        elif isinstance(msg, P.Pong):
            self._on_pong(rec, msg, rx_ms)
        elif isinstance(msg, P.Ack):
            self._on_ack(rec, msg)
        elif isinstance(msg, P.Result):
            self._on_result(rec, msg.model_dump(mode="json"))
        elif isinstance(msg, P.CmdStatusReply):
            self._on_cmd_status_reply(rec, session, msg)
        elif isinstance(msg, P.EstopAck):
            self._on_estop_ack(rec, msg)
        elif isinstance(msg, P.Log):
            self._on_log(rec, msg)
        elif isinstance(msg, P.Hello):
            self.note_protocol_error(session, P.ProtocolError("dup_hello", "会话中途又发了 hello"),
                                     close=True)
        elif isinstance(msg, P.UnknownMessage):
            if self._rate_ok(rec, f"unknown:{msg.t}", 60.0):
                logger.info("卫星 %s 发来不认识的消息 t=%r（忽略）", rec.sat_id, msg.t)
        elif isinstance(msg, (P.FileOffer, P.FileChunk, P.FileEnd)):
            self.on_file_message(session, msg)

    def note_protocol_error(self, session: _Session, err: P.ProtocolError, *,
                            close: bool = False) -> None:
        rec = self._records.get(session.sat_id)
        if rec is not None:
            rec.counters["protocol_errors"] += 1
            if self._rate_ok(rec, f"proto:{err.code}", 10.0):
                logger.warning("卫星 %s 协议错误：%s", session.sat_id, err)
        if close:
            session.transport.close(P.CLOSE_PROTOCOL, err.code)

    def _rate_ok(self, rec: _Record, key: str, period_s: float) -> bool:
        now = time.monotonic()
        last = rec.rate.get(key, 0.0)
        if now - last >= period_s:
            rec.rate[key] = now
            return True
        return False

    def _on_pong(self, rec: _Record, msg: P.Pong, rx_ms: int) -> None:
        with self._lock:
            t0 = rec.pings.pop(msg.n, None)
            if t0 is None or t0 != msg.t0:
                return
            sample = rec.clock.add(msg.t0, msg.t1, msg.t2, rx_ms)
            rec.last_alive_mono = time.monotonic()
            prev = rec.state
            if rec.state != LINK_CONNECTED and rec.session is not None:
                rec.state = LINK_CONNECTED
            snap = rec.clock.snapshot()
        if prev != LINK_CONNECTED:
            self._emit_link(rec.sat_id, prev, LINK_CONNECTED, "pong")
        if sample is not None:
            self._enqueue(("health", rec.sat_id, snap))

    def _on_evt(self, rec: _Record, session: _Session, evt: P.Evt, rx_ms: int) -> None:
        enriched = None
        notes: list[tuple] = []
        with self._lock:
            if evt.seq <= rec.landed_upto:
                rec.counters["dup_events"] += 1
                return
            if evt.seq > rec.landed_upto + 1:
                gap = evt.seq - rec.landed_upto - 1
                rec.counters["gap_events"] += gap
                if self._rate_ok(rec, "gap", 10.0):
                    logger.warning("卫星 %s 事件有缺口：%d..%d 没收到（卫星缓冲溢出或枢纽重启）",
                                   rec.sat_id, rec.landed_upto + 1, evt.seq - 1)
            rec.landed_upto = evt.seq
            rec.counters["events"] += 1
            self._state_dirty = True
            offset = rec.clock.offset_ms
            ts_hub = float(evt.ts_ms) - (offset or 0.0)
            enriched = {
                "sat_id": rec.sat_id, "boot_id": session.boot_id, "seq": evt.seq,
                "device": evt.device, "kind": evt.kind, "ts_ms": evt.ts_ms,
                "ts_hub_ms": ts_hub, "clock_offset_ms": offset,
                "clock_delay_ms": rec.clock.delay_ms, "rx_ms": rx_ms, "data": evt.data,
            }
            key = (evt.device, evt.kind)
            rec.latest[key] = {"data": evt.data, "ts_ms": evt.ts_ms, "ts_hub_ms": ts_hub,
                               "seq": evt.seq, "stale": False}
            rec.recent.append(_summary(enriched))
            if evt.kind == "device_state":
                notes.extend(self._apply_device_state_locked(rec, evt.device, evt.data))
            elif evt.kind in ("deadman", "error"):
                if evt.kind == "deadman" or self._rate_ok(rec, f"err:{evt.device}", 10.0):
                    notes.append(("bus_only", rec.sat_id, {
                        "kind": evt.kind, "device": evt.device,
                        "message": str(evt.data.get("message") or evt.data.get("reason") or "")[:200]}))
            elif evt.kind == "manual_activity" and self._rate_ok(rec, f"manual:{evt.device}", 10.0):
                notes.append(("bus_only", rec.sat_id, {"kind": "manual", "device": evt.device}))
            cmd_id = evt.data.get("cmd_id") if isinstance(evt.data, dict) else None
            if cmd_id and evt.kind in ("acq_started", "acq_progress", "spectrum", "axis_state"):
                c = rec.pending.get(str(cmd_id))
                if c is not None and c.state in ("sent", "accepted"):
                    c.state = "running"
        if evt.kind == "deadman":
            logger.warning("卫星 %s 报告死人开关动作：%s", rec.sat_id, evt.data)
        self._enqueue(("evt", rec.sat_id, enriched))
        for n in notes:
            self._enqueue(n)

    def _apply_device_state_locked(self, rec: _Record, device: str, data: dict) -> list[tuple]:
        desc = rec.devices.get(device)
        if desc is None:
            desc = {"id": device, "kind": "", "driver": "", "state": "not_started", "caps": [],
                    "info": {}}
            rec.devices[device] = desc
        prev_state = desc.get("state")
        prev_hold = bool(desc.get("operator_hold", False))
        state = data.get("state")
        if isinstance(state, str) and state:
            desc["state"] = state
        if "operator_hold" in data:
            desc["operator_hold"] = bool(data.get("operator_hold"))
        new_hold = bool(desc.get("operator_hold", False))
        out: list[tuple] = []
        changed_state = desc.get("state") != prev_state
        notable = (desc.get("state") in _NOTABLE_DEVICE_STATES
                   or prev_state in _NOTABLE_DEVICE_STATES)
        if new_hold != prev_hold or (changed_state and notable):
            out.append(("bus_only", rec.sat_id, {
                "kind": "device", "device": device, "state": desc.get("state"),
                "prev_state": prev_state, "operator_hold": new_hold}))
        return out

    def _on_ack(self, rec: _Record, msg: P.Ack) -> None:
        with self._lock:
            c = rec.pending.get(msg.id) or self._cmds.get(msg.id)
            if c is None or c.done:
                return
            c.ack_state = msg.state
            if msg.state in ("accepted", "duplicate_running"):
                if c.state in ("pending", "sent"):
                    c.state = "accepted"
            elif msg.state == "rejected":
                code = P.error_code(msg.reason)
                self._finish_cmd_locked(c, exc=SatelliteCommandError(
                    code, tr(f"卫星拒收：{msg.reason or ''}",
                             f"Rejected by the satellite: {msg.reason or ''}"),
                    sat_id=rec.sat_id, cmd_id=c.id))
            # duplicate_done：卫星随后会把缓存的 result 再发一遍，等它。

    def _on_result(self, rec: _Record, res: dict) -> None:
        cid = str(res.get("id") or "")
        with self._lock:
            c = rec.pending.get(cid) or self._cmds.get(cid)
            if c is None or c.done:
                rec.counters["late_results"] += 1
                if self._rate_ok(rec, "late_result", 30.0):
                    logger.info("卫星 %s 的 result id=%s 没有对应的在途命令（已收尾或枢纽重启过）",
                                rec.sat_id, cid)
                return
            if res.get("ok"):
                self._finish_cmd_locked(c, value=res.get("value"), result=res)
            else:
                err = res.get("error") or {}
                code = P.error_code(err.get("code") if isinstance(err, dict) else None)
                msg = str(err.get("message") or "") if isinstance(err, dict) else ""
                self._finish_cmd_locked(c, exc=SatelliteCommandError(
                    code, msg, sat_id=rec.sat_id, cmd_id=c.id, result=res), result=res)

    def _on_cmd_status_reply(self, rec: _Record, session: _Session, msg: P.CmdStatusReply) -> None:
        resend: list[PendingCommand] = []
        with self._lock:
            for item in msg.items:
                c = rec.pending.get(item.id)
                if c is None or c.done:
                    continue
                if item.state == "done" and item.result:
                    pass                                      # 走下面 _on_result
                elif item.state == "running":
                    c.state = "running"
                elif item.state == "lost_on_restart":
                    self._finish_cmd_locked(c, exc=SatelliteCommandError(
                        "lost_on_restart",
                        tr("卫星报告这条命令随重启丢失",
                           "The satellite reports that this command was lost with its restart"),
                        sat_id=rec.sat_id, cmd_id=c.id))
                elif item.state == "unknown":
                    # 同一 boot_id 下卫星不知道它 = 断线前根本没送到。按 request_id 幂等，
                    # 在时限内原样补发是安全的（换了 boot_id 的情形在 attach 里已判丢失）。
                    if time.monotonic() < c.deadline_mono:
                        resend.append(c)
                    else:
                        self._finish_cmd_locked(c, exc=SatelliteCommandError(
                            "unavailable",
                            tr("断线时命令没有送达，且已过时限",
                               "The command was not delivered while the link was down, and its "
                               "time limit has passed"),
                            sat_id=rec.sat_id, cmd_id=c.id))
        for item in msg.items:
            if item.state == "done" and item.result:
                r = dict(item.result)
                r.setdefault("id", item.id)
                self._on_result(rec, r)
        for c in resend:
            self._transmit(session, c)

    def _on_estop_ack(self, rec: _Record, msg: P.EstopAck) -> None:
        with self._lock:
            info = rec.last_estop or {"reason": "", "sent_ms": None}
            extra = msg.model_dump(mode="json")
            info["ack"] = {"stopped": list(msg.stopped),
                           "errors": [e.model_dump(mode="json") for e in msg.errors],
                           "active": extra.get("active") if isinstance(extra.get("active"), list)
                           else None,
                           "rx_ms": now_ms()}
            rec.last_estop = info
        lvl = logging.WARNING if msg.errors else logging.INFO
        logger.log(lvl, "卫星 %s 急停回执：停了 %s，错误 %s", rec.sat_id, msg.stopped,
                   [e.model_dump(mode="json") for e in msg.errors])
        # 停不下来的设备要点名（例如 xyz：Motion_Manipulator 没有远程停止，只能在它自己的窗口里
        # 按 STOP）—— 只给一个计数的话，操作员看到「急停已执行」，台子却可能还在动。
        self._enqueue(("hub_evt", rec.sat_id, {
            "kind": "estop_ack", "stopped": list(msg.stopped), "errors": len(msg.errors),
            "error_devices": [e.device for e in msg.errors],
            "first_error": (f"{msg.errors[0].device}: {msg.errors[0].message}"[:300]
                            if msg.errors else "")}))

    def _on_log(self, rec: _Record, msg: P.Log) -> None:
        lvl = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
               "warn": logging.WARNING, "error": logging.ERROR,
               "critical": logging.CRITICAL}.get(str(msg.level).lower(), logging.INFO)
        if lvl >= logging.WARNING or self._rate_ok(rec, "log", 1.0):
            logger.log(lvl, "[卫星 %s] %s", rec.sat_id, str(msg.msg)[:2000])

    # ── 文件（端点放在线程池里调；这里有磁盘 I/O） ──────────────────────────
    def on_file_message(self, session: _Session, msg: Any) -> None:
        with self._lock:
            rec = self._records.get(session.sat_id)
            if rec is None or rec.session is not session:
                return
        try:
            if isinstance(msg, P.FileOffer):
                replies = self._receiver.offer(session.sat_id, msg)
            elif isinstance(msg, P.FileChunk):
                replies = self._receiver.chunk(session.sat_id, msg)
            elif isinstance(msg, P.FileEnd):
                replies = self._receiver.end(session.sat_id, msg)
            else:
                return
        except P.ProtocolError as exc:
            self.note_protocol_error(session, exc)
            replies = [{"t": "file_done", "file_id": getattr(msg, "file_id", ""), "ok": False,
                        "error": exc.code}]
        except Exception as exc:  # noqa: BLE001 — 文件出错不能拖垮会话
            logger.warning("卫星 %s 文件处理出错：%s", session.sat_id, exc, exc_info=True)
            replies = [{"t": "file_done", "file_id": getattr(msg, "file_id", ""), "ok": False,
                        "error": f"internal: {type(exc).__name__}"}]
        for r in replies:
            session.transport.send(r)

    def _file_context(self, sat_id: str) -> dict:
        with self._lock:
            rec = self._records.get(sat_id)
            if rec is None:
                return {"boot_id": None, "hub_boot_id": self.hub_boot_id, "clock": {}}
            return {"boot_id": rec.boot_id, "hub_boot_id": self.hub_boot_id,
                    "clock": rec.clock.snapshot()}

    def _ingest_files(self, paths: list[Path], sat_id: str) -> str:
        fn = self._runtime_ingest
        if fn is None:
            return "no_runtime"
        try:
            return str(fn(paths, sat_id) or "submitted")
        except Exception as exc:  # noqa: BLE001
            logger.warning("卫星文件登记进实验文件夹失败：%s", exc)
            return f"error: {type(exc).__name__}"

    def _on_file_stored(self, entry: dict) -> None:
        sat_id = str(entry.get("sat_id") or "")
        with self._lock:
            rec = self._records.get(sat_id)
            publish = rec is None or self._rate_ok(rec, "file_stored", 5.0)
        if publish:
            self._enqueue(("hub_evt", sat_id, {"kind": "file", "file_id": entry.get("file_id"),
                                               "name": entry.get("name"),
                                               "stored": entry.get("stored_rel")}))

    def files(self):
        """已入库文件的查询（契约 §6 ``hub.files()``）。"""
        return self._archive

    # ── 命令（调用方线程） ────────────────────────────────────────────────
    def submit(self, sat_id: str, device: str, action: str, args: dict | None = None, *,
               timeout_s: float = 30.0, request_id: str | None = None) -> PendingCommand:
        return self._submit(sat_id, device, action, args, timeout_s=timeout_s,
                            request_id=request_id, allow_degraded=False)

    def call(self, sat_id: str, device: str, action: str, args: dict | None = None, *,
             timeout_s: float = 30.0, request_id: str | None = None,
             check_abort: Callable[[], Any] | None = None):
        cmd = self.submit(sat_id, device, action, args, timeout_s=timeout_s,
                          request_id=request_id)
        return cmd.wait(check_abort=check_abort)

    def _submit(self, sat_id, device, action, args, *, timeout_s, request_id,
                allow_degraded) -> PendingCommand:
        if not device or not action:
            raise SatelliteCommandError(
                "invalid_args", tr("device 和 action 都不能为空", "device and action must not be empty"),
                sat_id=sat_id)
        try:
            timeout_s = float(timeout_s)
        except (TypeError, ValueError):
            raise SatelliteCommandError(
                "invalid_args", tr(f"timeout_s={timeout_s!r} 不是数",
                                   f"timeout_s={timeout_s!r} is not a number"),
                sat_id=sat_id) from None
        if not (timeout_s > 0):
            raise SatelliteCommandError(
                "invalid_args", tr("timeout_s 必须大于 0", "timeout_s must be greater than 0"),
                sat_id=sat_id)
        args = dict(args or {})
        with self._lock:
            if self._stopping.is_set():
                # 关停开始后一律不发：关停钩子已经对每颗卫星发过 safe 并等了确认；这时再从
                # 别处（例如本机光学台的 stop_all 经远程驱动）补一条 stop，会打断有人在卫星上
                # 手动做的采集 —— 正常关停只该 safe。
                raise SatelliteUnavailable(
                    tr("枢纽正在关停，命令不发（卫星设备在关停时只收 safe）",
                       "The hub is shutting down; the command is not sent (satellite devices only "
                       "accept safe during shutdown)"),
                    sat_id=sat_id)
            if request_id:
                existing = self._cmds.get(str(request_id))
                if existing is not None:
                    if (existing.sat_id, existing.device, existing.action) != (sat_id, device, action):
                        raise SatelliteCommandError(
                            "invalid_args",
                            tr(f"request_id {request_id} 已用于另一条命令",
                               f"request_id {request_id} is already used by another command"),
                            sat_id=sat_id, cmd_id=str(request_id))
                    return existing
            rec = self._records.get(sat_id)
            if rec is None or (self._spec(sat_id) is None and not rec.ever_connected):
                raise SatelliteUnavailable(
                    tr(f"没有这颗卫星：{sat_id!r}", f"No such satellite: {sat_id!r}"), sat_id=sat_id)
            okstates = (LINK_CONNECTED, LINK_DEGRADED) if allow_degraded else (LINK_CONNECTED,)
            if rec.session is None or rec.state not in okstates:
                raise SatelliteUnavailable(
                    tr(f"卫星 {sat_id} 当前状态是 {rec.state}，命令不发（unavailable）",
                       f"Satellite {sat_id} is currently {rec.state}; the command is not sent "
                       f"(unavailable)"),
                    sat_id=sat_id)
            if rec.devices and device not in rec.devices:
                raise SatelliteCommandError(
                    "invalid_args",
                    tr(f"卫星 {sat_id} 没有设备 {device!r}（有 {sorted(rec.devices)}）",
                       f"Satellite {sat_id} has no device {device!r} (it has {sorted(rec.devices)})"),
                    sat_id=sat_id)
            self._check_hub_limits(sat_id, device, action, args)
            cmd = PendingCommand(self, cmd_id=str(request_id or uuid.uuid4()), sat_id=sat_id,
                                 device=device, action=action, args=args, timeout_s=timeout_s)
            cmd.boot_id = rec.boot_id
            rec.pending[cmd.id] = cmd
            self._cmds[cmd.id] = cmd
            rec.counters["cmds"] += 1
            session = rec.session
        self._transmit(session, cmd)
        return cmd

    def _transmit(self, session: _Session, cmd: PendingCommand) -> None:
        remaining = max(0.1, cmd.deadline_mono - time.monotonic()
                        - min(CMD_GRACE_MAX_S, max(CMD_GRACE_MIN_S, 0.1 * cmd.timeout_s)))
        sent = session.transport.send({
            "t": "cmd", "id": cmd.id, "device": cmd.device, "action": cmd.action,
            "args": cmd.args, "timeout_s": round(min(cmd.timeout_s, remaining), 3)})
        cmd.sends += 1
        if cmd.state == "pending":
            cmd.state = "sent"
        if not sent:
            logger.info("命令 %s 入队失败（连接正在关闭），等重连后的 cmd_status 找回", cmd.id)

    def _check_hub_limits(self, sat_id: str, device: str, action: str, args: dict) -> None:
        """枢纽侧软限位（契约 §5.4：XYZ 两边都做软限位、单次步长有上限）。
        只在 satellites.json 里给了限位时才检查；卫星侧照样会检查。"""
        spec = self._spec(sat_id) or {}
        dcfg = (spec.get("devices") or {}).get(device) or {}
        limits = dcfg.get("limits_mm")
        max_step = dcfg.get("max_step_mm")
        try:
            if action == "move_abs" and isinstance(limits, dict):
                for ax in ("x", "y", "z"):
                    if args.get(ax) is not None and ax in limits:
                        lo, hi = limits[ax]
                        v = float(args[ax])
                        if not (float(lo) <= v <= float(hi)):
                            raise SatelliteCommandError(
                                "limit",
                                tr(f"{device}.{ax}={v} mm 超出枢纽软限位 [{lo}, {hi}]",
                                   f"{device}.{ax}={v} mm is outside the hub soft limits [{lo}, {hi}]"),
                                sat_id=sat_id)
            if action == "move_rel" and max_step is not None:
                for ax in ("dx", "dy", "dz"):
                    if args.get(ax) is not None and abs(float(args[ax])) > float(max_step):
                        raise SatelliteCommandError(
                            "limit",
                            tr(f"{device}.{ax}={args[ax]} mm 超过单次步长上限 {max_step} mm",
                               f"{device}.{ax}={args[ax]} mm exceeds the single-step limit of "
                               f"{max_step} mm"),
                            sat_id=sat_id)
        except (TypeError, ValueError) as exc:
            raise SatelliteCommandError(
                "invalid_args", tr(f"{device}.{action} 参数不是数：{exc}",
                                   f"{device}.{action}: a parameter is not a number: {exc}"),
                sat_id=sat_id) from None

    def _finish_cmd_locked(self, cmd: PendingCommand, *, value: Any = None,
                           exc: Exception | None = None, result: dict | None = None) -> None:
        rec = self._records.get(cmd.sat_id)
        if rec is not None:
            rec.pending.pop(cmd.id, None)
            if exc is None:
                rec.counters["cmds_ok"] += 1
            else:
                rec.counters["cmds_failed"] += 1
        self._finished.append(cmd.id)
        while len(self._finished) > FINISHED_CMDS_KEEP:
            old = self._finished.popleft()
            oc = self._cmds.get(old)
            if oc is not None and oc.done:
                self._cmds.pop(old, None)
        # 回调可能很快就调别的枢纽方法；在锁里完成是安全的（RLock，回调只许做轻活）。
        cmd._finish(value=value, exc=exc, result=result)

    def _expire(self, cmd: PendingCommand) -> None:
        with self._lock:
            if cmd.done:
                return
            rec = self._records.get(cmd.sat_id)
            session = rec.session if rec else None
            self._finish_cmd_locked(cmd, exc=SatelliteTimeout(
                tr(f"{cmd.sat_id}/{cmd.device}.{cmd.action} 超过时限 {cmd.timeout_s} s 没有结果，"
                   "已请卫星取消",
                   f"{cmd.sat_id}/{cmd.device}.{cmd.action} produced no result within the time "
                   f"limit of {cmd.timeout_s} s; the satellite has been asked to cancel"),
                sat_id=cmd.sat_id, cmd_id=cmd.id, command_expired=True))
        if session is not None:
            session.transport.send({"t": "cancel", "id": cmd.id})

    def _send_cancel(self, cmd: PendingCommand) -> None:
        with self._lock:
            rec = self._records.get(cmd.sat_id)
            session = rec.session if rec else None
        if session is not None and not cmd.done:
            session.transport.send({"t": "cancel", "id": cmd.id})

    def stop_device(self, sat_id: str, device: str) -> None:
        """尽力而为地停一个设备，不等结果，永不抛。"""
        try:
            self._submit(sat_id, device, "stop", {}, timeout_s=10.0, request_id=None,
                         allow_degraded=True)
        except Exception as exc:  # noqa: BLE001
            logger.info("stop_device(%s, %s) 没发出去：%s", sat_id, device, exc)

    def emergency_stop_all(self, reason: str = "") -> dict:
        """E-STOP：对每个已连接的卫星发 ``estop``，并对它的每个设备发 ``stop``。

        非阻塞（只入队）、永不抛。返回 ``{sent, unreachable, errors}``；配置了却连不上的
        卫星列在 unreachable 里 —— 它们只能靠自己的死人开关在 deadman_s 内停下。
        """
        out: dict[str, Any] = {"sent": [], "unreachable": [], "errors": []}
        why = (str(reason or "").strip() or "MAST E-STOP")[:200]
        try:
            with self._lock:
                recs = list(self._records.values())
                enabled_ids = {e for e in self.configured_ids()
                               if (self._spec(e) or {}).get("enabled", True)}
            for rec in recs:
                try:
                    with self._lock:
                        session = rec.session
                        devices = list(rec.devices)
                        state = rec.state
                    if session is None:
                        if rec.sat_id in enabled_ids:
                            out["unreachable"].append({"id": rec.sat_id, "state": state})
                        continue
                    session.transport.send({"t": "estop", "reason": why})
                    with self._lock:
                        rec.last_estop = {"reason": why, "sent_ms": now_ms(), "ack": None}
                    for dev in devices:
                        try:
                            self._submit(rec.sat_id, dev, "stop", {}, timeout_s=10.0,
                                         request_id=None, allow_degraded=True)
                        except Exception as exc:  # noqa: BLE001
                            out["errors"].append(f"{rec.sat_id}/{dev} stop: {exc}")
                    out["sent"].append(rec.sat_id)
                except Exception as exc:  # noqa: BLE001
                    out["errors"].append(f"{rec.sat_id}: {type(exc).__name__}: {exc}")
            if out["sent"] or out["unreachable"]:
                logger.critical("卫星急停（%s）：已发 %s；连不上 %s", why, out["sent"],
                                [u["id"] for u in out["unreachable"]])
                self._enqueue(("hub_evt", "", {"kind": "estop", "reason": why,
                                               "sent": list(out["sent"]),
                                               "unreachable": [u["id"] for u in out["unreachable"]]}))
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"{type(exc).__name__}: {exc}")
        return out

    def safe_all(self, reason: str = "", *, wait_s: float = 0.0) -> dict:
        """服务**正常关停**用：对每个已连接卫星的每个设备发 ``safe`` 命令 —— 语义等同死人开关，
        只停枢纽发起的运动与采集，**不打断**有人在 LightField 里手动做的采集。

        E-STOP 与看门狗异常（撞针等）仍走 :meth:`emergency_stop_all`（什么都停）。永不抛。

        ``wait_s > 0``：最多等这么久，等卫星把这些 safe 做完再返回。关停钩子要先等确认、再关
        会话：只入队就关，帧虽然按顺序先于关闭帧发出，对端收没收到、停没停，谁也说不上来。
        枢纽已经关停时什么都不发（关停常被调两次，第二次时会话早关了）。

        返回 ``{sent, unreachable, errors, commands, confirmed, stopped, unconfirmed}``：
        ``confirmed`` / ``unconfirmed`` 按卫星列设备，``stopped`` 是卫星报告真停下来的设备。
        """
        out: dict[str, Any] = {"sent": [], "unreachable": [], "errors": [], "commands": {},
                               "confirmed": {}, "stopped": {}, "unconfirmed": {}}
        why = (str(reason or "").strip() or "MAST 关停")[:200]
        if self._stopping.is_set():
            logger.debug("卫星 safe（%s）：枢纽已关停，不再发", why)
            out["skipped"] = True
            return out
        pending: list[PendingCommand] = []
        try:
            with self._lock:
                recs = list(self._records.values())
                enabled_ids = {e for e in self.configured_ids()
                               if (self._spec(e) or {}).get("enabled", True)}
            for rec in recs:
                with self._lock:
                    session = rec.session
                    devices = list(rec.devices)
                    state = rec.state
                if session is None:
                    if rec.sat_id in enabled_ids:
                        out["unreachable"].append({"id": rec.sat_id, "state": state})
                    continue
                ids: dict[str, str] = {}
                for dev in devices:
                    try:
                        c = self._submit(rec.sat_id, dev, "safe", {"reason": why}, timeout_s=10.0,
                                         request_id=None, allow_degraded=True)
                        ids[dev] = c.id
                        pending.append(c)
                    except Exception as exc:  # noqa: BLE001
                        out["errors"].append(f"{rec.sat_id}/{dev} safe: {exc}")
                out["commands"][rec.sat_id] = ids
                out["sent"].append(rec.sat_id)
            if wait_s and wait_s > 0 and pending:
                self._await_safe(pending, float(wait_s), out)
            self._log_safe(why, out, waited=bool(wait_s and wait_s > 0 and pending))
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"{type(exc).__name__}: {exc}")
        return out

    def safe_for_shutdown(self, reason: str = "MAST 关停", *,
                          wait_s: float | None = None) -> dict:
        """服务正常关停时的 safe（发给每颗已连接卫星的每个设备、等回话），**只做一次**。

        关停端点在置 ``should_exit`` 之前调它（uvicorn 一退出就先关掉所有 websocket）；runtime 的
        关停钩子兜底再调 —— 第二次直接返回第一次的结果（带 ``already: True``），不重发。
        并发调用排队：后来的等先来的做完，拿同一份结果。永不抛。
        """
        with self._shutdown_safe_lock:
            if self._shutdown_safe is not None:
                return {**self._shutdown_safe, "already": True}
            out = self.safe_all(reason, wait_s=SHUTDOWN_SAFE_WAIT_S if wait_s is None else wait_s)
            self._shutdown_safe = out
            return out

    def _await_safe(self, pending: list[PendingCommand], wait_s: float, out: dict) -> None:
        deadline = time.monotonic() + wait_s
        for c in pending:
            try:
                value = c.wait(timeout_s=max(0.0, deadline - time.monotonic()))
            except SatelliteTimeout:
                out["unconfirmed"].setdefault(c.sat_id, []).append(c.device)
                continue
            except SatelliteError as exc:
                # 卫星回了话（拒收 / 出错）：送到了，但这台没按 safe 做完 —— 要报出来
                out["confirmed"].setdefault(c.sat_id, []).append(c.device)
                out["errors"].append(f"{c.sat_id}/{c.device} safe: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                out["unconfirmed"].setdefault(c.sat_id, []).append(c.device)
                out["errors"].append(f"{c.sat_id}/{c.device} safe: {exc}")
                continue
            out["confirmed"].setdefault(c.sat_id, []).append(c.device)
            stopped = value.get("stopped") if isinstance(value, dict) else None
            if isinstance(stopped, list) and stopped:
                out["stopped"].setdefault(c.sat_id, []).extend(str(s) for s in stopped)

    @staticmethod
    def _log_safe(why: str, out: dict, *, waited: bool) -> None:
        if not (out["sent"] or out["unreachable"]):
            return
        parts = [f"已发 {out['sent']}"]
        if waited:
            n_sent = {s: len(ids) for s, ids in out["commands"].items()}
            parts.append("确认 " + "，".join(
                f"{s} {len(out['confirmed'].get(s, []))}/{n}" for s, n in n_sent.items()))
            parts.append(f"停下了 {out['stopped'] or '（没有枢纽发起的活）'}")
            if out["unconfirmed"]:
                parts.append(f"没回音 {out['unconfirmed']}")
        if out["unreachable"]:
            parts.append(f"连不上 {[u['id'] for u in out['unreachable']]}（它们靠自己的死人开关）")
        if out["errors"]:
            parts.append(f"错误 {out['errors'][:5]}")
        bad = out["unreachable"] or out["unconfirmed"] or out["errors"] or not waited
        logger.log(logging.WARNING if bad else logging.INFO, "卫星 safe（%s）：%s", why,
                   "；".join(parts))

    # ── 查询 ──────────────────────────────────────────────────────────────
    def latest(self, sat_id: str, device: str, kind: str = "device_state", *,
               include_stale: bool = False) -> dict | None:
        """最近一条该类事件的 ``data``。卫星重启后、新事件到来之前返回 None
        （陈旧的位置与设置不可信）；``include_stale=True`` 时照样返回。"""
        with self._lock:
            rec = self._records.get(sat_id)
            if rec is None:
                return None
            v = rec.latest.get((device, kind))
            if v is None or (v.get("stale") and not include_stale):
                return None
            return dict(v["data"]) if isinstance(v.get("data"), dict) else v.get("data")

    def latest_entry(self, sat_id: str, device: str, kind: str = "device_state") -> dict | None:
        """``latest`` 的带时间戳版本：``{data, ts_ms, ts_hub_ms, seq, stale}``。"""
        with self._lock:
            rec = self._records.get(sat_id)
            v = rec.latest.get((device, kind)) if rec else None
            return dict(v) if v else None

    def clock_offset_ms(self, sat_id: str) -> float | None:
        with self._lock:
            rec = self._records.get(sat_id)
            return rec.clock.offset_ms if rec else None

    def clock(self, sat_id: str) -> dict | None:
        with self._lock:
            rec = self._records.get(sat_id)
            return rec.clock.snapshot() if rec else None

    def link_state(self, sat_id: str) -> str | None:
        with self._lock:
            rec = self._records.get(sat_id)
            return rec.state if rec else None

    def device_kind(self, sat_id: str, device: str) -> str:
        with self._lock:
            rec = self._records.get(sat_id)
            d = rec.devices.get(device) if rec else None
            return str((d or {}).get("kind") or "")

    def _hub_device_cfg(self, sat_id: str, device: str) -> dict:
        """satellites.json 里这个设备的枢纽侧配置（role、unit、limits_mm …）。"""
        spec = self._spec(sat_id) or {}
        cfg = (spec.get("devices") or {}).get(device)
        return dict(cfg) if isinstance(cfg, dict) else {}

    def devices(self, kind: str | None = None) -> list[tuple[str, dict]]:
        """``(sat_id, descriptor)``。descriptor 是卫星 hello 里的那份，另加 ``link_state``、
        ``role``（satellites.json 的 ``devices.<id>.role``，没配就是 None）和 ``hub_config``
        （那个设备在 satellites.json 里的整段，例如 ``limits_mm``）。"""
        out: list[tuple[str, dict]] = []
        with self._lock:
            for sid in sorted(self._records):
                rec = self._records[sid]
                for d in rec.devices.values():
                    if kind is None or d.get("kind") == kind:
                        desc = dict(d)
                        desc["link_state"] = rec.state
                        hc_ = self._hub_device_cfg(sid, str(d.get("id") or ""))
                        desc["role"] = hc_.get("role")
                        desc["hub_config"] = hc_
                        out.append((sid, desc))
        return out

    def drop_session(self, sat_id: str, code: int = 4000, reason: str = "dropped") -> bool:
        """主动断开一颗卫星的当前连接（测试与运维用）。卫星按退避重连、同一 boot_id 续上。
        返回是否真断了一条连接。"""
        with self._lock:
            rec = self._records.get(sat_id)
            s = rec.session if rec else None
        if s is None:
            return False
        s.transport.close(int(code), str(reason)[:100])
        return True

    def _device_view(self, rec: _Record, desc: dict) -> dict:
        d = dict(desc)
        hc_ = self._hub_device_cfg(rec.sat_id, str(desc.get("id") or ""))
        d["role"] = hc_.get("role")
        d["hub_config"] = hc_
        lv = rec.latest.get((desc.get("id", ""), "device_state"))
        d["latest"] = dict(lv["data"]) if lv and isinstance(lv.get("data"), dict) else None
        d["latest_ts_ms"] = lv.get("ts_ms") if lv else None
        # 陈旧 = 卫星重启后还没来新 device_state，或者链路此刻不是 connected（断线 / degraded 时
        # 看到的只是「最后已知」）。hub.latest() 只在前一种情况下藏数据：断线期间同一 boot 的
        # 最后读数仍可能有用，由调用方结合 link_state 决定信不信。
        d["stale"] = bool(lv.get("stale")) if lv else True
        if rec.state != LINK_CONNECTED:
            d["stale"] = True
        ax = rec.latest.get((desc.get("id", ""), "axis_state"))
        d["axis"] = dict(ax["data"]) if ax and isinstance(ax.get("data"), dict) else None
        d.setdefault("operator_hold", bool((d.get("latest") or {}).get("operator_hold", False))
                     or d.get("state") == "operator_hold")
        return d

    def _sat_view(self, rec: _Record) -> dict:
        spec = self._spec(rec.sat_id)
        clk = rec.clock.snapshot()
        return {
            "id": rec.sat_id,
            "configured": spec is not None,
            "enabled": bool(spec.get("enabled", True)) if spec else False,
            "note": (spec or {}).get("note", ""),
            "state": rec.state,
            "ever_connected": rec.ever_connected,
            "boot_id": rec.boot_id,
            "connected_since": rec.connected_since_ms if rec.session else None,
            "disconnected_ms": rec.disconnected_ms,
            "last_seen_ms": rec.last_seen_ms,
            "peer": rec.session.peer if rec.session else "",
            "sw": rec.hello.get("sw", ""),
            "host": rec.hello.get("host", ""),
            "proto": rec.hello.get("proto"),
            "rtt_ms": clk["rtt_ms"],
            "clock_offset_ms": clk["offset_ms"],
            "clock_delay_ms": clk["delay_ms"],
            "clock_samples": clk["samples"],
            "evt_landed": rec.landed_upto,
            "pending_commands": len(rec.pending),
            "counters": dict(rec.counters),
            "last_estop": rec.last_estop,
            "devices": [self._device_view(rec, d) for d in rec.devices.values()],
        }

    def list_satellites(self) -> list[dict]:
        self._load_config()
        with self._lock:
            ids = sorted(set(self._records) | set(self.configured_ids()))
            out = []
            for sid in ids:
                rec = self._record(sid)
                if self._spec(sid) is None and not rec.ever_connected:
                    continue
                out.append(self._sat_view(rec))
            return out

    def satellite(self, sat_id: str) -> dict | None:
        with self._lock:
            rec = self._records.get(sat_id)
            if rec is None or (self._spec(sat_id) is None and not rec.ever_connected):
                return None
            view = self._sat_view(rec)
            view["pending"] = [c.summary() for c in rec.pending.values()]
            return view

    def recent_events(self, sat_id: str, *, limit: int = 100, kind: str | None = None,
                      device: str | None = None) -> list[dict]:
        with self._lock:
            rec = self._records.get(sat_id)
            if rec is None:
                return []
            items = list(rec.recent)
        if kind:
            items = [e for e in items if e.get("kind") == kind]
        if device:
            items = [e for e in items if e.get("device") == device]
        return list(reversed(items))[: max(1, int(limit))]

    def status(self) -> dict:
        with self._lock:
            connected = sum(1 for r in self._records.values() if r.session is not None)
        return {
            "started": self._started,
            "enabled": self.hub_enabled(),
            "hub_boot_id": self.hub_boot_id,
            "config_path": str(self.config_path()),
            "config_error": self._cfg_error,
            "configured": len(self.configured_ids()),
            "connected": connected,
            "data_root": str(self.data_root()),
            "stm_ring": self.stm_ring.coverage(),
            "live": self.bridge.status(),
            "dispatch_dropped": self._dispatch_dropped,
        }

    # ── 监听 ──────────────────────────────────────────────────────────────
    def add_listener(self, fn: Callable[[str, dict], None]) -> int:
        """``fn(sat_id, evt)`` 在分发线程上同步调用，只许入队。返回句柄。

        ``evt`` 是卫星事件加上枢纽补的字段：``{sat_id, boot_id, seq, device, kind, ts_ms,
        ts_hub_ms, clock_offset_ms, clock_delay_ms, rx_ms, data}``。枢纽自己产生的事件
        （链路状态、重启、急停、急停回执、文件入库）``seq=0``、``origin="hub"``，``kind`` 是
        ``link`` / ``restart`` / ``estop`` / ``estop_ack`` / ``file``。
        """
        with self._lock:
            h = next(self._listener_ids)
            self._listeners[h] = fn
            return h

    def remove_listener(self, handle: int) -> None:
        with self._lock:
            self._listeners.pop(handle, None)

    # ── 内部：链路事件、分发 ──────────────────────────────────────────────
    def _emit_link(self, sat_id: str, prev: str, new: str, reason: str) -> None:
        if prev == new:
            return
        logger.info("卫星 %s 链路 %s → %s（%s）", sat_id, prev, new, reason)
        self._enqueue(("hub_evt", sat_id, {"kind": "link", "state": new, "prev": prev,
                                           "reason": reason}))

    def _enqueue(self, item: tuple) -> None:
        try:
            self._dispatch_q.put_nowait(item)
        except queue.Full:
            self._dispatch_dropped += 1
            if self._dispatch_dropped in (1, 100) or self._dispatch_dropped % 10000 == 0:
                logger.warning("卫星事件分发队列满，已丢 %d 条（监听者太慢？）",
                               self._dispatch_dropped)

    def _dispatch_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                item = self._dispatch_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._dispatch_one(item)
            except Exception:  # noqa: BLE001 — 分发线程绝不能死
                logger.warning("卫星事件分发出错", exc_info=True)

    def drain(self, timeout_s: float = 2.0) -> bool:
        """测试用：等分发队列清空（不含正在处理的那一条之后的回调完成保证）。"""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._dispatch_q.empty():
                time.sleep(0.05)
                if self._dispatch_q.empty():
                    return True
            time.sleep(0.01)
        return False

    def _dispatch_one(self, item: tuple) -> None:
        kind = item[0]
        if kind == "evt":
            _, sat_id, evt = item
            self._call_listeners(sat_id, evt)
            self.bridge.on_event(sat_id, evt)
        elif kind == "hub_evt":
            _, sat_id, data = item
            evt = {"sat_id": sat_id, "origin": "hub", "seq": 0, "device": data.get("device", ""),
                   "kind": data.get("kind", ""), "ts_ms": now_ms(), "ts_hub_ms": float(now_ms()),
                   "data": data}
            self._call_listeners(sat_id, evt)
            self.bridge.on_hub_event(sat_id, data)
            self._publish_bus(sat_id, data)
        elif kind == "bus_only":
            # 镜像卫星自己的事件（deadman / error / 设备状态跳变 / 人工操作）：监听者已经从
            # 原事件拿到了，这里只发 EventBus 指针，免得监听者收到两份同 kind 的事件。
            _, sat_id, data = item
            self._publish_bus(sat_id, data)
        elif kind == "health":
            _, sat_id, snap = item
            self.bridge.on_health(sat_id, snap, self.link_state(sat_id))
        elif kind == "call":
            item[1]()

    def _call_listeners(self, sat_id: str, evt: dict) -> None:
        with self._lock:
            fns = list(self._listeners.values())
        for fn in fns:
            try:
                fn(sat_id, evt)
            except Exception:  # noqa: BLE001
                logger.debug("satellite listener failed", exc_info=True)

    def _publish_bus(self, sat_id: str, data: dict) -> None:
        """EventBus 只发指针式的状态变化（契约 §5.3 / 陷阱 12）。"""
        try:
            from mast.core.events import Event, EventBus, EventType

            bus = self._bus or EventBus.get()
            payload = {"sat_id": sat_id, **{k: v for k, v in data.items()
                                            if isinstance(v, (str, int, float, bool, type(None), list))}}
            bus.publish(Event(type=EventType.SATELLITE, data=payload))
        except Exception:  # noqa: BLE001
            logger.debug("EventBus SATELLITE publish failed", exc_info=True)

    # ── 监视线程 ──────────────────────────────────────────────────────────
    def _monitor_loop(self) -> None:
        while not self._stopping.wait(MONITOR_TICK_S):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.warning("SatelliteHub monitor tick 出错", exc_info=True)

    def tick(self, now: float | None = None) -> None:
        """一次监视：活性、心跳、累计确认、命令到期、配置与总开关、持久化。测试可手动调。"""
        now = time.monotonic() if now is None else now
        if now - self._cfg_checked_mono >= 2.0:
            self._cfg_checked_mono = now
            self._load_config()
        hub_on = self.hub_enabled()
        transitions: list[tuple[str, str, str, str]] = []
        expired: list[PendingCommand] = []
        with self._lock:
            for rec in self._records.values():
                s = rec.session
                if s is not None:
                    spec = self._spec(rec.sat_id)
                    if not hub_on or spec is None or not spec.get("enabled", True):
                        why = "hub disabled" if not hub_on else "satellite disabled"
                        s.transport.close(P.CLOSE_FORBIDDEN, why)
                        continue
                    age = now - rec.last_alive_mono
                    if age > self.lost_after_s:
                        s.transport.close(P.CLOSE_LIVENESS, f"no pong for {age:.1f} s")
                        rec.session = None
                        rec.disconnected_ms = now_ms()
                        transitions.append((rec.sat_id, rec.state, LINK_LOST,
                                            f"no pong for {age:.1f} s"))
                        rec.state = LINK_LOST
                        continue
                    new_state = LINK_CONNECTED if age <= self.degraded_after_s else LINK_DEGRADED
                    if new_state != rec.state:
                        transitions.append((rec.sat_id, rec.state, new_state,
                                            f"last pong {age:.1f} s ago"))
                        rec.state = new_state
                    hb = float((spec or {}).get("heartbeat_s", 1.0))
                    if now >= rec.next_ping_mono:
                        rec.next_ping_mono = now + hb
                        rec.ping_n += 1
                        n = rec.ping_n
                        s.transport.send_lazy(lambda _r=rec, _n=n: self._make_ping(_r, _n))
                    if rec.landed_upto != rec.acked_upto and now - rec.last_ack_mono >= ACK_INTERVAL_S:
                        rec.acked_upto = rec.landed_upto
                        rec.last_ack_mono = now
                        s.transport.send({"t": "evt_ack", "upto": int(rec.landed_upto)})
                for c in list(rec.pending.values()):
                    if not c.done and now >= c.deadline_mono:
                        expired.append(c)
        for sat_id, prev, new, why in transitions:
            self._emit_link(sat_id, prev, new, why)
        for c in expired:
            self._expire(c)
        self._save_state()

    def _make_ping(self, rec: _Record, n: int) -> dict:
        t0 = now_ms()
        with self._lock:
            rec.pings[n] = t0
            if len(rec.pings) > 64:
                for k in sorted(rec.pings)[:-64]:
                    rec.pings.pop(k, None)
        return {"t": "ping", "n": n, "t0": t0}

    # ── 持久化（每颗卫星最近的 boot_id 与已落地事件游标） ──────────────────
    def _load_persisted_state(self) -> None:
        p = self._state_path()
        try:
            raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (OSError, ValueError) as exc:
            logger.warning("卫星枢纽状态文件读不了（当作没有）：%s", exc)
            raw = {}
        with self._lock:
            for sid, v in (raw.get("satellites") or {}).items():
                if not isinstance(v, dict):
                    continue
                rec = self._record(str(sid))
                rec.persisted_boot_id = v.get("boot_id") or None
                try:
                    rec.persisted_evt_ack = max(0, int(v.get("evt_ack") or 0))
                except (TypeError, ValueError):
                    rec.persisted_evt_ack = 0

    def _save_state(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (not self._state_dirty or now - self._state_saved_mono < 1.0):
            return
        with self._lock:
            data = {"hub_boot_id": self.hub_boot_id, "saved_ms": now_ms(), "satellites": {}}
            for sid, rec in self._records.items():
                boot = rec.boot_id or rec.persisted_boot_id
                if not boot:
                    continue
                ack = rec.landed_upto if rec.boot_id else rec.persisted_evt_ack
                data["satellites"][sid] = {"boot_id": boot, "evt_ack": int(ack),
                                           "last_seen_ms": rec.last_seen_ms}
            self._state_dirty = False
            self._state_saved_mono = now
        if not data["satellites"]:
            return
        try:
            p = self._state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as exc:
            logger.debug("卫星枢纽状态写盘失败：%s", exc)


def _summary(evt: dict) -> dict:
    """最近事件列表里存的摘要：不带数组。"""
    data = evt.get("data") or {}
    small: dict[str, Any] = {}
    for k, v in data.items():
        if k.endswith("_b64"):
            small[k] = f"<{len(str(v))} b64 chars>"
        elif isinstance(v, (str, int, float, bool, type(None))):
            small[k] = v if not isinstance(v, str) else v[:200]
        elif isinstance(v, dict) and len(json.dumps(v, default=str)) <= 400:
            small[k] = v
        elif isinstance(v, list) and len(v) <= 16:
            small[k] = v
    return {"seq": evt.get("seq"), "device": evt.get("device"), "kind": evt.get("kind"),
            "ts_ms": evt.get("ts_ms"), "ts_hub_ms": evt.get("ts_hub_ms"), "data": small}


# ── 进程级单例 ──────────────────────────────────────────────────────────────

_hub: SatelliteHub | None = None
_hub_lock = threading.Lock()


def get_satellite_hub() -> SatelliteHub:
    """进程级单例；没有配置也能构造（空）。不会自动起线程：服务启动时
    ``CoreRuntime`` 调 ``start(runtime)``，端点在第一次连接时调 ``ensure_started()``。"""
    global _hub
    if _hub is None:
        with _hub_lock:
            if _hub is None:
                _hub = SatelliteHub()
    return _hub


def peek_satellite_hub() -> SatelliteHub | None:
    """已有的单例，没有就返回 None（不顺手造一个 —— 关停路径上不该为了「没有卫星」去建目录）。"""
    return _hub


def reset_satellite_hub(new: SatelliteHub | None = None) -> SatelliteHub | None:
    """换掉单例（测试用）。返回旧的那个（不替你关它）。"""
    global _hub
    with _hub_lock:
        old, _hub = _hub, new
    return old


__all__ = ["SatelliteHub", "PendingCommand", "get_satellite_hub", "peek_satellite_hub",
           "reset_satellite_hub", "LINK_CONNECTED", "LINK_DEGRADED", "LINK_LOST",
           "DEGRADED_AFTER_S", "LOST_AFTER_S", "SHUTDOWN_SAFE_WAIT_S"]
