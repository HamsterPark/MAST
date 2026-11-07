"""对时估计、STM 状态环形缓冲、SPE 3.0 文件解析。"""

from __future__ import annotations

import struct
import time

import numpy as np
import pytest

from mast.satellite.clock import ClockEstimator
from mast.satellite.spe import (
    HEADER_BYTES,
    OFF_XML,
    build_footer_xml,
    iso_to_ms,
    parse_footer,
    parse_spe,
    write_spe,
)
from mast.satellite.stm_ring import StmStateRing


# ── 对时 ────────────────────────────────────────────────────────────────────

def _sample(est, theta, out_ms, back_ms, t0, proc=1):
    """卫星钟快 theta ms；去程 out_ms、回程 back_ms、卫星处理 proc ms。"""
    t1 = t0 + out_ms + theta
    t2 = t1 + proc
    t3 = t2 - theta + back_ms
    return est.add(t0, t1, t2, t3)


def test_offset_comes_from_min_delay_sample():
    est = ClockEstimator(window=32)
    # 不对称的大时延样本把 θ 带偏；最小时延的那个是对的。
    _sample(est, 120.0, 40, 2, 1000)       # θ 估计 = 120 + 19
    _sample(est, 120.0, 2, 30, 2000)       # θ 估计 = 120 - 14
    s = _sample(est, 120.0, 1, 1, 3000)    # θ 估计 = 120
    assert s.delta_ms == 2
    assert est.offset_ms == pytest.approx(120.0)
    assert est.delay_ms == 2
    assert est.to_hub_ms(10_000) == pytest.approx(10_000 - 120.0)


def test_window_forgets_old_samples_and_rejects_negative_delta():
    est = ClockEstimator(window=3)
    _sample(est, 0.0, 0, 0, 0)                         # δ=0：最好
    for i in range(3):
        _sample(est, 50.0, 5, 5, 1000 + i)
    assert est.offset_ms == pytest.approx(50.0)        # 那个最好的被挤出窗口了
    assert est.add(0, 100, 50, 10) is None             # t2 < t1：不合理
    assert est.rejected == 1


# ── STM 环形缓冲 ────────────────────────────────────────────────────────────

def test_ring_window_before_after_during_and_prune():
    now = [1_000_000.0]
    ring = StmStateRing(max_age_s=3600, now_fn=lambda: now[0])
    for i in range(10):
        ring.add(now[0] - 9 + i, {"bias_v": 0.5, "current_a": 1e-10 * (1 + i),
                                  "z_m": 2e-9, "z_controller_on": True, "withdrawn": False})
    t0 = (now[0] - 6) * 1000 + 500                     # 两条样本之间
    t1 = (now[0] - 2) * 1000 + 500
    w = ring.window(t0, t1)
    assert w["matched"] is True
    assert w["before"]["ts_ms"] == int((now[0] - 6) * 1000)
    assert w["after"]["ts_ms"] == int((now[0] - 1) * 1000)
    assert w["during"]["n"] == 4
    assert w["during"]["current_a"]["mean"] == pytest.approx(1e-10 * 6.5)   # i = 4..7
    assert w["during"]["z_controller_on"] == {"all": True, "any": True, "n": 4}
    # 窗口在环形缓冲之前很久：不匹配，不编
    w2 = ring.window((now[0] - 7200) * 1000, (now[0] - 7100) * 1000)
    assert w2["matched"] is False and w2["reason"] == "older_than_ring"
    # 时间不明
    assert ring.window(None, None)["matched"] is False
    # 一小时之后旧样本被淘汰
    now[0] += 4000
    ring.add(now[0], {"bias_v": 1.0})
    assert len(ring) == 1


def test_ring_subscribes_to_hardware_state():
    from mast.core.events import Event, EventBus, EventType

    bus = EventBus()
    ring = StmStateRing()
    assert ring.start(bus)
    bus.publish(Event(type=EventType.HARDWARE_STATE, data={"bias_v": 0.3}))
    bus.publish(Event(type=EventType.SKILL_STEP, data={"skill": "x"}))
    assert len(ring) == 1
    ring.stop()
    bus.publish(Event(type=EventType.HARDWARE_STATE, data={"bias_v": 0.4}))
    assert len(ring) == 1


# ── SPE 3.0 ─────────────────────────────────────────────────────────────────

def _footer(**kw):
    wl = np.linspace(635.43, 692.57, 1340)
    args = dict(width=1340, height=1, frames=2, wavelengths_nm=wl, laser_line_nm=633.0,
                exposure_ms=1000.0, center_nm=664.5495, grating="[500nm,1200][0][0]",
                created_iso="2026-10-16T11:08:03.0755947+08:00",
                origin_created_iso="2026-10-16T11:08:02.0142812+08:00",
                camera_model="PyLoN: 100B eXcelon", spectrometer_model="HRS-300-MS")
    args.update(kw)
    return build_footer_xml(**args)


def test_write_then_parse_spe(tmp_path):
    frames = np.full((2, 1340), 600, dtype=np.uint16)
    p = tmp_path / "Raman_ 13.spe"
    size = write_spe(p, frames, _footer())
    assert size == HEADER_BYTES + 2 * 1340 * 2 + len(_footer().encode())
    raw = p.read_bytes()
    assert struct.unpack_from("<Q", raw, OFF_XML)[0] == HEADER_BYTES + 2 * 1340 * 2
    out = parse_spe(p)
    assert out["ok"] is True, out["error"]
    h = out["header"]
    assert (h["xdim"], h["ydim"], h["dtype"], h["num_frames"]) == (1340, 1, "u16", 2)
    assert h["file_header_ver"] == 3.0 and h["lastvalue_ok"]
    k = out["footer"]["key"]
    assert k["laser_line_nm"] == 633.0
    assert k["exposure_ms"] == 1000.0
    assert k["frames"] == 2 and k["width"] == 1340
    assert k["grating"] == "[500nm,1200][0][0]"
    assert k["center_wavelength_nm"] == pytest.approx(664.5495)
    assert k["sensor_temperature_locked"] is True
    assert k["camera_model"] == "PyLoN: 100B eXcelon"
    assert k["wavelength_first_nm"] == pytest.approx(635.43)
    assert k["acq_origin_created_ms"] == iso_to_ms("2026-10-16T11:08:02.0142812+08:00")
    assert 1055 <= k["file_created_ms"] - k["acq_origin_created_ms"] <= 1070


def test_footer_structure_matches_lightfield_layout():
    """结构照 LightField 真实文件的尾部（命名空间、DataHistories/Origin/Experiment）。
    这里用的是按那份结构手写的片段，不把真实文件（含机器名与路径）放进仓库。"""
    xml = (
        '<SpeFormat version="3.0" xmlns="http://www.princetoninstruments.com/spe/2009">'
        '<DataFormat><DataBlock type="Frame" count="1337" pixelFormat="MonochromeUnsigned16" '
        'size="2680" stride="2680" calibrations="1"><DataBlock type="Region" calibrations="2,3" '
        'count="1" width="1340" height="1" size="2680" stride="2680" /></DataBlock></DataFormat>'
        '<Calibrations><WavelengthMapping id="1" orientation="Normal"><Wavelength '
        'xml:space="preserve">366.85,367.27,929.48</Wavelength></WavelengthMapping></Calibrations>'
        '<DataHistories><DataHistory id="1"><Origin created="2026-02-11T10:57:13.37+08:00" '
        'software="LightField" softwareVersion="6.18.9.2503"><Experiment '
        'xmlns="http://www.princetoninstruments.com/experiment/2009" version="12"><Devices><Cameras '
        'count="1"><Camera deviceID="2"><ShutterTiming><ExposureTime type="Double">1'
        '</ExposureTime></ShutterTiming><HardwareIO><Trigger><Response type="TriggerResponse">'
        'ReadoutPerTrigger</Response></Trigger></HardwareIO></Camera></Cameras></Devices>'
        '</Experiment></Origin></DataHistory></DataHistories></SpeFormat>')
    f = parse_footer(xml)
    k = f["key"]
    assert k["frames"] == 1337 and k["exposure_ms"] == 1.0
    assert k["trigger_response"] == "ReadoutPerTrigger"
    assert k["laser_line_nm"] is None                    # 泵浦探测文件没有激发线：不猜
    assert k["wavelength_last_nm"] == pytest.approx(929.48)
    assert k["software_version"] == "6.18.9.2503"


def test_parse_spe_never_raises_on_garbage(tmp_path):
    p = tmp_path / "bad.spe"
    p.write_bytes(b"\x00" * 100)
    assert parse_spe(p)["ok"] is False
    p.write_bytes(b"\x00" * HEADER_BYTES)
    out = parse_spe(p)
    assert out["ok"] is False and "no_xml_footer" in out["error"]
    hdr = bytearray(HEADER_BYTES + 10)
    struct.pack_into("<Q", hdr, OFF_XML, 10_000_000)
    p.write_bytes(bytes(hdr))
    assert "不在文件范围内" in parse_spe(p)["error"]
    hdr = bytearray(HEADER_BYTES) + b"<not-xml"
    struct.pack_into("<Q", hdr, OFF_XML, HEADER_BYTES)
    p.write_bytes(bytes(hdr))
    out = parse_spe(p)
    assert out["ok"] is False and out["header"] is not None


def test_iso_to_ms_refuses_naive_times():
    assert iso_to_ms("2026-10-16T11:08:03") is None
    assert iso_to_ms("garbage") is None
    assert iso_to_ms("2026-10-16T03:08:03+00:00") == iso_to_ms("2026-10-16T11:08:03+08:00")


def test_sim_spectrum_has_si_peak_where_expected():
    from mast.satellite.sim import spectra

    wl = spectra.wavelength_axis()
    assert wl.size == 1340 and 630 < wl[0] < 640 and 690 < wl[-1] < 700
    shift = spectra.raman_shift_cm1(wl)
    frame = spectra.synth_frame(wl, exposure_ms=1000, rng=np.random.default_rng(0))
    assert frame.dtype == np.dtype("<u2")
    peak = shift[int(np.argmax(frame[200:]) + 200)]       # 跳过低位移端的瑞利残余
    assert abs(peak - 520.7) < 5
    assert time.time() > 0
