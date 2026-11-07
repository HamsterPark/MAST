"""对光相机：Thorlabs 科学相机（Zelux CS165CU 一类）的驱动后端、实时取帧与光斑统计。

契约：docs/v2/design/ters_satellite_integration.md §13.1「相机」。

规矩
----
* **默认不碰相机**：MAST 启动、列设备、读状态都不打开相机；只有 ``ConnectCamera``（也就是
  :meth:`Camera.connect`）才打开。同一时刻只能有一个进程用这台相机，打不开时只读地查一眼
  进程表：ThorImageCAM 在跑就报「被 ThorImageCAM 占用，请先关掉它」。**从不替人关任何程序。**
* **断开即彻底释放**：停取帧线程 → disarm → 关相机句柄 → 关 SDK（引用计数归零时），之后
  ThorImageCAM 可以重新打开它。
* **厂家接口只用有文档的部分**：``tsi`` 后端按 Thorlabs Scientific Camera SDK 的 C 接口用 ctypes
  运行时晚绑定 ``thorlabs_tsi_camera_sdk.dll``，不反编译任何东西。函数签名核对自 Thorlabs 官方
  示例仓库（github.com/Thorlabs/Camera_Examples：C++ 的 Hardware_Triggering / Read_Parameters，
  Python 的 grab_frames_polling_continuous / windows_setup）；官方示例里没出现的几个函数和枚举
  数值标为「次要来源」（:data:`_SECONDARY`），解析不到就走退路，并列入维护窗口核对清单。
* **取帧线程只留最新一帧**：软件触发连续采集（frames_per_trigger=0、arm(2)、发一次软件触发），
  线程不停地取，盖掉上一帧。灰度、统计、JPEG 都在有人要的时候才从这一帧现算，按帧号缓存。
* **彩色（Bayer）默认先做 2×2 合成灰度**：每个 2×2 块恰好一 R 两 G 一 B，取平均即可，与滤色片
  相位无关；够对焦用。代价：单色光（例如红色激光几乎只亮 R 像素）时质心会朝块中心偏，最多
  半个传感器像素，对同一种颜色是个常数 —— 对焦、看光斑相对移动不受影响，要亚像素的绝对位置
  得另做按相位的去马赛克。预览可切 ``mode=color``：RGGB 双线性解马赛克成全分辨率 RGB JPEG，
  且不下采样（忽略 ``max_w``）；质心 / FWHM 仍始终走灰度路径。

坐标
----
``centroid_px`` / ``fwhm_px`` 的单位是**传感器像素**（与 2×2 合成、合并、JPEG 缩放都无关），
原点是 ROI 左上角那个像素的**中心** —— 也就是 frame.jpg 画面里的位置；``frame_w`` / ``frame_h``
是这幅画面在传感器上跨的像素数，前端画标记按 ``x / frame_w``。传感器上的绝对坐标另给
``centroid_sensor_px``（= ROI 偏移 + ``centroid_px``）。灰度图像素 i 的中心在 ROI 内是
``(i + 0.5)·s − 0.5``，``s`` = 合并 ×（马赛克时 2，否则 1），见 :attr:`Frame.pixel_scale`。
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import io
import json
import logging
import math
import os
import re
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import astuple, dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from mast.instruments.base import InstrumentError, InstrumentUnavailable
from mast.i18n import tr

logger = logging.getLogger(__name__)

#: ThorImageCAM 安装时把 SDK 的 DLL 放在这里（契约 §13.1）。cameras.json 的 ``dll_dir`` 可改。
DEFAULT_DLL_DIR = r"C:\Program Files\Thorlabs\ThorImageCAM\Bin"
SDK_DLL = "thorlabs_tsi_camera_sdk.dll"
CONFIG_REL = Path("config") / "cameras.json"
#: 已知会独占相机的厂家程序（小写进程名 → 显示名）。只读查，从不关。
HOLDER_PROCESSES: dict[str, str] = {"thorimagecam.exe": "ThorImageCAM", "thorcam.exe": "ThorCam"}
BACKENDS = ("tsi", "fake")
_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,40}")
#: 二阶矩宽度只用背景之上、超过峰值这个比例的像素（截断再按高斯修正，见 :func:`spot_stats`）。
_MOMENT_FLOOR = 0.05
_FWHM_PER_SIGMA = 2.0 * math.sqrt(2.0 * math.log(2.0))   # 2.3548…
#: 改了曝光 / 增益 / ROI 之后，这么多帧以内的画面可能还是旧设置拍的（SDK 的帧缓冲 2 帧 + 正在曝光的 1 帧）。
SETTLE_FRAMES = 3
#: 设备调用排队的上限：超过就当 SDK 卡住了，报错而不是把请求线程永远挂住。
IO_WAIT_S = 10.0


class CameraBusy(InstrumentUnavailable):
    """相机被别的进程占着（有证据：厂家程序的进程在跑）。"""

    def __init__(self, holder: str, detail: str = "") -> None:
        self.holder = holder
        msg = tr(f"相机被 {holder} 占用，请先关掉它（MAST 不会替你关）", f"The camera is in use by {holder}; close it first (MAST will not close it for you)")
        super().__init__(f"{msg}{tr('；', '; ')}{detail}" if detail else msg)


class CameraRefused(InstrumentError):
    """参数超出相机自己报告的范围 —— 拒绝，不悄悄夹到边上。"""


class NoCameraFound(InstrumentUnavailable):
    """SDK 一台相机都没发现（没插上，或者正被别的进程占着）。"""


class CameraOpenRefused(InstrumentUnavailable):
    """发现了相机，但打不开（多半是别的进程正用着）。"""


# ── 配置 ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CameraConfig:
    camera_id: str = "zelux"
    backend: str = "tsi"
    enabled: bool = False
    label: str = tr("对光相机", "Alignment camera")
    vendor: str = "Thorlabs"
    model: str = "Zelux CS165CU"
    serial: str | None = None          # None = SDK 发现的第一台
    dll_dir: str = DEFAULT_DLL_DIR
    poll_timeout_ms: int = 100         # 取帧线程每次最多等这么久（也是控制命令最长的排队时间）
    fake: dict = field(default_factory=dict)


def config_path(root: str | os.PathLike | None = None) -> Path:
    """``<数据根>/config/cameras.json``（与卫星枢纽的 satellites.json 同一个数据根）。"""
    if root:
        return Path(root) / CONFIG_REL
    for var in ("MAST2_PROJECT_ROOT", "MAST_PROJECT_ROOT"):
        v = os.environ.get(var, "").strip()
        if v:
            return Path(v) / CONFIG_REL
    try:
        from mast._runtime_paths import project_root

        return Path(project_root()) / CONFIG_REL
    except Exception:  # noqa: BLE001 — 脚本模式
        return Path.cwd() / CONFIG_REL


def _sdk_installed() -> bool:
    try:
        return os.path.isfile(os.path.join(DEFAULT_DLL_DIR, SDK_DLL))
    except OSError:
        return False


def load_camera_configs(path: str | os.PathLike | None = None) -> list[CameraConfig]:
    """读 cameras.json。

    格式 ``{"cameras": [{"camera_id", "backend", "enabled", "serial", "dll_dir", "label", "fake"…}]}``；
    ``enabled`` 缺省 false，``dll_dir`` 缺省 ThorImageCAM 的安装目录。坏条目跳过并记一条 WARNING
    —— 配置写错不许让服务起不来。

    文件不存在时：装了 ThorImageCAM（缺省目录里有 SDK 的 DLL，只看文件在不在）就给一台**未启用**的
    缺省相机，让面板上看得到它和「未启用」的原因；没装就一台都没有。"""
    p = Path(path) if path else config_path()
    if not p.exists():
        return [CameraConfig()] if _sdk_installed() else []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("cameras.json 读不了（%s）：%s —— 当作没有配置相机", p, exc)
        return []
    out: list[CameraConfig] = []
    seen: set[str] = set()
    for i, ent in enumerate((raw or {}).get("cameras") or []):
        try:
            cid = str(ent.get("camera_id") or "")
            if not _ID_RE.fullmatch(cid) or cid in seen:
                raise ValueError(tr(f"camera_id 不合法或重复：{cid!r}", f"camera_id is invalid or duplicated: {cid!r}"))
            backend = str(ent.get("backend") or "tsi")
            if backend not in BACKENDS:
                raise ValueError(tr(f"backend 必须是 {BACKENDS} 之一：{backend!r}", f"backend must be one of {BACKENDS}: {backend!r}"))
            serial = ent.get("serial")
            cfg = CameraConfig(
                camera_id=cid, backend=backend, enabled=ent.get("enabled", False) is True,
                label=str(ent.get("label") or tr("对光相机", "Alignment camera")), vendor=str(ent.get("vendor") or "Thorlabs"),
                model=str(ent.get("model") or "Zelux CS165CU"),
                serial=str(serial) if serial not in (None, "") else None,
                dll_dir=str(ent.get("dll_dir") or DEFAULT_DLL_DIR),
                poll_timeout_ms=max(10, min(1000, int(ent.get("poll_timeout_ms") or 100))),
                fake=dict(ent.get("fake") or {}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cameras.json 第 %d 条跳过：%s", i, exc)
            continue
        seen.add(cid)
        out.append(cfg)
    return out


# ── 帧与纯计算 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Frame:
    """一帧原始数据与拍它时的设置。``raw`` 是相机给的 uint16（ROI、合并之后的尺寸）。"""

    seq: int
    t: float
    raw: np.ndarray
    roi: tuple[int, int, int, int]       # x, y, w, h（传感器像素）
    binning: int
    mosaic: bool
    bit_depth: int
    exposure_us: int | None
    gain_db: float | None
    #: False = 设置改过之后的头几帧，可能还是旧设置拍的（见 :data:`SETTLE_FRAMES`）。
    settled: bool = True

    @property
    def full_scale(self) -> int:
        return (1 << int(self.bit_depth)) - 1

    @property
    def pixel_scale(self) -> int:
        """灰度图的一个像素在传感器上占几个像素（一边）。"""
        return int(self.binning) * (2 if self.mosaic else 1)


def gray_image(raw: np.ndarray, mosaic: bool) -> np.ndarray:
    """对焦用灰度（float32，原始 ADU 量纲）。马赛克传感器：2×2 块取平均（一 R 两 G 一 B）。"""
    a = np.asarray(raw)
    if not mosaic:
        return a.astype(np.float32, copy=False)
    h, w = (a.shape[0] // 2) * 2, (a.shape[1] // 2) * 2
    a = a[:h, :w].astype(np.float32)
    return 0.25 * (a[0::2, 0::2] + a[0::2, 1::2] + a[1::2, 0::2] + a[1::2, 1::2])


def _fill_bayer_plane(plane: np.ndarray) -> np.ndarray:
    """把 Bayer 平面里的 NaN 用邻域均值填上（两遍：先十字邻域，再含对角）。"""
    out = plane.astype(np.float32, copy=True)
    for _ in range(2):
        mask = np.isnan(out)
        if not mask.any():
            break
        p = np.pad(out, 1, constant_values=np.nan)
        neigh = np.stack(
            (p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:],
             p[:-2, :-2], p[:-2, 2:], p[2:, :-2], p[2:, 2:]),
            axis=0,
        )
        with np.errstate(all="ignore"):
            avg = np.nanmean(neigh, axis=0)
        out = np.where(mask, avg, out)
    return np.nan_to_num(out, nan=0.0)


def demosaic_rggb(raw: np.ndarray) -> np.ndarray:
    """RGGB Bayer → 全分辨率 RGB（float32，ADU）。与 FakeCameraBackend / Zelux 默认一致。"""
    a = np.asarray(raw, dtype=np.float32)
    h, w = (a.shape[0] // 2) * 2, (a.shape[1] // 2) * 2
    a = a[:h, :w]
    r = np.full((h, w), np.nan, dtype=np.float32)
    g = np.full((h, w), np.nan, dtype=np.float32)
    b = np.full((h, w), np.nan, dtype=np.float32)
    r[0::2, 0::2] = a[0::2, 0::2]
    g[0::2, 1::2] = a[0::2, 1::2]
    g[1::2, 0::2] = a[1::2, 0::2]
    b[1::2, 1::2] = a[1::2, 1::2]
    return np.stack((_fill_bayer_plane(r), _fill_bayer_plane(g), _fill_bayer_plane(b)), axis=-1)


def color_image(raw: np.ndarray, mosaic: bool) -> np.ndarray:
    """预览用 RGB（float32，ADU，全分辨率）。非马赛克：灰度叠成三通道。"""
    a = np.asarray(raw)
    if not mosaic:
        g = a.astype(np.float32, copy=False)
        return np.stack((g, g, g), axis=-1)
    return demosaic_rggb(a)


def _median3(a: np.ndarray, axis: int) -> np.ndarray:
    """沿一个方向的 3 点中值（边缘按复制填充）：median(x, y, z) = max(min(x, y), min(max(x, y), z))。"""
    if axis == 1:
        p = np.pad(a, ((0, 0), (1, 1)), mode="edge")
        x, y, z = p[:, :-2], p[:, 1:-1], p[:, 2:]
    else:
        p = np.pad(a, ((1, 1), (0, 0)), mode="edge")
        x, y, z = p[:-2], p[1:-1], p[2:]
    return np.maximum(np.minimum(x, y), np.minimum(np.maximum(x, y), z))


def despeckle(a: np.ndarray) -> np.ndarray:
    """先横向、再纵向的 3 点中值：孤立的热像素（以及一两个像素的小簇）去掉，几个像素宽的光斑留下。
    和 3×3 中值滤波效果相当，开销小一个数量级（满幅 1440×1080 也只要几毫秒）。"""
    return _median3(_median3(a, 1), 0)


def spot_stats(gray: np.ndarray, *, raw: np.ndarray | None = None, full_scale: int,
               scale: float = 1.0) -> dict:
    """光斑统计。坐标是 ROI 内的传感器像素（灰度像素 i 的中心在 ``(i + 0.5)·scale − 0.5``）。

    * 背景 = 中位数（光斑占画面一半以上时它就不是背景了 —— 对光时光斑小，够用）；
      噪声 = 1.4826·MAD。两者都在约 10 万个均匀抽取的像素上算。
    * 峰值取 :func:`despeckle` 之后的最大值：孤立的热像素进不了峰值，也进不了下面的连通域。
      峰值高出背景不到 5 倍噪声（至少 1 ADU）就算「没有光斑」，质心和宽度给 None。
    * 质心：阈值取半高（背景 + 0.5·(峰 − 背景)），只用**峰所在的那块连通域**里的像素，按阈值以上的
      强度（I − 阈值）加权 —— 别处的反射光斑、热像素不把质心拽走。
    * 宽度：峰所在、高出背景 5% 峰值的连通域上，背景扣除后的二阶矩 σ，FWHM = 2.355σ。截断会让
      二阶矩偏小，按二维高斯在 f 处截断的解析因子 ``(1 − f(1 + ln 1/f)) / (1 − f)`` 修正 —— 对高斯
      光斑给出真实的 FWHM，对别的形状就是它的二阶矩宽度。另给主轴 / 次轴宽度与主轴角度。
    * ``max`` / ``saturated_frac`` 取自原始像素（饱和要在合成之前看）。
    """
    from scipy import ndimage  # noqa: PLC0415 — 只在要统计的时候才用

    g = np.asarray(gray, dtype=np.float32)
    src = np.asarray(raw) if raw is not None else g
    out: dict[str, Any] = {
        "max": float(src.max()) if src.size else None,
        "saturated_frac": float(np.count_nonzero(src >= full_scale)) / src.size if src.size else None,
        "mean": float(g.mean()) if g.size else None,
        "background": None, "noise": None, "peak": None, "spot": False,
        "centroid_px": None, "fwhm_px": None,
        "fwhm_major_px": None, "fwhm_minor_px": None, "angle_deg": None,
    }
    if g.ndim != 2 or min(g.shape) < 3:
        return out
    step = max(1, math.ceil(math.sqrt(g.size / 100_000)))
    sample = g[::step, ::step]
    bg = float(np.median(sample))
    noise = 1.4826 * float(np.median(np.abs(sample - bg)))
    sm = despeckle(g)
    pk = np.unravel_index(int(np.argmax(sm)), sm.shape)
    amp = float(sm[pk]) - bg
    out.update(background=bg, noise=noise, peak=float(sm[pk]))
    if amp <= 5.0 * max(noise, 1.0):
        return out
    out["spot"] = True

    half = bg + 0.5 * amp
    lab, _ = ndimage.label(sm > half)
    yy, xx = np.nonzero(lab == lab[pk])
    w = np.clip(g[yy, xx].astype(np.float64) - half, 0.0, None)
    sw = float(w.sum())
    if sw <= 0:
        return out
    ci, cj = float((w * xx).sum() / sw), float((w * yy).sum() / sw)
    out["centroid_px"] = [(ci + 0.5) * scale - 0.5, (cj + 0.5) * scale - 0.5]

    lab2, _ = ndimage.label(sm > bg + _MOMENT_FLOOR * amp)
    yy, xx = np.nonzero(lab2 == lab2[pk])
    w2 = np.clip(g[yy, xx].astype(np.float64) - bg, 0.0, None)
    s2 = float(w2.sum())
    if s2 <= 0 or w2.size < 3:
        return out
    x2, y2 = xx.astype(np.float64), yy.astype(np.float64)
    mx, my = float((w2 * x2).sum() / s2), float((w2 * y2).sum() / s2)
    vxx = float((w2 * (x2 - mx) ** 2).sum() / s2)
    vyy = float((w2 * (y2 - my) ** 2).sum() / s2)
    vxy = float((w2 * (x2 - mx) * (y2 - my)).sum() / s2)
    f = _MOMENT_FLOOR
    corr = (1.0 - f * (1.0 + math.log(1.0 / f))) / (1.0 - f)
    k = _FWHM_PER_SIGMA * scale / math.sqrt(corr)
    out["fwhm_px"] = [k * math.sqrt(max(vxx, 0.0)), k * math.sqrt(max(vyy, 0.0))]
    tr, det = vxx + vyy, vxx * vyy - vxy * vxy
    disc = math.sqrt(max(tr * tr / 4.0 - det, 0.0))
    l1, l2 = tr / 2.0 + disc, max(tr / 2.0 - disc, 0.0)
    out["fwhm_major_px"], out["fwhm_minor_px"] = k * math.sqrt(l1), k * math.sqrt(l2)
    out["angle_deg"] = math.degrees(0.5 * math.atan2(2.0 * vxy, vxx - vyy))
    return out


def to_8bit(img: np.ndarray, *, full_scale: int, scale: str = "full") -> np.ndarray:
    """``full``：0…满量程线性映射（亮度如实，饱和看得见）；``auto``：0.5%–99.9% 分位拉伸。

    接受灰度 ``(H, W)`` 或 RGB ``(H, W, 3)``；拉伸时分位按亮度（RGB 三通道均值）算。
    """
    a = np.asarray(img, dtype=np.float32)
    if scale == "auto" and a.size:
        sample = a.mean(axis=-1) if a.ndim == 3 else a
        lo, hi = (float(v) for v in np.percentile(sample, [0.5, 99.9]))
        if hi <= lo:
            hi = lo + 1.0
    else:
        lo, hi = 0.0, float(max(full_scale, 1))
    return np.clip((a - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)


def encode_jpeg(img8: np.ndarray, *, max_w: int | None, quality: int) -> bytes:
    from PIL import Image  # noqa: PLC0415 — 只在要画面的时候才用

    arr = np.ascontiguousarray(img8)
    im = Image.fromarray(arr, mode="RGB" if arr.ndim == 3 else "L")
    if max_w and im.width > max_w:
        h = max(1, round(im.height * max_w / im.width))
        im = im.resize((int(max_w), int(h)), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=int(max(10, min(95, quality))))
    return buf.getvalue()


def encode_png(img8: np.ndarray) -> bytes:
    from PIL import Image  # noqa: PLC0415

    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(img8)).save(buf, format="PNG")
    return buf.getvalue()


# ── 后端 ──────────────────────────────────────────────────────────────────────

@dataclass
class SensorInfo:
    model: str = ""
    serial: str = ""
    name: str = ""
    firmware: str = ""
    width: int = 0
    height: int = 0
    bit_depth: int = 10
    mosaic: bool = False
    sensor_type: str = "monochrome"
    exposure_range_us: tuple[int, int] = (1, 1)
    gain_range_db: tuple[float, float] | None = None     # None = 不支持增益
    binning_range: tuple[int, int] = (1, 1)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["exposure_range_us"] = list(self.exposure_range_us)
        d["gain_range_db"] = list(self.gain_range_db) if self.gain_range_db else None
        d["binning_range"] = list(self.binning_range)
        return d


class CameraBackend(ABC):
    """一台相机的设备操作。:class:`Camera` 保证同一时刻只有一个线程调它。"""

    @abstractmethod
    def open(self) -> SensorInfo:
        """打开相机。失败时自己把已占的资源放掉再抛。"""

    @abstractmethod
    def close(self) -> None:
        """释放全部资源；可以重复调；不抛异常。"""

    @abstractmethod
    def get_exposure_us(self) -> int: ...

    @abstractmethod
    def set_exposure_us(self, us: int) -> int:
        """返回读回的值。"""

    @abstractmethod
    def get_gain_db(self) -> float | None: ...

    @abstractmethod
    def set_gain_db(self, db: float) -> float | None:
        """返回读回的值。"""

    @abstractmethod
    def get_roi(self) -> tuple[tuple[int, int, int, int], int]:
        """((x, y, w, h), binning)，传感器像素。"""

    @abstractmethod
    def set_roi(self, x: int, y: int, w: int, h: int, binning: int
                ) -> tuple[tuple[int, int, int, int], int]:
        """只在没在采集时调。返回读回的 ((x, y, w, h), binning)。"""

    @abstractmethod
    def start(self) -> None:
        """开始软件触发的连续采集。"""

    @abstractmethod
    def stop(self) -> None:
        """停止采集（disarm）。"""

    @abstractmethod
    def poll(self) -> np.ndarray | None:
        """取一帧（已拷贝的 uint16 数组），等不到就 None（最多等 poll_timeout）。"""


@dataclass
class FakeSpot:
    x: float = 720.0          # 传感器像素（像素中心为整数）
    y: float = 540.0
    sigma_x: float = 12.0
    sigma_y: float = 12.0
    angle_deg: float = 0.0
    peak: float = 600.0       # 曝光 exposure_ref_us、0 dB 时高出背景的 ADU
    background: float = 40.0
    noise: float = 2.0
    exposure_ref_us: float = 10_000.0


class FakeBackend(CameraBackend):
    """测试 / 无硬件演示用：可控的高斯光斑，帧率受曝光与 ``max_fps`` 限制。

    * ``spot`` 可以随时改（挪位置、改宽度），下一帧就按新的画；
    * ``bayer_gains=(r, g, b)``：马赛克传感器上按 RGGB 排布给各色像素乘不同的响应（例如红光只有
      R 像素亮），用来验 2×2 合成；
    * ``busy`` 为真时 open 失败（模拟被别的程序占着）；``missing`` 为真时一台都发现不了；
    * ``fail_next_poll`` 放一个异常进去，取帧线程下一次就抛它（模拟 USB 掉线）。"""

    def __init__(self, cfg: CameraConfig | None = None, *, width: int = 1440, height: int = 1080,
                 bit_depth: int = 10, mosaic: bool = True, max_fps: float = 15.0,
                 spot: FakeSpot | None = None, seed: int = 0, busy: bool = False,
                 missing: bool = False, poll_timeout_s: float = 0.05,
                 exposure_range_us: tuple[int, int] = (40, 20_000_000),
                 gain_range_db: tuple[float, float] | None = (0.0, 48.0),
                 binning_range: tuple[int, int] = (1, 1),
                 bayer_gains: tuple[float, float, float] | None = None) -> None:
        knobs = dict((cfg.fake if cfg else {}) or {})
        self.width = int(knobs.get("width", width))
        self.height = int(knobs.get("height", height))
        self.bit_depth = int(knobs.get("bit_depth", bit_depth))
        self.mosaic = bool(knobs.get("mosaic", mosaic))
        self.max_fps = float(knobs.get("max_fps", max_fps))
        self.spot = spot or FakeSpot(**{k: v for k, v in (knobs.get("spot") or {}).items()
                                        if k in FakeSpot.__dataclass_fields__})
        self.busy = bool(knobs.get("busy", busy))
        self.missing = bool(knobs.get("missing", missing))
        self.poll_timeout_s = (cfg.poll_timeout_ms / 1000.0) if cfg else poll_timeout_s
        self.exposure_range_us = tuple(int(v) for v in exposure_range_us)
        self.gain_range_db = gain_range_db
        self.binning_range = tuple(int(v) for v in binning_range)
        self.bayer_gains = bayer_gains
        self._rng = np.random.default_rng(seed)
        self.exposure_us = 10_000
        self.gain_db = 0.0
        self.roi = (0, 0, self.width, self.height)
        self.binning = 1
        self.is_open = False
        self.armed = False
        self.open_calls = 0
        self.close_calls = 0
        self.frames_served = 0
        self.fail_next_poll: Exception | None = None
        self._next_due = 0.0
        self._cache_key: tuple | None = None
        self._cache_img: np.ndarray | None = None

    def open(self) -> SensorInfo:
        if self.missing:
            raise NoCameraFound(tr("SDK 没发现相机", "The SDK found no camera"))
        if self.busy:
            raise CameraOpenRefused(tr("相机已被别的程序打开", "The camera is already open in another program"))
        self.open_calls += 1
        self.is_open = True
        return SensorInfo(model="Fake CS165CU", serial="FAKE0001", name="fake", firmware="0",
                          width=self.width, height=self.height, bit_depth=self.bit_depth,
                          mosaic=self.mosaic, sensor_type="bayer" if self.mosaic else "monochrome",
                          exposure_range_us=self.exposure_range_us,
                          gain_range_db=self.gain_range_db, binning_range=self.binning_range)

    def close(self) -> None:
        if self.is_open:
            self.close_calls += 1
        self.armed = False
        self.is_open = False

    def get_exposure_us(self) -> int:
        return self.exposure_us

    def set_exposure_us(self, us: int) -> int:
        self.exposure_us = int(us)
        return self.exposure_us

    def get_gain_db(self) -> float | None:
        return self.gain_db if self.gain_range_db else None

    def set_gain_db(self, db: float) -> float | None:
        self.gain_db = round(float(db), 1)
        return self.gain_db

    def get_roi(self):
        return self.roi, self.binning

    def set_roi(self, x, y, w, h, binning):
        if self.armed:
            raise InstrumentError(tr("采集中不能改 ROI", "Cannot change the ROI while acquiring"))
        self.roi, self.binning = (int(x), int(y), int(w), int(h)), int(binning)
        return self.roi, self.binning

    def start(self) -> None:
        self.armed = True
        self._next_due = time.monotonic()

    def stop(self) -> None:
        self.armed = False

    def _period_s(self) -> float:
        return max(self.exposure_us / 1e6, 1.0 / max(self.max_fps, 0.1))

    def noiseless(self) -> np.ndarray:
        """按当前 ROI / 合并 / 曝光 / 增益画不带噪声的一帧（float32，按设置缓存）。"""
        key = (self.roi, self.binning, self.exposure_us, self.gain_db, astuple(self.spot),
               self.bayer_gains, self.mosaic)
        if key == self._cache_key and self._cache_img is not None:
            return self._cache_img
        x0, y0, w, h = self.roi
        b = self.binning
        nw, nh = max(1, w // b), max(1, h // b)
        xs = x0 + (np.arange(nw) + 0.5) * b - 0.5
        ys = y0 + (np.arange(nh) + 0.5) * b - 0.5
        s = self.spot
        gain = 10.0 ** (self.gain_db / 20.0)
        amp = s.peak * (self.exposure_us / s.exposure_ref_us) * gain
        sx, sy = max(s.sigma_x, 1e-6), max(s.sigma_y, 1e-6)
        if s.angle_deg % 180.0 == 0.0:
            gx = np.exp(-0.5 * ((xs - s.x) / sx) ** 2)
            gy = np.exp(-0.5 * ((ys - s.y) / sy) ** 2)
            prof = np.outer(gy, gx)
        else:
            th = math.radians(s.angle_deg)
            X, Y = np.meshgrid(xs - s.x, ys - s.y)
            u = (X * math.cos(th) + Y * math.sin(th)) / sx
            v = (-X * math.sin(th) + Y * math.cos(th)) / sy
            prof = np.exp(-0.5 * (u * u + v * v))
        img = ((s.background + amp * prof) * (b * b)).astype(np.float32)
        if self.mosaic and self.bayer_gains is not None:
            r, gg, bb = self.bayer_gains
            m = np.full(img.shape, gg, dtype=np.float32)
            m[0::2, 0::2] = r      # RGGB；ROI 偏移是偶数，排布不变
            m[1::2, 1::2] = bb
            img = img * m
        self._cache_key, self._cache_img = key, img
        return img

    def render(self) -> np.ndarray:
        img = self.noiseless()
        if self.spot.noise > 0:
            img = img + self._rng.standard_normal(img.shape, dtype=np.float32) * np.float32(self.spot.noise)
        return np.clip(np.rint(img), 0, (1 << self.bit_depth) - 1).astype(np.uint16)

    def poll(self) -> np.ndarray | None:
        if not self.armed:
            time.sleep(self.poll_timeout_s)
            return None
        if self.fail_next_poll is not None:
            exc, self.fail_next_poll = self.fail_next_poll, None
            raise exc
        now = time.monotonic()
        wait = self._next_due - now
        if wait > self.poll_timeout_s:
            time.sleep(self.poll_timeout_s)
            return None
        if wait > 0:
            time.sleep(wait)
        self._next_due = max(self._next_due, now) + self._period_s()
        self.frames_served += 1
        return self.render()


# —— tsi：Thorlabs Scientific Camera SDK（ctypes 晚绑定）——

_P = ctypes.POINTER
_c_int, _c_uint, _c_ll, _c_dbl = ctypes.c_int, ctypes.c_uint, ctypes.c_longlong, ctypes.c_double
_c_vp, _c_cp = ctypes.c_void_p, ctypes.c_char_p
#: 函数名 → argtypes（返回值都是 int，0 = 成功；``tl_camera_get_last_error`` 例外，返回 const char*）。
#: 出处见模块说明；:data:`_SECONDARY` 里的不在官方示例里出现，解析不到时走退路。
_PROTOS: dict[str, list] = {
    "tl_camera_open_sdk": [],
    "tl_camera_close_sdk": [],
    "tl_camera_discover_available_cameras": [_c_cp, _c_int],
    "tl_camera_open_camera": [_c_cp, _P(_c_vp)],
    "tl_camera_close_camera": [_c_vp],
    "tl_camera_get_model": [_c_vp, _c_cp, _c_int],
    "tl_camera_get_name": [_c_vp, _c_cp, _c_int],
    "tl_camera_get_firmware_version": [_c_vp, _c_cp, _c_int],
    "tl_camera_get_camera_sensor_type": [_c_vp, _P(_c_int)],
    "tl_camera_get_sensor_width": [_c_vp, _P(_c_int)],
    "tl_camera_get_sensor_height": [_c_vp, _P(_c_int)],
    "tl_camera_get_bit_depth": [_c_vp, _P(_c_int)],
    "tl_camera_get_exposure_time_range": [_c_vp, _P(_c_ll), _P(_c_ll)],
    "tl_camera_set_exposure_time": [_c_vp, _c_ll],
    "tl_camera_get_exposure_time": [_c_vp, _P(_c_ll)],
    "tl_camera_get_gain_range": [_c_vp, _P(_c_int), _P(_c_int)],
    "tl_camera_set_gain": [_c_vp, _c_int],
    "tl_camera_get_gain": [_c_vp, _P(_c_int)],
    "tl_camera_convert_gain_to_decibels": [_c_vp, _c_int, _P(_c_dbl)],
    "tl_camera_convert_decibels_to_gain": [_c_vp, _c_dbl, _P(_c_int)],
    "tl_camera_set_roi": [_c_vp, _c_int, _c_int, _c_int, _c_int],
    "tl_camera_get_roi": [_c_vp, _P(_c_int), _P(_c_int), _P(_c_int), _P(_c_int)],
    "tl_camera_set_binx": [_c_vp, _c_int],
    "tl_camera_set_biny": [_c_vp, _c_int],
    "tl_camera_get_binx": [_c_vp, _P(_c_int)],
    "tl_camera_get_biny": [_c_vp, _P(_c_int)],
    "tl_camera_set_operation_mode": [_c_vp, _c_int],
    "tl_camera_set_frames_per_trigger_zero_for_unlimited": [_c_vp, _c_uint],
    "tl_camera_set_image_poll_timeout": [_c_vp, _c_int],
    "tl_camera_arm": [_c_vp, _c_int],
    "tl_camera_disarm": [_c_vp],
    "tl_camera_get_pending_frame_or_null": [_c_vp, _P(_P(ctypes.c_ushort)), _P(_c_int),
                                            _P(_P(ctypes.c_ubyte)), _P(_c_int)],
    # —— 次要来源 ——
    "tl_camera_issue_software_trigger": [_c_vp],
    "tl_camera_get_image_width": [_c_vp, _P(_c_int)],
    "tl_camera_get_image_height": [_c_vp, _P(_c_int)],
    "tl_camera_get_binx_range": [_c_vp, _P(_c_int), _P(_c_int)],
    "tl_camera_get_operation_mode": [_c_vp, _P(_c_int)],
}
#: 不在官方示例里、出自按 tl_camera_sdk.h 生成的第三方绑定的那几个。软件触发没有它就没法连续
#: 采集，所以缺了它连接时就报错；其余几个缺了走退路。
_SECONDARY = frozenset({"tl_camera_issue_software_trigger", "tl_camera_get_image_width",
                        "tl_camera_get_image_height", "tl_camera_get_binx_range",
                        "tl_camera_get_operation_mode"})
_OP_SOFTWARE_TRIGGERED = 0                       # TL_CAMERA_OPERATION_MODE（次要来源）
_SENSOR_TYPES = {0: "monochrome", 1: "bayer", 2: "monochrome_polarized"}   # （次要来源）
_ARM_FRAMES = 2                                  # 官方连续采集示例的 arm(2)


class _Sdk:
    """一个 DLL、一次 open_sdk，多台相机共用（引用计数）。最后一台释放时 close_sdk。"""

    _lock = threading.Lock()
    _instances: dict[str, _Sdk] = {}

    def __init__(self, dll: Any, dll_dir_handle: Any = None) -> None:
        self.dll = dll
        self._dir_handle = dll_dir_handle    # os.add_dll_directory 的句柄：DLL 在用期间要留着
        self.refs = 0
        self.fns: dict[str, Any] = {}
        for name, argtypes in _PROTOS.items():
            fn = getattr(dll, name, None)
            if fn is None:
                if name not in _SECONDARY:
                    raise InstrumentUnavailable(tr(f"Thorlabs 相机 SDK 里没有 {name}（版本不对？）", f"The Thorlabs camera SDK has no {name} (wrong version?)"))
                continue
            fn.argtypes = argtypes
            fn.restype = _c_int
            self.fns[name] = fn
        err = getattr(dll, "tl_camera_get_last_error", None)
        if err is not None:
            err.argtypes, err.restype = [], _c_cp
        self._err = err

    @classmethod
    def acquire(cls, dll_dir: str, *, loader: Callable[[str], Any] | None = None) -> _Sdk:
        key = os.path.normcase(os.path.abspath(dll_dir))
        with cls._lock:
            sdk = cls._instances.get(key)
            if sdk is None:
                dll, handle = (loader or _load_sdk_dll)(dll_dir)
                sdk = cls(dll, handle)
                cls._instances[key] = sdk
            if sdk.refs == 0:
                sdk.call("tl_camera_open_sdk")
            sdk.refs += 1
            return sdk

    def release(self) -> None:
        with self._lock:
            if self.refs <= 0:
                return
            self.refs -= 1
            if self.refs == 0:
                try:
                    self.call("tl_camera_close_sdk")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("tl_camera_close_sdk 失败：%s", exc)

    def has(self, name: str) -> bool:
        return name in self.fns

    def last_error(self) -> str:
        try:
            raw = self._err() if self._err is not None else None
            return raw.decode("utf-8", "replace") if raw else ""
        except Exception:  # noqa: BLE001
            return ""

    def call(self, name: str, *args: Any) -> None:
        rc = self.fns[name](*args)
        if rc != 0:
            raise InstrumentError(tr(f"{name} 失败（{rc}）：{self.last_error() or '没有错误描述'}", f"{name} failed ({rc}): {self.last_error() or 'no error description'}"))


def _load_sdk_dll(dll_dir: str) -> tuple[Any, Any]:
    """照官方 windows_setup.py 的做法：目录加进 PATH 和 DLL 搜索路径，再按全路径载入。"""
    if sys.platform != "win32":
        raise InstrumentUnavailable(tr("Thorlabs 相机 SDK 只有 Windows 版", "The Thorlabs camera SDK is Windows-only"))
    path = os.path.join(dll_dir, SDK_DLL)
    if not os.path.isfile(path):
        raise InstrumentUnavailable(
            tr(f"找不到 Thorlabs 相机 SDK：{path}（确认 ThorImageCAM 已安装，或在 cameras.json 里改 dll_dir）",
               f"Cannot find the Thorlabs camera SDK: {path} (check that ThorImageCAM is installed, or change dll_dir in cameras.json)"))
    os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")
    handle = None
    try:
        handle = os.add_dll_directory(dll_dir)
    except (AttributeError, OSError):
        pass
    try:
        return ctypes.CDLL(path), handle
    except OSError as exc:
        raise InstrumentUnavailable(tr(f"载入 {path} 失败：{exc}", f"Failed to load {path}: {exc}")) from exc


class TsiBackend(CameraBackend):
    """Thorlabs Scientific Camera SDK。``loader`` 只给测试注入替身 DLL 用。"""

    def __init__(self, cfg: CameraConfig, *, loader: Callable[[str], Any] | None = None) -> None:
        self.cfg = cfg
        self._loader = loader
        self._sdk: _Sdk | None = None
        self._h = _c_vp()
        self.info = SensorInfo()
        self._armed = False
        self._img_wh = (0, 0)
        self._roi_inclusive = True

    # —— 小工具 ——
    def _int(self, name: str) -> int:
        v = _c_int()
        self._sdk.call(name, self._h, ctypes.byref(v))
        return int(v.value)

    def _str(self, name: str) -> str:
        buf = ctypes.create_string_buffer(256)
        try:
            self._sdk.call(name, self._h, buf, 256)
        except InstrumentError:
            return ""
        return buf.value.decode("ascii", "replace").strip()

    def open(self) -> SensorInfo:
        self._sdk = _Sdk.acquire(self.cfg.dll_dir, loader=self._loader)
        try:
            if not self._sdk.has("tl_camera_issue_software_trigger"):
                raise InstrumentUnavailable(tr("这版 SDK 没有 tl_camera_issue_software_trigger，没法软件触发连续采集", "This SDK version has no tl_camera_issue_software_trigger, so software-triggered continuous acquisition is not possible"))
            ids = ctypes.create_string_buffer(1024)
            self._sdk.call("tl_camera_discover_available_cameras", ids, 1024)
            found = ids.value.decode("ascii", "replace").split()
            if not found:
                raise NoCameraFound(tr("SDK 没发现相机（没插上，或正被别的程序占着）", "The SDK found no camera (not plugged in, or held by another program)"))
            serial = self.cfg.serial or found[0]
            if serial not in found:
                raise NoCameraFound(tr(f"SDK 发现的相机是 {found}，没有配置里的 {serial}", f"The SDK found cameras {found}, not the configured {serial}"))
            h = _c_vp()
            try:
                self._sdk.call("tl_camera_open_camera", serial.encode("ascii"), ctypes.byref(h))
            except InstrumentError as exc:
                raise CameraOpenRefused(str(exc)) from exc
            if not h.value:
                raise CameraOpenRefused(tr("tl_camera_open_camera 没给句柄", "tl_camera_open_camera returned no handle"))
            self._h = h
            st = self._int("tl_camera_get_camera_sensor_type")
            lo, hi = _c_ll(), _c_ll()
            self._sdk.call("tl_camera_get_exposure_time_range", self._h, ctypes.byref(lo), ctypes.byref(hi))
            self.info = SensorInfo(
                model=self._str("tl_camera_get_model"), serial=serial,
                name=self._str("tl_camera_get_name"), firmware=self._str("tl_camera_get_firmware_version"),
                width=self._int("tl_camera_get_sensor_width"),
                height=self._int("tl_camera_get_sensor_height"),
                bit_depth=self._int("tl_camera_get_bit_depth"),
                sensor_type=_SENSOR_TYPES.get(st, f"unknown({st})"),
                mosaic=st == 1, exposure_range_us=(int(lo.value), int(hi.value)),
                gain_range_db=self._gain_range_db(), binning_range=self._binning_range(st))
            self._sdk.call("tl_camera_set_operation_mode", self._h, _OP_SOFTWARE_TRIGGERED)
            if self._sdk.has("tl_camera_get_operation_mode"):
                mode = self._int("tl_camera_get_operation_mode")
                if mode != _OP_SOFTWARE_TRIGGERED:
                    raise InstrumentError(tr(f"操作模式读回是 {mode}，不是软件触发（{_OP_SOFTWARE_TRIGGERED}）", f"Operation mode read back as {mode}, not software-triggered ({_OP_SOFTWARE_TRIGGERED})"))
            self._sdk.call("tl_camera_set_image_poll_timeout", self._h, int(self.cfg.poll_timeout_ms))
            self._sdk.call("tl_camera_set_frames_per_trigger_zero_for_unlimited", self._h, 0)
            self.get_roi()     # 顺带判定 ROI 右下角含不含端点、记下图像尺寸
            return self.info
        except BaseException:
            self.close()
            raise

    def _gain_range_db(self) -> tuple[float, float] | None:
        lo, hi = _c_int(), _c_int()
        try:
            self._sdk.call("tl_camera_get_gain_range", self._h, ctypes.byref(lo), ctypes.byref(hi))
        except InstrumentError:
            return None
        if hi.value <= lo.value:
            return None
        return self._idx_to_db(lo.value), self._idx_to_db(hi.value)

    def _binning_range(self, sensor_type: int) -> tuple[int, int]:
        # 彩色传感器不做合并：合并之后马赛克怎么排，没有文档依据。
        if sensor_type != 0 or not self._sdk.has("tl_camera_get_binx_range"):
            return (1, 1)
        lo, hi = _c_int(), _c_int()
        try:
            self._sdk.call("tl_camera_get_binx_range", self._h, ctypes.byref(lo), ctypes.byref(hi))
        except InstrumentError:
            return (1, 1)
        return (max(1, int(lo.value)), max(1, int(hi.value)))

    def _idx_to_db(self, idx: int) -> float:
        d = _c_dbl()
        self._sdk.call("tl_camera_convert_gain_to_decibels", self._h, int(idx), ctypes.byref(d))
        return float(d.value)

    def close(self) -> None:
        sdk, self._sdk = self._sdk, None
        if sdk is None:
            return
        if self._h.value:
            if self._armed:
                self._armed = False
                try:
                    sdk.call("tl_camera_disarm", self._h)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("tl_camera_disarm 失败：%s", exc)
            try:
                sdk.call("tl_camera_close_camera", self._h)
            except Exception as exc:  # noqa: BLE001
                logger.warning("tl_camera_close_camera 失败：%s", exc)
            self._h = _c_vp()
        sdk.release()

    def get_exposure_us(self) -> int:
        v = _c_ll()
        self._sdk.call("tl_camera_get_exposure_time", self._h, ctypes.byref(v))
        return int(v.value)

    def set_exposure_us(self, us: int) -> int:
        self._sdk.call("tl_camera_set_exposure_time", self._h, int(us))
        return self.get_exposure_us()

    def get_gain_db(self) -> float | None:
        if self.info.gain_range_db is None:
            return None
        return self._idx_to_db(self._int("tl_camera_get_gain"))

    def set_gain_db(self, db: float) -> float | None:
        idx = _c_int()
        self._sdk.call("tl_camera_convert_decibels_to_gain", self._h, float(db), ctypes.byref(idx))
        self._sdk.call("tl_camera_set_gain", self._h, int(idx.value))
        return self.get_gain_db()

    def get_roi(self):
        a, b, c, d = _c_int(), _c_int(), _c_int(), _c_int()
        self._sdk.call("tl_camera_get_roi", self._h, ctypes.byref(a), ctypes.byref(b),
                       ctypes.byref(c), ctypes.byref(d))
        binning = self._int("tl_camera_get_binx")
        # 满幅读回 (0,0,W-1,H-1) ⇒ 右下角含端点；读回 (0,0,W,H) ⇒ 不含。只有满幅时能判，判到就记住。
        if a.value == 0 and b.value == 0 and self.info.width:
            if c.value == self.info.width - 1:
                self._roi_inclusive = True
            elif c.value == self.info.width:
                self._roi_inclusive = False
        k = 1 if self._roi_inclusive else 0
        roi = (int(a.value), int(b.value), int(c.value - a.value + k), int(d.value - b.value + k))
        if self._sdk.has("tl_camera_get_image_width") and self._sdk.has("tl_camera_get_image_height"):
            self._img_wh = (self._int("tl_camera_get_image_width"),
                            self._int("tl_camera_get_image_height"))
        else:
            self._img_wh = (roi[2] // max(binning, 1), roi[3] // max(binning, 1))
        return roi, binning

    def set_roi(self, x, y, w, h, binning):
        if self._armed:
            raise InstrumentError(tr("采集中不能改 ROI（先停采集）", "Cannot change the ROI while acquiring (stop acquisition first)"))
        self._sdk.call("tl_camera_set_binx", self._h, int(binning))
        self._sdk.call("tl_camera_set_biny", self._h, int(binning))
        k = 1 if self._roi_inclusive else 0
        self._sdk.call("tl_camera_set_roi", self._h, int(x), int(y), int(x + w - k), int(y + h - k))
        return self.get_roi()

    def start(self) -> None:
        self._sdk.call("tl_camera_set_frames_per_trigger_zero_for_unlimited", self._h, 0)
        self._sdk.call("tl_camera_arm", self._h, _ARM_FRAMES)
        self._armed = True
        try:
            self._sdk.call("tl_camera_issue_software_trigger", self._h)
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        if self._armed and self._sdk is not None:
            self._armed = False
            self._sdk.call("tl_camera_disarm", self._h)

    def poll(self) -> np.ndarray | None:
        buf = _P(ctypes.c_ushort)()
        count, meta, meta_n = _c_int(), _P(ctypes.c_ubyte)(), _c_int()
        self._sdk.call("tl_camera_get_pending_frame_or_null", self._h, ctypes.byref(buf),
                       ctypes.byref(count), ctypes.byref(meta), ctypes.byref(meta_n))
        if not buf:
            return None
        w, h = self._img_wh
        if w <= 0 or h <= 0:
            raise InstrumentError(tr("不知道图像尺寸（ROI 读回失败）", "Image size unknown (ROI readback failed)"))
        # SDK 的缓冲区下一次取帧时就会被复用：立刻拷走。
        return np.ctypeslib.as_array(buf, shape=(h * w,)).reshape(h, w).copy()


def make_backend(cfg: CameraConfig) -> CameraBackend:
    if cfg.backend == "fake":
        return FakeBackend(cfg)
    return TsiBackend(cfg)


# ── 占用者（只读查进程表）────────────────────────────────────────────────────

_holder_cache: tuple[float, list[str]] = (-1e9, [])
_holder_lock = threading.Lock()


def holder_processes(max_age_s: float = 2.0) -> list[str]:
    """正在运行的、已知会独占相机的厂家程序（显示名）。只读进程名，读不到就是 []，缓存 2 s。"""
    global _holder_cache
    with _holder_lock:
        t, names = _holder_cache
        if time.monotonic() - t < max_age_s:
            return list(names)
    found: list[str] = []
    try:
        import psutil  # noqa: PLC0415

        for p in psutil.process_iter(["name"]):
            name = str((p.info or {}).get("name") or "").lower()
            if name in HOLDER_PROCESSES and HOLDER_PROCESSES[name] not in found:
                found.append(HOLDER_PROCESSES[name])
    except Exception as exc:  # noqa: BLE001 — 查不到不是答案，也不许抛
        logger.debug("进程表读不了：%s", exc)
    with _holder_lock:
        _holder_cache = (time.monotonic(), found)
    return list(found)


# ── 一台相机 ──────────────────────────────────────────────────────────────────

class _IoGate:
    """设备调用的互斥（取帧线程与控制命令共用）；控制命令等锁有上限，SDK 卡住时报错而不是挂住。

    不另做「命令优先」：Python 3.13 的 threading.Lock 建在 PyMutex 上，等了约 1 ms 以上的线程会被
    直接交棒，取帧线程放锁后马上再抢也抢不过它 —— 实测取帧每轮在锁里 30 ms 时，命令最多等一轮。
    ``test_commands_are_not_starved_by_the_frame_thread`` 钉着这个行为。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def command(self, timeout_s: float = IO_WAIT_S) -> Iterator[None]:
        if not self._lock.acquire(timeout=timeout_s):
            raise InstrumentError(tr(f"相机上一个设备调用 {timeout_s:g} s 没返回（SDK 卡住了？）", f"The previous device call on the camera did not return in {timeout_s:g} s (SDK stuck?)"))
        try:
            yield
        finally:
            self._lock.release()

    @contextlib.contextmanager
    def poll(self) -> Iterator[None]:
        with self._lock:
            yield


@dataclass(frozen=True)
class _Settings:
    exposure_us: int | None = None
    gain_db: float | None = None
    roi: tuple[int, int, int, int] | None = None
    binning: int = 1
    #: 帧号不超过它的帧可能还是旧设置拍的。
    settled_after: int = 0


class _FpsMeter:
    def __init__(self, n: int = 20) -> None:
        self._t: deque[float] = deque(maxlen=n)

    def tick(self, t: float) -> None:
        self._t.append(t)

    def fps(self) -> float | None:
        ts = list(self._t)
        if len(ts) < 2:
            return None
        dt = ts[-1] - ts[0]
        return (len(ts) - 1) / dt if dt > 0 else None

    def clear(self) -> None:
        self._t.clear()


def _roi_dict(roi: tuple | None, binning: int | None) -> dict | None:
    if not roi:
        return None
    x, y, w, h = roi
    return {"x": int(x), "y": int(y), "w": int(w), "h": int(h), "binning": int(binning or 1)}


class Camera:
    """一台相机：disconnected → connected ⇄ live；取帧出错叠加成 error（断开后可再连）。

    ``status()`` / ``stats()`` / 画面都不碰设备、不等锁；连接、设置、开停直播在 ``_lock`` 下串行，
    设备调用另在 :class:`_IoGate` 下与取帧线程互斥。"""

    def __init__(self, cfg: CameraConfig, *,
                 backend_factory: Callable[[CameraConfig], CameraBackend] | None = None,
                 holder_probe: Callable[[], list[str]] | None = None) -> None:
        self.cfg = cfg
        self._factory = backend_factory or make_backend
        self._holders = holder_probe or holder_processes
        self._lock = threading.RLock()
        self._io = _IoGate()
        self._backend: CameraBackend | None = None
        self.info: SensorInfo | None = None
        self._mode = "disconnected"          # disconnected | connected | live
        self._fault: str | None = None       # 取帧线程出错时填；断开时清
        self._settings = _Settings()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frame: Frame | None = None
        self._frame_cv = threading.Condition()
        self._seq = 0
        self._fps = _FpsMeter()
        self._cache: dict[tuple, Any] = {}
        self._cache_lock = threading.Lock()
        self.connected_since: float | None = None

    # —— 只读 ——
    @property
    def state(self) -> str:
        mode = self._mode
        return "error" if mode != "disconnected" and self._fault is not None else mode

    @property
    def connected(self) -> bool:
        return self.state in ("connected", "live")

    @property
    def live(self) -> bool:
        return self.state == "live"

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def settled_after(self) -> int:
        return self._settings.settled_after

    def occupied_by(self) -> str | None:
        """有证据的别的占用者：MAST 没连着这台相机、而厂家程序的进程在跑。读不到就是 None
        （None 不等于空闲）。"""
        if self._mode != "disconnected":
            return None
        holders = self._holders()
        return holders[0] if holders else None

    def status(self) -> dict:
        """不碰设备、不等锁（各字段各读一次，展示用）。"""
        state, info, s, f = self.state, self.info, self._settings, self._frame
        return {
            "camera_id": self.cfg.camera_id, "label": self.cfg.label, "backend": self.cfg.backend,
            "enabled": self.cfg.enabled, "state": state,
            "connected": state in ("connected", "live"), "live": state == "live",
            "error": self._fault if state == "error" else None,
            "connected_since": self.connected_since,
            "sensor": info.as_dict() if info else None,
            "exposure_us": s.exposure_us, "gain_db": s.gain_db, "roi": _roi_dict(s.roi, s.binning),
            "fps": self._fps.fps() if state == "live" else None,
            "frame_seq": f.seq if f is not None else None,
            "frame_t": f.t if f is not None else None,
            "occupied_by": self.occupied_by(),
        }

    # —— 连接 ——
    def connect(self) -> dict:
        with self._lock:
            if self.state == "error":
                raise InstrumentError(tr(f"相机处于错误状态（{self._fault}）：先断开相机再连接", f"The camera is in an error state ({self._fault}): disconnect and reconnect it"))
            if self._mode == "disconnected":
                self._open_locked()
        return self.status()

    def _open_locked(self) -> None:
        if not self.cfg.enabled:
            raise InstrumentUnavailable(
                tr(f"相机 {self.cfg.camera_id} 未启用：在 <数据根>/config/cameras.json 里把它的 enabled 设为 true",
                   f"Camera {self.cfg.camera_id} is not enabled: set its enabled to true in <data root>/config/cameras.json"))
        backend = self._factory(self.cfg)
        try:
            with self._io.command():
                info = backend.open()
                exposure = backend.get_exposure_us()
                gain = backend.get_gain_db()
                roi, binning = backend.get_roi()
        except (NoCameraFound, CameraOpenRefused) as exc:
            self._safe_close(backend)
            holders = self._holders()
            if holders:
                raise CameraBusy(holders[0], str(exc)) from exc
            raise InstrumentUnavailable(tr(f"{exc}（没看到已知的占用程序在跑；检查 USB 线）", f"{exc} (no known occupying program seen running; check the USB cable)")) from exc
        except BaseException:
            self._safe_close(backend)
            raise
        self._backend, self.info = backend, info
        self._settings = _Settings(exposure_us=exposure, gain_db=gain, roi=tuple(roi),
                                   binning=int(binning), settled_after=self._seq)
        self._mode, self._fault = "connected", None
        self.connected_since = time.time()

    def disconnect(self) -> dict:
        """彻底释放（可重复调）：停取帧线程 → disarm → 关句柄 → 关 SDK。"""
        with self._lock:
            self._stop_thread_locked()
            backend, self._backend = self._backend, None
            if backend is not None:
                try:
                    with self._io.command():
                        self._safe_close(backend)
                except InstrumentError as exc:
                    logger.warning("相机 %s 断开时设备调用卡住，句柄没能关：%s", self.cfg.camera_id, exc)
            self._mode, self._fault = "disconnected", None
            self.info, self.connected_since = None, None
            self._settings = _Settings(settled_after=self._seq)
            with self._frame_cv:
                self._frame = None
                self._frame_cv.notify_all()
            self._fps.clear()
            with self._cache_lock:
                self._cache.clear()
        return self.status()

    @staticmethod
    def _safe_close(backend: CameraBackend) -> None:
        try:
            backend.close()
        except Exception as exc:  # noqa: BLE001 — 释放路径不许抛
            logger.warning("相机关闭时出错：%s", exc)

    def _require_connected(self) -> CameraBackend:
        if self._mode == "disconnected" or self._backend is None:
            raise InstrumentUnavailable(tr(f"相机 {self.cfg.camera_id} 没连接：先连接相机（ConnectCamera）", f"Camera {self.cfg.camera_id} is not connected: connect it first (ConnectCamera)"))
        if self._fault is not None:
            raise InstrumentError(tr(f"相机处于错误状态（{self._fault}）：先断开相机再连接", f"The camera is in an error state ({self._fault}): disconnect and reconnect it"))
        return self._backend

    # —— 采集 ——
    def start_live(self) -> dict:
        with self._lock:
            backend = self._require_connected()
            if self._mode != "live":
                self._start_locked(backend)
        return self.status()

    def _start_locked(self, backend: CameraBackend) -> None:
        with self._io.command():
            backend.start()
        stop = threading.Event()
        self._stop = stop
        self._fps.clear()
        self._settings = replace(self._settings, settled_after=self._seq)
        self._mode = "live"
        th = threading.Thread(target=self._run, args=(backend, stop),
                              name=f"camera-live-{self.cfg.camera_id}", daemon=True)
        self._thread = th
        th.start()

    def stop_live(self) -> dict:
        with self._lock:
            if self._mode == "live":
                self._stop_thread_locked()
                self._mode = "connected"
        return self.status()

    def _stop_thread_locked(self) -> None:
        """停取帧线程，在直播就 disarm。调用方持 ``_lock``。"""
        th, self._thread = self._thread, None
        self._stop.set()
        if th is not None and th is not threading.current_thread():
            th.join(timeout=5.0)
            if th.is_alive():
                logger.warning("相机 %s 的取帧线程 5 s 内没退出", self.cfg.camera_id)
        backend = self._backend
        if backend is not None and self._mode == "live":
            try:
                with self._io.command():
                    backend.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("相机 %s 停止采集出错：%s", self.cfg.camera_id, exc)

    def _run(self, backend: CameraBackend, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                with self._io.poll():
                    if stop.is_set():
                        break
                    raw = backend.poll()
            except Exception as exc:  # noqa: BLE001
                if stop.is_set():
                    break
                self._fault = tr(f"取帧出错：{type(exc).__name__}: {exc}", f"Frame grab error: {type(exc).__name__}: {exc}")
                logger.warning("相机 %s 取帧出错，直播停止：%s", self.cfg.camera_id, exc)
                with self._frame_cv:
                    self._frame_cv.notify_all()
                return
            if raw is None:
                continue
            info, s = self.info, self._settings
            if info is None:
                break
            seq = self._seq + 1
            frame = Frame(seq=seq, t=time.time(), raw=raw,
                          roi=s.roi or (0, 0, int(raw.shape[1]), int(raw.shape[0])),
                          binning=int(s.binning or 1), mosaic=bool(info.mosaic),
                          bit_depth=int(info.bit_depth), exposure_us=s.exposure_us,
                          gain_db=s.gain_db, settled=seq > s.settled_after)
            with self._frame_cv:
                self._seq = seq
                self._frame = frame
                self._frame_cv.notify_all()
            self._fps.tick(frame.t)

    def latest_frame(self) -> Frame | None:
        """在直播时的最新一帧；不在直播就是 None。"""
        f = self._frame
        return f if f is not None and self.live else None

    def wait_frame(self, after_seq: int, timeout_s: float,
                   check_abort: Callable[[], object] | None = None) -> Frame | None:
        """等一帧 ``seq > after_seq`` 的帧。超时 / 中止 / 不在直播 → None。"""
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._frame_cv:
            while True:
                f = self._frame
                if f is not None and f.seq > after_seq:
                    return f
                if not self.live or (check_abort is not None and check_abort()):
                    return None
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._frame_cv.wait(timeout=min(left, 0.1))

    # —— 设置（超出相机报告的范围就拒绝，读回值为准）——
    def _settings_changed(self, **changes: Any) -> None:
        self._settings = replace(self._settings, settled_after=self._seq + SETTLE_FRAMES, **changes)

    def set_exposure(self, us: int) -> dict:
        with self._lock:
            backend = self._require_connected()
            lo, hi = self.info.exposure_range_us
            if not lo <= int(us) <= hi:
                raise CameraRefused(tr(f"曝光 {us} µs 超出相机范围 [{lo}, {hi}] µs", f"Exposure {us} µs is outside the camera range [{lo}, {hi}] µs"))
            with self._io.command():
                actual = backend.set_exposure_us(int(us))
            self._settings_changed(exposure_us=actual)
        return self.status()

    def set_gain(self, db: float) -> dict:
        with self._lock:
            backend = self._require_connected()
            rng = self.info.gain_range_db
            if rng is None:
                raise CameraRefused(tr("这台相机不支持增益", "This camera does not support gain"))
            if not rng[0] - 1e-9 <= float(db) <= rng[1] + 1e-9:
                raise CameraRefused(tr(f"增益 {db} dB 超出相机范围 [{rng[0]:g}, {rng[1]:g}] dB", f"Gain {db} dB is outside the camera range [{rng[0]:g}, {rng[1]:g}] dB"))
            with self._io.command():
                actual = backend.set_gain_db(float(db))
            self._settings_changed(gain_db=actual)
        return self.status()

    def set_roi(self, x: int, y: int, w: int, h: int, binning: int = 1) -> dict:
        """改 ROI / 合并。直播中会先停、改完再接着播。"""
        x, y, w, h, binning = int(x), int(y), int(w), int(h), int(binning)
        with self._lock:
            backend = self._require_connected()
            info = self.info
            b_lo, b_hi = info.binning_range
            if not b_lo <= binning <= b_hi:
                raise CameraRefused(tr(f"合并 {binning} 超出相机范围 [{b_lo}, {b_hi}]", f"Binning {binning} is outside the camera range [{b_lo}, {b_hi}]"))
            if min(x, y) < 0 or w < 1 or h < 1 or x + w > info.width or y + h > info.height:
                raise CameraRefused(tr(f"ROI ({x}, {y}, {w}, {h}) 超出传感器 {info.width}×{info.height}", f"ROI ({x}, {y}, {w}, {h}) is outside the sensor {info.width}×{info.height}"))
            if info.mosaic and any(v % 2 for v in (x, y, w, h)):
                raise CameraRefused(tr("彩色（马赛克）传感器的 ROI 要落在偶数像素上（x、y、w、h 都要是偶数）", "The ROI of a colour (mosaic) sensor must fall on even pixels (x, y, w and h must all be even)"))
            was_live = self._mode == "live"
            if was_live:
                self._stop_thread_locked()
                self._mode = "connected"
            with self._io.command():
                roi, b = backend.set_roi(x, y, w, h, binning)
            self._settings_changed(roi=tuple(roi), binning=int(b))
            if was_live:
                self._start_locked(backend)
        return self.status()

    # —— 画面与统计（按帧号缓存，只留当前这一帧的）——
    def _cached(self, key: tuple, make: Callable[[], Any]) -> Any:
        with self._cache_lock:
            if key in self._cache:
                return self._cache[key]
        val = make()
        with self._cache_lock:
            for k in [k for k in self._cache if k[1] != key[1]]:
                del self._cache[k]
            self._cache[key] = val
        return val

    def gray(self, frame: Frame) -> np.ndarray:
        return self._cached(("gray", frame.seq), lambda: gray_image(frame.raw, frame.mosaic))

    def color(self, frame: Frame) -> np.ndarray:
        return self._cached(("color", frame.seq), lambda: color_image(frame.raw, frame.mosaic))

    def frame_stats(self, frame: Frame) -> dict:
        """一帧的光斑统计（坐标约定见模块说明）。"""
        def make() -> dict:
            s = frame.pixel_scale
            g = self.gray(frame)
            st = spot_stats(g, raw=frame.raw, full_scale=frame.full_scale, scale=float(s))
            c = st["centroid_px"]
            st.update(
                centroid_sensor_px=[frame.roi[0] + c[0], frame.roi[1] + c[1]] if c else None,
                frame_w=int(g.shape[1]) * s, frame_h=int(g.shape[0]) * s, pixel_scale=s,
                image_w=int(g.shape[1]), image_h=int(g.shape[0]),
                frame_seq=frame.seq, t=frame.t, settled=frame.settled,
                bit_depth=frame.bit_depth, full_scale=frame.full_scale,
                exposure_us=frame.exposure_us, gain_db=frame.gain_db,
                roi=_roi_dict(frame.roi, frame.binning))
            return st
        return self._cached(("stats", frame.seq), make)

    def stats(self) -> dict:
        """``GET /api/camera/{id}/stats`` 的内容：状态 + 最新一帧的统计（不在直播时统计各项为 None）。"""
        st = self.status()
        out: dict[str, Any] = {
            "camera_id": st["camera_id"], "state": st["state"], "connected": st["connected"],
            "live": st["live"], "error": st["error"], "fps": st["fps"],
            "exposure_us": st["exposure_us"], "gain_db": st["gain_db"], "roi": st["roi"],
            "max": None, "saturated_frac": None, "centroid_px": None, "fwhm_px": None,
            "frame_seq": None, "t": None,
        }
        f = self.latest_frame()
        if f is not None:
            out.update(self.frame_stats(f))
        return out

    def jpeg(self, *, max_w: int | None = None, quality: int = 80,
             scale: str = "full", mode: str = "gray") -> tuple[bytes, Frame] | None:
        """最新一帧的 JPEG。``mode=gray``（默认）：对焦用灰度，可 ``max_w`` 缩宽。
        ``mode=color``：Bayer 解马赛克彩色、全分辨率（忽略 ``max_w``）。不在直播就是 None。"""
        frame = self.latest_frame()
        if frame is None:
            return None
        color = str(mode).lower() == "color"
        use_max_w = None if color else max_w
        key = ("jpeg", frame.seq, use_max_w, int(quality), scale, "color" if color else "gray")

        def make() -> bytes:
            img = self.color(frame) if color else self.gray(frame)
            return encode_jpeg(to_8bit(img, full_scale=frame.full_scale, scale=scale),
                               max_w=use_max_w, quality=quality)

        return self._cached(key, make), frame


# ── 管理器（进程级单例）───────────────────────────────────────────────────────

class CameraManager:
    """按 camera_id 管 :class:`Camera`。构造与列举都不碰硬件。

    不给 ``configs`` 时读 cameras.json，并在文件改过之后重读（不用重启 MAST）；连着的相机等断开
    之后才换上新配置。"""

    def __init__(self, configs: list[CameraConfig] | None = None, *,
                 backend_factory: Callable[[CameraConfig], CameraBackend] | None = None,
                 holder_probe: Callable[[], list[str]] | None = None,
                 path: str | os.PathLike | None = None) -> None:
        self._fixed = list(configs) if configs is not None else None
        self._path = Path(path) if path else None
        self._factory = backend_factory
        self._holders = holder_probe
        self._cams: dict[str, Camera] = {}
        self._stamp: object = object()        # 上次读配置时文件的 (mtime_ns, size)
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        if self._fixed is not None:
            for cfg in self._fixed:
                if cfg.camera_id not in self._cams:
                    self._cams[cfg.camera_id] = self._new(cfg)
            return
        p = self._path or config_path()
        try:
            st = p.stat()
            stamp: object = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp == self._stamp:
            return
        wanted = {c.camera_id: c for c in load_camera_configs(p)}
        pending = False
        for cid, cam in list(self._cams.items()):
            if wanted.get(cid) == cam.cfg:
                continue
            if cam.state != "disconnected":
                pending = True                 # 连着的相机先不动，断开之后再换
                continue
            del self._cams[cid]
        for cid, cfg in wanted.items():
            if cid not in self._cams:
                self._cams[cid] = self._new(cfg)
        if not pending:
            self._stamp = stamp

    def _new(self, cfg: CameraConfig) -> Camera:
        return Camera(cfg, backend_factory=self._factory, holder_probe=self._holders)

    def ids(self) -> list[str]:
        with self._lock:
            self._ensure()
            return list(self._cams)

    def camera(self, camera_id: str | None = None) -> Camera:
        """``camera_id`` 省略时：只配了一台就是它。找不到抛 KeyError（消息里列出有哪些）。"""
        with self._lock:
            self._ensure()
            if not camera_id:
                if len(self._cams) == 1:
                    return next(iter(self._cams.values()))
                if not self._cams:
                    raise KeyError(tr("没有配置相机（<数据根>/config/cameras.json）", "No camera configured (<data root>/config/cameras.json)"))
                raise KeyError(tr(f"配了 {len(self._cams)} 台相机，要指定 camera_id：{list(self._cams)}", f"{len(self._cams)} cameras are configured; specify camera_id: {list(self._cams)}"))
            cam = self._cams.get(str(camera_id))
            if cam is None:
                raise KeyError(tr(f"没有相机 {camera_id!r}（有：{list(self._cams)}）", f"No camera {camera_id!r} (available: {list(self._cams)})"))
            return cam

    def cameras(self) -> list[Camera]:
        with self._lock:
            return list(self._cams.values())

    def close_all(self) -> None:
        """断开所有相机（服务关停时）。不抛异常。"""
        for cam in self.cameras():
            if cam.state != "disconnected":
                try:
                    cam.disconnect()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("关停时断开相机 %s 出错：%s", cam.cfg.camera_id, exc)


_MANAGER: CameraManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_camera_manager() -> CameraManager:
    """进程级的相机管理器。每次用的时候调（测试会换掉它）。"""
    global _MANAGER
    mgr = _MANAGER
    if mgr is None:
        with _MANAGER_LOCK:
            if _MANAGER is None:
                _MANAGER = CameraManager()
            mgr = _MANAGER
    return mgr


def install_camera_manager(mgr: CameraManager | None) -> CameraManager | None:
    """换掉进程级单例，返回原来的（测试、演示用）。原来那个不会被断开 —— 调用方自己决定。"""
    global _MANAGER
    with _MANAGER_LOCK:
        prev, _MANAGER = _MANAGER, mgr
    return prev


@atexit.register
def _close_all_at_exit() -> None:
    mgr = _MANAGER
    if mgr is not None:
        mgr.close_all()


# ── 手动控制面板的描述（契约 §13.1；hub 的 GET /api/manual/devices 延迟 import 它）────

#: 面板动作 id → (按钮文字, 技能名, 让人填的参数, 悬停提示)。参数的类型、单位、范围由描述端点按
#: 技能注册的 ParameterSpec 逐字填写，这里只给名字。
MANUAL_ACTIONS: tuple[tuple[str, str, str, tuple[str, ...], str], ...] = (
    ("camera_connect", tr("连接", "Connect"), "ConnectCamera", (),
     tr("打开相机（MAST 启动时不碰它）；ThorImageCAM 占着时会失败并点名，MAST 不替人关程序",
        "Opens the camera (MAST does not touch it at startup); fails and names the holder if ThorImageCAM has it, and MAST does not close programs for you")),
    ("camera_disconnect", tr("断开", "Disconnect"), "DisconnectCamera", (),
     tr("停直播、关相机、关 SDK，彻底释放，之后 ThorImageCAM 可以用；急停闩挂着时也会被拒",
        "Stops live view, closes the camera and the SDK and releases it completely so ThorImageCAM can use it; refused while the emergency-stop latch is set")),
    ("live_start", tr("开始直播", "Start live view"), "CameraStartLive", (),
     tr("软件触发连续采集，只保留最新一帧", "Software-triggered continuous acquisition; only the latest frame is kept")),
    ("live_stop", tr("停止直播", "Stop live view"), "CameraStopLive", (),
     tr("停止采集，相机仍连着", "Stops acquisition; the camera stays connected")),
    ("snapshot", tr("快照", "Snapshot"), "CameraSnapshot", ("tag",),
     tr("存原始数组 + PNG + 元数据（含当时的光学轴位置）进当前实验，等设置生效后的那一帧",
        "Saves the raw array + PNG + metadata (including the optical axis positions at the time) into the current experiment, taking the first frame after the settings have taken effect")),
    ("set_exposure", tr("曝光", "Exposure"), "SetCameraExposure", ("exposure_us",),
     tr("超出相机报告的范围就拒绝，以读回值为准", "Refused if outside the range the camera reports; the read-back value is authoritative")),
    ("set_gain", tr("增益", "Gain"), "SetCameraGain", ("gain_db",),
     tr("超出相机报告的范围就拒绝，以读回值为准", "Refused if outside the range the camera reports; the read-back value is authoritative")),
    ("set_roi", tr("设 ROI", "Set ROI"), "SetCameraROI", ("x", "y", "w", "h", "binning"),
     tr("传感器像素；直播中会先停、改完接着播；彩色传感器要偶数、不合并",
        "Sensor pixels; live view is stopped first and resumed afterwards; a colour sensor needs even values and no binning")),
)


def _manual_action(action_id: str, label: str, skill: str, params: tuple[str, ...], hint: str,
                   camera_id: str) -> dict:
    return {"id": action_id, "label": label, "skill": skill, "fixed": {"camera_id": camera_id},
            "params": [{"name": n} for n in params], "per_axis": False, "direction": "none",
            "direction_param": None, "safety": "auto", "bypass_lock": False,
            "hold_repeat_ms": None, "stop_on_release": False, "hint": hint}


def _manual_device(cam: Camera) -> dict:
    cid = cam.cfg.camera_id
    st = cam.status()
    if not cam.cfg.enabled:
        why = tr("未启用：在 <数据根>/config/cameras.json 里把这台相机的 enabled 设为 true", "Not enabled: set enabled to true for this camera in <data root>/config/cameras.json")
    elif st["state"] == "error":
        why = tr(f"出错：{st['error']}；先断开相机再连接", f"Error: {st['error']}; disconnect and reconnect the camera")
    else:
        why = None
    sensor = st["sensor"] or {}
    return {
        "id": f"local/camera/{cid}", "machine": "local", "kind": "camera",
        "label": cam.cfg.label, "vendor": cam.cfg.vendor,
        "model": sensor.get("model") or cam.cfg.model,
        "serial": sensor.get("serial") or cam.cfg.serial,
        "available": why is None, "unavailable_reason": why,
        "occupied_by": st["occupied_by"], "tip_requirement": "none", "axes": [],
        "actions": [_manual_action(aid, label, skill, params, hint, cid)
                    for aid, label, skill, params, hint in MANUAL_ACTIONS],
        # 读数不走技能执行入口（每次执行都要入账）：前端按这个间隔轮询 GET /api/camera/{id}/stats。
        "readout": {"kind": "poll", "interval_s": 1.0},
    }


def manual_devices() -> list[dict]:
    """每台配置里的相机一条 ManualDevice。不碰硬件（只读进程表看占用者）。

    配置读不了之类的整体失败照常抛 —— 描述端点会把相机这一类标成 degraded。"""
    mgr = get_camera_manager()
    return [_manual_device(mgr.camera(cid)) for cid in mgr.ids()]


__all__ = [
    "BACKENDS", "Camera", "CameraBackend", "CameraBusy", "CameraConfig", "CameraManager",
    "CameraOpenRefused", "CameraRefused", "DEFAULT_DLL_DIR", "FakeBackend", "FakeSpot", "Frame",
    "HOLDER_PROCESSES", "MANUAL_ACTIONS", "NoCameraFound", "SETTLE_FRAMES", "SensorInfo",
    "TsiBackend", "config_path", "encode_jpeg", "encode_png", "get_camera_manager", "gray_image",
    "holder_processes", "install_camera_manager", "load_camera_configs", "make_backend",
    "manual_devices", "spot_stats", "to_8bit",
]
