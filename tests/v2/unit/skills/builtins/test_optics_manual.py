"""Manual control panel, this machine's side (contract §13.1).

* ``mast.instruments.manual.local_manual_devices`` — the ManualDevice shape, from
  config alone: disabled / misconfigured devices are listed unavailable with the
  reason, satellites / spectrometers / the STM coarse group are not listed, every
  button names a registered skill and copies that skill's own parameters,
  occupancy is evidence or ``None``, and nothing opens a port;
* the new stage skills — GetOpticalStageStatus (never opens a device unless
  asked), SetOpticalStageVelocity, OpticalStageServo, DisconnectOpticalDevice;
* a device's ``tip_requirement``: absent = no check and no STM read (the old
  behaviour), set = checked by every skill that moves the device.
"""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[5] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import json

import pytest

from mast.core.types import HardwareState
from mast.instruments import motor_groups as mg
from mast.instruments import registry as reg_mod
from mast.instruments.base import (
    AxisConfig,
    AxisStatus,
    MotionAxis,
    MotionController,
)
from mast.instruments.manual import local_manual_devices
from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry

# ── config fixtures ──────────────────────────────────────────────────────────

DEVICES = [
    {"id": "zs", "name": "泽攸二维台", "type": "pztc_nm003", "enabled": True, "port": "COM16",
     "counts_per_unit": 200.0,
     "axes": [{"name": "x", "channel": 0, "min_pos": 0, "max_pos": 5000, "unit": "um",
               "max_velocity": 2000.0},
              {"name": "y", "channel": 1, "min_pos": 0, "max_pos": 5000, "unit": "um"}]},
    {"id": "pi", "name": "PI E-861", "type": "pi_gcs", "enabled": False, "port": "COM17",
     "model": "E-861", "serial_no": "0000000001",
     "axes": [{"name": "s2", "channel": "1", "min_pos": 0, "max_pos": 13, "unit": "mm"}]},
    {"id": "pd", "name": "PDXC2", "type": "thorlabs_pdxc2", "enabled": True,
     "serial_no": "112000001", "counts_per_unit": 1e6, "tip_requirement": "tip_withdrawn",
     "axes": [{"name": "s1", "channel": 1, "min_pos": 0, "max_pos": 20, "unit": "mm"}]},
    {"id": "bad", "name": "错拼的要求", "type": "pztc_nm003", "enabled": True, "port": "COM13",
     "tip_requirement": "withdrawn",
     "axes": [{"name": "d", "channel": 0, "min_pos": 0, "max_pos": 10}]},
    {"id": "sat_pol", "type": "satellite_rotation", "enabled": True, "satellite": "s1",
     "device": "pol", "axes": [{"name": "angle", "channel": 1, "min_pos": -360,
                                "max_pos": 720, "unit": "deg"}]},
    {"id": "ccd", "type": "sim_spectrometer", "enabled": True},
]

GROUPS = {
    "stm_coarse_group": 0,
    "groups": [
        {"group": 1, "name": "Mirror X/Y", "role": "laser_mirror",
         "axes": {"X": "mirror_x", "Y": "mirror_y"}, "max_steps_per_cmd": 200,
         "requires": "tip_withdrawn", "freq_hz": 442.0, "amp_v": 200.0},
        {"group": 2, "name": "Beam Deflection", "role": "beam_deflection",
         "axes": {"X": "beam_deflection"}, "max_steps_per_cmd": 50, "requires": "none",
         "freq_hz": 300.0, "amp_v": 50.0, "enabled": False},
    ],
}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A tmp optical config + tmp motor-group config; port guard and latch hermetic;
    any attempt to open a serial port or load the Kinesis DLL fails the test."""
    from mast.core import port_guard as pg
    # the drivers bind ``open_transport`` by name at import: import them first and
    # patch the names they use — patching serial_transport while a driver is first
    # imported would bind the stand-in into it for the rest of the session
    from mast.instruments import pi_gcs, pztc_nm003, thorlabs_pdxc2

    def no_hardware(*a, **k):
        raise AssertionError("the manual-panel describer touched hardware")

    monkeypatch.setattr(pztc_nm003, "open_transport", no_hardware)
    monkeypatch.setattr(pi_gcs, "open_transport", no_hardware)
    monkeypatch.setattr(thorlabs_pdxc2, "_load_pdxc2_dll", no_hardware)
    monkeypatch.setattr(pg, "last_report", lambda: None)
    monkeypatch.setattr(pg, "load_guard_config", lambda path=None: ({}, None))
    gp = tmp_path / mg.CONFIG_FILENAME
    gp.write_text(json.dumps(GROUPS), encoding="utf-8")
    monkeypatch.setattr(mg, "config_path", lambda: gp)
    mg.reset_for_tests()
    op = tmp_path / "optical_instruments.json"
    op.write_text(json.dumps({"devices": DEVICES}), encoding="utf-8")
    reg = InstrumentRegistry(op)
    yield reg
    mg.reset_for_tests()


@pytest.fixture(scope="module")
def skill_registry():
    from mast.core.registry import SkillRegistry

    r = SkillRegistry()
    r.discover("mast.skills.builtins")
    return r


def _by_id(devices):
    return {d["id"]: d for d in devices}


# ── the describer ────────────────────────────────────────────────────────────


class TestDescriber:
    def test_lists_local_stages_and_optical_groups_only(self, setup, skill_registry):
        notes: list[str] = []
        devs = local_manual_devices(instruments=setup, skill_registry=skill_registry,
                                    notes=notes, processes=[])
        assert [d["id"] for d in devs] == [
            "local/stage/zs", "local/stage/pi", "local/stage/pd", "local/stage/bad",
            "local/nanonis_group/1", "local/nanonis_group/2"]
        assert notes == []
        for d in devs:
            assert set(d) >= {"id", "machine", "kind", "label", "vendor", "model", "serial",
                              "available", "unavailable_reason", "occupied_by",
                              "tip_requirement", "axes", "actions", "readout"}
            assert d["machine"] == "local"
            json.dumps(d)                                  # wire-safe
        assert setup.built_controllers() == {}             # nothing connected

    def test_disabled_and_misconfigured_devices_say_why(self, setup, skill_registry):
        d = _by_id(local_manual_devices(instruments=setup, skill_registry=skill_registry,
                                        processes=[]))
        pi = d["local/stage/pi"]
        assert pi["available"] is False and pi["unavailable_reason"] == "未启用"
        assert pi["readout"] is None and pi["serial"] == "0000000001"
        bad = d["local/stage/bad"]
        assert bad["available"] is False and "tip_requirement" in bad["unavailable_reason"]
        assert bad["tip_requirement"] is None
        g2 = d["local/nanonis_group/2"]
        assert g2["available"] is False and g2["unavailable_reason"] == "未启用"

    def test_buttons_follow_what_each_driver_documents(self, setup, skill_registry):
        d = _by_id(local_manual_devices(instruments=setup, skill_registry=skill_registry,
                                        processes=[]))
        ids = {k: [a["id"] for a in v["actions"]] for k, v in d.items()}
        assert ids["local/stage/zs"] == ["jog", "move_abs", "home", "set_velocity", "wiggle",
                                         "connect", "disconnect", "stop"]
        assert ids["local/stage/pi"] == ["jog", "move_abs", "home", "set_velocity", "servo_on",
                                         "servo_off", "wiggle", "connect", "disconnect", "stop"]
        # the PDXC2's Home re-zeroes the counter and does not move: it is a set_zero
        assert ids["local/stage/pd"] == ["jog", "move_abs", "set_zero", "wiggle", "connect",
                                         "disconnect", "stop"]
        sz = next(a for a in d["local/stage/pd"]["actions"] if a["id"] == "set_zero")
        assert "不动" in sz["hint"] and sz["skill"] == "HomeOpticalStage"
        assert d["local/stage/pd"]["tip_requirement"] == "tip_withdrawn"
        assert d["local/stage/zs"]["tip_requirement"] == "none"
        zs = d["local/stage/zs"]
        # read through GET /api/manual/readouts (not a skill execution, contract §13.1)
        assert zs["readout"] == {"kind": "poll", "interval_s": 1.0}
        assert [a["extra"]["max_velocity"] for a in zs["axes"]] == [2000.0, None]
        assert [(a["min"], a["max"], a["unit"]) for a in zs["axes"]] == [(0, 5000, "um")] * 2

    def test_every_button_is_a_registered_skill_with_its_own_parameters(self, setup,
                                                                        skill_registry):
        from mast.core.instrument_lock import BYPASS_NAMES
        from mast.instruments.manual import _param_dict

        devs = local_manual_devices(instruments=setup, skill_registry=skill_registry,
                                    processes=[])
        metas = {m.name: m for m in skill_registry.list_skills()}
        seen = 0
        for dev in devs:
            for a in dev["actions"]:
                seen += 1
                meta = metas[a["skill"]]                       # registered
                specs = {p.name: p for p in meta.parameters}
                assert set(a["fixed"]) <= set(specs), (dev["id"], a["id"])
                for p in a["params"]:     # verbatim from the skill, bar a pre-filled default
                    want = _param_dict(specs[p["name"]])
                    assert {k: v for k, v in p.items() if k != "default"} == {
                        k: v for k, v in want.items() if k != "default"}, (a["id"], p["name"])
                assert a["safety"] == meta.safety_level.value
                assert a["bypass_lock"] == (a["skill"] in BYPASS_NAMES)
                if a["per_axis"]:
                    assert a["axis_param"] in specs
                    assert all(ax["name"] for ax in dev["axes"])
                if a["direction"] == "signed":
                    assert a["direction_param"] in [p["name"] for p in a["params"]]
                assert a["hold_repeat_ms"] is None or a["hold_repeat_ms"] > 0
                if a["id"] == "stop":
                    assert a["bypass_lock"] is True and a["safety"] == "auto"
        assert seen > 30

    def test_the_hub_endpoint_accepts_every_device_and_keeps_every_button(self, setup,
                                                                           skill_registry):
        """What GET /api/manual/devices does with a provider's dicts: validate
        against ManualDevice, then re-check every action against the registry."""
        from mast.api.routes.manual import _checked_action
        from mast.api.schemas_manual import ManualDevice

        notes: list[str] = []
        for raw in local_manual_devices(instruments=setup, skill_registry=skill_registry,
                                        processes=[]):
            dev = ManualDevice.model_validate(raw)
            kept = [_checked_action(dev.id, a, skill_registry, notes) for a in dev.actions]
            assert all(k is not None for k in kept), notes
        assert notes == []

    def test_jog_steps_are_pre_filled_small(self, setup, skill_registry):
        """A jog needs a step the operator need not type — 1 µm in
        the axis unit (or the device's ``jog_step``), 10 steps for a group (never
        above its cap). A pre-filled value only: the skills keep their limits."""
        def dflt(dev, action, param):
            a = next(x for x in dev["actions"] if x["id"] == action)
            return next(p for p in a["params"] if p["name"] == param)["default"]

        d = _by_id(local_manual_devices(instruments=setup, skill_registry=skill_registry,
                                        processes=[]))
        assert dflt(d["local/stage/zs"], "jog", "position") == 1.0           # µm
        assert dflt(d["local/stage/zs"], "wiggle", "delta") == 1.0
        assert dflt(d["local/stage/pi"], "jog", "position") == 0.001         # mm
        assert dflt(d["local/stage/pd"], "jog", "position") == 0.001
        assert dflt(d["local/nanonis_group/1"], "jog", "steps") == 10
        # the absolute target and the velocity are never guessed
        assert dflt(d["local/stage/zs"], "move_abs", "position") is None
        assert dflt(d["local/stage/zs"], "set_velocity", "velocity") is None

    def test_jog_step_from_config_and_the_group_cap(self, tmp_path, setup, skill_registry):
        from mast.instruments.manual import _jog_default

        assert _jog_default({"jog_step": 0.25, "axes": [{"unit": "mm"}]}) == 0.25
        assert _jog_default({"axes": [{"unit": "deg"}]}) == 0.1
        assert _jog_default({"axes": [{"unit": "um"}, {"unit": "mm"}]}) is None   # mixed
        assert _jog_default({"axes": [{"unit": "furlong"}]}) is None
        gp = mg.config_path()
        gp.write_text(json.dumps({"stm_coarse_group": 0, "groups": [
            {"group": 3, "name": "tiny", "role": "lens", "axes": {"Z": "f"},
             "max_steps_per_cmd": 5, "requires": "none", "freq_hz": 300.0,
             "amp_v": 50.0}]}), encoding="utf-8")
        mg.reset_for_tests()
        g = _by_id(local_manual_devices(instruments=setup, skill_registry=skill_registry,
                                        processes=[]))["local/nanonis_group/3"]
        jog = next(a for a in g["actions"] if a["id"] == "jog")
        assert next(p for p in jog["params"] if p["name"] == "steps")["default"] == 5

    def test_nanonis_group_buttons(self, setup, skill_registry):
        mg.note_steps(1, "X", 30)
        g = _by_id(local_manual_devices(instruments=setup, skill_registry=skill_registry,
                                        processes=[]))["local/nanonis_group/1"]
        assert g["kind"] == "nanonis_motor_group" and g["tip_requirement"] == "tip_withdrawn"
        assert "Group 2" in g["label"]                    # TCP 1 = panel Group 2
        jog, stop = g["actions"]
        assert jog["skill"] == "NanonisMotorGroupMove" and jog["fixed"] == {"group": "1"}
        assert jog["direction"] == "signed" and jog["direction_param"] == "direction"
        assert next(p for p in jog["params"] if p["name"] == "direction")[
            "allowed_values"] == ["+", "-"]
        assert jog["hold_repeat_ms"] == 150 and jog["stop_on_release"] is False
        assert stop["skill"] == "StopMotor" and "所有" in stop["label"] and stop["bypass_lock"]
        x = next(a for a in g["axes"] if a["name"] == "mirror_x")
        assert x["extra"]["odometer_steps"] == 30 and x["extra"]["position_feedback"] is False
        assert x["extra"]["max_steps_per_cmd"] == 200 and x["position"] is None
        assert g["readout"] is None                     # no position feedback (§13.1)

    def test_a_broken_group_file_is_a_note_not_a_failure(self, setup, skill_registry):
        mg.config_path().write_text("{broken", encoding="utf-8")
        mg.reset_for_tests()
        notes: list[str] = []
        devs = local_manual_devices(instruments=setup, skill_registry=skill_registry,
                                    notes=notes, processes=[])
        assert all(d["kind"] != "nanonis_motor_group" for d in devs)
        assert len(devs) == 4 and any("无法使用" in n for n in notes)

    def test_an_unregistered_skill_drops_its_buttons_with_a_note(self, setup, skill_registry):
        class _Without:
            def list_skills(self):
                return [m for m in skill_registry.list_skills()
                        if m.name != "OpticalStageServo"]

        notes: list[str] = []
        pi = _by_id(local_manual_devices(instruments=setup, skill_registry=_Without(),
                                         notes=notes, processes=[]))["local/stage/pi"]
        assert "servo_on" not in [a["id"] for a in pi["actions"]]
        assert any("OpticalStageServo" in n for n in notes)


class TestOccupancy:
    def test_running_vendor_programs_are_named_as_possible_holders(self, setup,
                                                                   skill_registry):
        procs = [{"pid": 7, "name": "LabVIEW.exe"},
                 {"pid": 9, "name": "Thorlabs.MotionControl.Kinesis.exe"}]
        d = _by_id(local_manual_devices(instruments=setup, skill_registry=skill_registry,
                                        processes=procs))
        assert "LabVIEW" in d["local/stage/zs"]["occupied_by"]
        assert "可能" in d["local/stage/zs"]["occupied_by"]
        assert "Kinesis" in d["local/stage/pd"]["occupied_by"]
        assert d["local/nanonis_group/1"]["occupied_by"] is None
        quiet = _by_id(local_manual_devices(instruments=setup, skill_registry=skill_registry,
                                            processes=[]))
        assert all(v["occupied_by"] is None for v in quiet.values())   # None = unknown

    def test_a_device_this_process_holds_is_not_occupied(self, setup, skill_registry,
                                                         monkeypatch):
        monkeypatch.setattr(setup, "is_connected", lambda dev: dev == "zs")
        d = _by_id(local_manual_devices(instruments=setup, skill_registry=skill_registry,
                                        processes=[{"pid": 7, "name": "LabVIEW.exe"}]))
        assert d["local/stage/zs"]["occupied_by"] is None
        assert all(a["extra"]["connected"] for a in d["local/stage/zs"]["axes"])

    def test_port_guard_findings_are_used_as_evidence(self, setup, skill_registry,
                                                      monkeypatch):
        from mast.core import port_guard as pg

        rep = pg.PortGuardReport(checked_ms=0, ok=False, enforce=True)
        rep.optics = [{"id": "zs", "owners": [123]}]
        rep.conflicts = [
            pg.Finding(kind="serial_port", resource="COM16（zs）", severity="conflict",
                       pid=123, process="LabVIEW.exe"),
            pg.Finding(kind="nanonis_tcp", resource="tcp:6501(main)", severity="conflict",
                       pid=456, process="LabVIEW.exe"),
        ]
        monkeypatch.setattr(pg, "last_report", lambda: rep)
        d = _by_id(local_manual_devices(instruments=setup, skill_registry=skill_registry,
                                        processes=[]))
        assert d["local/stage/zs"]["occupied_by"].startswith("COM16 正被 LabVIEW.exe（pid 123）")
        assert "6501" in d["local/nanonis_group/1"]["occupied_by"]


# ── the new skills over a fake controller ───────────────────────────────────


class _Axis(MotionAxis):
    SUPPORTS = frozenset({"velocity"})

    def __init__(self, config, controller):
        super().__init__(config, controller)
        self.position = 1.0
        self.moves: list[float] = []
        self.velocity = None

    def _move_abs_raw(self, target):
        self.position = target
        self.moves.append(target)

    def _get_position_raw(self):
        return self.position

    def _get_status_raw(self):
        return AxisStatus(position=self.position, moving=False, on_target=True)

    def _stop_raw(self):
        self._controller.stops += 1

    def _home_raw(self):
        self.position = 0.0
        self.moves.append(0.0)

    def _status_details_raw(self):
        return {"velocity": self.velocity, "servo": True}

    def _set_velocity_raw(self, v):
        self.velocity = v
        return v


class _Ctl(MotionController):
    def __init__(self, configs):
        super().__init__()
        self._configs = configs
        self.connects = 0
        self.closes = 0
        self.stops = 0

    def _connect_raw(self):
        self.connects += 1

    def _close_raw(self):
        self.closes += 1

    def _build_axes(self):
        return {c.name: _Axis(c, self) for c in self._configs}


def _stage(dev_id="st", **extra):
    return {"id": dev_id, "name": dev_id, "type": "pztc_nm003", "enabled": True,
            "port": "COM13", **extra,
            "axes": [{"name": "x", "channel": 0, "min_pos": 0, "max_pos": 100, "unit": "um",
                      "max_velocity": 50.0}]}


@pytest.fixture
def stages(tmp_path, monkeypatch):
    """Install a registry over a tmp config whose controllers are ``_Ctl``."""
    built: dict[str, _Ctl] = {}

    def build(entry):
        ctl = _Ctl(reg_mod._axis_configs(entry))
        built[entry["id"]] = ctl
        return ctl

    monkeypatch.setattr(reg_mod, "_build_controller", build)

    def install(*entries, delay_line=None):
        p = tmp_path / "oi.json"
        p.write_text(json.dumps({"devices": list(entries), "delay_line": delay_line}),
                     encoding="utf-8")
        reg = InstrumentRegistry(p)
        reset_instrument_registry(reg)
        return reg

    install.built = built
    yield install
    reset_instrument_registry(None)


class _Ctx:
    """ExecutionContext double: a state that counts its reads, no abort."""

    def __init__(self, *, withdrawn=True, z_on=False, scan=False, state=True):
        self.reads = 0
        if state:
            snap = HardwareState(withdrawn=withdrawn, z_controller_on=z_on, scan_running=scan)
            ctx = self

            class _S:
                def snapshot(self):
                    ctx.reads += 1
                    return snap

                def refresh(self):
                    ctx.reads += 1
                    return snap

            self.state = _S()

    def check_abort(self):
        return False


class TestStageSkills:
    def test_status_never_opens_a_device_unless_asked(self, stages):
        from mast.skills.builtins.optics_stage import GetOpticalStageStatus

        reg = stages(_stage())
        res = GetOpticalStageStatus().execute(_Ctx(), {"device_id": "st"})
        assert res.success and res.data["connected"] is False
        assert res.data["axes"]["x"]["limits"] == [0, 100]
        assert stages.built == {} and reg.built_controllers() == {}
        res = GetOpticalStageStatus().execute(_Ctx(), {"device_id": "st", "connect": True})
        assert res.success and res.data["connected"] is True, res.error
        x = res.data["axes"]["x"]
        assert x["position"] == 1.0 and x["servo"] is True and x["supports"] == ["velocity"]
        assert x["max_velocity"] == 50.0 and stages.built["st"].connects == 1
        again = GetOpticalStageStatus().execute(_Ctx(), {"device_id": "st", "axis": "x"})
        assert again.data["connected"] is True and list(again.data["axes"]) == ["x"]

    def test_status_of_a_disabled_or_unknown_device_fails(self, stages):
        from mast.skills.builtins.optics_stage import GetOpticalStageStatus

        stages(_stage(enabled=False))
        res = GetOpticalStageStatus().execute(_Ctx(), {"device_id": "st", "connect": True})
        assert not res.success and "未启用" in res.error and stages.built == {}
        res = GetOpticalStageStatus().execute(_Ctx(), {"device_id": "nope"})
        assert not res.success and "nope" in res.error

    def test_velocity_goes_through_the_driver_and_its_limit(self, stages):
        from mast.skills.builtins.optics_stage import SetOpticalStageVelocity

        stages(_stage())
        ok = SetOpticalStageVelocity().execute(_Ctx(), {"device_id": "st", "axis": "x",
                                                        "velocity": 20.0})
        assert ok.success and ok.data["velocity"] == 20.0 and ok.data["unit"] == "um/s"
        over = SetOpticalStageVelocity().execute(_Ctx(), {"device_id": "st", "axis": "x",
                                                          "velocity": 60.0})
        assert not over.success and "max_velocity" in over.error

    def test_servo_on_a_driver_without_it_is_not_supported(self, stages):
        from mast.skills.builtins.optics_stage import OpticalStageServo

        stages(_stage())
        res = OpticalStageServo().execute(_Ctx(), {"device_id": "st", "axis": "x", "on": False})
        assert not res.success and "not supported" in res.error and "不要重试" in res.error

    def test_disconnect_stops_then_hands_the_device_back(self, stages):
        from mast.skills.builtins.optics_stage import (
            DisconnectOpticalDevice,
            OpticalStageMove,
        )

        reg = stages(_stage())
        cold = DisconnectOpticalDevice().execute(_Ctx(), {"device_id": "st"})
        assert cold.success and cold.data["was_connected"] is False
        assert OpticalStageMove().execute(_Ctx(), {"device_id": "st", "axis": "x",
                                                   "position": 5.0}).success
        ctl = stages.built["st"]
        res = DisconnectOpticalDevice().execute(_Ctx(), {"device_id": "st"})
        assert res.success and res.data["was_connected"] is True
        assert ctl.stops == 1 and ctl.closes == 1
        assert not reg.is_connected("st") and reg.built_controllers() == {}

    def test_stop_says_when_mast_had_nothing_open(self, stages):
        """The panel keeps stop clickable on every card. On a device MAST has not
        opened, "stopped" would be false comfort — another program may be driving it."""
        from mast.skills.builtins.optics_stage import OpticalStageMove, StopOpticalStage

        stages(_stage(), _stage("off", enabled=False))
        cold = StopOpticalStage().execute(_Ctx(), {"device_id": "st"})
        assert cold.success and cold.data["open"] is False and cold.data["stopped"] is None
        assert "别的程序" in cold.summary
        off = StopOpticalStage().execute(_Ctx(), {"device_id": "off"})
        assert off.success and off.data["open"] is False and "未启用" in off.summary
        assert all(c.connects == 0 for c in stages.built.values())   # stop never opens
        assert OpticalStageMove().execute(_Ctx(), {"device_id": "st", "axis": "x",
                                                   "position": 5.0}).success
        hot = StopOpticalStage().execute(_Ctx(), {"device_id": "st"})
        assert hot.success and hot.data == {"stopped": "st", "open": True}
        assert stages.built["st"].stops == 1
        unknown = StopOpticalStage().execute(_Ctx(), {"device_id": "nope"})
        assert not unknown.success and "nope" in unknown.error

    def test_a_motion_skill_opens_a_closed_device_itself(self, stages):
        """What the panel's 「未连接」 line may promise: pressing jog / move / home
        opens the device (after the tip check) — 「连接」 is not a prerequisite."""
        from mast.skills.builtins.optics_stage import HomeOpticalStage, OpticalStageMove

        stages(_stage())
        assert OpticalStageMove().execute(_Ctx(), {"device_id": "st", "axis": "x",
                                                   "position": 2.0, "relative": True}).success
        assert stages.built["st"].connects == 1
        assert HomeOpticalStage().execute(_Ctx(), {"device_id": "st", "axis": "x"}).success
        assert stages.built["st"].connects == 1                      # opened once

    def test_disconnect_refuses_a_satellite_device(self, stages):
        from mast.skills.builtins.optics_stage import DisconnectOpticalDevice

        stages({"id": "pol", "type": "satellite_rotation", "enabled": True, "satellite": "s1",
                "device": "pol", "axes": [{"name": "angle", "channel": 1, "min_pos": -360,
                                           "max_pos": 720, "unit": "deg"}]})
        res = DisconnectOpticalDevice().execute(_Ctx(), {"device_id": "pol"})
        assert not res.success and "卫星" in res.error and stages.built == {}

    def test_the_inventory_shows_the_tip_requirement(self, stages):
        from mast.skills.builtins.optics_stage import ListOpticalDevices

        stages(_stage(), _stage("w", tip_requirement="tip_withdrawn"))
        devs = {d["id"]: d for d in
                ListOpticalDevices().execute(_Ctx(), {}).data["devices"]}
        assert devs["st"]["tip_requirement"] == "none"
        assert devs["w"]["tip_requirement"] == "tip_withdrawn"
        assert devs["st"]["axes"][0]["max_velocity"] == 50.0


class TestTipRequirement:
    def test_absent_means_no_check_and_no_stm_read(self, stages):
        from mast.skills.builtins.optics_stage import OpticalStageMove

        stages(_stage())
        ctx = _Ctx(withdrawn=False)
        res = OpticalStageMove().execute(ctx, {"device_id": "st", "axis": "x", "position": 5.0})
        assert res.success and ctx.reads == 0

    @pytest.mark.parametrize("skill,params", [
        ("OpticalStageMove", {"position": 5.0}),
        ("OpticalStageWiggle", {"delta": 1.0}),
        ("HomeOpticalStage", {}),
        ("OpticalStageServo", {"on": True}),
    ])
    def test_every_motion_skill_checks_it(self, stages, skill, params):
        from mast.skills.builtins import optics_stage

        stages(_stage(tip_requirement="tip_withdrawn"))
        cls = getattr(optics_stage, skill)
        res = cls().execute(_Ctx(withdrawn=False), {"device_id": "st", "axis": "x", **params})
        assert not res.success and "tip_withdrawn" in res.error, res.error
        assert stages.built == {}                        # refused before the device was opened
        if skill != "OpticalStageServo":
            ok = cls().execute(_Ctx(withdrawn=True), {"device_id": "st", "axis": "x", **params})
            assert ok.success, ok.error

    def test_unreadable_state_or_unknown_value_refuses(self, stages):
        from mast.skills.builtins.optics_stage import OpticalStageMove

        stages(_stage(tip_requirement="z_feedback_on"))
        res = OpticalStageMove().execute(_Ctx(state=False),
                                         {"device_id": "st", "axis": "x", "position": 5.0})
        assert not res.success and "读不到" in res.error
        stages(_stage(tip_requirement="withdrawn"))
        res = OpticalStageMove().execute(_Ctx(), {"device_id": "st", "axis": "x",
                                                  "position": 5.0})
        assert not res.success and "不认识" in res.error

    def test_z_feedback_requirement(self, stages):
        from mast.skills.builtins.optics_stage import OpticalStageMove

        stages(_stage(tip_requirement="z_feedback_on"))
        p = {"device_id": "st", "axis": "x", "position": 5.0}
        assert not OpticalStageMove().execute(_Ctx(z_on=False), p).success
        assert OpticalStageMove().execute(_Ctx(z_on=True), p).success
        assert not OpticalStageMove().execute(_Ctx(z_on=True, scan=True), p).success

    @pytest.mark.parametrize("skill,module,dev_type,role,params", [
        ("MoveXYZStage", "optics_alignment", "satellite_motion", "focus_xyz", {"x": 1.0}),
        ("PicomotorJog", "optics_alignment", "satellite_openloop", "beam_steer",
         {"axis": "x", "steps": 10}),
        ("SetPolarizerAngle", "optics_polarization", "satellite_rotation", "polarizer",
         {"angle_deg": 10.0}),
        ("HomePolarizer", "optics_polarization", "satellite_rotation", "polarizer", {}),
    ])
    def test_satellite_motion_skills_check_the_same_field(self, stages, skill, module,
                                                          dev_type, role, params):
        """The hub's satellite cards show ``tip_requirement`` from the same entry,
        so the skills that move those devices must enforce it too."""
        import importlib

        entry = {"id": "sat", "type": dev_type, "enabled": True, "satellite": "s1",
                 "device": "d", "role": role, "tip_requirement": "tip_withdrawn",
                 "max_steps_per_cmd": 100,
                 "axes": [{"name": "x", "channel": 1, "min_pos": -100, "max_pos": 100,
                           "role": role}]}
        stages(entry)
        cls = getattr(importlib.import_module(f"mast.skills.builtins.{module}"), skill)
        res = cls().execute(_Ctx(withdrawn=False), dict(params))
        assert not res.success and "tip_withdrawn" in res.error, res.error
        for c in stages.built.values():
            assert all(not ax.moves for ax in c.open_axes().values())

    def test_delay_line_and_scans_check_their_devices(self, stages):
        from mast.skills.builtins.optics_pump_probe import PumpProbeScan
        from mast.skills.builtins.optics_scan import OpticalStageScan
        from mast.skills.builtins.optics_stage import DelayLineMoveTo

        stages(_stage(tip_requirement="tip_withdrawn"),
               delay_line={"device_id": "st", "axis": "x", "unit_per_mm": 1000.0})
        ctx = _Ctx(withdrawn=False)
        res = DelayLineMoveTo().execute(ctx, {"delay_ps": 0.1})
        assert not res.success and "tip_withdrawn" in res.error
        res = OpticalStageScan().execute(ctx, {"device1": "st", "axis1": "x", "start1": 0.0,
                                               "stop1": 10.0, "points1": 3, "dry_run": True})
        assert not res.success and "tip_withdrawn" in res.error
        res = PumpProbeScan().execute(ctx, {"delay_start_ps": 0.0, "delay_stop_ps": 0.1,
                                            "points": 2, "dry_run": True})
        assert not res.success and "tip_withdrawn" in res.error, res.error
        for c in stages.built.values():                  # refused before any motion
            assert all(not ax.moves for ax in c.open_axes().values())


class TestReadouts:
    """``local_manual_readouts`` — the batch read behind GET /api/manual/readouts."""

    def test_a_closed_stage_is_reported_closed_and_stays_closed(self, stages):
        from mast.instruments.manual import local_manual_readouts

        reg = stages(_stage(), _stage("off", enabled=False))
        out = local_manual_readouts(["local/stage/st", "local/stage/off", "local/stage/nope",
                                     "s1/pol"])
        assert set(out) == {"local/stage/st", "local/stage/off", "local/stage/nope"}
        st = out["local/stage/st"]
        assert st["error"] is None and st["axes"] == [
            {"name": "x", "position": None, "moving": None, "homed": None,
             "extra": {"connected": False}}]
        assert out["local/stage/off"]["error"] == "未启用"
        assert "nope" in out["local/stage/nope"]["error"]
        assert stages.built == {} and reg.built_controllers() == {}

    def test_an_open_stage_is_read_and_a_released_one_is_not_reopened(self, stages):
        from mast.instruments.manual import local_manual_readouts
        from mast.skills.builtins.optics_stage import (
            DisconnectOpticalDevice,
            GetOpticalStageStatus,
        )

        stages(_stage())
        assert GetOpticalStageStatus().execute(_Ctx(), {"device_id": "st",
                                                        "connect": True}).success
        ctl = stages.built["st"]
        st = local_manual_readouts(["local/stage/st"])["local/stage/st"]
        assert st["error"] is None and st["axes"][0]["position"] == 1.0
        assert st["axes"][0]["extra"]["connected"] is True and st["axes"][0]["extra"]["servo"]
        assert DisconnectOpticalDevice().execute(_Ctx(), {"device_id": "st"}).success
        st = local_manual_readouts(["local/stage/st"])["local/stage/st"]
        assert st["axes"][0]["extra"] == {"connected": False}
        assert ctl.connects == 1                          # not reopened by the readout

    def test_single_id_form(self, stages):
        from mast.instruments.manual import local_readout

        stages(_stage())
        one = local_readout("local/stage/st")
        assert one["error"] is None and one["axes"][0]["extra"] == {"connected": False}
        other = local_readout("s1/pol")
        assert other["axes"] == [] and "不是本机" in other["error"]
        assert stages.built == {}

    def test_a_group_reads_its_odometer_without_nanonis(self, setup):
        from mast.instruments.manual import local_manual_readouts

        mg.note_steps(1, "Y", -12)
        out = local_manual_readouts(["local/nanonis_group/1", "local/nanonis_group/7"])
        g = out["local/nanonis_group/1"]
        assert g["error"] is None
        y = next(a for a in g["axes"] if a["name"] == "mirror_y")
        assert y["extra"]["odometer_steps"] == -12 and y["position"] is None
        assert "7" in out["local/nanonis_group/7"]["error"]


# ── nothing that only READS ever opens a device ─────────────────
#
# Real drivers (PZTC over Modbus, PI over GCS, PDXC2 over the Kinesis DLL) on
# doubles that count every port / DLL open. The e2e symptom — a stage MAST had
# released showing ``connected: true`` again a minute later — came from read
# paths that connected lazily: GET /api/optics/position and /delay, and the
# inventory's delay-line binding (polled every 15 s by the optics page).


class _ModbusPort:
    """PZTC transport: answers every read with zeros, echoes every write."""

    def __init__(self, opens):
        self.opens = opens

    def open(self):
        self.opens["pztc"] += 1

    def transact(self, payload, *, read_size=None, read_until=None, timeout=None):
        from mast.instruments.pztc_nm003 import build_rtu_frame

        f = bytes(payload)
        func = f[1]
        if func in (0x03, 0x04):
            n = int.from_bytes(f[4:6], "big")
            return build_rtu_frame(1, func, bytes([2 * n]) + b"\x00\x00" * n)
        if func in (0x01, 0x02):
            return build_rtu_frame(1, func, bytes([1, 0]))
        if func == 0x10:
            return build_rtu_frame(1, 0x10, f[2:6])
        return f

    def close(self):
        pass


class _GcsPort:
    """PI transport: ERR? → 0, every query → a plausible value, commands silent."""

    _VALUES = {"POS?": "1.0", "ONT?": "1", "FRF?": "1", "SVO?": "1", "VEL?": "0.5",
               "TMN?": "0", "TMX?": "13", "LIM?": "1", "TRS?": "1"}

    def __init__(self, opens):
        self.opens = opens

    def open(self):
        self.opens["pi"] += 1

    def transact(self, payload, *, read_size=None, read_until=None, timeout=None):
        line = bytes(payload).decode("ascii").strip()
        if line == "ERR?":
            return b"0\n"
        cmd, _, ax = line.partition(" ")
        if cmd.endswith("?"):
            return f"{ax}={self._VALUES.get(cmd, '0')}\n".encode("ascii")
        return b""

    def close(self):
        pass


class _KinesisLib:
    """Kinesis C API double; counts PDXC2_Open."""

    def __init__(self, opens):
        self.opens = opens
        self.mode = 3

    def TLI_BuildDeviceList(self):
        return 0

    def PDXC2_Open(self, s):
        self.opens["pdxc2"] += 1
        return 0

    def PDXC2_StartPolling(self, s, ms):
        return True

    def PDXC2_Enable(self, s):
        return 0

    def PDXC2_SetPositionControlMode(self, s, mode):
        return 0

    def PDXC2_SetExternalTriggerConfig(self, s, mode):
        self.mode = int(mode)
        return 0

    def PDXC2_RequestExternalTriggerConfig(self, s):
        return 0

    def PDXC2_GetExternalTriggerConfig(self, s):
        return self.mode

    def PDXC2_RequestPosition(self, s):
        return 0

    def PDXC2_GetPosition(self, s, position):
        position._obj.value = 0
        return 0

    def PDXC2_GetStatusBits(self, s):
        return 0

    def PDXC2_GetPositionControlMode(self, s):
        return 2

    def PDXC2_MoveStop(self, s):
        return 0

    def PDXC2_StopPolling(self, s):
        pass

    def PDXC2_Disable(self, s):
        return 0

    def PDXC2_Close(self, s):
        pass


REAL_BENCH = [
    {"id": "zs", "type": "pztc_nm003", "enabled": True, "port": "COM13",
     "counts_per_unit": 1000.0,
     "axes": [{"name": "delay", "channel": 0, "min_pos": 0, "max_pos": 10000, "unit": "um"}]},
    {"id": "pi", "type": "pi_gcs", "enabled": True, "port": "COM17", "model": "E-861",
     "servo_on_connect": False,
     "axes": [{"name": "s2", "channel": "1", "min_pos": 0, "max_pos": 13, "unit": "mm"}]},
    {"id": "pd", "type": "thorlabs_pdxc2", "enabled": True, "serial_no": "112000001",
     "counts_per_unit": 1e6,
     "axes": [{"name": "s1", "channel": 1, "min_pos": 0, "max_pos": 20, "unit": "mm"}]},
]


@pytest.fixture
def real_bench(tmp_path, monkeypatch):
    """The three real driver types on counting doubles, as the process-wide
    registry, with the delay line bound to the PZTC stage."""
    from mast.instruments import pi_gcs, pztc_nm003, thorlabs_pdxc2

    opens = {"pztc": 0, "pi": 0, "pdxc2": 0}
    monkeypatch.setattr(pztc_nm003, "open_transport", lambda settings: _ModbusPort(opens))
    monkeypatch.setattr(pi_gcs, "open_transport", lambda settings: _GcsPort(opens))
    monkeypatch.setattr(thorlabs_pdxc2, "_load_pdxc2_dll", lambda d: _KinesisLib(opens))
    gp = tmp_path / mg.CONFIG_FILENAME
    gp.write_text(json.dumps(GROUPS), encoding="utf-8")
    monkeypatch.setattr(mg, "config_path", lambda: gp)
    mg.reset_for_tests()
    p = tmp_path / "oi.json"
    p.write_text(json.dumps({"devices": REAL_BENCH, "delay_line": {
        "device_id": "zs", "axis": "delay", "unit_per_mm": 1000.0}}), encoding="utf-8")
    reg = InstrumentRegistry(p)
    reset_instrument_registry(reg)
    yield reg, opens
    reset_instrument_registry(None)
    mg.reset_for_tests()


def _read_everything(reg, skill_registry, rounds=3):
    """Every read the panel and the optics page make, ``rounds`` times over; returns
    the readouts of the last round."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mast.api.routes.optics import router
    from mast.instruments.manual import local_manual_readouts
    from mast.skills.builtins.optics_stage import GetOpticalStageStatus, ListOpticalDevices

    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    ids = [f"local/stage/{e['id']}" for e in REAL_BENCH]
    out = {}
    for _ in range(rounds):
        local_manual_devices(instruments=reg, skill_registry=skill_registry, processes=[])
        out = local_manual_readouts(ids, instruments=reg)
        assert ListOpticalDevices().execute(_Ctx(), {}).success
        assert client.get("/api/optics/devices").status_code == 200
        assert client.get("/api/optics/delay").json()["ok"]
        for e in REAL_BENCH:
            st = GetOpticalStageStatus().execute(_Ctx(), {"device_id": e["id"]})
            assert st.success, st.error
            body = client.get("/api/optics/position", params={
                "device_id": e["id"], "axis": e["axes"][0]["name"]}).json()
            assert body["ok"], body
    return out


class TestNothingReopensADevice:
    def test_a_fresh_start_opens_nothing(self, real_bench, skill_registry):
        reg, opens = real_bench
        out = _read_everything(reg, skill_registry)
        assert opens == {"pztc": 0, "pi": 0, "pdxc2": 0}
        assert all(ax["extra"] == {"connected": False}
                   for r in out.values() for ax in r["axes"])
        assert reg.built_controllers() == {} or not any(
            c.is_connected for c in reg.built_controllers().values())
        # the inventory still reports the delay binding — from config, not the stage
        info = _inventory_delay_line()
        assert (info["device_id"], info["axis"]) == ("zs", "delay")
        assert info["delay_range_ps"][0] < info["delay_range_ps"][1]
        assert opens == {"pztc": 0, "pi": 0, "pdxc2": 0}

    def test_a_released_device_stays_released(self, real_bench, skill_registry):
        from mast.skills.builtins.optics_stage import (
            DisconnectOpticalDevice,
            GetOpticalStageStatus,
        )

        reg, opens = real_bench
        for e in REAL_BENCH:                            # the operator presses 「连接」
            res = GetOpticalStageStatus().execute(_Ctx(), {"device_id": e["id"],
                                                           "connect": True})
            assert res.success and res.data["connected"], res.error
        assert opens == {"pztc": 1, "pi": 1, "pdxc2": 1}
        out = _read_everything(reg, skill_registry, rounds=1)
        assert all(ax["extra"]["connected"] for r in out.values() for ax in r["axes"])
        assert opens == {"pztc": 1, "pi": 1, "pdxc2": 1}  # reading an open device opens nothing
        for e in REAL_BENCH:                            # … and then 「断开」
            assert DisconnectOpticalDevice().execute(_Ctx(), {"device_id": e["id"]}).success
        out = _read_everything(reg, skill_registry)
        assert opens == {"pztc": 1, "pi": 1, "pdxc2": 1}
        assert all(ax["extra"] == {"connected": False}
                   for r in out.values() for ax in r["axes"])
        assert not any(c.is_connected for c in reg.built_controllers().values())

    def test_simulated_stages_do_not_open_on_construction(self):
        """Frontend's e2e guess was that the simulators open when constructed;
        they do not — the reopening came from the read paths above."""
        from mast.instruments.registry import _build_controller

        for entry in ({"id": "r", "type": "sim_rotation",
                       "axes": [{"name": "a", "channel": 1, "min_pos": -360, "max_pos": 720,
                                 "unit": "deg"}]},
                      {"id": "o", "type": "sim_openloop", "max_steps_per_cmd": 100,
                       "axes": [{"name": "1", "channel": 1, "min_pos": -100, "max_pos": 100,
                                 "unit": "steps"}]}):
            assert _build_controller(entry).is_connected is False


def _inventory_delay_line():
    from mast.skills.builtins.optics_stage import ListOpticalDevices

    return ListOpticalDevices().execute(_Ctx(), {}).data["delay_line"]
