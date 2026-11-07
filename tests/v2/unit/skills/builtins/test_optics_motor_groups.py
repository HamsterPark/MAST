"""Nanonis motor groups: the config, the optical-group skills, and the STM coarse
call sites that used to hard-code group 0 (contract §9).

The controller double behaves like a Nanonis Mimea controller: one
module-global frequency/amplitude, any group number accepted, PosGet unsupported.
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

from mast.core.types import HardwareState, NanonisCallRecord
from mast.instruments import motor_groups as mg

GROUPS = {
    "stm_coarse_group": 0,
    "groups": [
        {"group": 1, "name": "Mirror X/Y", "role": "laser_mirror",
         "axes": {"X": "mirror_x", "Y": "mirror_y"}, "max_steps_per_cmd": 200,
         "chunk_steps": 50, "requires": "tip_withdrawn", "freq_hz": 1000.0, "amp_v": 30.0,
         "drive_policy": "write_and_restore"},
        {"group": 2, "name": "Lens", "role": "lens", "axes": {"Z": "lens_focus"},
         "max_steps_per_cmd": 100, "requires": "none", "freq_hz": 300.0, "amp_v": 200.0},
        {"group": 3, "name": "Beam deflection", "role": "beam_deflection",
         "axes": {"X": "bd_x"}, "max_steps_per_cmd": 50, "requires": "z_feedback_on",
         "freq_hz": 500.0, "amp_v": 40.0, "enabled": False},
    ],
}


@pytest.fixture
def groups_file(tmp_path, monkeypatch):
    """Point the loader at a tmp file; write() replaces its content. The STM rig
    ceiling (coarse_drive) is declared at 250 V for the duration."""
    from mast.core import coarse_drive

    p = tmp_path / mg.CONFIG_FILENAME
    monkeypatch.setattr(mg, "config_path", lambda: p)
    mg.reset_for_tests()
    saved = coarse_drive.get_declaration()
    monkeypatch.setattr(coarse_drive, "_persist_sink", None)
    coarse_drive.set_declaration({"max_amplitude_v": 250.0})

    def write(body):
        if body is None:
            if p.exists():
                p.unlink()
        else:
            p.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
        mg.reset_for_tests()
        return p

    yield write
    mg.reset_for_tests()
    coarse_drive.set_declaration(saved)


@pytest.fixture
def retract_dir():
    """Declare (or leave undeclared, ``None``) this rig's coarse retract direction
    in the in-memory instrument profile; starts undeclared, restores afterwards."""
    from mast.core import instrument_profile as ip

    before = ip.get_profile()

    def declare(value):
        prof = {k: v for k, v in before.items() if k != "retract_motor_dir"}
        if value is not None:
            prof["retract_motor_dir"] = value
        ip.set_profile(prof)

    declare(None)
    yield declare
    ip.set_profile(before)


class Controller:
    """Nanonis double: global drive, every group accepted, PosGet unsupported."""

    def __init__(self, *, withdrawn=True, z_on=False, scan=False, drive=(300.0, 200.0),
                 fail=None, abort_after_moves=None, state=True):
        self.drive = {a: tuple(drive) for a in (0, 1, 2, 3)}
        self.calls: list[tuple] = []
        self.runs = []
        self.fail = dict(fail or {})
        self.moves = 0
        self.abort_after_moves = abort_after_moves
        if state:
            snap = HardwareState(withdrawn=withdrawn, z_controller_on=z_on, scan_running=scan)
            self.state = type("S", (), {"snapshot": lambda s: snap, "refresh": lambda s: snap})()

    def safe_call(self, method, *args, role="main", allow_on_abort=False, recv_timeout_s=None):
        from mast.core.execution_context import _is_abort_safe

        self.calls.append((method, args, {"allow_on_abort": allow_on_abort,
                                          "recv_timeout_s": recv_timeout_s}))
        # the real ExecutionContext.safe_call abort gate: after an abort only
        # reads and stop/retract verbs reach the controller
        if not allow_on_abort and self.check_abort() and not _is_abort_safe(method, args):
            return NanonisCallRecord(method=method, args=args,
                                     error="aborted by operator — instrument writes are blocked")
        if method in self.fail:
            spec = self.fail[method]
            if not callable(spec) or spec(args):
                return NanonisCallRecord(method=method, args=args, error="canned failure")
        if method == "Motor_FreqAmpGet":
            f, a = self.drive[int(args[0])]
            return NanonisCallRecord(method=method, args=args, return_value=("", b"", [f, a]))
        if method == "Motor_FreqAmpSet":
            f, a, axis = args
            for k in ((0, 1, 2, 3) if axis == 0 else (axis,)):
                self.drive[k] = (float(f), float(a))
            return NanonisCallRecord(method=method, args=args, return_value=("", b"", []))
        if method == "Motor_StartMove":
            self.moves += 1
            return NanonisCallRecord(method=method, args=args, return_value=("", b"", []))
        if method == "Motor_PosGet":
            return NanonisCallRecord(method=method, args=args, error="Cannot access Motor Control",
                                     return_value=("e", b"", [0.0, 0.0, 0.0]))
        return NanonisCallRecord(method=method, args=args, return_value=("", b"", []))

    def check_abort(self):
        return (self.abort_after_moves is not None and self.moves >= self.abort_after_moves)

    def run(self, skill_name, params):
        """``ExecutionContext.run`` on the person-run path: the abort gate refuses
        every sub-skill once aborted; the hard gates of the autonomous path are
        exercised with the real ExecutionContext in TestDriveWritesGoThroughTheSkill."""
        from mast.core.types import SkillResult
        from mast.skills.builtins.motor import SetMotorFreqAmp

        self.runs.append((skill_name, dict(params)))
        if self.check_abort():
            return SkillResult(skill_name=skill_name, success=False,
                               error=("aborted by operator — refusing to start sub-skill "
                                      f"'{skill_name}'"))
        assert skill_name == "SetMotorFreqAmp", skill_name
        return SetMotorFreqAmp().execute(self, params)

    def verbs(self, name):
        return [c for c in self.calls if c[0] == name]


# ── config ──────────────────────────────────────────────────────────────────

class TestConfig:
    def test_no_file_means_group_0_and_no_optical_groups(self, groups_file):
        groups_file(None)
        assert mg.stm_coarse_group() == 0
        cfg = mg.load_motor_groups()
        assert cfg.groups == () and not cfg.fatal

    def test_valid_file(self, groups_file):
        groups_file(GROUPS)
        cfg = mg.load_motor_groups()
        assert [g.group for g in cfg.groups] == [1, 2, 3]
        assert mg.optical_group("Mirror X/Y").axis_letter("mirror_y") == "Y"
        assert mg.optical_group(2).axes == {"Z": "lens_focus"}
        assert mg.optical_group("1").chunk_steps == 50

    def test_broken_file_is_a_refusal_not_group_0(self, groups_file):
        groups_file("{broken")
        with pytest.raises(mg.MotorGroupConfigError, match="无法使用"):
            mg.stm_coarse_group()

    @pytest.mark.parametrize("bad", [7, -1, 1.5, "x", True])
    def test_out_of_range_stm_group_is_fatal(self, groups_file, bad):
        groups_file({"stm_coarse_group": bad})
        with pytest.raises(mg.MotorGroupConfigError):
            mg.stm_coarse_group()

    def test_bad_entries_are_dropped_and_reported_never_repaired(self, groups_file):
        bad = {"stm_coarse_group": 0, "groups": [
            {"group": 0, "name": "dup STM", "role": "mirror", "axes": {"X": "a"},
             "max_steps_per_cmd": 10, "freq_hz": 1, "amp_v": 1},             # = STM group
            {"group": 6, "name": "g7", "role": "mirror", "axes": {"X": "a"},
             "max_steps_per_cmd": 10, "freq_hz": 1, "amp_v": 1},             # out of 0..5
            {"group": 1, "name": "no drive", "role": "mirror", "axes": {"X": "a"},
             "max_steps_per_cmd": 10},                                       # no freq/amp
            {"group": 2, "name": "stm role", "role": "stm_coarse", "axes": {"X": "a"},
             "max_steps_per_cmd": 10, "freq_hz": 1, "amp_v": 1},
            {"group": 3, "name": "hot", "role": "mirror", "axes": {"X": "a"},
             "max_steps_per_cmd": 10, "freq_hz": 1, "amp_v": 500},           # > 400 V
            {"group": 4, "name": "ok", "role": "mirror", "axes": {"W": "a"},
             "max_steps_per_cmd": 10, "freq_hz": 1, "amp_v": 1},             # axis W
        ]}
        groups_file(bad)
        cfg = mg.load_motor_groups()
        assert cfg.groups == () and len(cfg.errors) == 6
        assert not cfg.fatal and mg.stm_coarse_group() == 0

    def test_duplicate_group_numbers_drop_both(self, groups_file):
        g = dict(GROUPS["groups"][0])
        groups_file({"stm_coarse_group": 0, "groups": [g, dict(g, name="again")]})
        assert mg.load_motor_groups().groups == ()

    def test_optical_lookup_refuses_stm_disabled_and_unknown(self, groups_file):
        groups_file(GROUPS)
        with pytest.raises(mg.MotorGroupConfigError, match="STM"):
            mg.optical_group(0)
        with pytest.raises(mg.MotorGroupConfigError, match="disabled"):
            mg.optical_group(3)
        with pytest.raises(mg.MotorGroupConfigError, match="no optical motor group"):
            mg.optical_group("Mirror Z")

    def test_edits_are_picked_up_without_a_restart(self, groups_file):
        p = groups_file({"stm_coarse_group": 1})
        assert mg.stm_coarse_group() == 1
        import os, time as _t

        p.write_text(json.dumps({"stm_coarse_group": 4}), encoding="utf-8")
        os.utime(p, (_t.time() + 5, _t.time() + 5))
        assert mg.stm_coarse_group() == 4

    def test_repo_templates_are_valid(self):
        repo = Path(__file__).resolve().parents[5]
        for name in ("nanonis_motor_groups.json",):
            p = repo / "config" / name
            cfg = mg.parse_motor_groups(json.loads(p.read_text(encoding="utf-8")),
                                        source=name)
            assert not cfg.fatal, (name, cfg.fatal)
            assert cfg.stm_coarse_group == 0


# ── NanonisMotorGroupMove ───────────────────────────────────────────────────

def _move(ctx, **kw):
    from mast.skills.builtins.optics_motor_groups import NanonisMotorGroupMove

    params = {"group": "1", "axis": "mirror_x", "direction": "+", "steps": 120}
    params.update(kw)
    return NanonisMotorGroupMove().execute(ctx, params)


class TestGroupMove:
    def test_happy_path_writes_verifies_steps_in_chunks_and_restores(self, groups_file):
        groups_file(GROUPS)
        ctx = Controller()
        res = _move(ctx)
        assert res.success, res.error
        sets = ctx.verbs("Motor_FreqAmpSet")
        assert sets[0][1] == (1000.0, 30.0, 0)                 # configured drive, axis 0 = all
        assert sets[-1][1] == (300.0, 200.0, 0)                # restored what was read
        # §10.3: the restore is an ordinary write — no abort-gate escape hatch
        assert all(s[2]["allow_on_abort"] is False for s in sets)
        moves = ctx.verbs("Motor_StartMove")
        assert [m[1] for m in moves] == [(0, 50, 1, 1), (0, 50, 1, 1), (0, 20, 1, 1)]
        assert all(m[2]["recv_timeout_s"] and m[2]["recv_timeout_s"] > 5 for m in moves)
        # every StartMove came after the readback that verified the write
        first_move = ctx.calls.index(moves[0])
        assert any(c[0] == "Motor_FreqAmpGet" for c in ctx.calls[ctx.calls.index(sets[0]):first_move])
        assert ctx.drive[0] == (300.0, 200.0)
        assert res.data["steps_done"] == 120 and res.data["drive"]["restored"]["verified"]
        assert res.data["freq_amp_restored"] is True
        assert res.data["position_feedback"] is False        # steps commanded, not a position
        assert mg.odometer() == {"1/X": 120}

    @pytest.mark.parametrize("ceiling,why", [(None, "尚未声明"), (20.0, "超过本机声明上限")])
    def test_write_needs_the_stm_rig_ceiling(self, groups_file, ceiling, why):
        """A global drive lands on the STM stack too, so coarse_drive.authorize
        gates the write — undeclared or too low means nothing is written."""
        from mast.core import coarse_drive

        groups_file(GROUPS)
        coarse_drive.set_declaration({} if ceiling is None else {"max_amplitude_v": ceiling})
        for dry in (False, True):
            ctx = Controller()
            res = _move(ctx, dry_run=dry)
            assert not res.success and why in res.error and "write_and_restore" in res.error
            assert not ctx.verbs("Motor_FreqAmpSet") and not ctx.verbs("Motor_StartMove")
        # no write needed ⇒ the ceiling is not consulted
        ctx = Controller(drive=(1000.0, 30.0))
        assert _move(ctx, steps=10).success

    def test_no_write_when_the_drive_already_matches(self, groups_file):
        groups_file(GROUPS)
        ctx = Controller(drive=(1000.0, 30.0))
        assert _move(ctx, steps=10).success
        assert not ctx.verbs("Motor_FreqAmpSet")

    @pytest.mark.parametrize("kw,why", [
        ({"group": "0"}, "STM"),
        ({"group": "Mirror Z"}, "no optical motor group"),
        ({"axis": "lens_focus"}, "no axis"),
        ({"steps": 201}, "单次上限"),
    ])
    def test_refusals_before_anything_is_written(self, groups_file, kw, why):
        groups_file(GROUPS)
        ctx = Controller()
        res = _move(ctx, **kw)
        assert not res.success and why in res.error
        assert not ctx.verbs("Motor_FreqAmpSet") and not ctx.verbs("Motor_StartMove")

    def test_tip_must_be_withdrawn_by_default(self, groups_file):
        groups_file(GROUPS)
        for ctx in (Controller(withdrawn=False), Controller(state=False),
                    Controller(scan=True)):
            res = _move(ctx)
            assert not res.success
            assert not ctx.verbs("Motor_StartMove")

    def test_verify_only_refuses_a_mismatch_without_writing(self, groups_file):
        groups_file(GROUPS)
        ctx = Controller(drive=(442.0, 200.0))           # the lens group wants 300 Hz / 200 V
        res = _move(ctx, group="Lens", axis="lens_focus", direction="+", steps=10)
        assert not res.success and "verify_only" in res.error
        assert not ctx.verbs("Motor_FreqAmpSet") and not ctx.verbs("Motor_StartMove")

    def test_verify_only_steps_on_a_match_and_never_writes(self, groups_file):
        groups_file(GROUPS)
        ctx = Controller(drive=(442.0, 200.0))
        ctx.drive[3] = (300.0, 200.0)                    # the moved axis (Z) is what counts
        res = _move(ctx, group="Lens", axis="lens_focus", direction="+", steps=10)
        assert res.success, res.error
        assert not ctx.verbs("Motor_FreqAmpSet")
        assert res.data["drive"]["policy"] == "verify_only"
        assert res.data["drive"]["after"] == [300.0, 200.0]

    def test_unknown_drive_policy_drops_the_group(self, groups_file):
        g = dict(GROUPS["groups"][1], drive_policy="yolo")
        groups_file({"stm_coarse_group": 0, "groups": [g]})
        cfg = mg.load_motor_groups()
        assert cfg.groups == () and "drive_policy" in cfg.errors[0]

    def test_requires_none_skips_the_tip_check(self, groups_file):
        groups_file(GROUPS)
        ctx = Controller(withdrawn=False, drive=(300.0, 200.0))
        res = _move(ctx, group="Lens", axis="lens_focus", direction="-", steps=30)
        assert res.success, res.error
        assert [m[1] for m in ctx.verbs("Motor_StartMove")] == [(5, 30, 2, 1)]

    def test_unreadable_drive_refuses(self, groups_file):
        groups_file(GROUPS)
        ctx = Controller(fail={"Motor_FreqAmpGet": None})
        res = _move(ctx)
        assert not res.success and "读不到" in res.error
        assert not ctx.verbs("Motor_StartMove")

    def test_readback_mismatch_after_write_refuses_and_restores(self, groups_file):
        groups_file(GROUPS)
        ctx = Controller()
        real = ctx.safe_call

        def sticky(method, *args, **kw):          # the write "succeeds" but nothing changes
            if method == "Motor_FreqAmpSet" and args[:2] == (1000.0, 30.0):
                ctx.calls.append((method, args, kw))
                return NanonisCallRecord(method=method, args=args, return_value=("", b"", []))
            return real(method, *args, **kw)

        ctx.safe_call = sticky
        res = _move(ctx)
        assert not res.success and "不符" in res.error
        assert not ctx.verbs("Motor_StartMove")
        assert res.data["drive"]["restored"]["verified"] is True

    def test_abort_between_chunks_stops_reports_unrestored_and_latches(self, groups_file):
        """§10.3: after an abort the safe_call gate refuses the
        restore write. The skill must say so — with the values to put back — and
        latch every coarse move until a readback shows the drive is back."""
        from mast.skills.builtins.motor import MotorMove

        groups_file(GROUPS)
        ctx = Controller(abort_after_moves=1)
        res = _move(ctx)
        assert not res.success and "aborted" in res.error
        assert len(ctx.verbs("Motor_StartMove")) == 1
        assert ctx.verbs("Motor_StopMove")
        assert res.data["steps_done"] == 50
        assert ctx.drive[0] == (1000.0, 30.0)                  # the gate refused the restore
        assert "未还原" in res.error and "300 Hz / 200 V" in res.error
        assert res.data["drive"]["restored"]["verified"] is False
        # named fields with the values to put back, and an alert
        assert res.data["freq_amp_written"] is True and res.data["freq_amp_restored"] is False
        assert res.data["freq_amp_original"]["0"] == {"frequency_hz": 300.0, "amplitude_v": 200.0}
        from mast.core import diagnostics

        alerts = [e for e in diagnostics.recent(subject="NanonisMotorGroupMove")
                  if e.get("alert") == "motor_drive_not_restored"]
        assert alerts and alerts[0]["aborted"] is True
        assert mg.drive_latch() is not None
        stm = Controller(drive=(1000.0, 30.0))
        assert not MotorMove().execute(stm, {"direction": "x+", "steps": 1}).success

    def test_failed_step_stops_and_restores(self, groups_file):
        groups_file(GROUPS)
        n = {"i": 0}

        def second(args):
            n["i"] += 1
            return n["i"] == 2

        ctx = Controller(fail={"Motor_StartMove": second})
        res = _move(ctx)
        assert not res.success and "after 50 steps" in res.error
        assert ctx.verbs("Motor_StopMove") and ctx.drive[0] == (300.0, 200.0)

    def test_per_axis_drive_is_restored_per_axis(self, groups_file):
        groups_file(GROUPS)
        ctx = Controller()
        ctx.drive[3] = (300.0, 150.0)              # Z set differently by the operator
        assert _move(ctx, steps=10).success
        assert ctx.drive == {0: (300.0, 200.0), 1: (300.0, 200.0), 2: (300.0, 200.0),
                             3: (300.0, 150.0)}

    def test_unverified_restore_latches_every_coarse_move(self, groups_file):
        from mast.skills.builtins.motor import MotorMove

        groups_file(GROUPS)
        ctx = Controller(fail={"Motor_FreqAmpSet": lambda a: a[:2] == (300.0, 200.0)})
        res = _move(ctx, steps=10)
        assert not res.success and "锁住" in res.error
        assert mg.drive_latch() is not None
        # STM coarse moves are refused while the drive is not the STM one …
        stm = Controller(drive=(1000.0, 30.0))
        r = MotorMove().execute(stm, {"direction": "x+", "steps": 5})
        assert not r.success and not stm.verbs("Motor_StartMove")
        # … and released once a readback shows the drive is back
        ok = Controller(drive=(300.0, 200.0))
        assert MotorMove().execute(ok, {"direction": "x+", "steps": 5}).success
        assert mg.drive_latch() is None

    def test_the_token_is_held_through_write_step_and_restore(self, groups_file):
        """The STM coarse skills use the same drive and are kept apart
        from this skill only by the instrument token, so write → steps → restore
        must all run under it — one skill call, never split."""
        from mast.core.execution_context import ExecutionContext
        from mast.core.instrument_lock import instrument_lock
        from mast.core.registry import SkillRegistry
        from mast.skills.builtins.optics_motor_groups import NanonisMotorGroupMove

        groups_file(GROUPS)
        holders: list = []

        class Pool(Controller):
            def safe_call(self, method, *args, **kw):
                if method in ("Motor_FreqAmpSet", "Motor_StartMove"):
                    holders.append((method, (instrument_lock().snapshot() or {}).get("skill")))
                return super().safe_call(method, *args, **kw)

        from mast.skills.builtins.motor import SetMotorFreqAmp

        pool = Pool()
        reg = SkillRegistry()
        reg.register(NanonisMotorGroupMove)
        reg.register(SetMotorFreqAmp)
        # a drive write runs only where a person ran the skill (hard gate)
        ec = ExecutionContext(pool=pool, state=pool.state, registry=reg, owner="test",
                              approval_source="human")
        res = ec.run("NanonisMotorGroupMove", {"group": "1", "axis": "mirror_x",
                                               "direction": "+", "steps": 120})
        assert res.success, res.error
        assert [m for m, _ in holders] == (["Motor_FreqAmpSet"] + ["Motor_StartMove"] * 3
                                           + ["Motor_FreqAmpSet"])
        assert {h for _, h in holders} == {"NanonisMotorGroupMove"}
        assert instrument_lock().snapshot() is None


class TestDriveWritesGoThroughTheSkill:
    """test_coarse_drive_authority pins that Motor_FreqAmpSet is written only by
    SetMotorFreqAmp (where coarse_drive.authorize runs). write_and_restore
    therefore writes through ctx.run("SetMotorFreqAmp") and inherits its hard
    gate: on the autonomous path the write is refused before anything is sent."""

    def _ec(self, approval_source):
        from mast.core.execution_context import ExecutionContext
        from mast.core.registry import SkillRegistry
        from mast.skills.builtins.motor import SetMotorFreqAmp
        from mast.skills.builtins.optics_motor_groups import NanonisMotorGroupMove

        pool = Controller()
        reg = SkillRegistry()
        reg.register(NanonisMotorGroupMove)
        reg.register(SetMotorFreqAmp)
        return pool, ExecutionContext(pool=pool, state=pool.state, registry=reg, owner="test",
                                      approval_source=approval_source)

    def test_autonomous_path_refuses_the_write_and_moves_nothing(self, groups_file):
        groups_file(GROUPS)
        pool, ec = self._ec("auto")
        res = ec.run("NanonisMotorGroupMove", {"group": "1", "axis": "mirror_x",
                                               "direction": "+", "steps": 20})
        assert not res.success and "硬闸" in res.error and "SetMotorFreqAmp" in res.error
        assert not pool.verbs("Motor_FreqAmpSet") and not pool.verbs("Motor_StartMove")
        assert res.data["freq_amp_written"] is False and res.data["freq_amp_restored"] is None
        assert mg.drive_latch() is None             # nothing was written, nothing to latch

    def test_person_run_path_writes_steps_and_restores(self, groups_file):
        groups_file(GROUPS)
        pool, ec = self._ec("human")
        res = ec.run("NanonisMotorGroupMove", {"group": "1", "axis": "mirror_x",
                                               "direction": "+", "steps": 20})
        assert res.success, res.error
        assert [c[1] for c in pool.verbs("Motor_FreqAmpSet")] == [(1000.0, 30.0, 0),
                                                                  (300.0, 200.0, 0)]
        assert res.data["freq_amp_restored"] is True and pool.drive[0] == (300.0, 200.0)

    def test_verify_only_needs_no_write_on_either_path(self, groups_file):
        groups_file(GROUPS)
        pool, ec = self._ec("auto")
        pool.drive[3] = (300.0, 200.0)                # the lens group's configured drive
        res = ec.run("NanonisMotorGroupMove", {"group": "Lens", "axis": "lens_focus",
                                               "direction": "+", "steps": 10})
        assert res.success, res.error
        assert not pool.verbs("Motor_FreqAmpSet") and pool.verbs("Motor_StartMove")

    def test_the_latch_survives_a_restart_until_a_readback_clears_it(self, groups_file):
        """A restart does not put the drive back, so it must not forget the latch."""
        from mast.skills.builtins.motor import MotorMove

        groups_file(GROUPS)
        assert not _move(Controller(abort_after_moves=1)).success
        assert mg.latch_path().is_file()
        mg.reset_for_tests()                                  # ≈ a new process
        assert mg.drive_latch()["expected_amp_v"] == 200.0
        stm = Controller(drive=(1000.0, 30.0))
        assert not MotorMove().execute(stm, {"direction": "x+", "steps": 1}).success
        assert MotorMove().execute(Controller(), {"direction": "x+", "steps": 1}).success
        assert mg.drive_latch() is None and not mg.latch_path().exists()

    def test_a_retract_away_from_the_sample_is_never_blocked_by_the_latch(self, groups_file,
                                                                          retract_dir):
        """Contract §10.3: the safe direction always passes — with a WARNING
        in the log and in the result; every other direction stays refused. "Away"
        is the DECLARED retract direction (here z+), never the factory default."""
        from mast.skills.builtins.motor import MotorMove

        groups_file(GROUPS)
        retract_dir("z+")
        assert not _move(Controller(abort_after_moves=1)).success      # latched
        stm = Controller(drive=(1000.0, 30.0))
        res = MotorMove().execute(stm, {"direction": "z-retract", "steps": 5})
        assert res.success, res.error
        latch = res.data["drive_latch"]                # reason + expected + current drive
        assert "没能还原" in latch["message"] and "restore not verified" in latch["reason"]
        assert latch["drive_now"] == [1000.0, 30.0]
        assert (latch["expected_freq_hz"], latch["expected_amp_v"]) == (300.0, 200.0)
        assert stm.verbs("Motor_StartMove")[0][1] == (4, 5, 0, 1)
        for d in ("x+", "z-approach"):
            stm = Controller(drive=(1000.0, 30.0))
            res = MotorMove().execute(stm, {"direction": d, "steps": 1})
            assert not res.success and res.data["drive_latch"]["drive_now"] == [1000.0, 30.0]
            assert not stm.verbs("Motor_StartMove")
        assert mg.drive_latch() is not None          # a retract does not clear it
        mg.clear_drive_latch()

    def test_the_exemption_follows_the_declared_retract_direction(self, groups_file,
                                                                 retract_dir):
        """MotorMove maps z-retract onto the DECLARED retract code: on
        a rig declared z− it sends 5, which is the away direction, so it passes the
        latch; declared z+ sends 4 and passes; z-approach (the other code) never
        does."""
        from mast.skills.builtins.motor import MotorMove

        groups_file(GROUPS)
        assert not _move(Controller(abort_after_moves=1)).success      # latched
        for declared, code in (("z-", 5), ("z+", 4)):
            retract_dir(declared)
            stm = Controller(drive=(1000.0, 30.0))
            res = MotorMove().execute(stm, {"direction": "z-retract", "steps": 5})
            assert res.success, res.error
            assert stm.verbs("Motor_StartMove")[0][1] == (code, 5, 0, 1)
            assert res.data["z_direction_basis"] == f"declared_{declared}"
            assert res.data["drive_latch"]["drive_now"] == [1000.0, 30.0]
            stm = Controller(drive=(1000.0, 30.0))
            res = MotorMove().execute(stm, {"direction": "z-approach", "steps": 1})
            assert not res.success and not stm.verbs("Motor_StartMove")
        mg.clear_drive_latch()

    def test_a_code_that_is_not_the_declared_one_is_never_exempt(self, retract_dir):
        """The exemption judges the code actually sent, not the label."""
        from mast.skills.builtins.motor import _retract_exemption

        retract_dir("z-")
        assert _retract_exemption(5) == (True, "")
        away, why = _retract_exemption(4)
        assert not away and "Z−" in why and "朝样品" in why

    def test_an_undeclared_retract_direction_gets_no_exemption(self, groups_file,
                                                               retract_dir):
        """``retract_motor_dir`` (coarse motor) and ``z_extend_sign``
        (piezo) are two separate conventions, and the factory ``z+`` is one of two
        answers — on a rig whose retract is Z− it names the approach. A rig that
        never declared its retract direction steps NO coarse Z under the latch."""
        from mast.core import instrument_profile as ip
        from mast.skills.builtins.motor import MotorMove

        groups_file(GROUPS)
        assert not _move(Controller(abort_after_moves=1)).success      # latched
        retract_dir(None)
        assert ip.get_retract_dir_code() == 4          # the factory reading still says "Z+"
        stm = Controller(drive=(1000.0, 30.0))
        res = MotorMove().execute(stm, {"direction": "z-retract", "steps": 5})
        assert not res.success and not stm.verbs("Motor_StartMove")
        assert "没声明粗动退针方向" in res.error and "设置 → 退针 → 退针方向" in res.error
        assert res.data["drive_latch"]["retract_exemption"].startswith("本机没声明")
        # without a latch the declaration does not matter — nothing to exempt from
        mg.clear_drive_latch()
        assert MotorMove().execute(Controller(), {"direction": "z-retract", "steps": 1}).success

    def test_retract_direction_reads_tri_state(self, retract_dir):
        """Undeclared ⇒ None while the factory reading answers 4 (the two readings
        really diverge there); declared ⇒ both agree, or a second source of truth
        has been made."""
        from mast.core import instrument_profile as ip

        retract_dir(None)
        assert ip.retract_dir_code_or_none() is None and ip.get_retract_dir_code() == 4
        for declared, code in (("z+", 4), ("z-", 5)):
            retract_dir(declared)
            assert ip.retract_dir_code_or_none() == code == ip.get_retract_dir_code()

    def test_an_unreadable_latch_file_stays_latched(self, groups_file):
        from mast.skills.builtins.motor import MotorMove

        groups_file(GROUPS)
        mg.latch_path().write_text("{broken", encoding="utf-8")
        mg.reset_for_tests()
        ctx = Controller()
        res = MotorMove().execute(ctx, {"direction": "x+", "steps": 1})
        assert not res.success and "读不出来" in res.error
        assert not ctx.verbs("Motor_StartMove")
        mg.clear_drive_latch()

    def test_coarse_drive_readback_alone_is_not_fail_safe_the_latch_is(self, groups_file):
        """Whether the STM side's coarse_drive readback refuses
        to step when the drive differs from the STM value. It does not in general:
        it refuses only an amplitude ABOVE the declared ceiling (or no
        declaration); a frequency mismatch is a warning, and MotorMove does not
        read back at all. What blocks the STM coarse skills after an unrestored
        optical-group write is the drive latch."""
        from mast.core import coarse_drive
        from mast.skills.builtins.motor import MotorMove

        groups_file(GROUPS)
        # the drive an aborted write_and_restore leaves behind (1000 Hz / 30 V):
        ok, _why = coarse_drive.readback_matches(30.0, 1000.0)
        assert ok                                             # under the 250 V ceiling: passes
        coarse_drive.set_declaration({"max_amplitude_v": 250.0, "expected_frequency_hz": 300.0})
        ok, why = coarse_drive.readback_matches(30.0, 1000.0)
        assert ok and "不符" in why                            # wrong frequency: a warning only
        assert not coarse_drive.readback_matches(260.0, 300.0)[0]   # above the ceiling: refused
        # … so the latch is what keeps the STM coarse skills from stepping
        assert not _move(Controller(abort_after_moves=1)).success
        stm = Controller(drive=(1000.0, 30.0))
        res = MotorMove().execute(stm, {"direction": "x+", "steps": 1})
        assert not res.success and "没能还原" in res.error and not stm.verbs("Motor_StartMove")
        mg.clear_drive_latch()

    def test_dry_run_writes_nothing(self, groups_file):
        groups_file(GROUPS)
        ctx = Controller()
        res = _move(ctx, dry_run=True)
        assert res.success and res.data["dry_run"] and res.data["drive"]["write_needed"]
        assert not ctx.verbs("Motor_FreqAmpSet") and not ctx.verbs("Motor_StartMove")

    def test_list_reports_config_drive_and_semantics(self, groups_file):
        from mast.skills.builtins.optics_motor_groups import ListNanonisMotorGroups

        groups_file(GROUPS)
        res = ListNanonisMotorGroups().execute(Controller(), {})
        assert res.success and res.data["stm_coarse_group"] == 0
        assert [g["group"] for g in res.data["groups"]] == [1, 2, 3]
        assert res.data["drive_now"]["0"] == {"frequency_hz": 300.0, "amplitude_v": 200.0}
        assert "module-global" in res.data["semantics"]["freq_amp"]

    def test_list_fails_loudly_on_a_broken_file(self, groups_file):
        from mast.skills.builtins.optics_motor_groups import ListNanonisMotorGroups

        groups_file("[1,")
        res = ListNanonisMotorGroups().execute(Controller(), {"read_drive": False})
        assert not res.success and "无法使用" in res.error


# ── approaching with a latched drive (contract §10.3, approach.py) ────

class TestApproachUnderTheLatch:
    @pytest.mark.parametrize("skill", ["AutoApproach", "ApproachTip"])
    def test_a_latched_drive_refuses_the_approach_before_anything_moves(self, groups_file,
                                                                        skill):
        from mast.skills.builtins import approach

        groups_file(GROUPS)
        assert not _move(Controller(abort_after_moves=1)).success      # latched
        ctx = Controller(drive=(1000.0, 30.0))
        res = getattr(approach, skill)().execute(ctx, {})
        assert not res.success and "没能还原" in res.error
        assert [c[0] for c in ctx.calls] == ["Motor_FreqAmpGet"]       # the readback only
        mg.clear_drive_latch()

    @pytest.mark.parametrize("skill", ["AutoApproach", "ApproachTip"])
    def test_an_unreadable_latch_file_refuses_the_approach(self, groups_file, skill):
        from mast.skills.builtins import approach

        groups_file(GROUPS)
        mg.latch_path().write_text("{broken", encoding="utf-8")
        mg.reset_for_tests()
        ctx = Controller()
        res = getattr(approach, skill)().execute(ctx, {})
        assert not res.success and "读不出来" in res.error and ctx.calls == []
        mg.clear_drive_latch()


# ── STM coarse call sites: group 0 unchanged, configured group honoured ─────

class TestStmCallSites:
    def test_motor_move(self, groups_file):
        from mast.skills.builtins.motor import MotorMove

        for body, group in ((None, 0), ({"stm_coarse_group": 2}, 2)):
            groups_file(body)
            ctx = Controller()
            assert MotorMove().execute(ctx, {"direction": "y-", "steps": 7}).success
            assert ctx.verbs("Motor_StartMove")[0][1] == (3, 7, group, 1)

    def test_motor_move_refuses_on_a_broken_file(self, groups_file):
        from mast.skills.builtins.motor import MotorMove

        groups_file("nope")
        ctx = Controller()
        res = MotorMove().execute(ctx, {"direction": "x+", "steps": 1})
        assert not res.success and not ctx.verbs("Motor_StartMove")

    def test_motor_get_pos(self, groups_file):
        from mast.skills.builtins.motor import MotorGetPos

        for body, group in ((None, 0), ({"stm_coarse_group": 4}, 4)):
            groups_file(body)
            ctx = Controller()
            res = MotorGetPos().execute(ctx, {})
            assert ctx.verbs("Motor_PosGet")[0][1] == (group, 500)
            # PosGet errors (with [0, 0, 0] in the reply): say "no position", never 0
            assert not res.success and "无位置反馈" in res.error
            assert res.data == {"position_feedback": False, "group": group}

    def test_closed_loop_defaults_to_the_stm_group_and_refuses_others(self, groups_file):
        from mast.skills.builtins.motor import MotorMoveClosedLoop

        groups_file(None)
        base = {"target_x_m": 1e-6, "target_y_m": 0.0, "target_z_m": 0.0}
        ctx = Controller()
        assert MotorMoveClosedLoop().execute(ctx, base).success
        assert ctx.verbs("Motor_StartClosedLoop")[0][1][-1] == 0
        ctx = Controller()
        assert MotorMoveClosedLoop().execute(ctx, {**base, "group": 0}).success
        ctx = Controller()
        res = MotorMoveClosedLoop().execute(ctx, {**base, "group": 1})
        assert not res.success and "NanonisMotorGroupMove" in res.error
        assert not ctx.verbs("Motor_StartClosedLoop")
        groups_file({"stm_coarse_group": 1})
        ctx = Controller()
        assert MotorMoveClosedLoop().execute(ctx, base).success
        assert ctx.verbs("Motor_StartClosedLoop")[0][1][-1] == 1

    def test_selfcheck_and_preflight_probe_the_stm_group(self, groups_file):
        from mast.skills.builtins.coarse_selfcheck import CoarseMotionSelfCheck
        from mast.skills.composite._preflight import PROBE_MOTOR

        groups_file({"stm_coarse_group": 2})
        ctx = Controller()
        out = CoarseMotionSelfCheck()._closed_loop(ctx, [])
        assert ctx.verbs("Motor_PosGet")[0][1] == (2, 500) and out["group"] == 2
        ctx = Controller()
        PROBE_MOTOR[0](ctx)
        assert ctx.verbs("Motor_PosGet")[0][1] == (2, 500)
        groups_file("broken")                    # the probe is a read: falls back to 0
        ctx = Controller()
        PROBE_MOTOR[0](ctx)
        assert ctx.verbs("Motor_PosGet")[0][1] == (0, 500)
