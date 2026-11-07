"""A stand-in for ``thorlabs_tsi_camera_sdk.dll`` at the ctypes boundary.

The real backend (:class:`mast.instruments.camera.TsiBackend`) sets ``argtypes`` /
``restype`` on every exported function and calls it with ctypes objects. Here every
export is a plain Python function (functions accept attribute assignment), so those
calls arrive unconverted: ``ctypes.byref(x)`` shows up as a CArgObject whose ``_obj`` is
``x``, string buffers arrive as ``c_char`` arrays, ints as ints.

:class:`FakeUsbCamera` is the physical camera and is shared by every "process": two
:class:`FakeTsiDll` instances on one device model MAST and ThorImageCAM. Only one handle
can be open on it at a time, which is the exclusivity the contract is about.
"""
from __future__ import annotations

import ctypes
import time

import numpy as np

_ERR_OK = 0
_ERR_FAIL = 1004


class FakeUsbCamera:
    """The device: who holds it, and what the sensor looks like."""

    def __init__(self, *, serial: str = "18342", width: int = 64, height: int = 48,
                 bit_depth: int = 10, sensor_type: int = 1) -> None:
        self.serial = serial
        self.width, self.height = width, height
        self.bit_depth = bit_depth
        self.sensor_type = sensor_type
        self.owner: object | None = None


class FakeTsiDll:
    def __init__(self, device: FakeUsbCamera | None = None, *, roi_inclusive: bool = True,
                 missing: tuple[str, ...] = (), exposure_range: tuple[int, int] = (40, 26_843_000),
                 gain_index_range: tuple[int, int] = (0, 480), fail: dict[str, int] | None = None,
                 discover_hides_when_owned: bool = False) -> None:
        self.dev = device or FakeUsbCamera()
        self.roi_inclusive = roi_inclusive
        self.exposure_range = exposure_range
        self.gain_index_range = gain_index_range
        self.fail = dict(fail or {})
        self.discover_hides_when_owned = discover_hides_when_owned
        self.calls: list[str] = []
        self.sdk_open = 0
        self.handle = 0
        self.armed = False
        self.triggered = False
        self.operation_mode = 0
        self.frames_per_trigger = 1
        self.poll_timeout_ms = 0
        self.exposure_us = 10_000
        self.gain_index = 0
        self.binx = self.biny = 1
        k = 1 if roi_inclusive else 0
        self.roi = [0, 0, self.dev.width - k, self.dev.height - k]
        self.pending: list[np.ndarray] = []
        self.frame_buffer: np.ndarray | None = None
        self.last_error = b""
        names = [n for n in dir(self) if n.startswith("_impl_")]
        for impl_name in names:
            export = impl_name[len("_impl_"):]
            if export in missing:
                continue
            setattr(self, export, self._export(export, getattr(self, impl_name)))

    # —— plumbing ——
    def _export(self, name, impl):
        def fn(*args):
            self.calls.append(name)
            rc = self.fail.get(name)
            if rc:
                self.last_error = f"{name}: injected failure".encode()
                return rc
            return impl(*args)
        return fn

    def _err(self, text: str) -> int:
        self.last_error = text.encode()
        return _ERR_FAIL

    @staticmethod
    def _set(ref, value) -> None:
        ref._obj.value = value

    def _check(self, h) -> bool:
        return bool(self.handle) and getattr(h, "value", h) == self.handle

    def _image_wh(self) -> tuple[int, int]:
        k = 1 if self.roi_inclusive else 0
        w = (self.roi[2] - self.roi[0] + k) // self.binx
        h = (self.roi[3] - self.roi[1] + k) // self.biny
        return w, h

    def queue_frame(self, value: int | np.ndarray | None = None) -> np.ndarray:
        """Make one frame pending (a constant, a given array, or a ramp by default)."""
        w, h = self._image_wh()
        if value is None:
            img = (np.arange(w * h, dtype=np.uint32) % (1 << self.dev.bit_depth)).astype(np.uint16)
            img = img.reshape(h, w)
        elif isinstance(value, np.ndarray):
            img = np.ascontiguousarray(value, dtype=np.uint16)
        else:
            img = np.full((h, w), int(value), dtype=np.uint16)
        self.pending.append(img)
        return img

    # —— exports ——
    def _impl_tl_camera_open_sdk(self):
        if self.sdk_open:
            return self._err("SDK already open")
        self.sdk_open = 1
        return _ERR_OK

    def _impl_tl_camera_close_sdk(self):
        self.sdk_open = 0
        return _ERR_OK

    def _impl_tl_camera_get_last_error(self):
        return self.last_error

    def _impl_tl_camera_discover_available_cameras(self, buf, size):
        if not self.sdk_open:
            return self._err("SDK not open")
        visible = not (self.discover_hides_when_owned and self.dev.owner not in (None, self))
        buf.value = self.dev.serial.encode() if visible else b""
        return _ERR_OK

    def _impl_tl_camera_open_camera(self, serial, ref):
        if serial.decode() != self.dev.serial:
            return self._err("no such camera")
        if self.dev.owner is not None and self.dev.owner is not self:
            return self._err("camera is in use by another process")
        self.dev.owner = self
        self.handle = 0x5EED
        self._set(ref, self.handle)
        return _ERR_OK

    def _impl_tl_camera_close_camera(self, h):
        if not self._check(h):
            return self._err("bad handle")
        self.handle = 0
        self.armed = False
        self.dev.owner = None
        return _ERR_OK

    def _str(self, buf, text):
        buf.value = text.encode()
        return _ERR_OK

    def _impl_tl_camera_get_model(self, h, buf, size):
        return self._str(buf, "CS165CU")

    def _impl_tl_camera_get_name(self, h, buf, size):
        return self._str(buf, "")

    def _impl_tl_camera_get_firmware_version(self, h, buf, size):
        return self._str(buf, "fake 1.0")

    def _impl_tl_camera_get_camera_sensor_type(self, h, ref):
        self._set(ref, self.dev.sensor_type)
        return _ERR_OK

    def _impl_tl_camera_get_sensor_width(self, h, ref):
        self._set(ref, self.dev.width)
        return _ERR_OK

    def _impl_tl_camera_get_sensor_height(self, h, ref):
        self._set(ref, self.dev.height)
        return _ERR_OK

    def _impl_tl_camera_get_bit_depth(self, h, ref):
        self._set(ref, self.dev.bit_depth)
        return _ERR_OK

    def _impl_tl_camera_get_exposure_time_range(self, h, lo, hi):
        self._set(lo, self.exposure_range[0])
        self._set(hi, self.exposure_range[1])
        return _ERR_OK

    def _impl_tl_camera_set_exposure_time(self, h, us):
        self.exposure_us = int(us)
        return _ERR_OK

    def _impl_tl_camera_get_exposure_time(self, h, ref):
        self._set(ref, self.exposure_us)
        return _ERR_OK

    def _impl_tl_camera_get_gain_range(self, h, lo, hi):
        self._set(lo, self.gain_index_range[0])
        self._set(hi, self.gain_index_range[1])
        return _ERR_OK

    def _impl_tl_camera_set_gain(self, h, idx):
        self.gain_index = int(idx)
        return _ERR_OK

    def _impl_tl_camera_get_gain(self, h, ref):
        self._set(ref, self.gain_index)
        return _ERR_OK

    # one gain index = 0.1 dB (the real conversion is the camera's; the backend only round-trips it)
    def _impl_tl_camera_convert_gain_to_decibels(self, h, idx, ref):
        self._set(ref, int(idx) / 10.0)
        return _ERR_OK

    def _impl_tl_camera_convert_decibels_to_gain(self, h, db, ref):
        self._set(ref, int(round(float(db) * 10)))
        return _ERR_OK

    def _impl_tl_camera_set_roi(self, h, ulx, uly, lrx, lry):
        if self.armed:
            return self._err("cannot change ROI while armed")
        self.roi = [int(ulx), int(uly), int(lrx), int(lry)]
        return _ERR_OK

    def _impl_tl_camera_get_roi(self, h, a, b, c, d):
        for ref, v in zip((a, b, c, d), self.roi):
            self._set(ref, v)
        return _ERR_OK

    def _impl_tl_camera_set_binx(self, h, v):
        self.binx = int(v)
        return _ERR_OK

    def _impl_tl_camera_set_biny(self, h, v):
        self.biny = int(v)
        return _ERR_OK

    def _impl_tl_camera_get_binx(self, h, ref):
        self._set(ref, self.binx)
        return _ERR_OK

    def _impl_tl_camera_get_biny(self, h, ref):
        self._set(ref, self.biny)
        return _ERR_OK

    def _impl_tl_camera_get_binx_range(self, h, lo, hi):
        self._set(lo, 1)
        self._set(hi, 4)
        return _ERR_OK

    def _impl_tl_camera_set_operation_mode(self, h, mode):
        self.operation_mode = int(mode)
        return _ERR_OK

    def _impl_tl_camera_get_operation_mode(self, h, ref):
        self._set(ref, self.operation_mode)
        return _ERR_OK

    def _impl_tl_camera_set_frames_per_trigger_zero_for_unlimited(self, h, n):
        self.frames_per_trigger = int(n)
        return _ERR_OK

    def _impl_tl_camera_set_image_poll_timeout(self, h, ms):
        self.poll_timeout_ms = int(ms)
        return _ERR_OK

    def _impl_tl_camera_arm(self, h, frames):
        if not self._check(h):
            return self._err("bad handle")
        self.armed = True
        return _ERR_OK

    def _impl_tl_camera_issue_software_trigger(self, h):
        if not self.armed:
            return self._err("not armed")
        self.triggered = True
        return _ERR_OK

    def _impl_tl_camera_disarm(self, h):
        self.armed = False
        self.triggered = False
        return _ERR_OK

    def _impl_tl_camera_get_image_width(self, h, ref):
        self._set(ref, self._image_wh()[0])
        return _ERR_OK

    def _impl_tl_camera_get_image_height(self, h, ref):
        self._set(ref, self._image_wh()[1])
        return _ERR_OK

    def _impl_tl_camera_get_pending_frame_or_null(self, h, buf_ref, count_ref, meta_ref, meta_n_ref):
        if not (self.armed and self.triggered) or not self.pending:
            # The real call blocks up to the image poll timeout before answering NULL.
            time.sleep(min(self.poll_timeout_ms, 200) / 1000.0)
            ctypes.memset(ctypes.addressof(buf_ref._obj), 0, ctypes.sizeof(ctypes.c_void_p))  # NULL
            return _ERR_OK
        # The SDK hands out a buffer it reuses on the next call: keep it, and scribble over
        # the previous one so a backend that did not copy would see its frame change.
        if self.frame_buffer is not None:
            self.frame_buffer[...] = 0xBEEF & ((1 << 16) - 1)
        self.frame_buffer = self.pending.pop(0).copy()
        addr = self.frame_buffer.ctypes.data
        buf_ref._obj.contents = ctypes.c_ushort.from_address(addr)
        self._set(count_ref, 1)
        self._set(meta_n_ref, 0)
        return _ERR_OK


def loader_for(dll: FakeTsiDll):
    """A ``TsiBackend(loader=...)`` that hands out *dll* (and counts the loads)."""
    loads: list[str] = []

    def load(dll_dir: str):
        loads.append(dll_dir)
        return dll, None

    load.loads = loads  # type: ignore[attr-defined]
    return load
