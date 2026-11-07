# -*- coding: utf-8 -*-
"""``RegisterDriftByMolecules`` —— 用分子极大值配准量漂移(比整帧互相关更强的漂移追踪)。

设计:``docs/v2/design/id23_site_marking_20261026.md``。算法取自分子配准的漂移追踪做法
(``register / drift_velocity / target``);数值细节(分箱、容差、迭代次数、门限)按参考做法给的数字,
参考做法没规定的地方在下面「偏差」里逐条写明。

## 为什么按分子配准,不用整帧互相关

``TrackDrift_ReferenceScan`` 是整像素的整帧互相关,而且任何异常都 ``return 0, 0``(量不出来 = 没漂移)。
分子配准问的是另一个问题:**参考帧上的每个分子,现在在哪**。于是

* 得到的是亚像素的平移,还带着「多少分子对上了」(``quality``)和残差(``rms_nm``)——量不出来就
  明说量不出来(``trustworthy=False``),不折成零漂移;
* 能给出**局部**位移(某个位点附近的分子各自挪了多远),压电蠕变 / 非线性造成的位置相关漂移
  不会被一个全局平移抹平;
* 与扫描角无关:配准发生在帧自己的轴上,是一个平移;角度只在最后把平移换算成扫描器坐标时进来。

## 配准(``register``,纯数值)

1. 新帧分子 ``Mn``、参考分子 ``REF``;所有成对偏移 ``Mn[i] − REF[j]``;
2. ``histogram2d``,0.05 nm 一格,只看 ``prev_drift ± 0.8 nm``;峰所在的格 = 初值;
   匹配数太少(``n_matched < 3`` 或 ``quality < 0.5``)就把窗口放宽到 ``± 2.5 nm`` 再来一遍;
3. 迭代 4 次:匹配半径 0.15 nm、一对一,``Dc = mean(Mn[匹配] − REF[匹配])``;
4. 报告:``quality = 匹配数 / 应当看得到的参考分子数``、匹配残差的 rms、(两帧都是 .sxm 时)
   匹配分子的高度比。

漂移的**符号约定**(这里写死,避免各处各猜一遍;``MeasureFrameDrift`` 曾因此被读反):
``drift = 新帧里分子的位置 − 参考帧里同一分子的位置`` = **特征(样品)相对名义位置挪了多远**。
所以要打到同一个分子上,针尖目标 = 名义位置 + drift。

## 偏差(参考做法没规定 / 本实现的选择)

* **近平局取先验。** 摘要写「峰所在的格 = 初值」。完美晶格上,真峰和差一个晶格矢量的别名峰票数
  几乎相等(合成数据里 0.97–0.99),噪声就能把别名顶上去 —— 悄悄差一个分子。所以:票数在最高峰
  ``TIE_FRAC``(0.9)以内的峰一律算「分不出」,取离 ``prev_drift`` 最近的那个;最高峰领先超过
  一成时行为和摘要完全一致。这只是平局裁决,**不是**否决门:``competing_peak_ratio`` 把「还有
  别的峰在竞争」如实报出来,但不改 ``trustworthy``(未标定的阈值没有否决权)。
* 找峰前把直方图做了 3×3 格的盒式求和(格宽仍是 0.05 nm):噪声会把一个峰的票分在相邻 2×2 格里。
* ``expected`` = 位移之后可能出现在新帧里的参考分子数(视野放宽一个匹配半径,于是 matched ≤ expected
  是结构性的);摘要只说 ``matched/expected``,没说 expected 怎么数。新帧视野未知(只给了坐标)时取
  全部参考分子。
* ``trustworthy`` = ``quality ≥ 0.5`` 且 ``n_matched ≥ 3``。少于 3 对的平移不叫配准。
* 高度比 = 匹配分子在新帧 / 参考帧上的「高斯平滑后峰高(减帧中位数)」均值之比(针尖变钝时 < 1)。
  摘要只说了有这么一项,定义是我取的;只做提示,不进 ``trustworthy``。
* ``drift_velocity`` 的 1 nm/h 上限按**速度矢量的模**封顶(保方向),摘要没说是分量还是模;
  「最近 4 次」取的是最近 4 次**可信**配准(不是最近 4 次里挑可信的)。
* ``DriftTracker.displacement_for_site`` 的锚点分子里没配上的,用全局漂移补(而不是丢掉),
  这样 ``均值(当前位置) + anchor_offset`` 的偏移约定不被少数几个没配上的分子拉歪。

## 与 D(``MeasureSpectraAtSites``)的接口

本模块自带无状态的数值(``register``)、有状态的追踪器(``DriftTracker``:历史、速度外推、
连续弱配准计数、按位点算目标),以及技能壳 ``RegisterDriftByMolecules``。D 只负责排步骤。
追踪器的状态是纯 JSON(``to_dict / from_dict``),可以放进 ``partial_data`` 过检查点。
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.i18n import tr
from mast.skills.base import BaseSkill

logger = logging.getLogger(__name__)

_SKILL_NAME = "RegisterDriftByMolecules"

# ── 参考做法给的数字 ─────────────────────────────────────────────────────────────
BIN_NM = 0.05                 # 偏移直方图的格宽
WINDOW_NM = 0.8               # 先看 prev_drift ± 0.8 nm
WIDE_WINDOW_NM = 2.5          # 匹配太少再放宽到 ± 2.5 nm
MATCH_TOL_NM = 0.15           # 匹配半径
REFINE_ITERS = 4              # 迭代次数
QUALITY_MIN = 0.5             # 低于它就别信这次配准
VELOCITY_FIT_N = 4            # 线性拟合最近这么多次可信配准
VELOCITY_CAP_NM_PER_H = 1.0   # 漂移速度上限
VELOCITY_MIN_BASELINE_S = 600.0   # 时间跨度不到 10 分钟:速度算 0
WEAK_STOP_N = 3               # 连续这么多次弱配准 → 漂移丢失,停
LOCAL_RADIUS_NM = 3.0         # 局部位移:位点 3 nm 以内的分子
LOCAL_MIN_MOLECULES = 3       # ……至少这么多个,否则退回全局漂移

# ── 本实现选的数字(见模块说明「偏差」)────────────────────────────────────────────
MIN_MATCHED = 3               # 少于它的平移不叫配准
MIN_MOLECULES = 3             # 任一帧检出的分子少于它,不做配准
TIE_FRAC = 0.9                # 票数在最高峰这么多以内算分不出,取离先验最近的
PEAK_EXCLUDE_NM = 0.3         # 竞争峰:离选中峰至少这么远(= 两倍匹配半径)
AMBIGUOUS_RATIO = 0.9         # competing_peak_ratio 超过它就在 warnings 里点名(只提示)
_ANGLE_TOL_DEG = 0.05         # 两帧扫描角相差超过它,平移模型不成立
_MAX_HISTORY = 32             # 追踪器记住的配准条数(JSON 里)
_PAIR_CHUNK = 2_000_000       # 成对偏移分块,控制内存
MAX_VOTE_POINTS = 2500        # 投票阶段每个点集最多用这么多点(大视野上万个分子时成对偏移是 10^8 量级);
                              # 只影响找峰,匹配 / 迭代 / 质量仍用全部分子


def _now() -> float:
    """所有时间都从这里取 —— 测试替换它就能控制时钟(速度外推只看时间差)。"""
    return time.time()


# ═══════════════════════════════════════════════════════════════════════════
# 一、配准(纯数值)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Registration:
    """一次配准。``drift_nm`` 在**参考帧自己的轴**上(+y 向上),= 新 − 参考。

    ``pairs`` 是 ``(k, 2)`` 的下标,列为 (新分子下标, 参考分子下标);``rms_nm`` 没有匹配时是 None。
    """

    drift_nm: tuple[float, float]
    quality: float
    rms_nm: float | None
    n_matched: int
    n_expected: int
    n_new: int
    n_ref: int
    pairs: "np.ndarray"
    window_nm: float
    widened: bool
    peak_votes: float
    competing_peak_ratio: float | None
    trustworthy: bool
    reason: str               # trustworthy 时为 ""

    def asdict(self) -> dict:
        return {"drift_nm": [round(self.drift_nm[0], 4), round(self.drift_nm[1], 4)],
                "quality": round(self.quality, 4),
                "rms_nm": None if self.rms_nm is None else round(self.rms_nm, 4),
                "n_matched": self.n_matched, "n_expected": self.n_expected,
                "n_new": self.n_new, "n_ref": self.n_ref,
                "window_used_nm": self.window_nm, "widened": self.widened,
                "peak_votes": self.peak_votes,
                "competing_peak_ratio": (None if self.competing_peak_ratio is None
                                         else round(self.competing_peak_ratio, 3)),
                "trustworthy": self.trustworthy, "reason": self.reason}


@dataclass(frozen=True)
class _Pass:
    drift: "np.ndarray"
    pairs: "np.ndarray"
    quality: float
    rms: float | None
    expected: int
    votes: float
    competing: float | None


def _as_points(pts: Any) -> "np.ndarray":
    a = np.asarray(pts, dtype=float)
    if a.size == 0:
        return np.empty((0, 2))
    a = a.reshape(-1, 2)
    return a[np.all(np.isfinite(a), axis=1)]


def _offset_histogram(new: "np.ndarray", ref: "np.ndarray", center: "np.ndarray",
                      half: float, bin_nm: float):
    """成对偏移 ``new[i] − ref[j]`` 落在 ``center ± half`` 方窗里的二维直方图。

    返回 ``(H, cx, cy)``,``cx / cy`` 是各格中心。分块累计,大帧(上千分子)也不会一次吃掉内存。
    """
    nb = max(1, int(round(2.0 * half / bin_nm)))
    ex = center[0] - half + bin_nm * np.arange(nb + 1)
    ey = center[1] - half + bin_nm * np.arange(nb + 1)
    H = np.zeros((nb, nb))
    step = max(1, _PAIR_CHUNK // max(1, len(ref)))
    for a in range(0, len(new), step):
        d = (new[a:a + step, None, :] - ref[None, :, :]).reshape(-1, 2)
        m = (d[:, 0] >= ex[0]) & (d[:, 0] < ex[-1]) & (d[:, 1] >= ey[0]) & (d[:, 1] < ey[-1])
        if m.any():
            h, _, _ = np.histogram2d(d[m, 0], d[m, 1], bins=[ex, ey])
            H += h
    return H, ex[:-1] + bin_nm / 2.0, ey[:-1] + bin_nm / 2.0


def _box3(H: "np.ndarray") -> "np.ndarray":
    """3×3 盒式求和(零填充)。"""
    nb0, nb1 = H.shape
    P = np.pad(H, 1)
    return sum(P[i:i + nb0, j:j + nb1] for i in range(3) for j in range(3))


def _pick_peak(S: "np.ndarray", cx: "np.ndarray", cy: "np.ndarray", prior: "np.ndarray",
               ) -> tuple["np.ndarray", float, float | None]:
    """平滑直方图 ``S`` 里选峰 → ``(偏移, 票数, 竞争峰比)``。

    先找局部极大(5×5 格内的最大),票数在全局最高的 ``TIE_FRAC`` 以内的都算候选,取离 ``prior``
    最近的。领先超过一成的最高峰没有对手,行为就是「取最高峰」。
    """
    from scipy import ndimage as ndi

    X, Y = np.meshgrid(cx, cy, indexing="ij")
    top = float(S.max())
    if top <= 0.0:
        return prior.copy(), 0.0, None
    is_max = (S == ndi.maximum_filter(S, size=5, mode="constant")) & (S > 0)
    cand = is_max & (S >= TIE_FRAC * top)
    dist = np.hypot(X - prior[0], Y - prior[1])
    # 并列取离先验最近;票数相同的格里再按票数优先(同距时)。
    idx = np.argwhere(cand)
    best = min(idx, key=lambda ij: (dist[ij[0], ij[1]], -S[ij[0], ij[1]]))
    i, j = int(best[0]), int(best[1])
    votes = float(S[i, j])
    far = np.hypot(X - X[i, j], Y - Y[i, j]) > PEAK_EXCLUDE_NM
    rival = float(S[far].max()) if far.any() else 0.0
    return np.array([X[i, j], Y[i, j]]), votes, (rival / votes if votes > 0 else None)


def _match(new: "np.ndarray", ref: "np.ndarray", tree, drift: "np.ndarray", tol: float,
           ) -> "np.ndarray":
    """一对一匹配:``new[i] − drift`` 与 ``ref[j]`` 相距 < tol,近者先得。返回 ``(k, 2)`` 下标。"""
    if len(new) == 0 or len(ref) == 0:
        return np.empty((0, 2), dtype=int)
    d, j = tree.query(new - drift, k=1, distance_upper_bound=tol)
    ok = np.nonzero(np.isfinite(d))[0]
    if ok.size == 0:
        return np.empty((0, 2), dtype=int)
    taken: dict[int, int] = {}
    for i in ok[np.argsort(d[ok], kind="stable")]:
        taken.setdefault(int(j[i]), int(i))
    pairs = np.array([(i, j_) for j_, i in taken.items()], dtype=int).reshape(-1, 2)
    return pairs[np.argsort(pairs[:, 0], kind="stable")]


def _expected(ref: "np.ndarray", drift: "np.ndarray",
              field: tuple[float, float, float, float] | None, tol: float) -> int:
    """位移后「可能」出现在新帧里的参考分子数。``field = (cx, cy, w, h)``,参考帧的轴上。

    视野放宽一个匹配半径:新帧里的分子都在视野内,与它配上的参考分子位移后最多落在视野外 ``tol``。
    这样每一对匹配的参考分子都被数进来,``n_matched ≤ n_expected`` 是结构性的(不会打印出
    「匹配 178 / 应有 161」这种话)。"""
    if field is None:
        return len(ref)
    cx, cy, w, h = field
    p = ref + drift
    inside = (np.abs(p[:, 0] - cx) <= w / 2.0 + tol) & (np.abs(p[:, 1] - cy) <= h / 2.0 + tol)
    return int(inside.sum())


def _thin(pts: "np.ndarray") -> "np.ndarray":
    """点数超过 ``MAX_VOTE_POINTS`` 时等间隔抽样(检测顺序是行优先,抽出来空间上仍是均匀的)。"""
    if len(pts) <= MAX_VOTE_POINTS:
        return pts
    return pts[np.linspace(0, len(pts) - 1, MAX_VOTE_POINTS).astype(int)]


def _one_pass(new, ref, tree, prev, half, *, bin_nm, tol, n_iter, field) -> _Pass | None:
    H, cx, cy = _offset_histogram(_thin(new), _thin(ref), prev, half, bin_nm)
    if H.sum() <= 0:
        return None                      # 窗口里一对偏移都没有
    D, votes, competing = _pick_peak(_box3(H), cx, cy, prev)
    for _ in range(n_iter):
        pairs = _match(new, ref, tree, D, tol)
        if len(pairs) == 0:
            break
        D = (new[pairs[:, 0]] - ref[pairs[:, 1]]).mean(axis=0)
    pairs = _match(new, ref, tree, D, tol)
    exp = _expected(ref, D, field, tol)
    if len(pairs):
        res = new[pairs[:, 0]] - ref[pairs[:, 1]] - D
        rms = float(np.sqrt(np.mean(np.sum(res * res, axis=1))))
    else:
        rms = None
    quality = min(1.0, len(pairs) / exp) if exp > 0 else 0.0
    return _Pass(D, pairs, float(quality), rms, exp, votes, competing)


def register(new_pts: Any, ref_pts: Any, prev_drift: Sequence[float] = (0.0, 0.0), *,
             field: tuple[float, float, float, float] | None = None,
             window_nm: float = WINDOW_NM, wide_window_nm: float = WIDE_WINDOW_NM,
             match_tol_nm: float = MATCH_TOL_NM, bin_nm: float = BIN_NM,
             n_iter: int = REFINE_ITERS, min_quality: float = QUALITY_MIN,
             min_matched: int = MIN_MATCHED) -> Registration:
    """新帧分子 ``new_pts`` 相对参考分子 ``ref_pts`` 的平移(两者都在**同一套轴**上,nm)。

    ``field = (cx, cy, w, h)`` 是新帧视野在这套轴上的位置,用来数 ``expected``;不给就取全部参考
    分子。``prev_drift`` 是搜索窗口的中心(上一次的漂移,或外推值)。
    """
    from scipy.spatial import cKDTree

    new, ref = _as_points(new_pts), _as_points(ref_pts)
    prev = np.asarray(prev_drift, dtype=float).reshape(2)
    kw = dict(bin_nm=bin_nm, tol=match_tol_nm, n_iter=n_iter, field=field)

    def _result(p: _Pass | None, win: float, widened: bool) -> Registration:
        if p is None:
            return Registration((float(prev[0]), float(prev[1])), 0.0, None, 0, 0, len(new),
                                len(ref), np.empty((0, 2), dtype=int), win, widened, 0.0, None,
                                False, "no_overlap")
        ok = p.quality >= min_quality and len(p.pairs) >= min_matched
        reason = ("" if ok else "few_matches" if len(p.pairs) < min_matched else "low_quality")
        return Registration((float(p.drift[0]), float(p.drift[1])), p.quality, p.rms,
                            int(len(p.pairs)), p.expected, len(new), len(ref), p.pairs, win,
                            widened, p.votes, p.competing, ok, reason)

    if len(new) == 0 or len(ref) == 0:
        return _result(None, window_nm, False)
    tree = cKDTree(ref)
    narrow = _one_pass(new, ref, tree, prev, window_nm, **kw)
    best, win, widened = narrow, window_nm, False
    narrow_ok = (narrow is not None and narrow.quality >= min_quality
                 and len(narrow.pairs) >= min_matched)
    if not narrow_ok and wide_window_nm > window_nm + 1e-9:
        wide = _one_pass(new, ref, tree, prev, wide_window_nm, **kw)
        if wide is not None and (narrow is None
                                 or (wide.quality, len(wide.pairs)) > (narrow.quality,
                                                                       len(narrow.pairs))):
            best, win, widened = wide, wide_window_nm, True
    return _result(best, win, widened)


# ═══════════════════════════════════════════════════════════════════════════
# 二、漂移速度与外推
# ═══════════════════════════════════════════════════════════════════════════

def _is_good(rec: dict, min_quality: float) -> bool:
    try:
        return (bool(rec.get("trustworthy", True)) and float(rec["quality"]) >= min_quality
                and math.isfinite(float(rec["t"]))
                and all(math.isfinite(float(v)) for v in rec["drift_nm"]))
    except (KeyError, TypeError, ValueError):
        return False


def drift_velocity(history: Sequence[dict], *, n_fit: int = VELOCITY_FIT_N,
                   min_quality: float = QUALITY_MIN, cap_nm_per_h: float = VELOCITY_CAP_NM_PER_H,
                   min_baseline_s: float = VELOCITY_MIN_BASELINE_S) -> tuple[float, float]:
    """漂移速度 ``(vx, vy)``,**nm/h**。

    ``history`` 是按时间排好的配准记录,每条至少有 ``t``(秒)、``drift_nm``、``quality``。
    取最近 ``n_fit`` 条 quality ≥ ``min_quality`` 的,``np.polyfit`` 一次拟合;时间跨度不到
    ``min_baseline_s`` 或不足两条返回 (0, 0);速度的模超过 ``cap_nm_per_h`` 就按原方向压到上限。
    """
    good = [r for r in history if _is_good(r, min_quality)][-max(2, int(n_fit)):]
    if len(good) < 2:
        return (0.0, 0.0)
    t = np.array([float(r["t"]) for r in good])
    if t[-1] - t[0] < min_baseline_s:
        return (0.0, 0.0)
    t = t - t[0]
    v = np.array([np.polyfit(t, [float(r["drift_nm"][k]) for r in good], 1)[0]
                  for k in (0, 1)]) * 3600.0
    speed = float(np.hypot(v[0], v[1]))
    if speed > cap_nm_per_h > 0:
        v = v * (cap_nm_per_h / speed)
    return (float(v[0]), float(v[1]))


# ═══════════════════════════════════════════════════════════════════════════
# 三、帧 → 分子(复用 ReadSiteMarks 的检测)
# ═══════════════════════════════════════════════════════════════════════════

def frame_delta_to_scanner_m(dx_nm: float, dy_nm: float, angle_deg: float) -> tuple[float, float]:
    """帧轴上的一个位移(nm)→ 扫描器轴上的位移(m)。

    经 ``ReadSiteMarks.frame_nm_to_scanner_m``(也就是全仓唯一的 ``px_to_m``)算,符号约定只有
    那一处;用一个零心 1 nm 的虚拟帧,于是只剩旋转,没有平移。θ=0 时就是 ``(dx, dy)·1e-9``。
    """
    from mast.skills.builtins.site_marking_read import FrameGeometry, frame_nm_to_scanner_m

    geo = FrameGeometry(0.0, 0.0, 1.0, 1.0, float(angle_deg), True, "drift_delta")
    return frame_nm_to_scanner_m(float(dx_nm), float(dy_nm), geo)


def scanner_delta_to_frame(dx_nm: float, dy_nm: float, angle_deg: float) -> tuple[float, float]:
    """``frame_delta_to_scanner_m`` 的逆(扫描器轴 → 帧轴),用来把两帧中心的差换到帧轴上。

    **不手写旋转**:前向换算的符号约定住在 ``px_to_m``,而它的符号曾被订正过(正角 = 顺时针)。这里取前向换算对两个基向量的像,拼出旋转矩阵 F,逆就是转置(F 是正交的)——
    于是约定再变,这里自动跟着变,不会和前向换算各转各的。
    """
    s = 1e9
    fx, fy = (v * s for v in frame_delta_to_scanner_m(1.0, 0.0, angle_deg))   # F 的第一列
    gx, gy = (v * s for v in frame_delta_to_scanner_m(0.0, 1.0, angle_deg))   # F 的第二列
    return (fx * dx_nm + fy * dy_nm, gx * dx_nm + gy * dy_nm)


@dataclass
class FrameMolecules:
    """一帧里的分子。``geo`` 是 ``FrameGeometry``(读不到几何时 None),坐标是帧内 nm。"""

    pts: "np.ndarray"
    geo: Any = None
    z: Any = None
    nm_per_px: float | None = None
    scan_dir: str = ""
    bias_v: float | None = None
    error: str = ""


def molecules_in_frame(frame_path: str) -> FrameMolecules:
    """读一个 .sxm → 拉平 → 检分子。**与 ReadSiteMarks 同一条检测**(同一个函数、同一份 profile),
    所以分子的下标和它给的 ``anchor`` 对得上。永不抛异常。"""
    from mast.skills.builtins.site_marking_read import DEFAULT_PROFILE, _load_frame, detect_molecules

    fd = _load_frame(str(frame_path), need_pixels=True)
    if fd.error or fd.z is None or fd.geo is None:
        return FrameMolecules(np.empty((0, 2)), geo=fd.geo, error=fd.error or tr(
            "没有可用的像素", "no usable pixels"))
    try:
        pts = detect_molecules(fd.z, nm_per_px=fd.nm_per_px, w_nm=fd.geo.w_nm, h_nm=fd.geo.h_nm,
                               profile=DEFAULT_PROFILE)
    except Exception as exc:  # noqa: BLE001 — 检测坏了按"读不了"报,不抛
        return FrameMolecules(np.empty((0, 2)), geo=fd.geo, error=f"{type(exc).__name__}: {exc}")
    scan_dir, bias_v = "", None
    try:                                  # 只读文件头,不再整帧读第二遍
        from mast.io.nanonis_files import read_sxm_header

        h = read_sxm_header(str(frame_path))
        scan_dir = str(h.get("scan_dir", "")).strip().lower()
        bias_v = float(str(h.get("bias", "")).split()[0])
    except Exception:  # noqa: BLE001 — 这两项只是提示
        pass
    return FrameMolecules(pts, geo=fd.geo, z=fd.z, nm_per_px=fd.nm_per_px,
                          scan_dir=scan_dir, bias_v=bias_v)


def _heights(z: Any, pts: "np.ndarray", geo: Any, nm_per_px: float) -> "np.ndarray":
    """分子位置处「高斯平滑后的高度 − 帧中位数」。位置是帧内 nm,按 px_to_m 的逆换成像素。"""
    from scipy import ndimage as ndi

    from mast.skills.builtins.site_marking_read import DEFAULT_PROFILE

    a = np.asarray(z, dtype=float)
    ny, nx = a.shape
    s = ndi.gaussian_filter(a, max(0.5, DEFAULT_PROFILE.sigma_nm / nm_per_px))
    col = (pts[:, 0] + geo.w_nm / 2.0) / geo.w_nm * nx - 0.5
    row = (geo.h_nm / 2.0 - pts[:, 1]) / geo.h_nm * ny - 0.5
    h = ndi.map_coordinates(s, [row, col], order=1, mode="nearest")
    return h - float(np.median(s))


def height_ratio(new: FrameMolecules, ref: FrameMolecules, pairs: "np.ndarray") -> float | None:
    """匹配分子的平均高度,新 / 参考。需要两帧都有像素和几何,且参考高度为正。"""
    if len(pairs) == 0 or new.z is None or ref.z is None or new.geo is None or ref.geo is None:
        return None
    try:
        hn = _heights(new.z, new.pts[pairs[:, 0]], new.geo, new.nm_per_px).mean()
        hr = _heights(ref.z, ref.pts[pairs[:, 1]], ref.geo, ref.nm_per_px).mean()
    except Exception:  # noqa: BLE001 — 提示项,算不出就不给
        return None
    return float(hn / hr) if hr > 0 and math.isfinite(hn / hr) else None


# ═══════════════════════════════════════════════════════════════════════════
# 四、追踪器(状态 + 门限 + 外推 + 按位点算目标)
# ═══════════════════════════════════════════════════════════════════════════

#: 吸附到分子上的位点种类 —— 它们的目标用锚点分子的当前位置。
_ANCHORED_KINDS = ("mol", "hbond", "ring")


class DriftTracker:
    """一次测量任务里的漂移状态。纯 JSON 可序列化(``to_dict / from_dict``)。

    * ``note_registration`` 收一次 ``RegisterDriftByMolecules`` 的结果;可信就清零弱计数,
      否则 +1。``note_failure`` 收「根本没配出来」(扫描失败 / 没找到帧 / 技能报错),同样 +1。
    * ``lost``:连续弱配准达到 ``weak_limit``(0 = 永不判丢)。
    * ``predict(t_now)``:当前漂移估计 = 最近一次**可信**配准 + 速度 × 经过的时间。最近一次配准
      不可信时用的就是外推(摘要:「quality < 0.5 就用外推漂移,不信这次配准」)。
    * ``displacement_for_site``:按位点算目标位移,见方法说明。
    """

    def __init__(self, *, min_quality: float = QUALITY_MIN, weak_limit: int = WEAK_STOP_N) -> None:
        self.min_quality = float(min_quality)
        self.weak_limit = int(weak_limit)
        self.history: list[dict] = []
        self.last_good: dict | None = None
        self.weak_streak = 0
        self.n_registrations = 0
        self.n_trusted = 0
        self.n_failed = 0
        #: 最近一次尝试(配准或失败)是不是可信的 —— predict 的 source 用
        self.latest_ok = False

    # ── 收数据 ──────────────────────────────────────────────────────────────

    def note_registration(self, reg: dict, t_mid: float) -> bool:
        """收一次配准结果(技能的 ``data``)。返回它是否可信。"""
        try:
            drift = [float(reg["drift_nm"][0]), float(reg["drift_nm"][1])]
            quality = float(reg["quality"])
        except (KeyError, TypeError, ValueError, IndexError):
            self.note_failure("unparseable registration", t_mid)
            return False
        matches = reg.get("matches") or []
        trusted = (bool(reg.get("trustworthy", True)) and quality >= self.min_quality
                   and int(reg.get("n_matched", len(matches))) >= MIN_MATCHED)
        rec = {"t": float(t_mid), "drift_nm": [round(drift[0], 4), round(drift[1], 4)],
               "quality": round(quality, 4), "rms_nm": reg.get("rms_nm"),
               "n_matched": int(reg.get("n_matched", len(matches))), "trustworthy": trusted,
               "reason": str(reg.get("reason") or "")}
        self._push(rec)
        self.n_registrations += 1
        self.latest_ok = trusted
        if trusted:
            self.n_trusted += 1
            self.weak_streak = 0
            self.last_good = {**rec, "matches": [[float(v) for v in m] for m in matches]}
        else:
            self.weak_streak += 1
        return trusted

    def note_failure(self, reason: str, t: float) -> None:
        """这一轮根本没有配准结果。"""
        self._push({"t": float(t), "drift_nm": None, "quality": 0.0, "trustworthy": False,
                    "n_matched": 0, "reason": str(reason)[:120]})
        self.n_failed += 1
        self.latest_ok = False
        self.weak_streak += 1

    def _push(self, rec: dict) -> None:
        self.history.append(rec)
        if len(self.history) > _MAX_HISTORY:
            del self.history[:len(self.history) - _MAX_HISTORY]

    # ── 状态 ────────────────────────────────────────────────────────────────

    @property
    def lost(self) -> bool:
        return self.weak_limit > 0 and self.weak_streak >= self.weak_limit

    def velocity_nm_per_h(self) -> tuple[float, float]:
        return drift_velocity([r for r in self.history if r.get("drift_nm") is not None],
                              min_quality=self.min_quality)

    def predict(self, t_now: float) -> dict:
        """``t_now`` 时的漂移估计(帧轴,nm)。没有任何可信配准时是 0,``source='none'``。"""
        lg = self.last_good
        if lg is None:
            return {"drift_nm": [0.0, 0.0], "source": "none", "age_s": None,
                    "velocity_nm_per_h": [0.0, 0.0]}
        vx, vy = self.velocity_nm_per_h()
        dt = float(t_now) - lg["t"]
        return {"drift_nm": [lg["drift_nm"][0] + vx / 3600.0 * dt,
                             lg["drift_nm"][1] + vy / 3600.0 * dt],
                "source": "registered" if self.latest_ok else "extrapolated",
                "age_s": dt, "velocity_nm_per_h": [vx, vy]}

    def displacement_for_site(self, site: dict, t_now: float) -> dict:
        """位点的目标位移 ``delta_nm``(帧轴上,目标 = 名义位置 + delta)。

        * 吸附到分子上的位点(``rkind``/``kind`` 为 mol / hbond / ring 且有 ``anchor``):锚点分子
          各自的位移的均值 —— 就是「锚点分子**当前**测得位置的均值 + anchor_offset」减去名义位置;
          锚点里没配上的,用全局漂移补。一个都没配上就退到下一档。
        * 其余:位点 ``LOCAL_RADIUS_NM`` 以内、配上的分子的位移的**中位数**(至少
          ``LOCAL_MIN_MOLECULES`` 个),否则全局漂移 D。
        * 最后加 ``速度 × (t_now − 这次配准的 t_mid)``。

        ``site``:``x_nm, y_nm``(名义位置,帧内)、``kind``、``anchor``(参考分子下标列表)。
        """
        lg = self.last_good
        if lg is None:
            return {"delta_nm": [0.0, 0.0], "mode": "none", "n_local": 0,
                    "drift_global_nm": [0.0, 0.0], "source": "none"}
        D = np.array(lg["drift_nm"], dtype=float)
        m = np.array(lg.get("matches") or [], dtype=float).reshape(-1, 5)
        disp = m[:, 3:5] - m[:, 1:3] if len(m) else np.empty((0, 2))
        kind = str(site.get("rkind") or site.get("kind") or "").lower()
        anchors = [int(a) for a in (site.get("anchor") or [])]
        delta, mode, n_local = D, "global", 0
        by_ref = {int(m[k, 0]): disp[k] for k in range(len(m))}
        if kind in _ANCHORED_KINDS and anchors and any(a in by_ref for a in anchors):
            delta = np.mean([by_ref.get(a, D) for a in anchors], axis=0)
            mode = "anchor"
        elif len(m):
            near = np.hypot(m[:, 1] - float(site.get("x_nm", 0.0)),
                            m[:, 2] - float(site.get("y_nm", 0.0))) <= LOCAL_RADIUS_NM
            n_local = int(near.sum())
            if n_local >= LOCAL_MIN_MOLECULES:
                delta, mode = np.median(disp[near], axis=0), "local"
        vx, vy = self.velocity_nm_per_h()
        dt = float(t_now) - lg["t"]
        delta = delta + np.array([vx, vy]) / 3600.0 * dt
        return {"delta_nm": [float(delta[0]), float(delta[1])], "mode": mode,
                "n_local": n_local, "drift_global_nm": [float(D[0]), float(D[1])],
                "source": "registered" if self.latest_ok else "extrapolated"}

    # ── 序列化(进 partial_data / 检查点)───────────────────────────────────

    def to_dict(self, include_matches: bool = True) -> dict:
        """纯 JSON 的状态。``include_matches=False`` 去掉最近一次可信配准里每个分子的位移(几百行)——
        放进检查点用这个;代价是从检查点恢复后,锚点 / 局部位移要等下一次配准才有,之前退到全局漂移。"""
        lg = self.last_good
        if lg is not None and not include_matches:
            lg = {**lg, "matches": []}
        return {"min_quality": self.min_quality, "weak_limit": self.weak_limit,
                "history": list(self.history), "last_good": lg,
                "weak_streak": self.weak_streak, "n_registrations": self.n_registrations,
                "n_trusted": self.n_trusted, "n_failed": self.n_failed,
                "latest_ok": self.latest_ok}

    @classmethod
    def from_dict(cls, d: dict | None) -> "DriftTracker":
        d = d or {}
        tr_ = cls(min_quality=d.get("min_quality", QUALITY_MIN),
                  weak_limit=d.get("weak_limit", WEAK_STOP_N))
        tr_.history = list(d.get("history") or [])
        tr_.last_good = d.get("last_good")
        tr_.weak_streak = int(d.get("weak_streak", 0))
        tr_.n_registrations = int(d.get("n_registrations", 0))
        tr_.n_trusted = int(d.get("n_trusted", 0))
        tr_.n_failed = int(d.get("n_failed", 0))
        tr_.latest_ok = bool(d.get("latest_ok", False))
        return tr_

    def summary(self) -> dict:
        """给结果 / 页面用的紧凑摘要(不含 matches)。"""
        vx, vy = self.velocity_nm_per_h()
        lg = self.last_good
        return {"n_registrations": self.n_registrations, "n_trusted": self.n_trusted,
                "n_failed": self.n_failed, "weak_streak": self.weak_streak, "lost": self.lost,
                "velocity_nm_per_h": [round(vx, 4), round(vy, 4)],
                "last_good": ({k: lg[k] for k in ("t", "drift_nm", "quality", "n_matched")}
                              if lg else None),
                "history": [{k: r.get(k) for k in ("t", "drift_nm", "quality", "n_matched",
                                                   "trustworthy", "reason")}
                            for r in self.history[-16:]]}


# ═══════════════════════════════════════════════════════════════════════════
# 五、找刚扫完的那一帧(D 用)
# ═══════════════════════════════════════════════════════════════════════════

def find_fresh_frame(context, since_ts: float, *, slack_s: float = 2.0,
                     wait_s: float = 0.0) -> str | None:
    """``since_ts`` 之后写出的最新 .sxm(Nanonis 自动保存的刚扫完的那一帧),没有就是 None。

    只读:什么都不存、不挪。Nanonis 自己挑目录和序号,要叫出刚写的那个文件只能「在扫完之后找最新的」
    (同 ``find_latest_saved`` / ``SaveScan``)。``wait_s`` > 0 时每 0.5 s 重找一次 —— 扫描一结束文件
    要过一会才落盘。找不到时由调用方决定要不要 ``SaveScan``(自动保存关着,或会话目录本机够不着)。
    """
    from mast.skills.builtins.scan_extra import _candidate_save_dirs, find_latest_saved

    deadline = _now() + max(0.0, float(wait_s))
    while True:
        try:
            roots = _candidate_save_dirs(context)
            age = max(1.0, _now() - float(since_ts) + slack_s)
            hit = find_latest_saved(roots, "*.sxm", max_age_s=age)
        except Exception as exc:  # noqa: BLE001 — 找不到就是找不到,别把追踪搞崩
            logger.warning("RegisterDriftByMolecules: 找刚扫完的帧失败：%s", exc)
            hit = None
        if hit is not None or _now() >= deadline:
            return str(hit) if hit is not None else None
        time.sleep(0.5)


# ═══════════════════════════════════════════════════════════════════════════
# 六、技能壳
# ═══════════════════════════════════════════════════════════════════════════

def _parse_points(raw: str, what: str) -> tuple["np.ndarray", "np.ndarray", str]:
    """JSON → ``(pts (m,2), ids (m,), error)``。

    接受 ``[[x, y], ...]``(下标 = 位置),也接受 ``ReadSiteMarks.anchor_molecules`` 的形状
    ``{"<下标>": {"x_nm": .., "y_nm": ..}}``(下标 = 键,于是 ``matches`` 里的参考下标和位点的
    ``anchor`` 在同一个下标空间里)。
    """
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return np.empty((0, 2)), np.empty(0, dtype=int), f"{what}: JSON {exc}"
    pts: list[tuple[float, float]] = []
    ids: list[int] = []
    try:
        if isinstance(doc, dict):
            for k in sorted(doc, key=lambda s: int(s)):
                v = doc[k]
                x = v["x_nm"] if isinstance(v, dict) else v[0]
                y = v["y_nm"] if isinstance(v, dict) else v[1]
                pts.append((float(x), float(y)))
                ids.append(int(k))
        elif isinstance(doc, list):
            for i, v in enumerate(doc):
                x = v["x_nm"] if isinstance(v, dict) else v[0]
                y = v["y_nm"] if isinstance(v, dict) else v[1]
                pts.append((float(x), float(y)))
                ids.append(i)
        else:
            return np.empty((0, 2)), np.empty(0, dtype=int), f"{what}: not a list/object"
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return np.empty((0, 2)), np.empty(0, dtype=int), f"{what}: {type(exc).__name__}: {exc}"
    a = np.array(pts, dtype=float).reshape(-1, 2)
    ok = np.all(np.isfinite(a), axis=1)
    return a[ok], np.array(ids, dtype=int)[ok], ""


class RegisterDriftByMolecules(BaseSkill):
    """新帧相对参考帧的分子配准 → 漂移(平移)、质量、残差。纯计算,不碰仪器。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=_SKILL_NAME,
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr(
                "用分子极大值配准量漂移：把新扫的一帧（frame_path）和参考帧（reference_frame_path，"
                "或一份参考分子坐标）上的分子逐个对上，给出平移 drift_nm（= 新 − 参考，即样品特征相对"
                "名义位置挪了多远；要打到同一个分子上，目标 = 名义位置 + drift）、匹配质量 quality"
                "（匹配数/应当看得到的分子数）、残差 rms_nm 和每个匹配分子的位移（matches，供按位点算局部"
                "位移）。trustworthy=false（quality<0.5 或匹配<3）时不要采信这次平移，改用外推漂移。"
                "只读：不碰仪器、不改任何文件。与扫描角无关（配准在帧自己的轴上），角度只用来把平移换"
                "算成扫描器坐标 drift_scanner_m。",
                "Measure drift by molecular registration: match the molecules of a freshly scanned "
                "frame (frame_path) to those of a reference frame (reference_frame_path, or a list "
                "of reference molecule coordinates) and return the translation drift_nm (= new - "
                "reference, i.e. how far the sample features moved from their nominal position; to "
                "land on the same molecule, target = nominal + drift), the match quality (matched / "
                "expected molecules), the residual rms_nm and each matched molecule's displacement "
                "(matches, for local per-site displacement). With trustworthy=false (quality<0.5 or "
                "fewer than 3 matches) do not use this translation; use the extrapolated drift "
                "instead. Read-only: touches no instrument and changes no file. Independent of the "
                "scan angle (registration happens in the frame's own axes); the angle is only used "
                "to convert the translation into scanner coordinates drift_scanner_m."),
            parameters=[
                ParameterSpec(
                    name="frame_path", type="str", required=False, default="",
                    description=tr("新扫的那一帧 .sxm。与 new_molecules 二选一。",
                                   "The freshly scanned .sxm frame. Give this or new_molecules.")),
                ParameterSpec(
                    name="reference_frame_path", type="str", required=False, default="",
                    description=tr("参考帧 .sxm（标注页所基于的那张）。分子由它检出，下标与 "
                                   "ReadSiteMarks 的 anchor 一致。与 reference_molecules 二选一。",
                                   "The reference .sxm (the one the marking page was made from). "
                                   "Molecules are detected on it; the indices match "
                                   "ReadSiteMarks' anchor. Give this or reference_molecules.")),
                ParameterSpec(
                    name="new_molecules", type="str", required=False, default="",
                    description=tr("新帧的分子坐标，JSON：[[x_nm, y_nm], ...]，帧心为原点、+y 向上、"
                                   "与参考同一套轴。给了就不读 frame_path。",
                                   "Molecule coordinates of the new frame, JSON: [[x_nm, y_nm], ...], "
                                   "frame-centre origin, +y up, same axes as the reference. If given, "
                                   "frame_path is not read.")),
                ParameterSpec(
                    name="reference_molecules", type="str", required=False, default="",
                    description=tr("参考分子坐标，JSON：[[x_nm, y_nm], ...]，或 ReadSiteMarks 的 "
                                   "anchor_molecules（{\"<下标>\": {\"x_nm\":..,\"y_nm\":..}}）。给了就"
                                   "不读 reference_frame_path。",
                                   "Reference molecule coordinates, JSON: [[x_nm, y_nm], ...], or "
                                   "ReadSiteMarks' anchor_molecules ({\"<index>\": {\"x_nm\":..,"
                                   "\"y_nm\":..}}). If given, reference_frame_path is not read.")),
                ParameterSpec(
                    name="prev_drift_x_nm", type="float", required=False, default=0.0,
                    unit="nm", min_value=-1000.0, max_value=1000.0,
                    description=tr("搜索窗口的中心 X（上一次的漂移或外推值），帧轴上的 nm。",
                                   "Search-window centre X (the last drift or the extrapolated "
                                   "value), nm in the frame's axes.")),
                ParameterSpec(
                    name="prev_drift_y_nm", type="float", required=False, default=0.0,
                    unit="nm", min_value=-1000.0, max_value=1000.0,
                    description=tr("搜索窗口的中心 Y。", "Search-window centre Y.")),
                ParameterSpec(
                    name="window_nm", type="float", required=False, default=WINDOW_NM,
                    unit="nm", min_value=0.1, max_value=50.0,
                    description=tr("先看 prev_drift ± 这么多 nm。", "First look at prev_drift ± this many nm.")),
                ParameterSpec(
                    name="wide_window_nm", type="float", required=False, default=WIDE_WINDOW_NM,
                    unit="nm", min_value=0.1, max_value=50.0,
                    description=tr("匹配太少时放宽到 ± 这么多 nm（没有先验的第一次配准可以直接给大）。",
                                   "Widen to ± this many nm when matches are too few (the first "
                                   "registration, which has no prior, may start with a large value).")),
                ParameterSpec(
                    name="match_tol_nm", type="float", required=False, default=MATCH_TOL_NM,
                    unit="nm", min_value=0.02, max_value=1.0,
                    description=tr("匹配半径。", "Match radius.")),
                ParameterSpec(
                    name="min_quality", type="float", required=False, default=QUALITY_MIN,
                    min_value=0.0, max_value=1.0,
                    description=tr("quality 低于它 trustworthy=false。",
                                   "trustworthy=false when quality is below this.")),
                ParameterSpec(
                    name="include_matches", type="bool", required=False, default=True,
                    description=tr("结果里带上每个匹配分子的位移（局部位移 / 锚点位移要用）。",
                                   "Include each matched molecule's displacement in the result "
                                   "(needed for local / anchored per-site displacement).")),
            ],
            estimated_duration_s=2.0,
            composition_level=2,
            tags=["drift", "registration", "molecules", "site-marking", "analysis"],
        )

    # ── 入口 ───────────────────────────────────────────────────────────────

    def execute(self, context, params: dict) -> SkillResult:
        try:
            return self._run(params or {})
        except Exception as exc:  # noqa: BLE001 — 技能不抛异常,否则到不了 agent 的错误路径
            logger.exception("RegisterDriftByMolecules 意外失败")
            return self._fail("internal_error", tr(
                f"配准时出错: {type(exc).__name__}: {exc}",
                f"Unexpected error while registering: {type(exc).__name__}: {exc}"))

    @staticmethod
    def _fail(reason: str, error: str, **extra) -> SkillResult:
        data = {"drift_nm": None, "drift_scanner_m": None, "quality": 0.0, "rms_nm": None,
                "n_matched": 0, "trustworthy": False, "degraded": True, "reason": reason,
                "matches": [], "warnings": []}
        data.update(extra)
        return SkillResult(skill_name=_SKILL_NAME, success=False, error=error, data=data)

    def _run(self, p: dict) -> SkillResult:
        warns: list[str] = []
        prev = (float(p.get("prev_drift_x_nm") or 0.0), float(p.get("prev_drift_y_nm") or 0.0))
        if not all(math.isfinite(v) for v in prev):
            prev = (0.0, 0.0)             # NaN / inf 的先验不是先验

        # ── 参考 ──
        ref_fm: FrameMolecules | None = None
        ref_ids: np.ndarray
        if str(p.get("reference_molecules") or "").strip():
            ref_pts, ref_ids, err = _parse_points(str(p["reference_molecules"]), "reference_molecules")
            if err:
                return self._fail("bad_reference", tr(f"参考分子坐标读不了：{err}",
                                                      f"Cannot read the reference molecules: {err}"))
        elif str(p.get("reference_frame_path") or "").strip():
            ref_fm = molecules_in_frame(str(p["reference_frame_path"]).strip())
            if ref_fm.error:
                return self._fail("reference_unreadable", tr(
                    f"参考帧读不了：{ref_fm.error}", f"Cannot read the reference frame: {ref_fm.error}"))
            ref_pts, ref_ids = ref_fm.pts, np.arange(len(ref_fm.pts))
        else:
            return self._fail("no_reference", tr(
                "没有参考：给 reference_frame_path 或 reference_molecules。",
                "No reference: give reference_frame_path or reference_molecules."))

        # ── 新帧 ──
        new_fm: FrameMolecules | None = None
        if str(p.get("new_molecules") or "").strip():
            new_pts, _ids, err = _parse_points(str(p["new_molecules"]), "new_molecules")
            if err:
                return self._fail("bad_new", tr(f"新帧分子坐标读不了：{err}",
                                                f"Cannot read the new molecules: {err}"))
        elif str(p.get("frame_path") or "").strip():
            new_fm = molecules_in_frame(str(p["frame_path"]).strip())
            if new_fm.error:
                return self._fail("frame_unreadable", tr(
                    f"新帧读不了：{new_fm.error}", f"Cannot read the new frame: {new_fm.error}"))
            new_pts = new_fm.pts
        else:
            return self._fail("no_frame", tr("没有新帧：给 frame_path 或 new_molecules。",
                                             "No new frame: give frame_path or new_molecules."))

        if len(ref_pts) < MIN_MOLECULES or len(new_pts) < MIN_MOLECULES:
            return self._fail("too_few_molecules", tr(
                f"检出的分子太少（参考 {len(ref_pts)}、新帧 {len(new_pts)}，至少各 {MIN_MOLECULES} 个）"
                "—— 新帧上看不到分子（针尖状态 / 扫描范围不对）？",
                f"Too few molecules (reference {len(ref_pts)}, new frame {len(new_pts)}; at least "
                f"{MIN_MOLECULES} each) - no molecules visible on the new frame (tip state / wrong "
                f"scan area?)"), n_new=int(len(new_pts)), n_ref=int(len(ref_pts)))

        # ── 几何:两帧的轴 / 中心 ──
        field = None
        shift = (0.0, 0.0)
        angle_ref = None
        frame_info: dict = {}
        if ref_fm is not None and ref_fm.geo is not None and ref_fm.geo.angle_known:
            angle_ref = float(ref_fm.geo.angle_deg)
        if ref_fm is not None and new_fm is not None and ref_fm.geo is not None \
                and new_fm.geo is not None:
            gr, gn = ref_fm.geo, new_fm.geo
            if gr.angle_known and gn.angle_known and abs(gr.angle_deg - gn.angle_deg) > _ANGLE_TOL_DEG:
                return self._fail("angle_mismatch", tr(
                    f"两帧扫描角不同（参考 {gr.angle_deg:.2f}°、新帧 {gn.angle_deg:.2f}°）—— 平移模型不成立。"
                    "新帧要用和参考一样的扫描角。",
                    f"The two frames have different scan angles (reference {gr.angle_deg:.2f} deg, new "
                    f"{gn.angle_deg:.2f} deg) - a pure translation does not apply. Scan the new frame "
                    f"at the reference angle."))
            if not (gr.angle_known and gn.angle_known):
                warns.append(tr("有一帧读不到扫描角：两帧中心的差按 0° 折算。",
                                "A frame's scan angle is unknown: the centre difference is converted "
                                "at 0 deg."))
            # 新帧中心相对参考中心的位移,换到参考帧自己的轴上(扫描器轴 → 帧轴)。
            shift = scanner_delta_to_frame(gn.cx_nm - gr.cx_nm, gn.cy_nm - gr.cy_nm,
                                           angle_ref if angle_ref is not None else 0.0)
            field = (shift[0], shift[1], gn.w_nm, gn.h_nm)
            if math.hypot(*shift) > 0.5 * min(gn.w_nm, gr.w_nm):
                warns.append(tr("两帧中心相距超过半帧，重叠很少，配准不可靠。",
                                "The frame centres are more than half a frame apart; little overlap, "
                                "registration is unreliable."))
            if abs(gn.w_nm - gr.w_nm) > 0.1 * gr.w_nm:
                warns.append(tr("两帧视野大小相差一成以上。", "The frame sizes differ by more than 10%."))
            if ref_fm.scan_dir and new_fm.scan_dir and ref_fm.scan_dir != new_fm.scan_dir:
                warns.append(tr(
                    f"扫描方向不同（参考 {ref_fm.scan_dir}、新帧 {new_fm.scan_dir}）：上/下扫之间有"
                    "回程差，会作为一个常数偏置混进漂移。",
                    f"Scan directions differ (reference {ref_fm.scan_dir}, new {new_fm.scan_dir}): "
                    f"up/down scans have a retrace offset that adds a constant bias to the drift."))
            if ref_fm.bias_v is not None and new_fm.bias_v is not None \
                    and abs(ref_fm.bias_v - new_fm.bias_v) > 0.05 * max(abs(ref_fm.bias_v), 0.01):
                warns.append(tr(
                    f"偏压不同（参考 {ref_fm.bias_v:g} V、新帧 {new_fm.bias_v:g} V）：分子衬度会变，"
                    "匹配质量可能偏低。",
                    f"Biases differ (reference {ref_fm.bias_v:g} V, new {new_fm.bias_v:g} V): the "
                    f"molecular contrast changes and the match quality may be low."))
            frame_info = {"reference": gr.as_dict(), "new": gn.as_dict(),
                          "center_shift_nm": [round(shift[0], 4), round(shift[1], 4)]}
            new_pts = new_pts + np.asarray(shift)
        elif new_fm is not None and new_fm.geo is not None:
            gn = new_fm.geo
            field = (0.0, 0.0, gn.w_nm, gn.h_nm)         # 只给了参考坐标:假定同一套轴、同一个中心

        reg = register(
            new_pts, ref_pts, prev, field=field,
            window_nm=float(p.get("window_nm") or WINDOW_NM),
            wide_window_nm=float(p.get("wide_window_nm") or WIDE_WINDOW_NM),
            match_tol_nm=float(p.get("match_tol_nm") or MATCH_TOL_NM),
            min_quality=float(QUALITY_MIN if p.get("min_quality") is None else p["min_quality"]))

        if reg.competing_peak_ratio is not None and reg.competing_peak_ratio >= AMBIGUOUS_RATIO:
            warns.append(tr(
                f"偏移直方图里有别的峰在竞争（票数是选中峰的 {reg.competing_peak_ratio:.0%}）：晶格状"
                "排布上，差整数个晶格矢量的平移分不开，这次靠 prev_drift 裁决。prev_drift 偏离真值超过"
                "半个晶格常数时会差一个分子。",
                f"Other peaks compete in the offset histogram ({reg.competing_peak_ratio:.0%} of the "
                f"chosen peak's votes): on a lattice-like arrangement translations that differ by a "
                f"lattice vector cannot be told apart and prev_drift decided. If prev_drift is off by "
                f"more than half a lattice constant the result is one molecule off."))
        if not reg.trustworthy:
            warns.append(tr(
                f"配准不可信（{reg.reason}：匹配 {reg.n_matched}/{reg.n_expected}，quality "
                f"{reg.quality:.2f}）—— 别采信这次平移。",
                f"Registration not trustworthy ({reg.reason}: matched {reg.n_matched}/"
                f"{reg.n_expected}, quality {reg.quality:.2f}) - do not use this translation."))

        data = reg.asdict()
        dx, dy = reg.drift_nm
        data["drift_scanner_m"] = (list(frame_delta_to_scanner_m(dx, dy, angle_ref))
                                   if angle_ref is not None else None)
        data["angle_deg"] = angle_ref
        data["height_ratio"] = (height_ratio(new_fm, ref_fm, reg.pairs)
                                if (new_fm is not None and ref_fm is not None) else None)
        if data["height_ratio"] is not None:
            data["height_ratio"] = round(data["height_ratio"], 3)
        data["frames"] = frame_info
        data["warnings"] = warns
        data["degraded"] = False
        if p.get("include_matches", True):
            data["matches"] = [
                [int(ref_ids[j]), round(float(ref_pts[j, 0]), 4), round(float(ref_pts[j, 1]), 4),
                 round(float(new_pts[i, 0]), 4), round(float(new_pts[i, 1]), 4)]
                for i, j in reg.pairs]
        else:
            data["matches"] = []
        rms = "—" if reg.rms_nm is None else f"{reg.rms_nm:.3f}"
        summary = tr(
            f"漂移 ({dx:+.3f}, {dy:+.3f}) nm，质量 {reg.quality:.2f}（匹配 {reg.n_matched}/"
            f"{reg.n_expected}），残差 rms {rms} nm，"
            + ("可信" if reg.trustworthy else f"不可信（{reg.reason}）"),
            f"Drift ({dx:+.3f}, {dy:+.3f}) nm, quality {reg.quality:.2f} (matched {reg.n_matched}/"
            f"{reg.n_expected}), residual rms {rms} nm, "
            + ("trustworthy" if reg.trustworthy else f"NOT trustworthy ({reg.reason})"))
        return SkillResult(skill_name=_SKILL_NAME, success=True, data=data, summary=summary)


def make_tool(context_provider):
    from mast.agents._shared.skill_adapter import wrap_skill
    return wrap_skill(RegisterDriftByMolecules, context_provider)
