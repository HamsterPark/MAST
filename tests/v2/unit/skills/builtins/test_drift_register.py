"""``RegisterDriftByMolecules`` 与它背后的数值(分子配准漂移追踪)。

纯软件:合成分子集 / 合成 .sxm,不碰仪器(``execute(None, ...)``)。这是数值代码,所以每条都是
「造一个已知的平移 → 看恢复出来的是不是它」,而不是只看形状:

* **配准**(``register``):窄窗口 / 放宽窗口两条路都恢复已知平移;干净匹配质量高、多数分子对不上时
  质量低且 ``trustworthy`` 为假;漏检 / 假检 / 噪声下仍恢复;离开视野的分子不拖累质量。
* **晶格别名**:完美晶格上差一个晶格矢量的平移分不开 —— 先验够准时靠先验裁决,先验偏离超过半个
  晶格常数时会差一个分子,而 ``competing_peak_ratio`` 把这件事如实报出来。后一条是**已知限制**,
  测试把它钉成限制,不假装它不存在。
* **漂移速度**:已知斜率、1 nm/h 封顶(保方向)、跨度 < 600 s 返回 0、只用可信配准、只用最近 4 条。
* **追踪器**:外推、弱配准不被采信、连续 3 次弱 → 丢失、按位点算目标(锚点 / 局部 / 全局)。
* **技能壳**:从 .sxm 恢复已知平移;θ≠0 且两帧中心不同,**与符号约定无关**地恢复物理漂移(用
  ``frame_nm_to_scanner_m`` 的前向矩阵造数据,用 ``np.linalg.inv`` 做逆,不用被测代码的逆)。
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
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

from mast.i18n import use_lang  # noqa: E402
from mast.skills.builtins import drift_register as DR  # noqa: E402
from mast.skills.builtins.drift_register import (  # noqa: E402
    DriftTracker,
    RegisterDriftByMolecules,
    drift_velocity,
    frame_delta_to_scanner_m,
    register,
    scanner_delta_to_frame,
)
from mast.skills.builtins.site_marking_read import FrameGeometry, frame_nm_to_scanner_m  # noqa: E402

from tests.v2.unit.skills._drift_synth import (  # noqa: E402
    FIELD_NM as W,
    hex_lattice,
    poisson_disk,
    render,
    scene,
    view,
    write_sxm,
)

H = W
FIELD = (0.0, 0.0, W, H)


_CJK = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")


def err(r, truth) -> float:
    return math.hypot(r.drift_nm[0] - truth[0], r.drift_nm[1] - truth[1])


@pytest.fixture(scope="module")
def ref_pts():
    return view(scene(), (0.0, 0.0), noise=0.02, seed=1)


# ═══════════════════════════════════════════════════════════════════════════
# 一、register:已知平移 → 恢复
# ═══════════════════════════════════════════════════════════════════════════

def test_scene_is_dense_enough_to_mean_something(ref_pts):
    """自检:参考集不是 12 个点糊弄 —— 一个足够密的分子排布,下面的断言才有鉴别力。"""
    assert len(ref_pts) > 150


@pytest.mark.parametrize("shift", [(0.0, 0.0), (0.31, -0.22), (-0.6, 0.5), (0.05, 0.74), (-0.75, -0.7)])
def test_recovers_known_translation_in_the_narrow_window(ref_pts, shift):
    """窗口 ±0.8 nm 以内的平移:不放宽、恢复到 20 pm 以内、质量近 1。"""
    new = view(scene(), shift, noise=0.04, seed=2)
    r = register(new, ref_pts, (0.0, 0.0), field=FIELD)
    assert err(r, shift) < 0.02, (r.drift_nm, shift)
    assert r.trustworthy and r.reason == ""
    assert r.quality > 0.95
    assert not r.widened and r.window_nm == pytest.approx(0.8)
    assert r.n_matched > 0.9 * r.n_expected


@pytest.mark.parametrize("shift", [(1.4, -1.1), (-2.2, 2.0), (0.9, 0.0), (2.4, 2.4), (-1.8, -0.2)])
def test_recovers_known_translation_in_the_widened_window(ref_pts, shift):
    """平移超出 ±0.8 nm:窄窗口配不上 → 放宽到 ±2.5 nm 再来,恢复到 20 pm 以内。"""
    new = view(scene(), shift, noise=0.04, seed=3)
    r = register(new, ref_pts, (0.0, 0.0), field=FIELD)
    assert err(r, shift) < 0.02, (r.drift_nm, shift)
    assert r.widened and r.window_nm == pytest.approx(2.5)
    assert r.trustworthy and r.quality > 0.9


def test_the_window_is_centred_on_prev_drift_not_on_zero(ref_pts):
    """先验决定窗口中心:平移 (3.0, -2.6) 远在 ±2.5 之外,但 prev_drift 给得准,窄窗口就够。"""
    shift = (3.0, -2.6)
    new = view(scene(), shift, noise=0.04, seed=4)
    near = register(new, ref_pts, (2.9, -2.5), field=FIELD)
    assert err(near, shift) < 0.02 and not near.widened
    far = register(new, ref_pts, (0.0, 0.0), field=FIELD)        # 先验在零:窗口够不着
    assert not (far.trustworthy and err(far, shift) < 0.05)


def test_window_sizes_are_parameters(ref_pts):
    """window_nm / wide_window_nm 可调:把宽窗口也限到 0.8,平移 1.4 就配不上、如实报不可信。"""
    new = view(scene(), (1.4, -1.1), noise=0.04, seed=5)
    r = register(new, ref_pts, (0.0, 0.0), field=FIELD, window_nm=0.8, wide_window_nm=0.8)
    assert not r.widened
    assert not (r.trustworthy and err(r, (1.4, -1.1)) < 0.05)
    r2 = register(new, ref_pts, (0.0, 0.0), field=FIELD, window_nm=2.0)   # 窄窗口本身就够大
    assert err(r2, (1.4, -1.1)) < 0.02 and not r2.widened


def test_a_clear_winner_beats_the_prior_inside_the_window(ref_pts):
    """先验只裁决近平局:真峰票数远高于别的峰时,即使离先验很远(窗口边缘)也选真峰。"""
    shift = (0.72, 0.65)
    new = view(scene(), shift, noise=0.03, seed=6)
    r = register(new, ref_pts, (0.0, 0.0), field=FIELD)
    assert err(r, shift) < 0.02 and not r.widened


@pytest.mark.parametrize("seed", range(6))
def test_recovery_survives_dropouts_spurious_detections_and_noise(ref_pts, seed):
    """漏检 25%、假检 30%、位置噪声 50 pm 下仍恢复(窄 / 宽两条路各一个平移)。"""
    rng = np.random.default_rng(seed)
    for shift in (rng.uniform(-0.7, 0.7, 2), rng.uniform(-2.4, 2.4, 2)):
        new = view(scene(), shift, noise=0.05, drop=0.25, spurious=0.3, seed=10 + seed)
        r = register(new, ref_pts, (0.0, 0.0), field=FIELD)
        assert err(r, shift) < 0.03, (r.drift_nm, shift, r.quality)
        assert r.trustworthy


# ═══════════════════════════════════════════════════════════════════════════
# 二、质量与可信度
# ═══════════════════════════════════════════════════════════════════════════

def test_quality_is_matched_over_expected(ref_pts):
    new = view(scene(), (0.4, -0.3), noise=0.03, seed=7)
    r = register(new, ref_pts, (0.0, 0.0), field=FIELD)
    assert r.quality == pytest.approx(min(1.0, r.n_matched / r.n_expected))
    assert r.quality > 0.95 and r.n_expected > 100


def test_quality_is_low_when_most_molecules_do_not_match(ref_pts):
    """新帧是另一片分子(针尖换了 / 扫到别处):多数对不上 → 质量低、不可信、原因点名。"""
    other = view(poisson_disk(24.0, 24.0, 0.6, seed=99), (0.0, 0.0), seed=8)
    r = register(other, ref_pts, (0.0, 0.0), field=FIELD)
    assert r.quality < 0.5, r.quality
    assert not r.trustworthy and r.reason in ("low_quality", "few_matches")


def test_quality_drops_as_the_matching_fraction_drops(ref_pts):
    """同一个平移,只留 85% / 40% / 10% 的分子:质量单调下降,跨过 0.5 时 trustworthy 翻转。"""
    qs, trusts = [], []
    for keep in (0.85, 0.4, 0.1):
        new = view(scene(), (0.25, 0.1), noise=0.03, drop=1 - keep, seed=20)
        r = register(new, ref_pts, (0.0, 0.0), field=FIELD)
        qs.append(r.quality)
        trusts.append(r.trustworthy)
    assert qs[0] > qs[1] > qs[2]
    assert trusts == [True, False, False]


def test_min_quality_is_the_trust_threshold(ref_pts):
    new = view(scene(), (0.25, 0.1), noise=0.03, drop=0.45, seed=21)    # 留 55% 左右
    lo = register(new, ref_pts, (0.0, 0.0), field=FIELD, min_quality=0.3)
    hi = register(new, ref_pts, (0.0, 0.0), field=FIELD, min_quality=0.9)
    assert lo.trustworthy and lo.reason == ""
    assert not hi.trustworthy and hi.reason == "low_quality"
    assert 0.3 < lo.quality < 0.9 and hi.quality < 0.9


def test_fewer_than_three_matches_is_not_a_registration():
    """两对点怎么平移都对得上 —— 少于 3 对不叫配准,哪怕 quality = 1.0。"""
    ref = np.array([[0.0, 0.0], [2.0, 1.0]])
    new = ref + np.array([0.3, -0.1])
    r = register(new, ref, (0.0, 0.0), field=None)
    assert r.n_matched == 2 and r.quality == pytest.approx(1.0)
    assert not r.trustworthy and r.reason == "few_matches"


def test_molecules_leaving_the_field_do_not_count_against_the_quality(ref_pts):
    """平移 2.0 nm:参考帧边上的分子被挪出了新帧视野,它们不该算「应当看得到」。
    给了 field → quality 近 1;不给(expected = 全部参考分子)→ 明显更低。这是 expected 的定义,不是巧合。"""
    shift = (2.0, 0.0)
    new = view(scene(), shift, noise=0.03, seed=9)
    with_field = register(new, ref_pts, (0.0, 0.0), field=FIELD)
    without = register(new, ref_pts, (0.0, 0.0), field=None)
    assert err(with_field, shift) < 0.02 and err(without, shift) < 0.02
    assert with_field.quality > 0.95
    assert without.quality < 0.9 < with_field.quality
    assert with_field.n_expected < without.n_expected == len(ref_pts)


def test_a_field_shifted_with_the_frame_centre_is_honoured(ref_pts):
    """新帧中心不在原点(两帧中心不同)时,expected 按新帧视野的真实位置数。"""
    shift = (0.3, 0.2)
    cx, cy = 1.5, -1.0
    p = scene() + np.asarray(shift)
    new = p[(np.abs(p[:, 0] - cx) <= W / 2) & (np.abs(p[:, 1] - cy) <= H / 2)]
    r = register(new, ref_pts, (0.0, 0.0), field=(cx, cy, W, H))
    assert err(r, shift) < 0.02
    assert r.quality > 0.95


def test_rms_is_the_residual_of_the_matched_pairs(ref_pts):
    clean = register(view(scene(), (0.3, 0.2), seed=11), ref_pts, field=FIELD)
    assert clean.rms_nm is not None and clean.rms_nm < 0.04     # 参考集自带 20 pm/轴 噪声 → 约 28 pm
    noisy = register(view(scene(), (0.3, 0.2), noise=0.06, seed=11), ref_pts, field=FIELD)
    assert 0.04 < noisy.rms_nm < 0.12
    assert noisy.rms_nm > clean.rms_nm


def test_matching_is_one_to_one(ref_pts):
    """新帧里同一处有两个靠得极近的假检:一个参考分子只能配一个。"""
    new = view(scene(), (0.2, 0.2), noise=0.02, seed=12)
    dup = np.vstack([new, new + 0.01])
    r = register(dup, ref_pts, field=FIELD)
    assert len(set(r.pairs[:, 1])) == len(r.pairs) == r.n_matched
    assert len(set(r.pairs[:, 0])) == len(r.pairs)
    assert r.n_matched <= len(ref_pts)


@pytest.mark.parametrize("new,ref", [
    (np.empty((0, 2)), np.zeros((5, 2))),
    (np.zeros((5, 2)), np.empty((0, 2))),
    (np.array([[np.nan, 0.0], [1.0, np.inf]]), np.array([[0.0, 0.0], [1.0, 1.0]])),
    (np.array([[0.0, 0.0]]), np.array([[0.0, 0.0]])),
])
def test_degenerate_inputs_do_not_raise_and_are_not_trusted(new, ref):
    r = register(new, ref, (0.0, 0.0))
    assert not r.trustworthy and r.n_matched < 3
    assert r.reason in ("no_overlap", "few_matches", "low_quality")


def test_the_registration_is_a_translation_it_does_not_look_at_the_scan_angle():
    """配准函数的签名里没有角度:与扫描角无关是结构性的,不是靠测出来的。"""
    import inspect

    assert not any("angle" in p for p in inspect.signature(register).parameters)


# ═══════════════════════════════════════════════════════════════════════════
# 三、晶格别名:已知限制,钉成限制
# ═══════════════════════════════════════════════════════════════════════════

LATTICE_A = 0.7


def test_on_a_perfect_lattice_the_prior_resolves_the_alias_and_the_ambiguity_is_reported():
    """完美六角晶格(a = 0.7 nm):差一个晶格矢量的平移票数几乎一样(这里是 100%)。先验在半个晶格
    常数以内时靠先验裁决,恢复得对;同时 competing_peak_ratio ≈ 1 把「还有别的峰在竞争」报出来。"""
    ref = hex_lattice(LATTICE_A, W, H)
    rng = np.random.default_rng(0)
    for shift, prev in [((0.12, -0.08), (0.0, 0.0)), ((0.2, 0.1), (0.0, 0.0)), ((0.34, 0.0), (0.3, 0.0)),
                        ((0.37, -0.21), (0.3, -0.1))]:
        new = hex_lattice(LATTICE_A, W, H, shift=shift)
        new = new + rng.normal(0.0, 0.03, new.shape)
        r = register(new, ref, prev, field=FIELD)
        assert err(r, shift) < 0.01, (shift, prev, r.drift_nm)
        assert r.competing_peak_ratio is not None and r.competing_peak_ratio >= DR.AMBIGUOUS_RATIO


def test_on_a_perfect_lattice_a_prior_off_by_more_than_half_a_lattice_constant_is_one_molecule_off():
    """已知限制:先验偏离真值超过半个晶格常数 → 选到邻近的别名,差一个晶格矢量(≈ a)。质量仍是 1.0
    (晶格对晶格,处处对得上),所以**质量发现不了**;只有 competing_peak_ratio ≈ 1 在喊。
    这条测试把限制钉住:它红了,说明有人改了裁决规则 —— 要有意识地改,别让它悄悄变。"""
    ref = hex_lattice(LATTICE_A, W, H)
    shift = (0.37, -0.21)                      # 离先验 (0,0) 0.43 nm,超过 a/2 = 0.35
    new = hex_lattice(LATTICE_A, W, H, shift=shift)
    r = register(new, ref, (0.0, 0.0), field=FIELD)
    assert r.quality > 0.9                      # 质量看不出错
    assert err(r, shift) > 0.5 * LATTICE_A      # 错了一个晶格矢量量级
    assert r.competing_peak_ratio >= DR.AMBIGUOUS_RATIO
    # 同一份数据,先验在 a/2 以内就对了:错的是先验,不是数据。
    ok = register(new, ref, (0.3, -0.1), field=FIELD)
    assert err(ok, shift) < 0.01


@pytest.mark.parametrize("noise", [0.03, 0.05])
def test_the_prior_resolves_the_lattice_alias_across_many_noise_draws(noise):
    """先验在半个晶格常数以内时,近平局取先验必须**稳**:30 次不同的噪声 / 平移一次别名都不能选错。
    (去掉 3×3 盒式求和后,同样的数据有三到五成选到别名 —— 逐格票数的涨落比 10% 的平局带还大,
    平局规则就失效了。这条钉的是「平局规则靠平滑的直方图才成立」。)"""
    ref0 = hex_lattice(LATTICE_A, W, H)
    wrong = []
    for seed in range(30):
        rng = np.random.default_rng(seed)
        s = rng.uniform(-0.28, 0.28, 2)
        new = hex_lattice(LATTICE_A, W, H, shift=s)
        new = new + rng.normal(0.0, noise, new.shape)
        ref = ref0 + rng.normal(0.0, noise * 0.5, ref0.shape)
        r = register(new, ref, (0.0, 0.0), field=FIELD)
        if err(r, s) > 0.1:
            wrong.append((seed, round(err(r, s), 3)))
    assert not wrong, wrong


def test_a_non_periodic_arrangement_has_no_competing_peak(ref_pts):
    new = view(scene(), (0.3, -0.2), noise=0.03, seed=13)
    r = register(new, ref_pts, (0.0, 0.0), field=FIELD)
    assert r.competing_peak_ratio is not None and r.competing_peak_ratio < 0.5


# ═══════════════════════════════════════════════════════════════════════════
# 四、drift_velocity
# ═══════════════════════════════════════════════════════════════════════════

T0 = 1.7e9


def hist(drifts, times, quality=0.9):
    q = quality if isinstance(quality, (list, tuple)) else [quality] * len(times)
    return [{"t": T0 + t, "drift_nm": [float(d[0]), float(d[1])], "quality": qq}
            for d, t, qq in zip(drifts, times, q)]


def line(v_nm_per_h, times):
    return [(v_nm_per_h[0] * t / 3600.0 + 0.5, v_nm_per_h[1] * t / 3600.0 - 0.2) for t in times]


def test_velocity_of_a_known_slope_in_nm_per_h():
    times = [0, 600, 1200, 1800]
    vx, vy = drift_velocity(hist(line((0.4, -0.2), times), times))
    assert vx == pytest.approx(0.4, abs=1e-6) and vy == pytest.approx(-0.2, abs=1e-6)


def test_velocity_is_capped_at_1_nm_per_h_and_keeps_its_direction():
    times = [0, 600, 1200, 1800]
    vx, vy = drift_velocity(hist(line((3.0, 4.0), times), times))      # 5 nm/h 的真实斜率
    assert math.hypot(vx, vy) == pytest.approx(1.0, abs=1e-9)
    assert vx / vy == pytest.approx(3.0 / 4.0)
    # 不封顶时(上限抬高)就是真斜率
    assert drift_velocity(hist(line((3.0, 4.0), times), times), cap_nm_per_h=10.0) == pytest.approx((3.0, 4.0))
    # 刚好不到上限的不动
    vx, vy = drift_velocity(hist(line((0.6, 0.8), times), times))
    assert (vx, vy) == pytest.approx((0.6, 0.8))


@pytest.mark.parametrize("times,expect_zero", [
    ([0, 200, 400], True),                  # 跨度 400 s < 600
    ([0, 599.9], True),
    ([0, 600], False),                      # 恰 600 s:够
    ([0, 300, 900], False),
])
def test_velocity_is_zero_when_the_time_baseline_is_under_600_s(times, expect_zero):
    got = drift_velocity(hist(line((0.5, 0.5), times), times))
    assert (got == (0.0, 0.0)) == expect_zero
    if not expect_zero:
        assert got == pytest.approx((0.5, 0.5), abs=1e-6)


def test_velocity_needs_two_registrations():
    assert drift_velocity([]) == (0.0, 0.0)
    assert drift_velocity(hist([(0.1, 0.1)], [0])) == (0.0, 0.0)


def test_velocity_ignores_weak_registrations():
    """quality < 0.5 的配准不进拟合:中间夹一个离谱的弱配准,斜率不变。"""
    times = [0, 600, 1200, 1800]
    d = line((0.4, -0.2), times)
    d[1] = (9.0, 9.0)                       # 弱配准给了个离谱值
    got = drift_velocity(hist(d, times, quality=[0.9, 0.2, 0.9, 0.9]))
    assert got == pytest.approx((0.4, -0.2), abs=1e-6)
    # 把它当成强配准,斜率就被拉歪了 —— 证明上面那条确实在过滤
    assert drift_velocity(hist(d, times, quality=0.9)) != pytest.approx((0.4, -0.2), abs=0.05)


def test_velocity_ignores_records_flagged_untrustworthy_even_with_a_high_quality():
    times = [0, 600, 1200]
    recs = hist(line((0.4, 0.4), times), times)
    recs[2]["drift_nm"] = [50.0, 50.0]
    recs[2]["trustworthy"] = False
    assert drift_velocity(recs) == pytest.approx((0.4, 0.4), abs=1e-6)


def test_velocity_fits_only_the_last_four_good_registrations():
    """六条可信配准,前两条斜率完全不同:拟合只看最后四条。"""
    times = [0, 600, 1200, 1800, 2400, 3000]
    d = [(50.0, 50.0), (-30.0, 7.0)] + line((0.4, -0.2), times[2:])
    got = drift_velocity(hist(d, times))
    assert got == pytest.approx((0.4, -0.2), abs=1e-6)
    # 夹着弱配准时,「最近四条」数的是可信的那几条,不是最近四条里挑可信的
    d2 = line((0.4, -0.2), times)
    q = [0.9, 0.9, 0.9, 0.9, 0.2, 0.2]
    assert drift_velocity(hist(d2, times, quality=q)) == pytest.approx((0.4, -0.2), abs=1e-6)


def test_velocity_extrapolates_the_drift_in_time():
    """速度 × 时间差 = 外推量(单位换算 nm/h → nm/s 别错):0.36 nm/h 走 1 h 就是 0.36 nm。"""
    t = DriftTracker()
    times = [0, 600, 1200, 1800]
    for tt, dr in zip(times, line((0.36, -0.18), times)):
        t.note_registration(_reg(dr), T0 + tt)
    last = t.last_good["drift_nm"]
    p = t.predict(T0 + 1800 + 3600)
    assert p["drift_nm"][0] == pytest.approx(last[0] + 0.36, abs=1e-3)
    assert p["drift_nm"][1] == pytest.approx(last[1] - 0.18, abs=1e-3)


# ═══════════════════════════════════════════════════════════════════════════
# 五、DriftTracker
# ═══════════════════════════════════════════════════════════════════════════

def _reg(drift, quality=0.9, n_matched=40, matches=None, trustworthy=True):
    return {"drift_nm": [float(drift[0]), float(drift[1])], "quality": quality,
            "n_matched": n_matched, "n_expected": 44, "rms_nm": 0.04, "trustworthy": trustworthy,
            "reason": "" if trustworthy else "low_quality", "matches": matches or []}


def test_tracker_without_any_registration_predicts_zero_and_says_so():
    p = DriftTracker().predict(T0)
    assert p["drift_nm"] == [0.0, 0.0] and p["source"] == "none"


def test_a_weak_registration_is_not_believed_the_extrapolated_drift_is_used():
    t = DriftTracker()
    t.note_registration(_reg((0.40, -0.20)), T0)
    assert t.predict(T0 + 10)["source"] == "registered"
    wild = _reg((3.0, 3.0), quality=0.2, trustworthy=False)
    assert t.note_registration(wild, T0 + 600) is False
    p = t.predict(T0 + 700)
    assert p["source"] == "extrapolated"
    assert p["drift_nm"] == pytest.approx([0.40, -0.20], abs=1e-9)      # 速度为 0:就是上一次可信值
    assert t.last_good["drift_nm"] == [0.4, -0.2]


def test_the_quality_threshold_is_applied_by_the_tracker_too():
    t = DriftTracker(min_quality=0.5)
    assert t.note_registration(_reg((0.1, 0.1), quality=0.49), T0) is False
    assert t.note_registration(_reg((0.1, 0.1), quality=0.5), T0 + 1) is True
    assert t.note_registration(_reg((0.1, 0.1), quality=0.9, n_matched=2), T0 + 2) is False   # < 3 对


def test_three_consecutive_weak_registrations_mean_drift_lost():
    t = DriftTracker()
    t.note_registration(_reg((0.1, 0.1)), T0)
    for k in range(1, 3):
        t.note_registration(_reg((9, 9), quality=0.1, trustworthy=False), T0 + 600 * k)
        assert not t.lost and t.weak_streak == k
    t.note_registration(_reg((9, 9), quality=0.1, trustworthy=False), T0 + 1800)
    assert t.lost and t.weak_streak == 3


def test_a_trustworthy_registration_in_between_resets_the_weak_count():
    t = DriftTracker()
    for q, ok in [(0.1, False), (0.1, False), (0.9, True), (0.1, False), (0.1, False)]:
        t.note_registration(_reg((0.1, 0.1), quality=q, trustworthy=ok), T0 + 600 * t.n_registrations)
    assert t.weak_streak == 2 and not t.lost


def test_a_failed_attempt_counts_as_a_weak_one():
    t = DriftTracker()
    t.note_failure("scan failed", T0)
    t.note_failure("no frame", T0 + 1)
    assert t.weak_streak == 2 and not t.lost
    t.note_failure("skill error", T0 + 2)
    assert t.lost and t.n_failed == 3


def test_weak_limit_zero_never_declares_drift_lost():
    t = DriftTracker(weak_limit=0)
    for k in range(10):
        t.note_failure("x", T0 + k)
    assert not t.lost


def test_an_unparseable_registration_is_a_failure_not_a_crash():
    t = DriftTracker()
    assert t.note_registration({"quality": "?"}, T0) is False
    assert t.weak_streak == 1


# ── 按位点算目标 ──────────────────────────────────────────────────────────────

def _field_matches():
    """参考分子排成 7×7 网格(间距 1 nm,中心在原点);位移场**不均匀**:x 位移 = 0.1 + 0.05·x_ref,
    y 位移 = −0.2 + 0.02·y_ref(模拟压电蠕变 / 非线性造成的位置相关漂移)。分子 j 的下标 = 行优先序号。"""
    rows, truth = [], {}
    j = 0
    for iy in range(-3, 4):
        for ix in range(-3, 4):
            rx, ry = float(ix), float(iy)
            dx, dy = 0.1 + 0.05 * rx, -0.2 + 0.02 * ry
            rows.append([j, rx, ry, rx + dx, ry + dy])
            truth[j] = (dx, dy)
            j += 1
    return rows, truth


def _tracker_with_field():
    rows, truth = _field_matches()
    t = DriftTracker()
    t.note_registration(_reg((0.1, -0.2), n_matched=len(rows), matches=rows), T0)
    return t, rows, truth


def test_an_anchored_site_uses_the_anchor_molecules_displacement():
    """mol / hbond / ring 位点:目标 = 锚点分子当前位置的均值 + anchor_offset。
    这里直接按规格式算一遍(当前位置均值 + offset − 名义位置),必须与追踪器给的位移一致。"""
    t, rows, truth = _tracker_with_field()
    ref_of = {int(r[0]): (r[1], r[2]) for r in rows}
    cur_of = {int(r[0]): (r[3], r[4]) for r in rows}
    anchors = [10, 11]                                           # 一个氢键:两个相邻分子
    mean_ref = np.mean([ref_of[a] for a in anchors], axis=0)
    nominal = mean_ref + np.array([0.12, -0.05])                 # 名义位置 = 锚点均值 + anchor_offset
    offset = nominal - mean_ref
    spec_target = np.mean([cur_of[a] for a in anchors], axis=0) + offset      # 规格式
    info = t.displacement_for_site({"x_nm": nominal[0], "y_nm": nominal[1], "kind": "auto",
                                    "rkind": "hbond", "anchor": anchors}, T0)
    assert info["mode"] == "anchor"
    assert nominal + np.array(info["delta_nm"]) == pytest.approx(spec_target, abs=1e-9)
    # 且与全局漂移明显不同(不均匀场里锚点位移 ≠ 全局):这条在鉴别「锚点」和「全局」
    assert abs(info["delta_nm"][0] - 0.1) > 0.01


def test_unmatched_anchors_are_filled_with_the_global_drift_not_dropped():
    t, rows, truth = _tracker_with_field()
    rows2 = [r for r in rows if int(r[0]) != 11]                 # 锚点 11 没配上
    t2 = DriftTracker()
    t2.note_registration(_reg((0.1, -0.2), n_matched=len(rows2), matches=rows2), T0)
    ref_of = {int(r[0]): (r[1], r[2]) for r in rows}
    nominal = np.mean([ref_of[10], ref_of[11]], axis=0)
    info = t2.displacement_for_site({"x_nm": nominal[0], "y_nm": nominal[1], "rkind": "hbond",
                                     "anchor": [10, 11]}, T0)
    want = np.mean([truth[10], (0.1, -0.2)], axis=0)             # 配上的用自己的,没配上的用全局 D
    assert info["mode"] == "anchor" and info["delta_nm"] == pytest.approx(list(want), abs=1e-9)


def test_a_site_with_no_matched_anchor_falls_back_to_local_then_global():
    t, rows, truth = _tracker_with_field()
    info = t.displacement_for_site({"x_nm": 0.4, "y_nm": 0.3, "rkind": "mol", "anchor": [9999]}, T0)
    assert info["mode"] == "local"


def test_other_sites_use_the_median_displacement_of_nearby_molecules():
    """非锚点位点(au / edge / fixed):3 nm 以内配上的分子的位移中位数。网格上 (0.5, 0.5) 周围
    3 nm 内的分子很多,位移场线性 → 中位数 = 区域中心处的场值附近,与全局 D 不同。"""
    t, rows, truth = _tracker_with_field()
    pos = np.array([2.0, -2.0])
    info = t.displacement_for_site({"x_nm": pos[0], "y_nm": pos[1], "rkind": "au", "anchor": []}, T0)
    near = [truth[int(r[0])] for r in rows if math.hypot(r[1] - pos[0], r[2] - pos[1]) <= 3.0]
    assert info["mode"] == "local" and info["n_local"] == len(near) >= 3
    assert info["delta_nm"] == pytest.approx(list(np.median(near, axis=0)), abs=1e-9)
    assert info["delta_nm"][0] != pytest.approx(0.1, abs=0.01)           # 不是全局


def test_local_median_is_robust_to_an_outlier_match():
    rows, _ = _field_matches()
    rows = [list(r) for r in rows]
    rows[24][3] += 5.0                                           # 中心分子被误配,位移差 5 nm
    t = DriftTracker()
    t.note_registration(_reg((0.1, -0.2), n_matched=len(rows), matches=rows), T0)
    info = t.displacement_for_site({"x_nm": 0.0, "y_nm": 0.0, "rkind": "au", "anchor": []}, T0)
    assert abs(info["delta_nm"][0] - 0.1) < 0.1                  # 均值会被拉到 > 0.2;中位数不会


def test_with_fewer_than_three_nearby_molecules_the_global_drift_is_used():
    rows = [[0, 0.0, 0.0, 0.3, -0.1], [1, 1.0, 0.0, 1.3, -0.1], [2, 20.0, 20.0, 20.3, 19.9]]
    t = DriftTracker()
    t.note_registration(_reg((0.3, -0.1), n_matched=3, matches=rows), T0)
    info = t.displacement_for_site({"x_nm": 0.5, "y_nm": 0.0, "rkind": "au", "anchor": []}, T0)
    assert info["mode"] == "global" and info["n_local"] == 2
    assert info["delta_nm"] == pytest.approx([0.3, -0.1])


def test_the_site_target_adds_the_velocity_times_the_time_since_the_registration():
    """p += drift_velocity·(t_now − reg.t_mid):位点目标也要外推到「现在」。"""
    t = DriftTracker()
    times = [0, 600, 1200, 1800]
    rows = [[0, 0.0, 0.0, 0.0, 0.0], [1, 1.0, 0.0, 1.0, 0.0], [2, 0.0, 1.0, 0.0, 1.0]]
    for tt, dr in zip(times, line((0.36, -0.18), times)):
        rr = [[r[0], r[1], r[2], r[1] + dr[0], r[2] + dr[1]] for r in rows]
        t.note_registration(_reg(dr, n_matched=3, matches=rr), T0 + tt)
    base = t.displacement_for_site({"x_nm": 0.3, "y_nm": 0.3, "rkind": "au"}, T0 + 1800)
    later = t.displacement_for_site({"x_nm": 0.3, "y_nm": 0.3, "rkind": "au"}, T0 + 1800 + 3600)
    assert later["delta_nm"][0] - base["delta_nm"][0] == pytest.approx(0.36, abs=1e-6)
    assert later["delta_nm"][1] - base["delta_nm"][1] == pytest.approx(-0.18, abs=1e-6)


def test_tracker_state_is_plain_json_and_roundtrips():
    t, rows, _ = _tracker_with_field()
    t.note_failure("x", T0 + 5)
    blob = json.dumps(t.to_dict())                                # 纯 JSON,能过检查点
    t2 = DriftTracker.from_dict(json.loads(blob))
    assert t2.weak_streak == t.weak_streak and t2.last_good["drift_nm"] == t.last_good["drift_nm"]
    site = {"x_nm": 2.0, "y_nm": -2.0, "rkind": "au", "anchor": []}
    assert t2.displacement_for_site(site, T0) == t.displacement_for_site(site, T0)


def test_a_checkpointed_tracker_drops_matches_and_degrades_to_the_global_drift():
    t, rows, _ = _tracker_with_field()
    small = json.dumps(t.to_dict(include_matches=False))
    assert len(small) < len(json.dumps(t.to_dict())) / 3
    t2 = DriftTracker.from_dict(json.loads(small))
    assert t2.last_good["drift_nm"] == [0.1, -0.2] and t2.last_good["matches"] == []
    info = t2.displacement_for_site({"x_nm": 2.0, "y_nm": -2.0, "rkind": "au"}, T0)
    assert info["mode"] == "global" and info["delta_nm"] == pytest.approx([0.1, -0.2])


def test_history_is_bounded():
    t = DriftTracker()
    for k in range(DR._MAX_HISTORY + 20):
        t.note_failure("x", T0 + k)
    assert len(t.history) == DR._MAX_HISTORY


# ═══════════════════════════════════════════════════════════════════════════
# 六、坐标换算:单一来源 + 往返
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("deg", [0.0, 30.0, -137.5, 90.0, 180.0])
def test_delta_to_scanner_is_the_single_source_conversion_without_the_translation(deg):
    """位移的换算 = ReadSiteMarks 那条(经 px_to_m)对两个点之差:符号约定只在那一处。"""
    centre = FrameGeometry(40.0, -60.0, 12.0, 12.0, deg, True, "t")
    a = frame_nm_to_scanner_m(1.7, -0.9, centre)
    o = frame_nm_to_scanner_m(0.0, 0.0, centre)
    assert frame_delta_to_scanner_m(1.7, -0.9, deg) == pytest.approx((a[0] - o[0], a[1] - o[1]), abs=1e-15)


@pytest.mark.parametrize("deg", [0.0, 12.0, 30.0, -137.5, 90.0, 180.0])
def test_scanner_to_frame_is_the_exact_inverse_for_any_sign_convention(deg):
    fx, fy = frame_delta_to_scanner_m(1.7, -0.9, deg)
    back = scanner_delta_to_frame(fx * 1e9, fy * 1e9, deg)
    assert back == pytest.approx((1.7, -0.9), abs=1e-9)
    assert math.hypot(fx, fy) * 1e9 == pytest.approx(math.hypot(1.7, 0.9), rel=1e-12)   # 只转不缩
    if deg == 0.0:
        assert (fx, fy) == pytest.approx((1.7e-9, -0.9e-9), abs=1e-24)


# ═══════════════════════════════════════════════════════════════════════════
# 七、技能壳:合成 .sxm
# ═══════════════════════════════════════════════════════════════════════════

def run(**params):
    return RegisterDriftByMolecules().execute(None, params)


@pytest.fixture(scope="module")
def frames(tmp_path_factory):
    """参考帧 + 平移了已知量的新帧(θ=0)。模块级:渲染一次。"""
    d = tmp_path_factory.mktemp("drift_frames")
    sc = scene(seed=11, size=18.0)
    shift = (0.37, -0.21)
    ref = write_sxm(d / "ref.sxm", render(sc), offset_nm=(-75.0, 153.0))
    new = write_sxm(d / "new.sxm", render(sc + np.asarray(shift), noise=3e-12, seed=1),
                    offset_nm=(-75.0, 153.0))
    return {"dir": d, "scene": sc, "shift": shift, "ref": str(ref), "new": str(new)}


def test_skill_recovers_a_known_shift_from_sxm_frames(frames):
    res = run(frame_path=frames["new"], reference_frame_path=frames["ref"])
    assert res.success, res.error
    d = res.data
    assert d["trustworthy"] and d["quality"] > 0.95 and d["reason"] == ""
    assert d["drift_nm"] == pytest.approx(list(frames["shift"]), abs=0.02)
    assert d["drift_scanner_m"] == pytest.approx([frames["shift"][0] * 1e-9, frames["shift"][1] * 1e-9], abs=2e-11)
    assert d["rms_nm"] < 0.05 and 100 < d["n_matched"] <= d["n_expected"]
    assert d["angle_deg"] == 0.0 and d["warnings"] == [] and d["degraded"] is False
    assert res.summary and "nm" in res.summary and f"{d['quality']:.2f}" in res.summary


def test_skill_matches_list_the_displacement_of_each_matched_molecule(frames):
    d = run(frame_path=frames["new"], reference_frame_path=frames["ref"]).data
    m = np.array(d["matches"])
    assert m.shape == (d["n_matched"], 5)
    disp = m[:, 3:5] - m[:, 1:3]
    assert np.median(disp, axis=0) == pytest.approx(list(frames["shift"]), abs=0.02)
    assert np.all(np.hypot(*(disp - np.array(d["drift_nm"])).T) < DR.MATCH_TOL_NM + 1e-9)
    assert len(set(m[:, 0].astype(int))) == len(m)                 # 参考下标不重复(一对一)


def test_skill_include_matches_false_returns_no_matches(frames):
    d = run(frame_path=frames["new"], reference_frame_path=frames["ref"], include_matches=False).data
    assert d["matches"] == [] and d["n_matched"] > 100


def test_skill_height_ratio_is_about_one_for_the_same_contrast_and_drops_for_a_blunter_tip(frames):
    same = run(frame_path=frames["new"], reference_frame_path=frames["ref"]).data["height_ratio"]
    assert same == pytest.approx(1.0, abs=0.05)
    blunt = write_sxm(frames["dir"] / "blunt.sxm",
                      render(frames["scene"] + np.asarray(frames["shift"]), amp=0.5e-10, noise=3e-12, seed=2),
                      offset_nm=(-75.0, 153.0))
    r = run(frame_path=str(blunt), reference_frame_path=frames["ref"])
    assert r.data["height_ratio"] == pytest.approx(0.5, abs=0.08)
    assert r.data["trustworthy"]                                  # 提示项,不进 trustworthy


def test_skill_prev_drift_is_the_search_window_centre(frames):
    far = (2.2, 1.7)
    new = write_sxm(frames["dir"] / "far.sxm",
                    render(frames["scene"] + np.asarray(far), noise=3e-12, seed=3), offset_nm=(-75.0, 153.0))
    near = run(frame_path=str(new), reference_frame_path=frames["ref"],
               prev_drift_x_nm=2.1, prev_drift_y_nm=1.6, window_nm=0.8, wide_window_nm=0.8).data
    assert near["drift_nm"] == pytest.approx(list(far), abs=0.02) and near["widened"] is False
    wide = run(frame_path=str(new), reference_frame_path=frames["ref"]).data      # 先验 0:靠放宽
    assert wide["drift_nm"] == pytest.approx(list(far), abs=0.02) and wide["widened"] is True


def test_skill_a_frame_with_other_molecules_is_a_success_that_is_not_trustworthy(frames):
    other = write_sxm(frames["dir"] / "other.sxm", render(poisson_disk(18.0, 18.0, 0.6, seed=123)),
                      offset_nm=(-75.0, 153.0))
    res = run(frame_path=str(other), reference_frame_path=frames["ref"])
    assert res.success                                            # 量出来了:对不上
    assert res.data["trustworthy"] is False and res.data["reason"] in ("low_quality", "few_matches")
    assert res.data["quality"] < 0.5
    assert any("not trustworthy" in w or "不可信" in w for w in res.data["warnings"])


def test_skill_two_different_centres_are_accounted_for(frames):
    """追踪帧的中心不是参考帧的中心:位移要折算进去,恢复的仍是物理漂移。"""
    sh = np.asarray(frames["shift"])
    c_new = np.array([-75.0 + 1.0, 153.0 + 0.5])
    new = write_sxm(frames["dir"] / "off.sxm", render(frames["scene"] + sh - np.array([1.0, 0.5]), noise=3e-12, seed=4),
                    offset_nm=tuple(c_new))
    d = run(frame_path=str(new), reference_frame_path=frames["ref"]).data
    assert d["drift_nm"] == pytest.approx(list(frames["shift"]), abs=0.02)
    assert d["frames"]["center_shift_nm"] == pytest.approx([1.0, 0.5], abs=1e-6)


@pytest.mark.parametrize("theta", [30.0, -75.0])
def test_skill_physical_drift_is_recovered_on_rotated_frames_with_different_centres(tmp_path, theta):
    """θ≠0 + 两帧中心不同。**与符号约定无关**:前向矩阵 F 来自 frame_nm_to_scanner_m(单一来源),
    造数据用 inv(F),被测代码用它自己的逆 —— 约定哪天再变,这条不用改,而写反了会红。"""
    F = np.array([frame_delta_to_scanner_m(1.0, 0.0, theta), frame_delta_to_scanner_m(0.0, 1.0, theta)]).T * 1e9
    Finv = np.linalg.inv(F)
    sc_abs = scene(seed=11, size=18.0) + np.array([10.0, -20.0])        # 场景在**扫描器**绝对坐标里
    c_ref, c_new = np.array([10.0, -20.0]), np.array([10.8, -19.3])
    d_phys = np.array([0.42, -0.15])                                    # 物理漂移(扫描器轴,nm)

    def frame_coords(p, c):
        return (Finv @ (p - c).T).T

    ref = write_sxm(tmp_path / "r.sxm", render(frame_coords(sc_abs, c_ref)), offset_nm=tuple(c_ref), angle_deg=theta)
    new = write_sxm(tmp_path / "n.sxm", render(frame_coords(sc_abs + d_phys, c_new), noise=3e-12, seed=5),
                    offset_nm=tuple(c_new), angle_deg=theta)
    d = run(frame_path=str(new), reference_frame_path=str(ref)).data
    assert d["trustworthy"] and d["quality"] > 0.9
    assert d["drift_scanner_m"] == pytest.approx(list(d_phys * 1e-9), abs=3e-11)
    assert d["drift_nm"] == pytest.approx(list(Finv @ d_phys), abs=0.03)
    assert d["angle_deg"] == pytest.approx(theta)


def test_skill_drift_nm_does_not_depend_on_the_scan_angle(frames, tmp_path):
    """同样的像素内容、不同的 SCAN_ANGLE:帧轴上的 drift_nm 不变(配准是平移,与角度无关),
    变的只有扫描器轴上的 drift_scanner_m。"""
    sh = np.asarray(frames["shift"])
    out = {}
    for th in (0.0, 40.0):
        ref = write_sxm(tmp_path / f"r{th}.sxm", render(frames["scene"]), angle_deg=th)
        new = write_sxm(tmp_path / f"n{th}.sxm", render(frames["scene"] + sh, noise=3e-12, seed=6), angle_deg=th)
        out[th] = run(frame_path=str(new), reference_frame_path=str(ref)).data
    assert out[0.0]["drift_nm"] == pytest.approx(out[40.0]["drift_nm"], abs=0.01)
    assert out[0.0]["drift_scanner_m"] != pytest.approx(out[40.0]["drift_scanner_m"], abs=1e-11)
    assert math.hypot(*out[0.0]["drift_scanner_m"]) == pytest.approx(math.hypot(*out[40.0]["drift_scanner_m"]), rel=0.02)


def test_skill_json_inputs_list_and_anchor_molecules_shape():
    ref = view(scene(), (0.0, 0.0), noise=0.02, seed=1)
    shift = (0.33, -0.12)
    new = view(scene(), shift, noise=0.03, seed=2)
    as_list = run(reference_molecules=json.dumps(ref.tolist()), new_molecules=json.dumps(new.tolist()))
    assert as_list.success and as_list.data["drift_nm"] == pytest.approx(list(shift), abs=0.02)
    assert as_list.data["drift_scanner_m"] is None             # 只给了坐标:没有扫描角,不假装有
    # ReadSiteMarks.anchor_molecules 的形状:键是下标(不连续),matches 的参考下标就是这些键
    keys = list(range(3, 3 * len(ref) + 3, 3))
    anchors = {str(k): {"x_nm": float(p[0]), "y_nm": float(p[1])} for k, p in zip(keys, ref)}
    as_dict = run(reference_molecules=json.dumps(anchors), new_molecules=json.dumps(new.tolist()))
    assert as_dict.success and as_dict.data["drift_nm"] == pytest.approx(list(shift), abs=0.02)
    ids = {int(m[0]) for m in as_dict.data["matches"]}
    assert ids <= set(keys) and len(ids) == as_dict.data["n_matched"]


def test_skill_scan_direction_and_bias_mismatches_are_warned(frames):
    up = write_sxm(frames["dir"] / "up.sxm",
                   render(frames["scene"] + np.asarray(frames["shift"]), noise=3e-12, seed=7),
                   offset_nm=(-75.0, 153.0), scan_dir="up", bias=0.5)
    d = run(frame_path=str(up), reference_frame_path=frames["ref"]).data
    text = " ".join(d["warnings"])
    assert ("up" in text and "down" in text) and ("0.5" in text)
    assert d["drift_nm"] == pytest.approx(list(frames["shift"]), abs=0.02)   # 方向修正后仍配得上


def test_skill_refuses_frames_with_different_scan_angles(frames, tmp_path):
    rot = write_sxm(tmp_path / "rot.sxm", render(frames["scene"]), offset_nm=(-75.0, 153.0), angle_deg=20.0)
    res = run(frame_path=str(rot), reference_frame_path=frames["ref"])
    assert not res.success and res.data["reason"] == "angle_mismatch" and res.data["degraded"] is True


@pytest.mark.parametrize("params,reason", [
    ({}, "no_reference"),
    ({"reference_molecules": "[[0,0],[1,1],[2,2]]"}, "no_frame"),
    ({"reference_molecules": "not json", "new_molecules": "[]"}, "bad_reference"),
    ({"reference_molecules": "[[0,0],[1,1],[2,2]]", "new_molecules": "{"}, "bad_new"),
    ({"reference_molecules": "[[0,0],[1,1]]", "new_molecules": "[[0,0],[1,1],[2,2]]"}, "too_few_molecules"),
    ({"reference_frame_path": "Z:/no/such.sxm", "new_molecules": "[[0,0]]"}, "reference_unreadable"),
])
def test_skill_errors_are_reasoned_degraded_and_not_a_crash(params, reason):
    res = run(**params)
    assert not res.success and res.data["reason"] == reason and res.data["degraded"] is True
    assert res.data["trustworthy"] is False and res.error


def test_skill_a_blank_new_frame_is_never_trusted(frames, tmp_path):
    """针尖崩了 / 扫到空处,新帧上没有分子:要么直接报「分子太少」,要么(噪声被当成极大值时)配准低质量
    ——无论哪条,都不能给出一个可信的漂移。"""
    blank = write_sxm(tmp_path / "blank.sxm", render([]), offset_nm=(-75.0, 153.0))
    res = run(frame_path=str(blank), reference_frame_path=frames["ref"])
    assert res.data["trustworthy"] is False


def test_skill_never_raises_even_on_garbage(tmp_path):
    bad = tmp_path / "bad.sxm"
    bad.write_bytes(b"not an sxm at all")
    res = run(frame_path=str(bad), reference_frame_path=str(bad))
    assert not res.success and res.data["degraded"] is True


def test_skill_metadata_contract():
    from mast.core.types import SafetyLevel, SkillCategory

    m = RegisterDriftByMolecules().metadata()
    assert m.name == "RegisterDriftByMolecules"
    assert m.safety_level is SafetyLevel.AUTO and m.category is SkillCategory.READ
    names = {p.name for p in m.parameters}
    assert {"frame_path", "reference_frame_path", "new_molecules", "reference_molecules",
            "prev_drift_x_nm", "prev_drift_y_nm", "window_nm", "wide_window_nm", "min_quality",
            "include_matches", "match_tol_nm"} <= names
    assert all(not p.required for p in m.parameters)
    assert not RegisterDriftByMolecules().validate_params({"frame_path": "x", "prev_drift_x_nm": 0.5})
    assert RegisterDriftByMolecules().validate_params({"nonsense": 1})
    # 不碰仪器:没有前置条件、不要上下文
    assert not m.preconditions


def test_skill_does_not_touch_the_context_or_the_files(frames):
    names = ["ref.sxm", "new.sxm"]
    before = {n: (Path(frames["dir"]) / n).stat().st_mtime_ns for n in names}
    res = RegisterDriftByMolecules().execute(object(), {"frame_path": frames["new"],
                                                         "reference_frame_path": frames["ref"]})
    assert res.success and res.nanonis_calls == []
    assert before == {n: (Path(frames["dir"]) / n).stat().st_mtime_ns for n in names}


# ═══════════════════════════════════════════════════════════════════════════
# 八、找刚扫完的帧
# ═══════════════════════════════════════════════════════════════════════════

def test_find_fresh_frame_returns_the_newest_sxm_written_after_the_scan(tmp_path, monkeypatch):
    import time as _t

    from mast.skills.builtins import scan_extra

    monkeypatch.setattr(scan_extra, "_candidate_save_dirs", lambda ctx: [tmp_path])
    monkeypatch.setattr(DR, "_now", _t.time)
    real = _t.time()
    for name, age in (("old.sxm", 900.0), ("new1.sxm", 30.0), ("new2.sxm", 10.0)):
        p = tmp_path / name
        p.write_bytes(b"x")
        os.utime(p, (real - age, real - age))
    # 扫描起点之后写出的里最新的;更早的(old)不会被当成刚扫的
    assert Path(DR.find_fresh_frame(None, real - 60.0)).name == "new2.sxm"
    assert Path(DR.find_fresh_frame(None, real - 15.0)).name == "new2.sxm"
    # 扫描之后没有新文件 → None(调用方这时才去 SaveScan)
    assert DR.find_fresh_frame(None, real + 100.0, slack_s=0.0) is None


def test_find_fresh_frame_ignores_non_sxm_and_missing_dirs(tmp_path, monkeypatch):
    import time as _t

    from mast.skills.builtins import scan_extra

    (tmp_path / "x.dat").write_bytes(b"x")
    monkeypatch.setattr(scan_extra, "_candidate_save_dirs", lambda ctx: [tmp_path, tmp_path / "nope"])
    monkeypatch.setattr(DR, "_now", _t.time)
    assert DR.find_fresh_frame(None, _t.time() - 5.0) is None


def test_find_fresh_frame_waits_for_the_file_to_land(tmp_path, monkeypatch):
    import time as _t

    from mast.skills.builtins import scan_extra

    monkeypatch.setattr(scan_extra, "_candidate_save_dirs", lambda ctx: [tmp_path])
    monkeypatch.setattr(DR, "_now", _t.time)
    calls = {"n": 0}

    def fake_sleep(s):
        calls["n"] += 1
        (tmp_path / "late.sxm").write_bytes(b"x")                          # 睡了一觉,文件落盘了

    monkeypatch.setattr(DR.time, "sleep", fake_sleep)
    got = DR.find_fresh_frame(None, _t.time() - 1.0, wait_s=5.0)
    assert got is not None and Path(got).name == "late.sxm" and calls["n"] == 1


def test_find_fresh_frame_gives_up_after_the_wait(tmp_path, monkeypatch):
    import time as _t

    from mast.skills.builtins import scan_extra

    monkeypatch.setattr(scan_extra, "_candidate_save_dirs", lambda ctx: [tmp_path])
    clock = {"t": _t.time()}
    start = clock["t"]
    monkeypatch.setattr(DR, "_now", lambda: clock["t"])
    monkeypatch.setattr(DR.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    assert DR.find_fresh_frame(None, start - 1.0, wait_s=2.0) is None
    assert clock["t"] - start >= 2.0                                        # 时钟走满了等待窗口才放弃


# ═══════════════════════════════════════════════════════════════════════════
# 九、参数确实传到了配准里;大帧;异常输入
# ═══════════════════════════════════════════════════════════════════════════

def test_skill_window_parameters_reach_the_registration(frames):
    far = (1.4, -1.1)
    new = write_sxm(frames["dir"] / "far2.sxm", render(frames["scene"] + np.asarray(far), noise=3e-12, seed=8),
                    offset_nm=(-75.0, 153.0))
    base = dict(frame_path=str(new), reference_frame_path=frames["ref"])
    narrow_only = run(**base, window_nm=0.8, wide_window_nm=0.8).data
    assert not (narrow_only["trustworthy"] and np.hypot(*(np.array(narrow_only["drift_nm"]) - far)) < 0.05)
    big_narrow = run(**base, window_nm=2.0, wide_window_nm=2.0).data
    assert big_narrow["drift_nm"] == pytest.approx(list(far), abs=0.02)
    assert big_narrow["widened"] is False and big_narrow["window_used_nm"] == pytest.approx(2.0)
    widened = run(**base, window_nm=0.8, wide_window_nm=2.0).data
    assert widened["drift_nm"] == pytest.approx(list(far), abs=0.02)
    assert widened["widened"] is True and widened["window_used_nm"] == pytest.approx(2.0)


def test_skill_match_tolerance_and_min_quality_reach_the_registration(frames):
    base = dict(frame_path=frames["new"], reference_frame_path=frames["ref"])
    tight = run(**base, match_tol_nm=0.01).data                     # 比检测噪声 + 初值误差(半格 25 pm)还小:配不上
    normal = run(**base).data
    assert tight["n_matched"] < 0.5 * normal["n_matched"]
    strict = run(**base, min_quality=1.01 - 1e-9).data            # 比 quality 还高的门槛:不可信
    assert strict["trustworthy"] is False and strict["reason"] == "low_quality"
    assert normal["trustworthy"] is True


def test_the_vote_stage_never_sees_more_than_max_vote_points(monkeypatch):
    """上万个分子的大视野:找峰的投票阶段只用 MAX_VOTE_POINTS 个点(否则成对偏移是 10^8 量级);
    匹配 / 质量仍用全部分子。"""
    rng = np.random.default_rng(5)
    ref = rng.uniform(-40.0, 40.0, (6000, 2))
    shift = (0.31, -0.22)
    new = ref + shift + rng.normal(0.0, 0.03, ref.shape)
    seen = []
    real = DR._offset_histogram

    def spy(n, r, *a, **k):
        seen.append((len(n), len(r)))
        return real(n, r, *a, **k)

    monkeypatch.setattr(DR, "_offset_histogram", spy)
    r = register(new, ref, (0.0, 0.0), field=(0.0, 0.0, 80.0, 80.0))
    assert seen and all(max(pair) <= DR.MAX_VOTE_POINTS for pair in seen)
    assert err(r, shift) < 0.02 and r.trustworthy
    assert r.n_new == r.n_ref == 6000 and r.n_matched > 5000


def test_a_non_finite_prev_drift_is_not_a_prior():
    ref = view(scene(), (0.0, 0.0), noise=0.02, seed=1)
    new = view(scene(), (0.33, -0.12), noise=0.03, seed=2)
    res = run(reference_molecules=json.dumps(ref.tolist()), new_molecules=json.dumps(new.tolist()),
              prev_drift_x_nm=float("nan"), prev_drift_y_nm=float("inf"))
    assert res.success and res.data["drift_nm"] == pytest.approx([0.33, -0.12], abs=0.02)


def test_skill_results_are_json_serialisable(frames):
    """结果要能进聊天 / 检查点:不能夹带 numpy 标量。"""
    for res in (run(frame_path=frames["new"], reference_frame_path=frames["ref"]), run(),
                run(reference_molecules="[[0,0],[1,1],[2,2]]", new_molecules="[[0,0],[1,1],[2,2]]")):
        json.dumps(res.data)


def test_a_site_without_a_nominal_position_does_not_crash_the_tracker():
    t, _rows, _ = _tracker_with_field()
    info = t.displacement_for_site({"rkind": "au"}, T0)
    assert info["mode"] == "local" and len(info["delta_nm"]) == 2


def test_matched_never_exceeds_expected(ref_pts):
    """expected 数的是「可能被配上的」参考分子(视野放宽一个匹配半径),所以 matched ≤ expected 是结构性的
    —— 边缘上的分子也在内。"""
    rng = np.random.default_rng(3)
    for k in range(25):
        s = rng.uniform(-2.4, 2.4, 2)
        r = register(view(scene(), s, noise=0.05, seed=30 + k), ref_pts, (0.0, 0.0), field=FIELD)
        assert r.n_matched <= r.n_expected, (s, r.n_matched, r.n_expected)


def test_the_english_metadata_and_results_have_no_chinese(frames, tmp_path):
    """tr() 的英文半边:元数据、报错、summary、warnings 在 en 下一个汉字都不能有(测试进程默认 zh)。"""
    up = write_sxm(tmp_path / "up_en.sxm", render(frames["scene"] + np.asarray(frames["shift"]), noise=3e-12, seed=9),
                   offset_nm=(-75.0, 153.0), scan_dir="up", bias=0.5)          # 触发方向 / 偏压两条 warning
    rot = write_sxm(tmp_path / "rot_en.sxm", render(frames["scene"]), offset_nm=(-75.0, 153.0), angle_deg=20.0)
    with use_lang("en"):
        meta = RegisterDriftByMolecules().metadata()
        good = run(frame_path=frames["new"], reference_frame_path=frames["ref"])
        warned = run(frame_path=str(up), reference_frame_path=frames["ref"])
        errors = [run(), run(reference_molecules="[[0,0],[1,1],[2,2]]"),
                  run(reference_molecules="[[0,0],[1,1]]", new_molecules="[[0,0],[1,1],[2,2]]"),
                  run(frame_path=str(rot), reference_frame_path=frames["ref"]),
                  run(reference_frame_path="Z:/no/such.sxm", new_molecules="[[0,0]]")]
    texts = [meta.description] + [p.description for p in meta.parameters] + [good.summary]
    texts += list(warned.data["warnings"]) + [e.error for e in errors]
    texts += [w for e in errors for w in e.data.get("warnings", [])]
    assert warned.data["warnings"], "这条用例要真的触发 warning,否则下面的断言是空的"
    assert not [t for t in texts if _CJK.search(t)], [t for t in texts if _CJK.search(t)]
    with use_lang("zh"):
        assert _CJK.search(RegisterDriftByMolecules().metadata().description)
