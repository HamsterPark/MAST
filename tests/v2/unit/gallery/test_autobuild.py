# -*- coding: utf-8 -*-
"""数据图库的自动增量更新（设计文档）。

定时用可注入的时钟与等待函数测，不真的等；构建本身在需要「正在跑」的地方用一个按事件放行的
替身（``build.run_build`` 的 monkeypatch，与 ``test_gallery_api`` 同一写法）。所有路径都在 tmp 下。
"""
from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mast.api.context import AppContext
from mast.api.routes.gallery import router
from mast.gallery import autobuild, build, index, service
from mast.gallery import config as gcfg
from mast.gallery import paths

SEC = 10**9


def _configure(root, *, auto=True, interval=10, enabled=True, workers=6):
    roots, errors = gcfg.normalise_roots([{"name": "SPM", "path": str(root), "enabled": enabled}])
    assert errors == []
    gcfg.save_config(gcfg.GalleryConfig(roots=roots, workers=workers, auto_build=auto,
                                        auto_interval_min=interval))


def _sts(path, synth, mtime_ns):
    V = np.linspace(-2.0, 2.0, 101)
    synth.dat(path, ["Bias calc (V)", "Current (A)"], np.column_stack([V, np.tanh(V) * 1e-10]),
              mtime_ns=mtime_ns)


def _names() -> set[str]:
    return {it["fn"] for it in index.read_index()["items"]}


class _Gate:
    """``build.run_build`` 的替身：记录调用，等放行事件；收到取消就提前返回。"""

    def __init__(self, monkeypatch, *, block=True):
        self.calls: list[dict] = []
        self.gate = threading.Event()
        self.block = block
        self.started = threading.Event()
        monkeypatch.setattr(build, "run_build", self)

    def __call__(self, **kw):
        self.calls.append(kw)
        self.started.set()
        if self.block:
            cancel = kw["cancel"]
            t0 = time.monotonic()
            while not self.gate.is_set() and not cancel.is_set() and time.monotonic() - t0 < 30:
                time.sleep(0.005)
            if cancel.is_set():
                kw["progress"].update(phase="cancelled")
        return {}


# ── 配置 ───────────────────────────────────────────────────────────────


def test_config_defaults_to_auto_on_and_old_files_read_as_on(gallery_dir):
    assert gcfg.load_config().auto_build is True and gcfg.load_config().auto_interval_min == 10
    gallery_dir.mkdir(parents=True)
    paths.layout().config.write_text(json.dumps({"version": 1, "roots": [], "workers": 3}), encoding="utf-8")
    cfg = gcfg.load_config()
    assert (cfg.auto_build, cfg.auto_interval_min, cfg.workers) == (True, 10, 3)


def test_config_roundtrip_and_interval_is_clamped(gallery_dir):
    gcfg.save_config(gcfg.GalleryConfig(auto_build=False, auto_interval_min=1))
    cfg = gcfg.load_config()
    assert cfg.auto_build is False and cfg.auto_interval_min == gcfg.AUTO_INTERVAL_MIN
    gcfg.save_config(gcfg.GalleryConfig(auto_interval_min=10**6))
    assert gcfg.load_config().auto_interval_min == gcfg.AUTO_INTERVAL_MAX


# ── 刚写过的文件 ────────────────────────────────────────────────────────


def test_a_file_written_a_moment_ago_is_skipped_then_picked_up(tmp_path, synth, gallery_dir, t0_ns):
    root = tmp_path / "SPM"
    _sts(root / "d" / "old_0001.dat", synth, t0_ns)
    now = t0_ns + 3600 * SEC
    _sts(root / "d" / "fresh_0002.dat", synth, now - 5 * SEC)       # Nanonis 可能还在写
    _configure(root)
    res = build.run_build(state=gallery_dir, skip_recent_s=60, now_ns=now)
    assert res["phase"] == "done" and res["n_deferred"] == 1
    assert _names() == {"old_0001.dat"}
    lay = paths.layout()
    for name in ("inventory.json", "render.json"):                  # 没有被写进任何缓存
        assert "fresh_0002" not in lay.cache_file(name).read_text(encoding="utf-8"), name
    res = build.run_build(state=gallery_dir, skip_recent_s=60, now_ns=now + 120 * SEC)
    assert res["n_deferred"] == 0 and res["n_new"] == 1
    assert _names() == {"old_0001.dat", "fresh_0002.dat"}


def test_a_deferred_file_is_not_treated_as_deleted(tmp_path, synth, gallery_dir, t0_ns):
    """已经在索引里的文件正在被重写（修改时刻很新）：这一轮不动它，更不能当成「已删除」清掉。"""
    root = tmp_path / "SPM"
    p = root / "d" / "a_0001.dat"
    _sts(p, synth, t0_ns)
    _configure(root)
    build.run_build(state=gallery_dir)
    assert _names() == {"a_0001.dat"}
    now = t0_ns + 3600 * SEC
    _sts(p, synth, now - 2 * SEC)                                   # 重写，mtime 很新
    res = build.run_build(state=gallery_dir, skip_recent_s=60, now_ns=now)
    assert res["n_removed"] == 0 and res["n_deferred"] == 1 and res["n_changed"] == 0
    assert _names() == {"a_0001.dat"}
    assert "a_0001" in paths.layout().cache_file("inventory.json").read_text(encoding="utf-8")
    res = build.run_build(state=gallery_dir, skip_recent_s=60, now_ns=now + 120 * SEC)
    assert res["n_changed"] == 1 and _names() == {"a_0001.dat"}


def test_skipping_is_off_unless_asked_for(tmp_path, synth, gallery_dir):
    root = tmp_path / "SPM"
    _sts(root / "d" / "now_0001.dat", synth, time.time_ns())
    _configure(root)
    assert build.run_build(state=gallery_dir)["n_deferred"] == 0
    assert _names() == {"now_0001.dat"}


def test_a_future_mtime_is_not_deferred_forever(tmp_path, synth, gallery_dir, t0_ns):
    root = tmp_path / "SPM"
    now = t0_ns
    _sts(root / "d" / "skewed_0001.dat", synth, now + 3600 * SEC)   # 时钟偏差造成的未来时刻
    _configure(root)
    res = build.run_build(state=gallery_dir, skip_recent_s=60, now_ns=now)
    assert res["n_deferred"] == 0 and _names() == {"skewed_0001.dat"}


# ── 自动触发的判断 ──────────────────────────────────────────────────────


def test_auto_does_not_start_without_roots_or_with_the_switch_off(tmp_path, gallery_dir, monkeypatch):
    g = _Gate(monkeypatch, block=False)
    assert service.start_auto("timer")[0] is False                  # 没有配置文件 ⇒ 没有数据根
    assert not gallery_dir.exists(), "判断不该创建状态目录"
    root = tmp_path / "SPM"
    root.mkdir()
    _configure(root, enabled=False)
    assert service.start_auto("timer")[0] is False                  # 有根但没启用
    _configure(root, auto=False)
    assert service.start_auto("timer")[0] is False                  # 开关关着
    assert g.calls == []


def test_auto_build_runs_with_capped_workers_and_skips_recent_files(tmp_path, gallery_dir, monkeypatch):
    g = _Gate(monkeypatch, block=False)
    root = tmp_path / "SPM"
    root.mkdir()
    _configure(root, workers=6)
    started, st = service.start_auto("timer")
    assert started and st["trigger"] == "auto"
    assert service.wait(10)
    kw = g.calls[0]
    assert kw["max_workers"] == service.AUTO_WORKERS == 2 and kw["force"] is False
    assert kw["skip_recent_s"] == build.RECENT_SKIP_S
    service.start_build(False)                                      # 手动：不封顶、同样跳过刚写的
    assert service.wait(10)
    assert "max_workers" not in g.calls[1] and g.calls[1]["skip_recent_s"] == build.RECENT_SKIP_S
    assert service.get_status()["trigger"] == "manual"


def test_auto_is_skipped_while_a_build_runs_and_inside_the_minimum_gap(tmp_path, gallery_dir, monkeypatch):
    g = _Gate(monkeypatch)
    root = tmp_path / "SPM"
    root.mkdir()
    _configure(root)
    clock = [1000.0]
    monkeypatch.setattr(service, "_now", lambda: clock[0])
    assert service.start_auto("timer")[0] is True
    assert g.started.wait(5)
    assert service.start_auto("timer")[0] is False                  # 在跑：跳过这一轮
    assert service.start_auto("open")[0] is False
    g.gate.set()
    assert service.wait(10)
    clock[0] += service.MIN_GAP_S - 1
    assert service.start_auto("open")[0] is False                   # 刚结束：多个标签页也不会连发
    clock[0] += 2
    assert service.start_auto("open")[0] is True
    assert service.wait(10)
    assert len(g.calls) == 2


def test_two_tabs_opening_at_once_start_one_build(tmp_path, gallery_dir, monkeypatch):
    g = _Gate(monkeypatch, block=False)
    root = tmp_path / "SPM"
    root.mkdir()
    _configure(root)
    results: list[bool] = []
    barrier = threading.Barrier(6)

    def hit():
        barrier.wait()
        results.append(service.start_auto("open")[0])

    ts = [threading.Thread(target=hit) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert service.wait(10)
    assert results.count(True) == 1 and len(g.calls) == 1


def test_the_first_round_waits_for_the_startup_hold(tmp_path, gallery_dir, monkeypatch):
    g = _Gate(monkeypatch, block=False)
    root = tmp_path / "SPM"
    root.mkdir()
    _configure(root)
    clock = [50.0]
    monkeypatch.setattr(service, "_now", lambda: clock[0])
    service.hold_auto(180)
    assert service.start_auto("open")[0] is False and service.start_auto("timer")[0] is False
    clock[0] += 181
    assert service.start_auto("timer")[0] is True
    assert service.wait(10) and len(g.calls) == 1


# ── 调磁场时暂停 ────────────────────────────────────────────────────────


class _FakeMagnet:
    def __init__(self, busy=False, active=None):
        self.busy = busy
        self.active = active


@pytest.fixture()
def magnet_source():
    from mast.core import magnet_control

    yield magnet_control.set_source
    magnet_control.set_source(None)


def test_auto_pauses_during_a_field_ramp_and_resumes_after(tmp_path, gallery_dir, monkeypatch, magnet_source):
    g = _Gate(monkeypatch, block=False)
    root = tmp_path / "SPM"
    root.mkdir()
    _configure(root)
    ctl = _FakeMagnet(busy=True)
    magnet_source(lambda: ctl)
    st0 = service.get_status()
    started, st = service.start_auto("timer")
    assert started is False and st == st0 and g.calls == []
    ctl.busy, ctl.active = False, {"target_t": 1.0}                  # active 也算在调
    assert service.start_auto("timer")[0] is False
    ctl.active = None                                                # 调完
    assert service.start_auto("timer")[0] is True
    assert service.wait(10) and len(g.calls) == 1


def test_a_manual_build_ignores_the_field_ramp(tmp_path, gallery_dir, monkeypatch, magnet_source):
    g = _Gate(monkeypatch, block=False)
    root = tmp_path / "SPM"
    root.mkdir()
    _configure(root)
    magnet_source(lambda: _FakeMagnet(busy=True))
    service.start_build(False)
    assert service.wait(10) and len(g.calls) == 1


def test_a_broken_or_absent_magnet_source_never_blocks_the_gallery(tmp_path, gallery_dir, monkeypatch, magnet_source):
    g = _Gate(monkeypatch, block=False)
    root = tmp_path / "SPM"
    root.mkdir()
    _configure(root)

    def boom():
        raise RuntimeError("no link")

    magnet_source(boom)                                              # present() 吞异常 ⇒ None
    assert service.start_auto("timer")[0] is True
    assert service.wait(10)

    class Bad:
        @property
        def busy(self):
            raise RuntimeError("state lock gone")

    service._LAST_END = None
    magnet_source(lambda: Bad())                                     # busy 抛异常 ⇒ 当作没在调
    assert service.start_auto("timer")[0] is True
    assert service.wait(10)
    service._LAST_END = None
    magnet_source(lambda: None)                                      # 没有 625
    assert service.start_auto("timer")[0] is True
    assert service.wait(10) and len(g.calls) == 3


# ── 手动点击不被吞 ──────────────────────────────────────────────────────


def test_a_manual_click_during_an_auto_build_cancels_it_and_runs_next(tmp_path, gallery_dir, monkeypatch):
    g = _Gate(monkeypatch)
    root = tmp_path / "SPM"
    root.mkdir()
    _configure(root)
    assert service.start_auto("timer")[0] is True
    assert g.started.wait(5)
    t0 = time.monotonic()
    st = service.start_build(force=True)                            # 请求线程只启动、不等待
    assert time.monotonic() - t0 < 1.0
    assert st["running"] and "取消自动更新" in st["message"]
    assert g.calls[0]["cancel"].is_set()
    g.gate.set()                                                    # 放行手动那一轮（自动那一轮是被取消退出的）
    assert service.wait(10)                                         # 同一个线程接着跑完手动那一轮
    assert len(g.calls) == 2
    assert g.calls[0]["cancel"].is_set()
    assert g.calls[1]["force"] is True and "max_workers" not in g.calls[1]
    assert service.get_status()["trigger"] == "manual"
    assert not g.calls[1]["cancel"].is_set()


def test_a_manual_full_rebuild_during_a_manual_incremental_is_queued_not_swallowed(tmp_path, gallery_dir, monkeypatch):
    g = _Gate(monkeypatch)
    root = tmp_path / "SPM"
    root.mkdir()
    _configure(root)
    service.start_build(force=False)
    assert g.started.wait(5)
    service.start_build(force=False)                                # 重复的增量：本轮已覆盖
    service.start_build(force=True)                                 # 全部重出：记待办，不打断
    assert not g.calls[0]["cancel"].is_set()
    g.gate.set()
    assert service.wait(10)
    assert [c["force"] for c in g.calls] == [False, True]


def test_cancel_drops_the_queued_request(tmp_path, gallery_dir, monkeypatch):
    g = _Gate(monkeypatch)
    root = tmp_path / "SPM"
    root.mkdir()
    _configure(root)
    service.start_auto("timer")
    assert g.started.wait(5)
    service.start_build(force=True)
    service.cancel_build()                                          # 用户说停就是停
    assert service.wait(10)
    assert len(g.calls) == 1


# ── 定时线程 ────────────────────────────────────────────────────────────


class _FakeTime:
    """假时钟 + 假等待：``wait`` 把时钟推进，返回「是否被叫停」。"""

    def __init__(self, stop_after_waits=None):
        self.now = 0.0
        self.waits: list[float] = []
        self.stop_after = stop_after_waits

    def clock(self):
        return self.now

    def wait(self, s):
        self.waits.append(s)
        self.now += s
        return self.stop_after is not None and len(self.waits) >= self.stop_after


def test_scheduler_waits_first_then_ticks_every_interval(tmp_path, gallery_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "start_auto", lambda reason="timer": (calls.append(reason) or True, {}))
    ft = _FakeTime(stop_after_waits=12)
    sched = autobuild.AutoBuildScheduler(first_delay_s=180, slice_s=30, clock=ft.clock, wait=ft.wait,
                                         interval_s=lambda: 120.0)
    sched._loop()                                                   # 同步跑到「叫停」
    assert ft.waits[0] == 180                                       # 启动后第一轮要等
    # 之后每 120 s 一轮（切片 30 s）：180 → tick → 4 片 → tick …
    assert calls and set(calls) == {"timer"}
    ticks_at = [sum(ft.waits[: i + 1]) for i in range(len(ft.waits))]
    assert ticks_at[0] == 180 and len(calls) == 3
    assert ft.waits[1:5] == [30, 30, 30, 30]


def test_scheduler_rereads_the_interval_every_slice(gallery_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "start_auto", lambda reason="timer": (calls.append(1) or True, {}))
    ft = _FakeTime(stop_after_waits=6)
    iv = [3600.0]

    def interval():
        if len(ft.waits) >= 3:
            iv[0] = 60.0                                            # 用户把间隔改短了
        return iv[0]

    sched = autobuild.AutoBuildScheduler(first_delay_s=0, slice_s=30, clock=ft.clock, wait=ft.wait,
                                         interval_s=interval)
    sched._loop()
    assert len(calls) >= 2, "改短的间隔没有立刻生效"


def test_scheduler_skips_a_round_when_a_build_is_running_and_keeps_going(tmp_path, gallery_dir, monkeypatch):
    g = _Gate(monkeypatch)
    root = tmp_path / "SPM"
    root.mkdir()
    _configure(root)
    service.start_build(force=False)                                # 一轮手动构建在跑
    assert g.started.wait(5)
    ft = _FakeTime(stop_after_waits=8)
    sched = autobuild.AutoBuildScheduler(first_delay_s=0, slice_s=60, clock=ft.clock, wait=ft.wait,
                                         interval_s=lambda: 120.0)
    sched._loop()
    assert sched.ticks == 0 and len(g.calls) == 1                   # 一轮都没起，也没抛
    g.gate.set()
    assert service.wait(10)


def test_the_scheduler_thread_exits_on_stop_and_cancels_a_running_auto_build(tmp_path, gallery_dir, monkeypatch):
    g = _Gate(monkeypatch)
    root = tmp_path / "SPM"
    root.mkdir()
    _configure(root)
    sched = autobuild.AutoBuildScheduler(first_delay_s=0, slice_s=0.01, interval_s=lambda: 3600.0)
    sched.start()
    assert g.started.wait(10), "定时线程没有起构建"
    for _ in range(200):                       # 槽在线程启动之后才换上，等它换好
        if service.get_status()["trigger"] == "auto":
            break
        time.sleep(0.01)
    assert service.get_status()["trigger"] == "auto"
    t0 = time.monotonic()
    sched.stop()                                                    # 关停钩子
    assert time.monotonic() - t0 < 1.0, "关停被构建拖住了"
    assert sched.join(5), "定时线程没有退出"
    assert g.calls[0]["cancel"].is_set()
    assert service.wait(10)
    assert service.start_auto("open")[0] is False                   # 关停后不再起
    assert service.start_build(False)["running"] is False


def test_start_hold_is_set_when_the_scheduler_starts(gallery_dir, monkeypatch):
    monkeypatch.setattr(service, "_now", lambda: 10.0)
    sched = autobuild.AutoBuildScheduler(first_delay_s=180, wait=lambda s: True)
    sched.start()
    assert sched.join(5)
    assert service._HOLD_UNTIL == pytest.approx(10.0 + 180 - autobuild.HOLD_MARGIN_S)


def test_nothing_starts_the_scheduler_by_itself_under_pytest():
    assert autobuild.start_for_runtime(lambda *a, **k: None) is None


# ── API ─────────────────────────────────────────────────────────────────


def _client() -> TestClient:
    app = FastAPI()
    app.state.ctx = AppContext()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_status_keys_and_schema_stay_in_step():
    from mast.api.schemas_gallery import GalleryBuildStatus

    assert set(build.STATUS_KEYS) == set(GalleryBuildStatus.model_fields)
    assert set(service.get_status()) == set(build.STATUS_KEYS)


def test_api_config_carries_the_auto_settings(tmp_path, gallery_dir):
    c = _client()
    body = c.get("/api/gallery/config").json()
    assert body["auto_build"] is True and body["auto_interval_min"] == 10
    root = tmp_path / "SPM"
    root.mkdir()
    body = c.post("/api/gallery/config", json={"roots": [{"path": str(root)}], "auto_build": False,
                                               "auto_interval_min": 30}).json()
    assert body["ok"] and body["auto_build"] is False and body["auto_interval_min"] == 30
    # 不带这两个键的更新（旧前端）不改它们
    body = c.post("/api/gallery/config", json={"roots": [{"path": str(root)}], "workers": 3}).json()
    assert body["auto_build"] is False and body["auto_interval_min"] == 30 and body["workers"] == 3
    assert c.post("/api/gallery/config", json={"roots": [], "auto_interval_min": 1}).status_code == 422


def test_api_auto_requires_a_body_and_never_waits(tmp_path, gallery_dir, monkeypatch):
    g = _Gate(monkeypatch)
    root = tmp_path / "SPM"
    root.mkdir()
    _configure(root)
    c = _client()
    assert c.post("/api/gallery/auto").status_code == 422           # 无体的 POST 不受理（CSRF 门）
    assert c.post("/api/gallery/auto", json={"reason": "nope"}).status_code == 422
    t0 = time.monotonic()
    r = c.post("/api/gallery/auto", json={"reason": "open"}).json()
    assert time.monotonic() - t0 < 1.0
    assert r["running"] and r["trigger"] == "auto" and r["degraded"] is False
    again = c.post("/api/gallery/auto", json={"reason": "open"}).json()
    assert again["started"] == r["started"] and len(g.calls) == 1
    g.gate.set()
    assert service.wait(10)


def test_api_auto_with_the_switch_off_just_reports_status(tmp_path, gallery_dir, monkeypatch):
    g = _Gate(monkeypatch, block=False)
    root = tmp_path / "SPM"
    root.mkdir()
    _configure(root, auto=False)
    r = _client().post("/api/gallery/auto", json={"reason": "open"}).json()
    assert r["running"] is False and g.calls == []
