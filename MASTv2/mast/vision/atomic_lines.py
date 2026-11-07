"""扫描**线**上的原子分辨判据 —— 「有没有规则的跳动」。

## 为什么要它，而帧级判据不够

按帧判，一个回合就是分钟级（例如 5 nm / 256 px 的图，在 0.768 s/线下要 **6.6 分钟**）。
而按扫描线判的手法是：

> 每做两下就停下来看扫描线，如此循环，直到扫描线出现规则的跳动

—— 按线判，一条线只要 `线时 × 2（正反扫）`，**回合比整帧快一个数量级**。
这就是该手法快的机制。

## 它回答的**不是**帧级判据那个问题

一条线只有一个方向，**判不了「是不是二维晶格」**。负例帧上也可能出现与正例
相同周期的峰 —— 区分正负的是**强度**，不是那个周期存在与否。
`vision.atomic_phase` 的角向集中度是二维的，它才判得了「晶格」。

所以本模块的输出叫 **advisory（建议）**，用途只有一个：
「值得不值得停下来扫一整帧、交给二维判据」。见 :func:`frame_line_advisory`。

## 标定

阈值的标定样本很少（正负各寥寥数帧），负例与正例的线级 SNR 之间有明显空档。按
「未标定的阈值没资格否决人」，:data:`ADVISORY_SNR` 是一道**建议线**，不是判决线。

⚠️ 必须喂 **Z 通道**：恒流反馈下电流是误差信号，电流通道的 SNR 明显低于
Z 通道，规则跳动主要落在 Z 上。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

#: 分析关注的周期区间（nm）；须按目标表面与像素尺度评估。
#: 比下沿小的是噪声，比上沿大的是形貌 —— 两头都不是「原子」。
PERIOD_MIN_NM = 0.15
PERIOD_MAX_NM = 0.45

#: 去趋势的多项式阶数。台阶与漂移是低频，不去掉会把晶格峰淹了；
#: 阶数再高会开始吃掉晶格本身。
DETREND_ORDER = 3

#: 一条线至少要这么多有效点才值得算 —— 再少，FFT 的频率分辨率撑不起
#: PERIOD_MIN..MAX 这个区间（见 :func:`line_score` 的 ``bins_in_band``）。
MIN_PIXELS = 32

#: 线级 SNR 的建议阈值，不是原子分辨验收阈值。
#: 目标样品需另行验证；上层仍须定期采整帧，防止建议指标漏报。
ADVISORY_SNR = 115.0

#: 连续多轮超过建议阈值才提示确认，减少单轮波动的影响。
ADVISORY_STREAK = 2


def line_score(line: Any, nm_per_px: float) -> dict:
    """一条扫描线的「规则跳动」得分。

    去慢变 → 加窗 → FFT → 在物理可能的周期区间里取峰 →
    ``line_snr = 峰功率 / 该区间内的中位功率``。

    读不到就说读不到：点数不够、像素尺度撑不起那个频段，都回 ``ok=False`` 加
    一句人话，**不回一个 0**。一个 0 会被下游读成「测了，没有」。
    """
    y = np.asarray(line, dtype=float)
    finite = np.isfinite(y)
    if int(finite.sum()) < MIN_PIXELS:
        return {"ok": False,
                "why": "这条线只有 %d 个有效点，不足 %d" % (int(finite.sum()), MIN_PIXELS)}
    y = y[finite]
    n = y.size
    x = np.arange(n, dtype=float)
    try:
        y = y - np.polyval(np.polyfit(x, y, DETREND_ORDER), x)
    except Exception:  # noqa: BLE001 — 病态拟合不该让整轮判读挂掉
        y = y - float(np.mean(y))

    sp = np.abs(np.fft.rfft(y * np.hanning(n))) ** 2
    freq = np.fft.rfftfreq(n, d=1.0)                      # 周期/像素
    with np.errstate(divide="ignore", invalid="ignore"):
        period_nm = np.where(freq > 0, nm_per_px / np.maximum(freq, 1e-12), np.inf)
    band = (period_nm >= PERIOD_MIN_NM) & (period_nm <= PERIOD_MAX_NM)
    if int(band.sum()) < 3:
        return {"ok": False,
                "why": ("%.4f nm/px 的尺度下，%d 点的线在 %.2f–%.2f nm 这个周期区间里"
                        "只有 %d 个频率格 —— 撑不起一个峰"
                        % (nm_per_px, n, PERIOD_MIN_NM, PERIOD_MAX_NM, int(band.sum())))}

    k = int(np.argmax(np.where(band, sp, -np.inf)))
    peak = float(sp[k])
    med = float(np.median(sp[band]))
    half = peak / 2.0
    lo, hi = k, k
    while lo > 1 and sp[lo - 1] >= half:
        lo -= 1
    while hi < sp.size - 1 and sp[hi + 1] >= half:
        hi += 1
    return {
        "ok": True,
        "period_nm": float(period_nm[k]),
        "line_snr": (float(peak / med) if med > 0 else float("inf")),
        "peak_width_bins": int(hi - lo + 1),
        "n_px": int(n),
        "bins_in_band": int(band.sum()),
    }


def usable_rows(img: Any) -> Any:
    """识别已经采集完成的扫描行：整行有限且不全零。

    原始扫描缓冲的未写入区域可为全零，因此仅检查 isfinite 不够。
    此检查不依赖相邻两次回包做差；固定绝对容差可能大于实际 Z 信号幅度，
    把零值与已采集信号错误地当作相同。
    """
    a = np.asarray(img, dtype=float)
    if a.ndim != 2:
        return np.zeros(0, dtype=bool)
    zero = (np.nan_to_num(a) == 0).all(axis=1)
    return (~zero) & np.isfinite(a).all(axis=1)


def frame_line_advisory(img: Any, nm_per_px: float, *,
                        rows: Optional[Any] = None,
                        advisory_snr: float = ADVISORY_SNR) -> dict:
    """把 :func:`line_score` 铺到给定的行上，回一份**建议**。

    ``rows`` 是布尔掩码或行号数组；不给就用 :func:`usable_rows`。

    统计用**中位数**不用均值：掠过一个台阶、一次针尖跳变的坏线会把均值拽走，
    而要看的是「多数线上都有规则跳动」。
    """
    a = np.asarray(img, dtype=float)
    if a.ndim != 2:
        return {"ok": False, "why": "不是二维图：shape=%s" % (a.shape,)}
    if rows is None:
        rows = usable_rows(a)
    idx = (np.flatnonzero(rows) if np.asarray(rows).dtype == bool
           else np.asarray(rows, dtype=int))
    scored, skipped = [], []
    for j in idx:
        s = line_score(a[int(j)], nm_per_px)
        (scored if s.get("ok") else skipped).append(s)
    if not scored:
        why = skipped[0]["why"] if skipped else "这一帧里没有已扫出来的行"
        return {"ok": False, "n_rows_offered": int(idx.size), "why": why}

    snr = np.array([s["line_snr"] for s in scored], dtype=float)
    per = np.array([s["period_nm"] for s in scored], dtype=float)
    wid = np.array([s["peak_width_bins"] for s in scored], dtype=float)
    med = float(np.median(snr))
    return {
        "ok": True,
        "n_lines_scored": int(snr.size),
        "n_lines_skipped": int(len(skipped)),
        "line_snr_median": med,
        "line_snr_p90": float(np.percentile(snr, 90)) if snr.size > 4 else None,
        "period_median_nm": float(np.median(per)),
        "peak_width_median_bins": float(np.median(wid)),
        "advisory_snr": float(advisory_snr),
        "worth_a_frame": bool(med >= advisory_snr),
        # 措辞刻意是「建议」不是「判定」：一条线判不了二维晶格，而且这条线的
        # 阈值只在 2 正 2 负上标过。
        "advisory": (
            "线上的规则跳动够强（中位 %.1f ≥ %.0f）—— **建议**停下来扫一整帧，"
            "交二维判据定夺。一条线判不了「是不是晶格」。" % (med, advisory_snr)
            if med >= advisory_snr else
            "线上的规则跳动还不够（中位 %.1f < %.0f）—— 建议接着打磨。"
            "单轮下跌可能来自采样波动，应结合重复观测判断针尖是否变化。"
            % (med, advisory_snr)),
    }
