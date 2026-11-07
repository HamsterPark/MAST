"""wss + SPKI 钉值端到端；真正的 LiveHub（严格模式）收得下枢纽转发的帧；文件登记进实验文件夹。"""

from __future__ import annotations

import pytest

from tests.v2.satellite.conftest import start_server, stop_server, wait_for, write_sat_config


@pytest.fixture
def tls_server(hub, sat_root):
    from fastapi import FastAPI

    from mast.api.context import AppContext
    from mast.api.routes import satellites
    from mast.net.tls import ensure_lan_cert

    cert = sat_root / "api key" / "lan_cert.pem"
    key = sat_root / "api key" / "lan_key.pem"
    cert.parent.mkdir(parents=True, exist_ok=True)
    res = ensure_lan_cert(cert, key, addresses=["127.0.0.1"], hostname="hub-test")
    assert res.ok
    app = FastAPI()
    app.state.ctx = AppContext()
    app.include_router(satellites.router)
    s = start_server(app, ssl_kw={"ssl_certfile": str(cert), "ssl_keyfile": str(key)})
    s.extra["pin"] = res.spki_sha256
    yield s
    stop_server(s)


def test_wss_with_correct_pin_connects(hub, tls_server, sat_root, sims):
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    url = f"wss://127.0.0.1:{tls_server.port}/ws/satellite/v1"
    sim = sims(url, token, spki_sha256=tls_server.extra["pin"], allow_plain_loopback=False)
    assert sim.wait_connected(15), sim.sat.last_close
    assert hub.call("sim1", "ccd", "status", timeout_s=5)["state"] == "ready"


def test_wss_with_wrong_pin_never_sends_the_token(hub, tls_server, sat_root, sims):
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    url = f"wss://127.0.0.1:{tls_server.port}/ws/satellite/v1"
    sim = sims(url, token, spki_sha256="0" * 64, allow_plain_loopback=False)
    import time

    time.sleep(2.0)
    assert not sim.sat.is_connected()
    assert hub.satellite("sim1")["counters"]["connects"] == 0
    assert hub.satellite("sim1")["ever_connected"] is False


def test_real_live_hub_accepts_everything_the_bridge_sends(sat_root, sims, tmp_path):
    """FakeLive 不校验帧；这里换成真的 LiveHub 严格模式：declare 前 publish、帧格式不对、
    轴长度不符都会抛。桥接层吞异常只计数，所以断言 bridge.errors == 0。"""
    from fastapi import FastAPI

    from mast.api.context import AppContext
    from mast.api.routes import satellites
    from mast.live.testing import LiveRecorder, isolated_live_hub
    from mast.satellite.hub import SatelliteHub, reset_satellite_hub

    token = write_sat_config(sat_root, "sim1")
    with isolated_live_hub(strict=True) as live, LiveRecorder(live) as rec:
        h = SatelliteHub(data_root=sat_root, degraded_after_s=0.6, lost_after_s=1.5,
                         enabled_fn=lambda: True)          # live=None：用 get_live_hub()
        old = reset_satellite_hub(h)
        h.start()
        app = FastAPI()
        app.state.ctx = AppContext()
        app.include_router(satellites.router)
        srv = start_server(app)
        try:
            sim = sims(srv.ws_url, token, watch_dir=tmp_path / "watch")
            assert sim.wait_connected(10)
            wait_for(lambda: h.link_state("sim1") == "connected")
            h.call("sim1", "ccd", "acquire", {"exposure_ms": 100, "frames": 2}, timeout_s=20)
            h.call("sim1", "pol", "home", timeout_s=20)
            h.call("sim1", "pol", "move_abs", {"deg": 10.0}, timeout_s=20)
            h.call("sim1", "pico", "jog", {"axis": "3", "steps": 20}, timeout_s=20)
            h.call("sim1", "xyz", "move_rel", {"dz": 0.1}, timeout_s=20)
            h.call("sim1", "ccd", "set_center_wavelength", {"nm": 680.0}, timeout_s=20)
            h.call("sim1", "ccd", "acquire", {"exposure_ms": 100, "frames": 1, "save": False},
                   timeout_s=20)
            wait_for(lambda: len(rec.frames("sim1/ccd/spectrum")) >= 3, msg="spectrum frames")
            wait_for(lambda: rec.frames("sim1/health"), msg="health")
            wait_for(lambda: rec.frames("sim1/events"), msg="events")
            h.drain()
            assert h.bridge.errors == 0, h.bridge.last_error
            declared = set(rec.declared())
            assert {"sim1/ccd/spectrum", "sim1/pol/angle", "sim1/pico/steps", "sim1/xyz/position",
                    "sim1/health", "sim1/events"} <= declared
            axes = rec.axes("sim1/ccd/spectrum")
            assert len(axes) >= 2 and axes[-1]["unit"] == "nm" and axes[-1]["n"] == 1340
            last = rec.frames("sim1/ccd/spectrum")[-1]
            assert last["axis_ver"] == axes[-1]["ver"]          # 换中心波长后帧跟上新轴
            assert rec.frames("sim1/pico/steps")[-1]["values"]["3"] == 20.0
        finally:
            stop_server(srv)
            h.shutdown(wait_s=1.0)
            reset_satellite_hub(old)


def test_stored_files_are_submitted_to_the_experiment_folder(hub, server, sat_root, sims,
                                                              tmp_path):
    calls = []

    class FakeRuntime:
        def _submit_ingest(self, action_id, paths, *, source="skill", skill=""):
            calls.append((action_id, list(paths), source, skill))

    hub.attach_runtime(FakeRuntime())
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    sim = sims(server.ws_url, token, watch_dir=tmp_path / "watch")
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    hub.call("sim1", "ccd", "acquire", {"exposure_ms": 50, "frames": 1}, timeout_s=20)
    wait_for(lambda: calls, timeout=15, msg="ingest submitted")
    action_id, paths, source, skill = calls[0]
    assert action_id is None and source == "import" and skill == "satellite:sim1"
    assert [p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in paths] == [
        "Raman 1.spe", "Raman 1.spe.meta.json"]
    # _finalize 先提交入库、紧接着才追加索引行（行里记的就是提交结果）：calls 非空的那一刻，
    # 索引行可能还没写下。等行本身，而不是等提交（全量 -n 4 里抢到过这个空档）。
    rows = wait_for(lambda: hub.files().list("sim1"), timeout=15, msg="index row written")
    assert rows[0]["experiment_ingest"] == "submitted"
