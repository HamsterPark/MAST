"""D 件 — ``MeasureSpectraAtSites`` 的分子漂移追踪装配(FakeCtx,不碰仪器)。

``test_measure_spectra_at_sites.py`` 管逐点测谱 / 回写 / 闸门 / 连续失败停机;这里只管
``track_every>0`` 时 **分子配准** 那条路:

* 走的是 ``ScanAt``(重扫参考帧)→ ``RegisterDriftByMolecules`` → 之后每个位点的 ``MoveToXY`` 目标被
  漂移修正;``drift_method=reference_scan`` 仍走旧的 ``TrackDrift_ReferenceScan``,什么都没退步;
* 配准不可信就用外推、**不**采信这次的值;连续 3 次不可信 → 「漂移丢失」,在**测下一个位点之前**停;
* 还没有任何可信配准时不在未校正的位置上测谱(立刻重配,直到成功或判丢);
* 追踪步骤本身失败(扫描 / 找不到帧 / 技能报错)一律记成一次弱配准;
* 吸附到分子的位点用锚点位移,其余用附近分子的局部位移;θ≠0 时位移经同一份 px_to_m 约定转到扫描器轴;
* 端到端:真的 ``RegisterDriftByMolecules`` 吃合成 .sxm,已知(随时间线性增长的)漂移 → 目标被推到该去的地方。

时钟是假的(``DR._now``),FakeCtx 的 ``run`` 让它往前走。默认步子很小(ScanAt 5 s、AcquireSTS 10 s),
两次配准之间不到 10 分钟,漂移速度恒为 0,目标就是「测得的漂移」,断言可以精确;要测速度外推的用例自己
把 ``ctx.durations`` 设成 ScanAt 100 s、AcquireSTS 400 s(两次配准相隔 ~900 s)。
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest

_MASTV2_ROOT = str(Path(__file__).resolve().parents[5] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _n in list(sys.modules):
    if _n == "mast" or _n.startswith("mast."):
        _f = getattr(sys.modules[_n], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_n]

from mast.core.types import SkillResult  # noqa: E402
from mast.i18n import use_lang  # noqa: E402
from mast.skills.builtins import drift_register as DR  # noqa: E402
from mast.skills.builtins.drift_register import RegisterDriftByMolecules, frame_delta_to_scanner_m  # noqa: E402
from mast.skills.builtins.site_marking_read import FrameGeometry, frame_nm_to_scanner_m  # noqa: E402
from mast.skills.composite import measure_spectra_at_sites as D  # noqa: E402
from mast.skills.composite.measure_spectra_at_sites import MeasureSpectraAtSites  # noqa: E402

from tests.v2.unit.skills._drift_synth import render, scene, write_sxm  # noqa: E402

CX, CY, WNM = -75.0, 153.0, 10.0          # 参考帧:帧心 (-75, 153) nm,边长 10 nm
T_START = 2_000_000_000.0
LOST = ("漂移丢失", "Drift lost")
_CJK = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")


# ═══════════════════════════════════════════════════════════════════════════
# 世界:假时钟、假上下文、假 ReadSiteMarks
# ═══════════════════════════════════════════════════════════════════════════

class Clock:
    def __init__(self) -> None:
        self.t = T_START

    def __call__(self) -> float:
        return self.t


@dataclass
class FakeCtx:
    """run() 记下 (技能名, 参数, 时刻) 并让假时钟往前走;``handlers[技能名](参数)`` 可给出脚本化的结果。"""
    clock: Clock
    run_log: list = field(default_factory=list)
    handlers: dict = field(default_factory=dict)
    durations: dict = field(default_factory=lambda: {"ScanAt": 5.0, "AcquireSTS": 10.0})

    def run(self, skill_name: str, params: dict) -> SkillResult:
        self.run_log.append((skill_name, dict(params), self.clock.t))
        res = self.handlers[skill_name](params) if skill_name in self.handlers else None
        self.clock.t += self.durations.get(skill_name, 1.0)
        if res is not None:
            return res
        return SkillResult(skill_name=skill_name, success=True, data={"num_points": 256})

    def emit_progress(self, progress) -> None:
        pass

    def checkpoint_flush(self) -> None:
        pass

    # 便利访问
    def calls(self, name: str) -> list[tuple[dict, float]]:
        return [(p, t) for n, p, t in self.run_log if n == name]

    def names(self) -> list[str]:
        return [n for n, _p, _t in self.run_log]

    def moves(self) -> list[tuple[float, float]]:
        return [(p["x_m"], p["y_m"]) for p, _t in self.calls("MoveToXY")]


def scripted(results: list):
    """依次给出 results 里的结果,用完了就一直给最后一个。每项可以是 SkillResult 或 callable(params)。"""
    box = {"i": 0}

    def handler(params):
        r = results[min(box["i"], len(results) - 1)]
        box["i"] += 1
        return r(params) if callable(r) else r

    handler.box = box
    return handler


def reg_result(rows, *, drift=None, quality=0.9, trusted=True, n_matched=None) -> SkillResult:
    """脚本化的 RegisterDriftByMolecules 结果(与真技能 data 同形)。drift 缺省 = 匹配分子位移的均值。"""
    rows = [list(r) for r in rows]
    if drift is None:
        disp = np.array([[r[3] - r[1], r[4] - r[2]] for r in rows])
        drift = list(disp.mean(axis=0))
    return SkillResult(skill_name="RegisterDriftByMolecules", success=True, data={
        "drift_nm": [float(drift[0]), float(drift[1])], "quality": quality, "rms_nm": 0.04,
        "n_matched": len(rows) if n_matched is None else n_matched, "n_expected": len(rows),
        "trustworthy": trusted, "reason": "" if trusted else "low_quality", "matches": rows,
        "warnings": [], "degraded": False})


def uniform_rows(pts, d):
    return [[j, float(p[0]), float(p[1]), float(p[0] + d[0]), float(p[1] + d[1])] for j, p in enumerate(pts)]


def good(pts, d=(0.4, -0.2), quality=0.9) -> SkillResult:
    return reg_result(uniform_rows(pts, d), quality=quality)


def weak(pts, d=(3.0, 3.0)) -> SkillResult:
    return reg_result(uniform_rows(pts, d), quality=0.2, trusted=False)


def geo_of(theta: float) -> FrameGeometry:
    return FrameGeometry(CX, CY, WNM, WNM, theta, True, "test")


def mk_site(n: int, x: float, y: float, *, theta: float = 0.0, kind: str = "fixed",
            anchor=(), refined: bool = False) -> dict:
    sx, sy = frame_nm_to_scanner_m(x, y, geo_of(theta))
    s = {"id": f"s{n}", "n": n, "kind": kind, "wide": True, "low": False, "x_nm": x, "y_nm": y,
         "scanner_x_m": sx, "scanner_y_m": sy, "anchor": list(anchor), "anchor_offset_nm": None}
    if refined:
        s.update(rx_nm=x, ry_nm=y, rkind=kind)
    return s


SITE_XY = [(-2.0, -1.0), (1.0, 1.5), (-1.5, 2.0), (2.5, -2.0), (0.5, -3.0), (-3.0, 0.5)]


def default_sites(theta: float = 0.0, n: int = 4) -> list[dict]:
    return [mk_site(i + 1, x, y, theta=theta) for i, (x, y) in enumerate(SITE_XY[:n])]


@pytest.fixture(scope="module")
def refs(tmp_path_factory):
    """θ=0 与 θ=30° 的参考帧(各渲染一次);分子由真正的检测器检出。"""
    d = tmp_path_factory.mktemp("d_wiring")
    sc = scene(seed=11, size=18.0)
    out = {"dir": d, "scene": sc}
    for th in (0.0, 30.0):
        p = write_sxm(d / f"ref{int(th)}.sxm", render(sc), offset_nm=(CX, CY), angle_deg=th)
        out[th] = {"path": str(p), "pts": DR.molecules_in_frame(str(p)).pts}
    return out


@dataclass
class World:
    ctx: FakeCtx
    clock: Clock
    patches: list
    state: dict
    ref_path: str
    ref_pts: "np.ndarray"
    theta: float = 0.0

    def run(self, **params):
        base = {"page_id": "p1", "frame_path": self.ref_path, "track_every": 2,
                "stop_after_consecutive_failures": 0}
        return MeasureSpectraAtSites().run_composite(self.ctx, {**base, **params})

    def live_texts(self) -> list[str]:
        return [p["data"]["text"] for p in self.patches if p.get("path") == "status/live"]

    def site_results(self) -> dict:
        return {p["id"]: p["data"].get("result") for p in self.patches
                if p.get("target") == "sites" and p.get("data", {}).get("status") == "done"}


@pytest.fixture
def world(refs, tmp_path, monkeypatch):
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path))
    clock = Clock()
    monkeypatch.setattr(DR, "_now", clock)
    ctx = FakeCtx(clock)
    patches: list = []
    monkeypatch.setattr(D, "_apply_patch_quiet", lambda page_id, patch: patches.append(patch))
    monkeypatch.setattr(D, "_find_tracking_frame", lambda c, since: str(tmp_path / "tracking.sxm"))
    # 参考帧每次都重读重检太慢;同一个文件(同 mtime)的检测结果是确定的,缓存它不改变语义
    real = DR.molecules_in_frame
    memo: dict = {}

    def cached(path):
        try:
            key = (str(path), os.stat(path).st_mtime_ns)
        except OSError:
            return real(path)                 # 不存在的文件:让真函数去报错
        if key not in memo:
            memo[key] = real(path)
        return memo[key]

    monkeypatch.setattr(DR, "molecules_in_frame", cached)
    th = 0.0
    state = {"sites": default_sites(th), "coords_trustworthy": True, "doc": {"plan/marking": {"done": True}},
             "warnings": [], "anchor_molecules": {},
             "frame": {"cx_nm": CX, "cy_nm": CY, "w_nm": WNM, "h_nm": WNM, "angle_deg": th,
                       "angle_known": True, "source": "page_meta"}}

    class FakeRead:
        def execute(self, context, params):
            return SkillResult(skill_name="ReadSiteMarks", success=True, data={
                "sites": state["sites"], "coords_trustworthy": state["coords_trustworthy"],
                "doc": state["doc"], "warnings": state["warnings"], "count": len(state["sites"]),
                "frame": state["frame"], "anchor_molecules": state["anchor_molecules"]})

    monkeypatch.setattr("mast.skills.builtins.site_marking_read.ReadSiteMarks", FakeRead)
    return World(ctx, clock, patches, state, refs[0.0]["path"], refs[0.0]["pts"], th)


@pytest.fixture
def world30(world, refs):
    """θ=30° 的世界:参考帧、页面几何、位点都按 30° 换算。"""
    world.theta = 30.0
    world.ref_path = refs[30.0]["path"]
    world.ref_pts = refs[30.0]["pts"]
    world.state["frame"]["angle_deg"] = 30.0
    world.state["sites"] = default_sites(30.0)
    return world


def with_registrations(w: World, results: list):
    h = scripted(results)
    w.ctx.handlers["RegisterDriftByMolecules"] = h
    return h


def expected_moves(sites, d_nm, theta=0.0):
    dx, dy = frame_delta_to_scanner_m(d_nm[0], d_nm[1], theta)
    return [(s["scanner_x_m"] + dx, s["scanner_y_m"] + dy) for s in sites]


def assert_moves(got, want, tol_m=1e-14):
    assert len(got) == len(want), (len(got), len(want))
    for (gx, gy), (wx, wy) in zip(got, want):
        assert gx == pytest.approx(wx, abs=tol_m) and gy == pytest.approx(wy, abs=tol_m)


# ═══════════════════════════════════════════════════════════════════════════
# 一、分子路径:扫 → 配 → 移(带修正)
# ═══════════════════════════════════════════════════════════════════════════

def test_molecule_path_scans_registers_then_moves_with_the_measured_drift(world):
    with_registrations(world, [good(world.ref_pts)])
    res = world.run()
    assert res.success, res.error
    assert world.ctx.names() == [
        "ConfigureSTS",
        "ScanAt", "RegisterDriftByMolecules", "MoveToXY", "AcquireSTS", "MoveToXY", "AcquireSTS",
        "ScanAt", "RegisterDriftByMolecules", "MoveToXY", "AcquireSTS", "MoveToXY", "AcquireSTS"]
    assert "TrackDrift_ReferenceScan" not in world.ctx.names()
    assert_moves(world.ctx.moves(), expected_moves(world.state["sites"], (0.4, -0.2)))
    assert res.data["drift_method"] == "molecules"
    assert res.data["drift"]["n_registrations"] == 2 and res.data["drift"]["n_trusted"] == 2
    assert res.data["succeeded"] == 4 and "stopped_early" not in res.data


def test_the_first_registration_comes_before_the_first_site(world):
    """track_every=N:第一个位点之前也先配一次(参考帧可能是很久以前的了)—— 旧方法 i>0 才插。"""
    with_registrations(world, [good(world.ref_pts)])
    world.run(track_every=3)
    names = world.ctx.names()
    assert names.index("RegisterDriftByMolecules") < names.index("MoveToXY")
    assert names.count("ScanAt") == 2                       # 4 个位点、每 3 个一次:i=0 与 i=3


def test_the_tracking_scan_reuses_the_reference_frames_centre_size_and_angle(world30):
    with_registrations(world30, [good(world30.ref_pts)])
    world30.run(track_every=2)
    p, _t = world30.ctx.calls("ScanAt")[0]
    assert p["center_x_m"] == pytest.approx(CX * 1e-9) and p["center_y_m"] == pytest.approx(CY * 1e-9)
    assert p["size_m"] == pytest.approx(WNM * 1e-9)
    assert p["angle_deg"] == pytest.approx(30.0)            # 同一个扫描角,否则两帧不是一个平移
    assert "pixels" not in p and "line_time_s" not in p    # 不替策略表编数字


def test_track_pixels_and_line_time_are_passed_only_when_given(world):
    with_registrations(world, [good(world.ref_pts)])
    world.run(track_pixels=128, track_line_time_s=0.05)
    p, _t = world.ctx.calls("ScanAt")[0]
    assert p["pixels"] == 128 and p["line_time_s"] == pytest.approx(0.05)


def test_the_register_step_gets_the_reference_the_new_frame_and_the_search_window(world):
    with_registrations(world, [good(world.ref_pts)])
    world.run()
    calls = world.ctx.calls("RegisterDriftByMolecules")
    p0, p1 = calls[0][0], calls[1][0]
    assert p0["reference_frame_path"] == world.ref_path and p0["frame_path"].endswith("tracking.sxm")
    # 第一次没有先验:直接宽窗口、中心在 0;第二次:窄窗口、中心 = 外推的漂移
    assert p0["window_nm"] == pytest.approx(DR.WIDE_WINDOW_NM) and (p0["prev_drift_x_nm"], p0["prev_drift_y_nm"]) == (0.0, 0.0)
    assert p1["window_nm"] == pytest.approx(DR.WINDOW_NM)
    assert (p1["prev_drift_x_nm"], p1["prev_drift_y_nm"]) == pytest.approx((0.4, -0.2))
    assert p1["min_quality"] == pytest.approx(DR.QUALITY_MIN)


def test_each_finished_site_records_the_drift_that_was_applied(world):
    with_registrations(world, [good(world.ref_pts)])
    world.run()
    results = world.site_results()
    assert set(results) == {"s1", "s2", "s3", "s4"}
    for r in results.values():
        assert r["drift_nm"] == pytest.approx([0.4, -0.2]) and r["drift_mode"] in ("anchor", "local", "global")


def test_the_page_progress_line_mentions_the_drift(world):
    with_registrations(world, [good(world.ref_pts)])
    world.run()
    assert any(("漂移" in t or "drift" in t) and "+0.40" in t for t in world.live_texts())


# ═══════════════════════════════════════════════════════════════════════════
# 二、旧方法不退步;默认值
# ═══════════════════════════════════════════════════════════════════════════

def test_reference_scan_method_keeps_the_old_behaviour(world):
    world.state["sites"] = default_sites(n=5)
    res = world.run(drift_method="reference_scan", track_every=2)
    assert res.success, res.error
    names = world.ctx.names()
    assert names.count("TrackDrift_ReferenceScan") == 2         # i=2, i=4(i=0 被 i>0 排除)
    assert "ScanAt" not in names and "RegisterDriftByMolecules" not in names
    assert_moves(world.ctx.moves(), [(s["scanner_x_m"], s["scanner_y_m"]) for s in world.state["sites"]])
    assert "drift" not in res.data and "drift_method" not in res.data


def test_defaults_molecules_method_and_tracking_off(world):
    spec = {p.name: p for p in MeasureSpectraAtSites().metadata().parameters}
    assert spec["drift_method"].default == "molecules"
    assert spec["drift_method"].allowed_values == ["molecules", "reference_scan"]
    assert spec["track_every"].default == 0
    assert spec["stop_after_weak_registrations"].default == DR.WEAK_STOP_N == 3
    res = world.run(track_every=0)                              # 不追踪:一步都不多
    assert res.success
    assert not {"ScanAt", "RegisterDriftByMolecules", "TrackDrift_ReferenceScan"} & set(world.ctx.names())
    assert_moves(world.ctx.moves(), [(s["scanner_x_m"], s["scanner_y_m"]) for s in world.state["sites"]])


def test_validate_params_accepts_the_new_parameters_and_rejects_a_bad_method():
    skill = MeasureSpectraAtSites()
    ok = {"page_id": "p", "drift_method": "molecules", "track_every": 3, "track_pixels": 128,
          "track_line_time_s": 0.05, "stop_after_weak_registrations": 2}
    assert skill.validate_params(ok) == []
    assert skill.validate_params({**ok, "drift_method": "magic"})


# ═══════════════════════════════════════════════════════════════════════════
# 三、弱配准:不采信、外推;连续三次 → 丢失
# ═══════════════════════════════════════════════════════════════════════════

def test_a_weak_registration_does_not_move_the_targets_the_last_good_drift_is_used(world):
    pts = world.ref_pts
    world.state["sites"] = default_sites(n=6)
    with_registrations(world, [good(pts, (0.4, -0.2)), weak(pts, (3.0, 3.0)), good(pts, (0.45, -0.22))])
    res = world.run(track_every=2)
    assert res.success, res.error
    s = world.state["sites"]
    want = (expected_moves(s[:2], (0.4, -0.2))             # 第一次(可信)
            + expected_moves(s[2:4], (0.4, -0.2))          # 第二次不可信:仍用上一次可信值,绝不是 (3, 3)
            + expected_moves(s[4:], (0.45, -0.22)))        # 第三次(可信)
    assert_moves(world.ctx.moves(), want)
    d = res.data["drift"]
    assert d["n_registrations"] == 3 and d["n_trusted"] == 2 and d["weak_streak"] == 0 and not d["lost"]


def test_three_consecutive_weak_registrations_stop_with_drift_lost_before_the_next_site(world):
    pts = world.ref_pts
    world.state["sites"] = default_sites(n=6) + [mk_site(7 + i, 0.2 * i, 0.1 * i) for i in range(6)]
    with_registrations(world, [good(pts), weak(pts), weak(pts), weak(pts)])
    res = world.run(track_every=2)
    # 事件:i=0 好、i=2 弱①、i=4 弱②、i=6 弱③ → 判丢 → 第 7 个位点(i=6)不测
    assert world.ctx.names().count("ScanAt") == 4
    assert world.ctx.names().count("AcquireSTS") == 6
    assert res.success and res.data["succeeded"] == 6                  # 前 6 个是好的,不因为后来丢了就作废
    assert any(k in res.data["stopped_early"] for k in LOST)
    assert res.data["drift"]["lost"] is True and res.data["drift"]["weak_streak"] == 3
    assert any(any(k in t for k in LOST) for t in world.live_texts()[-1:])      # 页面最后一行说明了为什么停


def test_a_trustworthy_registration_in_between_resets_the_weak_count(world):
    pts = world.ref_pts
    world.state["sites"] = default_sites(n=6) + [mk_site(7 + i, 0.2 * i, 0.1 * i) for i in range(6)]
    with_registrations(world, [good(pts), weak(pts), weak(pts), good(pts), weak(pts), weak(pts), good(pts)])
    res = world.run(track_every=2)
    assert res.success and res.data["succeeded"] == 12                 # 一直没连续三次弱
    assert "stopped_early" not in res.data and not res.data["drift"]["lost"]


@pytest.mark.parametrize("limit,expect_sites", [(2, 4), (0, 12)])
def test_stop_after_weak_registrations_is_a_parameter(world, limit, expect_sites):
    pts = world.ref_pts
    world.state["sites"] = default_sites(n=6) + [mk_site(7 + i, 0.2 * i, 0.1 * i) for i in range(6)]
    with_registrations(world, [good(pts), weak(pts)])                  # 之后一直弱
    res = world.run(track_every=2, stop_after_weak_registrations=limit)
    assert res.data["succeeded"] == expect_sites
    assert ("stopped_early" in res.data) == (limit > 0)


def test_velocity_extrapolates_the_targets_between_registrations(world):
    """漂移随时间线性增长(0.5 nm/h):配准是脚本化的,但按假时钟给出「那一刻的真漂移」。
    第一次配准之后速度未知(只有一条)→ 用测得值;之后每个位点的目标 = 那一刻的真漂移(外推得上)。"""
    world.ctx.durations = {"ScanAt": 100.0, "AcquireSTS": 400.0}      # 两次配准相隔 ~900 s ≥ 600 s
    pts = world.ref_pts
    v = np.array([0.5, -0.25])
    d0 = np.array([0.3, -0.1])

    def true_drift(t):
        return d0 + v * (t - T_START) / 3600.0

    def at_scan_midpoint(params):
        # 注册步骤跑在 ScanAt 之后、此刻时钟 = 扫描结束;扫描中点比它早 50 s
        return reg_result(uniform_rows(pts, true_drift(world.clock.t - 50.0)), quality=0.9)

    world.state["sites"] = default_sites(n=6)
    with_registrations(world, [at_scan_midpoint])
    res = world.run(track_every=2)
    assert res.success, res.error
    t_mid_first = world.ctx.calls("ScanAt")[0][1] + 50.0
    sites = world.state["sites"]
    for k, (p, t_call) in enumerate(world.ctx.calls("MoveToXY")):
        want = true_drift(t_mid_first) if k < 2 else true_drift(t_call)     # 前两个位点:只有一次配准,无速度
        assert p["x_m"] == pytest.approx(sites[k]["scanner_x_m"] + want[0] * 1e-9, abs=2e-12), k
        assert p["y_m"] == pytest.approx(sites[k]["scanner_y_m"] + want[1] * 1e-9, abs=2e-12), k
    # 速度确实被估出来了,且是真值;外推的量也确实不小(否则上面的断言鉴别不了「有没有外推」)
    assert res.data["drift"]["velocity_nm_per_h"] == pytest.approx(list(v), abs=2e-3)
    t_last = world.ctx.calls("MoveToXY")[-1][1]
    assert np.hypot(*(true_drift(t_last) - true_drift(t_mid_first))) > 0.2


# ═══════════════════════════════════════════════════════════════════════════
# 四、还没有可信配准时不测谱;追踪步骤失败 = 弱配准
# ═══════════════════════════════════════════════════════════════════════════

def test_no_spectrum_is_taken_before_the_first_trustworthy_registration(world):
    pts = world.ref_pts
    with_registrations(world, [weak(pts), weak(pts), good(pts)])
    res = world.run(track_every=2)
    names = world.ctx.names()
    assert res.success
    first_move = names.index("MoveToXY")
    assert names[:first_move].count("ScanAt") == 3 and names[:first_move].count("RegisterDriftByMolecules") == 3
    assert_moves(world.ctx.moves(), expected_moves(world.state["sites"], (0.4, -0.2)))   # 第一个位点就带修正
    ids = [s.step_id for s in _steps_run(res)]
    assert {"track_0:scan", "track_0r1:scan", "track_0r2:scan"} <= set(ids)


def _steps_run(res):
    """从结果里的进度快照还原跑过的步骤(只要 id)。"""
    from types import SimpleNamespace

    prog = res.data["_progress"]
    return [SimpleNamespace(step_id=i) for i in prog["completed_steps"] + prog["failed_steps"]]


def test_if_the_first_three_registrations_are_all_weak_no_site_is_measured(world):
    pts = world.ref_pts
    with_registrations(world, [weak(pts)])
    res = world.run(track_every=2)
    assert not res.success and any(k in res.error for k in LOST)
    assert "AcquireSTS" not in world.ctx.names() and "MoveToXY" not in world.ctx.names()
    assert world.ctx.names().count("ScanAt") == 3                       # 立刻重配,到判丢为止,不无限重来


def test_with_weak_limit_zero_the_initial_registration_is_not_retried_forever(world):
    pts = world.ref_pts
    with_registrations(world, [weak(pts)])
    res = world.run(track_every=2, stop_after_weak_registrations=0)
    assert res.success and world.ctx.names().count("AcquireSTS") == 4   # 没有出口时只扫一次,照常测(未校正)
    assert world.ctx.names().count("ScanAt") == 2


@pytest.mark.parametrize("what", ["scan_fails", "register_fails", "no_frame_anywhere"])
def test_failures_of_the_tracking_steps_count_as_weak_registrations(world, what, monkeypatch):
    pts = world.ref_pts
    if what == "scan_fails":
        world.ctx.handlers["ScanAt"] = lambda p: SkillResult(skill_name="ScanAt", success=False, error="boom")
        with_registrations(world, [good(pts)])
    elif what == "register_fails":
        with_registrations(world, [SkillResult(skill_name="RegisterDriftByMolecules", success=False,
                                               error="too few molecules")])
    else:
        monkeypatch.setattr(D, "_find_tracking_frame", lambda c, since: None)       # 自动保存的帧找不到
        # SaveScan 默认给 {"num_points": 256}:没有 saved_path
        with_registrations(world, [good(pts)])
    res = world.run(track_every=2)
    assert not res.success and any(k in res.error for k in LOST)        # 三次都没配出来 → 丢失,一个点也没测
    assert "AcquireSTS" not in world.ctx.names()
    if what == "scan_fails":
        assert "RegisterDriftByMolecules" not in world.ctx.names()      # 没扫成就不去配
    if what == "no_frame_anywhere":
        assert world.ctx.names().count("SaveScan") == 3                 # 先找自动保存的,没有才 SaveScan


def test_savescan_is_the_fallback_when_the_autosaved_frame_is_not_found(world, monkeypatch):
    monkeypatch.setattr(D, "_find_tracking_frame", lambda c, since: None)
    world.ctx.handlers["SaveScan"] = lambda p: SkillResult(skill_name="SaveScan", success=True,
                                                           data={"saved_path": "X:/saved/track.sxm"})
    with_registrations(world, [good(world.ref_pts)])
    res = world.run(track_every=4)
    assert res.success
    assert world.ctx.names()[:4] == ["ConfigureSTS", "ScanAt", "SaveScan", "RegisterDriftByMolecules"]
    assert world.ctx.calls("RegisterDriftByMolecules")[0][0]["frame_path"] == "X:/saved/track.sxm"


def test_autosaved_frame_makes_savescan_unnecessary(world):
    with_registrations(world, [good(world.ref_pts)])
    world.run()
    assert "SaveScan" not in world.ctx.names()                           # 不重复存一份


def test_the_frame_is_looked_for_after_the_scan_started_not_before(world, monkeypatch):
    seen = []
    monkeypatch.setattr(D, "_find_tracking_frame", lambda c, since: seen.append(since) or "t.sxm")
    with_registrations(world, [good(world.ref_pts)])
    world.run()
    scan_times = [t for _p, t in world.ctx.calls("ScanAt")]
    # 注意 approx 默认相对容差 1e-6:对 2e9 量级的时间戳是 ±2000 s,等于没比。这里要绝对容差。
    assert len(seen) == len(scan_times) == 2
    assert seen == pytest.approx(scan_times, rel=0, abs=1e-6)          # since = 该次 ScanAt 开始的时刻


# ═══════════════════════════════════════════════════════════════════════════
# 五、位点的目标:锚点 / 局部 / 全局;θ≠0
# ═══════════════════════════════════════════════════════════════════════════

def _nearest(pts, xy):
    return int(np.argmin(np.hypot(pts[:, 0] - xy[0], pts[:, 1] - xy[1])))


def _outlier_field(pts, a, base=(0.4, -0.2), odd=(0.9, 0.7)):
    rows = uniform_rows(pts, base)
    rows[a][3] = rows[a][1] + odd[0]
    rows[a][4] = rows[a][2] + odd[1]
    return rows


def test_an_anchored_site_follows_its_anchor_molecule_and_a_free_site_the_local_median(world):
    pts = world.ref_pts
    a = _nearest(pts, (1.0, 1.0))
    ax, ay = pts[a]
    world.state["sites"] = [mk_site(1, ax, ay, kind="mol", anchor=[a], refined=True),
                            mk_site(2, ax + 1.5, ay + 0.5, kind="au")]
    world.state["anchor_molecules"] = {str(a): {"x_nm": round(float(ax), 4), "y_nm": round(float(ay), 4)}}
    with_registrations(world, [reg_result(_outlier_field(pts, a), drift=(0.4, -0.2))])
    res = world.run(track_every=5)
    assert res.success, res.error
    s = world.state["sites"]
    assert_moves(world.ctx.moves(), expected_moves(s[:1], (0.9, 0.7)) + expected_moves(s[1:], (0.4, -0.2)))
    modes = {sid: v["mode"] for sid, v in res.data["site_drift"].items()}
    assert modes == {"s1": "anchor", "s2": "local"}


def test_anchors_that_do_not_line_up_with_the_reference_are_ignored(world):
    """ReadSiteMarks 的锚点下标与参考帧上重新检出的分子对不上(profile 变了 / 帧不对)→ 锚点位移不可用,
    位点退到局部位移 —— 绝不拿错分子的位移去修正。"""
    pts = world.ref_pts
    a = _nearest(pts, (1.0, 1.0))
    ax, ay = pts[a]
    world.state["sites"] = [mk_site(1, ax, ay, kind="mol", anchor=[a], refined=True)]
    world.state["anchor_molecules"] = {str(a): {"x_nm": float(ax) + 0.5, "y_nm": float(ay)}}      # 对不上
    with_registrations(world, [reg_result(_outlier_field(pts, a), drift=(0.4, -0.2))])
    res = world.run(track_every=5)
    assert res.success
    assert res.data["site_drift"]["s1"]["mode"] == "local"
    assert_moves(world.ctx.moves(), expected_moves(world.state["sites"], (0.4, -0.2)))


def test_a_site_without_the_nominal_position_fields_still_gets_the_global_drift(world):
    """旧形状的位点(没有 x_nm/y_nm)不该炸:退到名义位置 (0,0) 上的位移,仍给出有限的目标。"""
    s = {"id": "s1", "n": 1, "kind": "auto", "scanner_x_m": 1e-9, "scanner_y_m": 2e-9}
    world.state["sites"] = [s]
    with_registrations(world, [good(world.ref_pts)])
    res = world.run(track_every=5)
    assert res.success
    x, y = world.ctx.moves()[0]
    assert x == pytest.approx(1e-9 + 0.4e-9) and y == pytest.approx(2e-9 - 0.2e-9)


def test_on_a_rotated_frame_the_drift_is_converted_with_the_single_source_rotation(world30):
    with_registrations(world30, [good(world30.ref_pts, (0.4, -0.2))])
    res = world30.run(track_every=5)
    assert res.success, res.error
    want = expected_moves(world30.state["sites"], (0.4, -0.2), theta=30.0)
    assert_moves(world30.ctx.moves(), want)
    # 与不转的情形明显不同(30° 下 0.4 nm 的位移转出来差 ~0.2 nm),所以上面不是巧合
    flat = expected_moves(world30.state["sites"], (0.4, -0.2), theta=0.0)
    assert abs(want[0][0] - flat[0][0]) > 1e-10


# ═══════════════════════════════════════════════════════════════════════════
# 六、前置检查:备不好就直说,别等扫了几帧之后
# ═══════════════════════════════════════════════════════════════════════════

def test_molecule_tracking_without_a_reference_frame_fails_before_touching_the_instrument(world):
    res = world.run(frame_path="")
    assert not res.success and "frame_path" in res.error
    assert world.ctx.run_log == []


def test_a_reference_that_is_not_the_frame_the_page_was_marked_on_is_refused(world):
    world.state["frame"]["cx_nm"] = CX + 1.0                           # 页面的几何与 .sxm 文件头对不上
    res = world.run()
    assert not res.success and ("frame_path" in res.error)
    assert world.ctx.run_log == []


def test_an_unreadable_reference_fails_early(world):
    res = world.run(frame_path="Z:/no/such/ref.sxm")
    assert not res.success and world.ctx.run_log == []


def test_a_non_square_reference_cannot_be_rescanned_by_scanat(world):
    world.state["frame"]["h_nm"] = WNM * 1.2
    res = world.run()
    assert not res.success and world.ctx.run_log == []


def test_an_unknown_scan_angle_fails_early(world):
    world.state["frame"]["angle_known"] = False
    world.state["frame"]["angle_deg"] = None
    res = world.run()
    assert not res.success and world.ctx.run_log == []


def test_no_tracking_means_no_reference_is_needed(world):
    res = world.run(frame_path="", track_every=0)
    assert res.success and world.ctx.names().count("AcquireSTS") == 4


def test_the_old_method_needs_no_reference_frame_either(world):
    res = world.run(frame_path="", drift_method="reference_scan")
    assert res.success


# ═══════════════════════════════════════════════════════════════════════════
# 七、状态:纯 JSON、不带 matches;续跑能接上
# ═══════════════════════════════════════════════════════════════════════════

def test_the_progress_snapshot_is_plain_json_and_does_not_carry_the_matches(world):
    with_registrations(world, [good(world.ref_pts)])
    res = world.run()
    blob = json.dumps(res.data["_progress"])                            # 进检查点的东西必须是纯 JSON
    assert '"matches": [[' not in blob                                  # 几百行匹配不进检查点
    assert len(blob) < 40_000
    drift = res.data["_progress"]["partial_data"]["drift"]
    assert drift["last_good"]["drift_nm"] == pytest.approx([0.4, -0.2]) and drift["last_good"]["matches"] == []
    assert "matches" not in json.dumps(res.data["drift"])               # 摘要里也没有


def test_a_resumed_run_picks_the_tracker_up_from_partial_data():
    comp = MeasureSpectraAtSites()

    class Ex:
        def __init__(self, saved):
            self.progress = type("P", (), {"partial_data": {"drift": saved}})()

    t = DR.DriftTracker()
    t.note_registration({"drift_nm": [0.4, -0.2], "quality": 0.9, "n_matched": 40, "matches": []}, T_START)
    t.note_failure("x", T_START + 10)
    got = comp._tracker_for({"stop_after_weak_registrations": 3}, Ex(t.to_dict(include_matches=False)))
    assert got.weak_streak == 1 and got.last_good["drift_nm"] == [0.4, -0.2]
    fresh = comp._tracker_for({"stop_after_weak_registrations": 2}, Ex(None))
    assert fresh.weak_limit == 2 and fresh.last_good is None


def test_plan_dynamic_without_tracking_info_does_not_pretend_to_track():
    """绕过入口直接调 plan_dynamic、又选了 molecules:没有参考就没法配准,如实不追踪(不崩、不假装)。"""
    class Ex:
        def __init__(self):
            self.progress = type("P", (), {"partial_data": {}})()

        def set_partial(self, k, v):
            self.progress.partial_data[k] = v

    sites = default_sites(n=3)
    plan = list(MeasureSpectraAtSites().plan_dynamic(
        {"_sites": sites, "page_id": "p", "track_every": 1, "drift_method": "molecules"}, Ex()))
    assert [s.skill_name for s in plan] == ["ConfigureSTS"] + ["MoveToXY", "AcquireSTS"] * 3


# ═══════════════════════════════════════════════════════════════════════════
# 八、端到端:真的 RegisterDriftByMolecules 吃合成 .sxm
# ═══════════════════════════════════════════════════════════════════════════

def _live_scan_writer(world, refs, tmp_path, shift_at, *, theta=0.0, Finv=None):
    """ScanAt 的替身:按「此刻的物理漂移」渲染一张新帧写到盘上,并让 _find_tracking_frame 找得到它。"""
    sc = refs["scene"]
    written: list[str] = []

    def scan(params):
        t_mid = world.clock.t + 50.0                                   # ScanAt 从 clock.t 开始、100 s,中点 +50
        d = shift_at(t_mid)
        pts = sc + d if Finv is None else (Finv @ (sc + d).T).T
        path = write_sxm(tmp_path / f"track_{len(written)}.sxm", render(pts, noise=3e-12, seed=len(written)),
                         offset_nm=(CX, CY), angle_deg=theta)
        written.append(str(path))
        return None                                                    # 默认成功

    world.ctx.handlers["ScanAt"] = scan
    return written


def test_end_to_end_with_the_real_registration_skill_and_a_linearly_growing_drift(world, refs, tmp_path, monkeypatch):
    world.ctx.durations = {"ScanAt": 100.0, "AcquireSTS": 400.0}
    v = np.array([0.3, -0.15])
    d0 = np.array([0.37, -0.21])

    def true_drift(t):
        return d0 + v * (t - T_START) / 3600.0

    written = _live_scan_writer(world, refs, tmp_path, true_drift)
    monkeypatch.setattr(D, "_find_tracking_frame", lambda c, since: written[-1])
    world.ctx.handlers["RegisterDriftByMolecules"] = lambda params: RegisterDriftByMolecules().execute(None, params)
    world.state["sites"] = default_sites(n=6)
    res = world.run(track_every=2)
    assert res.success, res.error
    assert res.data["drift"]["n_trusted"] == 3 and not res.data["drift"]["lost"]
    sites = world.state["sites"]
    t_mid_first = world.ctx.calls("ScanAt")[0][1] + 50.0
    for k, (p, t_call) in enumerate(world.ctx.calls("MoveToXY")):
        want = (true_drift(t_mid_first) if k < 2 else true_drift(t_call)) * 1e-9
        # 实测最大偏差 ~1.5 pm(检测 + 配准 + 两三个点拟合速度再外推几百秒);容差放 15 pm,留足余量又鉴别得开
        assert p["x_m"] == pytest.approx(sites[k]["scanner_x_m"] + want[0], abs=1.5e-11), k
        assert p["y_m"] == pytest.approx(sites[k]["scanner_y_m"] + want[1], abs=1.5e-11), k
    assert res.data["drift"]["velocity_nm_per_h"] == pytest.approx(list(v), abs=0.02)      # 实测偏差 ~0.002


def test_end_to_end_on_a_rotated_frame_the_physical_drift_reaches_the_right_scanner_target(world30, refs, tmp_path, monkeypatch):
    """θ=30°:物理漂移在**扫描器轴**上给定(与符号约定无关地用前向矩阵 F 和 np.linalg.inv 造数据)。
    D 修正后的 MoveToXY 目标 = 名义位置 + 这个物理漂移 —— 而不是 + 它在帧轴上的分量。"""
    th = 30.0
    F = np.array([frame_delta_to_scanner_m(1.0, 0.0, th), frame_delta_to_scanner_m(0.0, 1.0, th)]).T * 1e9
    Finv = np.linalg.inv(F)
    d_phys = np.array([0.42, -0.15])                                   # 扫描器轴,nm
    written = _live_scan_writer(world30, refs, tmp_path, lambda t: d_phys, theta=th, Finv=Finv)
    # 参考帧也要按「场景被 F⁻¹ 转到帧轴」来渲染,否则新帧(转过的)和它不是同一片分子
    ref = write_sxm(tmp_path / "ref_rot.sxm", render((Finv @ refs["scene"].T).T), offset_nm=(CX, CY), angle_deg=th)
    world30.ref_path = str(ref)
    monkeypatch.setattr(D, "_find_tracking_frame", lambda c, since: written[-1])
    world30.ctx.handlers["RegisterDriftByMolecules"] = lambda params: RegisterDriftByMolecules().execute(None, params)
    world30.state["sites"] = default_sites(theta=th, n=3)
    res = world30.run(track_every=5)
    assert res.success, res.error
    for (x, y), s in zip(world30.ctx.moves(), world30.state["sites"]):
        assert x == pytest.approx(s["scanner_x_m"] + d_phys[0] * 1e-9, abs=1e-11)    # 实测 ~1 pm
        assert y == pytest.approx(s["scanner_y_m"] + d_phys[1] * 1e-9, abs=1e-11)


# ═══════════════════════════════════════════════════════════════════════════
# 九、给操作员的痕迹:丢失的原因、扫描框、参考帧文件名、配准提示
# ═══════════════════════════════════════════════════════════════════════════

def test_the_lost_message_names_the_latest_reason(world):
    with_registrations(world, [SkillResult(skill_name="RegisterDriftByMolecules", success=False,
                                           error="boom: skill not found")])
    res = world.run(track_every=2)
    assert not res.success and any(k in res.error for k in LOST)
    assert "boom: skill not found" in res.error                   # 不只说「丢失」,把为什么也带上


def test_tracking_leaves_the_scan_frame_at_the_reference_and_says_so(world):
    with_registrations(world, [good(world.ref_pts)])
    res = world.run(track_every=5)
    assert res.data["tracking_scan_frame"] == world.ctx.calls("ScanAt")[0][0]


def test_without_tracking_there_is_no_scan_frame_note(world):
    res = world.run(track_every=0)
    assert "tracking_scan_frame" not in res.data and "drift_warnings" not in res.data


def test_a_reference_with_another_file_name_is_used_but_warned(world):
    world.state["frame"]["file"] = "the_marked_frame.sxm"
    with_registrations(world, [good(world.ref_pts)])
    res = world.run(track_every=5)
    assert res.success
    assert res.data["drift_warnings"] and "the_marked_frame.sxm" in res.data["drift_warnings"][0]


def test_a_reference_with_the_recorded_file_name_is_not_warned(world):
    world.state["frame"]["file"] = Path(world.ref_path).name
    with_registrations(world, [good(world.ref_pts)])
    res = world.run(track_every=5)
    assert res.success and res.data["drift_warnings"] == []


def test_registration_warnings_are_collected_once_for_the_operator(world):
    note = "scan directions differ (reference down, new up)"
    r = good(world.ref_pts)
    r.data["warnings"] = [note]
    with_registrations(world, [r])
    res = world.run(track_every=2)                                  # 两次配准,同一条提示
    assert res.data["drift_warnings"].count(note) == 1


class _Runaway(BaseException):
    """BaseException:执行器的 ``except Exception`` 拦不住它 —— 追踪循环一旦失控,测试立刻红而不是挂住。"""


def test_the_initial_retry_loop_ends_even_if_the_trackers_bookkeeping_is_broken(world, monkeypatch):
    """重试的出口不能只靠追踪器记的账(记账出了岔子就是死循环):次数本身有上限 = weak_limit。"""
    monkeypatch.setattr(DR.DriftTracker, "note_failure", lambda self, reason, t: None)
    n = {"scans": 0}

    def scan(params):
        n["scans"] += 1
        if n["scans"] > 50:
            raise _Runaway()
        return SkillResult(skill_name="ScanAt", success=False, error="boom")

    world.ctx.handlers["ScanAt"] = scan
    world.run(track_every=2)
    assert n["scans"] == 6                         # 4 个位点 → 两次追踪(i=0、i=2),每次最多重试 3 次


def test_the_lost_message_is_also_available_in_english(world):
    """tr() 的英文半边也要有人看:测试进程默认 zh,所以显式切到 en 跑一遍。"""
    with_registrations(world, [SkillResult(skill_name="RegisterDriftByMolecules", success=False,
                                           error="boom: skill not found")])
    with use_lang("en"):
        res = world.run(track_every=2)
    assert not res.success and "Drift lost" in res.error
    assert "Latest reason" in res.error and "boom: skill not found" in res.error


def test_the_english_run_messages_and_metadata_have_no_chinese(world):
    with use_lang("en"):
        missing_ref = world.run(frame_path="")
        with_registrations(world, [SkillResult(skill_name="RegisterDriftByMolecules", success=False, error="boom")])
        lost = world.run(track_every=2)
        meta = MeasureSpectraAtSites().metadata()
    texts = [missing_ref.error, lost.error, meta.description] + [p.description for p in meta.parameters]
    assert not [t for t in texts if _CJK.search(t)], [t for t in texts if _CJK.search(t)]
    with use_lang("zh"):
        assert _CJK.search(MeasureSpectraAtSites().metadata().description)    # 另一半确实是中文
