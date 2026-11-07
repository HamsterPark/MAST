"""runtime 的光学侧急停钩子（契约 §10.1）：E-STOP、看门狗异常、关停都要停卫星与本机光学台，
尽力而为、不阻塞、不抛。CoreRuntime 用 ``__new__`` 造（不跑很重的 ``__init__``）。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import mast.instruments.registry as inst_registry
import mast.satellite as sat_pkg
from mast.core.runtime import CoreRuntime


class FakeHub:
    def __init__(self, *, unreachable=(), raises=False, order=None) -> None:
        self.calls: list[str] = []
        self.safe_calls: list[str] = []
        self.safe_waits: list[float] = []
        self.started_with = None
        self.shut = False
        self._unreachable = list(unreachable)
        self._raises = raises
        self.called = threading.Event()
        self.order = order if order is not None else []

    def emergency_stop_all(self, reason=""):
        self.calls.append(reason)
        self.called.set()
        if self._raises:
            raise RuntimeError("hub exploded")
        return {"sent": [], "unreachable": [{"id": u, "state": "lost"} for u in self._unreachable],
                "errors": []}

    def safe_for_shutdown(self, reason="", *, wait_s=None):
        # 只有这个入口会等卫星回话、并且只做一次；钩子要是退回去调只入队的 safe_all，
        # 这个替身没有那个方法，钩子吞掉 AttributeError，顺序断言就会红
        self.safe_calls.append(reason)
        self.safe_waits.append(wait_s)
        self.order.append("safe")
        return {"sent": [], "unreachable": [], "errors": []}

    def start(self, runtime=None):
        self.started_with = runtime
        return self

    def shutdown(self, **kw):
        self.shut = True
        self.order.append("hub.shutdown")


class FakeRegistry:
    def __init__(self, block_s: float = 0.0, order=None) -> None:
        self.stopped = threading.Event()
        self.block_s = block_s
        self.order = order if order is not None else []

    def stop_all(self):
        self.order.append("local stop_all")
        time.sleep(self.block_s)          # 串口慢：调用方不该等它
        self.stopped.set()


@pytest.fixture
def rt(monkeypatch):
    r = CoreRuntime.__new__(CoreRuntime)
    r._orch_abort = threading.Event()
    r._orch_run_aborts_lock = threading.Lock()
    r._orch_run_aborts = {}
    r._executor = None
    r._orch_interrupts = None
    r._pool = None
    r._shutdown_hooks = []
    return r


def _install(monkeypatch, hub, reg):
    monkeypatch.setattr(sat_pkg, "get_satellite_hub", lambda: hub)
    monkeypatch.setattr(inst_registry, "get_instrument_registry", lambda: reg)


def test_emergency_stop_reaches_satellites_and_local_optics(rt, monkeypatch):
    hub, reg = FakeHub(unreachable=["rig1"]), FakeRegistry(block_s=0.5)
    _install(monkeypatch, hub, reg)
    t0 = time.monotonic()
    res = rt.emergency_stop("操作员按了急停")
    assert time.monotonic() - t0 < 0.4             # 没等慢的本机光学台
    assert hub.calls == ["操作员按了急停"]
    assert res["optics"]["local_optics"] == "dispatched"
    assert reg.stopped.wait(3)
    assert res["aborted"] is True
    assert any("rig1" in e and "死人开关" in e for e in res["errors"])


def test_optics_stop_is_deduplicated_and_never_raises(rt, monkeypatch):
    hub, reg = FakeHub(raises=True), FakeRegistry()
    _install(monkeypatch, hub, reg)
    out = rt._optics_emergency_stop("a")
    assert "hub exploded" in out["satellites"]["errors"][0]
    assert rt._optics_emergency_stop("b") == {"deduped": True}
    rt._optics_estop_last = time.monotonic() - 10
    rt._optics_emergency_stop("c")
    assert hub.calls == ["a", "c"]

    def broken():
        raise ImportError("no satellite package in this build")

    monkeypatch.setattr(sat_pkg, "get_satellite_hub", broken)
    rt._optics_estop_last = 0.0
    out = rt._optics_emergency_stop("d")           # 不抛
    assert "ImportError" in out["satellites"]["errors"][0]


def test_wiring_starts_hub_subscribes_anomaly_and_registers_shutdown(rt, monkeypatch):
    from mast.core import port_guard
    from mast.core.events import EventBus

    hub, reg = FakeHub(), FakeRegistry()
    _install(monkeypatch, hub, reg)
    checks = []
    monkeypatch.setattr(port_guard, "check_async", lambda **kw: checks.append(kw))
    rt.config = None
    try:
        rt._wire_optics_hub()
        assert hub.started_with is rt
        assert checks == [{"reason": "startup"}]
        assert [n for n, _f in rt._shutdown_hooks] == ["satellite hub"]
        EventBus.get().publish_anomaly(threshold_a=1e-8, readings=[2e-8] * 8)
        assert hub.called.wait(3)
        assert "看门狗" in hub.calls[0]
        assert reg.stopped.wait(3)
        # 关停钩子：卫星只发 safe（等同死人开关，不打断人工采集），**不发 estop**；
        # 本机光学台照旧 stop_all；关会话；退订
        rt._optics_estop_last = 0.0
        n_estop = len(hub.calls)
        reg.stopped.clear()
        rt._optics_shutdown()
        assert hub.safe_calls == ["MAST 关停"]
        assert len(hub.calls) == n_estop, "正常关停不该发 estop"
        assert reg.stopped.wait(3)
        assert hub.shut is True
        assert rt._optics_anomaly_cb is None
        n = len(hub.calls)
        EventBus.get().publish_anomaly(threshold_a=1e-8, readings=[])
        time.sleep(0.2)
        assert len(hub.calls) == n
    finally:
        cb = getattr(rt, "_optics_anomaly_cb", None)
        if cb is not None:
            EventBus.get().unsubscribe(cb)


def test_shutdown_confirms_safe_then_closes_then_stops_local_and_runs_once(rt, monkeypatch):
    """顺序：safe（等确认）→ 关卫星会话 → 本机光学台 stop_all。本机的 stop_all 会经远程驱动给
    卫星设备发 stop（会打断人工采集）—— 排在会话关掉之后，枢纽已经拒发，卫星设备只收到 safe。
    shutdown() 常被调两次：第二次什么都不做（不再误报「连不上」）。"""
    order: list[str] = []
    hub, reg = FakeHub(order=order), FakeRegistry(order=order)
    _install(monkeypatch, hub, reg)
    rt._optics_shutdown()
    assert reg.stopped.wait(3)
    assert order == ["safe", "hub.shutdown", "local stop_all"]
    rt._optics_shutdown()
    time.sleep(0.2)
    assert order == ["safe", "hub.shutdown", "local stop_all"]
    assert hub.safe_calls == ["MAST 关停"]


def test_estop_buffer_hook_and_setup_are_wired():
    """结构钉：E_STOP 的 buffer 钩子与 setup() 都接上了光学侧（这两处没法不起整个 runtime 地跑）。"""
    import inspect

    import mast.core.runtime as rtmod

    src = inspect.getsource(rtmod._ensure_buffer_for_gui)
    assert "_optics_emergency_stop" in src
    setup_src = inspect.getsource(CoreRuntime.setup)
    assert "self._wire_optics_hub()" in setup_src
    em_src = inspect.getsource(CoreRuntime.emergency_stop)
    # 光学侧排在 Nanonis 的 urgent_call 之前（它只入队，Nanonis 那边最坏要等几秒）
    assert (em_src.index("self._optics_emergency_stop(")
            < em_src.index('_urgent = getattr(pool, "urgent_call"'))


# ── optical station (design §6.4): an E-STOP also takes every light source to the safe direction ──

def _station_registry(tmp_path):
    import json

    from mast.instruments.registry import InstrumentRegistry

    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": [
        {"id": "las", "type": "sim_laser"},
        {"id": "witec", "type": "sim_microscope"},
    ]}), encoding="utf-8")
    return InstrumentRegistry(config_path=p)


def _join_threads(name: str, timeout: float = 10.0) -> None:
    for t in list(threading.enumerate()):
        if t.name == name:
            t.join(timeout)


def test_estop_switches_lasers_off_and_closes_shutters(rt, monkeypatch, tmp_path):
    reg = _station_registry(tmp_path)
    try:
        las, mic = reg.laser("las"), reg.microscope("witec")
        las.emission_on()
        mic.set_shutter(True)
        _install(monkeypatch, FakeHub(), reg)
        rt._optics_emergency_stop("operator pressed E-STOP")
        _join_threads("optics-local-estop")
        assert las.emission_state() is False
        # the sim microscope's stop_all leaves the shutter alone: only the safe-off closes it
        assert mic.shutter_open is False
    finally:
        reg.close_all()


def test_safe_off_retries_once_when_not_everything_was_confirmed(monkeypatch):
    from mast.instruments import emission_safety

    calls: list[str] = []

    def fake(reason, **_kw):
        calls.append(reason)
        ok = len(calls) > 1
        return {"reason": reason, "devices": [{"device_id": "las", "confirmed": ok}], "all_confirmed": ok}

    monkeypatch.setattr(emission_safety, "safe_off_all", fake)
    res = CoreRuntime._optics_safe_off("E-STOP: x", retry_s=0.01)
    assert len(calls) == 2 and calls[1].endswith("(retry)")
    assert res["all_confirmed"] is True
    calls.clear()
    CoreRuntime._optics_safe_off("E-STOP: y", retry_s=0.0)        # no retry asked
    assert len(calls) == 1


def test_shutdown_waits_for_the_local_stop_within_a_bound(rt, monkeypatch):
    hub, reg = FakeHub(), FakeRegistry(block_s=0.3)
    _install(monkeypatch, hub, reg)
    t0 = time.monotonic()
    rt._optics_shutdown()
    # the hook returns only after the local stop finished (a daemon thread that is not joined
    # can be cut off by the process exit before the shutter closes) ...
    assert reg.stopped.is_set()
    assert time.monotonic() - t0 >= 0.3
    # ... but never longer than the bound
    rt2 = CoreRuntime.__new__(CoreRuntime)
    rt2._shutdown_hooks = []
    hub2, reg2 = FakeHub(), FakeRegistry(block_s=5.0)
    _install(monkeypatch, hub2, reg2)
    monkeypatch.setattr(CoreRuntime, "_OPTICS_SHUTDOWN_JOIN_S", 0.2)
    t0 = time.monotonic()
    rt2._optics_shutdown()
    assert time.monotonic() - t0 < 2.0
