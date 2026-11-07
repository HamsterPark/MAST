"""Child process for ``test_motor_groups_simulator.py`` — NOT a test module.

``tests/conftest.py`` replaces ``nanonis_spm`` with a MagicMock in the pytest
process, so the simulator can only be reached from a child (the same reason
``tests/v2/e2e_sim`` runs the service in one). Usage::

    python -I -X utf8 _motor_groups_sim_child.py <out.json>

``MAST2_PROJECT_ROOT`` must point at a scratch directory (the parent sets it).
Opens only main (6501) and emergency (6504); every move is lateral and paired;
the drive read at the start is put back and verified at the end; the pool is
closed gracefully in ``finally``.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "MASTv2"))


class _State:
    def snapshot(self):
        return None


class _Pool:
    """The real pool; sets the abort event once the n-th StartMove returned."""

    def __init__(self, real, ev, after):
        self.real, self.ev, self.after, self.moves = real, ev, after, 0

    def safe_call(self, method, *args, **kw):
        rec = self.real.safe_call(method, *args, **kw)
        if method == "Motor_StartMove":
            self.moves += 1
            if self.after is not None and self.moves >= self.after:
                self.ev.set()
        return rec


def _drive(pool):
    out = {}
    for a in (0, 1, 2, 3):
        rec = pool.safe_call("Motor_FreqAmpGet", a)
        if rec.error:
            raise RuntimeError(f"Motor_FreqAmpGet({a}): {rec.error}")
        vals = rec.return_value[2]
        out[a] = [round(float(vals[0]), 4), round(float(vals[1]), 4)]
    return out


def _brief(res):
    d = dict(res.data or {})
    return {"success": bool(res.success), "error": res.error or "",
            "steps_done": d.get("steps_done"), "aborted": d.get("aborted"),
            "drive": d.get("drive"),
            "calls": [[c.method, bool(c.error)] for c in (res.nanonis_calls or [])]}


def _put_back(pool, base):
    """Restore the drive read at the start (directly, as an operator would) and
    return what reads back."""
    if _drive(pool) != base:
        pool.safe_call("Motor_FreqAmpSet", base[0][0], base[0][1], 0)
        for a in (1, 2, 3):
            if base[a] != base[0]:
                pool.safe_call("Motor_FreqAmpSet", base[a][0], base[a][1], a)
    return _drive(pool)


def _manual_panel(result, pool, reg, base, ExecutionContext, Pool, State) -> None:
    """The manual panel's buttons for a group, run exactly as the frontend builds
    them (fixed + axis + direction + steps) through a real ExecutionContext: a
    verify_only group whose configured drive is the one the simulator has, so it
    steps without writing anything; +10, −10, then the card's stop."""
    import threading

    from mast.instruments import motor_groups as mg
    from mast.instruments.manual import local_manual_devices, local_manual_readouts
    from mast.instruments.registry import InstrumentRegistry
    from mast.skills.builtins.motor import StopMotor
    from mast.skills.builtins.optics_motor_groups import ListNanonisMotorGroups

    f0, a0 = base[0]
    mg.config_path().write_text(json.dumps({"stm_coarse_group": 0, "groups": [
        {"group": 3, "name": "panel", "role": "laser_mirror", "axes": {"Y": "py"},
         "max_steps_per_cmd": 20, "chunk_steps": 10, "requires": "none",
         "freq_hz": f0, "amp_v": a0, "drive_policy": "verify_only"},
    ]}), encoding="utf-8")
    mg.reset_for_tests()
    reg.register(StopMotor)
    reg.register(ListNanonisMotorGroups)
    notes: list = []
    empty = InstrumentRegistry(mg.config_path().with_name("no_optics.json"))
    devs = local_manual_devices(skill_registry=reg, notes=notes, instruments=empty)
    result["manual notes"] = notes
    result["manual ids"] = [d["id"] for d in devs]
    dev = devs[0]
    jog = next(a for a in dev["actions"] if a["id"] == "jog")
    stop = next(a for a in dev["actions"] if a["id"] == "stop")
    axis = dev["axes"][0]["name"]

    def press(action, **typed):
        ev = threading.Event()
        params = {**action["fixed"], **({action["axis_param"]: axis} if action["per_axis"]
                                        else {}), **typed}
        ec = ExecutionContext(pool=Pool(pool, ev, None), state=State(), registry=reg,
                              abort_event=[ev], owner="simulator test (manual panel)",
                              approval_source="human")
        return _brief(ec.run(action["skill"], params))

    result["manual jog +10"] = press(jog, direction="+", steps=10)
    result["manual readout +10"] = local_manual_readouts([dev["id"]])[dev["id"]]
    result["manual jog -10"] = press(jog, direction="-", steps=10)
    result["manual jog 30"] = press(jog, direction="+", steps=30)       # over the cap
    result["manual stop"] = press(stop)
    result["manual readout end"] = local_manual_readouts([dev["id"]])[dev["id"]]
    result["drive after manual"] = _drive(pool)


def main(out_path: Path) -> None:
    from mast.config import NanonisConfig
    from mast.core import coarse_drive
    from mast.core.connection import ConnectionPool
    from mast.core.execution_context import ExecutionContext
    from mast.core.registry import SkillRegistry
    from mast.instruments import motor_groups as mg
    from mast.skills.builtins.motor import SetMotorFreqAmp
    from mast.skills.builtins.optics_motor_groups import NanonisMotorGroupMove

    result: dict = {}
    pool = ConnectionPool(NanonisConfig(port_monitor=0, port_data=0, timeout_s=5.0))
    try:
        result["connect"] = pool.connect_all()
        if not result["connect"].get("main"):
            raise RuntimeError("simulator main port did not connect")
        base = _drive(pool)
        result["base"] = base
        f0, a0 = base[0]
        (mg.config_path()).parent.mkdir(parents=True, exist_ok=True)
        mg.config_path().write_text(json.dumps({"stm_coarse_group": 0, "groups": [
            {"group": 1, "name": "w&r", "role": "laser_mirror", "axes": {"X": "mx"},
             "max_steps_per_cmd": 100, "chunk_steps": 20, "requires": "none",
             "freq_hz": f0 + 1000.0, "amp_v": 25.0, "drive_policy": "write_and_restore"},
            {"group": 2, "name": "verify", "role": "lens", "axes": {"X": "lx"},
             "max_steps_per_cmd": 50, "chunk_steps": 50, "requires": "none",
             "freq_hz": f0 + 142.0, "amp_v": a0, "drive_policy": "verify_only"},
        ]}), encoding="utf-8")
        mg.reset_for_tests()
        coarse_drive.set_declaration({"max_amplitude_v": 250.0})
        reg = SkillRegistry()
        reg.register(NanonisMotorGroupMove)
        reg.register(SetMotorFreqAmp)          # drive writes go through it

        def run(params, abort_after=None, approval_source="human"):
            ev = threading.Event()
            ec = ExecutionContext(pool=_Pool(pool, ev, abort_after), state=_State(),
                                  registry=reg, abort_event=[ev], owner="simulator test",
                                  approval_source=approval_source)
            return _brief(ec.run("NanonisMotorGroupMove", params))

        try:
            # the autonomous path: the hard gate refuses the drive write, nothing moves
            result["w&r autonomous"] = run({"group": "1", "axis": "mx", "direction": "+",
                                            "steps": 40}, approval_source="auto")
            result["drive after autonomous"] = _drive(pool)
            for d in ("+", "-"):
                result[f"w&r {d}40"] = run({"group": "1", "axis": "mx", "direction": d,
                                            "steps": 40})
                result[f"drive after w&r {d}40"] = _drive(pool)
            result["verify_only"] = run({"group": "2", "axis": "lx", "direction": "+",
                                         "steps": 10})
            result["drive after verify_only"] = _drive(pool)
            result["abort"] = run({"group": "1", "axis": "mx", "direction": "+", "steps": 60},
                                  abort_after=1)
            result["latch after abort"] = mg.drive_latch()
            result["drive after abort"] = _drive(pool)
            done = int(result["abort"].get("steps_done") or 0)
            if done:   # what the operator would do: walk back
                pool.safe_call("Motor_StartMove", 1, done, 1, 1, recv_timeout_s=30)
            # ... and put the drive back by hand, as the latch tells them to
            result["drive at end"] = _put_back(pool, base)
            mg.clear_drive_latch()
            _manual_panel(result, pool, reg, base, ExecutionContext, _Pool, _State)
        finally:
            _put_back(pool, base)
            mg.clear_drive_latch()
    except Exception:  # noqa: BLE001 - reported to the parent
        result["fatal"] = traceback.format_exc()
    finally:
        try:
            pool.close_all()
            result["closed"] = True
        except Exception as exc:  # noqa: BLE001
            result["closed"] = f"close failed: {exc}"
        out_path.write_text(json.dumps(result, default=str), encoding="utf-8")


if __name__ == "__main__":
    if not os.environ.get("MAST2_PROJECT_ROOT"):
        raise SystemExit("MAST2_PROJECT_ROOT must point at a scratch directory")
    main(Path(sys.argv[1]))
