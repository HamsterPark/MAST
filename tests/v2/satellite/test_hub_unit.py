"""SatelliteHub 的语义（不经网络、不起线程）：会话、事件游标与 evt_ack、幂等、重启识别、
活性状态机、命令到期 / 取消 / check_abort、E-STOP、持久化。"""

from __future__ import annotations

import queue
import threading
import time

import pytest

from mast.satellite import protocol as P
from mast.satellite.errors import (
    SatelliteCommandError,
    SatelliteTimeout,
    SatelliteUnavailable,
)
from mast.satellite.hub import SatelliteHub

from tests.v2.satellite.conftest import FakeLive, attach, evt, write_sat_config


@pytest.fixture
def h(sat_root):
    write_sat_config(sat_root, "sim1")
    hub = SatelliteHub(data_root=sat_root, degraded_after_s=3.0, lost_after_s=10.0,
                       enabled_fn=lambda: True, live=FakeLive())
    yield hub
    hub.shutdown(close_sessions=False)


def dispatch(hub) -> list:
    """把分发队列里的东西就地处理掉（单测里没有分发线程）。"""
    items = []
    while True:
        try:
            item = hub._dispatch_q.get_nowait()
        except queue.Empty:
            return items
        items.append(item)
        hub._dispatch_one(item)


def result(cid, ok=True, value=None, code=None):
    m = {"t": "result", "id": cid, "ok": ok, "value": value}
    if not ok:
        m["error"] = {"code": code, "message": "x"}
    return P.parse_to_hub(m)


def test_welcome_and_device_view(h):
    s, t = attach(h, "sim1", boot="A")
    w = t.sent[0]
    assert w["t"] == "welcome" and w["evt_ack"] == 0 and w["proto"] == 1
    assert w["config"]["heartbeat_s"] == 0.2 and w["config"]["deadman_s"] == 1.0
    sats = h.list_satellites()
    assert sats[0]["state"] == "connected" and {d["id"] for d in sats[0]["devices"]} == {"ccd", "pol"}
    assert h.devices("rotation_stage")[0][0] == "sim1"


def test_attach_rejects_mismatched_hello(h):
    from mast.satellite.errors import SatelliteError

    from tests.v2.satellite.conftest import FakeTransport, hello

    with pytest.raises(SatelliteError):
        h.attach("sim1", FakeTransport(), hello("other"))


def test_events_land_once_ack_is_cumulative_and_coalesced(h):
    s, t = attach(h, "sim1", boot="A")
    got = []
    h.add_listener(lambda sid, e: e.get("origin") != "hub" and got.append(e["seq"]))
    for seq in (1, 2, 3):
        h.on_message(s, evt(seq, "pol", "axis_state", {"position_deg": seq}))
    h.on_message(s, evt(2, "pol", "axis_state", {"position_deg": 99}))   # 重发的旧事件
    dispatch(h)
    assert got == [1, 2, 3]
    assert h.latest("sim1", "pol", "axis_state") == {"position_deg": 3}
    assert h.satellite("sim1")["counters"]["dup_events"] == 1
    h.tick()
    acks = t.of("evt_ack")
    assert acks == [{"t": "evt_ack", "upto": 3}]
    h.tick()
    assert len(t.of("evt_ack")) == 1                  # 没有新事件就不再发
    h.on_message(s, evt(7, "pol", "axis_state", {}))  # 缺口 4..6（卫星缓冲溢出）
    assert h.satellite("sim1")["counters"]["gap_events"] == 3
    time.sleep(0.11)
    h.tick()
    assert t.of("evt_ack")[-1]["upto"] == 7


def test_reconnect_same_boot_gets_evt_ack_and_cmd_status(h):
    s, t = attach(h, "sim1", boot="A")
    for seq in range(1, 6):
        h.on_message(s, evt(seq, "ccd", "device_state", {"state": "ready"}))
    cmd = h.submit("sim1", "ccd", "acquire", {"exposure_ms": 10}, timeout_s=30)
    assert t.of("cmd")[-1]["id"] == cmd.id
    h.detach(s, "net")
    assert h.link_state("sim1") == "lost"
    with pytest.raises(SatelliteUnavailable):
        h.submit("sim1", "ccd", "status")
    assert not cmd.done                               # 在途命令等自己的时限
    s2, t2 = attach(h, "sim1", boot="A", evt_seq=5)
    assert t2.sent[0]["evt_ack"] == 5
    assert t2.sent[1] == {"t": "cmd_status", "ids": [cmd.id]}
    # 卫星说 unknown（断线前没送到）→ 枢纽用同一 id 补发
    h.on_message(s2, P.parse_to_hub({"t": "cmd_status_reply",
                                     "items": [{"id": cmd.id, "state": "unknown"}]}))
    assert t2.of("cmd")[-1]["id"] == cmd.id and cmd.sends == 2
    h.on_message(s2, P.parse_to_hub({"t": "cmd_status_reply", "items": [
        {"id": cmd.id, "state": "done",
         "result": {"t": "result", "id": cmd.id, "ok": True, "value": {"frames": 1}}}]}))
    assert cmd.wait(timeout_s=1) == {"frames": 1}


def test_boot_change_loses_inflight_and_never_replays(h):
    s, t = attach(h, "sim1", boot="A")
    h.on_message(s, evt(1, "pol", "axis_state", {"position_deg": 10.0}))
    cmd = h.submit("sim1", "pol", "move_abs", {"deg": 20}, timeout_s=30)
    h.detach(s, "bsod")
    s2, t2 = attach(h, "sim1", boot="B")
    with pytest.raises(SatelliteCommandError) as ei:
        cmd.wait(timeout_s=1)
    assert ei.value.code == "lost_on_restart"
    assert t2.sent[0]["evt_ack"] == 0
    assert t2.of("cmd") == [] and t2.of("cmd_status") == []      # 绝不自动重放
    assert h.latest("sim1", "pol", "axis_state") is None
    assert h.satellite("sim1")["counters"]["restarts"] == 1
    items = dispatch(h)
    assert any(i[0] == "hub_evt" and i[2]["kind"] == "restart" for i in items)


def test_satellite_counter_going_backwards_is_trusted_not_silently_dropped(h):
    """同一 boot_id 下 evt_seq 倒退只能是卫星端的 bug（比如重启了却沿用 boot_id）。按卫星为准：
    宁可多收重复，也不把它的新事件当成「已落地」静默丢掉。"""
    s, t = attach(h, "sim1", boot="A")
    for seq in range(1, 6):
        h.on_message(s, evt(seq, "pol", "axis_state", {}))
    h.detach(s, "x")
    s2, t2 = attach(h, "sim1", boot="A", evt_seq=0)
    assert t2.sent[0]["evt_ack"] == 0
    h.on_message(s2, evt(1, "pol", "axis_state", {"position_deg": 7.0}))
    assert h.latest("sim1", "pol", "axis_state") == {"position_deg": 7.0}


def test_replaced_connection_closes_the_old_one(h):
    s, t = attach(h, "sim1", boot="A")
    s2, t2 = attach(h, "sim1", boot="A")
    assert t.closed and t.close_code == P.CLOSE_REPLACED
    h.detach(s, "old socket ended")                   # 旧连接的收尾不能把新连接判 lost
    assert h.link_state("sim1") == "connected"
    h.on_message(s, evt(1, "pol", "axis_state", {}))  # 旧会话的消息被忽略
    assert h.satellite("sim1")["evt_landed"] == 0


def test_idempotent_submit_and_result_routing(h):
    s, t = attach(h, "sim1")
    a = h.submit("sim1", "ccd", "status", request_id="r1")
    assert h.submit("sim1", "ccd", "status", request_id="r1") is a
    with pytest.raises(SatelliteCommandError):
        h.submit("sim1", "pol", "status", request_id="r1")       # 同一 id 用于别的命令
    h.on_message(s, P.parse_to_hub({"t": "ack", "id": "r1", "state": "accepted"}))
    assert a.state == "accepted"
    h.on_message(s, result("r1", value={"state": "ready"}))
    assert a.wait() == {"state": "ready"}
    assert h.submit("sim1", "ccd", "status", request_id="r1") is a   # 完成了也拿回同一个
    h.on_message(s, result("r1", value={"late": True}))
    assert a.wait() == {"state": "ready"}
    assert h.satellite("sim1")["counters"]["late_results"] == 1


def test_rejected_ack_finishes_with_code(h):
    s, t = attach(h, "sim1")
    c = h.submit("sim1", "ccd", "acquire")
    h.on_message(s, P.parse_to_hub({"t": "ack", "id": c.id, "state": "rejected",
                                    "reason": "operator_hold"}))
    with pytest.raises(SatelliteCommandError) as ei:
        c.wait()
    assert ei.value.code == "operator_hold"
    c2 = h.submit("sim1", "ccd", "acquire")
    h.on_message(s, result(c2.id, ok=False, code="device_busy"))
    with pytest.raises(SatelliteCommandError) as ei:
        c2.wait()
    assert ei.value.code == "device_busy" and ei.value.result["ok"] is False


def test_command_deadline_expires_and_sends_cancel(h):
    s, t = attach(h, "sim1")
    c = h.submit("sim1", "ccd", "status", timeout_s=0.05)
    assert t.of("cmd")[-1]["timeout_s"] == pytest.approx(0.05, abs=0.01)
    with pytest.raises(SatelliteTimeout) as ei:
        c.wait()                                      # 时限 = 0.05 + 2 s 宽限
    assert ei.value.command_expired is True
    assert t.of("cancel")[-1] == {"t": "cancel", "id": c.id}


def test_wait_timeout_without_expiry_keeps_command_alive(h):
    s, t = attach(h, "sim1")
    c = h.submit("sim1", "ccd", "acquire", timeout_s=60)
    with pytest.raises(SatelliteTimeout) as ei:
        c.wait(timeout_s=0.1)
    assert ei.value.command_expired is False and not c.done and not t.of("cancel")


def test_check_abort_cancels_and_reraises(h):
    s, t = attach(h, "sim1")
    c = h.submit("sim1", "ccd", "acquire", timeout_s=60)
    calls = {"n": 0}

    class Abort(Exception):
        pass

    def _check():
        calls["n"] += 1
        if calls["n"] >= 3:
            raise Abort("user stop")

    with pytest.raises(Abort):
        c.wait(check_abort=_check)
    assert t.of("cancel")[-1]["id"] == c.id
    assert calls["n"] == 3


def test_liveness_state_machine(h):
    s, t = attach(h, "sim1")
    rec = h._records["sim1"]
    base = rec.last_alive_mono
    h.tick(now=base + 1.0)
    assert h.link_state("sim1") == "connected"
    pings = t.of("ping")
    assert pings and pings[0]["n"] == 1                 # 连上就 ping 一次
    h.tick(now=base + 4.0)
    assert h.link_state("sim1") == "degraded"
    with pytest.raises(SatelliteUnavailable):
        h.submit("sim1", "ccd", "status")
    h.stop_device("sim1", "ccd")                         # 停止类命令 degraded 也发
    assert t.of("cmd")[-1]["action"] == "stop"
    # 一个 pong 救回来
    p = pings[0]
    h.on_message(s, P.parse_to_hub({"t": "pong", "n": p["n"], "t0": p["t0"], "t1": p["t0"] + 1,
                                    "t2": p["t0"] + 2}), rx_ms=p["t0"] + 3)
    assert h.link_state("sim1") == "connected"
    assert h.clock_offset_ms("sim1") == pytest.approx(0.0)
    rec.last_alive_mono = base
    h.tick(now=base + 11.0)
    assert h.link_state("sim1") == "lost" and t.closed and t.close_code == P.CLOSE_LIVENESS
    items = dispatch(h)
    states = [i[2]["state"] for i in items if i[0] == "hub_evt" and i[2]["kind"] == "link"]
    assert states[-3:] == ["degraded", "connected", "lost"]


def test_pong_with_unknown_n_or_forged_t0_is_ignored(h):
    s, t = attach(h, "sim1")
    h.tick()
    p = t.of("ping")[0]
    h.on_message(s, P.parse_to_hub({"t": "pong", "n": 999, "t0": p["t0"], "t1": 1, "t2": 2}))
    h.on_message(s, P.parse_to_hub({"t": "pong", "n": p["n"], "t0": p["t0"] - 5, "t1": 1, "t2": 2}))
    assert h.clock("sim1")["samples"] == 0


def test_hub_disable_closes_sessions(sat_root):
    write_sat_config(sat_root, "sim1")
    on = {"v": True}
    hub = SatelliteHub(data_root=sat_root, enabled_fn=lambda: on["v"])
    s, t = attach(hub, "sim1")
    on["v"] = False
    hub.tick()
    assert t.closed and t.close_code == P.CLOSE_FORBIDDEN
    assert hub.authenticate("sim1", "whatever")[0] == 401


def test_emergency_stop_all_sends_estop_and_stops_every_device(h):
    s, t = attach(h, "sim1")
    out = h.emergency_stop_all("E-STOP 按钮")
    assert out == {"sent": ["sim1"], "unreachable": [], "errors": []}
    assert t.of("estop") == [{"t": "estop", "reason": "E-STOP 按钮"}]
    assert sorted(m["device"] for m in t.of("cmd") if m["action"] == "stop") == ["ccd", "pol"]
    h.on_message(s, P.parse_to_hub({"t": "estop_ack", "stopped": ["ccd"], "errors": []}))
    assert h.satellite("sim1")["last_estop"]["ack"]["stopped"] == ["ccd"]


def test_estop_ack_names_the_devices_that_did_not_stop(sat_root):
    """真卫星上 xyz（Motion_Manipulator）没有远程停止：急停回执把它列进 errors。枢纽要把
    **设备名**交给监听者、EventBus 和 LiveHub，而不是只给个计数 —— 否则界面上是「急停已执行」，
    台子却可能还在动。"""
    from mast.core.events import EventType
    from mast.live.hub import STATUS_LEVELS

    class _Bus:
        def __init__(self):
            self.events = []

        def publish(self, ev):
            self.events.append(ev)

    write_sat_config(sat_root, "sim1")
    live, bus = FakeLive(), _Bus()
    hub = SatelliteHub(data_root=sat_root, enabled_fn=lambda: True, live=live, bus=bus)
    try:
        s, t = attach(hub, "sim1")
        seen = []
        hub.add_listener(lambda sid, e: seen.append(e))
        hub.emergency_stop_all("E-STOP 按钮")
        hub.on_message(s, P.parse_to_hub({"t": "estop_ack", "stopped": ["ccd", "pol"], "errors": [
            {"device": "xyz", "message": "Motion_Manipulator 没有远程停止：请在它的窗口里按 STOP"}]}))
        dispatch(hub)
        ack = [e["data"] for e in seen if e["kind"] == "estop_ack"]
        assert len(ack) == 1
        assert ack[0]["error_devices"] == ["xyz"] and ack[0]["errors"] == 1
        assert ack[0]["first_error"].startswith("xyz: ") and "按 STOP" in ack[0]["first_error"]
        on_bus = [e.data for e in bus.events
                  if e.type == EventType.SATELLITE and e.data.get("kind") == "estop_ack"]
        assert on_bus and on_bus[0]["error_devices"] == ["xyz"] and on_bus[0]["sat_id"] == "sim1"
        frame = live.frames["sim1/events"][-1]
        assert frame["level"] == "error" and frame["level"] in STATUS_LEVELS
        assert "xyz" in frame["text"] and "按 STOP" in frame["text"]
        # REST 那边（last_estop）仍是卫星给的原样
        assert hub.satellite("sim1")["last_estop"]["ack"]["errors"] == [
            {"device": "xyz", "message": "Motion_Manipulator 没有远程停止：请在它的窗口里按 STOP"}]
    finally:
        hub.shutdown(close_sessions=False)


def test_clean_estop_ack_is_info(h):
    s, t = attach(h, "sim1")
    seen = []
    h.add_listener(lambda sid, e: seen.append(e))
    h.on_message(s, P.parse_to_hub({"t": "estop_ack", "stopped": ["ccd", "pol"], "errors": []}))
    dispatch(h)
    ack = [e["data"] for e in seen if e["kind"] == "estop_ack"]
    assert ack == [{"kind": "estop_ack", "stopped": ["ccd", "pol"], "errors": 0,
                    "error_devices": [], "first_error": ""}]
    frame = h.bridge._live.frames["sim1/events"][-1]
    assert frame["level"] == "info" and "没停下来" not in frame["text"]


def _safe_cmds(t) -> list[dict]:
    return [m for m in t.of("cmd") if m["action"] == "safe"]


def test_safe_all_waits_for_the_satellite_and_reports_what_stopped(h):
    """关停用的 safe 要等卫星确认再返回：只入队就关会话，对端收没收到、停没停，谁也说不上来。"""
    s, t = attach(h, "sim1")
    box: dict = {}
    th = threading.Thread(target=lambda: box.setdefault("out", h.safe_all("MAST 关停", wait_s=3.0)))
    th.start()
    deadline = time.monotonic() + 3
    while len(_safe_cmds(t)) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    cmds = _safe_cmds(t)
    assert {m["device"] for m in cmds} == {"ccd", "pol"}
    assert th.is_alive()                                  # 还在等回话
    for m in cmds:
        h.on_message(s, result(m["id"], value={"stopped": ["ccd"] if m["device"] == "ccd" else []}))
    th.join(5)
    out = box["out"]
    assert sorted(out["confirmed"]["sim1"]) == ["ccd", "pol"]
    assert out["stopped"] == {"sim1": ["ccd"]} and out["unconfirmed"] == {}


def test_safe_all_gives_up_after_wait_s_and_names_who_did_not_answer(h, caplog):
    import logging

    attach(h, "sim1")
    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="mast.satellite.hub"):
        out = h.safe_all("MAST 关停", wait_s=0.3)
    assert time.monotonic() - t0 < 1.5
    assert sorted(out["unconfirmed"]["sim1"]) == ["ccd", "pol"] and out["confirmed"] == {}
    assert any("没回音" in r.getMessage() for r in caplog.records)


def test_safe_for_shutdown_runs_once_and_later_callers_get_the_same_answer(h):
    """关停端点先调、runtime 的关停钩子兜底再调：第二次不重发，拿第一次的结果。"""
    s, t = attach(h, "sim1")
    first = h.safe_for_shutdown("MAST 关停", wait_s=0.2)      # 没人回话：0.2 s 后照常返回
    assert len(_safe_cmds(t)) == 2
    assert sorted(first["unconfirmed"]["sim1"]) == ["ccd", "pol"]
    again = h.safe_for_shutdown("MAST 关停")
    assert again["already"] is True and again["unconfirmed"] == first["unconfirmed"]
    assert len(_safe_cmds(t)) == 2                             # 没重发


def test_concurrent_shutdown_safes_send_once(h):
    s, t = attach(h, "sim1")
    outs: list = []
    ths = [threading.Thread(target=lambda: outs.append(h.safe_for_shutdown("x", wait_s=0.3)))
           for _ in range(2)]
    for th in ths:
        th.start()
    for th in ths:
        th.join(5)
    assert len(outs) == 2 and len(_safe_cmds(t)) == 2
    assert sum(1 for o in outs if o.get("already")) == 1


def test_after_shutdown_begins_the_hub_sends_nothing(h):
    """关停开始后一律不发：本机光学台的 stop_all 经远程驱动补发的 stop 会打断卫星上的人工采集；
    第二遍关停里的 safe_all 也不该再报「连不上」。"""
    s, t = attach(h, "sim1")
    h.shutdown(close_sessions=False)
    n = len(t.sent)
    with pytest.raises(SatelliteUnavailable, match="关停"):
        h.submit("sim1", "ccd", "status")
    h.stop_device("sim1", "ccd")                          # 不抛，也不发
    out = h.safe_all("MAST 关停", wait_s=1.0)
    assert out.get("skipped") is True and out["sent"] == [] and out["unreachable"] == []
    assert len(t.sent) == n
    # 卫星这时重连：拒（503），枢纽不被这条连接重新拉起来；显式 start() 才重启
    assert h.authenticate("sim1", "whatever")[0] == 503
    h.ensure_started()
    assert h.started is False
    with pytest.raises(SatelliteUnavailable):
        h.submit("sim1", "ccd", "status")


def test_emergency_stop_all_never_raises(h, monkeypatch):
    s, t = attach(h, "sim1")

    def boom(*a, **k):
        raise RuntimeError("transport exploded")

    monkeypatch.setattr(t, "send", boom)
    out = h.emergency_stop_all("x")
    assert out["errors"] and out["sent"] == []


def test_listener_errors_do_not_stop_dispatch(h):
    s, t = attach(h, "sim1")
    seen = []
    h.add_listener(lambda sid, e: 1 / 0)
    handle = h.add_listener(lambda sid, e: seen.append(e["kind"]))
    h.on_message(s, evt(1, "ccd", "acq_started", {"cmd_id": "x"}))
    dispatch(h)
    assert "acq_started" in seen
    h.remove_listener(handle)
    h.on_message(s, evt(2, "ccd", "acq_completed", {}))
    dispatch(h)
    assert "acq_completed" not in seen


def test_device_state_changes_publish_only_notable_transitions(h):
    from mast.core.events import EventBus, EventType

    bus_events = []

    def _cb(ev):
        if ev.type is EventType.SATELLITE:
            bus_events.append(ev.data)

    EventBus.get().subscribe(_cb)
    try:
        s, t = attach(h, "sim1")
        dispatch(h)
        assert [e["kind"] for e in bus_events] == ["link"]
        bus_events.clear()
        for seq, st in enumerate(["busy", "ready", "busy", "ready"], start=1):
            h.on_message(s, evt(seq, "ccd", "device_state", {"state": st}))
        dispatch(h)
        assert bus_events == []                      # busy↔ready 不进 EventBus
        h.on_message(s, evt(5, "ccd", "device_state",
                            {"state": "operator_hold", "operator_hold": True}))
        dispatch(h)
        assert [e["kind"] for e in bus_events] == ["device"]
        assert bus_events[0]["operator_hold"] is True and bus_events[0]["sat_id"] == "sim1"
    finally:
        EventBus.get().unsubscribe(_cb)


def test_state_persists_boot_id_and_cursor_across_hub_restart(sat_root):
    write_sat_config(sat_root, "sim1")
    a = SatelliteHub(data_root=sat_root, enabled_fn=lambda: True)
    s, t = attach(a, "sim1", boot="A")
    for seq in range(1, 4):
        a.on_message(s, evt(seq, "pol", "axis_state", {}))
    a._save_state(force=True)
    b = SatelliteHub(data_root=sat_root, enabled_fn=lambda: True)
    b._load_persisted_state()
    s2, t2 = attach(b, "sim1", boot="A", evt_seq=3)
    assert t2.sent[0]["evt_ack"] == 3                # 枢纽重启、卫星没重启：游标接着用
    c = SatelliteHub(data_root=sat_root, enabled_fn=lambda: True)
    c._load_persisted_state()
    s3, t3 = attach(c, "sim1", boot="Z")
    assert t3.sent[0]["evt_ack"] == 0 and c.satellite("sim1")["counters"]["restarts"] == 1


def test_unknown_satellite_and_bad_args(h):
    with pytest.raises(SatelliteUnavailable):
        h.submit("nosuch", "ccd", "status")
    s, t = attach(h, "sim1")
    with pytest.raises(SatelliteCommandError):
        h.submit("sim1", "ccd", "status", timeout_s=0)
    with pytest.raises(SatelliteCommandError):
        h.submit("sim1", "", "status")


def test_call_from_another_thread(h):
    s, t = attach(h, "sim1")
    out = {}

    def _caller():
        out["v"] = h.call("sim1", "ccd", "status", timeout_s=5)

    th = threading.Thread(target=_caller)
    th.start()
    deadline = time.monotonic() + 2
    while not t.of("cmd") and time.monotonic() < deadline:
        time.sleep(0.01)
    h.on_message(s, result(t.of("cmd")[-1]["id"], value={"ok": 1}))
    th.join(2)
    assert out["v"] == {"ok": 1}
