"""Princeton Instruments SPE 3.0 文件：读文件头与 XML 文件尾里的关键设置（契约 §11.2）。

只按厂家公开的 SPE 3.0 文件格式规范读，不依赖 LightField。布局：

- 文件头固定 4100 字节，小端。兼容 2.x 的字段里我们用到：
  ``xdim``@42 (u16)、``datatype``@108 (i16)、``ydim``@656 (u16)、
  **``XML 文件尾偏移``@678 (u64)**、``NumFrames``@1446 (i32)、``file_header_ver``@1992 (f32)。
- 数据从 4100 开始；XML 文件尾从偏移 678 记录的位置一直到文件末尾。
- 文件尾根元素 ``<SpeFormat version="3.0">``，命名空间
  ``http://www.princetoninstruments.com/spe/2009``；采集设置在
  ``DataHistories/DataHistory/Origin/Experiment`` 下（另一个命名空间）。下面一律用 ``{*}``
  通配命名空间。

结构按 LightField 6.18.9 实际写出的文件尾核对过。
这里**只读、永不抛**：坏文件返回 ``{"ok": False, "error": ...}``，元数据照样能落盘。
"""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

HEADER_BYTES = 4100
OFF_XDIM = 42
OFF_DATATYPE = 108
OFF_YDIM = 656
OFF_XML = 678
OFF_NUMFRAMES = 1446
OFF_HDRVER = 1992
OFF_LASTVALUE = 4098
LASTVALUE_MAGIC = 0x5555

DATATYPES = {0: "f32", 1: "i32", 2: "i16", 3: "u16", 5: "f64", 6: "u8", 8: "u32"}
MAX_FOOTER_BYTES = 16 << 20

SPE_NS = "http://www.princetoninstruments.com/spe/2009"
EXP_NS = "http://www.princetoninstruments.com/experiment/2009"


def _f(text: str | None) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _i(text: str | None) -> int | None:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _b(text: str | None) -> bool | None:
    s = str(text or "").strip().lower()
    if s in ("true", "1"):
        return True
    if s in ("false", "0"):
        return False
    return None


def iso_to_ms(text: str | None) -> int | None:
    """``2026-10-16T11:08:03.0755947+08:00`` → unix ms。不带时区的时间不猜，返回 None。"""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return int(round(dt.timestamp() * 1000))


def read_header(buf: bytes) -> dict:
    if len(buf) < HEADER_BYTES:
        raise ValueError(f"文件头不足 {HEADER_BYTES} 字节（只有 {len(buf)}）")
    xdim = struct.unpack_from("<H", buf, OFF_XDIM)[0]
    dtype = struct.unpack_from("<h", buf, OFF_DATATYPE)[0]
    ydim = struct.unpack_from("<H", buf, OFF_YDIM)[0]
    xml_off = struct.unpack_from("<Q", buf, OFF_XML)[0]
    frames = struct.unpack_from("<i", buf, OFF_NUMFRAMES)[0]
    hdr_ver = struct.unpack_from("<f", buf, OFF_HDRVER)[0]
    last = struct.unpack_from("<H", buf, OFF_LASTVALUE)[0]
    return {
        "xdim": int(xdim), "ydim": int(ydim), "datatype": int(dtype),
        "dtype": DATATYPES.get(int(dtype)), "num_frames": int(frames),
        "file_header_ver": round(float(hdr_ver), 3), "xml_offset": int(xml_off),
        "lastvalue_ok": int(last) == LASTVALUE_MAGIC,
    }


def _first(root, path: str):
    return root.find(path)


def _text(root, path: str) -> str | None:
    el = root.find(path)
    if el is None or el.text is None:
        return None
    return el.text.strip()


def parse_footer(xml_text: str) -> dict:
    """从文件尾 XML 抽出关键设置。缺的项就是 None，不猜。"""
    root = ET.fromstring(xml_text)
    out: dict[str, Any] = {"spe_version": root.attrib.get("version")}

    frame = root.find("{*}DataFormat/{*}DataBlock[@type='Frame']")
    if frame is not None:
        out["frame"] = {
            "count": _i(frame.attrib.get("count")),
            "pixel_format": frame.attrib.get("pixelFormat"),
            "size": _i(frame.attrib.get("size")),
            "stride": _i(frame.attrib.get("stride")),
        }
        out["regions"] = [
            {"width": _i(r.attrib.get("width")), "height": _i(r.attrib.get("height")),
             "size": _i(r.attrib.get("size")), "stride": _i(r.attrib.get("stride")),
             "count": _i(r.attrib.get("count"))}
            for r in frame.findall("{*}DataBlock[@type='Region']")
        ]

    wm = root.find("{*}Calibrations/{*}WavelengthMapping")
    if wm is not None:
        info: dict[str, Any] = {
            "laser_line_nm": _f(wm.attrib.get("laserLine")),
            "date": wm.attrib.get("date"),
            "orientation": wm.attrib.get("orientation"),
        }
        wl = wm.find("{*}Wavelength")
        if wl is None:
            wl = wm.find("{*}WavelengthError")
        if wl is not None and wl.text:
            vals = [v for v in (_f(x) for x in wl.text.split(",")) if v is not None]
            info.update({"n": len(vals), "first_nm": vals[0] if vals else None,
                         "last_nm": vals[-1] if vals else None})
        out["wavelength_mapping"] = info
    sm = root.find("{*}Calibrations/{*}SensorMapping")
    if sm is not None:
        out["sensor_mapping"] = {k: _i(v) if k != "id" else v for k, v in sm.attrib.items()}

    fi = root.find("{*}GeneralInformation/{*}FileInformation")
    if fi is not None:
        out["file_information"] = dict(fi.attrib)

    origin = root.find("{*}DataHistories/{*}DataHistory/{*}Origin")
    if origin is not None:
        out["origin"] = {k: origin.attrib.get(k) for k in
                         ("created", "creator", "software", "softwareVersion")}
        exp = origin.find("{*}Experiment")
        if exp is not None:
            out["experiment"] = _experiment(exp)
    out["key"] = _key_fields(out)
    return out


def _experiment(exp) -> dict:
    sysd: dict[str, Any] = {}
    sp = exp.find("{*}System/{*}Spectrometers/{*}Spectrometer")
    if sp is not None:
        sysd["spectrometer"] = {"model": sp.attrib.get("model"),
                                "serial": sp.attrib.get("serialNumber")}
        sysd["gratings"] = [g.text for g in sp.findall(".//{*}Gratings/{*}Grating") if g.text]
    cam = exp.find("{*}System/{*}Cameras/{*}Camera")
    if cam is not None:
        sysd["camera"] = {"model": cam.attrib.get("model"),
                          "serial": cam.attrib.get("serialNumber"),
                          "interface": cam.attrib.get("computerInterface")}
    c = exp.find("{*}Devices/{*}Cameras/{*}Camera")
    s = exp.find("{*}Devices/{*}Spectrometers/{*}Spectrometer")
    camera: dict[str, Any] = {}
    if c is not None:
        roi = c.find("{*}ReadoutControl/{*}RegionsOfInterest/{*}Result/{*}RegionOfInterest")
        camera = {
            "temperature_setpoint_c": _f(_text(c, "{*}Sensor/{*}Temperature/{*}SetPoint")),
            "temperature_c": _f(_text(c, "{*}Sensor/{*}Temperature/{*}Reading")),
            "temperature_status": _text(c, "{*}Sensor/{*}Temperature/{*}Status"),
            "exposure_ms": _f(_text(c, "{*}ShutterTiming/{*}ExposureTime")),
            "shutter_mode": _text(c, "{*}ShutterTiming/{*}Mode"),
            "readout_mode": _text(c, "{*}ReadoutControl/{*}Mode"),
            "readout_time_ms": _f(_text(c, "{*}ReadoutControl/{*}Time")),
            "roi_selection": _text(c, "{*}ReadoutControl/{*}RegionsOfInterest/{*}Selection"),
            "roi": ({k: _i(v) for k, v in roi.attrib.items()} if roi is not None else None),
            "output_signal": _text(c, "{*}HardwareIO/{*}OutputSignal"),
            "trigger_response": _text(c, "{*}HardwareIO/{*}Trigger/{*}Response"),
            "trigger_determination": _text(c, "{*}HardwareIO/{*}Trigger/{*}Determination"),
            "adc_speed_mhz": _f(_text(c, "{*}Adc/{*}Speed")),
            "adc_gain": _text(c, "{*}Adc/{*}AnalogGain"),
            "adc_quality": _text(c, "{*}Adc/{*}Quality"),
            "frame_tracking": _b(_text(c, "{*}Acquisition/{*}FrameTracking/{*}Enabled")),
            "time_stamps": _text(c, "{*}Acquisition/{*}TimeStamping/{*}Stamps"),
            "frames_to_store": _i(_text(c, "{*}Experiment/{*}Acquisition/{*}FramesToStore")),
            "frames_combined": _i(_text(
                c, "{*}Experiment/{*}OnlineProcessing/{*}FrameCombination/{*}FramesCombined")),
            "file_directory": _text(c, "{*}Experiment/{*}FileNameGeneration/{*}Directory"),
            "file_base_name": _text(c, "{*}Experiment/{*}FileNameGeneration/{*}BaseFileName"),
        }
    spectro: dict[str, Any] = {}
    if s is not None:
        spectro = {
            "grating": _text(s, "{*}Grating/{*}Selected"),
            "center_wavelength_nm": _f(_text(s, "{*}Grating/{*}CenterWavelength")),
            "calibration_mode": _text(s, "{*}Experiment/{*}WavelengthCalibration/{*}Mode"),
        }
    return {"system": sysd, "camera": camera, "spectrometer": spectro}


def _key_fields(out: dict) -> dict:
    """``.meta.json`` 顶层要的那几项，扁平化。"""
    exp = out.get("experiment") or {}
    cam = exp.get("camera") or {}
    spc = exp.get("spectrometer") or {}
    sysd = exp.get("system") or {}
    wm = out.get("wavelength_mapping") or {}
    regions = out.get("regions") or [{}]
    fi = out.get("file_information") or {}
    org = out.get("origin") or {}
    return {
        "exposure_ms": cam.get("exposure_ms"),
        "frames": (out.get("frame") or {}).get("count"),
        "frames_to_store": cam.get("frames_to_store"),
        "pixel_format": (out.get("frame") or {}).get("pixel_format"),
        "width": regions[0].get("width"),
        "height": regions[0].get("height"),
        "roi": cam.get("roi"),
        "adc_speed_mhz": cam.get("adc_speed_mhz"),
        "adc_quality": cam.get("adc_quality"),
        "adc_gain": cam.get("adc_gain"),
        "readout_mode": cam.get("readout_mode"),
        "trigger_response": cam.get("trigger_response"),
        "trigger_determination": cam.get("trigger_determination"),
        "output_signal": cam.get("output_signal"),
        "sensor_temperature_c": cam.get("temperature_c"),
        "sensor_temperature_setpoint_c": cam.get("temperature_setpoint_c"),
        "sensor_temperature_locked": (str(cam.get("temperature_status") or "").lower()
                                      == "locked") if cam.get("temperature_status") else None,
        "grating": spc.get("grating"),
        "center_wavelength_nm": spc.get("center_wavelength_nm"),
        "laser_line_nm": wm.get("laser_line_nm"),
        "wavelength_first_nm": wm.get("first_nm"),
        "wavelength_last_nm": wm.get("last_nm"),
        "camera_model": (sysd.get("camera") or {}).get("model"),
        "camera_serial": (sysd.get("camera") or {}).get("serial"),
        "spectrometer_model": (sysd.get("spectrometer") or {}).get("model"),
        "spectrometer_serial": (sysd.get("spectrometer") or {}).get("serial"),
        "acq_origin_created": org.get("created"),
        "acq_origin_created_ms": iso_to_ms(org.get("created")),
        "file_created": fi.get("created"),
        "file_created_ms": iso_to_ms(fi.get("created")),
        "software_version": org.get("softwareVersion"),
    }


def parse_spe(path: str | Path, *, max_footer_bytes: int = MAX_FOOTER_BYTES) -> dict:
    """读一个 .spe 文件的头和文件尾。永不抛。"""
    p = Path(path)
    out: dict[str, Any] = {"ok": False, "error": None, "header": None, "footer": None}
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            hdr = read_header(f.read(HEADER_BYTES))
            out["header"] = hdr
            off = hdr["xml_offset"]
            if off <= 0:
                out["error"] = "no_xml_footer（file_header_ver < 3 的旧格式？）"
                return out
            if off < HEADER_BYTES or off >= size:
                out["error"] = f"xml_offset={off} 不在文件范围内（大小 {size}）"
                return out
            if size - off > max_footer_bytes:
                out["error"] = f"文件尾 {size - off} 字节超过上限 {max_footer_bytes}"
                return out
            f.seek(off)
            xml_bytes = f.read(size - off)
        out["footer"] = parse_footer(xml_bytes.decode("utf-8", errors="replace").strip("\x00 \r\n\t"))
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001 — 坏文件不该挡住落盘
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# ── 写（参考卫星与测试用；不是 LightField 的完整复刻） ──────────────────────

def build_footer_xml(*, width: int, height: int, frames: int, wavelengths_nm,
                     laser_line_nm: float, exposure_ms: float, center_nm: float,
                     grating: str, created_iso: str, origin_created_iso: str,
                     adc_speed_mhz: float = 1.0, adc_quality: str = "LowNoise",
                     temperature_c: float = -120.0, trigger_response: str = "NoResponse",
                     camera_model: str = "SIM CCD", camera_serial: str = "SIM0001",
                     spectrometer_model: str = "SIM-300", spectrometer_serial: str = "SIM0002",
                     software_version: str = "sim") -> str:
    """生成结构与 LightField 一致的最小 SPE 3.0 文件尾（只含本模块会读的元素）。"""
    from xml.sax.saxutils import quoteattr

    wl = ",".join(repr(float(x)) for x in wavelengths_nm)
    size = width * height * 2
    return (
        f'<SpeFormat version="3.0" xmlns="{SPE_NS}">'
        f'<DataFormat><DataBlock type="Frame" count="{frames}" pixelFormat="MonochromeUnsigned16"'
        f' size="{size}" stride="{size}" calibrations="1">'
        f'<DataBlock type="Region" calibrations="2,3" count="1" width="{width}" height="{height}"'
        f' size="{size}" stride="{size}" /></DataBlock></DataFormat>'
        f'<Calibrations><WavelengthMapping id="1" orientation="Normal" laserLine="{laser_line_nm:g}">'
        f'<Wavelength xml:space="preserve">{wl}</Wavelength></WavelengthMapping>'
        f'<SensorInformation id="2" orientation="Normal" width="{width}" height="{height}" />'
        f'<SensorMapping id="3" x="0" y="0" height="{height}" width="{width}" xBinning="1"'
        f' yBinning="1" /></Calibrations>'
        f'<DataHistories><DataHistory id="1"><Origin creator="sim" created={quoteattr(origin_created_iso)}'
        f' software="sim" softwareVersion={quoteattr(software_version)} softwareCompany="MAST">'
        f'<Experiment xmlns="{EXP_NS}" version="12"><System><Spectrometers count="1">'
        f'<Spectrometer deviceID="1" model={quoteattr(spectrometer_model)}'
        f' serialNumber={quoteattr(spectrometer_serial)} /></Spectrometers>'
        f'<Cameras count="1"><Camera deviceID="2" model={quoteattr(camera_model)}'
        f' serialNumber={quoteattr(camera_serial)} computerInterface="Sim" /></Cameras></System>'
        f'<Devices><Cameras count="1"><Camera deviceID="2"><Sensor><Temperature>'
        f'<SetPoint type="Double">{temperature_c:g}</SetPoint>'
        f'<Reading type="Double">{temperature_c:g}</Reading><Status>Locked</Status>'
        f'</Temperature></Sensor><ShutterTiming><ExposureTime type="Double">{exposure_ms:g}'
        f'</ExposureTime><Mode>Normal</Mode></ShutterTiming>'
        f'<ReadoutControl><Mode>FullFrame</Mode><RegionsOfInterest><Selection>FullSensor</Selection>'
        f'<Result count="1"><RegionOfInterest id="0" x="0" width="{width}" xBinning="1" y="0"'
        f' height="{height}" yBinning="1" /></Result></RegionsOfInterest></ReadoutControl>'
        f'<HardwareIO><OutputSignal>ShutterOpen</OutputSignal><Trigger><Response>'
        f'{trigger_response}</Response><Determination>RisingEdge</Determination></Trigger>'
        f'</HardwareIO><Adc><Speed type="Double">{adc_speed_mhz:g}</Speed><AnalogGain>High'
        f'</AnalogGain><Quality>{adc_quality}</Quality></Adc>'
        f'<Acquisition><FrameTracking><Enabled>False</Enabled></FrameTracking></Acquisition>'
        f'<Experiment><Acquisition><FramesToStore type="Int64">{frames}</FramesToStore>'
        f'</Acquisition></Experiment></Camera></Cameras>'
        f'<Spectrometers count="1"><Spectrometer deviceID="1"><Grating><Selected>'
        f'{grating}</Selected><CenterWavelength type="Double">{center_nm!r}</CenterWavelength>'
        f'</Grating></Spectrometer></Spectrometers></Devices></Experiment></Origin>'
        f'</DataHistory></DataHistories>'
        f'<GeneralInformation><FileInformation creator="sim" created={quoteattr(created_iso)}'
        f' lastModified={quoteattr(created_iso)} /></GeneralInformation></SpeFormat>'
    )


def write_spe(path: str | Path, frames_u16, footer_xml: str) -> int:
    """写一个 SPE 3.0 文件（u16 数据）。``frames_u16``：形状 (frames, height, width) 或
    (frames, width)。返回文件大小。原子写（``.part`` + replace）。"""
    import os

    import numpy as np

    arr = np.asarray(frames_u16, dtype="<u2")
    if arr.ndim == 2:
        arr = arr[:, None, :]
    if arr.ndim != 3:
        raise ValueError("frames 形状必须是 (frames, width) 或 (frames, height, width)")
    nf, h, w = arr.shape
    data = arr.tobytes()
    hdr = bytearray(HEADER_BYTES)
    struct.pack_into("<H", hdr, OFF_XDIM, w)
    struct.pack_into("<h", hdr, OFF_DATATYPE, 3)
    struct.pack_into("<H", hdr, OFF_YDIM, h)
    struct.pack_into("<Q", hdr, OFF_XML, HEADER_BYTES + len(data))
    struct.pack_into("<i", hdr, OFF_NUMFRAMES, nf)
    struct.pack_into("<f", hdr, OFF_HDRVER, 3.0)
    struct.pack_into("<H", hdr, OFF_LASTVALUE, LASTVALUE_MAGIC)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".part")
    with open(tmp, "wb") as f:
        f.write(bytes(hdr))
        f.write(data)
        f.write(footer_xml.encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return p.stat().st_size


__all__ = ["parse_spe", "parse_footer", "read_header", "iso_to_ms", "build_footer_xml",
           "write_spe", "HEADER_BYTES", "OFF_XML"]
