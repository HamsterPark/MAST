"""卫星枢纽测试的共用件：临时数据根、枢纽实例、进程内 uvicorn（随机端口）、参考卫星。

时间都压短了（心跳 0.2 s、degraded 0.6 s、lost 1.5 s、死人开关 1 s），每个集成测试几秒内跑完。
不连 Nanonis；需要 STM 状态的地方往 EventBus 里发假的 HARDWARE_STATE。
"""

from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[3] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

import pytest  # noqa: E402

from mast.satellite import hub_config as hc  # noqa: E402

FAST = {"heartbeat_s": 0.2, "deadman_s": 1.0}


@pytest.fixture
def sat_root(tmp_path, monkeypatch):
    """临时数据根，布局照安装版：<root>/config、<root>/satellite、<root>/experiments。"""
    root = tmp_path / "data"
    (root / "config").mkdir(parents=True)
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(root))
    monkeypatch.setenv("MAST_EXPERIMENT_ROOT", str(root / "experiments"))
    return root


def write_sat_config(root: Path, sat_id: str = "sim1", *, enabled: bool = True,
                     devices: dict | None = None, **overrides) -> str:
    """在 satellites.json 里登记一颗卫星，返回明文令牌。"""
    p = hc.config_path(root)
    data = hc.load(p)
    token = hc.new_token()
    e = hc.set_token(data, sat_id, token, note="test")
    e.update(FAST)
    e["enabled"] = enabled
    if devices is not None:
        e["devices"] = devices
    e.update(overrides)
    hc.save(p, data)
    return token


class FakeLive:
    """LiveHub 的最小替身：记下 declare / set_axis / publish。"""

    def __init__(self) -> None:
        self.declared: dict[str, dict] = {}
        self.axes: dict[str, list] = {}
        self.frames: dict[str, list] = {}
        self._lock = threading.Lock()

    def declare(self, stream_id, kind, **kw):
        with self._lock:
            self.declared[stream_id] = {"kind": kind, **kw}

    def set_axis(self, stream_id, x, *, unit):
        with self._lock:
            self.axes.setdefault(stream_id, []).append((list(x), unit))
            return len(self.axes[stream_id])

    def publish(self, stream_id, frame):
        with self._lock:
            assert stream_id in self.declared, f"publish before declare: {stream_id}"
            self.frames.setdefault(stream_id, []).append(frame)
            return len(self.frames[stream_id])

    def end(self, stream_id):
        pass


@pytest.fixture
def hub(sat_root):
    from mast.satellite.hub import SatelliteHub, reset_satellite_hub

    live = FakeLive()
    h_enabled = {"on": True}
    h = SatelliteHub(data_root=sat_root, degraded_after_s=0.6, lost_after_s=1.5,
                     hello_timeout_s=3.0, live=live, enabled_fn=lambda: h_enabled["on"])
    h.test_enabled = h_enabled       # 测试里翻总开关
    h.test_live = live
    old = reset_satellite_hub(h)
    h.start()
    yield h
    h.shutdown(wait_s=1.0)
    reset_satellite_hub(old)


@dataclass
class Server:
    port: int
    server: object
    thread: threading.Thread
    extra: dict = field(default_factory=dict)

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws/satellite/v1"

    @property
    def http(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def start_server(app, *, ssl_kw: dict | None = None) -> Server:
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off",
                                           **(ssl_kw or {})))
    th = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    th.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    return Server(port=port, server=server, thread=th)


def stop_server(s: Server) -> None:
    s.server.should_exit = True
    s.thread.join(5)


@pytest.fixture
def server(hub):
    from fastapi import FastAPI

    from mast.api.context import AppContext
    from mast.api.routes import satellites

    app = FastAPI()
    app.state.ctx = AppContext()
    app.include_router(satellites.router)
    s = start_server(app)
    yield s
    stop_server(s)


@pytest.fixture
def sims(tmp_path):
    """造参考卫星；测试结束统一停掉。"""
    from mast.satellite.sim import SimConfig, SimThread

    made: list = []

    def _make(url: str, token: str, sat_id: str = "sim1", *, state_dir: Path | None = None,
              watch_dir: Path | None = None, start: bool = True, **kw):
        opts = {"allow_plain_loopback": True, "reconnect_delays": (0.1, 0.2, 0.3),
                "auth_retry_s": 0.5, "speed": 10.0}
        opts.update(kw)
        cfg = SimConfig(hub_url=url, satellite_id=sat_id, token=token,
                        state_dir=state_dir or (tmp_path / f"simstate-{sat_id}"),
                        watch_dir=watch_dir, **opts)
        st = SimThread(cfg)
        made.append(st)
        if start:
            st.start()
        return st

    yield _make
    for st in made:
        try:
            st.stop()
        except Exception:  # noqa: BLE001
            pass


class FakeTransport:
    """同步替身：记下枢纽发给卫星的每条消息（不经网络）。"""

    def __init__(self, peer: str = "fake:1") -> None:
        self.sent: list[dict] = []
        self.closed = False
        self.close_code: int | None = None
        self.peer = peer

    def send(self, obj: dict) -> bool:
        if self.closed:
            return False
        self.sent.append(obj)
        return True

    def send_lazy(self, fn) -> bool:
        if self.closed:
            return False
        obj = fn()
        if obj is not None:
            self.sent.append(obj)
        return True

    def close(self, code: int, reason: str) -> None:
        self.closed = True
        self.close_code = code

    def of(self, t: str) -> list[dict]:
        return [m for m in self.sent if m.get("t") == t]


def hello(sat_id: str = "sim1", boot: str = "boot-A", devices=None, evt_seq: int = 0):
    from mast.satellite import protocol as P

    devs = devices if devices is not None else [
        {"id": "ccd", "kind": "spectrometer", "driver": "sim", "state": "ready",
         "caps": ["status", "stop", "safe", "acquire"]},
        {"id": "pol", "kind": "rotation_stage", "driver": "sim", "state": "ready",
         "caps": ["status", "stop", "safe", "move_abs"]},
    ]
    return P.parse_to_hub({"t": "hello", "proto": 1, "satellite_id": sat_id, "boot_id": boot,
                           "sw": "test", "host": "h", "time_ms": int(time.time() * 1000),
                           "devices": devs, "evt_seq": evt_seq})


def attach(hub, sat_id: str = "sim1", boot: str = "boot-A", **kw):
    t = FakeTransport()
    s = hub.attach(sat_id, t, hello(sat_id, boot, **kw))
    return s, t


def evt(seq: int, device: str, kind: str, data: dict | None = None, ts_ms: int | None = None):
    from mast.satellite import protocol as P

    return P.parse_to_hub({"t": "evt", "seq": seq, "device": device, "kind": kind,
                           "ts_ms": ts_ms if ts_ms is not None else int(time.time() * 1000),
                           "data": data or {}})


def wait_for(pred, timeout: float = 10.0, interval: float = 0.02, msg: str = ""):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {msg or pred}: last={last!r}")
