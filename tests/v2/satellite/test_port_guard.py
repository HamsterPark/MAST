"""端口归属检查（core/port_guard.py）：分类、放行名单、enforce、告警；Windows 句柄枚举。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import namedtuple
from types import SimpleNamespace

import pytest

from mast.core import port_guard as pg

Addr = namedtuple("Addr", "ip port")
Conn = namedtuple("Conn", "fd family type laddr raddr status pid")

OWN = 1000
NANONIS = 2000
LABVIEW = 3000
OTHER_MAST = 4000

PROCS = {OWN: "MAST.exe", NANONIS: "Nanonis Mimea.exe", LABVIEW: "LabVIEW.exe",
         OTHER_MAST: "MAST.exe"}


def c(lip, lport, rip=None, rport=None, status="ESTABLISHED", pid=None):
    return Conn(-1, 2, 1, Addr(lip, lport), Addr(rip, rport) if rip else (), status, pid)


def _guard(conns, *, nanonis=None, optics=None, serial=None, procs=None, tmp_path=None,
           guard_cfg: dict | None = None):
    gpath = None
    if tmp_path is not None:
        gpath = tmp_path / "port_guard.json"
        if guard_cfg is not None:
            gpath.write_text(json.dumps(guard_cfg), encoding="utf-8")
    return pg.PortGuard(
        nanonis=nanonis or SimpleNamespace(host="127.0.0.1", port_main=6501, port_monitor=6502,
                                           port_data=6503, port_emergency=6504),
        guard_config_path=gpath, optics_config=optics or {"devices": []},
        connections_fn=lambda: conns, local_ips_fn=lambda: {"127.0.0.1", "198.51.100.11", "::1"},
        process_info_fn=lambda pid: {"name": PROCS.get(pid, f"p{pid}"), "exe": None,
                                     "cmdline": None},
        processes_fn=lambda: procs or [],
        serial_owners_fn=serial or (lambda ports: {"owners": {}, "complete": True}),
        own_pid=OWN, publish=False)


def _nanonis_server():
    return [c("127.0.0.1", p, status="LISTEN", pid=NANONIS) for p in (6501, 6502, 6503, 6504, 3364)]


def test_mast_own_connections_are_fine(tmp_path):
    conns = _nanonis_server() + [
        c("127.0.0.1", 50001, "127.0.0.1", 6501, pid=OWN),
        c("127.0.0.1", 6501, "127.0.0.1", 50001, pid=NANONIS),
    ]
    rep = _guard(conns, tmp_path=tmp_path).check()
    assert rep.ok and rep.conflicts == [] and rep.complete
    assert rep.nanonis["mast_connections"] == 1
    assert rep.nanonis["server_process"] == "Nanonis Mimea.exe"


def test_foreign_local_clients_conflict_and_are_named(tmp_path):
    conns = _nanonis_server() + [
        c("127.0.0.1", 50002, "127.0.0.1", 3364, pid=LABVIEW),       # LabVIEW 连 VI Server
        c("127.0.0.1", 3364, "127.0.0.1", 50002, pid=NANONIS),
        c("127.0.0.1", 50003, "127.0.0.1", 6502, pid=OTHER_MAST),    # 另一个 MAST 进程
        c("127.0.0.1", 6502, "127.0.0.1", 50003, pid=NANONIS),
        c("127.0.0.1", 50009, "127.0.0.1", 6501, status="TIME_WAIT", pid=0),   # 已关闭的不算
    ]
    rep = _guard(conns, tmp_path=tmp_path).check()
    assert not rep.ok
    got = sorted((f.resource, f.process, f.pid) for f in rep.conflicts)
    assert got == [("tcp:3364(VI Server)", "LabVIEW.exe", LABVIEW),
                   ("tcp:6502(monitor)", "MAST.exe", OTHER_MAST)]
    assert "LabVIEW.exe（pid 3000）" in rep.message and "不会替你关" in rep.message


def test_labview_vi_on_main_port_is_a_named_conflict(tmp_path):
    """泵浦-探测 / 2D 台之类的 LabVIEW VI 从本机连 Nanonis 6501，
    与 MAST 的 main 正面冲突：要报出进程名与 pid。"""
    conns = _nanonis_server() + [
        c("127.0.0.1", 50020, "127.0.0.1", 6501, pid=LABVIEW),
        c("127.0.0.1", 6501, "127.0.0.1", 50020, pid=NANONIS)]
    rep = _guard(conns, tmp_path=tmp_path).check()
    assert not rep.ok
    (f,) = rep.conflicts
    assert (f.resource, f.process, f.pid, f.kind) == ("tcp:6501(main)", "LabVIEW.exe", LABVIEW,
                                                       "nanonis_tcp")
    assert "LabVIEW.exe（pid 3000）" in rep.message


def test_occupant_hint_names_who_holds_the_port():
    conns = _nanonis_server() + [
        c("127.0.0.1", 50020, "127.0.0.1", 6501, pid=LABVIEW),
        c("127.0.0.1", 6501, "127.0.0.1", 50020, pid=NANONIS),
        c("127.0.0.1", 50021, "127.0.0.1", 6501, pid=OWN),          # 自己的不算
        c("127.0.0.1", 6501, "127.0.0.1", 50021, pid=NANONIS),
        c("198.51.100.11", 6502, "192.0.2.7", 40000, pid=NANONIS),   # 别的机器连 6502
    ]
    kw = dict(own_pid=OWN, connections_fn=lambda: conns,
              local_ips_fn=lambda: {"127.0.0.1", "198.51.100.11"},
              process_info_fn=lambda pid: {"name": PROCS.get(pid, "?")})
    occ = pg.port_occupants("127.0.0.1", 6501, **kw)
    assert occ == [{"pid": LABVIEW, "process": "LabVIEW.exe", "remote": None}]
    hint = pg.occupant_hint("127.0.0.1", 6501, **kw)
    assert "6501 已被 LabVIEW.exe（pid 3000）占用" in hint and "不会替你关" in hint
    assert pg.port_occupants("127.0.0.1", 6502, **kw) == [
        {"pid": None, "process": "", "remote": "192.0.2.7:40000"}]
    assert pg.occupant_hint("127.0.0.1", 6503, **kw) == ""
    # 查不了就不提示，绝不抛
    assert pg.occupant_hint("127.0.0.1", 6501, own_pid=OWN,
                            connections_fn=lambda: 1 / 0) == ""


def test_connection_pool_failure_log_names_the_occupant(monkeypatch, caplog):
    import logging
    import socket as _socket

    from mast.config import NanonisConfig
    from mast.core.connection import ConnectionPool

    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()                                         # 一个没人监听的口：connect 立刻被拒
    cfg = NanonisConfig(host="127.0.0.1", port_main=free, port_monitor=0, port_data=0,
                        port_emergency=0)
    monkeypatch.setattr(pg, "occupant_hint",
                        lambda host, port, **kw: f" —— {port} 已被 LabVIEW.exe（pid 3000）占用")
    caplog.set_level(logging.ERROR, logger="mast.core.connection")
    pool = ConnectionPool(cfg)
    try:
        res = pool.connect_all()
    finally:
        pool.close_all()
    assert res["main"] is False
    msgs = [r.getMessage() for r in caplog.records if "Failed to connect main" in r.getMessage()]
    assert msgs and f"{free} 已被 LabVIEW.exe（pid 3000）占用" in msgs[0]


def test_remote_clients_seen_from_server_side(tmp_path):
    conns = _nanonis_server() + [c("198.51.100.11", 6501, "198.51.100.10", 51000, pid=NANONIS)]
    rep = _guard(conns, tmp_path=tmp_path).check()
    assert [f.remote for f in rep.conflicts] == ["198.51.100.10:51000"]


def test_port_mast_disabled_is_info_only(tmp_path):
    """为外部只读直连腾出了 6504（port_emergency=0）的情形：那上面的客户端只记 info。"""
    nan = SimpleNamespace(host="127.0.0.1", port_main=6501, port_monitor=6502, port_data=6503,
                          port_emergency=0)
    conns = _nanonis_server() + [
        c("127.0.0.1", 50010, "127.0.0.1", 6504, pid=LABVIEW),
        c("127.0.0.1", 6504, "127.0.0.1", 50010, pid=NANONIS)]
    rep = _guard(conns, nanonis=nan, tmp_path=tmp_path).check()
    assert rep.ok and rep.conflicts == []
    assert any(f.resource.startswith("tcp:6504") and f.pid == LABVIEW for f in rep.infos)


def test_allowlist_and_enforce_flag(tmp_path):
    conns = _nanonis_server() + [
        c("127.0.0.1", 50002, "127.0.0.1", 3364, pid=LABVIEW),
        c("127.0.0.1", 3364, "127.0.0.1", 50002, pid=NANONIS)]
    rep = _guard(conns, tmp_path=tmp_path, guard_cfg={
        "allow": [{"process": "LabVIEW.exe", "ports": [3364], "note": "Stage.lvproj 只读"}]}).check()
    assert rep.ok and rep.infos[0].allowed_by == "Stage.lvproj 只读"
    rep2 = _guard(conns, tmp_path=tmp_path, guard_cfg={"enforce": False}).check()
    assert rep2.ok and rep2.conflicts and "enforce=false" in rep2.message


def test_serial_handle_scan_is_off_by_default(tmp_path):
    """设计决定：跨进程句柄扫描默认关（卡住的线程杀不掉、对实验机太侵入）。默认只按
    「可能占串口的厂家程序在跑」给 warn，绝不去调句柄扫描。"""
    optics = {"devices": [
        {"id": "pi_stage", "type": "pi_gcs", "enabled": True, "port": "COM7"},
        {"id": "stage_2d", "type": "pztc_nm003", "enabled": False, "port": "COM8"}]}

    def must_not_scan(ports):
        raise AssertionError("默认路径不许跨进程枚举句柄")

    procs = [{"pid": LABVIEW, "name": "LabVIEW.exe"}, {"pid": 9, "name": "explorer.exe"}]
    rep = _guard(_nanonis_server(), optics=optics, serial=must_not_scan, procs=procs,
                 tmp_path=tmp_path).check()
    assert rep.ok and rep.complete and rep.serial_handle_scan is False
    warns = [f for f in rep.warnings if f.kind == "serial_port"]
    assert [(f.resource, f.process) for f in warns] == [("COM7（pi_stage）", "LabVIEW.exe")]
    assert "serial_handle_scan" in warns[0].detail
    entry = {e["id"]: e for e in rep.optics}
    assert entry["pi_stage"]["scan"] == "disabled" and entry["pi_stage"]["owners"] is None
    # 没有厂家程序在跑：什么都不报
    rep2 = _guard(_nanonis_server(), optics=optics, serial=must_not_scan,
                  procs=[{"pid": 9, "name": "explorer.exe"}], tmp_path=tmp_path).check()
    assert [f for f in rep2.warnings if f.kind == "serial_port"] == []


def test_serial_owner_conflict_only_for_enabled_devices(tmp_path):
    optics = {"devices": [
        {"id": "pi_stage", "type": "pi_gcs", "enabled": True, "port": "COM7"},
        {"id": "stage_2d", "type": "pztc_nm003", "enabled": False, "port": "COM8"}]}
    serial = lambda ports: {"owners": {"COM7": [LABVIEW], "COM8": [LABVIEW, OWN]},
                            "complete": True, "denied_pids": []}
    rep = _guard(_nanonis_server(), optics=optics, serial=serial, tmp_path=tmp_path,
                 guard_cfg={"serial_handle_scan": True}).check()
    assert rep.serial_handle_scan is True
    assert [f.resource for f in rep.conflicts] == ["COM7（pi_stage）"]
    assert rep.conflicts[0].process == "LabVIEW.exe"
    assert any(f.resource == "COM8（stage_2d）" for f in rep.infos)
    entry = {e["id"]: e for e in rep.optics}
    assert entry["stage_2d"]["mast_holds"] is True and entry["pi_stage"]["owners"] == [LABVIEW]


def test_elevated_vendor_process_gives_a_warning_not_silence(tmp_path):
    optics = {"devices": [{"id": "pi_stage", "type": "pi_gcs", "enabled": True, "port": "COM7"}]}
    serial = lambda ports: {"owners": {"COM7": []}, "complete": True, "denied_pids": [LABVIEW]}
    procs = [{"pid": LABVIEW, "name": "LabVIEW.exe"}]
    rep = _guard(_nanonis_server(), optics=optics, serial=serial, procs=procs,
                 tmp_path=tmp_path, guard_cfg={"serial_handle_scan": True}).check()
    assert rep.ok
    assert any(f.kind == "serial_port" and f.severity == "warn" for f in rep.warnings)


def test_serial_open_hint_for_mast_own_open_failures(tmp_path):
    gpath = tmp_path / "port_guard.json"
    procs = [{"pid": LABVIEW, "name": "LabVIEW.exe"}, {"pid": 9, "name": "explorer.exe"}]

    def must_not_scan(ports):
        raise AssertionError("默认不扫句柄")

    hint = pg.serial_open_hint("COM7", processes_fn=lambda: procs, guard_config_path=gpath,
                               serial_owners_fn=must_not_scan)
    assert "COM7 打不开，可能被占用：LabVIEW.exe（pid 3000）在运行" in hint
    assert "不会替你关" in hint
    assert pg.serial_open_hint("COM7", processes_fn=lambda: [{"pid": 9, "name": "explorer.exe"}],
                               guard_config_path=gpath, serial_owners_fn=must_not_scan) == ""
    # 打开了句柄扫描：给确切占用者
    gpath.write_text(json.dumps({"serial_handle_scan": True}), encoding="utf-8")
    hint2 = pg.serial_open_hint(
        "com7", processes_fn=lambda: procs, guard_config_path=gpath,
        serial_owners_fn=lambda ports: {"owners": {"COM7": [LABVIEW, OWN]}, "complete": True},
        process_info_fn=lambda pid: {"name": PROCS.get(pid, "?")}, own_pid=OWN)
    assert "com7 正被 LabVIEW.exe（pid 3000）打开着" in hint2
    # 永不抛
    assert pg.serial_open_hint("COM7", processes_fn=lambda: 1 / 0, guard_config_path=tmp_path /
                               "nope.json") == ""
    assert pg.serial_open_hint("") == ""


def test_everything_degrades_without_psutil(tmp_path, monkeypatch):
    """冻结包里有 psutil（accelerate / peft 的依赖）；
    万一没有：连接池的提示退化成「不追加那句话」，端口检查报 complete=False，都不抛。"""
    def no_psutil():
        raise ImportError("No module named 'psutil'")

    monkeypatch.setattr(pg, "_psutil", no_psutil)
    pg._HINT_CACHE.clear()
    assert pg.occupant_hint("127.0.0.1", 6501) == ""
    assert pg.port_occupants("127.0.0.1", 6501) == []
    g = pg.PortGuard(nanonis=SimpleNamespace(host="127.0.0.1", port_main=6501, port_monitor=6502,
                                             port_data=6503, port_emergency=6504),
                     guard_config_path=tmp_path / "pg.json", optics_config={"devices": []},
                     publish=False)
    rep = g.check()
    assert rep.ok and rep.complete is False
    assert any("psutil" in e for e in rep.errors)
    assert pg.serial_open_hint("COM7", guard_config_path=tmp_path / "pg.json") == ""


def test_usb_devices_get_vendor_gui_heuristic(tmp_path):
    optics = {"devices": [{"id": "pdxc2", "type": "thorlabs_pdxc2", "enabled": True,
                           "serial_no": "112000001"}]}
    procs = [{"pid": 77, "name": "Thorlabs.MotionControl.Kinesis.exe"}]
    rep = _guard(_nanonis_server(), optics=optics, procs=procs, tmp_path=tmp_path).check()
    assert rep.ok
    assert [f.kind for f in rep.warnings] == ["usb_device"]
    assert "112000001" in rep.warnings[0].resource


def test_detection_failure_is_reported_not_swallowed(tmp_path):
    def boom():
        raise PermissionError("no access")

    g = _guard([], tmp_path=tmp_path)
    g._connections = boom
    rep = g.check()
    assert rep.ok and rep.complete is False and "no access" in rep.errors[0]


def test_cache_and_refresh(tmp_path, monkeypatch):
    calls = {"n": 0}

    def conns():
        calls["n"] += 1
        return _nanonis_server()

    g = _guard([], tmp_path=tmp_path)
    g._connections = conns
    g.check()
    g.check()
    assert calls["n"] == 1
    g.check(refresh=True)                 # 刚查过：refresh 也不重查（防反复按）
    assert calls["n"] == 1
    monkeypatch.setattr(pg, "MIN_REFRESH_S", 0.0)
    g.check(refresh=True)
    assert calls["n"] == 2


@pytest.fixture
def no_min_refresh(monkeypatch):
    monkeypatch.setattr(pg, "MIN_REFRESH_S", 0.0)


def test_alert_publishes_once_per_change(tmp_path, monkeypatch, no_min_refresh):
    from mast.core.events import EventBus, EventType
    from mast.live.testing import LiveRecorder, isolated_live_hub

    seen = []

    def _cb(ev):
        if ev.type is EventType.SATELLITE:
            seen.append(ev.data)

    EventBus.get().subscribe(_cb)
    try:
        with isolated_live_hub() as live, LiveRecorder(live) as rec:
            conns = _nanonis_server() + [
                c("127.0.0.1", 50002, "127.0.0.1", 3364, pid=LABVIEW),
                c("127.0.0.1", 3364, "127.0.0.1", 50002, pid=NANONIS)]
            g = _guard(conns, tmp_path=tmp_path)
            g.publish = True
            g.check(refresh=True)
            g.check(refresh=True)
            assert [e["kind"] for e in seen] == ["port_guard"]
            g._connections = lambda: _nanonis_server()
            g.check(refresh=True)
            assert len(seen) == 2 and seen[-1]["conflicts"] == 0
            frames = rec.frames("mast/port_guard")
            assert [f["level"] for f in frames] == ["warn", "info"]
            assert "LabVIEW" in frames[0]["text"]
    finally:
        EventBus.get().unsubscribe(_cb)


def test_module_api_never_raises(monkeypatch):
    class Boom(pg.PortGuard):
        def check(self, **kw):
            raise RuntimeError("x")

    old = pg.get_port_guard()
    pg.set_port_guard(Boom(publish=False))
    try:
        rep = pg.check()
        assert rep.ok and rep.complete is False
    finally:
        pg.set_port_guard(old)
    with pytest.raises(pg.PortConflictError):
        g = _guard(_nanonis_server() + [
            c("127.0.0.1", 50002, "127.0.0.1", 3364, pid=LABVIEW),
            c("127.0.0.1", 3364, "127.0.0.1", 50002, pid=NANONIS)])
        pg.set_port_guard(g)
        try:
            pg.ensure_no_conflicts("TERSGridMap")
        finally:
            pg.set_port_guard(old)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 句柄枚举")
def test_windows_handle_enumeration_finds_who_holds_a_file(tmp_path):
    """用磁盘文件验证句柄枚举这套机制（测试机上不一定有空闲串口可用，也不该去开）。"""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    import psutil

    target = tmp_path / "held.bin"
    target.write_bytes(b"x")
    child = subprocess.Popen([sys.executable, "-c",
                              f"f=open(r'{target}','rb');import time;time.sleep(60)"])
    try:
        time.sleep(1.5)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                                  wintypes.DWORD, wintypes.DWORD]
        with open(target, "rb") as f:
            buf = ctypes.create_unicode_buffer(1024)
            k32.GetFinalPathNameByHandleW(msvcrt.get_osfhandle(f.fileno()), buf, 1024, 2)
            nt_name = buf.value
            res = pg.win_file_handle_owners([nt_name], file_types=(1,))
        owners = res["owners"][nt_name]
        # venv 的 python.exe 是个启动器，真正开文件的是它的子进程
        family = {child.pid} | {p.pid for p in psutil.Process(child.pid).children(recursive=True)}
        assert set(owners) & family, (owners, family, res)
        assert os.getpid() not in owners                # 自己的句柄不算
        assert res["scanned"] > 100
    finally:
        child.terminate()
        child.wait(10)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows")
def test_query_dos_device_on_missing_port_is_empty():
    assert pg.query_dos_device("COM199") == []
