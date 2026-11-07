"""对光相机技能（契约 §13.1「相机」）：连接、直播、快照、曝光 / 增益 / ROI、状态。

9 个技能::

    ConnectCamera        AUTO   打开相机（MAST 启动时不碰它，只有这一步才打开）
    DisconnectCamera     AUTO   彻底释放：之后 ThorImageCAM 可以重新打开它
    CameraStartLive      AUTO   软件触发连续采集，后台线程只留最新一帧
    CameraStopLive       AUTO   停止采集（相机仍连着）
    CameraSnapshot       AUTO   存一帧：原始数组 .npy + PNG + 元数据 .json，进当前实验文件夹
    SetCameraExposure    AUTO   曝光（µs），超出相机报告的范围就拒绝
    SetCameraGain        AUTO   增益（dB），同上
    SetCameraROI         AUTO   ROI（传感器像素）与合并，直播中会先停、改完接着播
    GetCameraStatus      AUTO   只读：状态、设置、最新一帧的光斑统计

设备在 :mod:`mast.instruments.camera`（进程级的 :func:`get_camera_manager`），不走 ExecutionContext
—— 那是 Nanonis 的路。

**都不取仪器令牌**（``instrument_lock.OFF_INSTRUMENT_NAMES``）：相机不碰 Nanonis，跟 STM 互不阻塞
—— TERS 组合技能拿着令牌跑的时候照样能看画面、调曝光、拍快照。相机自己的独占由 SDK 管
（同一时刻只有一个进程能打开它）。

**安全级都是 AUTO**：相机只是看，没有执行器，碰不到样品、针尖、激光和任何运动部件；最坏的结果是
画面不好看或者占着相机。打不开（ThorImageCAM 占着）一律拒绝并说明，从不去抢、从不关别的程序。

**中止闩不开口子**：急停 / 中止闩挂着时，技能直调入口拒绝一切技能，相机的也一样
（包括断开）。急停本身不停相机的直播，画面与 /stats 照样能看（它们不走技能）。要把相机交还
ThorImageCAM：先由操作员清闩，或者优雅关停 MAST（atexit 断开相机）。

快照不要求先选样品（没有产数据类标签）：对光往往发生在选样品之前。有活跃样品时文件进它的
``raw/``，没有时留在 ``<数据根>/artifacts/camera/<日期>/``，结果里写明在哪。

没有相机的机器上每个技能都返回失败的 SkillResult 和明确原因，从不抛异常。
"""

from __future__ import annotations

import copy
import io
import re
import threading
import time
from typing import Any

import numpy as np

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.i18n import tr
from mast.instruments.base import InstrumentError, InstrumentUnavailable
from mast.instruments.camera import (
    Camera,
    CameraBusy,
    CameraRefused,
    encode_png,
    get_camera_manager,
    to_8bit,
)
from mast.skills.base import BaseSkill
from mast.skills.builtins.optics_common import (
    abort_probe,
    atomic_write_bytes,
    dump_json_bytes,
    json_safe,
    optics_positions,
    scope_ids,
    staging_dir,
    store_in_experiment,
)

__all__ = [
    "ConnectCamera",
    "DisconnectCamera",
    "CameraStartLive",
    "CameraStopLive",
    "CameraSnapshot",
    "SetCameraExposure",
    "SetCameraGain",
    "SetCameraROI",
    "GetCameraStatus",
]

#: 技能名（instrument_lock.OFF_INSTRUMENT_NAMES 与 hardware_modules 的 ters_optics 按这张表认）。
CAMERA_SKILLS: tuple[str, ...] = tuple(__all__)

#: optional-hardware：归 hardware_modules 的 ters_optics 模块管（没相机的机器上关着，不占 agent 的工具表）。
_TAGS = ["optics", "camera", "alignment", "optional-hardware"]

_CAMERA_ID = ParameterSpec(
    name="camera_id", type="str",
    description=tr("cameras.json 里的 camera_id；只配了一台相机时可以省略",
                   "camera_id from cameras.json; may be omitted when only one camera is configured"),
    required=False,
)

#: 快照里读光学轴位置的总时限（秒）。读不完照样存快照，元数据里写明没读完。
AXES_BUDGET_S = 2.0


def _camera(params: dict) -> Camera:
    return get_camera_manager().camera(params.get("camera_id") or None)


def _fail(skill: str, exc: BaseException, **data: Any) -> SkillResult:
    if isinstance(exc, KeyError):
        msg = str(exc.args[0]) if exc.args else tr("没有这台相机", "No such camera")
    elif isinstance(exc, CameraBusy):
        msg = str(exc)
    elif isinstance(exc, CameraRefused):
        msg = f"refused: {exc}"
    elif isinstance(exc, InstrumentUnavailable):
        msg = f"camera unavailable: {exc}"
    elif isinstance(exc, InstrumentError):
        msg = str(exc)
    else:
        msg = f"{type(exc).__name__}: {exc}"
    return SkillResult(skill_name=skill, success=False, error=msg, data=json_safe(data))


def _settings_line(st: dict) -> str:
    roi = st.get("roi") or {}
    parts = []
    if st.get("exposure_us") is not None:
        parts.append(tr(f"曝光 {st['exposure_us']} µs", f"exposure {st['exposure_us']} µs"))
    if st.get("gain_db") is not None:
        parts.append(tr(f"增益 {st['gain_db']:g} dB", f"gain {st['gain_db']:g} dB"))
    if roi:
        parts.append(f"ROI {roi['w']}×{roi['h']} @ ({roi['x']}, {roi['y']})"
                     + (tr(f" 合并 {roi['binning']}", f" binning {roi['binning']}") if roi.get("binning", 1) != 1 else ""))
    return tr("，", ", ").join(parts)


def _spot_line(stats: dict | None) -> str:
    if not stats:
        return ""
    c, f = stats.get("centroid_px"), stats.get("fwhm_px")
    sat = stats.get("saturated_frac") or 0.0
    if not c:
        txt = tr("没找到光斑", "no spot found")
    else:
        txt = tr(f"光斑质心 ({c[0]:.1f}, {c[1]:.1f}) px", f"spot centroid ({c[0]:.1f}, {c[1]:.1f}) px")
        if f:
            txt += tr(f"，FWHM {f[0]:.1f} × {f[1]:.1f} px", f", FWHM {f[0]:.1f} × {f[1]:.1f} px")
    if sat > 0:
        txt += tr(f"，{sat * 100:.2g}% 像素饱和", f", {sat * 100:.2g}% pixels saturated")
    return txt


def _ok(skill: str, cam: Camera, st: dict, summary: str, **extra: Any) -> SkillResult:
    return SkillResult(skill_name=skill, success=True,
                       data=json_safe({"camera_id": cam.cfg.camera_id, "camera": st, **extra}),
                       summary=summary)


# ── 连接 / 断开 ───────────────────────────────────────────────────────────────

class ConnectCamera(BaseSkill):
    """打开对光相机。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ConnectCamera", version="1.0.0",
            category=SkillCategory.WRITE, safety_level=SafetyLevel.AUTO,
            description=(
                tr("打开对光相机（Thorlabs Zelux 一类）。MAST 启动时不碰相机，只有这一步才打开。同一时刻"
                "只有一个程序能用这台相机：ThorImageCAM 开着时会失败并说明，先请人关掉它——MAST 不会替人关。"
                "已经连着时什么也不做。不取仪器令牌，不影响 STM。",
                   "Open the alignment camera (Thorlabs Zelux or similar). MAST does not touch the camera at startup; only this step opens it. "
                   "Only one program can use the camera at a time: if ThorImageCAM is open this fails with an explanation, so ask someone to close it first - MAST never closes it for them. "
                   "Does nothing if already connected. Does not take the instrument token and does not affect the STM.")),
            parameters=[_CAMERA_ID],
            estimated_duration_s=2.0, composition_level=0,
            tags=[*_TAGS, "connect", "write"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            cam = _camera(params)
            st = cam.connect()
            sensor = st.get("sensor") or {}
            return _ok("ConnectCamera", cam, st,
                       tr(f"相机 {cam.cfg.camera_id} 已连接：{sensor.get('model') or cam.cfg.model} "
                       f"{sensor.get('width')}×{sensor.get('height')}，{sensor.get('bit_depth')} bit",
                          f"camera {cam.cfg.camera_id} connected: {sensor.get('model') or cam.cfg.model} "
                          f"{sensor.get('width')}×{sensor.get('height')}, {sensor.get('bit_depth')} bit")
                       + (tr(f"；{_settings_line(st)}", f"; {_settings_line(st)}") if _settings_line(st) else ""))
        except Exception as exc:  # noqa: BLE001
            return _fail("ConnectCamera", exc)


class DisconnectCamera(BaseSkill):
    """彻底释放对光相机。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="DisconnectCamera", version="1.0.0",
            category=SkillCategory.WRITE, safety_level=SafetyLevel.AUTO,
            description=(
                tr("断开对光相机并彻底释放（停止直播、关相机句柄、关 SDK），之后 ThorImageCAM 可以重新打开它。"
                "没连着时什么也不做；相机处于错误状态时用它复位。急停 / 中止闩挂着时它和所有技能一样被拒"
                "（安全闸门不为相机开口子）：要把相机交还 ThorImageCAM，先请操作员清闩，或者优雅关停 MAST"
                "（退出时会释放相机）。急停不影响 MAST 里正在进行的直播，画面照样能看。",
                   "Disconnect the alignment camera and release it completely (stop live view, close the camera handle, close the SDK) so that ThorImageCAM can open it again. "
                   "Does nothing if not connected; use it to reset the camera when it is in an error state. When an emergency stop / abort latch is set it is refused like every other skill "
                   "(the safety gate makes no exception for the camera): to hand the camera back to ThorImageCAM, ask the operator to clear the latch first, or shut MAST down gracefully "
                   "(the camera is released on exit). An emergency stop does not affect a live view already running in MAST; the image can still be viewed.")),
            parameters=[_CAMERA_ID],
            estimated_duration_s=1.0, composition_level=0,
            tags=[*_TAGS, "disconnect", "write"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            cam = _camera(params)
            st = cam.disconnect()
            return _ok("DisconnectCamera", cam, st,
                       tr(f"相机 {cam.cfg.camera_id} 已断开并释放，ThorImageCAM 现在可以用它",
                          f"camera {cam.cfg.camera_id} disconnected and released; ThorImageCAM can use it now"))
        except Exception as exc:  # noqa: BLE001
            return _fail("DisconnectCamera", exc)


# ── 直播 ──────────────────────────────────────────────────────────────────────

class CameraStartLive(BaseSkill):
    """开始连续采集。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="CameraStartLive", version="1.0.0",
            category=SkillCategory.WRITE, safety_level=SafetyLevel.AUTO,
            description=(
                tr("对光相机开始连续采集（软件触发），后台只保留最新一帧；画面在光学台页的相机面板里看，"
                "或者 GET /api/camera/{camera_id}/frame.jpg。要先 ConnectCamera。",
                   "Start continuous acquisition on the alignment camera (software trigger); only the latest frame is kept in the background. View the image in the camera panel of the optics page, "
                   "or via GET /api/camera/{camera_id}/frame.jpg. ConnectCamera must be run first.")),
            parameters=[_CAMERA_ID],
            estimated_duration_s=0.5, composition_level=0,
            tags=[*_TAGS, "live", "write"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            cam = _camera(params)
            st = cam.start_live()
            return _ok("CameraStartLive", cam, st, tr(f"相机 {cam.cfg.camera_id} 开始直播",
                                                      f"camera {cam.cfg.camera_id} live view started"))
        except Exception as exc:  # noqa: BLE001
            return _fail("CameraStartLive", exc)


class CameraStopLive(BaseSkill):
    """停止连续采集。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="CameraStopLive", version="1.0.0",
            category=SkillCategory.WRITE, safety_level=SafetyLevel.AUTO,
            description=tr("对光相机停止连续采集；相机仍连着（要让给 ThorImageCAM 用 DisconnectCamera）。",
                           "Stop continuous acquisition on the alignment camera; the camera stays connected (use DisconnectCamera to hand it over to ThorImageCAM)."),
            parameters=[_CAMERA_ID],
            estimated_duration_s=0.5, composition_level=0,
            tags=[*_TAGS, "live", "write"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            cam = _camera(params)
            st = cam.stop_live()
            return _ok("CameraStopLive", cam, st, tr(f"相机 {cam.cfg.camera_id} 停止直播",
                                                     f"camera {cam.cfg.camera_id} live view stopped"))
        except Exception as exc:  # noqa: BLE001
            return _fail("CameraStopLive", exc)


# ── 快照 ──────────────────────────────────────────────────────────────────────

def _optical_axes(budget_s: float = AXES_BUDGET_S) -> dict:
    """快照时光学轴的位置（尽力而为、有时限）：

    * :func:`optics_common.optics_positions` —— 偏振片角度（配了一台时读一次）与卫星设备最近一次
      上报的轴状态，和光谱元数据同一套；
    * 本机**已经连着**的光学台逐轴读一次位置。没连的不去连 —— 快照不许顺手打开串口。

    整体限时 ``budget_s``，没读完就带上 ``incomplete``；读不到的写原因，快照照样存。"""
    box: dict[str, Any] = {}
    lock = threading.Lock()
    done = threading.Event()

    def _put(key: str, value: Any) -> None:
        with lock:
            box[key] = value

    def _go() -> None:
        try:
            from mast.instruments.registry import get_instrument_registry

            reg = get_instrument_registry()
            for key, value in optics_positions(reg).items():
                _put(key, value)
            built = reg.built_controllers() if callable(getattr(reg, "built_controllers", None)) else {}
            for dev, ctrl in built.items():
                if callable(getattr(ctrl, "live_status", None)) or not getattr(ctrl, "is_connected", False):
                    continue
                axes: dict[str, Any] = {}
                for name, ax in ctrl.axes().items():
                    try:
                        axes[name] = float(ax.get_position())
                    except Exception as exc:  # noqa: BLE001
                        axes[name] = {"error": f"{type(exc).__name__}: {exc}"}
                with lock:
                    box.setdefault("local", {})[dev] = axes
        except Exception as exc:  # noqa: BLE001
            _put("error", f"{type(exc).__name__}: {exc}")
        finally:
            done.set()

    threading.Thread(target=_go, name="camera-snapshot-axes", daemon=True).start()
    finished = done.wait(budget_s)
    with lock:
        out = copy.deepcopy(json_safe(box))
    if not finished:
        out["incomplete"] = tr(f"{budget_s:g} s 内没读完，后面的轴没记",
                               f"not finished within {budget_s:g} s; the remaining axes were not recorded")
    return out


def _safe_tag(tag: str) -> str:
    return re.sub(r"[^\w.-]+", "_", tag).strip("_.")[:40]


class CameraSnapshot(BaseSkill):
    """存一帧到当前实验文件夹。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="CameraSnapshot", version="1.0.0",
            category=SkillCategory.WRITE, safety_level=SafetyLevel.AUTO,
            description=(
                tr("存对光相机的一帧：原始数组（.npy，uint16，ROI 之后的尺寸）、PNG（灰度，按满量程映射）"
                "与元数据（.json：曝光、增益、ROI、时间、传感器、光斑质心 / FWHM、当时的光学轴位置）。"
                "取的是调用之后、设置生效之后拍到的那一帧。有活跃样品时进它的 raw/，没有时留在"
                "数据根的 artifacts/camera/ 下，结果里写明位置。相机连着但没在直播时会临时开一下。",
                   "Save one frame from the alignment camera: the raw array (.npy, uint16, size after ROI), a PNG (grayscale, mapped over the full range) "
                   "and metadata (.json: exposure, gain, ROI, time, sensor, spot centroid / FWHM, optical axis positions at that time). "
                   "The frame saved is the one captured after the call and after the settings took effect. With an active sample it goes into its raw/ folder; otherwise it stays under "
                   "artifacts/camera/ in the data root, and the result states the location. If the camera is connected but not live, it is started briefly.")),
            parameters=[
                _CAMERA_ID,
                ParameterSpec(name="tag", type="str", required=False,
                              description=tr("写进文件名和元数据的简短标记，例如 'after_lens_z+20'",
                                             "Short tag written into the file name and metadata, e.g. 'after_lens_z+20'")),
            ],
            estimated_duration_s=1.5, composition_level=1,
            tags=[*_TAGS, "snapshot", "write"])

    def execute(self, context, params: dict) -> SkillResult:
        name = "CameraSnapshot"
        try:
            cam = _camera(params)
            started_here = False
            if not cam.live:
                cam.start_live()          # 没连 / 出错时它抛出清楚的原因
                started_here = True
            try:
                frame = self._wait_settled_frame(cam, context)
            finally:
                if started_here:
                    cam.stop_live()
            if frame is None:
                aborted = (abort_probe(context) or (lambda: False))()
                return SkillResult(skill_name=name, success=False, data={"camera_id": cam.cfg.camera_id},
                                   error=(tr("aborted: 快照被中止", "aborted: snapshot aborted") if aborted
                                          else tr("没等到新的一帧（相机停了、出错了，或者曝光太长）",
                                                  "No new frame arrived (the camera stopped, errored, or the exposure is too long)")))
            tag = str(params.get("tag") or "").strip()
            return self._save(cam, frame, tag)
        except Exception as exc:  # noqa: BLE001
            return _fail(name, exc)

    @staticmethod
    def _wait_settled_frame(cam: Camera, context: Any):
        exposure_s = (cam.status().get("exposure_us") or 0) / 1e6
        deadline = time.monotonic() + min(120.0, 5.0 + 4.0 * exposure_s)
        after = cam.last_seq
        probe = abort_probe(context)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            f = cam.wait_frame(max(after, cam.settled_after), left, check_abort=probe)
            if f is None or f.settled:
                return f
            after = f.seq

    @staticmethod
    def _save(cam: Camera, frame, tag: str) -> SkillResult:
        stats = cam.frame_stats(frame)
        st = cam.status()
        cid = cam.cfg.camera_id
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(frame.t))
        stem = f"camera_{cid}_{stamp}_f{frame.seq}" + (f"_{_safe_tag(tag)}" if _safe_tag(tag) else "")
        d = staging_dir("camera")
        npy, png, meta = d / f"{stem}.npy", d / f"{stem}.png", d / f"{stem}.json"
        buf = io.BytesIO()
        np.save(buf, frame.raw, allow_pickle=False)
        atomic_write_bytes(npy, buf.getvalue())
        atomic_write_bytes(png, encode_png(to_8bit(cam.gray(frame), full_scale=frame.full_scale)))
        metadata = {
            "kind": "camera_snapshot", "camera_id": cid, "tag": tag or None,
            "frame_seq": frame.seq, "t": frame.t,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(frame.t)),
            "exposure_us": frame.exposure_us, "gain_db": frame.gain_db,
            "roi": {"x": frame.roi[0], "y": frame.roi[1], "w": frame.roi[2], "h": frame.roi[3]},
            "binning": frame.binning, "settled": frame.settled,
            "sensor": st.get("sensor"), "backend": cam.cfg.backend,
            "raw": {"file": npy.name, "dtype": str(frame.raw.dtype), "shape": list(frame.raw.shape),
                    "bit_depth": frame.bit_depth, "mosaic": frame.mosaic},
            "png": {"file": png.name,
                    "mapping": (tr("灰度：彩色传感器 2×2 合成（一 R 两 G 一 B 取平均）；0…满量程线性映射到 0…255",
                                   "grayscale: color sensor combined 2×2 (one R, two G, one B averaged); linear map 0...full scale to 0...255")
                                if frame.mosaic else tr("灰度：0…满量程线性映射到 0…255", "grayscale: linear map 0...full scale to 0...255"))},
            "spot": {k: stats.get(k) for k in (
                "centroid_px", "centroid_sensor_px", "fwhm_px", "fwhm_major_px", "fwhm_minor_px",
                "angle_deg", "max", "saturated_frac", "background", "noise", "peak", "spot",
                "frame_w", "frame_h", "pixel_scale")},
            "coordinates": (tr("centroid_px / fwhm_px：传感器像素，原点是 ROI 左上角像素的中心；"
                            "centroid_sensor_px = ROI 偏移 + centroid_px",
                               "centroid_px / fwhm_px: sensor pixels, origin at the center of the top-left ROI pixel; "
                               "centroid_sensor_px = ROI offset + centroid_px")),
            "optical_axes": _optical_axes(),
            "scope": scope_ids(),
        }
        atomic_write_bytes(meta, dump_json_bytes(metadata))
        stored = store_in_experiment([npy, png, meta], skill="CameraSnapshot")
        paths = stored.get("paths") or [str(npy), str(png), str(meta)]
        where = (tr(f"已存进当前实验（{stored.get('sample_dir')}）", f"stored in the current experiment ({stored.get('sample_dir')})") if stored.get("stored")
                 else tr(f"没有活跃样品，留在 {d}", f"no active sample, kept in {d}"))
        spot = _spot_line(stats)
        return SkillResult(
            skill_name="CameraSnapshot", success=True,
            data=json_safe({"camera_id": cid, "frame_seq": frame.seq, "files": {
                "raw": paths[0], "png": paths[1], "metadata": paths[2]},
                "stored_in_experiment": bool(stored.get("stored")), "stored": stored,
                "spot": metadata["spot"], "exposure_us": frame.exposure_us,
                "gain_db": frame.gain_db, "roi": metadata["roi"], "optical_axes": metadata["optical_axes"]}),
            summary=tr(f"快照 #{frame.seq} {where}", f"snapshot #{frame.seq} {where}") + (tr(f"；{spot}", f"; {spot}") if spot else ""),
            images=[paths[1]])


# ── 设置 ──────────────────────────────────────────────────────────────────────

class SetCameraExposure(BaseSkill):
    """设曝光。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetCameraExposure", version="1.0.0",
            category=SkillCategory.WRITE, safety_level=SafetyLevel.AUTO,
            description=(
                tr("设对光相机的曝光时间（µs）。超出相机自己报告的范围就拒绝（不夹到边上）；返回读回的实际值。"
                "直播中改了之后，头几帧可能还是旧曝光拍的（统计里 settled=false），快照会等它稳定。",
                   "Set the exposure time of the alignment camera (µs). Values outside the range the camera reports are rejected (not clamped); returns the value read back. "
                   "After changing it during live view, the first few frames may still use the old exposure (settled=false in the stats); a snapshot waits until it settles.")),
            parameters=[
                ParameterSpec(name="exposure_us", type="int", unit="µs", min_value=1,
                              max_value=60_000_000, description=tr("曝光时间；实际可用范围以相机报告的为准",
                                                                   "Exposure time; the usable range is whatever the camera reports")),
                _CAMERA_ID,
            ],
            estimated_duration_s=0.3, composition_level=0,
            tags=[*_TAGS, "exposure", "write"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            cam = _camera(params)
            st = cam.set_exposure(int(params["exposure_us"]))
            return _ok("SetCameraExposure", cam, st, tr(f"相机 {cam.cfg.camera_id} 曝光 {st['exposure_us']} µs",
                                                        f"camera {cam.cfg.camera_id} exposure {st['exposure_us']} µs"))
        except Exception as exc:  # noqa: BLE001
            return _fail("SetCameraExposure", exc)


class SetCameraGain(BaseSkill):
    """设增益。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetCameraGain", version="1.0.0",
            category=SkillCategory.WRITE, safety_level=SafetyLevel.AUTO,
            description=(
                tr("设对光相机的增益（dB）。超出相机自己报告的范围就拒绝；SDK 按档位换算，返回读回的实际值。",
                   "Set the gain of the alignment camera (dB). Values outside the range the camera reports are rejected; the SDK converts to its gain steps and the value read back is returned.")),
            parameters=[
                ParameterSpec(name="gain_db", type="float", unit="dB", min_value=0.0, max_value=100.0,
                              description=tr("增益；实际可用范围以相机报告的为准", "Gain; the usable range is whatever the camera reports")),
                _CAMERA_ID,
            ],
            estimated_duration_s=0.3, composition_level=0,
            tags=[*_TAGS, "gain", "write"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            cam = _camera(params)
            st = cam.set_gain(float(params["gain_db"]))
            return _ok("SetCameraGain", cam, st, tr(f"相机 {cam.cfg.camera_id} 增益 {st['gain_db']:g} dB",
                                                    f"camera {cam.cfg.camera_id} gain {st['gain_db']:g} dB"))
        except Exception as exc:  # noqa: BLE001
            return _fail("SetCameraGain", exc)


class SetCameraROI(BaseSkill):
    """设 ROI 与合并。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetCameraROI", version="1.0.0",
            category=SkillCategory.WRITE, safety_level=SafetyLevel.AUTO,
            description=(
                tr("设对光相机的感兴趣区域（传感器像素，左上角 x、y 与宽高 w、h）和合并。彩色传感器的 x、y、w、h "
                "要是偶数（保持滤色片排布），也不做合并。恢复满幅：x=0、y=0、w、h 取传感器宽高。直播中会先停、"
                "改完接着播；返回读回的实际 ROI。",
                   "Set the region of interest of the alignment camera (sensor pixels: top-left x, y and width/height w, h) and binning. For color sensors x, y, w, h "
                   "must be even (to keep the color filter layout), and binning is not applied. To restore the full frame: x=0, y=0, w and h equal to the sensor width and height. "
                   "Live view is stopped first and resumed after the change; returns the ROI read back.")),
            parameters=[
                ParameterSpec(name="x", type="int", unit="px", min_value=0, max_value=100_000,
                              description=tr("左上角列号（传感器像素，从 0 起）",
                                             "Left column of the top-left corner (sensor pixels, counted from 0)")),
                ParameterSpec(name="y", type="int", unit="px", min_value=0, max_value=100_000,
                              description=tr("左上角行号（传感器像素，从 0 起）",
                                             "Top row of the top-left corner (sensor pixels, counted from 0)")),
                ParameterSpec(name="w", type="int", unit="px", min_value=1, max_value=100_000,
                              description=tr("宽", "Width")),
                ParameterSpec(name="h", type="int", unit="px", min_value=1, max_value=100_000,
                              description=tr("高", "Height")),
                ParameterSpec(name="binning", type="int", required=False, default=1, min_value=1,
                              max_value=16, description=tr("合并（两个方向相同）；实际可用范围以相机报告的为准",
                                                           "Binning (same in both directions); the usable range is whatever the camera reports")),
                _CAMERA_ID,
            ],
            estimated_duration_s=0.5, composition_level=0,
            tags=[*_TAGS, "roi", "write"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            cam = _camera(params)
            binning = params.get("binning")
            st = cam.set_roi(int(params["x"]), int(params["y"]), int(params["w"]), int(params["h"]),
                             int(binning) if binning is not None else 1)
            return _ok("SetCameraROI", cam, st, tr(f"相机 {cam.cfg.camera_id}：{_settings_line(st)}",
                                                   f"camera {cam.cfg.camera_id}: {_settings_line(st)}"))
        except Exception as exc:  # noqa: BLE001
            return _fail("SetCameraROI", exc)


# ── 状态 ──────────────────────────────────────────────────────────────────────

class GetCameraStatus(BaseSkill):
    """只读：相机状态与最新一帧的光斑统计。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetCameraStatus", version="1.0.0",
            category=SkillCategory.READ, safety_level=SafetyLevel.AUTO,
            description=(
                tr("只读：对光相机的状态（连没连、在不在直播、出错原因、谁占着）、当前曝光 / 增益 / ROI、"
                "帧率，在直播时再给最新一帧的光斑质心、FWHM、最大值与饱和比例。不碰设备。"
                "不给 camera_id 时列出所有配置的相机。",
                   "Read-only: status of the alignment camera (connected or not, live or not, error reason, who occupies it), current exposure / gain / ROI, "
                   "frame rate, and, while live, the latest frame's spot centroid, FWHM, maximum and saturated fraction. Does not touch the device. "
                   "Without camera_id, lists all configured cameras.")),
            parameters=[_CAMERA_ID],
            estimated_duration_s=0.1, composition_level=0,
            tags=[*_TAGS, "read", "status"])

    def execute(self, context, params: dict) -> SkillResult:
        try:
            mgr = get_camera_manager()
            if not params.get("camera_id") and len(mgr.ids()) != 1:
                cams = [mgr.camera(cid).status() for cid in mgr.ids()]
                return SkillResult(skill_name="GetCameraStatus", success=True,
                                   data=json_safe({"cameras": cams}),
                                   summary=(tr(f"{len(cams)} 台相机：", f"{len(cams)} camera(s):") + tr("，", ", ").join(
                                       f"{c['camera_id']} {c['state']}" for c in cams)) if cams
                                   else tr("没有配置相机", "no cameras configured"))
            cam = _camera(params)
            st = cam.status()
            spot = None
            frame = cam.latest_frame()
            if frame is not None:
                full = cam.frame_stats(frame)
                spot = {k: full.get(k) for k in (
                    "centroid_px", "centroid_sensor_px", "fwhm_px", "max", "saturated_frac",
                    "frame_seq", "t", "settled", "frame_w", "frame_h")}
            line = tr(f"相机 {cam.cfg.camera_id}：{st['state']}", f"camera {cam.cfg.camera_id}: {st['state']}")
            if st.get("error"):
                line += tr(f"（{st['error']}）", f" ({st['error']})")
            if st.get("occupied_by"):
                line += tr(f"；{st['occupied_by']} 在运行，可能占着相机",
                           f"; {st['occupied_by']} is running and may be occupying the camera")
            if _settings_line(st):
                line += tr(f"；{_settings_line(st)}", f"; {_settings_line(st)}")
            if spot:
                line += tr(f"；{_spot_line(spot)}", f"; {_spot_line(spot)}")
            return _ok("GetCameraStatus", cam, st, line, spot=spot)
        except Exception as exc:  # noqa: BLE001
            return _fail("GetCameraStatus", exc)
