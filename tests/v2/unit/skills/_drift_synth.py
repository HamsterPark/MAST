"""分子配准漂移追踪的合成数据:分子排布、视野、合成 .sxm。

给 ``builtins/test_drift_register.py`` 和 ``composite/test_measure_spectra_at_sites_drift.py`` 共用。
这里只有造数据的函数,不含任何断言,也不 import 被测代码 —— 造数据的一侧不该依赖被测的一侧。
"""
from __future__ import annotations

import math
import struct
from functools import lru_cache
from pathlib import Path

import numpy as np

FIELD_NM = 12.0         # register 级测试的视野
SXM_W_NM = 10.0         # 合成 .sxm 的帧宽
SXM_NPX = 160


def poisson_disk(w: float, h: float, rmin: float, seed: int) -> "np.ndarray":
    """随机但互相隔开 ≥ rmin 的点(飞镖法)—— 一个**不周期**的分子排布,配准的峰是唯一的。"""
    rng = np.random.default_rng(seed)
    n_try = int(40 * w * h / rmin ** 2)
    cand = np.column_stack([rng.uniform(-w / 2, w / 2, n_try), rng.uniform(-h / 2, h / 2, n_try)])
    cell = rmin / math.sqrt(2)
    grid: dict = {}
    kept = []
    for p in cand:
        gi = (int((p[0] + w / 2) // cell), int((p[1] + h / 2) // cell))
        ok = True
        for di in range(-2, 3):
            for dj in range(-2, 3):
                q = grid.get((gi[0] + di, gi[1] + dj))
                if q is not None and math.hypot(*(p - q)) < rmin:
                    ok = False
                    break
            if not ok:
                break
        if ok:
            grid[gi] = p
            kept.append(p)
    return np.array(kept)


@lru_cache(maxsize=None)
def scene(seed: int = 7, size: float = 24.0) -> "np.ndarray":
    return poisson_disk(size, size, 0.6, seed)


def view(sc: "np.ndarray", shift, *, w=FIELD_NM, h=FIELD_NM, noise=0.0, drop=0.0, spurious=0.0,
         seed=0) -> "np.ndarray":
    """视野 w×h(帧心在原点)里看到的东西,当整个场景相对名义位置挪了 ``shift``(特征位置 + shift)。
    视野外的分子看不到(平移后进出视野的边缘效应是真实的)。"""
    rng = np.random.default_rng(1000 + seed)
    p = sc + np.asarray(shift, dtype=float)
    out = p[(np.abs(p[:, 0]) <= w / 2) & (np.abs(p[:, 1]) <= h / 2)]
    if noise:
        out = out + rng.normal(0.0, noise, out.shape)
    if drop:
        out = out[rng.random(len(out)) > drop]
    if spurious:
        k = int(spurious * len(out))
        out = np.vstack([out, np.column_stack([rng.uniform(-w / 2, w / 2, k),
                                               rng.uniform(-h / 2, h / 2, k)])])
    return out


def hex_lattice(a: float, w: float, h: float, shift=(0.0, 0.0)) -> "np.ndarray":
    a1 = np.array([a, 0.0])
    a2 = np.array([a / 2, a * math.sqrt(3) / 2])
    n = int(max(w, h) / a) + 6
    pts = np.array([i * a1 + j * a2 + np.asarray(shift) for i in range(-n, n + 1) for j in range(-n, n + 1)])
    return pts[(np.abs(pts[:, 0]) <= w / 2) & (np.abs(pts[:, 1]) <= h / 2)]


def render(mols, *, w_nm=SXM_W_NM, npx=SXM_NPX, sigma=0.15, amp=1e-10, noise=0.0, seed=0) -> "np.ndarray":
    """行 0 = 帧上沿的 Z 图(米):每个分子一个高斯 + 一张缓倾的平面(检测前会被拉平)。"""
    rng = np.random.default_rng(seed)
    col = (np.arange(npx) + 0.5) / npx * w_nm - w_nm / 2
    row = w_nm / 2 - (np.arange(npx) + 0.5) / npx * w_nm
    X, Y = np.meshgrid(col, row)
    z = np.zeros((npx, npx))
    for mx, my in mols:
        if abs(mx) < w_nm / 2 + 1 and abs(my) < w_nm / 2 + 1:
            z += amp * np.exp(-((X - mx) ** 2 + (Y - my) ** 2) / (2 * sigma ** 2))
    z += 0.5e-12 * np.arange(npx)[None, :] + 0.3e-12 * np.arange(npx)[:, None]
    if noise:
        z += rng.normal(0.0, noise, z.shape)
    return z


def write_sxm(path, arr, *, offset_nm=(0.0, 0.0), w_nm=SXM_W_NM, angle_deg=0.0, scan_dir="down",
              bias=0.1) -> Path:
    ny, nx = arr.shape
    rng_m = w_nm * 1e-9
    header = (
        ":NANONIS_VERSION:\n2\n"
        ":SCANIT_TYPE:\n\t FLOAT            MSBFIRST\n"
        ":REC_DATE:\n 26.10.2026\n:REC_TIME:\n12:00:00\n"
        f":BIAS:\n\t{bias:.6E}\n"
        f":SCAN_PIXELS:\n{nx:>10d}{ny:>10d}\n"
        f":SCAN_RANGE:\n{rng_m:>19.6E}{rng_m:>19.6E}\n"
        f":SCAN_OFFSET:\n{offset_nm[0] * 1e-9:>19.6E}{offset_nm[1] * 1e-9:>19.6E}\n"
        f":SCAN_ANGLE:\n{angle_deg:>12.6E}\n"
        f":SCAN_DIR:\n{scan_dir}\n"
        ":Z-CONTROLLER>SETPOINT:\n10.0000E-12\n"
        ":DATA_INFO:\n\tChannel\tName\tUnit\tDirection\tCalibration\tOffset\n"
        "\t14\tZ\tm\tfwd\t9.000E-9\t0.000E+0\n"
        ":SCANIT_END:\n\n"
    )
    data = np.flipud(arr) if scan_dir == "up" else arr      # 「up」扫描的第一行是帧底
    flat = data.astype(np.float32).ravel()
    path = Path(path)
    path.write_bytes(header.encode("utf-8") + b"\x1a\x04" + struct.pack(">%df" % flat.size, *flat.tolist()))
    return path
