# -*- coding: utf-8 -*-
"""数据文件的环境标签：runtime 里的三处钩子与读数环的接线（设计 §5）。

每块都有自己的单测；这里核对它们**接上了**——实现了、测了、就是没接线，从外面看和
「功能不工作」一模一样（环境历史 test_wiring 的同一教训）。
"""
from __future__ import annotations

import ast
import os
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mast.core.runtime import CoreRuntime
from mast.core.types import SensorReading
from mast.environment.base import EnvironmentSensor
from mast.environment.monitor import EnvironmentMonitor
from mast.envhistory.filetags.ring import ReadingRing
from tests.v2.srcref import source_of


class _RecordingTagger:
    def __init__(self) -> None:
        self.calls: list = []
        self.enabled_cached = True

    def submit(self, paths, *, origin, observed_at=None):
        self.calls.append((origin, [str(p) for p in paths]))
        return len(self.calls[-1][1])


def _rt(**kw) -> SimpleNamespace:
    rt = SimpleNamespace(**kw)
    rt._env_tagger_or_none = lambda: CoreRuntime._env_tagger_or_none(rt)
    rt._submit_env_tags = lambda paths, origin="skill": CoreRuntime._submit_env_tags(rt, paths, origin=origin)
    return rt


def _file(p: Path, age_s: float = 60.0) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"NANONIS" * 50)
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


# ── H1：技能记录接缝 ────────────────────────────────────────────────────


@pytest.fixture()
def live_v2(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST_DATA_DIR", str(tmp_path / "data_root"))
    from mast.logging.v2.live import open_live_v2
    repos, eid = open_live_v2()
    assert repos is not None and eid
    return repos, eid


def test_h1_a_successful_skill_hands_every_file_it_wrote_to_the_tagger(live_v2, tmp_path):
    repos, eid = live_v2
    tg = _RecordingTagger()
    rt = _rt(_v2_repos=repos, _v2_eid=eid, _active_thread_id=None, _env_tagger=tg)
    a, b = _file(tmp_path / "w" / "R1.sxm"), _file(tmp_path / "w" / "R2.sxm")
    aid = CoreRuntime._record_v2_action(rt, {
        "skill": "BatchRegionsScan", "params": {}, "success": True,
        "data": {"scanned_paths": [str(a), str(b)]}})
    assert aid
    assert tg.calls == [("skill", [str(a), str(b)])]


def test_h1_a_failed_skill_hands_nothing(live_v2, tmp_path):
    repos, eid = live_v2
    tg = _RecordingTagger()
    rt = _rt(_v2_repos=repos, _v2_eid=eid, _active_thread_id=None, _env_tagger=tg)
    f = _file(tmp_path / "w" / "partial.sxm")
    CoreRuntime._record_v2_action(rt, {"skill": "StartScan", "params": {}, "success": False,
                                       "error": "aborted", "artifact_path": str(f)})
    assert tg.calls == []


def test_h1_a_stub_without_the_hook_is_still_recorded(live_v2, tmp_path):
    """测试里常用的 stub self 没有 _submit_env_tags：记录照常，钩子跳过。"""
    repos, eid = live_v2
    rt = SimpleNamespace(_v2_repos=repos, _v2_eid=eid, _active_thread_id=None)
    f = _file(tmp_path / "w" / "R1.sxm")
    assert CoreRuntime._record_v2_action(rt, {"skill": "StartScan", "params": {}, "success": True,
                                              "artifact_path": str(f)})


# ── H2：文件监视 ────────────────────────────────────────────────────────


def test_h2_the_first_tick_only_registers_then_new_and_changed_files_go_out(tmp_path):
    sess = tmp_path / "w" / "session"
    old = _file(sess / "Topo_0001.sxm", age_s=3600)
    _file(sess / ".mast" / "envtags" / "Topo_0001.sxm.json")         # 我们自己的 sidecar
    _file(sess / "_gallery" / "x.sxm")                                 # 图库状态目录
    _file(sess / "notes.txt")
    tg = _RecordingTagger()
    rt = _rt(_scan_search_dirs=(str(sess),), _env_tagger=tg)
    state: dict = {}
    CoreRuntime._env_tag_tick(rt, state)
    assert tg.calls == []                                              # 不回头补打
    new = _file(sess / "sub" / "Bias-Spectroscopy001.dat", age_s=1.0)
    _file(old, age_s=2.0)                                              # 同名重写
    CoreRuntime._env_tag_tick(rt, state)
    assert len(tg.calls) == 1 and tg.calls[0][0] == "watcher"
    assert sorted(tg.calls[0][1]) == sorted([str(new), str(old)])
    CoreRuntime._env_tag_tick(rt, state)
    assert len(tg.calls) == 1


def test_h2_switched_off_still_registers_so_switching_on_does_not_backfill(tmp_path):
    sess = tmp_path / "w" / "session"
    sess.mkdir(parents=True)
    tg = _RecordingTagger()
    rt = _rt(_scan_search_dirs=(str(sess),), _env_tagger=tg)
    state: dict = {}
    CoreRuntime._env_tag_tick(rt, state)
    tg.enabled_cached = False
    _file(sess / "while_off.sxm")
    CoreRuntime._env_tag_tick(rt, state)
    tg.enabled_cached = True
    CoreRuntime._env_tag_tick(rt, state)
    assert tg.calls == []


def test_h2_a_root_whose_registrations_were_pruned_is_seeded_again_when_it_returns(tmp_path, monkeypatch):
    """登记表超界时只留这一轮看得见的文件；被清掉的根回来时要重新播种，否则它的老文件全成了「新的」。"""
    from mast.core import runtime as rtmod

    monkeypatch.setattr(rtmod, "_ENV_TAG_FILES_MAX", 2)
    a, b = tmp_path / "w" / "A", tmp_path / "w" / "B"
    for i in range(2):
        _file(a / f"Topo_{i:03d}.sxm", age_s=86400)
        _file(b / f"Topo_{100 + i:03d}.sxm", age_s=86400)
    tg = _RecordingTagger()
    rt = _rt(_scan_search_dirs=(str(a),), _env_tagger=tg)
    state: dict = {}
    CoreRuntime._env_tag_tick(rt, state)                 # 启动：A 播种
    rt._scan_search_dirs = (str(b),)                     # A 暂时不在、B 进来：超界，A 的登记被清掉
    CoreRuntime._env_tag_tick(rt, state)
    assert str(a) not in state["roots"]
    rt._scan_search_dirs = (str(a), str(b))              # A 回来：重新播种，老文件不补打
    CoreRuntime._env_tag_tick(rt, state)
    assert tg.calls == []


def test_h2_the_watcher_loop_gives_the_env_tick_its_own_state(tmp_path):
    """真的监视循环跑一轮：环境标签那一跳拿到的状态不是另外两跳的 seen。"""
    sess = tmp_path / "w" / "session"
    sess.mkdir(parents=True)
    got: dict = {}
    done = threading.Event()
    stub = SimpleNamespace(_state=None, _experiment_log=None, _scan_search_dirs=(str(sess),))
    stub._current_scan_dirs = lambda: (str(sess),)
    stub._map_state_tick = lambda b, p: (b, p)
    stub._chat_export_tick = lambda: None
    stub._map_spectrum_tick = lambda seen: got.__setitem__("map", seen)
    stub._data_ingest_tick = lambda seen: got.__setitem__("ingest", seen)

    def env_tick(state):
        got["env"] = state
        done.set()

    stub._env_tag_tick = env_tick
    CoreRuntime._start_map_activity_watcher(stub, interval_s=0.5)
    try:
        assert done.wait(15)
    finally:
        stub._map_watch_stop.set()
        stub._map_watch_thread.join(10)
    assert got["env"] is not got["map"] and got["env"] is not got["ingest"]


# ── H3：实验文件夹副本 ───────────────────────────────────────────────────


def test_h3_a_landed_copy_is_handed_over_and_a_failed_one_is_not(tmp_path):
    tg = _RecordingTagger()
    rt = _rt(_env_tagger=tg)
    dest = tmp_path / "exp" / "samples" / "S01" / "raw" / "sxm" / "Topo_0001.sxm"
    CoreRuntime._on_ingest_result(rt, None, SimpleNamespace(ok=True, dest=dest))
    CoreRuntime._on_ingest_result(rt, None, SimpleNamespace(ok=False, dest=dest))
    CoreRuntime._on_ingest_result(rt, None, SimpleNamespace(ok=True, dest=None))
    assert tg.calls == [("copy", [str(dest)])]


def test_h3_the_ingest_sink_calls_the_hook_it_was_given_after_construction(tmp_path):
    from mast.logging.v2.ingest_sink import QueuedIngestSink

    seen: list = []
    sink = QueuedIngestSink(None)
    try:
        sink.set_on_result(lambda job, res: seen.append((job.path, res.disposition)))
        src = _file(tmp_path / "w" / "session" / "Topo_0001.sxm", age_s=60)
        exp = tmp_path / "w" / "exp"
        (exp / "samples" / "S01").mkdir(parents=True)
        sink.submit([src], exp_dir=exp, sample_dir_name="S01", source="manual")
        assert sink.flush(timeout=20)
    finally:
        sink.close()
    assert seen and seen[0][0] == src


# ── 读数环与记录器的接线 ─────────────────────────────────────────────────


class _Gauge(EnvironmentSensor):
    def name(self):
        return "vac"

    def read(self):
        return SensorReading(value=3e-8, unit="Pa", status="ok")


def test_the_monitor_loop_feeds_the_ring_through_the_sink_protocol():
    ring = CoreRuntime._build_env_tag_ring(SimpleNamespace())
    assert isinstance(ring, ReadingRing)
    mon = EnvironmentMonitor([_Gauge()], storage=None, interval_s=0.05, sinks=[ring])
    mon.start()
    try:
        deadline = time.monotonic() + 10
        while not ring.snapshot("vac") and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        mon.stop()
    entries = ring.snapshot("vac")
    assert entries and entries[-1][1:] == (3e-8, "Pa", "ok")


def _ring_wiring(fn_src: str) -> str:
    """setup() 怎么把读数环交给监视器。两种接法，**只能是其中一种**：

    * ``"sinks"``：监视器构造之前 ``sinks.append(self._env_tag_ring)``（环在循环里、
      入库之后喂）；
    * ``"live_sinks"``：构造时 ``EnvironmentMonitor(..., live_sinks=[...self._env_tag_ring...])``，且没有再
      append 到 sinks（在读数线程上喂，带 ts=读数时刻，归档卡住时环也不落后）。

    两种都有、都没有、append 在监视器之后、记录器不在监视器之后建 ⇒ AssertionError，带原因。
    语句按**源码顺序**排（ast.walk 是广度优先，不是源码顺序）。"""
    tree = ast.parse(textwrap.dedent(fn_src))
    nodes = sorted((n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.Expr))),
                   key=lambda n: (n.lineno, n.col_offset))
    text = [ast.unparse(n) for n in nodes]
    mon = [i for i, s in enumerate(text) if s.startswith("self._monitor = EnvironmentMonitor(")]
    assert len(mon) == 1, f"expected one monitor construction, found {len(mon)}"
    i_mon = mon[0]
    earlier = {ast.unparse(n.targets[0]): n.value for n in nodes[:i_mon]
               if isinstance(n, ast.Assign) and len(n.targets) == 1}

    def mentions_ring(expr) -> bool:
        if isinstance(expr, ast.Name) and expr.id in earlier:      # live_sinks=live，live 在前面赋过值
            expr = earlier[expr.id]
        return any(ast.unparse(x) == "self._env_tag_ring" for x in ast.walk(expr))

    live = any(kw.arg == "live_sinks" and mentions_ring(kw.value) for kw in nodes[i_mon].value.keywords)
    appends = [i for i, s in enumerate(text) if s == "sinks.append(self._env_tag_ring)"]
    assert not (live and appends), "the ring is wired twice: appended to sinks AND passed as live_sinks"
    assert live or appends, "the ring is not wired into the monitor at all"
    assert all(i < i_mon for i in appends), "sinks.append(self._env_tag_ring) comes after the monitor is built"
    tag = [i for i, s in enumerate(text) if s == "self._env_tagger = self._build_env_tagger()"]
    assert tag and tag[0] > i_mon, "the tagger must be built after the monitor"
    return "live_sinks" if live else "sinks"


def test_setup_hands_the_ring_to_the_monitor_one_way_and_builds_the_tagger_after():
    assert _ring_wiring(source_of(CoreRuntime.setup)) in ("sinks", "live_sinks")


_SETUP_SINKS = """
def setup(self):
    sinks = []
    self._env_tag_ring = self._build_env_tag_ring()
    if self._env_tag_ring is not None:
        sinks.append(self._env_tag_ring)
    self._monitor = EnvironmentMonitor(sensors, storage=storage, sinks=sinks)
    self._env_tagger = self._build_env_tagger()
"""
_SETUP_LIVE = """
def setup(self):
    sinks = []
    self._env_tag_ring = self._build_env_tag_ring()
    self._monitor = EnvironmentMonitor(sensors, storage=storage, sinks=sinks,
                                       live_sinks=[self._env_tag_ring])
    self._env_tagger = self._build_env_tagger()
"""
_SETUP_LIVE_VAR = """
def setup(self):
    self._env_tag_ring = self._build_env_tag_ring()
    live = [self._env_tag_ring] if self._env_tag_ring is not None else []
    self._monitor = EnvironmentMonitor(sensors, storage=storage, sinks=[], live_sinks=live)
    self._env_tagger = self._build_env_tagger()
"""


@pytest.mark.parametrize(("src", "form"), [(_SETUP_SINKS, "sinks"), (_SETUP_LIVE, "live_sinks"),
                                           (_SETUP_LIVE_VAR, "live_sinks")])
def test_the_wiring_judge_accepts_either_form(src, form):
    assert _ring_wiring(src) == form


@pytest.mark.parametrize(("src", "why"), [
    (_SETUP_LIVE.replace("    self._monitor", "    sinks.append(self._env_tag_ring)\n    self._monitor"),
     "wired twice"),
    (_SETUP_SINKS.replace("        sinks.append(self._env_tag_ring)\n", "        pass\n"), "not wired"),
    (_SETUP_SINKS.replace("    self._env_tagger = self._build_env_tagger()\n", "")
     .replace("    sinks = []\n", "    sinks = []\n    self._env_tagger = self._build_env_tagger()\n"),
     "tagger must be built after"),
    (_SETUP_SINKS.replace("    if self._env_tag_ring is not None:\n        sinks.append(self._env_tag_ring)\n", "")
     .replace("    self._env_tagger", "    sinks.append(self._env_tag_ring)\n    self._env_tagger"),
     "after the monitor is built"),
])
def test_the_wiring_judge_rejects_everything_else(src, why):
    with pytest.raises(AssertionError, match=why):
        _ring_wiring(src)


def test_shutdown_stops_the_tagger_after_draining_ingest():
    src = source_of(CoreRuntime.shutdown)
    assert src.index("ing.close()") < src.index("tagger.stop(timeout=2.0)")


def test_the_runtime_builds_a_tagger_from_cached_sources_and_registers_it():
    from mast.envhistory.filetags.tagger import get_tagger

    mon = EnvironmentMonitor([_Gauge()], storage=None)
    rt = SimpleNamespace(_monitor=mon, _env_tag_ring=ReadingRing())
    rt._env_latest_readings = lambda: CoreRuntime._env_latest_readings(rt)
    rt._env_sensor_meta = lambda: CoreRuntime._env_sensor_meta(rt)
    tg = CoreRuntime._build_env_tagger(rt)
    assert tg is not None and get_tagger() is tg
    assert CoreRuntime._env_sensor_meta(rt)["vac"].kind == "other"   # 认不出的类、还没读过：没单位
