"""``POST /api/admin/shutdown``：卫星的 safe 必须在 uvicorn 关掉 websocket **之前**送到并被确认。

真起 uvicorn（进程内、随机端口）+ 参考卫星，走真的 HTTP 关停端点，两种接法都测：

- ctx 上没有 live_app（端点不调 runtime.shutdown）：只有端点自己在置 should_exit 之前发的
  safe 赶得上 —— 之后 uvicorn 第一件事就是关掉所有 websocket；
- ctx.live_app 带真的 runtime 光学关停钩子：端点发过的 safe，钩子兜底时不重发。

参考卫星的光谱仪停采集要 0.6 s（``stop_latency_s``，LightField 也要等这一帧读出）：比 uvicorn
检查 should_exit 的 0.1 s 长得多。safe 要是排在 should_exit 之后，等回话的那段时间里 websocket
就被关了、确认永远到不了 —— 这条测试靠这一点分辨顺序（0 延迟时两种顺序都恰好赶得上）。

死人开关调到 30 s：采集停下来只能是 safe 停的。``_arm_exit_backstop``（20 s 后
``os._exit``）一律换成空操作 —— 不能在测试进程里武装它。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request

import pytest

from tests.v2.satellite.conftest import start_server, wait_for, write_sat_config

_DEVICES = ["ccd", "pico", "pol", "spe", "xyz"]


class _LiveApp:
    """``ctx.live_app`` 的最小形状：``shutdown()`` 跑真的 runtime 光学关停钩子。"""

    def __init__(self) -> None:
        from mast.core.runtime import CoreRuntime

        self.rt = CoreRuntime.__new__(CoreRuntime)
        self.calls = 0

    def shutdown(self) -> None:
        self.calls += 1
        self.rt._optics_shutdown()


def _cmds(sim) -> int:
    return sum(1 for m in list(sim.sat.received) if m["t"] == "cmd")


@pytest.mark.parametrize("with_runtime", [False, True], ids=["endpoint-only", "with-runtime-hook"])
def test_shutdown_endpoint_confirms_safe_before_uvicorn_closes_the_websocket(
        with_runtime, hub, sat_root, sims, monkeypatch, caplog):
    from fastapi import FastAPI

    import mast.instruments.registry as inst_registry
    from mast.api.context import AppContext
    from mast.api.routes import admin, satellites

    monkeypatch.setattr(admin, "_arm_exit_backstop", lambda: None)
    monkeypatch.setenv("MAST2_SHUTDOWN_TOKEN", "")
    local_stopped = threading.Event()

    class _Registry:
        def stop_all(self) -> None:
            local_stopped.set()

    monkeypatch.setattr(inst_registry, "get_instrument_registry", lambda: _Registry())

    app = FastAPI()
    app.state.ctx = AppContext()
    live = _LiveApp() if with_runtime else None
    if live is not None:
        app.state.ctx.live_app = live
    app.include_router(satellites.router)
    app.include_router(admin.router, prefix="/api")
    srv = start_server(app)
    app.state.uvicorn_server = srv.server

    token = write_sat_config(sat_root, "sim1", deadman_s=30.0)
    hub._load_config(force=True)
    sim = sims(srv.ws_url, token, device_opts={"ccd": {"stop_latency_s": 0.6}})
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    acq = hub.submit("sim1", "ccd", "acquire", {"exposure_ms": 60000, "frames": 1, "save": False},
                     timeout_s=120)
    ccd = sim.sat.devices["ccd"]
    wait_for(lambda: ccd.hub_active(), msg="acquiring")
    n_cmd = _cmds(sim)

    req = urllib.request.Request(srv.http + "/api/admin/shutdown", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with caplog.at_level(logging.INFO, logger="mast.satellite.hub"):
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode("utf-8"))
        srv.thread.join(15)
    assert not srv.thread.is_alive(), "uvicorn 没有退出"
    assert body["ok"] is True and body["exiting"] is True
    # 端点的回包里就有卫星的确认：safe 是在 should_exit（也就是关 websocket）之前确认的
    assert sorted(body["satellites"]["confirmed"]["sim1"]) == _DEVICES
    assert body["satellites"]["unconfirmed"] == {} and body["satellites"]["unreachable"] == []
    assert any("确认 sim1 5/5" in r.getMessage() for r in caplog.records)
    assert not ccd.busy and sim.sat.deadman_trips == 0          # safe 停的，不是死人开关
    # 被 safe 停掉的采集在卫星 journal 里以 cancelled 收尾。（它自己的 result 排在 safe 的回话
    # 之后发，连接这时正在关，枢纽收不收得到看时序 —— 那是重连后 cmd_status 的事，不在这里测。）
    wait_for(lambda: ((sim.sat.journal.get(acq.id) or {}).get("result") or {}).get("error"),
             msg="journal result")
    assert sim.sat.journal.get(acq.id)["result"]["error"]["code"] == "cancelled"
    time.sleep(0.3)
    assert _cmds(sim) == n_cmd + len(_DEVICES)                  # 每个设备一条 safe，没有重发
    if live is not None:
        assert live.calls == 1 and local_stopped.wait(3)
