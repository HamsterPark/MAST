"""TERS 谱的在线分析 —— 纯函数，不碰仪器（规格 docs/v2/design/ters_modes_spec.md §1.7、各模式的「分析」一步）。

组合技能在每条谱落盘之后用这里的函数算谱带强度、质检标记、对比度与拟合，结果进
运行级的 index.csv / summary.json 与 LiveHub 的构建中图线。**所有判据都只标记、不改数据**：
饱和不自动改曝光，宇宙射线默认只标记（``cosmic_filter=median_frames`` 才在多帧上取中值），
对比度在远场低于噪声时只给下限（M04.1）。

横轴约定：光谱仪给的是波长（nm），这里一律换成拉曼位移（cm⁻¹）再按谱带积分；换算用的
激发线是**参数**（CP-A ``laser_line_nm``，633 还是 632.8），不是光谱仪自报的值。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import numpy as np
from mast.i18n import tr

__all__ = [
    "Band",
    "parse_range",
    "parse_bands",
    "parse_baselines",
    "parse_float_list",
    "nm_to_shift_cm1",
    "shift_cm1_to_nm",
    "band_metrics",
    "total_counts",
    "find_cosmic",
    "is_saturated",
    "combine_frames",
    "contrast",
    "band_correlation",
    "fit_cos2",
    "fit_kappa_per_nm",
    "DropDetector",
    "peak_of_grid",
    "FULL_SCALE_COUNTS",
]

#: 16 位 ADC 的满量程（PyLoN eXcelon）。饱和判据是它的一个比例（CP-A ``max_saturation_frac``）。
FULL_SCALE_COUNTS = 65535.0

_NUM = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_RANGE_RE = re.compile(rf"^\s*({_NUM})\s*-\s*({_NUM})\s*$")


@dataclass(frozen=True)
class Band:
    """一个谱带窗口（cm⁻¹，或 STML 积分时的 nm）。"""

    name: str
    lo: float
    hi: float

    def as_dict(self) -> dict:
        return {"name": self.name, "lo": self.lo, "hi": self.hi}


def parse_range(text: str, *, what: str = "range") -> tuple[float, float]:
    """``'1560-1620'`` → ``(1560.0, 1620.0)``；负数下限也认（``'-50--20'``）。不合法抛 ValueError。"""
    m = _RANGE_RE.match(str(text or ""))
    if not m:
        raise ValueError(tr(f"{what} 写成 'lo-hi'（例如 '1560-1620'），收到 {text!r}",
                             f"{what} must be written as 'lo-hi' (e.g. '1560-1620'), got {text!r}"))
    lo, hi = float(m.group(1)), float(m.group(2))
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        raise ValueError(tr(f"{what} 要求 lo < hi，收到 {text!r}", f"{what} requires lo < hi, got {text!r}"))
    return lo, hi


def parse_bands(text: str, *, what: str = "bands_cm1") -> list[Band]:
    """``'ring:1540-1620,breath:990-1010'`` → 两个 Band；不带名字的项按 b1、b2… 命名。

    空串返回 ``[]``（调用方按「总计数」处理）。名字重复、名字不合法都拒绝 —— 名字会成为
    LiveHub 的通道名和 CSV 的列名。"""
    out: list[Band] = []
    raw = str(text or "").strip()
    if not raw:
        return out
    seen: set[str] = set()
    for i, item in enumerate(p for p in raw.split(",") if p.strip()):
        item = item.strip()
        if ":" in item:
            name, rng = item.split(":", 1)
            name = name.strip()
        else:
            name, rng = f"b{i + 1}", item
        if not re.fullmatch(r"[A-Za-z0-9_.+@-]{1,40}", name or ""):
            raise ValueError(tr(f"{what}：谱带名 {name!r} 只能用字母、数字与 _.+@-（≤40 字符）",
                                 f"{what}: band name {name!r} may only use letters, digits and _.+@- (≤40 characters)"))
        if name in seen:
            raise ValueError(tr(f"{what}：谱带名 {name!r} 重复", f"{what}: band name {name!r} is duplicated"))
        seen.add(name)
        lo, hi = parse_range(rng, what=f"{what}[{name}]")
        out.append(Band(name, lo, hi))
    return out


def parse_baselines(text: str, *, what: str = "baseline_cm1") -> list[tuple[float, float]]:
    """``'1500-1530,1630-1660'`` → 两个窗口；空串返回 ``[]``（= 谱带两侧各 20 cm⁻¹）。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    return [parse_range(p, what=what) for p in raw.split(",") if p.strip()]


def parse_float_list(text: str, *, what: str) -> list[float]:
    """逗号/空格分隔的数列（``biases_v`` / ``setpoints_a`` 的约定）。空、非数、非有限都拒绝。"""
    raw = str(text or "").replace(";", ",").strip()
    if not raw:
        raise ValueError(tr(f"{what} 不能为空", f"{what} must not be empty"))
    vals: list[float] = []
    for tok in raw.replace(",", " ").split():
        try:
            v = float(tok)
        except ValueError:
            raise ValueError(tr(f"{what}：{tok!r} 不是数", f"{what}: {tok!r} is not a number")) from None
        if not math.isfinite(v):
            raise ValueError(tr(f"{what}：{tok!r} 不是有限的数", f"{what}: {tok!r} is not a finite number"))
        vals.append(v)
    return vals


# ── 横轴换算 ──────────────────────────────────────────────────────────────────

def nm_to_shift_cm1(nm: Any, laser_nm: float) -> Any:
    """波长（nm）→ 拉曼位移（cm⁻¹）：1e7/λ_laser − 1e7/λ。数组或标量都行。"""
    return 1e7 / float(laser_nm) - 1e7 / np.asarray(nm, dtype=np.float64)


def shift_cm1_to_nm(shift_cm1: float, laser_nm: float) -> float:
    """拉曼位移（cm⁻¹）→ 波长（nm）。750 cm⁻¹ @ 633 nm ≈ 664.55 nm（CP-A 的默认中心）。"""
    k = 1e7 / float(laser_nm) - float(shift_cm1)
    if k <= 0:
        raise ValueError(tr(f"位移 {shift_cm1} cm⁻¹ 超过激发线 {laser_nm} nm 能换算的范围",
                             f"shift {shift_cm1} cm⁻¹ is outside the range convertible with excitation line {laser_nm} nm"))
    return 1e7 / k


# ── 谱带强度 ──────────────────────────────────────────────────────────────────

def _finite_xy(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ok = np.isfinite(x) & np.isfinite(y)
    return x[ok], y[ok]


def band_metrics(x: Any, y: Any, band: Band, *,
                 baselines: Sequence[tuple[float, float]] | None = None,
                 edge: float = 20.0) -> dict:
    """谱带在线性基线之上的净强度。

    基线：``baselines`` 给的窗口（不给 = 谱带两侧各 ``edge``）里的点做一次线性拟合；
    窗口里点不够两个时退回两端点的中值常数基线，``baseline_sigma`` 记 None（SNR 也就没有）。

    返回 ``{valid, area, area_x, height, peak_x, baseline_sigma, snr, n_px}``；
    ``area`` = 净计数之和（与像素间距无关，跨像素尺度可比），``area_x`` = 对横轴的梯形积分。
    谱带落在横轴外（换了中心波长而谱带没跟着改）⇒ ``valid=False`` 并说明，不返回 0。
    """
    xa, ya = _finite_xy(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
    if xa.size < 3:
        return {"valid": False, "reason": "spectrum has no finite axis"}
    order = np.argsort(xa)
    xa, ya = xa[order], ya[order]
    in_band = (xa >= band.lo) & (xa <= band.hi)
    n = int(in_band.sum())
    if n < 2:
        return {"valid": False, "n_px": n,
                "reason": (tr(f"谱带 {band.name} [{band.lo:g}, {band.hi:g}] 不在谱的横轴 "
                           f"[{xa[0]:.1f}, {xa[-1]:.1f}] 内（中心波长 / 激发线对吗？）",
                               f"band {band.name} [{band.lo:g}, {band.hi:g}] is not within the spectrum axis [{xa[0]:.1f}, {xa[-1]:.1f}] (are the center wavelength / excitation line right?)"))}
    wins = list(baselines) if baselines else [(band.lo - edge, band.lo), (band.hi, band.hi + edge)]
    in_base = np.zeros_like(in_band)
    for lo, hi in wins:
        in_base |= (xa >= lo) & (xa <= hi)
    in_base &= ~in_band
    sigma: float | None = None
    if int(in_base.sum()) >= 2:
        coef = np.polyfit(xa[in_base], ya[in_base], 1)
        base = np.polyval(coef, xa[in_band])
        resid = ya[in_base] - np.polyval(coef, xa[in_base])
        sigma = float(np.std(resid, ddof=1)) if resid.size > 2 else None
    else:
        idx = np.flatnonzero(in_band)
        base = np.full(n, float(np.median([ya[idx[0]], ya[idx[-1]]])))
    net = ya[in_band] - base
    area = float(np.sum(net))
    area_x = float(np.trapezoid(net, xa[in_band])) if hasattr(np, "trapezoid") else float(
        np.trapz(net, xa[in_band]))
    k = int(np.argmax(net))
    snr = (area / (sigma * math.sqrt(n))) if sigma and sigma > 0 else None
    return {"valid": True, "area": area, "area_x": area_x, "height": float(net[k]),
            "peak_x": float(xa[in_band][k]), "baseline_sigma": sigma,
            "snr": snr, "n_px": n}


def total_counts(y: Any) -> float:
    """「没给谱带」时的强度：整条谱的计数之和（只计有限值）。"""
    a = np.asarray(y, dtype=np.float64)
    return float(np.nansum(a))


# ── 质检 ──────────────────────────────────────────────────────────────────────

def find_cosmic(y: Any, *, k: float = 8.0, width: int = 5, max_spike_px: int = 3) -> list[int]:
    """单帧里的窄正尖峰（宇宙射线）位置。只用于**标记**（CP-A ``cosmic_filter=flag``）。

    残差 = y − 滑动中值（窗 ``width``）；阈值 = max(k·MAD, 5·√基线) —— 后一项是计数的散粒噪声
    下限，没有它一条平坦的低计数谱会把泊松起伏当成尖峰。连续超过阈值的像素数 ≤ ``max_spike_px``
    才算尖峰（拉曼峰再窄也有好几个像素）。"""
    a = np.asarray(y, dtype=np.float64)
    if a.size < width + 2 or not np.isfinite(a).all():
        return []
    half = width // 2
    padded = np.pad(a, half, mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(padded, width)
    med = np.median(win, axis=1)
    r = a - med
    mad = float(np.median(np.abs(r - np.median(r)))) * 1.4826
    floor = 5.0 * np.sqrt(np.clip(med, 1.0, None))
    thr = np.maximum(k * mad, floor)
    hot = r > thr
    out: list[int] = []
    i = 0
    while i < a.size:
        if hot[i]:
            j = i
            while j + 1 < a.size and hot[j + 1]:
                j += 1
            if j - i + 1 <= max_spike_px:
                out.append(int(i + np.argmax(r[i:j + 1])))
            i = j + 1
        else:
            i += 1
    return out


def is_saturated(frames: Any, frac: float, *, full_scale: float = FULL_SCALE_COUNTS) -> bool:
    """任一帧的最大值 ≥ ``frac`` × 满量程 ⇒ 饱和（只标记，不自动改曝光）。"""
    a = np.asarray(frames, dtype=np.float64)
    if a.size == 0 or not np.isfinite(a).any():
        return False
    return bool(np.nanmax(a) >= float(frac) * full_scale)


def combine_frames(frames: Any, mode: str) -> tuple[np.ndarray, dict]:
    """多帧 → 一条谱。``median_frames`` 且帧数 ≥ 3 时逐像素取中值（去宇宙射线），否则取均值。

    返回 ``(spectrum, info)``，``info`` 说清用的是哪一种（帧数不够 3 时如实写 fallback）。"""
    a = np.atleast_2d(np.asarray(frames, dtype=np.float64))
    if mode == "median_frames" and a.shape[0] >= 3:
        return np.nanmedian(a, axis=0), {"combine": "median", "n_frames": int(a.shape[0])}
    info = {"combine": "mean", "n_frames": int(a.shape[0])}
    if mode == "median_frames":
        info["note"] = tr("median_frames 需要 ≥ 3 帧，帧数不够，按均值合并",
                           "median_frames needs ≥ 3 frames; not enough frames, merged by mean instead")
    return np.nanmean(a, axis=0), info


# ── 模式级分析 ─────────────────────────────────────────────────────────────────

def contrast(i_nf: float | None, i_ff: float | None, sigma_ff: float | None,
             n_sigma: float) -> dict:
    """近场/远场对比度 C = I_NF / I_FF − 1（M04）。

    远场低于 ``n_sigma`` × σ_FF ⇒ 只报下限 C_min = I_NF / (n σ_FF) − 1（M04.1）。
    任一读数缺失 ⇒ ``valid=False``，不编一个数。"""
    if i_nf is None or i_ff is None or not math.isfinite(i_nf) or not math.isfinite(i_ff):
        return {"valid": False, "reason": "missing NF or FF intensity"}
    noise = (n_sigma * sigma_ff) if (sigma_ff is not None and sigma_ff > 0) else None
    if noise is not None and i_ff < noise:
        return {"valid": True, "lower_bound": True, "C": i_nf / noise - 1.0,
                "ff_below_noise": True, "noise_level": noise}
    if i_ff <= 0:
        return {"valid": False, "reason": "FF intensity ≤ 0 and no noise estimate"}
    return {"valid": True, "lower_bound": False, "C": i_nf / i_ff - 1.0,
            "ff_below_noise": False, "noise_level": noise}


def band_correlation(x: Any, y_ref: Any, y: Any, bands: Iterable[Band]) -> float | None:
    """两条谱在谱带窗口并集内的皮尔逊相关（M02 参考像素：针尖有没有变）。

    没给谱带 ⇒ 用整条谱。点数不够或任一条是常数 ⇒ None（判不了，不是「相关为 0」）。"""
    xa = np.asarray(x, dtype=np.float64)
    a = np.asarray(y_ref, dtype=np.float64)
    b = np.asarray(y, dtype=np.float64)
    if not (xa.shape == a.shape == b.shape):
        return None
    mask = np.isfinite(xa) & np.isfinite(a) & np.isfinite(b)
    bl = list(bands)
    if bl:
        sel = np.zeros_like(mask)
        for band in bl:
            sel |= (xa >= band.lo) & (xa <= band.hi)
        mask &= sel
    if int(mask.sum()) < 3:
        return None
    a, b = a[mask], b[mask]
    if float(np.std(a)) == 0.0 or float(np.std(b)) == 0.0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def fit_cos2(theta_deg: Sequence[float], intensity: Sequence[float], *,
             model: str = "cos2_offset") -> dict:
    """I(θ) = A·cos²(θ − θ0) + B 的线性最小二乘（M08）。

    cos²(θ−θ0) = ½ + ½cos(2θ−2θ0) ⇒ I = c0 + c1·cos2θ + c2·sin2θ，三参数线性。
    ``cos2`` 模型强制 B = 0（只适合纯 Malus 型）。返回 θ0（°，[0,180)）、A、B、消光比
    I_min/I_max 与残差；点数不够 ⇒ ``valid=False``。"""
    t = np.radians(np.asarray(theta_deg, dtype=np.float64))
    y = np.asarray(intensity, dtype=np.float64)
    ok = np.isfinite(t) & np.isfinite(y)
    t, y = t[ok], y[ok]
    if model == "none":
        return {"valid": False, "reason": "fit_model = none"}
    need = 3
    if t.size < need:
        return {"valid": False, "reason": f"need ≥ {need} angles, have {t.size}"}
    m = np.column_stack([np.ones_like(t), np.cos(2 * t), np.sin(2 * t)])
    coef, *_ = np.linalg.lstsq(m, y, rcond=None)
    c0, c1, c2 = (float(v) for v in coef)
    amp_half = math.hypot(c1, c2)
    a = 2.0 * amp_half
    b = c0 - amp_half
    theta0 = (math.degrees(0.5 * math.atan2(c2, c1))) % 180.0
    if model == "cos2":
        # B = 0：只拟合 A 与 θ0（θ0 沿用三参数解的相位，A 用投影重估）。
        basis = np.cos(t - math.radians(theta0)) ** 2
        denom = float(np.dot(basis, basis))
        a = float(np.dot(basis, y) / denom) if denom > 0 else float("nan")
        b = 0.0
    pred = b + a * np.cos(t - math.radians(theta0)) ** 2
    rms = float(np.sqrt(np.mean((y - pred) ** 2)))
    imax, imin = b + max(a, 0.0), b + min(a, 0.0)
    ext = (imin / imax) if imax not in (0.0,) and math.isfinite(imax) else None
    return {"valid": True, "model": model, "A": a, "B": b, "theta0_deg": theta0,
            "extinction_ratio": ext, "rms": rms, "n": int(t.size)}


def fit_kappa_per_nm(current_a: Sequence[float], z_m: Sequence[float]) -> dict:
    """ln|I| 对 z 的线性拟合 → 表观衰减常数 κ（nm⁻¹）：I ∝ exp(−2κ·gap)（M07 / M05）。

    恒流反馈下设定点升高 ⇒ 针尖变近 ⇒ z 变小（本仓约定负方向朝样品）：ln I = a − 2κ·z。
    返回 κ、截距、R²；点数不够、电流非正 ⇒ ``valid=False``。"""
    i = np.abs(np.asarray(current_a, dtype=np.float64))
    z = np.asarray(z_m, dtype=np.float64)
    ok = np.isfinite(i) & np.isfinite(z) & (i > 0)
    i, z = i[ok], z[ok]
    if i.size < 2 or float(np.ptp(z)) == 0.0:
        return {"valid": False, "reason": "need ≥ 2 points with distinct z and I > 0"}
    zn = z * 1e9
    slope, intercept = np.polyfit(zn, np.log(i), 1)
    pred = slope * zn + intercept
    ss_res = float(np.sum((np.log(i) - pred) ** 2))
    ss_tot = float(np.sum((np.log(i) - np.mean(np.log(i))) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
    return {"valid": True, "kappa_per_nm": float(-slope / 2.0), "intercept": float(intercept),
            "r2": r2, "n": int(i.size)}


class DropDetector:
    """信号下降监视（M09 / M10）：连续 ``n`` 帧低于滚动中位数 × (1 − frac) ⇒ 下降。

    ``frac <= 0`` 关闭。滚动中位数只用**没被判成下降**的帧更新，免得一段持续下降把中位数
    拖下来、自己把自己判没了。"""

    def __init__(self, frac: float, n: int, *, history: int = 21) -> None:
        self.frac = float(frac)
        self.n = max(1, int(n))
        self._hist: list[float] = []
        self._history = max(3, int(history))
        self._low = 0
        self.flagged = False
        self.flagged_at: int | None = None
        self._count = 0

    @property
    def enabled(self) -> bool:
        return self.frac > 0

    def update(self, value: float | None) -> bool:
        """喂一帧的谱带强度，返回这一帧之后是否处于「已下降」状态。"""
        self._count += 1
        if not self.enabled or value is None or not math.isfinite(value):
            return self.flagged
        if len(self._hist) >= 3:
            med = float(np.median(self._hist))
            if med > 0 and value < med * (1.0 - self.frac):
                self._low += 1
                if self._low >= self.n and not self.flagged:
                    self.flagged = True
                    self.flagged_at = self._count
                return self.flagged
        self._low = 0
        self._hist.append(float(value))
        if len(self._hist) > self._history:
            self._hist.pop(0)
        return self.flagged


def peak_of_grid(values: Any, axis1: Sequence[float], axis2: Sequence[float] | None) -> dict:
    """栅格上目标函数的极大值位置（M12）：argmax，加一条三点抛物线细化（每轴各一次）。

    ``values`` 形状 (n2, n1)（一维时 n2 = 1）；NaN 跳过。细化落到栅格外就退回网格点。"""
    v = np.atleast_2d(np.asarray(values, dtype=np.float64))
    if not np.isfinite(v).any():
        return {"valid": False, "reason": "no finite objective value"}
    j2, j1 = np.unravel_index(int(np.nanargmax(v)), v.shape)
    a1 = np.asarray(axis1, dtype=np.float64)
    a2 = np.asarray(axis2, dtype=np.float64) if axis2 is not None else np.zeros(1)

    def _refine(line: np.ndarray, axis: np.ndarray, k: int) -> float:
        if axis.size < 3 or k == 0 or k == axis.size - 1:
            return float(axis[k])
        y0, y1, y2 = line[k - 1], line[k], line[k + 1]
        if not (np.isfinite(y0) and np.isfinite(y1) and np.isfinite(y2)):
            return float(axis[k])
        denom = y0 - 2 * y1 + y2
        if denom >= 0:
            return float(axis[k])
        off = 0.5 * (y0 - y2) / denom
        step = float(axis[k + 1] - axis[k])
        return float(axis[k] + max(-1.0, min(1.0, off)) * step)

    best1 = _refine(v[j2, :], a1, int(j1))
    best2 = _refine(v[:, j1], a2, int(j2)) if v.shape[0] > 1 else float(a2[0])
    return {"valid": True, "index": [int(j1), int(j2)], "value": float(v[j2, j1]),
            "axis1_best": best1, "axis2_best": best2 if axis2 is not None else None,
            "grid_axis1": float(a1[j1]), "grid_axis2": float(a2[j2]) if axis2 is not None else None}
