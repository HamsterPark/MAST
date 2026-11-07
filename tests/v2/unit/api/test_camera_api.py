"""对光相机的画面与统计端点（契约 §13.1「相机」）：GET /api/camera/{id}/frame.jpg、/stats。

* 不在直播时画面 404、统计照样回 200（各项为 null）；没有这台相机 404；端点从不打开相机；
* JPEG：尺寸跟 ROI 走、max_w 只缩不放、参数越界 422、不缓存；图像不走 GZip；
* 统计的坐标是 ROI 内的传感器像素，带 frame_w / frame_h；
* LAN 模式两条都要认证（AccessGate）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.modules.setdefault("nanonis_spm", MagicMock())

_MASTV2_ROOT = str(Path(__file__).resolve().parents[4] / "MASTv2")
if sys.path and sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)

import base64  # noqa: E402
import io  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from mast.instruments import camera as C  # noqa: E402
from mast.instruments.camera import CameraConfig, CameraManager, FakeBackend, FakeSpot  # noqa: E402

FWHM = 2.3548200450309493


@pytest.fixture(scope="module")
def client():
    from mast.api.app import create_app

    return TestClient(create_app(dev_cors=False))


@pytest.fixture(scope="module")
def lan_client():
    from mast.api.app import create_app

    return TestClient(create_app(dev_cors=False, auth=("op", "secret")))


@pytest.fixture
def fake():
    fb = FakeBackend(width=640, height=480, mosaic=False, max_fps=60,
                     spot=FakeSpot(x=300.4, y=200.6, sigma_x=9.0, sigma_y=9.0))
    mgr = CameraManager([CameraConfig(camera_id="zelux", backend="fake", enabled=True)],
                        backend_factory=lambda cfg: fb, holder_probe=lambda: [])
    prev = C.install_camera_manager(mgr)
    yield SimpleNamespace(fb=fb, mgr=mgr, cam=mgr.camera("zelux"))
    mgr.close_all()
    C.install_camera_manager(prev)
    deadline = time.monotonic() + 3.0
    while any(t.name.startswith("camera-live-") for t in threading.enumerate()):
        assert time.monotonic() < deadline, "取帧线程没收干净"
        time.sleep(0.02)


def _live(cam):
    cam.connect()
    cam.start_live()
    assert cam.wait_frame(cam.last_seq, 5.0) is not None


def _jpeg_size(body: bytes) -> tuple[int, int]:
    assert body[:2] == b"\xff\xd8"
    with Image.open(io.BytesIO(body)) as im:
        return im.size


def test_not_live_is_404_for_the_picture_and_nulls_for_the_stats(client, fake):
    r = client.get("/api/camera/zelux/frame.jpg")
    assert r.status_code == 404 and "没在直播" in r.json()["detail"]
    r = client.get("/api/camera/zelux/stats")
    assert r.status_code == 200
    s = r.json()
    assert s["state"] == "disconnected" and s["live"] is False and s["connected"] is False
    assert s["centroid_px"] is None and s["fwhm_px"] is None and s["frame_seq"] is None
    for path in ("/api/camera/nope/frame.jpg", "/api/camera/nope/stats"):
        r = client.get(path)
        assert r.status_code == 404 and "没有相机" in r.json()["detail"]
    fake.cam.connect()
    assert client.get("/api/camera/zelux/frame.jpg").status_code == 404     # 连着但没直播
    assert client.get("/api/camera/zelux/stats").json()["state"] == "connected"
    assert fake.fb.open_calls == 1                                         # 只有 connect 打开过


def test_endpoints_never_open_the_camera(client, fake):
    for _ in range(3):
        client.get("/api/camera/zelux/frame.jpg")
        client.get("/api/camera/zelux/stats")
    assert fake.fb.open_calls == 0 and fake.cam.state == "disconnected"


def test_the_picture_scales_down_only_and_checks_its_parameters(client, fake):
    _live(fake.cam)
    r = client.get("/api/camera/zelux/frame.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"
    assert int(r.headers["x-frame-seq"]) >= 1 and float(r.headers["x-frame-t"]) > 0
    assert _jpeg_size(r.content) == (640, 480)
    assert _jpeg_size(client.get("/api/camera/zelux/frame.jpg?max_w=320").content) == (320, 240)
    assert _jpeg_size(client.get("/api/camera/zelux/frame.jpg?max_w=4000").content) == (640, 480)
    assert client.get("/api/camera/zelux/frame.jpg?scale=auto&q=50").status_code == 200
    r_color = client.get("/api/camera/zelux/frame.jpg?mode=color&max_w=320")
    assert r_color.status_code == 200 and r_color.headers.get("x-frame-mode") == "color"
    assert _jpeg_size(r_color.content) == (640, 480)                          # color 忽略 max_w
    for bad in ("q=5", "q=96", "max_w=8", "max_w=5000", "scale=log", "mode=rainbow"):
        assert client.get(f"/api/camera/zelux/frame.jpg?{bad}").status_code == 422, bad


def test_the_picture_follows_the_roi(client, fake):
    _live(fake.cam)
    fake.cam.set_roi(200, 140, 200, 120)
    fake.cam.wait_frame(fake.cam.settled_after, 5.0)
    assert _jpeg_size(client.get("/api/camera/zelux/frame.jpg").content) == (200, 120)
    s = client.get("/api/camera/zelux/stats").json()
    assert s["roi"] == {"x": 200, "y": 140, "w": 200, "h": 120, "binning": 1}
    assert (s["frame_w"], s["frame_h"]) == (200, 120)
    assert s["centroid_px"] == pytest.approx([100.4, 60.6], abs=0.1)         # ROI 内
    assert s["centroid_sensor_px"] == pytest.approx([300.4, 200.6], abs=0.1)
    fake.cam.set_roi(0, 0, 200, 100)                                        # 光斑出了 ROI
    fake.cam.wait_frame(fake.cam.settled_after, 5.0)
    s = client.get("/api/camera/zelux/stats").json()
    assert s["centroid_px"] is None and s["fwhm_px"] is None


def test_images_are_not_gzipped(client, fake):
    _live(fake.cam)
    r = client.get("/api/camera/zelux/frame.jpg", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and "content-encoding" not in r.headers
    assert _jpeg_size(r.content) == (640, 480)


def test_stats_while_live(client, fake):
    _live(fake.cam)
    r = client.get("/api/camera/zelux/stats")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    s = r.json()
    assert s["live"] is True and s["state"] == "live" and s["error"] is None
    assert s["exposure_us"] == 10_000 and s["gain_db"] == 0.0
    assert s["roi"] == {"x": 0, "y": 0, "w": 640, "h": 480, "binning": 1}
    assert (s["frame_w"], s["frame_h"]) == (640, 480)
    assert s["centroid_px"] == pytest.approx([300.4, 200.6], abs=0.1)
    assert s["fwhm_px"] == pytest.approx([FWHM * 9.0] * 2, rel=0.02)
    assert s["max"] > 0 and s["saturated_frac"] == 0.0
    assert isinstance(s["frame_seq"], int) and s["t"] > 0 and s["settled"] is True


def test_lan_mode_needs_credentials_for_both(lan_client, fake):
    _live(fake.cam)
    for path in ("/api/camera/zelux/frame.jpg", "/api/camera/zelux/stats"):
        assert lan_client.get(path).status_code == 401
        auth = "Basic " + base64.b64encode(b"op:secret").decode()
        assert lan_client.get(path, headers={"Authorization": auth}).status_code == 200
