"""V0 instrument actions: the only way the model changes the instrument.

Each action is a short, fixed sequence of MAST skills run through ``ExecutionContext`` (the
same safety stack every MAST entry point uses: parameter validation, SafetyGate, instrument
lock). Nothing here reads hidden instrument state; :mod:`mast.vista.service` only ever sees
what the controller returns and the files the instrument saved.

Conventions the model sees (and ``act``'s description states):

* distances in nm, bias in V, current in pA, time in s, speed in nm/s;
* a position is ``{"x_nm": .., "y_nm": ..}`` in scan-frame nm, or
  ``{"frame": "t0007.s0", "x": 412, "y": 233}`` in display pixels of an archived frame
  (converted with that frame's own geometry; the reply echoes both);
* an action returns facts (what the controller reported), never interpretations. Composite
  MAST skills that would judge their own outcome (``MoveAtomTo``'s verify scan) are run with
  that judgement switched off: the model checks the outcome itself, by imaging.

The action table is :data:`ACTIONS`; :class:`ActionRunner` executes one validated call.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

#: hard limits of the action interface (MAST and the rig enforce their own on top)
MAX_SCAN_NM = 1500.0
MIN_SCAN_NM = 0.5
MIN_PIXELS, MAX_PIXELS = 16, 1024
MAX_WAIT_S = 3600.0


class ActionError(ValueError):
    """A call the model has to correct (bad parameter, unknown frame …). Nothing executed."""


@dataclass
class ActionOutcome:
    ok: bool
    error: str | None = None
    facts: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    resolved: dict = field(default_factory=dict)      # positions etc. after resolution
    #: what the action resolved before running (a scan's policy: line time, pixels, estimate)
    plan: dict = field(default_factory=dict)
    #: a request to the host, not done here: a scan that started and stopped before the frame
    #: was complete left its rows in the controller's buffer; ``{"skill": "SaveScan", "reason"}``
    #: asks the host to save them as a partial frame (it may have to do so outside this action's
    #: own stop, see :meth:`mast.vista.service.VistaService._salvage_partial`)
    salvage: dict | None = None


# ── parameter specs ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class P:
    """One parameter of an action. ``kind``: number | integer | boolean | point."""

    name: str
    kind: str
    doc: str
    required: bool = False
    lo: float | None = None
    hi: float | None = None
    default: Any = None
    hint: str = ""                  # shown in act's description (params is a free-form object there)

    def schema(self) -> dict:
        if self.kind == "point":
            return {"oneOf": [
                {"type": "object", "properties": {"x_nm": {"type": "number"}, "y_nm": {"type": "number"}},
                 "required": ["x_nm", "y_nm"], "additionalProperties": False},
                {"type": "object", "properties": {"frame": {"type": "string"}, "x": {"type": "number"},
                                                  "y": {"type": "number"}},
                 "required": ["frame", "x", "y"], "additionalProperties": False}],
                "description": self.doc}
        s: dict = {"type": self.kind, "description": self.doc}
        if self.lo is not None:
            s["minimum"] = self.lo
        if self.hi is not None:
            s["maximum"] = self.hi
        if self.default is not None:
            s["default"] = self.default
        return s

    def describe(self) -> str:
        rng = ""
        if self.lo is not None or self.hi is not None:
            rng = f" [{'' if self.lo is None else _fmt(self.lo)}..{'' if self.hi is None else _fmt(self.hi)}]"
        opt = "" if self.required else "?"
        return f"{self.name}{opt}: {self.kind}{rng}" + (f" ({self.hint})" if self.hint else "")


def _fmt(x: float) -> str:
    return f"{x:g}"


@dataclass(frozen=True)
class ActionSpec:
    name: str
    summary: str
    params: tuple[P, ...]
    fn: Callable[["ActionRunner", dict], ActionOutcome]
    qplus_only: bool = False
    produces: str = "none"          # "frame" | "spectrum" | "none"

    def param_names(self) -> set[str]:
        return {p.name for p in self.params}

    def describe(self) -> str:
        ps = ", ".join(p.describe() for p in self.params) or "no parameters"
        return f"- `{self.name}` ({ps}): {self.summary}"


def validate_params(spec: ActionSpec, params: Any) -> dict:
    """Check ``params`` against ``spec``; return a clean dict with defaults filled in."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ActionError(f"{spec.name}: params must be an object")
    unknown = sorted(set(params) - spec.param_names())
    if unknown:
        raise ActionError(f"{spec.name}: unknown parameter(s) {unknown}; expected "
                          f"{sorted(spec.param_names()) or 'none'}")
    out: dict = {}
    for p in spec.params:
        if p.name not in params or params[p.name] is None:
            if p.required:
                raise ActionError(f"{spec.name}: missing required parameter {p.name!r}")
            if p.default is not None:
                out[p.name] = p.default
            continue
        v = params[p.name]
        if p.kind in ("number", "integer"):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                raise ActionError(f"{spec.name}: {p.name} must be a finite number")
            if p.kind == "integer":
                if float(v) != int(v):
                    raise ActionError(f"{spec.name}: {p.name} must be an integer")
                v = int(v)
            else:
                v = float(v)
            if p.lo is not None and v < p.lo:
                raise ActionError(f"{spec.name}: {p.name}={v:g} is below the minimum {p.lo:g}")
            if p.hi is not None and v > p.hi:
                raise ActionError(f"{spec.name}: {p.name}={v:g} is above the maximum {p.hi:g}")
        elif p.kind == "boolean":
            if not isinstance(v, bool):
                raise ActionError(f"{spec.name}: {p.name} must be true or false")
        elif p.kind == "point":
            if not isinstance(v, dict):
                raise ActionError(f"{spec.name}: {p.name} must be an object "
                                  "{x_nm, y_nm} or {frame, x, y}")
        out[p.name] = v
    return out


# ── the runner ──────────────────────────────────────────────────────────────
class ActionRunner:
    """Executes validated actions against MAST; :mod:`mast.vista.service` builds one per act.

    ``ctx``: anything with ``run(name, params)`` returning a skill result (the service passes a
    step context that runs every step through ``direct_exec.run_and_record``). ``resolve_point``:
    callable mapping a frame-pixel position to scan nm
    (:meth:`mast.vista.workspace.Workspace.resolve_point`, from the workspace's archive).
    ``clock``: kept from VISTA-SPM (its simulator clock); no action reads it.
    ``on_plan``: called with what an action resolved before it runs (a scan's policy and its
    estimated duration), so a running action can say how long it will take.
    """

    def __init__(self, ctx, *, resolve_point: Callable[[dict], tuple[float, float, dict]],
                 time_scale: float, clock=None, is_qplus: bool = False, sleep=time.sleep,
                 list_files: Callable[[], set[str]] | None = None,
                 on_plan: Callable[[dict], None] | None = None):
        self.ctx = ctx
        self.list_files = list_files or (lambda: set())
        self._resolve_point = resolve_point
        self.time_scale = float(time_scale or 1.0)
        self.clock = clock
        self.is_qplus = bool(is_qplus)
        self._sleep = sleep
        self._on_plan = on_plan
        self._steps: list[dict] = []
        self._plan: dict = {}

    # helpers used by the action functions
    def point(self, spec: dict, what: str) -> tuple[float, float, dict]:
        try:
            return self._resolve_point(spec)
        except ActionError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad reference is the model's to fix
            raise ActionError(f"{what}: {exc}") from None

    def skill(self, name: str, params: dict | None = None, *, optional: bool = False):
        """Run one MAST skill. A failure of a non-optional step raises ``_StepFailed``."""
        res, rec = self._call(name, params)
        if res is None:
            if optional:
                return None
            raise _StepFailed(name, rec["error"])
        if not rec["success"] and not optional:
            raise _StepFailed(name, rec["error"] or "failed")
        return res

    def attempt(self, name: str, params: dict | None = None) -> tuple[Any, str]:
        """Run one MAST skill and hand back its result whether or not it succeeded:
        ``(result or None if it raised, error text)``. For an action that has to look at a
        failed step's data before it gives up (``scan``)."""
        res, rec = self._call(name, params)
        return res, ("" if rec["success"] else (rec["error"] or "failed"))

    def _call(self, name: str, params: dict | None) -> tuple[Any, dict]:
        params = dict(params or {})
        rec: dict = {"skill": name, "params": _jsonable(params)}
        try:
            res = self.ctx.run(name, params)
        except Exception as exc:  # noqa: BLE001
            rec.update(success=False, error=f"{type(exc).__name__}: {exc}")
            self._steps.append(rec)
            return None, rec
        rec.update(success=bool(getattr(res, "success", False)),
                   error=str(getattr(res, "error", "") or ""))
        self._steps.append(rec)
        return res, rec

    def note_plan(self, plan: dict) -> None:
        """Record what the action resolved before running and pass it to ``on_plan``."""
        self._plan.update(plan)
        if self._on_plan is not None:
            try:
                self._on_plan(dict(self._plan))
            except Exception:  # noqa: BLE001 — reporting progress never stops the action
                pass

    @staticmethod
    def data(res, key: str, default=None):
        d = getattr(res, "data", None) if res is not None else None
        return d.get(key, default) if isinstance(d, dict) else default

    def run(self, spec: ActionSpec, params: dict) -> ActionOutcome:
        if spec.qplus_only and not self.is_qplus:
            return ActionOutcome(False, f"{spec.name} needs a qPlus sensor; this tip is not one")
        self._steps = []
        self._plan = {}
        try:
            out = spec.fn(self, params)
        except ActionError as exc:
            out = ActionOutcome(False, str(exc))
        except _StepFailed as exc:
            out = ActionOutcome(False, f"{exc.skill} failed: {exc.error}")
        except Exception as exc:  # noqa: BLE001 — never let an action crash the service
            out = ActionOutcome(False, f"internal error: {type(exc).__name__}: {exc}")
        out.steps = list(self._steps)
        out.plan = dict(self._plan)
        return out


class _StepFailed(Exception):
    def __init__(self, skill: str, error: str):
        super().__init__(f"{skill}: {error}")
        self.skill, self.error = skill, error


def _jsonable(x: Any) -> Any:
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return str(x)


# ── action implementations ─────────────────────────────────────────────────
#: the ``ScanAt`` step after which the controller's buffer holds rows of THIS frame (before it,
#: the buffer still holds the previous frame, and saving it would archive that one again)
_SCAN_STARTED_STEP = "start_scan"


def _scan_plan(args: dict) -> dict:
    """How ``ScanAt`` will resolve these arguments (line time, pixels, policy tier, estimated
    duration), by ScanAt's own resolution. Empty when the policy cannot resolve them; ScanAt
    then fails and says why."""
    try:
        from mast.skills.composite.scan_at import ScanAt

        res = ScanAt()._resolve(dict(args))
    except Exception:  # noqa: BLE001 — the estimate is a courtesy; ScanAt reports a bad request
        return {}
    plan = {"line_time_s": (getattr(res, "configure_scan", None) or {}).get("line_time_s"),
            "pixels": (getattr(res, "set_scan_buffer", None) or {}).get("pixels"),
            "tier": getattr(res, "tier_name", None),
            "estimated_scan_s": getattr(res, "estimated_scan_s", None)}
    return {k: _jsonable(v) for k, v in plan.items() if v is not None}


def _scan_facts(res) -> dict:
    d = getattr(res, "data", None) or {}
    facts = {k: d[k] for k in ("line_time_s", "requested_pixels", "estimated_scan_s")
             if d.get(k) is not None}
    for k_out, keys in (("lines_done", ("lines_done", "scan_lines_done")),
                        ("lines_total", ("lines_total", "scan_lines_total"))):
        v = next((d[k] for k in keys if isinstance(d.get(k), int) and not isinstance(d.get(k), bool)),
                 None)
        if v is not None:
            facts[k_out] = v
    return facts


def _scan_started(res) -> bool:
    prog = (getattr(res, "data", None) or {}).get("_progress")
    done = prog.get("completed_steps") if isinstance(prog, dict) else None
    return isinstance(done, (list, tuple)) and _SCAN_STARTED_STEP in done


def _scan(r: ActionRunner, p: dict) -> ActionOutcome:
    cx, cy, src = r.point(p["center"], "center")
    size = float(p["size_nm"])
    # ``purpose`` stays at ScanAt's default ("auto": the policy tier follows the size); any value
    # the model gave explicitly overrides the tier's.
    args: dict = {"center_x_m": cx * 1e-9, "center_y_m": cy * 1e-9, "size_m": size * 1e-9}
    if "pixels" in p:
        args["pixels"] = int(p["pixels"])
    if "angle_deg" in p:
        args["angle_deg"] = float(p["angle_deg"])
    if "line_time_s" in p:
        args["line_time_s"] = float(p["line_time_s"])
    if "bias_v" in p:
        args["bias_v"] = float(p["bias_v"])
    if "current_pa" in p:
        args["setpoint_a"] = float(p["current_pa"]) * 1e-12
    plan = _scan_plan(args)
    if plan:
        r.note_plan({"scan": plan})
    resolved = {"center_nm": [cx, cy], "center_from": src}
    before = {f for f in r.list_files() if f.lower().endswith(".sxm")}
    res, err = r.attempt("ScanAt", args)
    new_frame = bool({f for f in r.list_files() if f.lower().endswith(".sxm")} - before)
    if err:
        # stopped (cancelled, timed out, halted by someone): the rows it acquired are in the
        # controller's buffer; unless the controller saved them itself, ask the host to save them
        salvage = None
        if res is not None and not new_frame and _scan_started(res):
            salvage = {"skill": "SaveScan", "reason": "the scan stopped before the frame was complete"}
        return ActionOutcome(False, f"ScanAt failed: {err}", facts=_scan_facts(res),
                             resolved=resolved, salvage=salvage)
    # the controller autosaves a completed frame; save explicitly only when it did not, so
    # one scan is one archived frame (an extra SaveScan writes a byte-identical duplicate)
    if not new_frame:
        r.skill("SaveScan", {})
    return ActionOutcome(True, facts=_scan_facts(res), resolved=resolved)


def _bias_needs_safe_path(start_v: float, target_v: float) -> bool:
    """``BiasSettleChange``'s own two cases for preferring it over ``SetBias`` — the change
    crosses zero, or it is larger than its ramp threshold (zero crossing as it defines it) —
    and a target inside its dead band, wherever the change starts. BiasSettleChange refuses to
    end there while the feedback is on or unknown (the constant-current feedback drives the tip
    into the surface); routing only large changes through it would let 0.3 V -> 0 V reach that
    band directly, the very stop it exists to refuse."""
    from mast.skills.composite.bias_settle import RAMP_THRESHOLD_V, ZERO_DEADBAND_V

    crosses = (start_v > 0) != (target_v > 0) and start_v != 0 and target_v != 0
    return (crosses or abs(target_v - start_v) > RAMP_THRESHOLD_V
            or abs(target_v) < ZERO_DEADBAND_V)


def _set_bias(r: ActionRunner, p: dict) -> ActionOutcome:
    target = float(p["bias_v"])
    start = r.data(r.skill("GetBias", {}, optional=True), "bias_v")
    if not isinstance(start, (int, float)) or isinstance(start, bool) \
            or _bias_needs_safe_path(float(start), target):
        # an unreadable start goes the safe way too: BiasSettleChange then refuses, since it
        # cannot tell whether the change crosses zero
        res = r.skill("BiasSettleChange", {"bias_v": target})
        facts = {k: r.data(res, k) for k in ("bias_v_start", "strategy", "crossed_zero", "settle_s")
                 if r.data(res, k) is not None}
        return ActionOutcome(True, facts={"path": "BiasSettleChange", **facts})
    r.skill("SetBias", {"bias_v": target})
    return ActionOutcome(True, facts={"path": "SetBias", "bias_v_start": float(start)})


def _set_current(r: ActionRunner, p: dict) -> ActionOutcome:
    r.skill("SetSetpoint", {"setpoint_a": float(p["current_pa"]) * 1e-12})
    return ActionOutcome(True)


def _feedback(r: ActionRunner, p: dict) -> ActionOutcome:
    gains = [k for k in ("p_gain_pm", "i_gain_nm_s", "time_constant_us") if k in p]
    if not gains and "on" not in p:
        raise ActionError("feedback: give `on` and/or the gains (p_gain_pm, i_gain_nm_s, time_constant_us)")
    if gains:
        cur = r.skill("GetZCtrlGain", {}, optional=True)
        pg = p.get("p_gain_pm")
        ig = p.get("i_gain_nm_s")
        tc = p.get("time_constant_us")
        args = {"p_gain": (pg * 1e-12) if pg is not None else r.data(cur, "p_gain"),
                "i_gain": (ig * 1e-9) if ig is not None else r.data(cur, "i_gain"),
                "time_constant_s": (tc * 1e-6) if tc is not None else r.data(cur, "time_constant_s")}
        if any(v is None for v in args.values()):
            raise ActionError("feedback: the current gains could not be read; give all three of "
                              "p_gain_pm, i_gain_nm_s, time_constant_us")
        r.skill("SetZCtrlGain", args)
    if "on" in p:
        r.skill("ZControllerOnOff", {"enable": bool(p["on"])})
    return ActionOutcome(True)


def _move_tip(r: ActionRunner, p: dict) -> ActionOutcome:
    x, y, src = r.point(p["to"], "to")
    r.skill("MoveToXY", {"x_m": x * 1e-9, "y_m": y * 1e-9, "wait": True})
    return ActionOutcome(True, resolved={"to_nm": [x, y], "to_from": src})


def _maybe_move(r: ActionRunner, p: dict, key: str = "at") -> dict:
    if key not in p:
        return {}
    x, y, src = r.point(p[key], key)
    r.skill("MoveToXY", {"x_m": x * 1e-9, "y_m": y * 1e-9, "wait": True})
    return {f"{key}_nm": [x, y], f"{key}_from": src}


def _pulse(r: ActionRunner, p: dict) -> ActionOutcome:
    resolved = _maybe_move(r, p)
    args = {"pulse_v": float(p["bias_v"]), "duration_s": float(p["duration_s"])}
    if "count" in p:
        args["count"] = int(p["count"])
    res = r.skill("TipPulse", args)
    facts = {k: v for k, v in (getattr(res, "data", None) or {}).items()
             if isinstance(v, (int, float, str, bool)) and k in ("dz_m", "z_change_m", "pulses")}
    return ActionOutcome(True, facts=facts, resolved=resolved)


def _poke(r: ActionRunner, p: dict) -> ActionOutcome:
    resolved = _maybe_move(r, p)
    depth_m = abs(float(p["depth_nm"])) * 1e-9
    args: dict = {"tip_lift_m": -depth_m, "lift_height_m": depth_m, "restore_feedback": True}
    if "bias_v" in p:
        args.update(change_bias=True, bias_v=float(p["bias_v"]))
    if "dwell_s" in p:
        args["lift_time_1_s"] = float(p["dwell_s"])
    r.skill("TipShape", args)
    return ActionOutcome(True, resolved=resolved)


def _sts(r: ActionRunner, p: dict) -> ActionOutcome:
    resolved = _maybe_move(r, p)
    cfg = {"start_v": float(p["start_v"]), "end_v": float(p["end_v"]),
           "num_points": int(p.get("num_points", 256))}
    if "z_offset_nm" in p:
        cfg["z_offset_m"] = float(p["z_offset_nm"]) * 1e-9
    r.skill("ConfigureSTS", cfg)
    r.skill("AcquireSTS", {})
    return ActionOutcome(True, resolved=resolved)


def _df_curve(r: ActionRunner, p: dict) -> ActionOutcome:
    resolved = _maybe_move(r, p)
    args = {"z_sweep_distance_m": float(p["z_sweep_nm"]) * 1e-9,
            "num_points": int(p.get("num_points", 256))}
    if "z_offset_nm" in p:
        args["z_offset_m"] = float(p["z_offset_nm"]) * 1e-9
    res = r.skill("AcquireDeltaFCurve", args)
    facts = {k: r.data(res, k) for k in ("f0_hz", "df_rest_hz", "amplitude_setpoint_m")
             if r.data(res, k) is not None}
    return ActionOutcome(True, facts=facts, resolved=resolved)


def _pll(r: ActionRunner, p: dict) -> ActionOutcome:
    if "amplitude_pm" in p:
        r.skill("SetPLLAmpCtrlSetpnt", {"modulator_index": 1, "setpoint_m": float(p["amplitude_pm"]) * 1e-12})
    if "on" in p:
        on = bool(p["on"])
        r.skill("PLLOnOff", {"modulator_index": 1, "output_on": on, "amp_ctrl_on": on, "phase_ctrl_on": on})
    if p.get("auto_center"):
        r.skill("PLLFreqShiftAutoCenter", {"modulator_index": 1})
    status = r.skill("GetPLLStatus", {"modulator_index": 1}, optional=True)
    amp = r.skill("ReadTipOscillationAmplitude", {}, optional=True)
    facts = {}
    d = getattr(status, "data", None) or {}
    for k in ("center_freq_hz", "f0_hz", "output_on", "amp_ctrl_on", "phase_ctrl_on"):
        if k in d and isinstance(d[k], (int, float, bool)):
            facts[k] = d[k]
    a = r.data(amp, "amplitude")
    if isinstance(a, (int, float)):
        facts["amplitude_pm"] = float(a) * 1e12
    return ActionOutcome(True, facts=facts)


def _move_atom(r: ActionRunner, p: dict) -> ActionOutcome:
    ax, ay, asrc = r.point(p["atom"], "atom")
    tx, ty, tsrc = r.point(p["target"], "target")
    args: dict = {"atom_x_m": ax * 1e-9, "atom_y_m": ay * 1e-9, "target_x_m": tx * 1e-9,
                  "target_y_m": ty * 1e-9, "verify": False, "max_attempts": 1}
    if "current_pa" in p:
        args["manip_setpoint_a"] = float(p["current_pa"]) * 1e-12
    if "bias_v" in p:
        args["manip_bias_v"] = abs(float(p["bias_v"]))
    if "speed_nm_s" in p:
        args["manip_speed_m_s"] = float(p["speed_nm_s"]) * 1e-9
    if "approach_offset_nm" in p:
        args["approach_offset_m"] = float(p["approach_offset_nm"]) * 1e-9
    res = r.skill("MoveAtomTo", args)
    facts = {}
    rj = r.data(res, "junction_resistance_ohm")
    if isinstance(rj, (int, float)):
        facts["junction_resistance_kohm"] = float(rj) / 1e3
    facts["instrument_restored"] = r.data(res, "instrument_restored")
    return ActionOutcome(True, facts=facts,
                         resolved={"atom_nm": [ax, ay], "atom_from": asrc,
                                   "target_nm": [tx, ty], "target_from": tsrc})


def _drift_comp(r: ActionRunner, p: dict) -> ActionOutcome:
    args: dict = {"enable": bool(p["enable"])}
    for k_in, k_out in (("vx_nm_s", "vx"), ("vy_nm_s", "vy"), ("vz_nm_s", "vz")):
        if k_in in p:
            args[k_out] = float(p[k_in]) * 1e-9
    r.skill("SetDriftCompensation", args)
    return ActionOutcome(True)


def _wait(r: ActionRunner, p: dict) -> ActionOutcome:
    seconds = float(p["seconds"])
    r._sleep(seconds / r.time_scale)          # instrument seconds (time_scale > 1: a sped-up simulator)
    return ActionOutcome(True, facts={"waited_s": seconds})


def _withdraw(r: ActionRunner, p: dict) -> ActionOutcome:
    r.skill("WithdrawTip", {})
    return ActionOutcome(True)


def _approach(r: ActionRunner, p: dict) -> ActionOutcome:
    # ApproachTip: close the feedback without motor motion first; only when that cannot reach
    # tunnelling, the current-feedback AutoApproach with its own (generous) wait budget. The
    # former direct AutoApproach(wait_timeout_s=300) skipped the first stage and cut the budget
    # its own parameter text warns against cutting.
    res = r.skill("ApproachTip", {})
    facts: dict = {}
    for k in ("method", "engaged", "auto_approach_used"):
        v = r.data(res, k)
        if isinstance(v, (bool, str)):
            facts[k] = v
    for k_in, k_out in (("peak_current_a", "peak_current_pa"),
                        ("measured_current_a", "measured_current_pa"), ("setpoint_a", "setpoint_pa")):
        v = r.data(res, k_in)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
            facts[k_out] = round(float(v) * 1e12, 3)
    return ActionOutcome(True, facts=facts)


#: ``BiasSettleChange``'s ramp threshold and dead band as the description states them (the
#: routing reads the skill's own constants; tests/v2/vista/test_actions.py holds the two equal,
#: and importing the composite package here would load every composite skill with this module)
BIAS_RAMP_THRESHOLD_V = 0.5
BIAS_DEADBAND_V = 0.05


def _set_bias_summary() -> str:
    return ("set the sample bias (V). A change that crosses zero or is larger than "
            f"{_fmt(BIAS_RAMP_THRESHOLD_V)} V, and any target with |V| < {_fmt(BIAS_DEADBAND_V * 1e3)} mV, "
            "goes through MAST's safe path (BiasSettleChange): it ramps through zero without stopping "
            "inside that band, waits for the feedback to settle, and refuses to end inside the band "
            "while the feedback is on or unknown; a smaller change of the same sign is set directly. "
            "`path` in the result says which.")


_POINT_DOC = ("position: {x_nm, y_nm} in scan-frame nm, or {frame, x, y} in display pixels of an "
              "archived frame")

ACTIONS: dict[str, ActionSpec] = {s.name: s for s in (
    ActionSpec("scan", "acquire and save one topographic frame (forward and backward, every recorded "
               "channel) centred at `center`; returns the new frame. Unset optional values come from "
               "the instrument's scan policy for that size.",
               (P("center", "point", _POINT_DOC, required=True),
                P("size_nm", "number", "frame side length in nm", required=True, lo=MIN_SCAN_NM, hi=MAX_SCAN_NM),
                P("pixels", "integer", "pixels per line (and lines)", lo=MIN_PIXELS, hi=MAX_PIXELS),
                P("angle_deg", "number", "scan angle in degrees", lo=-180, hi=180),
                P("line_time_s", "number", "time per line in seconds", lo=0.01, hi=60),
                P("bias_v", "number", "imaging bias in V", lo=-10, hi=10),
                P("current_pa", "number", "imaging setpoint current in pA", lo=0.1, hi=100000)),
               _scan, produces="frame"),
    ActionSpec("set_bias", _set_bias_summary(),
               (P("bias_v", "number", "bias in V", required=True, lo=-10, hi=10),), _set_bias),
    ActionSpec("set_current", "set the feedback setpoint current (pA).",
               (P("current_pa", "number", "setpoint in pA", required=True, lo=0.1, hi=100000),), _set_current),
    ActionSpec("feedback", "switch the Z feedback on/off and/or set its gains (omitted gains keep their value).",
               (P("on", "boolean", "feedback on (true) or off (false)"),
                P("p_gain_pm", "number", "proportional gain in pm", lo=0.0),
                P("i_gain_nm_s", "number", "integral gain in nm/s", lo=0.0),
                P("time_constant_us", "number", "time constant in microseconds", lo=0.0)), _feedback),
    ActionSpec("move_tip", "move the tip laterally to a position (feedback stays on).",
               (P("to", "point", _POINT_DOC, required=True),), _move_tip),
    ActionSpec("pulse", "apply a bias voltage pulse to the tip (optionally at a position first). "
               "May change the tip, and the surface under it.",
               (P("bias_v", "number", "pulse amplitude in V", required=True, lo=-10, hi=10),
                P("duration_s", "number", "pulse length in s", required=True, lo=0.001, hi=1.0),
                P("count", "integer", "number of pulses", lo=1, hi=5),
                P("at", "point", _POINT_DOC)), _pulse),
    ActionSpec("poke", "controlled indentation: move the tip `depth_nm` into the surface and back, feedback "
               "restored afterwards (optionally at a position, at a bias). Changes the tip and leaves a mark.",
               (P("depth_nm", "number", "indentation depth in nm", required=True, lo=0.05, hi=5.0, hint="into the surface"),
                P("bias_v", "number", "bias during the indentation in V", lo=-10, hi=10),
                P("dwell_s", "number", "duration of the downward ramp in s", lo=0.01, hi=5.0),
                P("at", "point", _POINT_DOC)), _poke),
    ActionSpec("sts", "measure one bias spectrum (I(V), dI/dV where recorded) at a position; returns the spectrum.",
               (P("start_v", "number", "sweep start in V", required=True, lo=-5, hi=5),
                P("end_v", "number", "sweep end in V", required=True, lo=-5, hi=5),
                P("num_points", "integer", "points in the sweep", lo=16, hi=4096, default=256),
                P("z_offset_nm", "number", "tip offset before the sweep in nm (negative = closer)", lo=-1, hi=1,
                  hint="negative = closer"),
                P("at", "point", _POINT_DOC)), _sts, produces="spectrum"),
    ActionSpec("df_curve", "qPlus only: measure one frequency-shift vs distance curve Δf(z) at a position; "
               "returns the curve.",
               (P("z_sweep_nm", "number", "sweep distance in nm", required=True, lo=0.05, hi=5.0),
                P("z_offset_nm", "number", "start offset in nm (positive = farther)", lo=-1, hi=5, hint="positive = farther"),
                P("num_points", "integer", "points in the sweep", lo=16, hi=4096, default=256),
                P("at", "point", _POINT_DOC)), _df_curve, qplus_only=True, produces="spectrum"),
    ActionSpec("pll", "qPlus only: set the oscillation amplitude, switch the PLL/amplitude control on or off, "
               "and/or auto-centre the frequency shift; returns the PLL status.",
               (P("on", "boolean", "oscillation and controllers on/off"),
                P("amplitude_pm", "number", "oscillation amplitude setpoint in pm", lo=1, hi=2000),
                P("auto_center", "boolean", "auto-centre the frequency shift")), _pll, qplus_only=True),
    ActionSpec("move_atom", "lateral manipulation: lower the junction resistance behind `atom`, drag the tip to "
               "`target`, restore the imaging conditions. Does not check where the atom went — image it.",
               (P("atom", "point", "where the atom is now; " + _POINT_DOC, required=True),
                P("target", "point", "where to drag it; " + _POINT_DOC, required=True),
                P("current_pa", "number", "manipulation setpoint in pA (higher = lower resistance); "
                  "57000 when omitted", lo=1000, hi=100000, hint="default 57000"),
                P("bias_v", "number", "manipulation bias magnitude in V (sign follows the imaging bias); "
                  "0.01 when omitted", lo=0.001, hi=0.5, hint="magnitude; default 0.01"),
                P("speed_nm_s", "number", "drag speed in nm/s; 0.5 when omitted", lo=0.01, hi=100, hint="default 0.5"),
                P("approach_offset_nm", "number", "start this far behind the atom in nm; 0 when omitted",
                  lo=0, hi=1, hint="start behind the atom; default 0")),
               _move_atom),
    ActionSpec("drift_compensation", "switch the piezo drift compensation on/off with velocities in nm/s "
               "(the velocity at which features move in the scan frame).",
               (P("enable", "boolean", "compensation on/off", required=True),
                P("vx_nm_s", "number", "x velocity in nm/s", lo=-10, hi=10, hint="feature velocity in the scan frame"),
                P("vy_nm_s", "number", "y velocity in nm/s", lo=-10, hi=10),
                P("vz_nm_s", "number", "z velocity in nm/s", lo=-10, hi=10)), _drift_comp),
    ActionSpec("wait", "let instrument time pass without acting (thermal settling, drift measurement).",
               (P("seconds", "number", "instrument seconds to wait", required=True, lo=1, hi=MAX_WAIT_S),),
               _wait),
    ActionSpec("withdraw", "withdraw the tip from the surface (feedback off, tip retracted).", (), _withdraw),
    ActionSpec("approach", "establish tunnelling the safe way: first close the Z feedback without moving "
               "the motors; only when the surface is out of the piezo's reach, run the coarse "
               "current-feedback auto-approach, which stops at the setpoint. `method` in the result "
               "says which way it went.", (), _approach),
)}


#: what an act result carries as its observation, per observation mode (VISTA-SPM's benchmark
#: arms; MAST's act observes with images)
OBSERVATION_PHRASE = {
    "image": "the new image when the action produced one",
    "text": "the new frame as a numeric text grid (no image) when the action produced one",
    "vigil": ("a text summary of a new scan frame from MAST's automated detectors and learned vision model "
              "(no image), or a new spectrum as a numeric table, when the action produced one"),
}
#: appended to the image phrase when scan images carry the metric grid (frames.view_grid)
GRID_PHRASE = (" (on a scan image, faint amber lines draw a square grid every grid_nm nm, counted from the "
               "frame's top-left corner; grid_nm is in the image's label)")


def act_description(names: list[str], observation: str = "image", grid: bool = False) -> str:
    """The model-facing description of ``act`` for the given action names and observation mode;
    ``grid``: scan images carry the metric grid."""
    phrase = OBSERVATION_PHRASE.get(observation, OBSERVATION_PHRASE["image"])
    if grid and observation == "image":
        phrase += GRID_PHRASE
    lines = [
        "Execute exactly one instrument action. This is the only tool that changes the instrument; each call "
        "counts as one action and consumes instrument time. The result reports what the controller returned, "
        "the instrument state afterwards, the ids of any new frames or spectra, the instrument time used so "
        f"far, and {phrase}. You observe this result "
        "before another action can execute.",
        "Units: nm, V, pA, s, nm/s. A position is {\"x_nm\": .., \"y_nm\": ..} in scan-frame nm (the coordinates "
        "report_result uses), or {\"frame\": \"t0007.s0\", \"x\": .., \"y\": ..} in display pixels of an archived "
        "frame, converted with that frame's own geometry. Positions in older frames may be stale: the sample "
        "drifts and the piezo creeps.",
        "Actions (`action` name, `params`):",
    ]
    lines += [ACTIONS[n].describe() for n in names]
    return "\n".join(lines)


def act_schema(names: list[str]) -> dict:
    return {"type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(names), "description": "which action"},
                "params": {"type": "object", "description": "the action's parameters (see the list above)"},
                "note": {"type": "string", "maxLength": 1000,
                         "description": "optional: what you expect to see (kept in the history)"}},
            "required": ["action", "params"], "additionalProperties": False}
