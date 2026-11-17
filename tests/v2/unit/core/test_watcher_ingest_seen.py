# -*- coding: utf-8 -*-
"""文件监视线程：归档那一跳与环境标签那一跳各自的 ``seen``，按监视根播种。

1. 既有缺陷：监视线程每 4 拍先跑 ``_map_spectrum_tick`` 再跑
   ``_data_ingest_tick``，两跳共用一个 ``seen``。前者对「至少存了 5 s」的新 .dat/.3ds
   写 ``seen[p] = mtime``（裸浮点），后者见到 ``prev == st_mtime`` 就当「已收」—— 于是
   第一次被看到时已存了 ≥ 5 s 的手动谱永远进不了实验文件夹。
2. 按根播种：只在第一次调用时播种的话，之后才进监视集合的根（Nanonis 会话
   目录晚解析出来、操作员切到一个有旧文件的会话文件夹）里的老文件全被当成新存的 —— 归进
   当前样品、被补写「保存时记录」的标签。
3. 归档那一跳的既有缺陷：超过 64 个的那部分永不归档；``seen`` 只增不减；
   正在长的 .3ds 每长一个点归档一份。

这里的测试不手抄监视线程的接线：先用 stub 把**真的**监视循环跑一轮，截下它交给各跳的
字典，再按原顺序（map → ingest → 环境标签）跑真的 tick。截下字典之后的第一轮就是监视线程的
第 0 拍（启动那一轮）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mast.core import runtime as rtmod
from mast.core.runtime import CoreRuntime

_DAT = "Experiment\tbias spectroscopy\n\n[DATA]\nBias (V)\tCurrent (A)\n0\t0\n"


def _write(path: Path, age_s: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_DAT, encoding="utf-8")
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


def _grow(path: Path, age_s: float) -> Path:
    """网格谱又写进去一个点。"""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("0\t0\n")
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


class _Tagger:
    def __init__(self) -> None:
        self.got: list[str] = []
        self.enabled_cached = True

    def refresh_settings(self):
        return True, 30.0

    def submit(self, paths, *, origin, observed_at=None):
        self.got.extend(str(p) for p in paths)
        return len(paths)


def _stub(dirs: list, exp: Path, submitted: list, tagger: _Tagger) -> SimpleNamespace:
    """够三个 tick 与监视循环用的最小 runtime。没有当前实验 ⇒ 不写手动 marker。

    监视的根由 ``dirs`` 决定（可变：测试往里加根，模拟会话目录晚出现或被切换）。"""
    stub = SimpleNamespace(
        _storage=object(),
        _experiment_log=SimpleNamespace(current_experiment_id=None, current_sample_id=None),
        _map_last_skill_ts=0.0,
        _scan_search_dirs=tuple(dirs),
        _ingest=SimpleNamespace(
            submit=lambda paths, **kw: submitted.extend(str(p) for p in paths)),
        _v2_eid="",
        _state=None,
    )
    stub._current_scan_dirs = lambda: tuple(dirs)
    stub._ingest_watcher_enabled = lambda: True
    stub._ensure_scope_dirs = lambda: (exp, "S01")
    stub._persist_ingest_state = lambda exp_dir, seen: None
    stub._synth_manual_action = lambda p: "aid"
    stub._quarantine_file = lambda p: submitted.append(f"quarantine:{p}")
    stub._note_stale_session_path = lambda p: None
    stub._ingest_copy_mode = lambda: "copy"
    stub._map_state_tick = lambda baseline, prev: (baseline, prev)
    stub._chat_export_tick = lambda: None
    stub._env_tagger_or_none = lambda: tagger
    return stub


def _dicts_from_the_real_watcher(stub) -> tuple[dict, dict]:
    """跑一轮真的监视循环，截下它交给两跳的字典（这一轮不跑 tick 的本体）。"""
    got: dict = {}
    done = threading.Event()

    def map_tick(seen):
        got["map"] = seen

    def ingest_tick(seen):
        got["ingest"] = seen

    def env_tick(state):
        got["env"] = state
        done.set()

    saved = {k: getattr(stub, k, None) for k in ("_map_spectrum_tick", "_data_ingest_tick", "_env_tag_tick")}
    stub._map_spectrum_tick, stub._data_ingest_tick, stub._env_tag_tick = map_tick, ingest_tick, env_tick
    CoreRuntime._start_map_activity_watcher(stub, interval_s=0.5)
    try:
        assert done.wait(15), "the watcher loop never reached its first file round"
    finally:
        stub._map_watch_stop.set()
        stub._map_watch_thread.join(10)
    assert not stub._map_watch_thread.is_alive()
    for k, v in saved.items():
        setattr(stub, k, v)
    assert got["env"] == {}                       # 环境标签那一跳从一个空状态开始
    return got["map"], got["ingest"]


def _round(stub, map_seen: dict, ingest_seen: dict, env_state: dict | None) -> None:
    CoreRuntime._map_spectrum_tick(stub, map_seen)          # 原顺序：map → ingest → 环境标签
    CoreRuntime._data_ingest_tick(stub, ingest_seen)
    if env_state is not None:
        CoreRuntime._env_tag_tick(stub, env_state)


def _rounds(stub, map_seen: dict, ingest_seen: dict, n: int = 3, env_state: dict | None = None) -> None:
    for _ in range(n):
        _round(stub, map_seen, ingest_seen, env_state)


@pytest.fixture()
def world(tmp_path):
    base = tmp_path / "w"                      # tmp_path 里另有 _sidecars/，场景放子目录
    sess, exp = base / "session", base / "exp"
    sess.mkdir(parents=True)
    exp.mkdir()
    submitted: list = []
    dirs: list = [str(sess)]
    tagger = _Tagger()
    return SimpleNamespace(base=base, sess=sess, exp=exp, submitted=submitted, dirs=dirs, tagger=tagger,
                           stub=_stub(dirs, exp, submitted, tagger))


def _start(world, *, env: bool = False):
    """截下字典，再跑监视线程的第 0 拍（启动那一轮：只播种）。"""
    map_seen, ingest_seen = _dicts_from_the_real_watcher(world.stub)
    env_state: dict | None = {} if env else None
    _round(world.stub, map_seen, ingest_seen, env_state)
    return map_seen, ingest_seen, env_state


# ── 归档那一跳自己的 seen ──────────────────────────────────────────


def test_the_ingest_tick_gets_its_own_dict_seeded_with_the_startup_baseline(world):
    old = _write(world.sess / "old.dat", age_s=3600)
    map_seen, ingest_seen = _dicts_from_the_real_watcher(world.stub)
    assert map_seen is not ingest_seen
    # 拷贝了启动基线，而不是一个空字典：老文件在两边都登记着。
    assert str(old) in map_seen and str(old) in ingest_seen
    assert ingest_seen[str(old)] == map_seen[str(old)]


@pytest.mark.parametrize("age_s", [1.0, 6.0, 30.0])
def test_a_manual_spectrum_reaches_ingest_exactly_once_whatever_its_age_when_first_seen(world, age_s):
    old = _write(world.sess / "old.dat", age_s=3600)
    map_seen, ingest_seen, _ = _start(world)
    new = _write(world.sess / f"Bias-Spectroscopy{int(age_s):03d}.dat", age_s=age_s)
    _rounds(world.stub, map_seen, ingest_seen)
    assert world.submitted.count(str(new)) == 1, world.submitted
    assert str(old) not in world.submitted
    # map 那一跳照旧把它记成处理过（裸 mtime），说明它的去重没被动过
    if age_s >= 5.0:
        assert map_seen[str(new)] == os.path.getmtime(new)


def test_files_already_on_disk_at_startup_are_not_bulk_ingested(world):
    """启动时就在盘上的 .dat/.3ds/.sxm 一个都不收（.sxm 原来不在启动基线里，前 64 个会被收走）。"""
    olds = [_write(world.sess / f"old{i:02d}.dat", age_s=3600 + i) for i in range(5)]
    olds += [_write(world.sess / "sub" / f"Grid Spectroscopy{i:03d}.3ds", age_s=7200 + i) for i in range(3)]
    olds += [_write(world.sess / f"Topo_{i:03d}.sxm", age_s=7200 + i) for i in range(3)]
    map_seen, ingest_seen, _ = _start(world)
    _rounds(world.stub, map_seen, ingest_seen)
    assert world.submitted == []


def test_a_rewritten_startup_file_is_ingested_once(world):
    """启动基线里的老文件被同名重写（Nanonis 重用编号）⇒ 是新测量，照收一次。"""
    old = _write(world.sess / "reused.dat", age_s=3600)
    map_seen, ingest_seen, _ = _start(world)
    _write(old, age_s=10.0)
    _rounds(world.stub, map_seen, ingest_seen)
    assert world.submitted.count(str(old)) == 1


# ── 按监视根播种────────────────────────────────────────


def test_a_directory_that_joins_later_gives_up_none_of_its_old_files(world):
    """B 在启动之后才进监视集合：它的老文件既不归档、也不打标；之后在 B 里新存的照常处理。"""
    b = world.base / "sessB"
    olds = [_write(b / f"Bias-Spectroscopy{i:03d}.dat", age_s=86400 + i) for i in range(3)]
    olds += [_write(b / f"Grid Spectroscopy{i:03d}.3ds", age_s=86400 + i) for i in range(2)]
    olds += [_write(b / f"Topo_{i:03d}.sxm", age_s=86400 + i) for i in range(2)]
    map_seen, ingest_seen, env_state = _start(world, env=True)
    world.dirs.append(str(b))
    _rounds(world.stub, map_seen, ingest_seen, env_state=env_state)
    assert world.submitted == [] and world.tagger.got == []
    new = _write(b / "Bias-Spectroscopy100.dat", age_s=1.0)
    _rounds(world.stub, map_seen, ingest_seen, env_state=env_state)
    assert world.submitted == [str(new)]
    assert world.tagger.got == [str(new)]


def test_a_new_directory_gives_up_a_file_saved_before_its_first_walk_but_not_its_old_ones(world):
    """B 先进监视集合、后出现（新建的会话目录，或暂时够不着的网络盘）。它第一次被走到之前就存下的
    那一个照常处理（否则永远既不归档也不打标）；带着原 mtime 拷进来的旧文件不算。"""
    map_seen, ingest_seen, env_state = _start(world, env=True)
    b = world.base / "sessB"
    world.dirs.append(str(b))
    _rounds(world.stub, map_seen, ingest_seen, n=1, env_state=env_state)    # 还不存在：不播种
    fresh = _write(b / "Bias-Spectroscopy001.dat", age_s=2.0)
    copied = _write(b / "Bias-Spectroscopy000.dat", age_s=86400.0)
    _rounds(world.stub, map_seen, ingest_seen, env_state=env_state)
    assert world.submitted == [str(fresh)] and str(copied) not in world.submitted
    assert world.tagger.got == [str(fresh)]


def test_the_startup_round_lets_nothing_through_even_a_file_saved_seconds_before(world):
    """启动那一轮不放行（行为不变）：刚存了 2 s 的文件也只登记。"""
    _write(world.sess / "Bias-Spectroscopy001.dat", age_s=2.0)
    map_seen, ingest_seen, env_state = _start(world, env=True)
    _rounds(world.stub, map_seen, ingest_seen, env_state=env_state)
    assert world.submitted == [] and world.tagger.got == []


# 规则：晚出现的根，第一次被走到之前 60 s 内存下的文件照常交出；更早的只登记；
# 启动那一轮一个都不放行。三种情况各钉一条（另有上面的整条链路测试）。


def test_the_first_walk_rule_admits_only_files_inside_the_window_and_nothing_at_startup():
    fresh = rtmod._WATCH_FIRST_WALK_FRESH_S
    now = 1_000_000.0
    assert rtmod._first_walk_admits(now - (fresh - 1.0), now, startup=False) is True     # 窗口内的新文件
    assert rtmod._first_walk_admits(now - fresh, now, startup=False) is True             # 边界上算新
    assert rtmod._first_walk_admits(now - (fresh + 1.0), now, startup=False) is False    # 窗口外的老文件
    assert rtmod._first_walk_admits(now + 5.0, now, startup=False) is True               # mtime 在将来（时钟）
    for age in (0.0, 1.0, fresh - 1.0, fresh + 1.0, 86400.0):
        assert rtmod._first_walk_admits(now - age, now, startup=True) is False           # 启动那一轮


def test_a_late_root_gives_up_a_file_inside_the_window_and_keeps_one_outside_it(world):
    """晚出现的根：窗口内（离窗口边 10 s）的照常归档、打标；窗口外（过边 10 s）的只登记 —— 两跳一样。"""
    fresh = rtmod._WATCH_FIRST_WALK_FRESH_S
    map_seen, ingest_seen, env_state = _start(world, env=True)
    b = world.base / "sessB"
    inside = _write(b / "Bias-Spectroscopy001.dat", age_s=fresh - 10.0)
    outside = _write(b / "Bias-Spectroscopy000.dat", age_s=fresh + 10.0)
    world.dirs.append(str(b))
    _rounds(world.stub, map_seen, ingest_seen, env_state=env_state)
    assert world.submitted == [str(inside)] and world.tagger.got == [str(inside)]
    assert str(outside) in ingest_seen and str(outside) in env_state["files"]            # 登记了，没交出


def test_the_startup_round_holds_back_a_file_inside_the_window_too(world):
    """同样离窗口边 10 s 的文件，启动时就在盘上 ⇒ 一个都不放行（与上一条对照）。"""
    fresh = rtmod._WATCH_FIRST_WALK_FRESH_S
    inside = _write(world.sess / "Bias-Spectroscopy001.dat", age_s=fresh - 10.0)
    sxm = _write(world.sess / "Topo_001.sxm", age_s=fresh - 10.0)
    map_seen, ingest_seen, env_state = _start(world, env=True)
    _rounds(world.stub, map_seen, ingest_seen, env_state=env_state)
    assert world.submitted == [] and world.tagger.got == []
    assert str(inside) in ingest_seen and str(sxm) in ingest_seen and str(sxm) in env_state["files"]


# ── 归档那一跳的既有缺陷────────────────────────────────


def test_more_new_files_than_one_tick_takes_are_all_archived_over_the_next_ticks(world):
    map_seen, ingest_seen, _ = _start(world)
    n = rtmod._INGEST_MAX_PER_TICK + 6
    news = [_write(world.sess / f"Bias-Spectroscopy{i:03d}.dat", age_s=10.0) for i in range(n)]
    _rounds(world.stub, map_seen, ingest_seen, n=1)
    assert len(world.submitted) == rtmod._INGEST_MAX_PER_TICK
    _rounds(world.stub, map_seen, ingest_seen, n=2)
    assert sorted(world.submitted) == sorted(str(p) for p in news)       # 每个恰好一次


def test_a_growing_grid_is_archived_once_after_it_has_settled(world):
    map_seen, ingest_seen, _ = _start(world)
    grid = _write(world.sess / "Grid Spectroscopy001.3ds", age_s=10.0)
    _rounds(world.stub, map_seen, ingest_seen)
    _grow(grid, age_s=5.0)
    _rounds(world.stub, map_seen, ingest_seen)
    assert world.submitted == []
    t = time.time() - (rtmod._INGEST_3DS_SETTLE_S + 30.0)               # 写完、静置够了
    os.utime(grid, (t, t))
    _rounds(world.stub, map_seen, ingest_seen)
    assert world.submitted == [str(grid)]


def test_the_ingest_seen_drops_files_that_are_gone_once_it_is_large(world):
    keep = _write(world.sess / "keep.dat", age_s=3600)
    map_seen, ingest_seen, _ = _start(world)
    gone = {str(world.sess / f"gone{i:05d}.dat"): (1.0, 1) for i in range(rtmod._INGEST_SEEN_MAX + 1)}
    ingest_seen.update(gone)
    _rounds(world.stub, map_seen, ingest_seen, n=1)
    assert not any(p in ingest_seen for p in gone)
    assert str(keep) in ingest_seen and rtmod._INGEST_WALK_KEY in ingest_seen
    assert world.submitted == []


def test_the_ingest_state_file_leaves_the_seeding_state_out(tmp_path):
    """播种状态（一个集合）不进 ingest_state.json —— 进去的话 json.dumps 抛错，被吞掉，文件从此不写。"""
    exp = tmp_path / "w" / "exp"
    exp.mkdir(parents=True)
    a = str(tmp_path / "w" / "a.dat")
    seen = {a: (1.0, 2), rtmod._INGEST_WALK_KEY: {"roots": {str(tmp_path)}}}
    CoreRuntime._persist_ingest_state(SimpleNamespace(), exp, seen)
    doc = json.loads((exp / ".mast" / "ingest_state.json").read_text(encoding="utf-8"))
    assert list(doc) == [a]
