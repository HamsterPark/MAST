"""English text for the factory composite specs in :mod:`mast.skills.composite.builtin_composites`.

The ten declarative twins are fingerprinted (``factory_content_hash`` decides whether a stored copy
is still the untouched factory one) and persisted in the composite version store, so their Chinese
text must stay exactly as it is. The UI shows English by translating at read time instead:
:func:`localize_composite_dict` swaps a string for its English entry only when the active language
is English and the string is *exactly* a factory text. A text the user edited no longer matches and
stays as they wrote it.

Display only. Never feed the result to a store, a hash, a validator that persists, or a registry.

Keys are the Chinese strings as they occur in ``builtin_composites.py`` (descriptions, parameter
descriptions and the message strings of the ``PreScanCheck`` twin). The strings that start and end
with ``'`` are DSL string expressions: the English value keeps the quotes and contains no apostrophe,
so it is still a valid expression. ``test_builtin_composites_i18n.py`` pins every key to a literal in
``builtin_composites.py`` and every Chinese text of the factory specs to an entry here.
"""
from __future__ import annotations

import copy
from typing import Any

from mast.i18n import get_lang

__all__ = ["EN_BY_ZH", "localize_composite_dict"]

#: factory Chinese text -> English
EN_BY_ZH: dict[str, str] = {
    # -- FullScan
    "一步式扫描：配置帧+速度→启动→等结束→撞针检测；超时或撞针则失败。":
        ("One-step scan: configure frame + speed -> start -> wait for the end -> tip crash check; "
         "fails on timeout or tip crash."),
    "扫描中心 X (m)": "Scan center X (m)",
    "扫描中心 Y (m)": "Scan center Y (m)",
    "宽 (m)": "Width (m)",
    "高 (m)": "Height (m)",
    "每行时间 (s)": "Time per line (s)",
    "采集通道": "Acquisition channels",
    "等扫完超时 (s)": "Wait-for-scan timeout (s)",
    # -- TipPulse
    "偏压脉冲修针：先快照当前偏压，再施加 count 次硬件定时脉冲。":
        "Bias-pulse tip conditioning: snapshot the current bias, then apply count hardware-timed pulses.",
    "脉冲电压 (V)": "Pulse voltage (V)",
    "脉冲宽度 (s)": "Pulse width (s)",
    "脉冲次数": "Number of pulses",
    # -- GridSTS
    "N×N 栅格 STS：逐点移动+采谱；部分成功（≥1 点成功即算成功）。":
        ("N x N grid STS: move to each point and acquire a spectrum; partial success (succeeds if "
         "at least 1 point succeeds)."),
    "中心 X (m)": "Center X (m)",
    "中心 Y (m)": "Center Y (m)",
    "X 点数": "Number of points in X",
    "Y 点数": "Number of points in Y",
    "点间距 (m)": "Point spacing (m)",
    "STS 起始 (V)": "STS start (V)",
    "STS 终止 (V)": "STS end (V)",
    "STS 点数": "Number of STS points",
    # -- ConditionTip
    "修针闭环：脉冲→扫描→FFT 评质→重复，直到达标或用尽次数。":
        ("Closed-loop tip conditioning: pulse -> scan -> FFT quality assessment -> repeat, until "
         "the target is met or the attempts run out."),
    "最大轮次": "Maximum rounds",
    "目标质量": "Target quality",
    "测试扫描宽 (m)": "Test scan width (m)",
    # -- DemoScanAndSTS
    "演示：扫描+存盘+多点 STS；某点移动失败则跳过该点采谱（_failed 标记）。":
        ("Demo: scan + save + multi-point STS; if the move to a point fails, that point's "
         "spectrum is skipped (_failed flag)."),
    "扫描尺寸 (m)": "Scan size (m)",
    "扫描超时 (s)": "Scan timeout (s)",
    # -- ShapeTipOnSurface
    ("样品上整针：宽扫→选平点→渐进扎针(接触即停)→扫坑→评圆度(够圆即收)→不行换点重试；"
     "无论成败都恢复 Z 反馈(try/finally)。"):
        ("Tip shaping on the sample: wide scan -> pick a flat site -> progressive plunge (stop on "
         "contact) -> scan the crater -> assess roundness (accept once round enough) -> otherwise "
         "retry at another site; Z feedback is restored whether it succeeds or fails (try/finally)."),
    "宽扫宽 (m)": "Wide scan width (m)",
    "坑区窗 (m)": "Crater window (m)",
    "最浅扎入 (m)": "Shallowest plunge (m)",
    "最深扎入 (m)": "Deepest plunge (m)",
    "深度步数": "Number of depth steps",
    "接触电流阈 (A)": "Contact current threshold (A)",
    "圆度阈(等效轴比;旧 round_threshold=0.65 已作废,不可换算)":
        ("Roundness threshold (equivalent axis ratio; the old round_threshold=0.65 is obsolete "
         "and cannot be converted)"),
    "最大尝试": "Maximum attempts",
    # -- SurveySurface_TileScan
    "把方形区域切成 grid_n×grid_n 瓦片逐块巡扫建全局概览；部分成功；可评质并记最佳瓦。":
        ("Cut a square area into grid_n x grid_n tiles and survey them one by one to build a "
         "global overview; partial success; can assess quality and record the best tile."),
    "每边瓦片数 (N×N)": "Tiles per side (N x N)",
    "瓦片边长 (m)": "Tile edge length (m)",
    "每瓦等扫超时 (s)": "Wait-for-scan timeout per tile (s)",
    "是否逐瓦评质": "Assess quality tile by tile",
    # -- BatchRegionsScan
    "按 JSON 区域清单逐个扫（每区可不同角度），部分成功。":
        ("Scan the regions of a JSON list one by one (each region may have its own angle); "
         "partial success."),
    "区域清单 JSON 数组": "Region list (JSON array)",
    "每区等扫超时 (s)": "Wait-for-scan timeout per region (s)",
    "每区存盘": "Save each region",
    # -- TrackDrift_ReferenceScan
    "按参考图跟踪样品漂移并补偿扫描框；首次（无参考路径）则把本次当参考。":
        ("Track sample drift against a reference image and compensate the scan frame; on the "
         "first call (no reference path) this scan becomes the reference."),
    "参考点 X (m)": "Reference point X (m)",
    "参考点 Y (m)": "Reference point Y (m)",
    "参考扫描宽 (m)": "Reference scan width (m)",
    "偏压 (V)": "Bias (V)",
    "参考图 .npy 路径（空=存为参考）": "Reference image .npy path (empty = store as the reference)",
    # -- PreScanCheck
    ("预扫描 + 抓实时缓冲区的正反扫。**它给不出针尖判决** —— 缓冲区里的数据与 .sxm 不可比、"
     "且未必是本帧,所以 tip_ready/similarity 恒为 None(判不了),另报 inconclusive_reason "
     "说明为什么。要针尖判决请用手写的PreScanCheck(它先读存盘的 .sxm)。"):
        ("Pre-scan + grab the forward/backward scan from the live buffer. **It cannot give a tip "
         "verdict**: the data in the buffer is not comparable with the .sxm and is not "
         "necessarily this frame, so tip_ready/similarity are always None (undecidable), and "
         "inconclusive_reason says why. For a tip verdict use the hand-written PreScanCheck (it "
         "reads the saved .sxm first)."),
    "宽 (m，与完整扫描同宽)": "Width (m, same as the full scan)",
    "【已不再接线】旧余弦阈,保留仅为兼容签名":
        "[No longer wired] old cosine threshold, kept only to keep the signature compatible",
    ("每线时间(s)，必须显式提供。请根据当前仪器配置与扫描范围设置；"
     "声明式流程无法查询档位表，不提供默认值。"):
        ("Time per line (s). **Required**: the declarative twin cannot read the tier table and "
         "will not guess for you. Set it according to the current instrument configuration and "
         "scan range; no default is provided."),
    ("'PreScanCheck(声明式)必须显式给 line_time_s。"
     "请根据当前仪器配置与扫描范围提供每线时间；此流程无法查询档位表。'"):
        ("'PreScanCheck (declarative) requires line_time_s to be given explicitly. "
         "Provide a time per line suited to the current instrument configuration and scan range; "
         "this flow cannot look up the tier table.'"),
    "'预扫描还没走到取数那一步'": "'The pre-scan has not reached the data-grab step yet'",
    ("'判不了：缓冲路径相似度与 .sxm 路径不可比,且缓冲内容未必是本帧。"
     "这是弃权,不是「针尖不合格」:下一步是去 .sxm 里取这一帧 / 换个地方重新量,"
     "不是修针。'"):
        ("'The similarity of the buffer path is not comparable with the .sxm path, and the buffer "
         "content is not necessarily this frame - undecidable. This is an abstention, not \"tip not "
         "qualified\": the next step is to take this frame from the .sxm / re-measure at another "
         "place, not to condition the tip.'"),
    "'实时缓冲区取不回正反扫数据(GrabScanFrameData 失败)—— 通信/模块问题,不是针尖问题。'":
        ("'Could not retrieve the forward/backward scan data from the live buffer "
         "(GrabScanFrameData failed) - a communication/module problem, not a tip problem.'"),
}


def _swap(x: Any) -> Any:
    if isinstance(x, str):
        return EN_BY_ZH.get(x, x)
    if isinstance(x, list):
        return [_swap(v) for v in x]
    if isinstance(x, dict):
        return {k: _swap(v) for k, v in x.items()}
    return x


def localize_composite_dict(d: dict) -> dict:
    """A deep copy of *d* (a composite spec, a list row or a version row) for display.

    In English every string value that is exactly a factory text is replaced by its English entry,
    wherever it sits (description, ``params[*].description``, node ``reason`` / ``value`` ...). In
    any other language the copy is equal to *d*. Dict keys are never touched.
    """
    out = copy.deepcopy(d)
    if get_lang() != "en":
        return out
    return _swap(out)
