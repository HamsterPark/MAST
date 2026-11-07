# -*- coding: utf-8 -*-
"""D 件 — MeasureSpectraAtSites：在标注页位点上逐点测谱（composite）。

mock ReadSiteMarks 给站位、mock 回写、FakeCtx 跑 GraphExecutor。不碰仪器。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from mast.core.types import SkillResult
from mast.skills.composite.graph_executor import CompositeProgress
from mast.skills.composite import measure_spectra_at_sites as D
from mast.skills.composite.measure_spectra_at_sites import MeasureSpectraAtSites


@dataclass
class FakeCtx:
    """最小上下文：run() 跑子技能，可令指定技能失败；带 composite 的可选进度钩子。"""
    run_log: list = field(default_factory=list)
    fail_skills: set = field(default_factory=set)
    prior_progress: dict | None = None
    emitted: list = field(default_factory=list)
    flushes: int = 0

    def run(self, skill_name: str, params: dict) -> SkillResult:
        self.run_log.append((skill_name, dict(params)))
        if skill_name in self.fail_skills:
            return SkillResult(skill_name=skill_name, success=False, error="forced fail")
        return SkillResult(skill_name=skill_name, success=True, data={"num_points": 256})

    def emit_progress(self, progress) -> None:
        self.emitted.append(CompositeProgress.from_dict(progress.to_dict()))

    def get_progress(self, name: str):
        return self.prior_progress

    def checkpoint_flush(self) -> None:
        self.flushes += 1


def _sites(n):
    return [{"id": f"s{i}", "n": i + 1, "kind": "mol", "wide": True, "low": False,
             "scanner_x_m": (-76.0 + i) * 1e-9, "scanner_y_m": 150.0e-9} for i in range(n)]


class _StubExec:
    """plan_dynamic 只用到 executor.progress.partial_data + set_partial。"""
    def __init__(self, partial=None):
        self.progress = type("P", (), {"partial_data": dict(partial or {})})()
    def set_partial(self, k, v):
        self.progress.partial_data[k] = v


def _plan_steps(params, partial=None):
    return list(MeasureSpectraAtSites().plan_dynamic(params, _StubExec(partial)))


@pytest.fixture
def _mock(monkeypatch):
    """mock ReadSiteMarks（给站位）+ 捕获回写。返回可配的 state。"""
    state = {"sites": _sites(3), "coords_trustworthy": True,
             "doc": {"plan/marking": {"done": True}}, "warnings": [], "patches": []}

    class FakeRead:
        def execute(self, context, params):
            return SkillResult(skill_name="ReadSiteMarks", success=True, data={
                "sites": state["sites"], "coords_trustworthy": state["coords_trustworthy"],
                "doc": state["doc"], "warnings": state["warnings"], "count": len(state["sites"])})

    monkeypatch.setattr("mast.skills.builtins.site_marking_read.ReadSiteMarks", FakeRead)
    monkeypatch.setattr(D, "_apply_patch_quiet",
                        lambda page_id, patch: state["patches"].append((page_id, patch)))
    return state


# ── plan 结构 ────────────────────────────────────────────────────────────────

def test_plan_is_configure_then_move_sts_per_site():
    plan = _plan_steps({"_sites": _sites(2), "page_id": "p1"})
    assert plan[0].step_id == "configure" and plan[0].skill_name == "ConfigureSTS"
    ids = [s.step_id for s in plan]
    assert ids == ["configure", "move_s0", "sts_s0", "move_s1", "sts_s1"]
    # AcquireSTS 带 ramp
    sts = next(s for s in plan if s.step_id == "sts_s0")
    assert sts.skill_name == "AcquireSTS"
    assert sts.params["ramp_before"] is True and sts.params["restore_after"] is True


def test_plan_inserts_drift_every_n():
    # 旧方法（drift_method=reference_scan）：5 站位、track_every=2 ⇒ 在 i=2、i=4 前各插一次
    # （i=0 被 i>0 排除）。默认方法现在是 molecules（见 test_measure_spectra_at_sites_drift.py）。
    plan = _plan_steps({"_sites": _sites(5), "page_id": "p", "track_every": 2,
                        "drift_method": "reference_scan"})
    drift = [s for s in plan if s.step_id.startswith("drift_")]
    assert [s.skill_name for s in drift] == ["TrackDrift_ReferenceScan", "TrackDrift_ReferenceScan"]


# ── 全程执行 + 回写 ──────────────────────────────────────────────────────────

def test_full_run_measures_each_site_and_writes_back(_mock):
    ctx = FakeCtx()
    res = MeasureSpectraAtSites().run_composite(ctx, {"page_id": "p1"})
    assert res.success, res.error
    names = [n for n, _ in ctx.run_log]
    assert names.count("ConfigureSTS") == 1
    assert names.count("MoveToXY") == 3 and names.count("AcquireSTS") == 3
    assert res.data["succeeded"] == 3 and res.data["failed"] == 0
    # 回写：每点 status=done + status/live；收尾 finished
    site_done = [p for _, p in _mock["patches"]
                 if p.get("target") == "sites" and p.get("data", {}).get("status") == "done"]
    assert len(site_done) == 3
    live = [p for _, p in _mock["patches"] if p.get("path") == "status/live"]
    assert live and live[-1]["data"]["phase"] == "finished"


# ── 闸门 ────────────────────────────────────────────────────────────────────

def test_refuses_when_coords_not_trustworthy(_mock):
    _mock["coords_trustworthy"] = False
    _mock["warnings"] = ["scan angle unverified"]
    ctx = FakeCtx()
    res = MeasureSpectraAtSites().run_composite(ctx, {"page_id": "p1"})
    assert not res.success
    assert not any(n == "AcquireSTS" for n, _ in ctx.run_log), "坐标不可信不该开测"


def test_coords_gate_can_be_disabled(_mock):
    _mock["coords_trustworthy"] = False
    ctx = FakeCtx()
    res = MeasureSpectraAtSites().run_composite(
        ctx, {"page_id": "p1", "require_coords_trustworthy": False})
    assert res.success and res.data["succeeded"] == 3


def test_refuses_when_marking_not_done(_mock):
    _mock["doc"] = {"plan/marking": {"done": False}}
    ctx = FakeCtx()
    res = MeasureSpectraAtSites().run_composite(ctx, {"page_id": "p1"})
    assert not res.success and "marking" in (res.error or "").lower() + (res.error or "")
    assert not any(n == "AcquireSTS" for n, _ in ctx.run_log)


def test_marking_gate_can_be_disabled(_mock):
    _mock["doc"] = {"plan/marking": {"done": False}}
    ctx = FakeCtx()
    res = MeasureSpectraAtSites().run_composite(
        ctx, {"page_id": "p1", "require_marking_done": False})
    assert res.success and res.data["succeeded"] == 3


def test_no_usable_sites_fails(_mock):
    _mock["sites"] = [{"id": "s0", "n": 1, "scanner_x_m": None, "scanner_y_m": None}]
    ctx = FakeCtx()
    res = MeasureSpectraAtSites().run_composite(ctx, {"page_id": "p1"})
    assert not res.success


# ── 连续失败停机 ─────────────────────────────────────────────────────────────

def test_stops_after_consecutive_failures(_mock):
    _mock["sites"] = _sites(10)
    ctx = FakeCtx(fail_skills={"AcquireSTS"})
    res = MeasureSpectraAtSites().run_composite(
        ctx, {"page_id": "p1", "stop_after_consecutive_failures": 3})
    assert not res.success
    # 连续 3 次失败就停：绝不会把 10 个点都测一遍（generator 在发下一个点前读 streak 停）。
    n_sts = sum(1 for n, _ in ctx.run_log if n == "AcquireSTS")
    assert n_sts < 10, f"应提前停，实际跑了 {n_sts} 个 AcquireSTS"
    assert "连续" in (res.error or "") or "consecutive" in (res.error or "")


def test_no_stop_runs_all_when_threshold_zero(_mock):
    # stop_after_consecutive_failures=0 ⇒ 不因失败停，10 个点都尝试。
    _mock["sites"] = _sites(10)
    ctx = FakeCtx(fail_skills={"AcquireSTS"})
    res = MeasureSpectraAtSites().run_composite(
        ctx, {"page_id": "p1", "stop_after_consecutive_failures": 0})
    assert sum(1 for n, _ in ctx.run_log if n == "AcquireSTS") == 10
