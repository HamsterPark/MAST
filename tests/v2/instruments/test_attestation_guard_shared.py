"""One laser-area attestation guard for every light source (design D7).

The WITec driver and the external light-source drivers each had a copy of the guard and of its
exception. Two classes with one name meant ``except LaserAttestationRequired`` caught one and
missed the other. There is now one implementation, in ``mast.instruments.laser``; the WITec
module re-exports it under the old names.
"""

from __future__ import annotations

import json

from mast.instruments import laser
from mast.instruments.registry import InstrumentRegistry
from mast.instruments.witec import microscope as witec


def test_the_witec_names_are_the_shared_objects():
    assert witec.LaserAttestationGuard is laser.LaserAttestationGuard
    assert witec.LaserAttestationRequired is laser.LaserAttestationRequired
    assert witec.EmissionGuard is laser.EmissionGuard


def test_the_witec_default_guard_follows_its_config():
    on = witec.default_emission_guard(witec.WITecConfig.from_entry({"id": "w", "type": "witec_com"}))
    off = witec.default_emission_guard(witec.WITecConfig.from_entry(
        {"id": "w", "type": "witec_com", "require_laser_attestation": False}))
    assert isinstance(on, laser.LaserAttestationGuard)
    assert off is None


def _reg(tmp_path, devices):
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": devices}), encoding="utf-8")
    return InstrumentRegistry(config_path=p)


def test_a_sim_laser_entry_can_ask_for_the_operator_gate(tmp_path):
    reg = _reg(tmp_path, [
        {"id": "plain", "type": "sim_laser"},
        {"id": "gated", "type": "sim_laser", "require_laser_attestation": True},
        {"id": "gated_tun", "type": "sim_tunable_laser", "require_laser_attestation": True},
    ])
    try:
        assert reg.laser("plain").emission_guard is None          # a sim has no gate by default
        assert isinstance(reg.laser("gated").emission_guard, laser.LaserAttestationGuard)
        assert isinstance(reg.laser("gated_tun").emission_guard, laser.LaserAttestationGuard)
    finally:
        reg.close_all()


# ── the sims model the real gate: ANY raise while emitting adds light on the sample (a double must not
#    allow what the hardware refuses); dimming, switching to 0 and a same-level write never do ──

import pytest  # noqa: E402

from mast.instruments.laser import EmissionControlDisabled, SimLaser, SimTunableLaser  # noqa: E402


def test_a_sim_tunable_source_gates_every_raise_of_a_channel_like_the_nkt():
    src = SimTunableLaser(device_id="sel", allow_emission_control=False)
    src.emitting = True                                    # the operator switched it on by hand
    with pytest.raises(EmissionControlDisabled):
        src.set_amplitude_pct(10, channel=0)               # dark channel -> light: refused
    assert src.channels()[0]["amplitude_pct"] == 0.0
    src._ch[0]["amplitude_pct"] = 5.0                      # the operator lit it
    with pytest.raises(EmissionControlDisabled):
        src.set_amplitude_pct(10, channel=0)               # brighter is more light on the sample: refused too
    assert src.channels()[0]["amplitude_pct"] == 5.0
    assert src.set_amplitude_pct(5, channel=0) == 5.0      # the same level adds nothing
    assert src.set_amplitude_pct(3, channel=0) == 3.0      # dimming is never gated
    assert src.set_amplitude_pct(0, channel=0) == 0.0      # darkening neither
    src.emitting = False
    assert src.set_amplitude_pct(10, channel=1) == 10.0    # no emission: raising starts no light


def test_a_sim_tunable_source_asks_the_operator_gate_for_every_raise():
    asked: list[str] = []
    src = SimTunableLaser(device_id="sel", emission_guard=asked.append)
    src.emitting = True
    src.set_amplitude_pct(10, channel=2)                   # dark -> lit
    assert len(asked) == 1 and "channel 2" in asked[0]
    src.set_amplitude_pct(20, channel=2)                   # brighter
    assert len(asked) == 2 and "channel 2" in asked[1]
    for pct in (20, 15, 0):                                # same level, dimmer, off
        src.set_amplitude_pct(pct, channel=2)
    assert len(asked) == 2


def test_a_sim_laser_gates_every_raise_of_its_power_while_emitting():
    las = SimLaser(device_id="las", allow_emission_control=False)
    las.emitting, las.power_pct = True, 0.0
    with pytest.raises(EmissionControlDisabled):
        las.set_power(10)                                  # from 0: starts light
    las.power_pct = 5.0
    with pytest.raises(EmissionControlDisabled):
        las.set_power(10)                                  # from 5: more light
    assert las.power_pct == 5.0
    assert las.set_power(5) == 5.0 and las.set_power(2) == 2.0 and las.set_power(0) == 0.0
    las.emitting = False
    assert las.set_power(80) == 80.0                       # no emission: nothing to add to


def test_a_sim_laser_asks_the_operator_gate_for_every_raise_of_its_power():
    asked: list[str] = []
    las = SimLaser(device_id="las", emission_guard=asked.append)
    las.emitting, las.power_pct = True, 5.0
    las.set_power(10)
    assert len(asked) == 1 and "power 10" in asked[0]
    las.set_power(10), las.set_power(4), las.set_power(0)
    assert len(asked) == 1
