"""枢纽 ↔ Python 参考卫星，走真的 WebSocket（进程内 uvicorn，随机端口）。

覆盖契约 §5 的主要语义：认证（accept 之前 401/403、协议版本 4406）、命令与结果、事件与
LiveHub 转发、人工接管、断线重连后用 cmd_status 找回结局、boot_id 变化判 lost_on_restart、
死人开关、文件断点续传与 sha 不符、E-STOP 广播、REST。
"""

from __future__ import annotations

import json
import time
import urllib.request

import pytest

from tests.v2.satellite.conftest import wait_for, write_sat_config


def _ws_status(url: str, headers: dict) -> int | str:
    """握手结果：HTTP 状态码（被拒）或 'open'。"""
    import asyncio

    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    async def _go():
        try:
            ws = await connect(url, additional_headers=headers, open_timeout=5)
        except InvalidStatus as exc:
            return exc.response.status_code
        await ws.close()
        return "open"

    return asyncio.run(_go())


def _get(server, path: str):
    with urllib.request.urlopen(server.http + path, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


# ── 认证 ────────────────────────────────────────────────────────────────────

def test_auth_is_checked_before_accept(hub, server, sat_root):
    token = write_sat_config(sat_root, "sim1")
    disabled_token = write_sat_config(sat_root, "off1", enabled=False)
    hub._load_config(force=True)
    base = {"X-MAST-Satellite-Proto": "1"}
    assert _ws_status(server.ws_url, {**base, "X-MAST-Satellite-Id": "sim1"}) == 401
    assert _ws_status(server.ws_url, {**base, "X-MAST-Satellite-Id": "sim1",
                                      "Authorization": "Bearer wrong"}) == 401
    assert _ws_status(server.ws_url, {**base, "X-MAST-Satellite-Id": "nosuch",
                                      "Authorization": f"Bearer {token}"}) == 401
    # 令牌对、id 不对（拿 sim1 的令牌冒充 off1）也不行
    assert _ws_status(server.ws_url, {**base, "X-MAST-Satellite-Id": "off1",
                                      "Authorization": f"Bearer {token}"}) == 401
    assert _ws_status(server.ws_url, {**base, "X-MAST-Satellite-Id": "off1",
                                      "Authorization": f"Bearer {disabled_token}"}) == 403
    hub.test_enabled["on"] = False
    assert _ws_status(server.ws_url, {**base, "X-MAST-Satellite-Id": "sim1",
                                      "Authorization": f"Bearer {token}"}) == 403
    hub.test_enabled["on"] = True
    assert _ws_status(server.ws_url, {**base, "X-MAST-Satellite-Id": "sim1",
                                      "Authorization": f"Bearer {token}"}) == "open"


def test_proto_mismatch_closes_with_4406(hub, server, sat_root):
    import asyncio

    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)

    async def _go():
        ws = await connect(server.ws_url, additional_headers={
            "Authorization": f"Bearer {token}", "X-MAST-Satellite-Id": "sim1",
            "X-MAST-Satellite-Proto": "2"}, open_timeout=5)
        try:
            await asyncio.wait_for(ws.recv(), 5)
        except ConnectionClosed:
            pass
        return ws.close_code

    assert asyncio.run(_go()) == 4406


def test_plain_ws_refused_without_loopback_flag(tmp_path):
    import asyncio

    from mast.satellite.sim import SimConfig, SimSatellite

    s = SimSatellite(SimConfig(hub_url="ws://10.0.0.5:7862/ws/satellite/v1", satellite_id="x",
                               token="t", state_dir=tmp_path / "s", allow_plain_loopback=True))
    with pytest.raises(PermissionError):
        asyncio.run(s._ssl_context())
    s2 = SimSatellite(SimConfig(hub_url="wss://10.0.0.5:7862/ws/satellite/v1", satellite_id="x",
                                token="t", state_dir=tmp_path / "s2"))
    with pytest.raises(PermissionError, match="钉值"):
        asyncio.run(s2._ssl_context())


# ── 命令、事件、LiveHub ─────────────────────────────────────────────────────

def test_connect_status_acquire_and_live(hub, server, sat_root, sims, tmp_path):
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    got: list[dict] = []
    hub.add_listener(lambda sid, e: got.append(e))
    sim = sims(server.ws_url, token, watch_dir=tmp_path / "watch")
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected", msg="connected")
    sats = hub.list_satellites()
    assert [s["id"] for s in sats] == ["sim1"]
    assert {d["id"] for d in sats[0]["devices"]} == {"ccd", "pol", "pico", "xyz", "spe"}
    wait_for(lambda: hub.clock_offset_ms("sim1") is not None, msg="clock sample")
    assert abs(hub.clock_offset_ms("sim1")) < 50          # 同一台机器

    st = hub.call("sim1", "ccd", "status", timeout_s=5)
    assert st["state"] == "ready" and st["laser_line_nm"] == 633.0
    res = hub.call("sim1", "ccd", "acquire", {"exposure_ms": 100, "frames": 3, "save": False},
                   timeout_s=20)
    assert res["frames"] == 3 and res["files"] == []
    cmd_frames = wait_for(lambda: [e for e in got if e["kind"] == "spectrum"
                                   and e["data"].get("cmd_id")], msg="spectra")
    assert len(cmd_frames) == 3
    assert all(e["data"]["n"] == 1340 for e in cmd_frames)
    assert all("ts_hub_ms" in e and "clock_offset_ms" in e for e in cmd_frames)
    hub.drain()
    live = hub.test_live
    wait_for(lambda: len(live.frames.get("sim1/ccd/spectrum", [])) >= 3, msg="live frames")
    assert live.declared["sim1/ccd/spectrum"]["kind"] == "spectrum"
    assert live.axes.get("sim1/ccd/spectrum"), "wavelength_axis 应当 set_axis"
    x, unit = live.axes["sim1/ccd/spectrum"][-1]
    assert unit == "nm" and len(x) == 1340 and 630 < x[0] < x[-1] < 700
    fr = live.frames["sim1/ccd/spectrum"][-1]
    assert len(fr["y"]) == 1340 and fr["axis_ver"] >= 1 and fr["t"] < 1e11
    wait_for(lambda: live.frames.get("sim1/health"), msg="health stream")


def test_motion_streams_axis_state(hub, server, sat_root, sims):
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    sim = sims(server.ws_url, token)
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    from mast.satellite.errors import SatelliteCommandError

    # 没回零就没有绝对角度：与真 KDC101+PRM1-Z8、C# 卫星一致，拒收
    with pytest.raises(SatelliteCommandError) as ei:
        hub.call("sim1", "pol", "move_abs", {"deg": 45.0}, timeout_s=5)
    assert ei.value.code == "unavailable"
    assert hub.call("sim1", "pol", "move_rel", {"deg": 5.0}, timeout_s=20)["position_deg"] == 5.0
    assert hub.call("sim1", "pol", "home", timeout_s=20)["homed"] is True
    out = hub.call("sim1", "pol", "move_abs", {"deg": 45.0}, timeout_s=20)
    assert out["position_deg"] == pytest.approx(45.0) and out["moving"] is False
    wait_for(lambda: (hub.latest("sim1", "pol", "axis_state") or {}).get("position_deg") == 45.0,
             msg="axis_state 45")
    live = hub.test_live
    wait_for(lambda: live.frames.get("sim1/pol/angle"), msg="angle stream")
    with pytest.raises(Exception) as ei:
        hub.call("sim1", "pol", "move_abs", {"deg": 9999.0}, timeout_s=5)
    assert getattr(ei.value, "code", "") == "limit"

    out = hub.call("sim1", "pico", "jog", {"axis": "1", "steps": 100}, timeout_s=10)
    assert out["axes"]["1"]["steps"] == 100
    with pytest.raises(SatelliteCommandError) as ei:
        hub.call("sim1", "nosuch", "status")
    assert ei.value.code == "invalid_args"


def test_move_is_running_while_axis_state_names_it(hub, server, sat_root, sims):
    """axis_state.data.cmd_id（运动中才带）⇒ 枢纽把 move 命令从 accepted 推到 running；
    停下后最新的 axis_state 是 moving=false、不带 cmd_id。"""
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    sim = sims(server.ws_url, token, speed=1.0)          # 20°/s：60° 要 3 s
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    c = hub.submit("sim1", "pol", "move_rel", {"deg": 60.0})
    wait_for(lambda: c.state == "running", timeout=5, msg="move running")
    mid = hub.latest("sim1", "pol", "axis_state")
    assert mid["moving"] is True and mid["cmd_id"] == c.id
    out = c.wait(20)
    assert out["position_deg"] == pytest.approx(60.0) and out["moving"] is False
    assert c.state == "done"
    wait_for(lambda: (hub.latest("sim1", "pol", "axis_state") or {}).get("moving") is False,
             msg="stopped axis_state")
    assert "cmd_id" not in hub.latest("sim1", "pol", "axis_state")


def test_hub_side_soft_limits(hub, server, sat_root, sims):
    token = write_sat_config(sat_root, "sim1", devices={
        "xyz": {"role": "focus_xyz", "limits_mm": {"x": [0, 1], "y": [0, 5], "z": [0, 9.6]},
                "max_step_mm": 0.1}})
    hub._load_config(force=True)
    sim = sims(server.ws_url, token)
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    from mast.satellite.errors import SatelliteCommandError

    with pytest.raises(SatelliteCommandError) as ei:
        hub.submit("sim1", "xyz", "move_abs", {"x": 5.0})
    assert ei.value.code == "limit"
    with pytest.raises(SatelliteCommandError) as ei:
        hub.submit("sim1", "xyz", "move_rel", {"dx": 0.5})
    assert ei.value.code == "limit"
    assert hub.satellite("sim1")["counters"]["cmds"] == 0     # 根本没发出去


def test_operator_hold_rejects_but_stop_passes(hub, server, sat_root, sims):
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    sim = sims(server.ws_url, token)
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    sim.sat.set_operator_hold("ccd", True)
    wait_for(lambda: any(d["id"] == "ccd" and d.get("operator_hold")
                         for d in hub.satellite("sim1")["devices"]), msg="hold visible")
    from mast.satellite.errors import SatelliteCommandError

    with pytest.raises(SatelliteCommandError) as ei:
        hub.call("sim1", "ccd", "acquire", {"exposure_ms": 10}, timeout_s=5)
    assert ei.value.code == "operator_hold"
    assert hub.call("sim1", "ccd", "stop", timeout_s=5)["operator_hold"] is True
    assert hub.call("sim1", "ccd", "status", timeout_s=5)["state"] == "operator_hold"
    # 拒收也进卫星 journal：同一 request_id 再发拿回同一个结局
    with pytest.raises(SatelliteCommandError) as ei2:
        hub.call("sim1", "ccd", "acquire", {"exposure_ms": 10}, timeout_s=5,
                 request_id=ei.value.cmd_id)
    assert ei2.value.code == "operator_hold"


def test_request_id_is_idempotent(hub, server, sat_root, sims):
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    sim = sims(server.ws_url, token)
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    rid = "11111111-2222-3333-4444-555555555555"
    a = hub.submit("sim1", "pico", "jog", {"axis": "2", "steps": 50}, request_id=rid)
    b = hub.submit("sim1", "pico", "jog", {"axis": "2", "steps": 50}, request_id=rid)
    assert a is b
    v = a.wait()
    assert v["axes"]["2"]["steps"] == 50
    # 枢纽这边忘掉它，再用同一 id 发：卫星按 journal 回 duplicate_done + 缓存结果，不会再走 50 步
    with hub._lock:
        hub._cmds.pop(rid, None)
    c = hub.submit("sim1", "pico", "jog", {"axis": "2", "steps": 50}, request_id=rid)
    assert c is not a
    assert c.wait()["axes"]["2"]["steps"] == 50
    assert c.ack_state == "duplicate_done"
    assert hub.call("sim1", "pico", "status")["axes"]["2"]["steps"] == 50


# ── 断线、重连、重启 ────────────────────────────────────────────────────────

def test_reconnect_same_boot_recovers_result_via_cmd_status(hub, server, sat_root, sims):
    # 死人开关调长：这条测的是「断线期间做完的命令经 cmd_status 找回」，而不是死人开关。
    # 用默认的 1 s 时，1 s 的采集与死人开关赛跑，机器一忙就被死人开关取消（那是对的行为）。
    token = write_sat_config(sat_root, "sim1", deadman_s=15.0)
    hub._load_config(force=True)
    sim = sims(server.ws_url, token)
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    seqs: list[int] = []
    hub.add_listener(lambda sid, e: e.get("seq") and seqs.append(e["seq"]))
    boot = hub.satellite("sim1")["boot_id"]
    cmd = hub.submit("sim1", "ccd", "acquire", {"exposure_ms": 5000, "frames": 2, "save": False},
                     timeout_s=60)
    wait_for(lambda: cmd.state in ("accepted", "running"), msg="accepted")
    # 关总开关：枢纽断开会话，卫星重连吃 403。采集在断线期间做完，结果只在卫星 journal 里。
    hub.test_enabled["on"] = False
    wait_for(lambda: not sim.sat.is_connected(), msg="disconnected")
    wait_for(lambda: (sim.sat.journal.get(cmd.id) or {}).get("state") == "done", timeout=15,
             msg="finished while offline")
    assert not cmd.done
    hub.test_enabled["on"] = True
    v = cmd.wait(timeout_s=30)
    assert v["frames"] == 2
    assert hub.satellite("sim1")["boot_id"] == boot
    assert any(m["t"] == "cmd_status" for m in sim.sat.received), "结局应当经 cmd_status 找回"
    hub.drain()
    # 事件不重不漏：枢纽交给监听者的 seq 连续、没有重复（断线期间的事件重连后补上了）
    assert seqs and seqs == list(range(seqs[0], seqs[-1] + 1))
    assert hub.satellite("sim1")["counters"]["restarts"] == 0


def test_boot_id_change_marks_inflight_lost_on_restart(hub, server, sat_root, sims, tmp_path):
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    state = tmp_path / "same-state"
    a = sims(server.ws_url, token, state_dir=state)
    assert a.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    boot_a = hub.satellite("sim1")["boot_id"]
    wait_for(lambda: hub.latest("sim1", "pol", "axis_state") is not None, msg="pol state")
    cmd = hub.submit("sim1", "ccd", "acquire", {"exposure_ms": 20000, "frames": 1, "save": False},
                     timeout_s=60)
    wait_for(lambda: cmd.state in ("accepted", "running"))
    a.stop()                                     # 「蓝屏」：进程没了，在途作业结局未知
    wait_for(lambda: hub.link_state("sim1") == "lost", msg="lost")
    b = sims(server.ws_url, token, state_dir=state)   # 同一状态目录，新 boot_id
    assert b.wait_connected(10)
    from mast.satellite.errors import SatelliteCommandError

    with pytest.raises(SatelliteCommandError) as ei:
        cmd.wait(timeout_s=10)
    assert ei.value.code == "lost_on_restart"
    sat = hub.satellite("sim1")
    assert sat["boot_id"] != boot_a and sat["counters"]["restarts"] == 1
    # 旧 boot 的位置不可信：新事件到来之前 latest() 不返回
    # （卫星一连上就会发新的 axis_state，所以这里查的是「重启那一刻」被标陈旧过）
    # 卫星 journal：重启前 running 的条目判 lost_on_restart，同一 id 再发如实回答
    with pytest.raises(SatelliteCommandError) as ei2:
        hub.call("sim1", "ccd", "acquire", {"exposure_ms": 10}, request_id=cmd.id, timeout_s=5)
    assert ei2.value.code == "lost_on_restart"


def test_restart_marks_latest_stale(hub, sat_root):
    """不经网络：直接用替身会话验证「boot_id 变了 → latest 陈旧」。"""
    from tests.v2.satellite.conftest import attach, evt

    write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    s1, t1 = attach(hub, "sim1", boot="A")
    hub.on_message(s1, evt(1, "pol", "axis_state", {"position_deg": 10.0}))
    assert hub.latest("sim1", "pol", "axis_state") == {"position_deg": 10.0}
    hub.detach(s1, "gone")
    s2, t2 = attach(hub, "sim1", boot="B")
    assert hub.latest("sim1", "pol", "axis_state") is None
    assert hub.latest("sim1", "pol", "axis_state", include_stale=True) == {"position_deg": 10.0}
    assert t2.sent[0]["evt_ack"] == 0


def test_deadman_stops_hub_initiated_acquisition(hub, server, sat_root, sims):
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    sim = sims(server.ws_url, token)
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    got: list[dict] = []
    hub.add_listener(lambda sid, e: got.append(e))
    cmd = hub.submit("sim1", "ccd", "acquire", {"exposure_ms": 60000, "frames": 1, "save": False},
                     timeout_s=120)
    wait_for(lambda: sim.sat.devices["ccd"].hub_active(), msg="acquiring")
    sim.sat.ping_paused_until = time.monotonic() + 3.0      # 卫星「听不到」枢纽
    wait_for(lambda: sim.sat.deadman_trips >= 1, timeout=5, msg="deadman")
    assert not sim.sat.devices["ccd"].busy
    from mast.satellite.errors import SatelliteCommandError

    with pytest.raises(SatelliteCommandError) as ei:
        cmd.wait(timeout_s=10)
    assert ei.value.code == "cancelled"
    wait_for(lambda: any(e["kind"] == "deadman" for e in got), msg="deadman event")


def test_deadman_leaves_manual_acquisition_alone(hub, server, sat_root, sims):
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    sim = sims(server.ws_url, token)
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    ccd = sim.sat.devices["ccd"]
    from mast.satellite.sim.devices import ActionCtx

    ctx = ActionCtx(None, "manual")
    import asyncio

    task_box = {}

    def _start_manual():
        task_box["t"] = asyncio.ensure_future(
            ccd._run_job("acquire", ccd.act_acquire({"exposure_ms": 60000, "frames": 1,
                                                     "save": False}, ctx), ctx))
    sim.sat.call_threadsafe(_start_manual)
    # 另起一个枢纽发起的运动，让死人开关有理由触发（700° 在 10 倍速下要 3.5 s，够死人开关先到）
    hub.call("sim1", "pol", "home", timeout_s=20)
    cmd = hub.submit("sim1", "pol", "move_abs", {"deg": 700.0}, timeout_s=60)
    wait_for(lambda: sim.sat.devices["pol"].hub_active(), msg="moving")
    sim.sat.ping_paused_until = time.monotonic() + 3.0
    wait_for(lambda: sim.sat.deadman_trips >= 1, timeout=5, msg="deadman")
    assert not sim.sat.devices["pol"].busy          # 枢纽发起的停了
    assert ccd.busy and ccd._job_origin == "manual"   # 人工发起的没停
    sim.sat.call_threadsafe(lambda: task_box["t"].cancel())
    from mast.satellite.errors import SatelliteCommandError

    with pytest.raises(SatelliteCommandError):
        cmd.wait(timeout_s=10)


def test_liveness_degraded_then_lost(hub, server, sat_root, sims):
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    sim = sims(server.ws_url, token)
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    states: list[str] = []
    hub.add_listener(lambda sid, e: e["kind"] == "link" and states.append(e["data"]["state"]))
    sim.sat.ping_paused_until = time.monotonic() + 30   # 不回 pong
    wait_for(lambda: "degraded" in states, timeout=5, msg="degraded")
    from mast.satellite.errors import SatelliteUnavailable

    with pytest.raises(SatelliteUnavailable):
        hub.submit("sim1", "ccd", "status")
    wait_for(lambda: "lost" in states, timeout=5, msg="lost")
    sim.sat.ping_paused_until = 0.0
    wait_for(lambda: hub.link_state("sim1") == "connected", timeout=10, msg="back")


# ── 文件 ────────────────────────────────────────────────────────────────────

def _publish_stm_state(n: int = 5) -> None:
    from mast.core.events import EventBus

    for i in range(n):
        EventBus.get().publish_hardware_state(bias_v=0.5, current_a=1e-10 + i * 1e-12, z_m=1e-9,
                                              z_controller_on=True, setpoint_a=1e-10,
                                              scan_running=False, withdrawn=False)


def test_file_upload_with_meta_and_stm_snapshot(hub, server, sat_root, sims, tmp_path):
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    watch = tmp_path / "watch"
    sim = sims(server.ws_url, token, watch_dir=watch)
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    _publish_stm_state()
    res = hub.call("sim1", "ccd", "acquire", {"exposure_ms": 100, "frames": 2}, timeout_s=20)
    _publish_stm_state()
    assert len(res["files"]) == 1
    rows = wait_for(lambda: hub.files().list("sim1"), timeout=15, msg="stored file")
    row = rows[0]
    stored = sat_root / row["stored_rel"]
    assert stored.exists() and stored.stat().st_size == row["size"]
    assert row["stored_rel"].startswith("satellite/sim1/files/")
    meta = hub.files().meta(row["file_id"])
    assert meta["schema"] == "mast.satellite.file_meta/1"
    assert meta["spe"]["ok"] is True
    assert meta["spe"]["key"]["laser_line_nm"] == 633.0
    assert meta["spe"]["key"]["frames"] == 2
    assert meta["times"]["source"] == "satellite_meta"
    assert meta["stm_state"]["matched"] is True
    assert meta["stm_state"]["during"]["n"] >= 0
    assert meta["clock"]["offset_ms"] is not None
    # REST
    lst = _get(server, "/api/satellites/files?sat_id=sim1")
    assert lst["count"] == 1 and lst["files"][0]["file_id"] == row["file_id"]


def test_file_resume_after_disconnect(hub, server, sat_root, sims, tmp_path):
    token = write_sat_config(sat_root, "sim1", max_upload_bytes_per_s=200_000)
    hub._load_config(force=True)
    watch = tmp_path / "watch"
    watch.mkdir()
    import os

    blob = os.urandom(600_000)
    sim = sims(server.ws_url, token, watch_dir=watch, chunk_bytes=64 * 1024)
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    sim.sat.uploader.pause_after_bytes = 200_000          # 发到 ~200 KB 时断开
    (watch / "big.csv").write_bytes(blob)
    wait_for(lambda: hub.satellite("sim1")["counters"]["connects"] >= 2, timeout=20,
             msg="dropped and reconnected")
    rows = wait_for(lambda: hub.files().list("sim1"), timeout=30, msg="stored after resume")
    assert (sat_root / rows[0]["stored_rel"]).read_bytes() == blob
    assert hub._receiver.stats["resumed"] >= 1
    # 续传时没有从 0 重发：卫星实际发出的字节数明显小于两倍文件大小
    assert sim.sat.uploader.bytes_sent < 2 * len(blob)


def test_file_sha_mismatch_is_retried(hub, server, sat_root, sims, tmp_path):
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    watch = tmp_path / "watch"
    watch.mkdir()
    sim = sims(server.ws_url, token, watch_dir=watch)
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    sim.sat.uploader.corrupt_next = True
    (watch / "x.csv").write_bytes(b"a,b\n1,2\n" * 1000)
    rows = wait_for(lambda: hub.files().list("sim1"), timeout=30, msg="stored after retry")
    assert hub._receiver.stats["sha_mismatch"] == 1
    assert (sat_root / rows[0]["stored_rel"]).read_bytes() == b"a,b\n1,2\n" * 1000
    assert any("sha256_mismatch" in f for f in sim.sat.uploader.failures)


# ── E-STOP ──────────────────────────────────────────────────────────────────

def test_estop_broadcast_stops_everything(hub, server, sat_root, sims):
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    sim = sims(server.ws_url, token)
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    hub.call("sim1", "pol", "home", timeout_s=20)
    acq = hub.submit("sim1", "ccd", "acquire", {"exposure_ms": 60000, "save": False}, timeout_s=90)
    mv = hub.submit("sim1", "pol", "move_abs", {"deg": 700.0}, timeout_s=90)
    wait_for(lambda: sim.sat.devices["ccd"].busy and sim.sat.devices["pol"].busy, msg="busy")
    out = hub.emergency_stop_all("test estop")
    assert out["sent"] == ["sim1"] and out["errors"] == []
    from mast.satellite.errors import SatelliteCommandError

    for c in (acq, mv):
        with pytest.raises(SatelliteCommandError) as ei:
            c.wait(timeout_s=10)
        assert ei.value.code == "cancelled"
    est = wait_for(lambda: (hub.satellite("sim1")["last_estop"] or {}).get("ack"), msg="estop_ack")
    # stopped = 每个 stop 正常做完的设备（含本来就空闲的）——急停覆盖到了谁一目了然、没有竞态
    assert set(est["stopped"]) == {"ccd", "pol", "pico", "xyz", "spe"} and est["errors"] == []


def test_safe_all_on_shutdown_stops_only_hub_initiated_work(hub, server, sat_root, sims):
    """MAST 正常关停发 safe：枢纽发起的采集与运动停下，人工在 LightField 里做的
    采集不打断；人工接管中的设备照样收得下 safe。"""
    import asyncio

    from mast.satellite.errors import SatelliteCommandError
    from mast.satellite.sim.devices import ActionCtx

    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    sim = sims(server.ws_url, token, speed=2.0)
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    hub.call("sim1", "pol", "home", timeout_s=20)
    mv = hub.submit("sim1", "pol", "move_abs", {"deg": 700.0}, timeout_s=90)
    ccd = sim.sat.devices["ccd"]
    ctx = ActionCtx(None, "manual")
    box = {}
    sim.sat.call_threadsafe(lambda: box.setdefault("t", asyncio.ensure_future(
        ccd._run_job("acquire", ccd.act_acquire({"exposure_ms": 60000, "frames": 1, "save": False},
                                                ctx), ctx))))
    sim.sat.set_operator_hold("xyz", True)
    wait_for(lambda: sim.sat.devices["pol"].hub_active() and ccd.busy, msg="both running")
    out = hub.safe_all("MAST 关停")
    assert out["sent"] == ["sim1"] and out["errors"] == []
    assert set(out["commands"]["sim1"]) == {"ccd", "pol", "pico", "xyz", "spe"}
    with pytest.raises(SatelliteCommandError) as ei:
        mv.wait(timeout_s=10)
    assert ei.value.code == "cancelled"                    # 枢纽发起的运动停了
    # 人工接管中的设备也收得下 safe（只停枢纽发起的活，不该被 operator_hold 拒）
    xyz_safe = hub._cmds[out["commands"]["sim1"]["xyz"]]
    assert xyz_safe.wait(timeout_s=10) == {"stopped": []}
    pol_safe = hub._cmds[out["commands"]["sim1"]["pol"]]
    assert pol_safe.wait(timeout_s=10) == {"stopped": ["pol"]}
    hub.drain()
    time.sleep(0.5)
    assert ccd.busy and ccd._job_origin == "manual"       # 人工采集没被打断
    assert not any(m["t"] == "estop" for m in sim.sat.received)
    sim.sat.call_threadsafe(lambda: box["t"].cancel())


def test_runtime_shutdown_hook_confirms_safe_before_closing(hub, server, sat_root, sims,
                                                           monkeypatch, caplog):
    """真的关停钩子（CoreRuntime._optics_shutdown）+ 真枢纽 + 真 WebSocket + 参考卫星：

    - safe 在会话关掉**之前**送到并被确认（日志里有「确认 sim1 5/5」），枢纽发起的采集停了，
      而且不是死人开关停的（死人开关调到 30 s）；
    - 会话关掉之后，本机光学台 stop_all 经真的远程驱动（RemoteEndpoint.stop）补的 stop 一条也
      没发到卫星 —— 关停时卫星设备只收 safe；
    - 卫星被关掉会话后立刻重连：枢纽不能被这条连接重新拉起来（回 503），服务还在退出途中；
    - 再调一次钩子什么都不做（shutdown() 常被调两次）。
    """
    import logging
    import threading

    import mast.instruments.registry as inst_registry
    from mast.core.runtime import CoreRuntime
    from mast.instruments.remote import RemoteEndpoint, set_hub_provider

    token = write_sat_config(sat_root, "sim1", deadman_s=30.0)
    hub._load_config(force=True)
    set_hub_provider(None)                       # RemoteEndpoint → 这个枢纽单例
    sim = sims(server.ws_url, token)
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    acq = hub.submit("sim1", "ccd", "acquire", {"exposure_ms": 60000, "frames": 1, "save": False},
                     timeout_s=120)
    ccd = sim.sat.devices["ccd"]
    wait_for(lambda: ccd.hub_active(), msg="acquiring")

    class _Registry:                             # 一台由远程驱动接管的卫星设备
        def __init__(self) -> None:
            self.done = threading.Event()

        def stop_all(self) -> None:
            RemoteEndpoint("sim1", "pol").stop()
            self.done.set()

    reg = _Registry()
    monkeypatch.setattr(inst_registry, "get_instrument_registry", lambda: reg)
    n_cmd = sum(1 for m in list(sim.sat.received) if m["t"] == "cmd")
    rt = CoreRuntime.__new__(CoreRuntime)
    with caplog.at_level(logging.INFO, logger="mast.satellite.hub"):
        rt._optics_shutdown()
        assert reg.done.wait(5)
    assert any("确认 sim1 5/5" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]
    assert not ccd.busy and sim.sat.deadman_trips == 0     # safe 停的，不是死人开关
    # 采集自己的 result 排在 safe 的回话之后发，会话这时正在关 —— 看卫星 journal，不看枢纽
    wait_for(lambda: ((sim.sat.journal.get(acq.id) or {}).get("result") or {}).get("error"),
             msg="journal result")
    assert sim.sat.journal.get(acq.id)["result"]["error"]["code"] == "cancelled"
    # 卫星立刻重连（重连间隔 0.1–0.3 s）：被 503 挡回去，枢纽没有被重新拉起来
    wait_for(lambda: "503" in (sim.sat.last_close or ""), timeout=5, msg="reconnect refused")
    assert hub.started is False and hub.link_state("sim1") != "connected"
    got = list(sim.sat.received)
    assert sum(1 for m in got if m["t"] == "cmd") == n_cmd + 5    # 5 条 safe，没有 stop
    assert not any(m["t"] == "estop" for m in got)
    rt._optics_shutdown()                                    # 第二遍：什么都不做
    time.sleep(0.3)
    assert sum(1 for m in list(sim.sat.received) if m["t"] == "cmd") == n_cmd + 5


def test_estop_reports_unreachable_and_never_raises(hub, sat_root):
    write_sat_config(sat_root, "rig1")
    hub._load_config(force=True)
    out = hub.emergency_stop_all("x")
    assert out["sent"] == [] and out["unreachable"] == [{"id": "rig1", "state": "lost"}]


# ── 命令行 ──────────────────────────────────────────────────────────────────

def test_sim_cli_connects_and_exits_cleanly(hub, server, sat_root, tmp_path):
    """真正的 ``python -m mast.satellite.sim`` 子进程（令牌从文件读，带 BOM 也认）。"""
    import os
    import subprocess
    import sys
    from pathlib import Path

    token = write_sat_config(sat_root, "cli1")
    hub._load_config(force=True)
    tok_file = tmp_path / "cli1.token"
    tok_file.write_text(token + "\n", encoding="utf-8-sig")
    mastv2 = Path(__file__).resolve().parents[3] / "MASTv2"
    proc = subprocess.Popen(
        [sys.executable, "-m", "mast.satellite.sim", "--hub", server.ws_url, "--id", "cli1",
         "--token-file", str(tok_file), "--allow-plain-loopback", "--state-dir",
         str(tmp_path / "st"), "--devices", "ccd,pol", "--exit-after", "6"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "PYTHONPATH": str(mastv2), "PYTHONUTF8": "1"})
    try:
        wait_for(lambda: hub.link_state("cli1") == "connected", timeout=20, msg="cli connected")
        assert hub.call("cli1", "pol", "move_rel", {"deg": 2.0}, timeout_s=10)["position_deg"] == 2.0
        assert {d["id"] for d in hub.satellite("cli1")["devices"]} == {"ccd", "pol"}
        out, _ = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.communicate(timeout=30)
    assert proc.returncode == 0, out
    assert "exit: connects=1" in out
    wait_for(lambda: hub.link_state("cli1") == "lost", timeout=5, msg="cli gone")


# ── REST ────────────────────────────────────────────────────────────────────

def test_rest_endpoints(hub, server, sat_root, sims):
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    sim = sims(server.ws_url, token)
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected")
    hub.call("sim1", "pol", "move_rel", {"deg": 1.0}, timeout_s=10)
    a = _get(server, "/api/satellites")
    assert a["hub"]["started"] is True and a["satellites"][0]["state"] == "connected"
    d = _get(server, "/api/satellites/sim1")
    assert d["id"] == "sim1" and isinstance(d["pending"], list)
    dv = _get(server, "/api/satellites/sim1/devices")
    assert {x["id"] for x in dv["devices"]} >= {"ccd", "pol"}
    ev = _get(server, "/api/satellites/sim1/events?limit=5&device=pol")
    assert ev["events"] and all(e["device"] == "pol" for e in ev["events"])
    fl = _get(server, "/api/satellites/files")
    assert fl["count"] == 0
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(server, "/api/satellites/nosuch")
    assert ei.value.code == 404
    pg = _get(server, "/api/port-guard")
    assert "ok" in pg and "nanonis" in pg
