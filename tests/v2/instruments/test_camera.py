"""对光相机驱动（契约 §13.1「相机」）：mast.instruments.camera。

* 光斑统计对已知高斯：质心、二阶矩 FWHM（2.355σ）、热像素与第二个光斑拽不走质心；
* 彩色传感器 2×2 合成灰度（红光只亮 R 像素也照样定得准）；
* fake 后端全流程：连接 → 直播 → 改设置（头几帧标 settled=false）→ 停 → 断开 → 再连；
* 默认不碰相机；没启用拒绝；被 ThorImageCAM 占着时点名、没有证据时不点名；
* 超范围拒绝不夹；取帧出错进 error，断开后能再连；控制命令不被取帧线程饿死；
* tsi 后端对着 ctypes 层的替身 DLL：调用顺序、彻底释放（另一个程序能接着打开）、ROI 端点、
  次要来源缺失时的退路、SDK 错误文本；
* 手动控制面板的描述符合 hub 的 schema，动作只用技能真有的参数；
* 不漏线程。
"""
from __future__ import annotations

import json
import math
import threading
import time

import numpy as np
import pytest

from mast.instruments import camera as C
from mast.instruments.base import InstrumentError, InstrumentUnavailable
from mast.instruments.camera import (
    Camera,
    CameraBusy,
    CameraConfig,
    CameraManager,
    CameraRefused,
    FakeBackend,
    FakeSpot,
    TsiBackend,
    gray_image,
    spot_stats,
)

from tests.v2.instruments.fake_tsi_dll import FakeTsiDll, FakeUsbCamera, loader_for

FWHM = 2.0 * math.sqrt(2.0 * math.log(2.0))
BUSY_TEXT = "被 ThorImageCAM 占用，请先关掉它"


@pytest.fixture(autouse=True)
def _no_camera_threads_left():
    yield
    deadline = time.monotonic() + 3.0
    left: list[str] = []
    while time.monotonic() < deadline:
        left = [t.name for t in threading.enumerate() if t.name.startswith("camera-live-")]
        if not left:
            return
        time.sleep(0.02)
    pytest.fail(f"取帧线程没收干净：{left}")


@pytest.fixture(autouse=True)
def _fresh_sdk_cache(monkeypatch):
    monkeypatch.setattr(C._Sdk, "_instances", {})


class _Cams:
    """建相机、测试结束时一律断开（失败的测试也不留线程）。"""

    def __init__(self) -> None:
        self.cams: list[Camera] = []
        self.factory_calls = 0

    def make(self, backend, *, enabled: bool = True, holders=(), camera_id: str = "cam0") -> Camera:
        def factory(cfg):
            self.factory_calls += 1
            return backend

        cam = Camera(CameraConfig(camera_id=camera_id, backend="fake", enabled=enabled),
                     backend_factory=factory, holder_probe=lambda: list(holders))
        self.cams.append(cam)
        return cam


@pytest.fixture
def cams():
    box = _Cams()
    yield box
    for cam in box.cams:
        cam.disconnect()


def _spot_image(w, h, *, x, y, sx, sy, amp=600.0, bg=40.0, angle_deg=0.0, noise=1.0, seed=1):
    fb = FakeBackend(width=w, height=h, mosaic=False, seed=seed,
                     spot=FakeSpot(x=x, y=y, sigma_x=sx, sigma_y=sy, angle_deg=angle_deg, peak=amp,
                                   background=bg, noise=noise))
    return fb.render()


def _frame(cam: Camera, after: int = 0, timeout: float = 5.0):
    f = cam.wait_frame(after, timeout)
    assert f is not None, "等不到帧"
    return f


# ── 光斑统计 ──────────────────────────────────────────────────────────────────

def test_gaussian_centroid_and_fwhm_are_recovered():
    raw = _spot_image(320, 240, x=150.3, y=101.7, sx=8.0, sy=11.0)
    st = spot_stats(raw.astype(np.float32), raw=raw, full_scale=1023)
    assert st["spot"] is True
    assert st["centroid_px"] == pytest.approx([150.3, 101.7], abs=0.05)
    assert st["fwhm_px"][0] == pytest.approx(FWHM * 8.0, rel=0.015)
    assert st["fwhm_px"][1] == pytest.approx(FWHM * 11.0, rel=0.015)
    assert st["max"] == float(raw.max()) and st["saturated_frac"] == 0.0


def test_a_rotated_ellipse_gives_its_axes_and_angle():
    raw = _spot_image(320, 240, x=160.0, y=120.0, sx=14.0, sy=6.0, angle_deg=30.0)
    st = spot_stats(raw.astype(np.float32), raw=raw, full_scale=1023)
    assert st["fwhm_major_px"] == pytest.approx(FWHM * 14.0, rel=0.02)
    assert st["fwhm_minor_px"] == pytest.approx(FWHM * 6.0, rel=0.03)
    assert st["angle_deg"] == pytest.approx(30.0, abs=1.0)


def test_hot_pixel_and_a_second_spot_do_not_drag_the_centroid():
    raw = _spot_image(320, 240, x=200.0, y=150.0, sx=7.0, sy=7.0).astype(np.int32)
    raw[20, 20] = 1023                                            # 热像素，比光斑亮
    yy, xx = np.mgrid[0:240, 0:320]
    # 第二个光斑（反射）：比主光斑的半高还亮，只能靠「峰所在的连通域」把它排除在外
    raw += np.rint(420.0 * np.exp(-((xx - 60) ** 2 + (yy - 200) ** 2) / (2 * 6.0 ** 2))).astype(np.int32)
    raw = raw.astype(np.uint16)
    st = spot_stats(raw.astype(np.float32), raw=raw, full_scale=1023)
    assert st["centroid_px"] == pytest.approx([200.0, 150.0], abs=0.1)
    assert st["fwhm_px"][0] == pytest.approx(FWHM * 7.0, rel=0.03)
    assert st["max"] == 1023.0                                     # max 如实报原始像素
    assert st["saturated_frac"] == pytest.approx(1 / raw.size)


def test_no_spot_means_none_not_a_guess():
    raw = _spot_image(160, 120, x=80, y=60, sx=5, sy=5, amp=0.0, noise=2.0)
    st = spot_stats(raw.astype(np.float32), raw=raw, full_scale=1023)
    assert st["spot"] is False
    assert st["centroid_px"] is None and st["fwhm_px"] is None
    assert st["background"] == pytest.approx(40.0, abs=1.0)


def test_bayer_two_by_two_gray_is_phase_free_and_follows_a_red_laser(cams):
    flat = np.zeros((8, 8), np.uint16)
    flat[0::2, 0::2], flat[0::2, 1::2], flat[1::2, 0::2], flat[1::2, 1::2] = 400, 200, 200, 40
    assert gray_image(flat, True).shape == (4, 4)
    np.testing.assert_allclose(gray_image(flat, True), 210.0)
    np.testing.assert_allclose(gray_image(flat[1:-1, 1:-1], True), 210.0)     # 换一个相位也一样

    from mast.instruments.camera import color_image
    rgb = color_image(flat, True)
    assert rgb.shape == (8, 8, 3)
    np.testing.assert_allclose(rgb[0, 0], [400, 200, 40], atol=1.0)          # R 位点：邻域推 G/B

    fb = FakeBackend(width=640, height=480, mosaic=True, bayer_gains=(1.0, 0.15, 0.03),
                     spot=FakeSpot(x=301.4, y=222.6, sigma_x=9.0, sigma_y=9.0, peak=900.0))
    cam = cams.make(fb)
    cam.connect()
    cam.start_live()
    st = cam.frame_stats(_frame(cam))
    assert st["pixel_scale"] == 2 and st["frame_w"] == 640 and st["image_w"] == 320
    # 只亮 R 像素时 2×2 合成把光算在块中心：质心偏移最多半个传感器像素（模块说明里写明的代价）
    assert st["centroid_px"] == pytest.approx([301.4, 222.6], abs=0.5)
    assert st["fwhm_px"][0] == pytest.approx(FWHM * 9.0, rel=0.03)
    # 白光（各色响应相同）没有这个偏移
    fb.bayer_gains = None
    st = cam.frame_stats(_frame(cam, cam.last_seq))
    assert st["centroid_px"] == pytest.approx([301.4, 222.6], abs=0.05)


def test_roi_and_binning_keep_coordinates_in_sensor_pixels(cams):
    fb = FakeBackend(width=1440, height=1080, mosaic=False, binning_range=(1, 4),
                     spot=FakeSpot(x=700.3, y=512.7, sigma_x=10.0, sigma_y=10.0, peak=150.0))
    cam = cams.make(fb)
    cam.connect()
    cam.start_live()
    cam.set_roi(400, 300, 640, 480)
    st = cam.frame_stats(_frame(cam, cam.settled_after))
    assert st["roi"] == {"x": 400, "y": 300, "w": 640, "h": 480, "binning": 1}
    assert st["frame_w"] == 640 and st["frame_h"] == 480
    assert st["centroid_px"] == pytest.approx([300.3, 212.7], abs=0.05)       # ROI 内
    assert st["centroid_sensor_px"] == pytest.approx([700.3, 512.7], abs=0.05)
    cam.set_roi(400, 300, 640, 480, binning=2)
    st = cam.frame_stats(_frame(cam, cam.settled_after))
    assert st["image_w"] == 320 and st["frame_w"] == 640 and st["pixel_scale"] == 2
    assert st["centroid_sensor_px"] == pytest.approx([700.3, 512.7], abs=0.1)
    assert st["fwhm_px"][0] == pytest.approx(FWHM * 10.0, rel=0.03)


# ── 状态机与全流程（fake 后端）─────────────────────────────────────────────────

def test_full_flow_and_reconnect_after_disconnect(cams):
    fb = FakeBackend(width=320, height=240, mosaic=False, max_fps=60,
                     spot=FakeSpot(x=160.0, y=120.0, sigma_x=6.0, sigma_y=6.0))
    cam = cams.make(fb)
    st = cam.connect()
    assert st["state"] == "connected" and st["sensor"]["width"] == 320
    assert cam.latest_frame() is None and cam.jpeg() is None                  # 没在直播没有画面

    cam.start_live()
    f1 = _frame(cam)
    data, f = cam.jpeg(max_w=160)
    assert data[:2] == b"\xff\xd8" and f.seq >= f1.seq
    color_data, fc = cam.jpeg(mode="color", max_w=160)                        # color 忽略 max_w
    assert color_data[:2] == b"\xff\xd8" and fc.seq >= f1.seq
    from PIL import Image
    import io as _io
    with Image.open(_io.BytesIO(color_data)) as im:
        assert im.size == (320, 240) and im.mode in ("RGB", "YCbCr")

    before = cam.last_seq
    cam.set_exposure(20_000)
    limit = cam.settled_after
    assert limit >= before + C.SETTLE_FRAMES
    seen = [_frame(cam, before)]
    while seen[-1].seq <= limit + 1:
        seen.append(_frame(cam, seen[-1].seq))
    for f in seen:
        if f.exposure_us == 20_000:
            assert f.settled == (f.seq > limit), (f.seq, limit)                 # 改完头几帧不算数
        else:
            assert f.seq <= limit - C.SETTLE_FRAMES                           # 改之前拍的，标旧值
    assert seen[-1].settled and seen[-1].exposure_us == 20_000
    cam.set_gain(6.0)
    assert cam.status()["gain_db"] == 6.0

    assert cam.stop_live()["state"] == "connected"
    assert cam.disconnect()["state"] == "disconnected"
    assert fb.close_calls == 1 and not fb.is_open and not fb.armed

    cam.connect()
    cam.start_live()
    assert _frame(cam).seq > seen[-1].seq                                     # 帧号不回头
    cam.disconnect()
    assert fb.open_calls == 2 and fb.close_calls == 2


def test_nothing_opens_the_camera_except_connect(cams, tmp_path):
    fb = FakeBackend(width=64, height=48)
    cam = cams.make(fb)
    cam.status()
    cam.stats()
    assert cam.jpeg() is None and cam.latest_frame() is None
    mgr = CameraManager([cam.cfg], backend_factory=lambda cfg: fb, holder_probe=lambda: [])
    prev = C.install_camera_manager(mgr)
    try:
        C.manual_devices()
    finally:
        C.install_camera_manager(prev)
    assert cams.factory_calls == 0 and fb.open_calls == 0


def test_a_disabled_camera_refuses_to_connect(cams):
    fb = FakeBackend(width=64, height=48)
    cam = cams.make(fb, enabled=False)
    with pytest.raises(InstrumentUnavailable, match="未启用"):
        cam.connect()
    assert cams.factory_calls == 0


@pytest.mark.parametrize("how", ["busy", "missing"])
def test_busy_with_thorimagecam_running_names_it(cams, how):
    fb = FakeBackend(width=64, height=48, **{how: True})
    cam = cams.make(fb, holders=["ThorImageCAM"])
    with pytest.raises(CameraBusy) as ei:
        cam.connect()
    assert BUSY_TEXT in str(ei.value) and ei.value.holder == "ThorImageCAM"
    assert cam.state == "disconnected" and not fb.is_open
    assert cam.status()["occupied_by"] == "ThorImageCAM"


def test_busy_without_a_known_holder_blames_nobody(cams):
    cam = cams.make(FakeBackend(width=64, height=48, busy=True), holders=[])
    with pytest.raises(InstrumentUnavailable) as ei:
        cam.connect()
    assert not isinstance(ei.value, CameraBusy)
    assert "占用，请先关掉它" not in str(ei.value) and "没看到已知的占用程序" in str(ei.value)
    assert cam.status()["occupied_by"] is None


def test_out_of_range_settings_are_refused_not_clamped(cams):
    fb = FakeBackend(width=640, height=480, mosaic=True)
    cam = cams.make(fb)
    cam.connect()
    before = cam.status()
    for call, match in [
        (lambda: cam.set_exposure(10), "曝光"),
        (lambda: cam.set_exposure(30_000_000), "曝光"),
        (lambda: cam.set_gain(60.0), "增益"),
        (lambda: cam.set_roi(1, 0, 100, 100), "偶数"),
        (lambda: cam.set_roi(600, 0, 100, 100), "超出传感器"),
        (lambda: cam.set_roi(0, 0, 100, 100, binning=2), "合并"),
    ]:
        with pytest.raises(CameraRefused, match=match):
            call()
    after = cam.status()
    assert (after["exposure_us"], after["gain_db"], after["roi"]) == \
        (before["exposure_us"], before["gain_db"], before["roi"])
    assert fb.exposure_us == 10_000 and fb.roi == (0, 0, 640, 480)


def test_a_frame_error_puts_the_camera_in_error_until_disconnect(cams):
    fb = FakeBackend(width=64, height=48, mosaic=False, max_fps=60)
    cam = cams.make(fb)
    cam.connect()
    cam.start_live()
    _frame(cam)
    fb.fail_next_poll = OSError("USB 掉线")
    deadline = time.monotonic() + 3.0
    while cam.state != "error" and time.monotonic() < deadline:
        time.sleep(0.01)
    st = cam.status()
    assert st["state"] == "error" and "USB 掉线" in st["error"]
    assert cam.latest_frame() is None and cam.stats()["centroid_px"] is None
    for call in (cam.start_live, lambda: cam.set_exposure(1000), cam.connect):
        with pytest.raises(InstrumentError, match="先断开"):
            call()
    assert cam.disconnect()["state"] == "disconnected"
    cam.connect()
    cam.start_live()
    _frame(cam, cam.last_seq)
    assert cam.state == "live"


class _SlowPoll(FakeBackend):
    """取帧线程每次在设备锁里待 30 ms、一直拿不到帧 —— 最容易把控制命令饿死的形状。"""

    def poll(self):
        time.sleep(0.03)
        return None


def test_commands_are_not_starved_by_the_frame_thread(cams):
    cam = cams.make(_SlowPoll(width=64, height=48))
    cam.connect()
    cam.start_live()
    worst = 0.0
    for i in range(15):
        t0 = time.perf_counter()
        cam.set_exposure(1000 + i)
        worst = max(worst, time.perf_counter() - t0)
    assert worst < 0.3, f"控制命令等了 {worst:.2f} s"


def test_wait_frame_respects_abort_and_returns_none_when_not_live(cams):
    cam = cams.make(_SlowPoll(width=64, height=48))
    cam.connect()
    assert cam.wait_frame(0, 1.0) is None                                    # 没在直播
    cam.start_live()
    t0 = time.monotonic()
    assert cam.wait_frame(0, 5.0, check_abort=lambda: True) is None
    assert time.monotonic() - t0 < 1.0


# ── 配置与管理器 ──────────────────────────────────────────────────────────────

def test_config_loader_skips_bad_entries_and_defaults_to_disabled(tmp_path, monkeypatch):
    p = tmp_path / "cameras.json"
    p.write_text(json.dumps({"cameras": [
        {"camera_id": "ok"},
        {"camera_id": "on", "enabled": True, "backend": "fake"},
        {"camera_id": "str_true", "enabled": "true"},
        {"camera_id": "ok"},                                   # 重复
        {"camera_id": "bad id"},
        {"camera_id": "nope", "backend": "usb"},
    ]}), encoding="utf-8")
    got = {c.camera_id: c for c in C.load_camera_configs(p)}
    assert set(got) == {"ok", "on", "str_true"}
    assert got["ok"].enabled is False and got["ok"].backend == "tsi"
    assert got["ok"].dll_dir == C.DEFAULT_DLL_DIR
    assert got["on"].enabled is True
    assert got["str_true"].enabled is False                    # 只认 JSON 的 true

    monkeypatch.setattr(C, "_sdk_installed", lambda: False)
    assert C.load_camera_configs(tmp_path / "absent.json") == []
    monkeypatch.setattr(C, "_sdk_installed", lambda: True)
    (only,) = C.load_camera_configs(tmp_path / "absent.json")
    assert only.enabled is False and only.camera_id == "zelux"


def test_manager_rereads_the_config_but_keeps_a_connected_camera(tmp_path):
    p = tmp_path / "cameras.json"

    def write(label):
        p.write_text(json.dumps({"cameras": [
            {"camera_id": "c1", "backend": "fake", "enabled": True, "label": label,
             "fake": {"width": 64, "height": 48, "mosaic": False}}]}), encoding="utf-8")

    write("一号")
    mgr = CameraManager(path=p, holder_probe=lambda: [])
    cam = mgr.camera()
    assert cam.cfg.label == "一号"
    cam.connect()
    write("改过的名字")
    assert mgr.camera("c1") is cam                              # 连着：不换
    cam.disconnect()
    assert mgr.camera("c1").cfg.label == "改过的名字"            # 断开之后换上新配置
    with pytest.raises(KeyError, match="没有相机"):
        mgr.camera("c2")
    p.write_text(json.dumps({"cameras": []}), encoding="utf-8")
    with pytest.raises(KeyError, match="没有配置相机"):
        mgr.camera()
    mgr.close_all()


def test_holder_check_reads_process_names_only_and_is_cached(monkeypatch):
    import psutil

    class P:
        def __init__(self, name):
            self.info = {"name": name}

    calls = []

    def fake_iter(attrs):
        calls.append(list(attrs))
        return [P("explorer.exe"), P("ThorImageCAM.exe"), P("python.exe")]

    monkeypatch.setattr(psutil, "process_iter", fake_iter)
    monkeypatch.setattr(C, "_holder_cache", (-1e9, []))
    assert C.holder_processes() == ["ThorImageCAM"]
    assert C.holder_processes() == ["ThorImageCAM"]
    assert calls == [["name"]]                                  # 只要进程名，2 s 内不重查

    def broken(attrs):
        raise PermissionError("no")

    monkeypatch.setattr(psutil, "process_iter", broken)
    monkeypatch.setattr(C, "_holder_cache", (-1e9, []))
    assert C.holder_processes() == []                           # 读不到不是答案，也不抛


# ── tsi 后端（ctypes 层替身）──────────────────────────────────────────────────

def _tsi_cam(cams, dll, *, holders=(), serial=None):
    load = loader_for(dll)
    cfg = CameraConfig(camera_id="zelux", backend="tsi", enabled=True, serial=serial,
                       dll_dir="X:/fake-thorlabs-bin", poll_timeout_ms=20)
    cam = Camera(cfg, backend_factory=lambda c: TsiBackend(c, loader=load),
                 holder_probe=lambda: list(holders))
    cams.cams.append(cam)
    return cam, load


def test_tsi_connect_live_and_full_release(cams):
    dll = FakeTsiDll()
    cam, load = _tsi_cam(cams, dll)
    assert load.loads == []                                     # 构造不载 DLL
    st = cam.connect()
    s = st["sensor"]
    assert (s["width"], s["height"], s["bit_depth"], s["mosaic"]) == (64, 48, 10, True)
    assert s["serial"] == "18342" and s["model"] == "CS165CU"
    assert s["exposure_range_us"] == [40, 26_843_000] and s["gain_range_db"] == [0.0, 48.0]
    assert s["binning_range"] == [1, 1]                         # 彩色不合并
    assert dll.operation_mode == 0 and dll.frames_per_trigger == 0 and dll.poll_timeout_ms == 20
    assert st["roi"] == {"x": 0, "y": 0, "w": 64, "h": 48, "binning": 1}

    cam.start_live()
    assert dll.armed and dll.triggered
    sent = dll.queue_frame()
    f = _frame(cam)
    assert np.array_equal(f.raw, sent)
    dll.queue_frame(7)
    _frame(cam, f.seq)                                         # 替身此时把上一块缓冲区写花了
    assert np.array_equal(f.raw, sent), "SDK 的缓冲区要立刻拷走"

    dll.calls.clear()
    cam.disconnect()
    tail = [c for c in dll.calls if c in ("tl_camera_disarm", "tl_camera_close_camera", "tl_camera_close_sdk")]
    assert tail == ["tl_camera_disarm", "tl_camera_close_camera", "tl_camera_close_sdk"]
    assert dll.sdk_open == 0 and dll.handle == 0 and dll.dev.owner is None


def test_after_disconnect_another_program_can_open_the_camera(cams):
    usb = FakeUsbCamera()
    mast_dll, vendor = FakeTsiDll(usb), FakeTsiDll(usb)        # 两个进程，一台相机
    cam, _ = _tsi_cam(cams, mast_dll, holders=["ThorImageCAM"])

    vendor._impl_tl_camera_open_sdk()
    assert vendor._impl_tl_camera_open_camera(b"18342", _Ref()) == 0          # ThorImageCAM 先占着
    with pytest.raises(CameraBusy, match=BUSY_TEXT):
        cam.connect()
    assert mast_dll.sdk_open == 0 and usb.owner is vendor                      # 失败也把 SDK 放掉

    vendor._impl_tl_camera_close_camera(vendor.handle)
    cam.connect()
    assert usb.owner is mast_dll
    assert vendor._impl_tl_camera_open_camera(b"18342", _Ref()) != 0          # 这时轮到它打不开
    cam.disconnect()
    assert vendor._impl_tl_camera_open_camera(b"18342", _Ref()) == 0          # 断开之后它能用


class _Ref:
    """``ctypes.byref`` 的样子：替身只认 ``._obj.value``。"""

    def __init__(self):
        import ctypes

        self._obj = ctypes.c_void_p()


@pytest.mark.parametrize("inclusive", [True, False])
def test_roi_lower_right_end_is_read_back_not_assumed(cams, inclusive):
    dll = FakeTsiDll(roi_inclusive=inclusive)
    cam, _ = _tsi_cam(cams, dll)
    cam.connect()
    st = cam.set_roi(10, 6, 32, 20)
    assert st["roi"] == {"x": 10, "y": 6, "w": 32, "h": 20, "binning": 1}
    assert dll.roi == ([10, 6, 41, 25] if inclusive else [10, 6, 42, 26])
    cam.start_live()
    dll.queue_frame()
    assert _frame(cam).raw.shape == (20, 32)


def test_secondary_functions_missing_fall_back(cams):
    dll = FakeTsiDll(missing=("tl_camera_get_image_width", "tl_camera_get_image_height",
                              "tl_camera_get_operation_mode", "tl_camera_get_binx_range"),
                     device=FakeUsbCamera(sensor_type=0))
    cam, _ = _tsi_cam(cams, dll)
    st = cam.connect()
    assert st["sensor"]["mosaic"] is False and st["sensor"]["binning_range"] == [1, 1]
    cam.start_live()
    dll.queue_frame()
    assert _frame(cam).raw.shape == (48, 64)                   # 尺寸由 ROI / 合并推出来


@pytest.mark.parametrize("missing, match", [
    ("tl_camera_issue_software_trigger", "软件触发"),
    ("tl_camera_arm", "tl_camera_arm"),
])
def test_missing_essential_functions_refuse_and_release(cams, missing, match):
    dll = FakeTsiDll(missing=(missing,))
    cam, _ = _tsi_cam(cams, dll)
    with pytest.raises(InstrumentUnavailable, match=match):
        cam.connect()
    assert dll.sdk_open == 0 and dll.dev.owner is None and cam.state == "disconnected"


def test_sdk_error_text_reaches_the_caller_and_camera_stays_usable(cams):
    dll = FakeTsiDll(fail={"tl_camera_set_exposure_time": 1004})
    cam, _ = _tsi_cam(cams, dll)
    cam.connect()
    with pytest.raises(InstrumentError, match="injected failure"):
        cam.set_exposure(5000)
    assert cam.state == "connected"
    st = cam.set_gain(12.3)
    assert st["gain_db"] == pytest.approx(12.3) and dll.gain_index == 123


def test_operation_mode_readback_mismatch_refuses_and_releases(cams):
    dll = FakeTsiDll()
    dll.tl_camera_get_operation_mode = dll._export(
        "tl_camera_get_operation_mode", lambda h, ref: (FakeTsiDll._set(ref, 1), 0)[1])
    cam, _ = _tsi_cam(cams, dll)
    with pytest.raises(InstrumentError, match="不是软件触发"):
        cam.connect()
    assert dll.sdk_open == 0 and dll.dev.owner is None


def test_one_sdk_is_shared_and_closed_by_the_last_user():
    dll = FakeTsiDll()
    load = loader_for(dll)
    sdk1 = C._Sdk.acquire("X:/same", loader=load)
    sdk2 = C._Sdk.acquire("X:/same", loader=load)
    assert sdk1 is sdk2 and sdk1.refs == 2 and len(load.loads) == 1 and dll.sdk_open == 1
    sdk1.release()
    assert dll.sdk_open == 1
    sdk2.release()
    assert dll.sdk_open == 0
    sdk2.release()                                              # 多放一次不会把计数放成负的
    assert sdk2.refs == 0


# ── 手动控制面板的描述 ────────────────────────────────────────────────────────

_CONTRACT_ACTIONS = {"camera_connect", "camera_disconnect", "live_start", "live_stop", "snapshot",
                     "set_exposure", "set_gain", "set_roi"}


@pytest.fixture(scope="module")
def skills():
    from mast.agents.instrument_control.tools import discover_instrument_skills

    return discover_instrument_skills()


def test_manual_devices_match_the_hub_schema_and_the_skills(skills):
    from mast.api.schemas_manual import ManualDevice

    holders: list[str] = []
    fb = FakeBackend(width=64, height=48, mosaic=False)
    mgr = CameraManager([CameraConfig(camera_id="zelux", backend="fake", enabled=True),
                         CameraConfig(camera_id="spare", backend="fake", enabled=False)],
                        backend_factory=lambda cfg: fb, holder_probe=lambda: list(holders))
    prev = C.install_camera_manager(mgr)
    try:
        holders.append("ThorImageCAM")
        devs = {d["id"]: d for d in C.manual_devices()}
        assert set(devs) == {"local/camera/zelux", "local/camera/spare"}
        for d in devs.values():
            ManualDevice.model_validate(d)
            assert {a["id"] for a in d["actions"]} == _CONTRACT_ACTIONS
            assert all(a["hint"] for a in d["actions"])                         # 每个按钮都说清做什么
            for a in d["actions"]:
                meta = skills._get_metadata(skills.get(a["skill"]))
                names = {p.name for p in meta.parameters}
                used = set(a["fixed"]) | {p["name"] for p in a["params"]}
                assert used <= names, (a["id"], used - names)
                assert meta.safety_level.value == "auto"
        z, s = devs["local/camera/zelux"], devs["local/camera/spare"]
        assert z["available"] is True and z["occupied_by"] == "ThorImageCAM"   # 有证据才填
        assert s["available"] is False and "enabled" in s["unavailable_reason"]
        assert z["readout"] == {"kind": "poll", "interval_s": 1.0}             # 轮询 /api/camera/{id}/stats

        cam = mgr.camera("zelux")
        cam.connect()
        z = {d["id"]: d for d in C.manual_devices()}["local/camera/zelux"]
        assert z["occupied_by"] is None                          # 自己连着就不是别人占着
        fb.fail_next_poll = OSError("掉线")
        cam.start_live()
        deadline = time.monotonic() + 3.0
        while cam.state != "error" and time.monotonic() < deadline:
            time.sleep(0.01)
        z = {d["id"]: d for d in C.manual_devices()}["local/camera/zelux"]
        assert z["available"] is False and "先断开" in z["unavailable_reason"]
    finally:
        mgr.close_all()
        C.install_camera_manager(prev)
