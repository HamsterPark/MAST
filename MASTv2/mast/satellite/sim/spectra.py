"""合成拉曼光谱（参考卫星的模拟光谱仪用）。

量级取典型的 CCD 光谱仪：1340 像素；1200 g/mm 光栅时全幅约 57 nm；激发线 633 nm。

拉曼位移 ν = 10⁷/λ_laser − 10⁷/λ（cm⁻¹）。谱里有：Si 520 cm⁻¹ 主峰、Si 2TA ~300 cm⁻¹、
Si 二阶 ~960 cm⁻¹ 宽峰、低位移端瑞利边缘滤光片的残余、宽荧光背景、暗电平与读出噪声、
信号的散粒噪声。只为让前端和测试看见「像样的谱」，不是物理模型。
"""

from __future__ import annotations

import numpy as np

N_PIXELS = 1340
DEFAULT_LASER_NM = 633.0
DEFAULT_CENTER_NM = 664.5495
CENTER_PIXEL = 669.5
#: 1200 g/mm 时每像素的波长宽度（57.14 nm / 1340 px），其他光栅按刻线密度反比缩放。
DISP_1200_NM_PER_PX = 0.042644
DARK_COUNTS = 600.0
READ_NOISE_COUNTS = 4.0

#: (位移 cm⁻¹, 半高宽 cm⁻¹, 峰高 counts/s)
PEAKS = (
    (520.7, 4.5, 3200.0),     # Si 一阶光学声子
    (302.0, 28.0, 160.0),     # Si 2TA
    (960.0, 70.0, 260.0),     # Si 二阶
    (435.0, 12.0, 90.0),      # 弱伴峰
)


def grating_density(grating: str) -> float:
    """``"[500nm,1200][0][0]"`` / ``"500nm,1200"`` → 1200。认不出就当 1200。"""
    s = str(grating or "")
    try:
        inner = s.split("]")[0].lstrip("[")
        return float(inner.split(",")[1])
    except (IndexError, ValueError):
        return 1200.0


def wavelength_axis(center_nm: float = DEFAULT_CENTER_NM, grating: str = "[500nm,1200][0][0]",
                    n: int = N_PIXELS) -> np.ndarray:
    disp = DISP_1200_NM_PER_PX * 1200.0 / max(1.0, grating_density(grating))
    i = np.arange(n, dtype=np.float64) - CENTER_PIXEL
    # 一点点二次项，像真的色散曲线那样两端不完全线性。
    return center_nm + disp * i - 1.6e-7 * i * i * (disp / DISP_1200_NM_PER_PX)


def raman_shift_cm1(wl_nm: np.ndarray, laser_nm: float = DEFAULT_LASER_NM) -> np.ndarray:
    return 1e7 / float(laser_nm) - 1e7 / np.asarray(wl_nm, dtype=np.float64)


def expected_rate(wl_nm: np.ndarray, laser_nm: float = DEFAULT_LASER_NM,
                  intensity: float = 1.0) -> np.ndarray:
    """每像素的期望信号（counts/s），不含暗电平与噪声。"""
    shift = raman_shift_cm1(wl_nm, laser_nm)
    rate = np.zeros_like(shift)
    for pos, fwhm, height in PEAKS:
        g = fwhm / 2.0
        rate += height / (1.0 + ((shift - pos) / g) ** 2)
    # 宽荧光背景 + 瑞利边缘残余（位移越小越强）
    rate += 140.0 + 60.0 * np.exp(-((shift - 1400.0) / 1500.0) ** 2)
    rate += 2500.0 * np.exp(-np.clip(shift, 0.0, None) / 18.0)
    return rate * float(intensity)


def synth_frame(wl_nm: np.ndarray, *, exposure_ms: float, laser_nm: float = DEFAULT_LASER_NM,
                intensity: float = 1.0, rng: np.random.Generator | None = None) -> np.ndarray:
    """一帧 u16 光谱。"""
    rng = rng or np.random.default_rng()
    signal = expected_rate(wl_nm, laser_nm, intensity) * max(0.0, float(exposure_ms)) / 1000.0
    counts = rng.poisson(np.clip(signal, 0.0, None)).astype(np.float64)
    counts += DARK_COUNTS + rng.normal(0.0, READ_NOISE_COUNTS, size=counts.shape)
    return np.clip(np.rint(counts), 0, 65535).astype("<u2")


__all__ = ["N_PIXELS", "DEFAULT_LASER_NM", "DEFAULT_CENTER_NM", "PEAKS", "grating_density",
           "wavelength_axis", "raman_shift_cm1", "expected_rate", "synth_frame"]
