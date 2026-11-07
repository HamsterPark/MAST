"""mast.vista actions: parameter validation and ActionRunner against a fake ExecutionContext.

The fake records every skill call and returns canned results; no MAST runtime, no controller.
What is pinned: the unit conversions the model's vocabulary (nm, V, pA, s) maps onto, the skill
sequence of each action, that a failed or refused step stops the action and says which, and that
nothing raises into the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from mast.vista.actions import (ACTIONS, GRID_PHRASE, OBSERVATION_PHRASE, ActionError, ActionRunner,
                                act_description, act_schema, validate_params)


@dataclass
class FakeResult:
    success: bool = True
    error: str = ""
    data: dict = field(default_factory=dict)


class FakeCtx:
    """Records skill calls. ``ScanAt`` adds a saved frame to the session listing when ``autosave``,
    ``SaveScan`` always does; ``fail`` makes a skill refuse (with ``data`` of that skill, as a
    failed composite carries its progress), ``raise_on`` makes it raise, ``data`` is what a
    skill returns."""

    def __init__(self, autosave: bool = True):
        self.autosave = autosave
        self.calls: list[tuple[str, dict]] = []
        self.files: set[str] = set()
        self.fail: dict[str, str] = {}
        self.raise_on: dict[str, Exception] = {}
        self.data: dict[str, dict] = {}

    def run(self, name, params):
        self.calls.append((name, dict(params)))
        if name in self.raise_on:
            raise self.raise_on[name]
        if name in self.fail:
            return FakeResult(False, self.fail[name], data=dict(self.data.get(name, {})))
        if name == "SaveScan" or (name == "ScanAt" and self.autosave):
            self.files.add(f"frame_{len(self.files) + 1:03d}.sxm")
        return FakeResult(True, data=dict(self.data.get(name, {})))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def params(self, name: str) -> dict:
        return next(p for n, p in self.calls if n == name)


def resolve(spec: dict) -> tuple[float, float, dict]:
    """A host's point resolution: ``{x_nm, y_nm}`` as given; ``{frame, x, y}`` of the one known frame
    ``t0001.s0`` at 0.1 nm per display pixel; an unknown frame raises like ``Archive.get``."""
    if "frame" in spec:
        if spec["frame"] != "t0001.s0":
            raise KeyError(f"no frame {spec['frame']!r}")
        return spec["x"] * 0.1, spec["y"] * 0.1, {"frame": spec["frame"], "x": spec["x"], "y": spec["y"]}
    return float(spec["x_nm"]), float(spec["y_nm"]), {"x_nm": spec["x_nm"], "y_nm": spec["y_nm"]}


def make_runner(ctx: FakeCtx | None = None, **kw) -> tuple[ActionRunner, FakeCtx]:
    ctx = ctx or FakeCtx()
    kw.setdefault("time_scale", 1.0)
    return ActionRunner(ctx, resolve_point=resolve, list_files=lambda: set(ctx.files), **kw), ctx


def act(runner: ActionRunner, name: str, params: dict):
    return runner.run(ACTIONS[name], validate_params(ACTIONS[name], params))


# ── validate_params ─────────────────────────────────────────────────────────
def test_validate_params_ranges_and_defaults():
    spec = ACTIONS["sts"]
    out = validate_params(spec, {"start_v": -0.5, "end_v": 0.5})
    assert out["num_points"] == 256
    with pytest.raises(ActionError):
        validate_params(spec, {"start_v": -9, "end_v": 0.5})
    with pytest.raises(ActionError):
        validate_params(ACTIONS["scan"], {"center": {"x_nm": 0, "y_nm": 0}, "size_nm": 10, "pixels": 12.5})


def test_validate_params_types_and_messages():
    scan = ACTIONS["scan"]
    out = validate_params(scan, {"center": {"x_nm": 1, "y_nm": 2}, "size_nm": 50, "pixels": 256.0, "bias_v": None})
    assert out == {"center": {"x_nm": 1, "y_nm": 2}, "size_nm": 50.0, "pixels": 256}
    assert isinstance(out["size_nm"], float) and isinstance(out["pixels"], int)
    for params, needle in (
            ({"center": {"x_nm": 0, "y_nm": 0}, "size_nm": 50, "zoom": 2}, "unknown parameter(s) ['zoom']"),
            ({"size_nm": 50}, "missing required parameter 'center'"),
            ({"center": None, "size_nm": 50}, "missing required parameter 'center'"),
            ({"center": [0, 0], "size_nm": 50}, "center must be an object"),
            ({"center": {"x_nm": 0, "y_nm": 0}, "size_nm": True}, "size_nm must be a finite number"),
            ({"center": {"x_nm": 0, "y_nm": 0}, "size_nm": float("nan")}, "size_nm must be a finite number"),
            ({"center": {"x_nm": 0, "y_nm": 0}, "size_nm": "50"}, "size_nm must be a finite number"),
            ({"center": {"x_nm": 0, "y_nm": 0}, "size_nm": 0.1}, "size_nm=0.1 is below the minimum 0.5"),
            ({"center": {"x_nm": 0, "y_nm": 0}, "size_nm": 5000}, "size_nm=5000 is above the maximum 1500"),
            ({"center": {"x_nm": 0, "y_nm": 0}, "size_nm": 50, "pixels": 12.5}, "pixels must be an integer")):
        with pytest.raises(ActionError) as exc:
            validate_params(scan, params)
        assert needle in str(exc.value) and str(exc.value).startswith("scan: "), (params, str(exc.value))
    with pytest.raises(ActionError, match="on must be true or false"):
        validate_params(ACTIONS["feedback"], {"on": 1})
    with pytest.raises(ActionError, match="params must be an object"):
        validate_params(ACTIONS["withdraw"], ["now"])
    assert validate_params(ACTIONS["withdraw"], None) == {}
    with pytest.raises(ActionError, match="expected none"):
        validate_params(ACTIONS["withdraw"], {"fast": True})


def test_action_table_and_act_description():
    names = list(ACTIONS)
    for spec in ACTIONS.values():
        params = [p.name for p in spec.params]
        assert len(params) == len(set(params)), spec.name
        line = spec.describe()
        assert line.startswith(f"- `{spec.name}` (") and all(p in line for p in params)
        for p in spec.params:
            s = p.schema()
            assert ("oneOf" in s) == (p.kind == "point")
    schema = act_schema(names)
    assert schema["properties"]["action"]["enum"] == names and schema["additionalProperties"] is False
    assert schema["required"] == ["action", "params"]
    desc = act_description(["scan", "wait"])
    assert ACTIONS["scan"].describe() in desc and ACTIONS["wait"].describe() in desc
    assert ACTIONS["withdraw"].describe() not in desc
    assert OBSERVATION_PHRASE["image"] in desc and GRID_PHRASE not in desc
    assert GRID_PHRASE in act_description(["scan"], grid=True)
    assert GRID_PHRASE not in act_description(["scan"], observation="text", grid=True)
    assert act_description(["scan"], observation="nonsense") == act_description(["scan"])


# ── ActionRunner ────────────────────────────────────────────────────────────
def test_scan_converts_units_and_saves_only_without_autosave():
    runner, ctx = make_runner()
    ctx.data["ScanAt"] = {"line_time_s": 0.5, "requested_pixels": 256, "unrelated": 1}
    out = act(runner, "scan", {"center": {"x_nm": 12.5, "y_nm": -4.0}, "size_nm": 50, "pixels": 256,
                               "current_pa": 20, "bias_v": 0.5, "angle_deg": 30, "line_time_s": 0.4})
    assert out.ok and out.error is None and ctx.names() == ["ScanAt"]      # autosaved: no SaveScan
    assert ctx.params("ScanAt") == pytest.approx({"center_x_m": 12.5e-9, "center_y_m": -4e-9, "size_m": 50e-9,
                                                  "pixels": 256, "angle_deg": 30.0, "line_time_s": 0.4,
                                                  "bias_v": 0.5, "setpoint_a": 20e-12})
    assert out.facts == {"line_time_s": 0.5, "requested_pixels": 256}
    assert out.resolved == {"center_nm": [12.5, -4.0], "center_from": {"x_nm": 12.5, "y_nm": -4.0}}
    assert out.steps[0]["skill"] == "ScanAt" and out.steps[0]["success"] is True
    runner, ctx = make_runner(FakeCtx(autosave=False))
    out = act(runner, "scan", {"center": {"frame": "t0001.s0", "x": 100, "y": 50}, "size_nm": 20})
    assert out.ok and ctx.names() == ["ScanAt", "SaveScan"]
    assert set(ctx.params("ScanAt")) == {"center_x_m", "center_y_m", "size_m"}   # the rest: the scan policy's
    assert out.resolved["center_nm"] == pytest.approx([10.0, 5.0])


def test_failed_step_stops_the_action_and_says_which():
    runner, ctx = make_runner()
    ctx.fail["MoveToXY"] = "refused by SafetyGate"
    out = act(runner, "pulse", {"bias_v": 2.0, "duration_s": 0.05, "at": {"x_nm": 1, "y_nm": 1}})
    assert not out.ok and out.error == "MoveToXY failed: refused by SafetyGate"
    assert ctx.names() == ["MoveToXY"]                                     # the pulse never fired
    assert out.steps == [{"skill": "MoveToXY", "params": {"x_m": 1e-9, "y_m": 1e-9, "wait": True},
                          "success": False, "error": "refused by SafetyGate"}]
    ctx.fail.clear()
    ctx.data["GetBias"] = {"bias_v": 0.25}
    out = act(runner, "set_bias", {"bias_v": 0.2})
    assert out.ok and [s["skill"] for s in out.steps] == ["GetBias", "SetBias"]   # steps are per action


def test_action_runner_never_raises():
    class Boom:
        def run(self, name, params):
            raise RuntimeError("socket closed")
    r = ActionRunner(Boom(), resolve_point=lambda s: (0.0, 0.0, {}), time_scale=1.0)
    out = r.run(ACTIONS["set_bias"], {"bias_v": 0.1})
    assert out.ok is False and "socket closed" in out.error
    runner, _ = make_runner()
    out = runner.run(ACTIONS["scan"], {"center": {"x_nm": 0, "y_nm": 0}, "size_nm": "big"})   # unvalidated
    assert not out.ok and out.error.startswith("internal error: ValueError")


def test_point_resolution_errors_are_the_models_to_fix():
    runner, ctx = make_runner()
    out = act(runner, "move_tip", {"to": {"frame": "t0009.s0", "x": 1, "y": 2}})
    assert not out.ok and out.error.startswith("to: ") and "t0009.s0" in out.error and ctx.calls == []

    def refuse(spec):
        raise ActionError("t0002.d0 is not a scan frame")
    r = ActionRunner(ctx, resolve_point=refuse, time_scale=1.0)
    out = r.run(ACTIONS["move_atom"], validate_params(ACTIONS["move_atom"], {
        "atom": {"frame": "t0002.d0", "x": 1, "y": 1}, "target": {"x_nm": 0, "y_nm": 0}}))
    assert out.error == "t0002.d0 is not a scan frame" and ctx.calls == []


def test_positioned_actions_move_first_and_report_facts_only():
    runner, ctx = make_runner()
    ctx.data["TipPulse"] = {"dz_m": 1e-10, "pulses": 2, "trace": [1, 2, 3], "verdict": None}
    out = act(runner, "pulse", {"bias_v": -3, "duration_s": 0.02, "count": 2,
                                "at": {"frame": "t0001.s0", "x": 30, "y": 40}})
    assert out.ok and ctx.names() == ["MoveToXY", "TipPulse"]
    assert ctx.params("MoveToXY") == pytest.approx({"x_m": 3e-9, "y_m": 4e-9, "wait": True})
    assert ctx.params("TipPulse") == {"pulse_v": -3.0, "duration_s": 0.02, "count": 2}
    assert out.facts == {"dz_m": 1e-10, "pulses": 2}
    assert out.resolved == {"at_nm": pytest.approx([3.0, 4.0]), "at_from": {"frame": "t0001.s0", "x": 30, "y": 40}}
    runner, ctx = make_runner()
    assert act(runner, "move_tip", {"to": {"x_nm": -2, "y_nm": 7}}).resolved["to_nm"] == [-2.0, 7.0]
    assert ctx.params("MoveToXY") == pytest.approx({"x_m": -2e-9, "y_m": 7e-9, "wait": True})


def test_poke_and_spectroscopy_arguments():
    runner, ctx = make_runner()
    assert act(runner, "poke", {"depth_nm": 0.3, "bias_v": 0.05, "dwell_s": 0.2}).ok
    assert ctx.params("TipShape") == pytest.approx({"tip_lift_m": -3e-10, "lift_height_m": 3e-10,
                                                    "restore_feedback": True, "change_bias": True,
                                                    "bias_v": 0.05, "lift_time_1_s": 0.2})
    runner, ctx = make_runner()
    assert act(runner, "sts", {"start_v": -1, "end_v": 1, "z_offset_nm": -0.1}).ok
    assert ctx.names() == ["ConfigureSTS", "AcquireSTS"]
    assert ctx.params("ConfigureSTS") == pytest.approx({"start_v": -1.0, "end_v": 1.0, "num_points": 256,
                                                        "z_offset_m": -1e-10})


def test_qplus_only_actions_refuse_on_an_stm_tip():
    runner, ctx = make_runner()
    out = act(runner, "df_curve", {"z_sweep_nm": 0.5})
    assert not out.ok and "qPlus" in out.error and ctx.calls == []
    runner, ctx = make_runner(is_qplus=True)
    ctx.data["AcquireDeltaFCurve"] = {"f0_hz": 30300.0, "df_rest_hz": -1.2, "curve": [1, 2]}
    out = act(runner, "df_curve", {"z_sweep_nm": 0.5, "z_offset_nm": 0.2})
    assert out.ok and out.facts == {"f0_hz": 30300.0, "df_rest_hz": -1.2}
    assert ctx.params("AcquireDeltaFCurve") == pytest.approx({"z_sweep_distance_m": 5e-10, "num_points": 256,
                                                              "z_offset_m": 2e-10})
    ctx.data["GetPLLStatus"] = {"center_freq_hz": 30300.0, "output_on": True, "note": "locked"}
    ctx.data["ReadTipOscillationAmplitude"] = {"amplitude": 5e-11}
    out = act(runner, "pll", {"on": True, "amplitude_pm": 50, "auto_center": True})
    assert out.ok and out.facts == {"center_freq_hz": 30300.0, "output_on": True, "amplitude_pm": pytest.approx(50.0)}
    assert ctx.names()[-5:] == ["SetPLLAmpCtrlSetpnt", "PLLOnOff", "PLLFreqShiftAutoCenter", "GetPLLStatus",
                                "ReadTipOscillationAmplitude"]


def test_feedback_keeps_omitted_gains():
    runner, ctx = make_runner()
    ctx.data["GetZCtrlGain"] = {"p_gain": 3e-12, "i_gain": 5e-8, "time_constant_s": 6e-5}
    assert act(runner, "feedback", {"p_gain_pm": 5}).ok
    assert ctx.params("SetZCtrlGain") == pytest.approx({"p_gain": 5e-12, "i_gain": 5e-8, "time_constant_s": 6e-5})
    assert "ZControllerOnOff" not in ctx.names()
    runner, ctx = make_runner()
    ctx.fail["GetZCtrlGain"] = "not connected"
    out = act(runner, "feedback", {"i_gain_nm_s": 40, "on": True})
    assert not out.ok and "could not be read" in out.error and "SetZCtrlGain" not in ctx.names()
    runner, ctx = make_runner()
    assert act(runner, "feedback", {"on": False}).ok and ctx.calls == [("ZControllerOnOff", {"enable": False})]
    out = act(runner, "feedback", {})
    assert not out.ok and out.error.startswith("feedback: give `on`")


def test_move_atom_switches_the_skills_own_verdict_off():
    runner, ctx = make_runner()
    ctx.data["MoveAtomTo"] = {"junction_resistance_ohm": 250000.0, "instrument_restored": True, "moved": True}
    out = act(runner, "move_atom", {"atom": {"x_nm": 1, "y_nm": 2}, "target": {"frame": "t0001.s0", "x": 30, "y": 20},
                                    "current_pa": 50000, "bias_v": 0.02, "speed_nm_s": 1.0, "approach_offset_nm": 0.1})
    assert out.ok
    assert ctx.params("MoveAtomTo") == pytest.approx({
        "atom_x_m": 1e-9, "atom_y_m": 2e-9, "target_x_m": 3e-9, "target_y_m": 2e-9, "verify": False,
        "max_attempts": 1, "manip_setpoint_a": 5e-8, "manip_bias_v": 0.02, "manip_speed_m_s": 1e-9,
        "approach_offset_m": 1e-10})
    assert out.facts == {"junction_resistance_kohm": 250.0, "instrument_restored": True}   # no "moved" verdict
    assert out.resolved["target_nm"] == pytest.approx([3.0, 2.0]) and out.resolved["atom_nm"] == [1.0, 2.0]


def test_wait_sleeps_instrument_time_over_the_time_scale():
    slept: list[float] = []
    runner, ctx = make_runner(time_scale=20.0, sleep=slept.append)
    out = act(runner, "wait", {"seconds": 60})
    assert out.ok and out.facts == {"waited_s": 60.0} and slept == [3.0] and ctx.calls == []


def test_drift_compensation_withdraw_and_approach():
    runner, ctx = make_runner()
    assert act(runner, "drift_compensation", {"enable": True, "vx_nm_s": 0.02, "vz_nm_s": -0.01}).ok
    assert ctx.params("SetDriftCompensation") == pytest.approx({"enable": True, "vx": 2e-11, "vz": -1e-11})
    assert act(runner, "withdraw", {}).ok and act(runner, "approach", {}).ok
    assert ctx.calls[-2:] == [("WithdrawTip", {}), ("ApproachTip", {})]


# ── 7.0.1: the safe paths MAST itself prefers ──────────────────────────────
def test_approach_is_approach_tip_and_reports_how_it_went():
    """``approach`` used to call AutoApproach(wait_timeout_s=300): no feedback-only first try, and
    a budget AutoApproach's own parameter text warns against. ApproachTip engages the feedback
    first and falls back to AutoApproach with that skill's own budget."""
    runner, ctx = make_runner()
    ctx.data["ApproachTip"] = {"engaged": True, "method": "engage_controller", "auto_approach_used": False,
                               "peak_current_a": 5.0003e-11, "setpoint_a": 5e-11,
                               "qplus_note": "noise for an STM", "steps": [{"skill": "TryEngageController"}]}
    out = act(runner, "approach", {})
    assert out.ok and ctx.calls == [("ApproachTip", {})]
    assert out.facts == {"method": "engage_controller", "engaged": True, "auto_approach_used": False,
                         "peak_current_pa": pytest.approx(50.003), "setpoint_pa": pytest.approx(50.0)}
    ctx.fail["ApproachTip"] = "engage phase failed: feedback state unknown"
    out = act(runner, "approach", {})
    assert not out.ok and out.error == "ApproachTip failed: engage phase failed: feedback state unknown"
    assert "AutoApproach" not in ctx.names()


@pytest.mark.parametrize("start, target, path", [
    (0.5, 0.4, "SetBias"),               # small, same sign: direct
    (-1.2, -1.0, "SetBias"),
    (0.5, -0.5, "BiasSettleChange"),     # crosses zero
    (2.0, -2.0, "BiasSettleChange"),
    (0.2, 1.5, "BiasSettleChange"),      # larger than the ramp threshold
    (0.0, 0.3, "SetBias"),               # from exactly 0 is not a crossing (BiasSettleChange's rule)
    (0.3, 0.0, "BiasSettleChange"),      # a target inside the dead band, however small the change
    (0.1, 0.02, "BiasSettleChange"),
    (-0.1, -0.04, "BiasSettleChange"),
    (0.03, 0.02, "BiasSettleChange"),    # already inside: still the path that refuses with the feedback on
    (0.1, 0.05, "SetBias"),              # the band is open at 50 mV, as BiasSettleChange's own test is
])
def test_set_bias_goes_the_safe_way_across_zero_and_for_large_changes(start, target, path):
    runner, ctx = make_runner()
    ctx.data["GetBias"] = {"bias_v": start}
    ctx.data["BiasSettleChange"] = {"bias_v_start": start, "bias_v": target, "strategy": "ramp_through_zero",
                                    "crossed_zero": True, "settle_s": 2.0, "feedback_on": True}
    out = act(runner, "set_bias", {"bias_v": target})
    assert out.ok and ctx.names() == ["GetBias", path]
    assert ctx.params(path) == {"bias_v": target}
    assert out.facts["path"] == path
    if path == "SetBias":
        assert out.facts == {"path": "SetBias", "bias_v_start": start}
    else:
        assert out.facts == {"path": "BiasSettleChange", "bias_v_start": start,
                             "strategy": "ramp_through_zero", "crossed_zero": True, "settle_s": 2.0}


def test_set_bias_with_an_unreadable_start_and_a_refusal_never_falls_back_to_set_bias():
    runner, ctx = make_runner()
    ctx.fail["GetBias"] = "not connected"
    ctx.fail["BiasSettleChange"] = "目标偏压 0.01 V 落在低偏压死区内"
    out = act(runner, "set_bias", {"bias_v": 0.01})
    assert not out.ok and out.error == "BiasSettleChange failed: 目标偏压 0.01 V 落在低偏压死区内"
    assert ctx.names() == ["GetBias", "BiasSettleChange"]      # the refusal stands: no SetBias after it


def test_set_bias_routing_and_description_follow_bias_settle(monkeypatch):
    import mast.skills.composite.bias_settle as BS
    from mast.vista import actions as A

    assert (A.BIAS_RAMP_THRESHOLD_V, A.BIAS_DEADBAND_V) == (BS.RAMP_THRESHOLD_V, BS.ZERO_DEADBAND_V)
    line = ACTIONS["set_bias"].describe()
    assert "0.5 V" in line and "50 mV" in line and "BiasSettleChange" in line
    monkeypatch.setattr(BS, "RAMP_THRESHOLD_V", 2.0)            # the routing reads the skill's constant
    runner, ctx = make_runner()
    ctx.data["GetBias"] = {"bias_v": 0.2}
    assert act(runner, "set_bias", {"bias_v": 1.5}).ok and ctx.names() == ["GetBias", "SetBias"]


# ── 7.0.1: a stopped scan, and a running scan's plan ───────────────────────
def _stopped_scan(ctx, completed, *, error="WaitScanComplete failed: aborted by user"):
    ctx.autosave = False
    ctx.fail["ScanAt"] = error
    ctx.data["ScanAt"] = {"line_time_s": 0.5, "requested_pixels": 128, "estimated_scan_s": 128.0,
                          "scan_lines_done": 21, "scan_lines_total": 128,
                          "_progress": {"completed_steps": completed, "failed_steps": ["wait_scan"]}}


def test_a_scan_that_stopped_after_starting_asks_the_host_to_save_its_rows():
    runner, ctx = make_runner()
    _stopped_scan(ctx, ["configure", "set_buffer", "start_scan"])
    out = act(runner, "scan", {"center": {"x_nm": 10, "y_nm": 10}, "size_nm": 30, "pixels": 128})
    assert not out.ok and out.error == "ScanAt failed: WaitScanComplete failed: aborted by user"
    assert out.salvage == {"skill": "SaveScan", "reason": "the scan stopped before the frame was complete"}
    assert ctx.names() == ["ScanAt"]                          # the runner does not save: the host does
    assert out.facts == {"line_time_s": 0.5, "requested_pixels": 128, "estimated_scan_s": 128.0,
                         "lines_done": 21, "lines_total": 128}
    assert out.resolved["center_nm"] == [10.0, 10.0]


def test_no_save_request_before_the_scan_started_or_when_the_controller_saved():
    runner, ctx = make_runner()
    _stopped_scan(ctx, ["configure"], error="ConfigureScan failed: precondition")
    out = act(runner, "scan", {"center": {"x_nm": 0, "y_nm": 0}, "size_nm": 30})
    assert not out.ok and out.salvage is None                  # the buffer holds the previous frame

    class SavesThenFails(FakeCtx):
        def run(self, name, params):
            res = super().run(name, params)
            if name == "ScanAt":
                self.files.add("partial_by_controller.sxm")
            return res
    ctx = SavesThenFails()
    runner, _ = make_runner(ctx)
    _stopped_scan(ctx, ["start_scan"])
    out = act(runner, "scan", {"center": {"x_nm": 0, "y_nm": 0}, "size_nm": 30})
    assert not out.ok and out.salvage is None                  # the controller saved them itself
    runner, ctx = make_runner()
    ctx.raise_on["ScanAt"] = RuntimeError("socket closed")
    out = act(runner, "scan", {"center": {"x_nm": 0, "y_nm": 0}, "size_nm": 30})
    assert out.error == "ScanAt failed: RuntimeError: socket closed" and out.salvage is None


def test_a_scan_reports_its_plan_before_it_runs():
    plans: list[dict] = []
    runner, ctx = make_runner(on_plan=plans.append)
    out = act(runner, "scan", {"center": {"x_nm": 0, "y_nm": -2}, "size_nm": 6, "pixels": 256,
                               "line_time_s": 0.2})
    assert out.ok and plans and plans[0] == out.plan
    scan = out.plan["scan"]
    assert (scan["pixels"], scan["line_time_s"]) == (256, 0.2)                 # explicit values win
    assert scan["estimated_scan_s"] == pytest.approx(256 * 0.2 * 2)            # ScanAt's own resolution
    out = act(runner, "set_bias", {"bias_v": 0.1})
    assert out.plan == {}                                                      # per action
    runner, _ = make_runner(on_plan=lambda plan: (_ for _ in ()).throw(RuntimeError("boom")))
    assert act(runner, "scan", {"center": {"x_nm": 0, "y_nm": 0}, "size_nm": 6}).ok   # reporting never stops it


def test_attempt_hands_back_a_failed_result():
    runner, ctx = make_runner()
    ctx.fail["GetZCtrlGain"] = "not connected"
    ctx.data["GetZCtrlGain"] = {"partial": 1}
    res, err = runner.attempt("GetZCtrlGain", {})
    assert err == "not connected" and res.data == {"partial": 1}
    ctx.raise_on["GetBias"] = RuntimeError("socket closed")
    res, err = runner.attempt("GetBias", {})
    assert res is None and err == "RuntimeError: socket closed"
    res, err = runner.attempt("WithdrawTip", {})
    assert res.success and err == ""
