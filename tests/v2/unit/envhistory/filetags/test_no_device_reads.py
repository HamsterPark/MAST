# -*- coding: utf-8 -*-
"""设计硬约束：打标路径**只读内存缓存，绝不碰设备**。

625 的链路锁与磁场扫场监督共用；启动时可能因 GIL 争用认不出 625；保存路径卡在串口上
会连带卡住数据文件写入。所以：不调传感器的任何读方法（基类的 ``is_healthy()`` 里就是一次
``read()``），不调 ``read_all()``，不碰链路；读数过期了照记时刻与早了多久，**不为刷新去重读**。

证明方式：真 ``EnvironmentMonitor`` 里放「一被碰就记账、一被读就失败」的假传感器（一个 625、
一个 DL-7，都是真驱动类的子类，归类走的就是生产路径），它的缓存（``_latest``）与读数环照
监视器循环本来会留下的样子预置好，然后走完三处钩子 → 工作线程 → 写出 sidecar：记账必须是空的，
而 sidecar 里有缓存里的那些数。另加一条源码扫描。
"""
from __future__ import annotations

import ast
import json
import os
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from mast.core.runtime import CoreRuntime
from mast.core.types import SensorReading
from mast.environment.dl7_vacuum import DL7VacuumSensor
from mast.environment.lakeshore_625 import Model625Sensor
from mast.environment.monitor import EnvironmentMonitor
from mast.envhistory.filetags import sidecar as SC
from mast.envhistory.filetags.ring import ReadingRing
from tests.v2.srcref import source_of


class _Trap:
    def __init__(self) -> None:
        self.armed = False
        self.touched: list[str] = []

    def hit(self, what: str) -> None:
        if self.armed:
            self.touched.append(what)


class _TrapLink:
    """625 的链路替身：装好之后**任何**属性访问都记账。"""

    def __init__(self, trap: _Trap) -> None:
        object.__setattr__(self, "_t", trap)

    def __getattribute__(self, name):
        trap = object.__getattribute__(self, "_t")
        trap.hit(f"link.{name}")
        if name == "field_unit":                 # 构造时会读一次（那时还没装好）
            return "T"
        raise AttributeError(name)


class _Trap625(Model625Sensor):
    def __init__(self, trap: _Trap) -> None:
        super().__init__(_TrapLink(trap), name="magnet_field", quantity="field")
        self._trap = trap

    def read(self):
        self._trap.hit("625.read")
        raise AssertionError("the tag path read the 625")

    def is_healthy(self):
        self._trap.hit("625.is_healthy")
        raise AssertionError("the tag path asked the 625 for its health (that is a read)")

    def instrument_state(self):
        self._trap.hit("625.instrument_state")
        raise AssertionError("the tag path read the 625 snapshot over its link")

    @property
    def link(self):
        self._trap.hit("625.link")
        raise AssertionError("the tag path touched the 625 link")

    def close(self):
        self._trap.hit("625.close")


class _TrapDL7(DL7VacuumSensor):
    def __init__(self, trap: _Trap) -> None:
        super().__init__(name="DL-7 真空计 (COM15)", port="COM15")
        self._trap = trap

    def read(self):
        self._trap.hit("dl7.read")
        raise AssertionError("the tag path read the gauge")

    def is_healthy(self):
        self._trap.hit("dl7.is_healthy")
        raise AssertionError("the tag path asked the gauge for its health (that is a read)")

    def close(self):
        self._trap.hit("dl7.close")


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t).isoformat()


def _file(p: Path, mtime: float, body: bytes = b"NANONIS" * 100) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    os.utime(p, (mtime, mtime))
    return p


@pytest.fixture()
def live_v2(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST_DATA_DIR", str(tmp_path / "data_root"))
    from mast.logging.v2.live import open_live_v2
    repos, eid = open_live_v2()
    assert repos is not None and eid
    return repos, eid


def _rig(tmp_path, live_v2, cache_age_s: float):
    """``cache_age_s``：缓存里的读数比数据文件的保存时刻早多少秒。"""
    repos, eid = live_v2
    trap = _Trap()
    mon = EnvironmentMonitor([_TrapDL7(trap), _Trap625(trap)], storage=None)   # 不启动循环
    t_file = time.time() - 120.0
    t_read = t_file - cache_age_s
    # 监视器循环本来会留下的缓存：get_latest() 的那一份与读数环里的逐条读数
    mon._latest = {
        "DL-7 真空计 (COM15)": SensorReading(value=3.1e-8, unit="Pa", status="ok", timestamp=_iso(t_read)),
        "magnet_field": SensorReading(value=2.0, unit="T", status="ok", timestamp=_iso(t_read)),
    }
    clk = [t_read - 4.0]
    ring = ReadingRing(clock=lambda: clk[0], horizon_s=1e9)
    for _ in range(3):
        ring.write("DL-7 真空计 (COM15)", 3.1e-8, "Pa", "ok")
        ring.write("magnet_field", 2.0, "T", "ok")
        clk[0] += 2.0
    sess = tmp_path / "w" / "session"
    sess.mkdir(parents=True)
    rt = SimpleNamespace(_monitor=mon, _env_tag_ring=ring, _scan_search_dirs=(str(sess),),
                         _v2_repos=repos, _v2_eid=eid, _active_thread_id=None)
    rt._env_latest_readings = lambda: CoreRuntime._env_latest_readings(rt)
    rt._env_sensor_meta = lambda: CoreRuntime._env_sensor_meta(rt)
    rt._env_tagger_or_none = lambda: CoreRuntime._env_tagger_or_none(rt)
    rt._submit_env_tags = lambda paths, origin="skill": CoreRuntime._submit_env_tags(rt, paths, origin=origin)
    rt._env_tagger = CoreRuntime._build_env_tagger(rt)
    assert rt._env_tagger is not None
    trap.armed = True                            # 从这里起，碰一下都算
    return SimpleNamespace(trap=trap, rt=rt, sess=sess, t_file=t_file, root=tmp_path / "w")


def _run_all_three_hooks(rig) -> list[Path]:
    rt = rig.rt
    state: dict = {}
    CoreRuntime._env_tag_tick(rt, state)                                   # H2 先登记
    watched = _file(rig.sess / "Topo_0001.sxm", rig.t_file)
    CoreRuntime._env_tag_tick(rt, state)                                   # H2 交出新文件
    skill = _file(rig.root / "working-sessions" / "Bias-Spectroscopy001.dat", rig.t_file,
                  body=b"SPECTRUM" * 80)
    # H1 —— payload 的形状照技能适配器与直调（skill_adapter / direct_exec 把 data.path
    # 解析进 artifact_path）
    assert CoreRuntime._record_v2_action(rt, {"skill": "AcquireSTS", "params": {}, "success": True,
                                              "data": {"path": str(skill)},
                                              "artifact_path": str(skill)})
    copy = _file(rig.root / "exp" / "samples" / "S01" / "raw" / "dat" / skill.name,
                 rig.t_file, body=skill.read_bytes())
    os.utime(copy, ns=(skill.stat().st_mtime_ns, skill.stat().st_mtime_ns))
    CoreRuntime._on_ingest_result(rt, None, SimpleNamespace(ok=True, dest=copy))  # H3
    assert rt._env_tagger.drain(20)
    return [watched, skill, copy]


def test_all_three_hooks_tag_from_the_cache_and_never_touch_a_sensor(tmp_path, live_v2):
    rig = _rig(tmp_path, live_v2, cache_age_s=1.0)
    files = _run_all_three_hooks(rig)
    for f in files:
        doc = json.loads(SC.sidecar_path(f).read_text(encoding="utf-8"))
        vals = {v["kind"]: v for v in doc["values"]}
        assert vals["pressure"]["value"] == 3.1e-8 and vals["pressure"]["fresh"] is True
        assert vals["field"]["value"] == 2.0 and vals["field"]["driver"] == "Model625Sensor"
    assert rig.trap.touched == []


def test_stale_cache_is_recorded_with_its_age_and_still_nothing_is_read(tmp_path, live_v2):
    rig = _rig(tmp_path, live_v2, cache_age_s=600.0)
    files = _run_all_three_hooks(rig)
    doc = json.loads(SC.sidecar_path(files[0]).read_text(encoding="utf-8"))
    for v in doc["values"]:
        assert v["fresh"] is False and v["dt_s"] == pytest.approx(-600.0, abs=0.01)
    assert rig.trap.touched == []                 # 过期了也不为刷新去读


# ── 源码扫描：这些名字在打标代码里一次都不许出现 ─────────────────────────

FORBIDDEN = {"read_all", "is_healthy", "check_health", "instrument_state", "cryostat_states",
             "magnet_controller", "fast_state", "read_state", "read_level", "link"}

RUNTIME_METHODS = ("_build_env_tag_ring", "_build_env_tagger", "_env_latest_readings",
                   "_env_sensor_meta", "_env_tagger_or_none", "_submit_env_tags",
                   "_env_tag_tick", "_on_ingest_result")


def _code_names(src: str) -> set[str]:
    """代码里真正用到的名字：属性、变量、``getattr(x, "名字")``。不看注释与文档串。"""
    out: set[str] = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Attribute):
            if isinstance(n.value, ast.Name) and n.value.id == "os":
                continue                       # os.link（不覆盖地落地 sidecar）不是设备链路
            out.add(n.attr)
        elif isinstance(n, ast.Name):
            out.add(n.id)
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in
              ("getattr", "hasattr", "setattr") and len(n.args) >= 2
              and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str)):
            out.add(n.args[1].value)
    return out


def test_the_tag_code_never_names_a_device_read():
    import mast.envhistory.filetags as pkg

    pkg_dir = Path(pkg.__file__).parent
    for f in sorted(pkg_dir.glob("*.py")):
        bad = _code_names(f.read_text(encoding="utf-8")) & FORBIDDEN
        assert not bad, f"{f.name} uses {sorted(bad)}"
    for name in RUNTIME_METHODS:
        bad = _code_names(source_of(getattr(CoreRuntime, name))) & FORBIDDEN
        assert not bad, f"CoreRuntime.{name} uses {sorted(bad)}"


def test_the_scan_would_catch_one():
    """扫描本身得会响（扫描 0 命中也可能只是没扫到）。"""
    assert _code_names("def f(mon):\n    return mon.read_all()\n") & FORBIDDEN == {"read_all"}
    assert _code_names("def f(s):\n    return getattr(s, 'link', None)\n") & FORBIDDEN == {"link"}
    assert _code_names('def f():\n    """calls read_all"""\n    return 1\n') & FORBIDDEN == set()
