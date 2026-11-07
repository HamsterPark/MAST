"""Which optical-station skills take the instrument token.

Design: docs/v2/design/optical_station_witec.md (D5/D7).
On the optical station the WITec is the instrument. A measurement
protocol holds the token for its whole run, so a second agent must not be able
to turn the analyzer, move the stage or load a state in the middle of it. That
is why the WITec write skills are NOT in ``OFF_INSTRUMENT_NAMES`` (the camera's
exemption) and take the token like any instrument write. Only the stops run
without it: they must reach the very run they stop.
"""

from __future__ import annotations

import pytest

from mast.agents._shared.buffer_hitl import REMEDY_TOOL_NAMES
from mast.core.instrument_lock import BYPASS_NAMES, OFF_INSTRUMENT_NAMES, needs_token
from mast.skills.builtins import optics_witec

PROTOCOLS = ("SHGExcitationScan", "PolarizationMatrix", "TemperatureSeries", "PowerSeries")


@pytest.fixture(scope="module")
def metas():
    from mast.agents.instrument_control.tools import discover_instrument_skills

    return {m.name: m for m in discover_instrument_skills().list_skills()}


def test_witec_stops_bypass_the_token_and_count_as_remedies(metas):
    stops = set(optics_witec.WITEC_STOP_SKILL_NAMES)
    assert stops, "the WITec module must name its stop skills"
    assert stops <= BYPASS_NAMES
    assert stops <= REMEDY_TOOL_NAMES
    for name in stops:
        assert not needs_token(metas[name], name), name


def test_witec_write_skills_take_the_token(metas):
    stops = set(optics_witec.WITEC_STOP_SKILL_NAMES)
    writes = 0
    for name in optics_witec.WITEC_SKILL_NAMES:
        assert name in metas, f"{name} is listed but not registered"
        assert name not in OFF_INSTRUMENT_NAMES, name
        if name in stops:
            continue
        meta = metas[name]
        if str(meta.category.value).lower() in ("read", "analysis"):
            assert not needs_token(meta, name), name
        else:
            writes += 1
            assert needs_token(meta, name), f"{name} drives the WITec and must take the token"
    assert writes >= 10, "the WITec module lost its write skills?"


def test_no_witec_write_skill_is_listed_as_a_stop():
    leaked = (set(optics_witec.WITEC_SKILL_NAMES) & BYPASS_NAMES) - set(optics_witec.WITEC_STOP_SKILL_NAMES)
    assert not leaked


def test_protocols_hold_the_token(metas):
    for name in PROTOCOLS:
        assert name in metas, name
        assert needs_token(metas[name], name), name
        assert name not in BYPASS_NAMES and name not in OFF_INSTRUMENT_NAMES, name


SOURCE_WRITES = ("LaserEmissionOn", "SetLightSourcePower", "SetLaserWavelength", "SetTemperature")
SOURCE_SAFE_DIRECTION = ("LaserEmissionOff", "StopTemperatureRamp")


def test_light_source_safe_direction_skills_never_wait_for_the_token(metas):
    from mast.skills.builtins import optics_sources

    assert set(optics_sources.SAFE_DIRECTION_SKILLS) == set(SOURCE_SAFE_DIRECTION)
    for name in SOURCE_SAFE_DIRECTION:
        assert not needs_token(metas[name], name), name


def test_light_source_writes_take_the_token(metas):
    # Not OFF_INSTRUMENT_NAMES: a protocol
    # holds the token for its whole run, and a second agent switching the source on, changing
    # its power or wavelength, or moving the temperature would corrupt that run.
    for name in SOURCE_WRITES:
        assert name in metas, name
        assert needs_token(metas[name], name), name
        assert name not in OFF_INSTRUMENT_NAMES and name not in BYPASS_NAMES, name


def test_the_busy_message_names_the_instrument_neutrally_only_on_the_optical_station(monkeypatch):
    from mast.core.instrument_lock import InstrumentBusy
    from mast.i18n import use_lang

    holder = {"owner": "外部 agent", "skill": "RunAnalyzerSeries", "held_s": 12.0}
    for lang in ("zh", "en"):
        with use_lang(lang):
            monkeypatch.setenv("MAST_STATION_PROFILE", "stm")
            stm = InstrumentBusy(holder, "SetAnalyzerAngle", 5.0).message()
            # an STM station keeps the exact text it always had
            assert ("同一台 Nanonis 不能被两条链路同时驱动" in stm) if lang == "zh" else (
                "one Nanonis cannot be driven by two chains at once" in stm)
            monkeypatch.setenv("MAST_STATION_PROFILE", "optical")
            opt = InstrumentBusy(holder, "SetAnalyzerAngle", 5.0).message()
            assert "Nanonis" not in opt
            assert ("同一台仪器不能被两条链路同时驱动" in opt) if lang == "zh" else (
                "one instrument cannot be driven by two chains at once" in opt)
