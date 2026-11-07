"""C# 卫星（satellite/MastSatellite）对真枢纽的端到端测试。

起本机枢纽（进程内 uvicorn，随机端口，测试令牌，见 conftest），以模拟设备模式起
``MastSatellite.exe --headless``，走真 WebSocket 覆盖：采谱 → 实时帧 → 文件上传（含续传）→
断线重连 + 死人开关 + cmd_status 找回结局 → ``boot_id`` 变化判 lost_on_restart → E-STOP →
MAST 正常关停的 safe（人工采集不打断）→ 人工接管下放行 safe → TLS 公钥钉值。每个测试结束用
``--stop`` 优雅停掉 exe（模拟蓝屏的那一步除外，那是故意强杀）。

exe 不存在（没构建）或不在 Windows 上就整体跳过。构建：satellite/README.md。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from tests.v2.satellite.conftest import start_server, stop_server, wait_for, write_sat_config

REPO = Path(__file__).resolve().parents[3]
EXE = REPO / "satellite" / "MastSatellite" / "bin" / "x64" / "Release" / "MastSatellite.exe"

pytestmark = [
    pytest.mark.skipif(os.name != "nt", reason="C# 卫星只在 Windows 上跑"),
    pytest.mark.skipif(not EXE.exists(), reason=f"C# 卫星未构建：{EXE}"),
]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class CSharpSat:
    """一个 MastSatellite.exe 进程：写配置、DPAPI 令牌，起停，收集它的日志（stdout）。"""

    def __init__(self, tmp_path: Path, sat_id: str, url: str, token: str, devices: list[dict], *,
                 spki: str = "", plain: bool = True, upload: dict | None = None, name: str | None = None):
        self.sat_id = sat_id
        self.plain = plain
        self.state = tmp_path / f"cs-{name or sat_id}"
        self.state.mkdir(parents=True, exist_ok=True)
        self.token_file = self.state / "token.dpapi"
        self.cfg_path = self.state / "satellite.json"
        cfg = {
            "satellite_id": sat_id,
            "hub": {"url": url, "spki_sha256": spki, "token_file": str(self.token_file),
                    "backoff_max_s": 1.0, "auth_retry_s": 1.0, "connect_timeout_s": 5},
            "state_dir": str(self.state),
            "upload": upload or {"max_bytes_per_s": 4 << 20, "chunk_bytes": 64 * 1024},
            "log": {"level": "debug"},
            "devices": devices,
        }
        self.cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
        r = subprocess.run([str(EXE), "--protect-token", "--out", str(self.token_file)], input=token,
                           text=True, capture_output=True, timeout=60, creationflags=_NO_WINDOW)
        assert r.returncode == 0, r.stdout + r.stderr
        self.proc: subprocess.Popen | None = None
        self.lines: list[str] = []

    def start(self) -> "CSharpSat":
        args = [str(EXE), "--config", str(self.cfg_path), "--headless"]
        if self.plain:
            args.append("--allow-plain-loopback")
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)

        def _pump(p=self.proc):
            for raw in iter(p.stdout.readline, b""):
                self.lines.append(raw.decode("utf-8", errors="replace").rstrip())
        threading.Thread(target=_pump, daemon=True).start()
        return self

    def log(self, tail: int = 80) -> str:
        return "\n".join(self.lines[-tail:])

    def stop(self, timeout: float = 45.0) -> int | None:
        """优雅停止：``--stop`` 让它停设备、发完结果、以 1000 关掉 WebSocket 再退出。"""
        p = self.proc
        if p is None or p.poll() is not None:
            return None if p is None else p.returncode
        subprocess.run([str(EXE), "--stop", "--config", str(self.cfg_path), "--timeout", "30"],
                       capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW)
        try:
            return p.wait(timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(10)
            raise AssertionError("MastSatellite did not exit after --stop:\n" + self.log())

    def crash(self) -> None:
        """模拟实验机蓝屏：不给任何收尾的机会。"""
        assert self.proc is not None
        self.proc.kill()
        self.proc.wait(15)


@pytest.fixture
def csharp(tmp_path):
    made: list[CSharpSat] = []

    def _make(*a, **kw) -> CSharpSat:
        s = CSharpSat(tmp_path, *a, **kw)
        made.append(s)
        return s

    yield _make
    errors = []
    for s in made:
        try:
            rc = s.stop()
            if rc not in (None, 0):
                errors.append(f"{s.sat_id}: exit code {rc}\n{s.log(40)}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{s.sat_id}: {exc}")
    assert not errors, "\n\n".join(errors)


def _devices(watch: Path, **over) -> list[dict]:
    devs = [
        {"id": "ccd", "driver": "lightfield", "sim": True, "readout_ms": 5, "data_dir": str(watch)},
        {"id": "pol", "driver": "kinesis_kdc101", "sim": True, "speed_deg_s": 60, "home_time_s": 0.5},
        {"id": "pico", "driver": "picomotor8742", "sim": True, "steps_per_s": 5000},
        {"id": "xyz", "driver": "motion_manipulator", "sim": True, "speed_mm_s": 5},
        {"id": "spe", "driver": "spe_watcher", "root": str(watch), "stable_s": 0.5, "scan_interval_s": 2,
         "backfill_hours": 24},
    ]
    for d in devs:
        d.update(over.get(d["id"], {}))
    return devs


def _connected(hub, sat_id: str, sat: CSharpSat, timeout: float = 30.0) -> None:
    try:
        wait_for(lambda: hub.link_state(sat_id) == "connected", timeout=timeout, msg=f"{sat_id} connected")
    except AssertionError:
        raise AssertionError("satellite did not connect; its log:\n" + sat.log())


# ── 采谱、实时帧、文件、运动、幂等、优雅停止 ─────────────────────────────────

def test_csharp_satellite_end_to_end(hub, server, sat_root, csharp, tmp_path):
    from mast.satellite.errors import SatelliteCommandError

    token = write_sat_config(sat_root, "cs1")
    hub._load_config(force=True)
    watch = tmp_path / "watch"
    watch.mkdir()
    got: list[dict] = []
    hub.add_listener(lambda sid, e: got.append(e))
    sat = csharp("cs1", server.ws_url, token, _devices(watch)).start()
    _connected(hub, "cs1", sat)

    info = hub.satellite("cs1")
    assert {d["id"] for d in info["devices"]} == {"ccd", "pol", "pico", "xyz", "spe"}
    assert {d["kind"] for d in info["devices"]} >= {"spectrometer", "rotation_stage", "openloop_stepper",
                                                    "xyz_stage", "file_watcher"}
    boot = info["boot_id"]
    wait_for(lambda: hub.clock_offset_ms("cs1") is not None, msg="clock sample")
    assert abs(hub.clock_offset_ms("cs1")) < 50                    # 同一台机器

    st = hub.call("cs1", "ccd", "status", timeout_s=10)
    assert st["state"] == "ready" and st["laser_line_nm"] == 633.0 and st["temperature_locked"] is True
    res = hub.call("cs1", "ccd", "acquire", {"exposure_ms": 50, "frames": 3, "save": True}, timeout_s=30)
    assert res["frames"] == 3 and len(res["files"]) == 1
    frames = wait_for(lambda: [e for e in got if e["kind"] == "spectrum" and e["data"].get("cmd_id")],
                      msg="spectra")
    assert len(frames) == 3
    assert all(e["data"]["n"] == 1340 and e["data"]["dtype"] == "u16" for e in frames)
    assert all("ts_hub_ms" in e and "clock_offset_ms" in e for e in frames)

    hub.drain()
    live = hub.test_live
    wait_for(lambda: len(live.frames.get("cs1/ccd/spectrum", [])) >= 3, msg="live frames")
    x, unit = live.axes["cs1/ccd/spectrum"][-1]
    assert unit == "nm" and len(x) == 1340 and 630 < x[0] < x[-1] < 700
    assert len(live.frames["cs1/ccd/spectrum"][-1]["y"]) == 1340

    rows = wait_for(lambda: hub.files().list("cs1"), timeout=30, msg="stored SPE")
    row = rows[0]
    assert (sat_root / row["stored_rel"]).stat().st_size == row["size"]
    meta = hub.files().meta(row["file_id"])
    assert meta["spe"]["ok"] is True
    assert meta["spe"]["key"]["frames"] == 3 and meta["spe"]["key"]["laser_line_nm"] == 633.0
    assert meta["times"]["source"] == "satellite_meta"           # 卫星记下的采集时间窗
    sm = meta["satellite_meta"]
    assert sm["device"] == "ccd" and sm["frames"] == 3 and sm["manual"] is False
    assert res["started_ms"] <= sm["acq_started_ms"] <= sm["acq_finished_ms"] <= res["finished_ms"] + 1000

    # 偏振台：上电后没回零不给绝对角度；回零后走到位；软限位
    with pytest.raises(SatelliteCommandError) as ei:
        hub.call("cs1", "pol", "move_abs", {"deg": 10.0}, timeout_s=10)
    assert ei.value.code == "unavailable"
    hub.call("cs1", "pol", "home", timeout_s=30)
    out = hub.call("cs1", "pol", "move_abs", {"deg": 45.0}, timeout_s=30)
    assert out["position_deg"] == pytest.approx(45.0) and out["moving"] is False and out["homed"] is True
    wait_for(lambda: (hub.latest("cs1", "pol", "axis_state") or {}).get("position_deg") == pytest.approx(45.0),
             msg="axis_state 45")
    with pytest.raises(SatelliteCommandError) as ei:
        hub.call("cs1", "pol", "move_abs", {"deg": 9999.0}, timeout_s=10)
    assert ei.value.code == "limit"
    # 契约 §5.3 第 11 条：枢纽发起的转动期间 axis_state 带 cmd_id（枢纽据此显示 running），
    # 转完立刻补一条 moving=false、不带 cmd_id 的
    mvc = hub.submit("cs1", "pol", "move_rel", {"deg": 120.0}, timeout_s=30)   # 60°/s，约 2 s
    wait_for(lambda: mvc.state == "running", msg="move shown as running")
    assert (hub.latest("cs1", "pol", "axis_state") or {}).get("cmd_id") == mvc.id
    assert mvc.wait(timeout_s=30)["position_deg"] == pytest.approx(165.0)
    last = wait_for(lambda: (lambda a: a if a and a.get("moving") is False else None)(
        hub.latest("cs1", "pol", "axis_state")), msg="axis_state after the move")
    assert "cmd_id" not in last and last["position_deg"] == pytest.approx(165.0)
    out = hub.call("cs1", "pico", "jog", {"axis": "1", "steps": 100}, timeout_s=10)
    assert out["axes"]["1"]["steps"] == 100

    # 契约 §13.1 手动面板的新动作：caps 如实列出，参数名和返回形状按 §5.4
    caps = {d["id"]: set(d["caps"]) for d in hub.satellite("cs1")["devices"]}
    assert {"set_velocity", "identify"} <= caps["pol"]
    assert {"move_abs", "set_velocity", "set_accel", "abort"} <= caps["pico"]
    out = hub.call("cs1", "pol", "set_velocity", {"deg_s": 20.0, "accel_deg_s2": 15.0}, timeout_s=10)
    assert out["velocity_deg_s"] == pytest.approx(20.0) and out["accel_deg_s2"] == pytest.approx(15.0)
    assert "position_deg" in hub.call("cs1", "pol", "identify", timeout_s=10)
    with pytest.raises(SatelliteCommandError) as ei:
        hub.call("cs1", "pol", "set_velocity", {"deg_s": 99.0}, timeout_s=10)
    assert ei.value.code == "invalid_args"                                   # 速度越界：和参考卫星一致
    hub.call("cs1", "pico", "set_zero", {"axis": "3"}, timeout_s=10)
    assert hub.call("cs1", "pico", "move_abs", {"axis": "3", "steps": 120}, timeout_s=10)["axes"]["3"]["steps"] == 120
    assert hub.call("cs1", "pico", "move_abs", {"axis": "3", "steps": 20}, timeout_s=10)["axes"]["3"]["steps"] == 20
    out = hub.call("cs1", "pico", "set_velocity", {"axis": "3", "steps_s": 100}, timeout_s=10)
    assert out["axes"]["3"]["velocity_steps_s"] == 100
    out = hub.call("cs1", "pico", "set_accel", {"axis": "3", "steps_s2": 2000}, timeout_s=10)
    assert out["axes"]["3"]["accel_steps_s2"] == 2000
    long_jog = hub.submit("cs1", "pico", "jog", {"axis": "3", "steps": 1500}, timeout_s=60)   # 15 s at 100 steps/s
    wait_for(lambda: ((hub.latest("cs1", "pico", "axis_state") or {}).get("axes") or {}).get("3", {}).get("moving") is True,
             msg="pico axis 3 moving")
    t0 = time.monotonic()
    out = hub.call("cs1", "pico", "abort", timeout_s=10)                     # 设备在忙也收；所有轴立即停，不减速
    with pytest.raises(SatelliteCommandError) as ei:
        long_jog.wait(timeout_s=10)
    assert ei.value.code == "cancelled" and time.monotonic() - t0 < 5
    assert out["axes"]["3"]["moving"] is False

    # request_id 幂等：枢纽忘掉之后用同一 id 再发，卫星按 journal 回 duplicate_done，不会再走 50 步
    rid = str(uuid.uuid4())
    a = hub.submit("cs1", "pico", "jog", {"axis": "2", "steps": 50}, request_id=rid)
    assert a.wait(timeout_s=10)["axes"]["2"]["steps"] == 50
    with hub._lock:
        hub._cmds.pop(rid, None)
    c = hub.submit("cs1", "pico", "jog", {"axis": "2", "steps": 50}, request_id=rid)
    assert c.wait(timeout_s=10)["axes"]["2"]["steps"] == 50 and c.ack_state == "duplicate_done"
    assert hub.call("cs1", "pico", "status", timeout_s=10)["axes"]["2"]["steps"] == 50
    with pytest.raises(SatelliteCommandError) as ei:
        hub.call("cs1", "nosuch", "status", timeout_s=10)
    assert ei.value.code == "invalid_args"
    assert hub.satellite("cs1")["boot_id"] == boot

    rc = sat.stop()
    assert rc == 0, sat.log()
    wait_for(lambda: hub.link_state("cs1") == "lost", timeout=10, msg="link closed after --stop")


# ── 断点续传 ────────────────────────────────────────────────────────────────

def test_csharp_file_resume_after_disconnect(hub, server, sat_root, csharp, tmp_path):
    token = write_sat_config(sat_root, "cs2", max_upload_bytes_per_s=200_000)
    hub._load_config(force=True)
    watch = tmp_path / "watch2"
    watch.mkdir()
    sat = csharp("cs2", server.ws_url, token, [d for d in _devices(watch) if d["id"] == "spe"]).start()
    _connected(hub, "cs2", sat)
    blob = os.urandom(600_000)
    (watch / "big.csv").write_bytes(blob)
    wait_for(lambda: hub._receiver.stats["bytes"] >= 150_000, timeout=30, msg="first part received")
    assert hub.drop_session("cs2", code=4000, reason="test: drop the link mid-upload")
    wait_for(lambda: hub.satellite("cs2")["counters"]["connects"] >= 2, timeout=30, msg="reconnected")
    rows = wait_for(lambda: hub.files().list("cs2"), timeout=60, msg="stored after resume")
    assert (sat_root / rows[0]["stored_rel"]).read_bytes() == blob
    assert hub._receiver.stats["resumed"] >= 1
    assert hub._receiver.stats["bytes"] < 2 * len(blob)           # 续传，不是从 0 重发


# ── 断线：死人开关自己停下，重连后 cmd_status 找回结局 ─────────────────────────

def test_csharp_deadman_and_recovery_via_cmd_status(hub, server, sat_root, csharp, tmp_path):
    from mast.satellite.errors import SatelliteCommandError

    token = write_sat_config(sat_root, "cs3")                     # deadman_s = 1.0（conftest.FAST）
    hub._load_config(force=True)
    got: list[dict] = []
    hub.add_listener(lambda sid, e: sid == "cs3" and got.append(e))
    devs = [{"id": "pol", "driver": "kinesis_kdc101", "sim": True, "speed_deg_s": 10, "sim_start_homed": True}]
    sat = csharp("cs3", server.ws_url, token, devs).start()
    _connected(hub, "cs3", sat)
    boot = hub.satellite("cs3")["boot_id"]
    cmd = hub.submit("cs3", "pol", "move_rel", {"deg": 300.0}, timeout_s=120)   # 30 s 的运动
    wait_for(lambda: cmd.state in ("accepted", "running"), msg="accepted")
    time.sleep(0.5)
    hub.test_enabled["on"] = False            # 枢纽断开会话并拒绝重连（403）：卫星收不到 ping
    wait_for(lambda: hub.link_state("cs3") != "connected", timeout=10, msg="link down")
    time.sleep(3.0)                           # > deadman_s：卫星自己停轴，结局只在它的 journal 里
    assert not cmd.done
    hub.test_enabled["on"] = True
    with pytest.raises(SatelliteCommandError) as ei:
        cmd.wait(timeout_s=30)
    assert ei.value.code == "cancelled" and "deadman" in str(ei.value)
    hub.drain()
    dm = wait_for(lambda: [e for e in got if e["kind"] == "deadman"], timeout=15, msg="deadman event")
    assert "pol" in dm[0]["data"]["stopped"] and dm[0]["device"] == ""
    pos = (hub.latest("cs3", "pol", "axis_state") or {}).get("position_deg")
    assert pos is not None and 0 < pos < 100                      # 远没走完 300°
    info = hub.satellite("cs3")
    assert info["boot_id"] == boot and info["counters"]["restarts"] == 0
    seqs = [e["seq"] for e in got if e.get("seq")]
    assert seqs == list(range(seqs[0], seqs[-1] + 1))             # 不重不漏


# ── 重启（蓝屏）：在途命令 lost_on_restart，不重放 ────────────────────────────

def test_csharp_boot_id_change_marks_lost_on_restart(hub, server, sat_root, csharp, tmp_path):
    from mast.satellite.errors import SatelliteCommandError

    token = write_sat_config(sat_root, "cs4")
    hub._load_config(force=True)
    devs = [{"id": "ccd", "driver": "lightfield", "sim": True, "readout_ms": 5}]
    sat = csharp("cs4", server.ws_url, token, devs).start()
    _connected(hub, "cs4", sat)
    boot_a = hub.satellite("cs4")["boot_id"]
    cmd = hub.submit("cs4", "ccd", "acquire", {"exposure_ms": 20000, "frames": 1, "save": False}, timeout_s=60)
    wait_for(lambda: cmd.state in ("accepted", "running"), msg="accepted")
    sat.crash()
    wait_for(lambda: hub.link_state("cs4") == "lost", timeout=20, msg="lost")
    sat.start()                                                   # 同一状态目录，新 boot_id
    with pytest.raises(SatelliteCommandError) as ei:
        cmd.wait(timeout_s=30)
    assert ei.value.code == "lost_on_restart"
    info = hub.satellite("cs4")
    assert info["boot_id"] != boot_a and info["counters"]["restarts"] == 1
    # 卫星 journal：重启前在跑的条目判 lost_on_restart，同一 id 再发如实回答，不重跑
    with pytest.raises(SatelliteCommandError) as ei2:
        hub.call("cs4", "ccd", "acquire", {"exposure_ms": 10}, request_id=cmd.id, timeout_s=10)
    assert ei2.value.code == "lost_on_restart"


# ── E-STOP ──────────────────────────────────────────────────────────────────

def test_csharp_estop_stops_everything(hub, server, sat_root, csharp, tmp_path):
    from mast.satellite.errors import SatelliteCommandError

    token = write_sat_config(sat_root, "cs6")
    hub._load_config(force=True)
    devs = [{"id": "ccd", "driver": "lightfield", "sim": True, "readout_ms": 5},
            {"id": "pol", "driver": "kinesis_kdc101", "sim": True, "speed_deg_s": 10, "sim_start_homed": True}]
    sat = csharp("cs6", server.ws_url, token, devs).start()
    _connected(hub, "cs6", sat)
    acq = hub.submit("cs6", "ccd", "acquire", {"exposure_ms": 60000, "save": False}, timeout_s=90)
    mv = hub.submit("cs6", "pol", "move_rel", {"deg": 300.0}, timeout_s=90)
    wait_for(lambda: acq.state in ("accepted", "running") and mv.state in ("accepted", "running"), msg="busy")
    time.sleep(0.3)
    out = hub.emergency_stop_all("test estop")
    assert out["sent"] == ["cs6"] and out["errors"] == []
    for c in (acq, mv):
        with pytest.raises(SatelliteCommandError) as ei:
            c.wait(timeout_s=15)
        assert ei.value.code == "cancelled"
    est = wait_for(lambda: (hub.satellite("cs6")["last_estop"] or {}).get("ack"), msg="estop_ack")
    assert set(est["stopped"]) >= {"ccd", "pol"}          # 每个停成功的设备，空闲的也算
    assert est["errors"] == []
    # active 只作信息：枢纽同时给每个设备发 stop，谁先打断作业是竞态，所以这里只查形状
    assert isinstance(est.get("active"), list) and set(est["active"]) <= set(est["stopped"])


# ── MAST 正常关停：safe 只停枢纽发起的，人工采集不打断（不是 estop） ─────────────

def test_csharp_safe_all_spares_manual_acquisition(hub, server, sat_root, csharp, tmp_path):
    from mast.satellite.errors import SatelliteCommandError

    token = write_sat_config(sat_root, "cs7")
    hub._load_config(force=True)
    got: list[dict] = []
    hub.add_listener(lambda sid, e: got.append(e) if sid == "cs7" else None)
    # 模拟驱动每隔 sim_manual_every_s 自己起一次"有人在 LightField 窗口里点了采集"（约 6 s 长）
    devs = [{"id": "ccd", "driver": "lightfield", "sim": True, "readout_ms": 5,
             "sim_manual_every_s": 0.3, "sim_manual_frames": 60, "sim_manual_exposure_ms": 100},
            {"id": "pol", "driver": "kinesis_kdc101", "sim": True, "speed_deg_s": 10, "sim_start_homed": True}]
    sat = csharp("cs7", server.ws_url, token, devs).start()
    _connected(hub, "cs7", sat)
    mv = hub.submit("cs7", "pol", "move_rel", {"deg": 300.0}, timeout_s=90)
    wait_for(lambda: mv.state in ("accepted", "running"), msg="hub move accepted")
    wait_for(lambda: (hub.latest("cs7", "pol", "axis_state") or {}).get("moving") is True, msg="pol moving")

    def fresh_manual():
        now = time.time() * 1000
        return [e for e in got if e["kind"] == "acq_started" and e["data"].get("manual")
                and now - e["data"]["started_ms"] < 1500]

    wait_for(fresh_manual, timeout=20, msg="a manual acquisition just started")
    t_safe = time.time() * 1000
    out = hub.safe_all("MAST 关停")
    assert out["sent"] == ["cs7"] and out["errors"] == []
    ids = out["commands"]["cs7"]
    assert set(ids) == {"ccd", "pol"}
    assert hub._cmds[ids["pol"]].wait(timeout_s=15) == {"stopped": ["pol"]}   # 枢纽发起的转动停了
    assert hub._cmds[ids["ccd"]].wait(timeout_s=15) == {"stopped": []}        # 人工采集不归枢纽管
    with pytest.raises(SatelliteCommandError) as ei:
        mv.wait(timeout_s=15)
    assert ei.value.code == "cancelled" and "MAST 关停" in ei.value.message

    def finished():  # 关停那一刻正在跑的那次人工采集照常采完
        return [e for e in got if e["kind"] == "acq_completed" and e["data"].get("manual")
                and e["data"]["started_ms"] <= t_safe <= e["data"]["finished_ms"]]

    done = wait_for(finished, timeout=20, msg="the manual acquisition ran to the end")
    assert done[0]["data"]["aborted"] is False and done[0]["data"]["frames"] == 60
    assert hub.satellite("cs7")["last_estop"] is None                         # 走的是 safe，不是 estop


# ── 人工接管：safe 放行（stop 的子集），其余动作拒收 ──────────────────────────

def test_csharp_operator_hold_admits_safe(hub, server, sat_root, csharp, tmp_path):
    from mast.satellite.errors import SatelliteCommandError

    token = write_sat_config(sat_root, "cs8")
    hub._load_config(force=True)
    devs = [{"id": "ccd", "driver": "lightfield", "sim": True, "readout_ms": 5, "operator_hold": True},
            {"id": "pico", "driver": "picomotor8742", "sim": True, "operator_hold": True}]
    sat = csharp("cs8", server.ws_url, token, devs).start()
    _connected(hub, "cs8", sat)
    # abort 和 stop 同列：接管期间也放行（只会让运动停下）；运动类动作照样拒收
    assert hub.call("cs8", "pico", "abort", timeout_s=10)["axes"]["1"]["moving"] is False
    with pytest.raises(SatelliteCommandError) as ei:
        hub.call("cs8", "pico", "jog", {"axis": "1", "steps": 10}, timeout_s=10)
    assert ei.value.code == "operator_hold"
    st = hub.call("cs8", "ccd", "status", timeout_s=10)
    assert st["operator_hold"] is True and st["state"] == "operator_hold"
    assert hub.call("cs8", "ccd", "safe", {"reason": "t"}, timeout_s=10) == {"stopped": []}
    out = hub.safe_all("MAST 关停")
    assert hub._cmds[out["commands"]["cs8"]["ccd"]].wait(timeout_s=10) == {"stopped": []}
    assert hub.call("cs8", "ccd", "stop", timeout_s=10)["stopped"] is True
    with pytest.raises(SatelliteCommandError) as ei:
        hub.call("cs8", "ccd", "acquire", {"frames": 1, "save": False}, timeout_s=10)
    assert ei.value.code == "operator_hold"


# ── TLS：SPKI 钉值（与 admin show-pin 用的 mast.net.tls.spki_sha256_of 同一定义） ──

def _self_signed(tmp_path: Path) -> tuple[Path, Path, bytes]:
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mast-test-hub")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    kp = tmp_path / "hub.key"
    cp = tmp_path / "hub.crt"
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    cp.write_bytes(pem)
    return kp, cp, pem


def test_csharp_tls_spki_pinning(hub, sat_root, csharp, tmp_path):
    from fastapi import FastAPI

    from mast.api.context import AppContext
    from mast.api.routes import satellites
    from mast.net.tls import spki_sha256_of

    kp, cp, pem = _self_signed(tmp_path)
    app = FastAPI()
    app.state.ctx = AppContext()
    app.include_router(satellites.router)
    srv = start_server(app, ssl_kw={"ssl_keyfile": str(kp), "ssl_certfile": str(cp)})
    try:
        url = f"wss://127.0.0.1:{srv.port}/ws/satellite/v1"
        pin = spki_sha256_of(pem)
        token = write_sat_config(sat_root, "cs5")
        hub._load_config(force=True)
        devs = [{"id": "pico", "driver": "picomotor8742", "sim": True}]
        good = csharp("cs5", url, token, devs, spki=pin, plain=False).start()
        _connected(hub, "cs5", good)
        assert good.stop() == 0
        wait_for(lambda: hub.link_state("cs5") == "lost", timeout=10, msg="closed")
        connects = hub.satellite("cs5")["counters"]["connects"]
        bad = csharp("cs5", url, token, devs, spki="0" * 64, plain=False, name="cs5-badpin").start()
        wait_for(lambda: any("pin mismatch" in ln for ln in bad.lines), timeout=20, msg="pin mismatch logged")
        time.sleep(1.0)
        assert hub.satellite("cs5")["counters"]["connects"] == connects   # 令牌从没发出去
        assert hub.link_state("cs5") != "connected"
        assert bad.stop() == 0
    finally:
        stop_server(srv)
