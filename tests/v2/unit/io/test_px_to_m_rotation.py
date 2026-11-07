# -*- coding: utf-8 -*-
"""把 ``px_to_m`` 的旋转符号钉死。

背景:``px_to_m``(帧内像素 → 压电坐标)一度误用 ``R(+θ)``,与图库
``to_frame``(压电 → 帧内,``frame = R(+θ)·offset``)**同向**而非互逆 ——
于是 θ≠0 的图上针尖目标被算到镜像位置。图库约定「正 SCAN_ANGLE = 顺时针」,
``to_frame`` 就按这个出图。``px_to_m`` 是它的逆,必须用 ``R(−θ)``。

这条测试不重新判断「哪个方向对」(那是图库的约定),而是把
**px_to_m 必须是 to_frame 的逆**钉住:谁再把符号翻回去,这里当场红。
"""
from __future__ import annotations

import math

import pytest

from mast.io.mosaic import px_to_m
from mast.gallery.figures.common import to_frame

NX, NY = 256, 256
W, H = 38.4, 38.4          # nm(方形像素;符号与单位无关)
CX, CY = 10.0, -20.0       # nm


def _frame_nm_to_px(xp: float, yp: float) -> tuple[float, float]:
    """px_to_m 里 (dx,dy) 定义的精确逆:帧内 nm(x′右 y′上)→ 像素 (col,row)。"""
    col = (xp + W / 2) / W * NX - 0.5
    row = (H / 2 - yp) / H * NY - 0.5
    return col, row


@pytest.mark.parametrize("ang", [0.0, 15.0, 30.0, 90.0, -45.0, 137.0])
@pytest.mark.parametrize("xs,ys", [(10.0, -20.0), (13.0, -16.0),
                                   (25.0, 5.0), (-3.0, -40.0)])
def test_px_to_m_inverts_to_frame(ang, xs, ys):
    """压电 →(to_frame)→ 帧内 nm →(像素)→(px_to_m)→ 压电,必须回到原点。

    px_to_m=R(+θ) 时,θ≠0 这条就不闭合(回到镜像点)。"""
    f = {"ang": ang, "cx": CX, "cy": CY}
    xp, yp = to_frame(f, xs, ys)                 # 压电 nm → 帧内 nm
    col, row = _frame_nm_to_px(xp, yp)           # 帧内 nm → 像素
    gx, gy = px_to_m(col, row, nx=NX, ny=NY,
                     cx_m=CX, cy_m=CY, w_m=W, h_m=H, angle_deg=ang)
    assert gx == pytest.approx(xs, abs=1e-9)
    assert gy == pytest.approx(ys, abs=1e-9)


def test_clockwise_90deg_concrete():
    """人工核验:正角=顺时针。θ=90° 下帧内 +x′(右) 必落到压电 −y(下)。"""
    # 帧心右侧 w/4 处、帧心那一行的像素。
    col = (W / 4 + W / 2) / W * NX - 0.5         # dx = +w/4
    row = (H / 2 - 0.0) / H * NY - 0.5           # dy = 0
    gx, gy = px_to_m(col, row, nx=NX, ny=NY,
                     cx_m=0.0, cy_m=0.0, w_m=W, h_m=H, angle_deg=90.0)
    assert gx == pytest.approx(0.0, abs=1e-9)
    assert gy == pytest.approx(-W / 4, abs=1e-9)   # 落到 −y(下),不是 +y


def test_angle_zero_unchanged():
    """θ=0 恒等变换不受符号订正影响(sin 项为零)。"""
    gx, gy = px_to_m(NX - 0.5, 0.0, nx=NX, ny=NY,   # 右上角附近
                     cx_m=0.0, cy_m=0.0, w_m=W, h_m=H, angle_deg=0.0)
    assert gx == pytest.approx(W / 2, abs=1e-6)      # 右 → +x
    assert gy == pytest.approx(H / 2 - 0.5 / NY * H, abs=1e-6)  # 上 → +y
