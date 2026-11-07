"""Declarative optical-instrument inventory — ``config/optical_instruments.json``.

Follows the ``mast.environment.config`` pattern (external JSON manifest,
atomic save, resolved via ``project_root()``) rather than baking hardware
into ``mast/config.py``: benches differ per rig and get re-wired without a
code release.

Schema::

    {
      "devices": [
        {
          "id": "delay1",                  // unique handle
          "name": "泵浦-探测延迟线",         // display name
          "type": "pztc_nm003",            // driver key, see DEVICE_TYPES
          "enabled": true,
          // -- pztc_nm003 --
          "port": "COM17", "baudrate": 115200, "slave": 1,
          "counts_per_unit": 1.0,          // encoder counts per axis unit
          "position_tolerance": 5.0,       // counts
          "register_map": {                // one-time on-rig calibration
            "setpoint_place": null, "setpoint_speed": null,
            "place_counter": null, "closed_loop_move": null,
            "look_zero": null, "zero_status": null, "stop": null,
            "channel_stride": 0, "word_order_big": true
          },
          // -- pi_gcs --
          //   "port": "COM18", "baudrate": 115200, "model": "E-861",
          //   "servo_on_connect": true,
          // -- thorlabs_benchtop_piezo --
          //   "serial_no": "71000001", "dll_dir": null, "closed_loop": true,
          // -- satellite_* (device on a satellite node, via the hub, §8.1) --
          //   "satellite": "<satellite id>", "device": "pol",
          //   satellite_motion:   "max_step_mm": 0.5, axes x/y/z (channel "x"…), mm
          //   satellite_rotation: "wrap": false, "shortest_path": false, one axis, deg
          //   satellite_openloop: "max_steps_per_cmd": 2000, axes channel = motor no.
          //   satellite_spectrometer: "laser_line_nm": 633, "limits": {...}, no axes
          // -- sim_* (in-process simulators: tests / office) --
          //   sim_spectrometer, sim_rotation, sim_openloop
          "role": null,                    // device-level role (spectrometers)
          // tip state required before this device moves (manual panel, §13.1):
          //   "none" (default — the behaviour before the field existed),
          //   "tip_withdrawn", "z_feedback_on". Checked by every skill that moves
          //   it (OpticalStageMove/Wiggle, HomeOpticalStage, OpticalStageServo,
          //   DelayLineMoveTo, OpticalStageScan, PumpProbeScan; for satellite
          //   entries MoveXYZStage, PicomotorJog, SetPolarizerAngle, HomePolarizer);
          //   an unknown value refuses the motion.
          "tip_requirement": "none",
          // optional: the manual panel's pre-filled jog / wiggle step (axis
          // units); absent ⇒ 1 µm (0.1° / 10 steps). Only a pre-filled value —
          // soft limits and the skills still decide.
          "jog_step": 1.0,
          "axes": [
            {"name": "delay", "channel": 1,
             "min_pos": 0.0, "max_pos": 15000.0, "unit": "um",
             "settle_s": 0.05, "role": "delay_line",
             // optional: SetOpticalStageVelocity refuses above it (unit/s);
             // the PZTC sets no velocity without it
             "max_velocity": 2000.0}
          ]
        }
      ],
      "delay_line": {                       // pump-probe delay conversion
        "device_id": "delay1", "axis": "delay",
        "unit_per_mm": 1000.0,              // axis native units per mm
        "ps_per_mm": 6.6713,                // optical delay per stage mm
        "zero_offset_mm": 0.0,              // stage pos (mm) of zero delay
        "sign": 1                           // +1: larger pos = later probe
      }
    }
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from mast._runtime_paths import project_root

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIG_FILENAME",
    "DEVICE_TYPES",
    "SATELLITE_TYPES",
    "SPECTROMETER_TYPES",
    "device_kind",
    "config_path",
    "load_config",
    "save_config",
    "device_by_id",
    "TIP_REQUIREMENTS",
    "device_tip_requirement",
]

#: a device's ``tip_requirement`` values — the Nanonis optical groups' ``requires``
TIP_REQUIREMENTS = ("none", "tip_withdrawn", "z_feedback_on")


def device_tip_requirement(entry: dict[str, Any] | None) -> str:
    """The device's ``tip_requirement``; absent ⇒ ``"none"`` (what every stage skill
    did before the field existed). An unknown value raises :class:`ValueError` —
    a misspelt requirement must not read as "no requirement"."""
    raw = (entry or {}).get("tip_requirement")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "none"
    value = str(raw).strip()
    if value not in TIP_REQUIREMENTS:
        raise ValueError(
            f"optical device {(entry or {}).get('id')!r}: tip_requirement={raw!r} is not one "
            f"of {TIP_REQUIREMENTS}")
    return value

CONFIG_FILENAME = "optical_instruments.json"

#: devices on a satellite node, reached through the hub (contract §8.1)
SATELLITE_TYPES = ("satellite_spectrometer", "satellite_motion", "satellite_rotation",
                   "satellite_openloop")

#: optical-station devices (docs/v2/design/optical_station_witec.md §6.1). The real
#: drivers live in their own modules and expose ``build(entry)``; see
#: ``registry._STATION_DRIVERS``.
MICROSCOPE_TYPES = ("witec_com", "sim_microscope")
LASER_TYPES = ("nkt_superk", "npi_rainbow", "sim_laser", "sim_tunable_laser")
POWER_METER_TYPES = ("thorlabs_pm", "sim_power_meter")
TEMPERATURE_TYPES = ("tc100", "sim_temperature")

#: driver keys accepted in a device entry's "type"
DEVICE_TYPES = ("pztc_nm003", "pi_gcs", "thorlabs_benchtop_piezo", "thorlabs_pdxc2",
                *SATELLITE_TYPES, "sim_spectrometer", "sim_rotation", "sim_openloop",
                *MICROSCOPE_TYPES, *LASER_TYPES, *POWER_METER_TYPES, *TEMPERATURE_TYPES)

#: types that build a Spectrometer rather than a MotionController. A microscope is
#: also a spectrometer: ``registry.spectrometer(id)`` returns the microscope object.
SPECTROMETER_TYPES = ("satellite_spectrometer", "sim_spectrometer", *MICROSCOPE_TYPES)

_KIND_BY_TYPE = {
    "satellite_spectrometer": "spectrometer", "sim_spectrometer": "spectrometer",
    "satellite_rotation": "rotation", "sim_rotation": "rotation",
    "satellite_openloop": "openloop", "sim_openloop": "openloop",
    **{t: "microscope" for t in MICROSCOPE_TYPES},
    **{t: "laser" for t in LASER_TYPES},
    **{t: "power_meter" for t in POWER_METER_TYPES},
    **{t: "temperature" for t in TEMPERATURE_TYPES},
}

#: kinds that are NOT motion controllers (``registry.controller(id)`` refuses them)
NON_MOTION_KINDS = ("spectrometer", "microscope", "laser", "power_meter", "temperature")


def device_kind(dev_type: str) -> str:
    """``motion`` | ``rotation`` | ``openloop`` | ``spectrometer`` | ``microscope`` |
    ``laser`` | ``power_meter`` | ``temperature`` from a type key."""
    return _KIND_BY_TYPE.get(str(dev_type or ""), "motion")

_DEFAULT_CONFIG: dict[str, Any] = {"devices": [], "delay_line": None}


def config_path() -> Path:
    return project_root() / "config" / CONFIG_FILENAME


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Read the manifest; a missing/corrupt file degrades to the empty
    inventory (never raises — no-hardware machines must boot clean)."""
    p = path or config_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return json.loads(json.dumps(_DEFAULT_CONFIG))
    except Exception as exc:
        logger.error("optical_instruments config unreadable (%s): %s", p, exc)
        return json.loads(json.dumps(_DEFAULT_CONFIG))
    if not isinstance(raw, dict):
        logger.error("optical_instruments config is not an object: %s", p)
        return json.loads(json.dumps(_DEFAULT_CONFIG))
    raw.setdefault("devices", [])
    raw.setdefault("delay_line", None)
    if not isinstance(raw["devices"], list):
        raw["devices"] = []
    return raw


def save_config(cfg: dict[str, Any], path: Path | None = None) -> Path:
    """Atomic write (tmp + replace), mirroring environment.config."""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(cfg, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        prefix=p.stem + ".", suffix=".tmp", dir=str(p.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return p


def device_by_id(cfg: dict[str, Any], device_id: str) -> dict[str, Any] | None:
    for entry in cfg.get("devices", []):
        if isinstance(entry, dict) and entry.get("id") == device_id:
            return entry
    return None
