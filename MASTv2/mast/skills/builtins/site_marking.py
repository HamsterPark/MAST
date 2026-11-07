"""位点标注页生成（MAST 本地原生）。

``PrepareSiteMarkingPage``：取一帧 .sxm → 拉平 → 渲成 afmhot PNG →（可选）找分子极大值 →
把这些填进自带的标点页模板 ``site_marking_template.html``，产出一张 **MAST 本地服务**的 HTML
（页面里 ``window.claude.use("db")`` 已被模板内的 shim 换成打 MAST 端点的 fetch+poll）。
人在这张图上点出要测谱的位点；外部 agent 经 ``ReadSiteMarks`` 读回、精确化、漂移追踪测谱。

实现：① 不走 HTTP ``data/frame``，直接用 ``mast.io.nanonis_files.read_sxm``；
② 渲染复用 ``mast.webui.scan_preview._image_to_png_uri``；③ 数据通道是 MAST 本地端点。

⚠️ 坐标：页面/marks 的 (x,y) 是**绝对 nm**（viewBox 锚在 cx-w/2），与这里 ``_pix_to_nm``
给的分子坐标同一套（中心 (cx,cy) + 沿扫描轴的局部偏移，未旋转）。扫描角的处理在 ``ReadSiteMarks``
里做（把局部偏移旋到扫描器坐标），本 skill 只如实把 angle 写进 META。

注：标题/eyebrow 之外，编辑卡里的谱档位文案（±400/±80 mV）等是
特定体系的默认值，换体系时可参数化；``frame_path`` 留空时不会自动取最新帧（必须显式给）。
"""

from __future__ import annotations

import base64
import json
import logging
import math
import re
from pathlib import Path

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

_TEMPLATE_PATH = Path(__file__).with_name("site_marking_template.html")
#: 页面 id 的合法字符（防路径穿越：拼进 artifacts 目录名）。用 ``fullmatch``——``^…$`` + ``match``
#: 会放过尾随换行（``$`` 匹配末尾 \n 之前），"sm_x\n" 这类就漏过去了。
_PAGE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: 空的 marks 文档（与 api/routes/site_marking.py、ReadSiteMarks 对齐的形状）。
_EMPTY_DOC = {"sites": {}, "plan/marking": None, "plan/card": None,
              "plan/confirm": None, "status/live": None}


#: 标点页模板里的纯展示文案（中文默认英文，随请求语言在 PrepareSiteMarkingPage 里填）。
#: 值被 tr() 翻译后替换模板中的同名 __T_*__ 占位符；键既被页面显示、又不被任何代码比较，所以可翻译。
#: zh 必须与模板历史版本逐字节一致（见 tests 的 zh 不变量 / verify_i18n 的还原闸门）。
_UI_STRINGS = {
    '__T_SUB__': ('在图上点出要测谱的位置。程序会把每个点精确到分子中心或氢键位置，在这里生成任务卡；确认后才开始测量。', 'Click where you want spectra measured. Each point is refined to a molecule centre or H-bond site and turned into a task card here; measurement starts only after you confirm.'),
    '__T_DT_IMAGE__': ('图像', 'Image'),
    '__T_DT_REC__': ('记录时间', 'Recorded'),
    '__T_DT_COND__': ('扫描条件', 'Scan conditions'),
    '__T_DT_RANGE__': ('范围', 'Range'),
    '__T_DT_CENTER__': ('中心', 'Centre'),
    '__T_ARIA_IMAGE__': ('STM 图像', 'STM image'),
    '__T_ADD_MODE__': ('点击图像添加位点', 'Click the image to add a site'),
    '__T_SHOW_MOL__': ('显示程序识别的分子', 'Show detected molecules'),
    '__T_CONTRAST__': ('对比度', 'Contrast'),
    '__T_BRIGHT__': ('亮度', 'Brightness'),
    '__T_ALT__': ('形貌图', 'topography'),
    '__T_ARIA_OVERLAY__': ('标注层', 'Annotation layer'),
    '__T_READOUT_HINT__': ('把鼠标移到图上可读坐标', 'Move the pointer over the image to read coordinates'),
    '__T_LEG_YOURS__': ('你们的标注', 'Your marks'),
    '__T_LEG_REFINED__': ('精确化后的位置', 'Refined position'),
    '__T_LEG_DONE__': ('已测完', 'Measured'),
    '__T_H2_PROGRESS__': ('测量进度', 'Measurement progress'),
    '__T_DB_CONNECTING__': ('连接存储中', 'Connecting to storage'),
    '__T_LIVE_IDLE__': ('尚未开始。确认任务卡后，这里会显示当前位点和完成数。', 'Not started yet. Once the task card is confirmed, the current site and completion count appear here.'),
    '__T_SITE__': ('位点', 'Site'),
    '__T_KIND_FIELD__': ('类型（决定程序怎样精确化）', 'Type (determines how the point is refined)'),
    '__T_SPECTRA_FIELD__': ('要测的谱', 'Spectra to measure'),
    '__T_SPEC_WIDE__': ('宽谱 STS + IETS（±400 mV）', 'Wide STS + IETS (±400 mV)'),
    '__T_SPEC_LOW__': ('低能 IETS（±80 mV）', 'Low-energy IETS (±80 mV)'),
    '__T_NOTE__': ('备注', 'Note'),
    '__T_NOTE_PH__': ('例如：链末端的分子', 'e.g. molecule at the chain end'),
    '__T_DEL_SITE__': ('删除这个位点', 'Delete this site'),
    '__T_DEL_YES__': ('确认删除', 'Confirm delete'),
    '__T_CLOSE__': ('完成', 'Done'),
    '__T_H2_LIST__': ('位点列表', 'Site list'),
    '__T_LIST_HINT__': ('编号即测量顺序；时间不够时按编号优先。拖动圆圈可微调，点圆圈可改类型或删除。', 'The number is the measurement order; when time is short, lower numbers come first. Drag a circle to nudge it; click one to change its type or delete it.'),
    '__T_TH_KIND__': ('类型', 'Type'),
    '__T_TH_SPEC__': ('谱', 'Spectra'),
    '__T_TH_REFINE__': ('精确化', 'Refined'),
    '__T_TH_STATUS__': ('状态', 'Status'),
    '__T_LIST_EMPTY__': ('还没有位点。在图上点一下就能添加第一个。', 'No sites yet. Click on the image to add the first one.'),
    '__T_DONE_MARK__': ('标注完成', 'Marking done'),
    '__T_COPY_BTN__': ('复制坐标（备用）', 'Copy coordinates (backup)'),
    '__T_COPY_ARIA__': ('位点坐标 JSON', 'Site coordinates JSON'),
    '__T_H2_CARD__': ('任务卡', 'Task card'),
    '__T_CARD_WAIT__': ('等待生成', 'Awaiting generation'),
    '__T_CARD_EMPTY__': ('标注完成后，我会精确化位置、排时间，并在这里生成任务卡。', "Once marking is done, I'll refine the positions, schedule them, and generate the task card here."),
    '__T_CONFIRM_START__': ('确认任务卡，开始测量', 'Confirm task card, start measuring'),
    '__T_KIND_AUTO__': ('自动判断', 'Auto'),
    '__T_KIND_MOL__': ('分子中心', 'Molecule centre'),
    '__T_KIND_HBOND__': ('氢键位置（两分子之间）', 'H-bond site (between two molecules)'),
    '__T_KIND_EDGE__': ('分子边缘 / 链端', 'Molecule edge / chain end'),
    '__T_KIND_AU__': ('裸 Au 参考', 'Bare Au reference'),
    '__T_KIND_FIXED__': ('按标注原位，不移动', 'As marked, do not move'),
    '__T_ST_TODO__': ('待测', 'To do'),
    '__T_ST_RUNNING__': ('测量中', 'Measuring'),
    '__T_ST_DONE__': ('完成', 'Done'),
    '__T_ST_SKIPPED__': ('跳过', 'Skipped'),
    '__T_ST_FAILED__': ('失败', 'Failed'),
    '__T_SAVE_FAIL_PRE__': ('保存失败（', 'Save failed ('),
    '__T_REASON_UNKNOWN__': ('未知原因', 'unknown reason'),
    '__T_SAVE_FAIL_POST__': ('）。改动只留在本页，请稍后再改一次，或用“复制坐标”发给我。', "). The change stays on this page only; please try again shortly, or use 'Copy coordinates' to send it to me."),
    '__T_NEAREST__': ('距最近分子', 'nearest molecule'),
    '__T_NO_DB_COPY__': ('存储不可用：请点“复制坐标”，把内容粘贴到对话里。', "Storage unavailable: click 'Copy coordinates' and paste the contents into the chat."),
    '__T_COPIED_PRE__': ('已复制 ', 'Copied '),
    '__T_COPIED_POST__': (' 个位点，可以直接粘贴给我。', ' site(s), ready to paste to me.'),
    '__T_COPY_MANUAL__': ('浏览器不允许自动复制，已选中下面的文本，请手动复制。', 'The browser blocked automatic copying; the text below is selected, please copy it manually.'),
    '__T_CONFIRM_ARM__': ('再点一次，确认开始', 'Click again to confirm start'),
    '__T_NO_DB_CONFIRM__': ('存储不可用：请直接在对话里回复“确认”。', "Storage unavailable: reply 'confirm' directly in the chat."),
    '__T_CNT_UNIT__': (' 个', ' site(s)'),
    '__T_WIDE_SHORT__': ('宽', 'Wide'),
    '__T_LOW_SHORT__': ('低能', 'Low'),
    '__T_NONE__': ('无', 'None'),
    '__T_REFINED_PRE__': ('精确化：', 'Refined: '),
    '__T_COMMA__': ('，', ', '),
    '__T_RESULT_PRE__': ('结果：', 'Result: '),
    '__T_JOIN_SEP__': ('\u3000', '  '),
    '__T_EDIT_MORE__': ('继续修改标注', 'Edit marks again'),
    '__T_SUBMITTED_PRE__': ('已提交 ', 'Submitted '),
    '__T_SUBMITTED_MID__': (' 个位点 · ', ' site(s) · '),
    '__T_CONFIRMED__': ('已确认', 'Confirmed'),
    '__T_TO_CONFIRM__': ('待确认', 'To confirm'),
    '__T_CONFIRM_ON__': ('确认于 ', 'Confirmed at '),
    '__T_VER_PRE__': ('版本 ', 'Version '),
    '__T_VER_MID__': (' · 生成于 ', ' · generated '),
    '__T_DONE_PRE__': ('完成 ', 'Done '),
    '__T_UPDATED_AT__': ('更新于 ', 'Updated '),
    '__T_SYNC_LOST__': ('同步中断，请刷新', 'Sync lost, please refresh'),
    '__T_LOCAL_ONLY__': ('仅本页（无法保存）', 'This page only (cannot save)'),
    '__T_NO_SHARED_DB__': ('这个视图连不上共享存储，标注不会自动传给我。标完后请点“复制坐标”，粘贴到对话里。', "This view can't reach shared storage, so marks won't be sent to me automatically. When you're done, click 'Copy coordinates' and paste them into the chat."),
    '__T_CONNECTED__': ('已连接 · 自动保存', 'Connected · auto-saving'),
}


def site_marking_dir(page_id: str) -> Path:
    """某个标点页的目录：``<project_root>/artifacts/site_marking/<page_id>/``。

    用全仓主流 artifacts 约定 ``project_root()/"artifacts"/<kind>``（diagnostics / mosaics /
    maps / vision_frames … ~20 处都是这样；wishlist 的 ``MASTv2/artifacts`` 是异类，别照它）。
    A/B（api/routes/site_marking.py）/C（site_marking_read.py）三方都用这一处。
    """
    from mast._runtime_paths import project_root

    return project_root() / "artifacts" / "site_marking" / page_id


def _level(z):
    """平面拟合 + 逐行中位去除（照蓝本 refimage.level）。"""
    import numpy as np

    a = np.array(z, dtype=float)
    yy, xx = np.mgrid[: a.shape[0], : a.shape[1]]
    mk = np.isfinite(a)
    if mk.sum() >= 3:
        c, *_ = np.linalg.lstsq(
            np.c_[xx[mk], yy[mk], np.ones(mk.sum())], a[mk], rcond=None)
        a = a - (c[0] * xx + c[1] * yy + c[2])
    a = a - np.nanmedian(a, axis=1, keepdims=True)
    return np.nan_to_num(a, nan=float(np.nanmedian(a)) if mk.any() else 0.0)


def _pix_to_nm(i, j, nx, ny, w_nm, h_nm, cx_nm, cy_nm):
    """像素 (row i 从顶算, col j) → 绝对 nm（帧心 (cx,cy)、+y 向上），与页面坐标同一套。"""
    return (cx_nm - w_nm / 2 + (j + 0.5) * (w_nm / nx),
            cy_nm + h_nm / 2 - (i + 0.5) * (h_nm / ny))


def _molecules(a, w_nm, h_nm, cx_nm, cy_nm, sigma_px=2.5, size_nm=0.45, frac=0.45):
    """分子极大值检测（照蓝本 refimage.molecules）。返回 [{x,y}]（绝对 nm）。

    依赖 scipy；装不上 / 出错一律返回空表（标点页照样能用，只是不画「程序识别的分子」）。
    """
    try:
        import numpy as np
        from scipy import ndimage as ndi
    except Exception as exc:  # noqa: BLE001 — scipy 缺失不该让建页失败
        logger.warning("PrepareSiteMarkingPage: 跳过分子检测（scipy 不可用：%s）", exc)
        return []
    try:
        ny, nx = a.shape
        s = ndi.gaussian_filter(a, sigma_px)
        win = max(3, int(round(size_nm / (w_nm / nx))) | 1)
        mx = s == ndi.maximum_filter(s, size=win)
        lo, hi = np.percentile(s, [50, 99.7])
        thr = lo + frac * (hi - lo)
        ii, jj = np.nonzero(mx & (s > thr))
        out = []
        for i, j in zip(ii, jj):
            if 2 <= i < ny - 2 and 2 <= j < nx - 2:
                di = 0.5 * (s[i - 1, j] - s[i + 1, j]) / (
                    s[i - 1, j] - 2 * s[i, j] + s[i + 1, j] or 1e-30)
                dj = 0.5 * (s[i, j - 1] - s[i, j + 1]) / (
                    s[i, j - 1] - 2 * s[i, j] + s[i, j + 1] or 1e-30)
            else:
                di = dj = 0.0
            x, y = _pix_to_nm(i + di, j + dj, nx, ny, w_nm, h_nm, cx_nm, cy_nm)
            out.append({"x": round(float(x), 3), "y": round(float(y), 3)})
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("PrepareSiteMarkingPage: 分子检测失败，返回空表：%s", exc)
        return []


def _floats(raw, scale=1.0, n=2):
    """把头部里 "a b" 这类原始字符串取成 n 个 float（乘 scale）；取不到给 None 列表。"""
    try:
        vals = [float(t) * scale for t in str(raw).split()[:n]]
        if len(vals) == n:
            return vals
    except (TypeError, ValueError):
        pass
    return [None] * n


def _build_meta(header: dict, frame_path: str) -> dict:
    """从 .sxm 头部拼出页面要的 META（长度单位 nm、角度 deg、偏压 mV）。"""
    w_nm, h_nm = _floats(header.get("scan_range"), scale=1e9)
    cx_nm, cy_nm = _floats(header.get("scan_offset"), scale=1e9)
    px = header.get("scan_pixels") or [None, None]
    # 角度读不到就给 None，别折成 0.0 —— 「读不到」不是「0°」。ReadSiteMarks 见 None 会
    # 报 angle_unknown、不给扫描器坐标（而不是默默按 0° 把站位算错）。
    angle = None
    if "scan_angle" in header:
        try:
            angle = float(str(header["scan_angle"]).split()[0])
        except (ValueError, IndexError):
            angle = None
    try:
        bias_mv = round(float(str(header.get("bias", "")).split()[0]) * 1000, 1)
    except (TypeError, ValueError, IndexError):
        bias_mv = None
    rec = " ".join(x for x in (header.get("rec_date", ""), header.get("rec_time", "")) if x).strip()
    return {
        "file": Path(frame_path).name,
        "rec": rec,
        "bias_mv": bias_mv,
        "sp_pa": "—",  # setpoint 不在 .sxm 头里（蓝本亦硬编）；留占位，不假造
        "px": px[0],
        "w": round(w_nm, 4) if w_nm is not None else None,
        "h": round(h_nm, 4) if h_nm is not None else None,
        "cx": round(cx_nm, 4) if cx_nm is not None else None,
        "cy": round(cy_nm, 4) if cy_nm is not None else None,
        "angle": angle,
        "x0": round(cx_nm - w_nm / 2, 4) if (cx_nm is not None and w_nm is not None) else None,
        "y1": round(cy_nm + h_nm / 2, 4) if (cy_nm is not None and h_nm is not None) else None,
    }


class PrepareSiteMarkingPage(BaseSkill):
    """从一帧扫描图生成一张「位点标注」网页（供人点出要测谱的位点）。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="PrepareSiteMarkingPage",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("从一帧 .sxm 生成一张「位点标注」网页：图上点出要测谱的位点，MAST 本地服务，"
                   "外部 agent 再读回这些点去精确化并漂移追踪测谱。不碰仪器，只读帧、写本地页面。",
                   "Build a 'site marking' web page from one .sxm frame: click points to measure on "
                   "the image; MAST serves it locally and an external agent reads the points back to "
                   "refine and drift-track spectroscopy. Touches no instrument — reads a frame, writes a local page.")
            ),
            parameters=[
                ParameterSpec(
                    name="frame_path", type="str", required=True,
                    description=tr("要标注的那帧 .sxm 的路径。", "Path of the .sxm frame to mark up."),
                ),
                ParameterSpec(
                    name="page_id", type="str", required=False, default="",
                    description=tr("页面 id（留空＝自动生成；只允许字母数字/-/_）。决定 artifacts 下的目录名。",
                                   "Page id (empty = auto-generate; alnum/-/_ only). Names the folder under artifacts."),
                ),
                ParameterSpec(
                    name="auto_detect_molecules", type="bool", required=False, default=True,
                    description=tr("是否自动找分子极大值叠在图上（供人参考，默认藏起，勾选才显示）。",
                                   "Auto-detect molecule maxima to overlay (shown only when the operator ticks the box)."),
                ),
                ParameterSpec(
                    name="title", type="str", required=False, default="",
                    description=tr("页面标题（留空＝默认「位点标注」）。", "Page title (empty = default)."),
                ),
            ],
            estimated_duration_s=5.0,
            composition_level=1,
            tags=["spectroscopy", "sites", "marking", "web"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        import numpy as np

        frame_path = str(params.get("frame_path", "") or "").strip()
        if not frame_path:
            return SkillResult(
                skill_name="PrepareSiteMarkingPage", success=False,
                error=tr("没有给 frame_path —— 需要一帧 .sxm 才能生成标注页（「取最新帧」尚未接）。",
                         "No frame_path — a .sxm frame is required to build the marking page "
                         "(auto 'latest frame' is not wired yet)."))

        page_id = str(params.get("page_id", "") or "").strip()
        if not page_id:
            import secrets
            page_id = "sm_" + secrets.token_hex(4)
        if not _PAGE_ID_RE.fullmatch(page_id):
            return SkillResult(
                skill_name="PrepareSiteMarkingPage", success=False,
                error=tr(f"page_id 不合法：{page_id!r}（只允许字母数字、- 和 _，≤64）。",
                         f"Invalid page_id {page_id!r} (alnum, -, _ only, <=64)."))

        # ── 读帧 ────────────────────────────────────────────────────────
        try:
            from mast.io.nanonis_files import read_sxm, rows_top_first
            parsed = read_sxm(frame_path)
        except Exception as exc:  # noqa: BLE001 — 读不了就如实报，不臆造
            return SkillResult(
                skill_name="PrepareSiteMarkingPage", success=False,
                error=tr(f"读 .sxm 失败：{exc}", f"Failed to read .sxm: {exc}"))
        header = parsed.get("header", {})
        channels = parsed.get("channels", {})
        zname = next((k for k in channels if str(k).strip().lower().startswith("z")), None)
        zdata = (channels.get(zname) or {}).get("forward") if zname else None
        if zdata is None:
            return SkillResult(
                skill_name="PrepareSiteMarkingPage", success=False,
                error=tr(f"这帧里没有可用的 Z 正扫通道（通道：{list(channels)}）。",
                         f"No usable Z forward channel in this frame (channels: {list(channels)})."),
                data={"channels": list(channels)})

        meta = _build_meta(header, frame_path)
        if meta["w"] is None or meta["cx"] is None or meta["px"] is None:
            return SkillResult(
                skill_name="PrepareSiteMarkingPage", success=False,
                error=tr("头部缺扫描范围/中心/像素数，拼不出页面坐标系。",
                         "Header is missing scan range/offset/pixels; cannot build the page coordinate system."),
                data={"meta": meta})
        # 模板把帧当方形显示（viewBox W×W、frame aspect-ratio:1）。w≠h 的帧会被挤，
        # 几何略失真 —— 如实告警 + 打标记，别默默出一张坐标错的图。
        if meta["h"] and abs(meta["w"] - meta["h"]) > 0.02 * meta["w"]:
            logger.warning("PrepareSiteMarkingPage: 非方形帧 w=%.3f h=%.3f nm —— 标点页按方形显示，"
                           "几何会略失真。", meta["w"], meta["h"])
            meta["nonsquare"] = True

        # 统一成 row0=顶（与 _pix_to_nm、页面 y1 在顶一致），再拉平
        oriented = rows_top_first(np.asarray(zdata, dtype=float), header.get("scan_dir", "down"))
        a = _level(oriented)
        ny, nx = a.shape

        # ── 渲 PNG（复用 webui 的渲染，去掉 data: 前缀取裸 base64）──────────
        from mast.webui.scan_preview import _image_to_png_uri
        uri = _image_to_png_uri(a, size=768, clip=(0.3, 99.8), cmap="afmhot")
        if not uri:
            return SkillResult(
                skill_name="PrepareSiteMarkingPage", success=False,
                error=tr("渲染这帧失败（空图）。", "Failed to render this frame (empty image)."))
        img_b64 = uri.split(",", 1)[1] if uri.startswith("data:") else uri

        # ── 分子 ────────────────────────────────────────────────────────
        mols = []
        if bool(params.get("auto_detect_molecules", True)):
            mols = _molecules(a, meta["w"], meta["h"], meta["cx"], meta["cy"])

        # ── 填模板 ──────────────────────────────────────────────────────
        try:
            template = _TEMPLATE_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            return SkillResult(
                skill_name="PrepareSiteMarkingPage", success=False,
                error=tr(f"读页面模板失败：{exc}", f"Failed to read the page template: {exc}"))
        title = str(params.get("title", "") or "").strip() or tr("位点标注", "Site marking")
        scalebar_nm = max(1, round((meta["w"] or 6) / 6))  # 比例尺 CSS 宽 = 帧宽的 1/6
        repl = {
            "__IMG__": img_b64,
            "__META__": json.dumps(meta, ensure_ascii=True),
            "__MOLS__": json.dumps(mols, ensure_ascii=True),
            "__PAGE_ID__": page_id,
            "__API_BASE__": "",  # 同源：页面由 MAST 自己服务
            "__TITLE__": title,
            "__EYEBROW__": meta.get("file", ""),
            "__H1__": title,
            "__SCALEBAR__": f"{scalebar_nm} nm",
        }
        for _tok, (_zh, _en) in _UI_STRINGS.items():
            repl[_tok] = tr(_zh, _en)
        page_html = template
        for k, v in repl.items():
            page_html = page_html.replace(k, v)
        if "__IMG__" in page_html or "__META__" in page_html:  # 占位符没填干净
            return SkillResult(
                skill_name="PrepareSiteMarkingPage", success=False,
                error=tr("页面模板占位符未全部替换（模板与本 skill 不同步）。",
                         "Some page-template placeholders were not replaced (template out of sync with the skill)."))
        _unfilled = sorted(k for k in _UI_STRINGS if k in page_html)
        if _unfilled:
            return SkillResult(
                skill_name="PrepareSiteMarkingPage", success=False,
                error=tr(f"页面模板有未替换的文案占位符：{_unfilled[:5]}（模板与 _UI_STRINGS 不同步）。",
                         f"Unfilled text placeholders in the page template: {_unfilled[:5]} (template out of sync with _UI_STRINGS)."))

        # ── 落盘：page.html + 空 marks.json（与 B 端点/ C 读取同一目录）──────
        base = site_marking_dir(page_id)
        try:
            base.mkdir(parents=True, exist_ok=True)
            (base / "page.html").write_text(page_html, encoding="utf-8")
            # meta.json：几何（nm/deg）单独落一份，供 ReadSiteMarks 在不给 frame_path 时
            # 拿来换算坐标（页面里也有同一份 META，但 skill 侧读文件更稳）。
            (base / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            marks_file = base / "marks.json"
            if not marks_file.exists():  # 已存在就别覆盖人标过的点
                marks_file.write_text(json.dumps(_EMPTY_DOC, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            return SkillResult(
                skill_name="PrepareSiteMarkingPage", success=False,
                error=tr(f"写页面文件失败：{exc}", f"Failed to write the page files: {exc}"))

        page_url = f"/api/site-marking/{page_id}/page"
        logger.info("PrepareSiteMarkingPage: 生成 %s（%d 个分子，帧 %s）", page_url, len(mols), meta.get("file"))
        return SkillResult(
            skill_name="PrepareSiteMarkingPage", success=True,
            data={
                "page_id": page_id,
                "page_url": page_url,
                "page_path": str(base / "page.html"),
                "marks_path": str(base / "marks.json"),
                "n_molecules": len(mols),
                "meta": meta,
                # 提示调用方：拿这个 URL 让操作员在浏览器里打开标点（MAST 需在本地服务这页）。
                "hint": tr("在浏览器打开 page_url 标点；标完后用 ReadSiteMarks 读回。",
                           "Open page_url in a browser to mark sites; read them back with ReadSiteMarks."),
            })
