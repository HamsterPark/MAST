"""Python 参考卫星：卫星协议 v1 的客户端（契约 §5），C# 卫星的行为参照。

它既是测试替身，也是前端演示的数据源。全部跑在一个 asyncio 循环里：

- **连接**：``hello`` → ``welcome``；断线按退避重连，``boot_id`` 在一个进程内不变。
- **事件**：``seq`` 在同一 ``boot_id`` 内从 1 单调递增；未确认的放在有界缓冲（默认 4000 条 /
  32 MiB，满了丢最旧的），重连后从 ``welcome.evt_ack + 1`` 重发。
- **命令**：``request_id → 状态/结果`` 写 JSONL journal；重复的 ``cmd``：还在跑回
  ``duplicate_running``，跑完了回 ``duplicate_done`` 并把缓存的结果再发一遍。拒收时先回
  ``ack{rejected, reason=<错误码>}``，再回 ``result{ok:false}``（并记进 journal）。
- **死人开关**：连续 ``deadman_s`` 收不到 ping 且有枢纽发起的运动/采集在进行 → 各设备 ``safe``，
  发 ``evt kind=deadman``（断线时进缓冲，重连后上报）。
- **人工接管**：``set_operator_hold(dev, True)`` 后该设备只放行 stop 与只读动作。
- **急停**：``estop`` → 全部设备 ``stop``（含人工发起的采集）→ ``estop_ack``。
- **文件**：文件监视器把稳定的文件交给上传器：``file_offer`` → 等 ``file_want`` → 限速发块 →
  ``file_end`` → 等 ``file_done``；断线后重新 offer，从枢纽给的 offset 续传。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from mast.satellite import protocol as P
from mast.satellite.sim.devices import (
    DEFAULT_DEVICES,
    DEVICE_CLASSES,
    ActionCtx,
    DeviceError,
    SimDevice,
    SimSpectrometer,
)
from mast.satellite.sim.journal import Journal

logger = logging.getLogger("mast.satellite.sim")

SW_VERSION = "mast-sim-satellite/1"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def now_ms() -> int:
    return int(time.time() * 1000)


class AuthRejected(Exception):
    pass


@dataclass
class SimConfig:
    hub_url: str
    satellite_id: str
    token: str
    state_dir: Path
    watch_dir: Path | None = None
    devices: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DEVICES))
    device_opts: dict[str, dict] = field(default_factory=dict)
    spki_sha256: str | None = None
    allow_plain_loopback: bool = False
    chunk_bytes: int = 256 * 1024
    upload_bytes_per_s: int | None = None        # None = 用 welcome 给的
    buffer_max_events: int = 4000
    buffer_max_bytes: int = 32 << 20
    reconnect_delays: tuple[float, ...] = (0.2, 0.5, 1.0, 2.0, 5.0)
    auth_retry_s: float = 10.0
    default_deadman_s: float = 5.0
    speed: float = 1.0
    manual_acq_every_s: float | None = None
    hold: tuple[str, ...] = ()
    backfill_hours: float = 24.0
    sw: str = SW_VERSION


@dataclass
class _Upload:
    path: Path
    data: bytes
    sha: str
    rel_path: str
    kind: str
    mtime_ms: int
    meta: dict
    on_done: Callable[[str, int], None] | None = None
    attempts: int = 0
    not_before: float = 0.0


class Uploader:
    """一次传一个文件；断线就停，重连后从头 offer（枢纽据 .part 告诉我们从哪续）。"""

    def __init__(self, sat: "SimSatellite", manifest_path: Path | None) -> None:
        self.sat = sat
        self.manifest_path = manifest_path
        self.manifest: dict[str, dict] = {}
        if manifest_path is not None and manifest_path.exists():
            try:
                self.manifest = json.loads(manifest_path.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                self.manifest = {}
        self.queue: deque[_Upload] = deque()
        self._wakeup: asyncio.Event | None = None
        self._want: asyncio.Queue | None = None
        self._done: asyncio.Queue | None = None
        self.current: _Upload | None = None
        self.corrupt_next = False                # 测试钩子：下一次上传把第一块改坏一个字节
        self.chunks_sent = 0
        self.bytes_sent = 0
        self.completed: list[str] = []
        self.failures: list[str] = []
        self.pause_after_bytes: int | None = None  # 测试钩子：发够这么多字节就断开（测续传）

    def _ensure_async(self) -> None:
        if self._wakeup is None:
            self._wakeup = asyncio.Event()
            self._want = asyncio.Queue()
            self._done = asyncio.Queue()

    def enqueue(self, *, path, data, sha, rel_path, kind, mtime_ms, meta, on_done=None) -> bool:
        if sha in self.manifest or any(u.sha == sha for u in self.queue):
            return False
        self.queue.append(_Upload(Path(path), data, sha, rel_path, kind, mtime_ms, meta, on_done))
        self._ensure_async()
        self._wakeup.set()
        return True

    def on_want(self, msg: P.FileWant) -> None:
        self._ensure_async()
        self._want.put_nowait(msg)

    def on_done(self, msg: P.FileDone) -> None:
        self._ensure_async()
        self._done.put_nowait(msg)

    def kick(self) -> None:
        self._ensure_async()
        self._wakeup.set()

    def _save_manifest(self) -> None:
        if self.manifest_path is None:
            return
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_name(self.manifest_path.name + ".tmp")
        tmp.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.manifest_path)

    async def run(self) -> None:
        self._ensure_async()
        while True:
            if not self.queue or not self.sat.is_connected():
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            u = self.queue[0]
            if time.monotonic() < u.not_before:
                await asyncio.sleep(min(1.0, u.not_before - time.monotonic()))
                continue
            self.current = u
            try:
                ok, err = await self._upload(u)
            except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                ok, err = False, f"transport: {exc}"
            finally:
                self.current = None
            if ok:
                self.queue.popleft()
                self.manifest[u.sha] = {"rel_path": u.rel_path, "size": len(u.data),
                                        "uploaded_ms": now_ms()}
                self._save_manifest()
                self.completed.append(u.rel_path)
                if u.on_done:
                    u.on_done(u.path.name, len(u.data))
                continue
            u.attempts += 1
            self.failures.append(f"{u.rel_path}: {err}")
            if err and err.startswith("transport"):
                u.not_before = time.monotonic() + 0.5       # 等重连
            elif u.attempts >= 5:
                logger.warning("文件 %s 连续 %d 次上传失败，放弃：%s", u.rel_path, u.attempts, err)
                self.queue.popleft()
            else:
                u.not_before = time.monotonic() + min(30.0, 0.5 * 2 ** u.attempts)

    async def _next(self, q: asyncio.Queue, sha: str, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise asyncio.TimeoutError("等枢纽回复超时")
            if not self.sat.is_connected():
                raise ConnectionError("disconnected")
            try:
                msg = await asyncio.wait_for(q.get(), timeout=min(left, 0.5))
            except asyncio.TimeoutError:
                continue
            if msg.file_id == sha:
                return msg

    def _drain(self) -> None:
        for q in (self._want, self._done):
            while not q.empty():
                q.get_nowait()

    async def _upload(self, u: _Upload) -> tuple[bool, str | None]:
        self._drain()
        sat = self.sat
        await sat.send({"t": "file_offer", "file_id": u.sha, "name": u.path.name,
                        "rel_path": u.rel_path, "size": len(u.data), "sha256": u.sha,
                        "mtime_ms": u.mtime_ms, "kind": u.kind, "meta": u.meta})
        want = await self._next(self._want, u.sha, 15.0)
        offset = int(want.offset)
        size = len(u.data)
        if offset >= size:
            # 枢纽已经有了（或刚替我们收尾）：等它的 file_done。
            done = await self._next(self._done, u.sha, 30.0)
            return bool(done.ok), done.error
        corrupt = self.corrupt_next
        self.corrupt_next = False
        while offset < size:
            # 中途枢纽可能回 file_want 重新对齐，或直接回 file_done(失败)。
            if not self._want.empty():
                w = self._want.get_nowait()
                if w.file_id == u.sha:
                    offset = int(w.offset)
                    continue
            if not self._done.empty():
                d = self._done.get_nowait()
                if d.file_id == u.sha:
                    return bool(d.ok), d.error
            chunk = u.data[offset: offset + sat.cfg.chunk_bytes]
            if corrupt:
                chunk = bytes([chunk[0] ^ 0xFF]) + chunk[1:]
                corrupt = False
            import base64

            await sat.send({"t": "file_chunk", "file_id": u.sha, "offset": offset,
                            "data_b64": base64.b64encode(chunk).decode("ascii")})
            offset += len(chunk)
            self.chunks_sent += 1
            self.bytes_sent += len(chunk)
            rate = sat.upload_rate()
            if rate:
                await asyncio.sleep(len(chunk) / rate)
            if self.pause_after_bytes is not None and self.bytes_sent >= self.pause_after_bytes:
                self.pause_after_bytes = None
                await sat.drop_connection("test: pause_after_bytes")
                raise ConnectionError("test drop")
        await sat.send({"t": "file_end", "file_id": u.sha})
        while True:
            msg = await self._next_any(u.sha, 60.0)
            if isinstance(msg, P.FileWant):
                # 枢纽说还差字节（块丢了）：从它给的位置补发。
                return await self._resume_from(u, int(msg.offset))
            return bool(msg.ok), msg.error

    async def _next_any(self, sha: str, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                raise asyncio.TimeoutError("等 file_done 超时")
            if not self.sat.is_connected():
                raise ConnectionError("disconnected")
            for q in (self._done, self._want):
                if not q.empty():
                    m = q.get_nowait()
                    if m.file_id == sha:
                        return m
            await asyncio.sleep(0.05)

    async def _resume_from(self, u: _Upload, offset: int) -> tuple[bool, str | None]:
        import base64

        size = len(u.data)
        while offset < size:
            chunk = u.data[offset: offset + self.sat.cfg.chunk_bytes]
            await self.sat.send({"t": "file_chunk", "file_id": u.sha, "offset": offset,
                                 "data_b64": base64.b64encode(chunk).decode("ascii")})
            offset += len(chunk)
        await self.sat.send({"t": "file_end", "file_id": u.sha})
        done = await self._next(self._done, u.sha, 60.0)
        return bool(done.ok), done.error


class SimSatellite:
    def __init__(self, cfg: SimConfig) -> None:
        self.cfg = cfg
        self.boot_id = uuid.uuid4().hex
        self.evt_seq = 0
        self._buf: deque[tuple[int, str, int]] = deque()
        self._buf_bytes = 0
        self.dropped_events = 0
        self._next_send_seq = 1
        self.cfg.state_dir = Path(cfg.state_dir)
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.cfg.state_dir / "journal.jsonl")
        self.uploader = Uploader(self, self.cfg.state_dir / "uploads.json")
        self._file_meta: dict[str, dict] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ws = None
        self._connected = False
        self._conn_event: asyncio.Event | None = None
        self._evt_wakeup: asyncio.Event | None = None
        self._send_lock: asyncio.Lock | None = None
        self._stopping = False
        self._tasks: list[asyncio.Task] = []
        self._running: dict[str, asyncio.Task] = {}
        self.welcome: P.Welcome | None = None
        self._last_ping_mono: float | None = None
        self.deadman_trips = 0
        self._deadman_tripped = False
        self.connects = 0
        self.last_close: str | None = None
        self.received: deque[dict] = deque(maxlen=2000)     # 测试/调试用：收到的消息
        self.ping_paused_until = 0.0
        self.devices: dict[str, SimDevice] = {}
        for dev_id, kind in cfg.devices.items():
            cls = DEVICE_CLASSES[kind]
            opts = dict(cfg.device_opts.get(dev_id) or {})
            if kind == "file_watcher":
                opts.setdefault("root", str(cfg.watch_dir) if cfg.watch_dir else None)
                opts.setdefault("backfill_hours", cfg.backfill_hours)
            if kind == "spectrometer" and cfg.watch_dir:
                opts.setdefault("watch_dir", str(cfg.watch_dir))
            self.devices[dev_id] = cls(self, dev_id, speed=cfg.speed, **opts)
        for d in cfg.hold:
            if d in self.devices:
                self.devices[d].operator_hold = True

    # ── 事件缓冲 ──
    def emit(self, device: str, kind: str, data: dict) -> int:
        self.evt_seq += 1
        seq = self.evt_seq
        text = P.encode({"t": "evt", "seq": seq, "device": device, "kind": kind,
                         "ts_ms": now_ms(), "data": data})
        if P.frame_too_big(text):
            logger.error("事件 %s/%s 超过 1 MiB，丢弃", device, kind)
            return seq
        n = len(text)
        self._buf.append((seq, text, n))
        self._buf_bytes += n
        while (len(self._buf) > self.cfg.buffer_max_events
               or self._buf_bytes > self.cfg.buffer_max_bytes):
            _s, _t, sz = self._buf.popleft()
            self._buf_bytes -= sz
            self.dropped_events += 1
        if self._evt_wakeup is not None:
            self._evt_wakeup.set()
        return seq

    def _apply_evt_ack(self, upto: int) -> None:
        while self._buf and self._buf[0][0] <= upto:
            _s, _t, sz = self._buf.popleft()
            self._buf_bytes -= sz

    @property
    def unacked(self) -> int:
        return len(self._buf)

    def note_file_meta(self, path: Path, meta: dict) -> None:
        self._file_meta[str(Path(path))] = meta

    def file_meta_for(self, path: Path) -> dict | None:
        return self._file_meta.pop(str(Path(path)), None)

    # ── 连接 ──
    def is_connected(self) -> bool:
        return self._connected

    def upload_rate(self) -> int | None:
        if self.cfg.upload_bytes_per_s:
            return int(self.cfg.upload_bytes_per_s)
        if self.welcome is not None:
            return int(self.welcome.config.max_upload_bytes_per_s)
        return None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.cfg.token}",
                P.HDR_ID: self.cfg.satellite_id, P.HDR_PROTO: str(P.PROTO_VERSION)}

    async def _ssl_context(self):
        u = urlsplit(self.cfg.hub_url)
        if u.scheme == "ws":
            if not self.cfg.allow_plain_loopback or (u.hostname or "") not in _LOOPBACK:
                raise PermissionError("明文 ws:// 只允许 127.0.0.1，且要显式给 --allow-plain-loopback")
            return None
        if u.scheme != "wss":
            raise ValueError(f"hub 地址必须是 wss:// 或 ws://：{self.cfg.hub_url}")
        pin = (self.cfg.spki_sha256 or "").strip().lower().replace(":", "")
        if not pin:
            raise PermissionError("没配 hub.spki_sha256 钉值，拒绝连接（契约 §5.1）")
        from mast.satellite.tls import pinned_context, probe_server_cert, spki_sha256

        der = await asyncio.to_thread(probe_server_cert, u.hostname, u.port or 443)
        got = spki_sha256(der)
        if got != pin:
            raise PermissionError(f"枢纽证书公钥钉值不符：期望 {pin[:16]}…，实际 {got[:16]}…")
        return pinned_context(der)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._conn_event = asyncio.Event()
        self._evt_wakeup = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self.uploader._ensure_async()
        for d in self.devices.values():
            self._tasks.append(asyncio.create_task(d.background(), name=f"dev-{d.id}"))
        self._tasks.append(asyncio.create_task(self.uploader.run(), name="uploader"))
        self._tasks.append(asyncio.create_task(self._deadman_loop(), name="deadman"))
        if self.cfg.manual_acq_every_s:
            self._tasks.append(asyncio.create_task(self._manual_loop(), name="manual"))
        attempt = 0
        try:
            while not self._stopping:
                try:
                    await self._session()
                    attempt = 0
                except AuthRejected as exc:
                    logger.error("枢纽拒绝认证：%s（%.0f s 后重试）", exc, self.cfg.auth_retry_s)
                    await asyncio.sleep(self.cfg.auth_retry_s)
                    continue
                except PermissionError as exc:
                    logger.error("不连接：%s", exc)
                    await asyncio.sleep(self.cfg.auth_retry_s)
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.last_close = f"{type(exc).__name__}: {exc}"
                    logger.info("连接结束：%s", self.last_close)
                if self._stopping:
                    break
                delay = self.cfg.reconnect_delays[min(attempt, len(self.cfg.reconnect_delays) - 1)]
                attempt += 1
                await asyncio.sleep(delay)
        finally:
            for t in self._tasks:
                t.cancel()
            for t in list(self._running.values()):
                t.cancel()

    async def _session(self) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus

        ssl_ctx = await self._ssl_context()
        try:
            ws = await connect(self.cfg.hub_url, additional_headers=self._headers(), ssl=ssl_ctx,
                               max_size=P.MAX_FRAME_BYTES + 4096, open_timeout=10,
                               ping_interval=20, ping_timeout=20, close_timeout=2,
                               compression=None)
        except InvalidStatus as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code in (401, 403):
                raise AuthRejected(f"HTTP {code}") from exc
            raise
        try:
            await self._handshake(ws)
            reader = asyncio.create_task(self._reader(ws), name="reader")
            sender = asyncio.create_task(self._event_sender(ws), name="evt-sender")
            done, pending = await asyncio.wait({reader, sender}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception() if not t.cancelled() else None
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    raise exc
        finally:
            self._connected = False
            self._ws = None
            if self._conn_event is not None:
                self._conn_event.clear()
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            rc = getattr(ws, "close_code", None)
            self.last_close = f"code={rc} reason={getattr(ws, 'close_reason', '')}"

    async def _handshake(self, ws) -> None:
        hello = {"t": "hello", "proto": P.PROTO_VERSION, "satellite_id": self.cfg.satellite_id,
                 "boot_id": self.boot_id, "sw": self.cfg.sw, "host": socket.gethostname(),
                 "time_ms": now_ms(), "devices": [d.descriptor() for d in self.devices.values()],
                 "evt_seq": self.evt_seq}
        await ws.send(P.encode(hello))
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        msg = P.parse_to_satellite(raw)
        if not isinstance(msg, P.Welcome):
            raise ConnectionError(f"第一条回复不是 welcome：{getattr(msg, 't', '?')}")
        self.welcome = msg
        self._ws = ws
        self._apply_evt_ack(msg.evt_ack)
        self._next_send_seq = (self._buf[0][0] if self._buf else self.evt_seq + 1)
        self._next_send_seq = max(self._next_send_seq, msg.evt_ack + 1)
        self._last_ping_mono = time.monotonic()
        self._connected = True
        self.connects += 1
        self._conn_event.set()
        self.uploader.kick()
        for d in self.devices.values():
            d.emit_state(force=True)
        logger.info("已连上枢纽 %s（session=%s evt_ack=%d 待重发 %d 条）", self.cfg.hub_url,
                    msg.session, msg.evt_ack, len(self._buf))

    async def send(self, obj: dict) -> None:
        ws = self._ws
        if ws is None or not self._connected:
            raise ConnectionError("not connected")
        text = P.encode(obj)
        async with self._send_lock:
            await ws.send(text)

    async def _send_quiet(self, obj: dict) -> bool:
        try:
            await self.send(obj)
            return True
        except Exception:  # noqa: BLE001 — 断线时结果留在 journal 里，靠 cmd_status 找回
            return False

    async def drop_connection(self, reason: str = "") -> None:
        ws = self._ws
        if ws is not None:
            try:
                await ws.close(code=4000, reason=reason[:100])
            except Exception:  # noqa: BLE001
                pass

    async def _event_sender(self, ws) -> None:
        while True:
            item = None
            if self._buf:
                idx = self._next_send_seq - self._buf[0][0]
                if idx < 0:
                    self._next_send_seq = self._buf[0][0]
                    idx = 0
                if idx < len(self._buf):
                    item = self._buf[idx]
            if item is None:
                self._evt_wakeup.clear()
                if self._buf and self._next_send_seq - self._buf[0][0] < len(self._buf):
                    continue
                await self._evt_wakeup.wait()
                continue
            seq, text, _n = item
            async with self._send_lock:
                await ws.send(text)
            self._next_send_seq = seq + 1

    async def _reader(self, ws) -> None:
        async for raw in ws:
            rx = now_ms()
            try:
                msg = P.parse_to_satellite(raw)
            except P.ProtocolError as exc:
                logger.warning("枢纽发来不合协议的消息：%s", exc)
                continue
            self.received.append({"t": getattr(msg, "t", "?"), "rx_ms": rx})
            if isinstance(msg, P.Ping):
                if time.monotonic() < self.ping_paused_until:
                    continue                    # 测试钩子：假装没收到 ping
                self._last_ping_mono = time.monotonic()
                n, t0, t1 = msg.n, msg.t0, rx
                async with self._send_lock:
                    await ws.send(P.encode({"t": "pong", "n": n, "t0": t0, "t1": t1,
                                            "t2": now_ms()}))
            elif isinstance(msg, P.Cmd):
                self._on_cmd(msg)
            elif isinstance(msg, P.Cancel):
                t = self._running.get(msg.id)
                if t is not None and not t.done():
                    t.cancel()
            elif isinstance(msg, P.EvtAckMsg):
                self._apply_evt_ack(msg.upto)
            elif isinstance(msg, P.FileWant):
                self.uploader.on_want(msg)
            elif isinstance(msg, P.FileDone):
                self.uploader.on_done(msg)
            elif isinstance(msg, P.CmdStatus):
                await self._send_quiet({"t": "cmd_status_reply",
                                        "items": [self._status_item(i) for i in msg.ids]})
            elif isinstance(msg, P.Estop):
                asyncio.create_task(self._on_estop(msg))

    def _status_item(self, cid: str) -> dict:
        row = self.journal.get(cid)
        if row is None:
            return {"id": cid, "state": "unknown"}
        st = row.get("state")
        if st == "running":
            return {"id": cid, "state": "running"}
        if st == "lost_on_restart":
            return {"id": cid, "state": "lost_on_restart"}
        return {"id": cid, "state": "done", "result": row.get("result")}

    # ── 命令 ──
    def _on_cmd(self, cmd: P.Cmd) -> None:
        row = self.journal.get(cmd.id)
        if row is not None:
            st = row.get("state")
            if st == "running":
                asyncio.create_task(self._send_quiet({"t": "ack", "id": cmd.id,
                                                      "state": "duplicate_running"}))
            else:
                res = row.get("result") or {
                    "t": "result", "id": cmd.id, "ok": False,
                    "error": {"code": "lost_on_restart", "message": "卫星重启前的作业，结局未知"}}
                asyncio.create_task(self._send_many([
                    {"t": "ack", "id": cmd.id, "state": "duplicate_done"}, res]))
            return
        dev = self.devices.get(cmd.device)
        try:
            if dev is None:
                raise DeviceError("invalid_args", f"没有设备 {cmd.device!r}")
            dev.check(cmd.action, cmd.args)
        except DeviceError as exc:
            res = {"t": "result", "id": cmd.id, "ok": False,
                   "error": {"code": exc.code, "message": exc.message},
                   "started_ms": now_ms(), "finished_ms": now_ms()}
            self.journal.put(cmd.id, device=cmd.device, action=cmd.action, state="done", result=res)
            asyncio.create_task(self._send_many([
                {"t": "ack", "id": cmd.id, "state": "rejected", "reason": exc.code}, res]))
            return
        self.journal.put(cmd.id, device=cmd.device, action=cmd.action, state="running")
        task = asyncio.create_task(self._run_cmd(cmd, dev), name=f"cmd-{cmd.id[:8]}")
        self._running[cmd.id] = task
        asyncio.create_task(self._send_quiet({"t": "ack", "id": cmd.id, "state": "accepted"}))

    async def _send_many(self, msgs: list[dict]) -> None:
        for m in msgs:
            await self._send_quiet(m)

    async def _run_cmd(self, cmd: P.Cmd, dev: SimDevice) -> None:
        started = now_ms()
        try:
            value = await asyncio.wait_for(dev.run(cmd.action, cmd.args, ActionCtx(cmd.id, "hub")),
                                           timeout=cmd.timeout_s)
            res = {"t": "result", "id": cmd.id, "ok": True, "value": value}
        except asyncio.TimeoutError:
            res = {"t": "result", "id": cmd.id, "ok": False,
                   "error": {"code": "timeout", "message": f"{cmd.timeout_s} s 内没做完"}}
        except asyncio.CancelledError:
            res = {"t": "result", "id": cmd.id, "ok": False,
                   "error": {"code": "cancelled", "message": "已取消"}}
        except DeviceError as exc:
            res = {"t": "result", "id": cmd.id, "ok": False,
                   "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # noqa: BLE001
            logger.exception("命令 %s 内部错误", cmd.id)
            res = {"t": "result", "id": cmd.id, "ok": False,
                   "error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}"}}
        finally:
            self._running.pop(cmd.id, None)
        res["started_ms"] = started
        res["finished_ms"] = now_ms()
        self.journal.put(cmd.id, state="done", result=res)
        await self._send_quiet(res)

    # ── 死人开关 ──
    def deadman_s(self) -> float:
        if self.welcome is not None:
            return float(self.welcome.config.deadman_s)
        return float(self.cfg.default_deadman_s)

    async def _deadman_loop(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            last = self._last_ping_mono
            if last is None:
                continue
            silent = time.monotonic() - last
            if silent < self.deadman_s():
                self._deadman_tripped = False
                continue
            if self._deadman_tripped:
                continue
            active = [d for d in self.devices.values() if d.hub_active()]
            if not active:
                continue
            self._deadman_tripped = True
            stopped: list[str] = []
            errors: list[dict] = []
            for d in self.devices.values():
                try:
                    stopped += await asyncio.wait_for(d.safe(), timeout=5.0)
                except Exception as exc:  # noqa: BLE001
                    errors.append({"device": d.id, "message": str(exc)})
            # 计数在停完之后才加：看到 trips 加一的人可以认定该停的已经停了（先加的话，读计数
            # 的一方会和正在进行的 stop 赛跑）。
            self.deadman_trips += 1
            logger.warning("死人开关：%.1f s 没收到 ping，停了 %s", silent, stopped)
            self.emit("", "deadman", {"reason": f"{silent:.1f} s 没收到 ping",
                                      "silent_s": round(silent, 2), "deadman_s": self.deadman_s(),
                                      "stopped": stopped, "errors": errors})

    # ── 急停 ──
    async def _on_estop(self, msg: P.Estop) -> None:
        """``stopped`` = 每一个 stop 正常做完的设备（**包括本来就空闲的**）；``errors`` = 停失败的。
        另带 ``active``：当时确实在动 / 在采、被这次停下来的那几个（信息用）。

        不把 ``stopped`` 定义成「被这次急停打断的」：枢纽同时还会给每个设备发一条 ``stop``
        命令，两条路谁先打断作业是竞态，那样 ``stopped`` 就时有时无，操作员看不出急停到底
        覆盖了哪些设备。"""
        stopped: list[str] = []
        active: list[str] = []
        errors: list[dict] = []
        for d in self.devices.values():
            try:
                if await asyncio.wait_for(d.stop(origin="estop"), timeout=5.0):
                    active.append(d.id)
                stopped.append(d.id)
            except Exception as exc:  # noqa: BLE001
                errors.append({"device": d.id, "message": str(exc)})
        for t in list(self._running.values()):
            t.cancel()
        logger.warning("急停（%s）：已停 %s（其中在动的 %s）", msg.reason, stopped, active)
        await self._send_quiet({"t": "estop_ack", "stopped": stopped, "errors": errors,
                                "active": active})

    # ── 人工操作模拟 ──
    async def _manual_loop(self) -> None:
        period = float(self.cfg.manual_acq_every_s or 30.0)
        while True:
            await asyncio.sleep(period)
            for d in self.devices.values():
                if isinstance(d, SimSpectrometer) and not d.busy:
                    self.emit(d.id, "manual_activity", {"what": "LightField 窗口里有人点了采集"})
                    try:
                        await d._run_job("acquire", d.act_acquire(
                            {"exposure_ms": d.settings["exposure_ms"], "frames": 1, "save": True},
                            ActionCtx(None, "manual")), ActionCtx(None, "manual"))
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("manual acquisition failed: %s", exc)

    # ── 线程外的控制（测试与 CLI 用） ──
    def call_threadsafe(self, fn: Callable[[], Any]) -> Any:
        """在卫星的事件循环上同步执行 ``fn`` 并返回结果（从别的线程调）。"""
        if self.loop is None:
            return fn()

        async def _co():
            return fn()
        return asyncio.run_coroutine_threadsafe(_co(), self.loop).result(timeout=10)

    def run_coro_threadsafe(self, coro, timeout: float = 30.0) -> Any:
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def set_operator_hold(self, device: str, on: bool) -> None:
        self.call_threadsafe(lambda: self.devices[device].set_operator_hold(on))

    def stop(self) -> None:
        self._stopping = True


class SimThread:
    """把一个 ``SimSatellite`` 放在后台线程的事件循环里跑（测试用）。"""

    def __init__(self, cfg: SimConfig) -> None:
        self.sat = SimSatellite(cfg)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main: asyncio.Task | None = None
        self._ready = threading.Event()

    def start(self) -> "SimThread":
        def _run():
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            self._main = loop.create_task(self.sat.run())
            self._ready.set()
            try:
                loop.run_until_complete(self._main)
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            finally:
                try:
                    pending = asyncio.all_tasks(loop)
                    for t in pending:
                        t.cancel()
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:  # noqa: BLE001
                    pass
                loop.close()

        self._thread = threading.Thread(target=_run, name=f"sim-{self.sat.cfg.satellite_id}",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self

    def wait_connected(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.sat.is_connected():
                return True
            time.sleep(0.02)
        return False

    def stop(self, timeout: float = 5.0) -> None:
        """模拟进程结束（也用来模拟崩溃：不发任何收尾消息就断开）。"""
        self.sat.stop()
        if self._loop is not None and self._main is not None:
            self._loop.call_soon_threadsafe(self._main.cancel)
        if self._thread is not None:
            self._thread.join(timeout)


__all__ = ["SimConfig", "SimSatellite", "SimThread", "Uploader", "AuthRejected"]
