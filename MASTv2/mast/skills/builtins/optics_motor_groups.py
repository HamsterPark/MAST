"""Nanonis optical motor groups — mirrors, beam deflection and lenses on the PMD4.

2 skills: ListNanonisMotorGroups (AUTO), NanonisMotorGroupMove (CONFIRM).

The STM coarse group is NOT reachable here, and the STM coarse skills cannot
reach an optical group (``motor_groups.stm_coarse_group``): the two paths share
the controller and nothing else — not the guards, not the voltage locks, not the
judgement of when a move is safe. What this path has instead (contract §9):

* **only groups declared in** ``nanonis_motor_groups.json`` (role ≠ STM), a
  per-group cap on steps per command, and abort checked between chunks;
* **drive values only from config, verified before stepping.** ``Motor_FreqAmp*``
  carry no group argument. On the simulator they are module-global, but a real
  session may keep amplitude/frequency per group, so on the PMD4 a write most
  likely lands on the group selected in the panel — maybe the STM one. Each
  group therefore declares a ``drive_policy``:

  - ``verify_only`` (default): never write; step only when the moved axis reads
    back the configured value, otherwise refuse and send the operator to the
    Nanonis panel;
  - ``write_and_restore`` (only where the drive is shown to be global on that
    rig): read the drive, write the configured value, verify, step, and in
    ``finally`` restore what was read and verify again. Both writes go through
    ``SetMotorFreqAmp`` (``ctx.run``) — the only place the coarse drive is
    written — so its locks hold: ``coarse_drive.authorize`` against the STM rig
    ceiling (a global drive reaches the STM stack too), and the hard gate that
    refuses drive writes on the autonomous path: this policy runs only where a
    person ran the skill; an agent gets a refusal before anything is written.
    An unverified restore latches :func:`motor_groups.latch_drive_not_restored`,
    which blocks every coarse move toward or along the sample until a readback
    shows the drive is back. After an abort ``ctx.run`` refuses the restore and
    the skill says so, with the values to put back (§10.3);
* **tip state per group** (``requires``): ``tip_withdrawn`` (default — stick-slip
  shakes the head), ``z_feedback_on``, or ``none``. An unreadable state refuses;
* ``Motor_StopMove`` has no group argument and stops whatever moves — it is the
  emergency path (StopMotor, token-free) and the abort path here;
* the simulator accepted group numbers 6 and 7 without complaint, so the group
  number is validated in config (0–5), not left to Nanonis;
* ``Motor_PosGet`` answers "Cannot access Motor Control Module… may not be
  supported" with ``[0, 0, 0]`` on the simulator (and on a single-group PMD rig), so
  there is no position: the skill keeps an odometer of commanded steps.
"""

from __future__ import annotations

import logging
from typing import Any

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.i18n import tr
from mast.instruments import motor_groups as mg
from mast.instruments.base import InstrumentError
from mast.skills.base import BaseSkill
from mast.skills.builtins.optics_common import (
    abort_probe,
    fail,
    stm_snapshot,
    tip_requirement_refusal,
)

logger = logging.getLogger(__name__)

__all__ = ["ListNanonisMotorGroups", "NanonisMotorGroupMove", "read_drive", "drive_close",
           "check_drive_latch", "drive_latch_status"]

#: same tolerance as the STM coarse readback (coarse_drive.READBACK_REL_TOL)
_REL_TOL = 0.02


def _parse(rec) -> tuple[float | None, float | None]:
    from mast.skills.builtins.motor import _parse_freq_amp

    if rec is None or getattr(rec, "error", ""):
        return None, None
    return _parse_freq_amp(rec.return_value)


def read_drive(context, calls: list, axes=(0, 1, 2, 3)) -> dict[int, tuple[float, float]] | None:
    """``{axis: (freq_hz, amp_v)}`` via Motor_FreqAmpGet, or None if any read fails."""
    out: dict[int, tuple[float, float]] = {}
    for a in axes:
        rec = context.safe_call("Motor_FreqAmpGet", a)
        calls.append(rec)
        f, amp = _parse(rec)
        if f is None or amp is None:
            return None
        out[a] = (float(f), float(amp))
    return out


def drive_close(a: tuple[float, float] | None, b: tuple[float, float] | None) -> bool:
    if a is None or b is None:
        return False
    for x, y in zip(a, b):
        if abs(x - y) > _REL_TOL * max(abs(x), abs(y), 1e-9):
            return False
    return True


def _tip_check(requires: str, snap: dict) -> str:
    """Refusal text for the group's tip-state policy, or '' (the same judgement
    the local optical stages use for their ``tip_requirement``)."""
    return tip_requirement_refusal(requires, snap, what=tr("该组", "this group"))


_AXIS_NAMES = {0: "all", 1: "x", 2: "y", 3: "z"}


def _set_drive(context, freq_hz: float, amp_v: float, axis: int) -> Any:
    """Write the drive through ``SetMotorFreqAmp`` — the one place in the tree
    that writes ``Motor_FreqAmpSet`` (pinned by test_coarse_drive_authority), so
    every lock on the coarse drive holds here too: ``coarse_drive.authorize``
    (rig ceiling) inside the skill, and, as a sub-step, the hard gate that keeps
    drive writes off the autonomous path — write_and_restore runs only where a
    person ran it. Needs an ExecutionContext (``ctx.run``)."""
    run = getattr(context, "run", None)
    if not callable(run):
        return SkillResult(skill_name="SetMotorFreqAmp", success=False,
                           error="drive writes go through SetMotorFreqAmp and need an "
                                 "ExecutionContext (ctx.run)")
    return run("SetMotorFreqAmp", {"frequency_hz": float(freq_hz), "amplitude_v": float(amp_v),
                                   "axis": _AXIS_NAMES[int(axis)]})


def _alert(subject: str, reason: str, **fields: Any) -> None:
    """The operator-facing alert: a line in the diagnostics ledger (诊断面板,
    ``artifacts/diagnostics/refusals.jsonl``) — the same channel the auto-approval
    notices use. Never raises."""
    try:
        from mast.core.diagnostics import record

        record("note", subject, reason, alert="motor_drive_not_restored", **fields)
    except Exception:  # noqa: BLE001 - an alert that failed must not hide the failure
        logger.debug("motor-group alert not recorded", exc_info=True)


def drive_latch_status(context, calls: list) -> tuple[str, dict | None]:
    """``(refusal_text, details)`` while a restore is unverified, ``("", None)``
    otherwise. The drive is re-read first: back to the expected value ⇒ the
    latch is cleared here. ``details`` = reason, the expected drive and the
    drive read now (None when unreadable) — what a caller that is allowed to
    go on anyway (a retract away from the sample) must report."""
    latch = mg.drive_latch()
    if not latch:
        return "", None
    info: dict[str, Any] = {"reason": latch.get("reason"), "since": latch.get("since"),
                            "expected_freq_hz": latch.get("expected_freq_hz"),
                            "expected_amp_v": latch.get("expected_amp_v"), "drive_now": None}
    if latch.get("expected_freq_hz") is None or latch.get("expected_amp_v") is None:
        return (tr(f"粗动驱动锁存记录读不出来（{latch.get('reason')}）—— 不知道该把驱动核对成什么值，"
                "**所有粗动（STM 与光学组）一律拒绝**。请在 Nanonis Motor Control 面板上人工核对"
                f"STM 组的频率/幅度，确认无误后删除 {latch.get('path') or mg.latch_path()}。",
                   f"cannot read the coarse-drive latch record ({latch.get('reason')}) - do not know what value to verify the drive against, "
                   "so **all coarse motion (STM and optical groups) is refused**. Check the STM group's frequency/amplitude by hand in the Nanonis Motor Control panel, "
                   f"then delete {latch.get('path') or mg.latch_path()} once it is correct.")), info
    now = read_drive(context, calls, axes=(0,))
    info["drive_now"] = list(now[0]) if now else None
    want = (latch["expected_freq_hz"], latch["expected_amp_v"])
    if now is not None and drive_close(now[0], want):
        mg.clear_drive_latch()
        return "", None
    return (tr(f"粗动驱动值没能还原（期望 {want[0]:g} Hz / {want[1]:g} V，现在读到 "
            f"{now[0] if now else '读不到'}；原因：{latch.get('reason')}）—— 在有人把 Nanonis "
            "Motor Control 面板上的频率/幅度设回去之前，**所有粗动（STM 与光学组）一律拒绝**。",
               f"coarse drive values were not restored (expected {want[0]:g} Hz / {want[1]:g} V, read now "
               f"{now[0] if now else 'unreadable'}; reason: {latch.get('reason')}) - until someone sets the frequency/amplitude back in the Nanonis "
               "Motor Control panel, **all coarse motion (STM and optical groups) is refused**.")), info


def check_drive_latch(context, calls: list) -> str:
    """The refusal text of :func:`drive_latch_status` ('' when not latched)."""
    return drive_latch_status(context, calls)[0]


class ListNanonisMotorGroups(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ListNanonisMotorGroups", version="1.0.0", category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("列出 Nanonis 电机组配置（nanonis_motor_groups.json）：STM 粗动组号、每个光学组（反射镜 / "
                "Beam Deflection / 透镜）的组号、轴名、单次步数上限、针尖要求、配置的驱动频率与幅度，"
                "被丢弃的配置条目及原因，本次进程的步数里程（命令过的步数，不是位置），以及当前驱动读回。只读。",
                   "List the Nanonis motor group configuration (nanonis_motor_groups.json): the STM coarse group number, for each optical group (mirror / "
                   "Beam Deflection / lens) its group number, axis names, per-command step limit, tip requirement, configured drive frequency and amplitude, "
                   "discarded config entries and the reason, the step odometer of this process (steps commanded, not position), and the current drive read-back. Read-only.")),
            parameters=[ParameterSpec(name="read_drive", type="bool", required=False, default=True,
                                      description=tr("同时读回当前驱动频率/幅度（Motor_FreqAmpGet，只读）",
                                                     "Also read back the current drive frequency/amplitude (Motor_FreqAmpGet, read-only)"))],
            estimated_duration_s=0.3, composition_level=0,
            tags=["optics", "nanonis_motor_group", "inventory", "read", "status",
                  "optional-hardware"])

    def execute(self, context, params: dict) -> SkillResult:
        cfg = mg.load_motor_groups()
        data: dict[str, Any] = {**cfg.as_dict(), "odometer": mg.odometer(),
                                "drive_latch": mg.drive_latch(),
                                "semantics": {
                                    "freq_amp": ("module-global on the Mimea simulator; "
                                                 "may be stored per group in a real "
                                                 "session ini — see each group's "
                                                 "drive_policy"),
                                    "stop": "Motor_StopMove stops any group (StopMotor)",
                                    "position": "no feedback (Motor_PosGet unsupported)"}}
        calls: list = []
        if params.get("read_drive", True) and callable(getattr(context, "safe_call", None)):
            drive = read_drive(context, calls)
            data["drive_now"] = ({str(k): {"frequency_hz": v[0], "amplitude_v": v[1]}
                                  for k, v in drive.items()} if drive else None)
        if cfg.fatal:
            return SkillResult(skill_name="ListNanonisMotorGroups", success=False,
                               error=tr(f"{mg.CONFIG_FILENAME} 无法使用：{cfg.fatal}",
                                        f"{mg.CONFIG_FILENAME} cannot be used: {cfg.fatal}"),
                               data=data, nanonis_calls=calls)
        names = ", ".join(f"{g.group}:{g.name}({g.role})" for g in cfg.groups) or "none"
        return SkillResult(
            skill_name="ListNanonisMotorGroups", success=True, data=data, nanonis_calls=calls,
            summary=(f"STM coarse group {cfg.stm_coarse_group}; optical groups: {names}"
                     + (f"; {len(cfg.errors)} entr(ies) dropped" if cfg.errors else "")))


class NanonisMotorGroupMove(BaseSkill):
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="NanonisMotorGroupMove", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("让 Nanonis 的一个**光学**电机组（反射镜、Beam Deflection、透镜，见 ListNanonisMotorGroups）"
                "沿一个轴走 steps 步。只接受配置里标为光学用途的组，STM 粗动组一律拒绝；超过该组单次上限直接拒绝。"
                "驱动频率/幅度只用配置值：默认（verify_only）不写驱动，读回与配置一致才走步，不一致就拒绝并请操作员"
                "在 Nanonis 面板核对；配置为 write_and_restore 的组先写配置值、读回核对、走步，再还原原值并核对"
                "（驱动值经 SetMotorFreqAmp 写入：自主路径上被硬闸拒绝，只有人手动执行时才会写）。"
                "按该组的 requires 检查针尖（默认必须已退针）。分块走步，块间检查中止；急停请用 StopMotor。"
                "没有位置反馈，返回的是命令过的步数。dry_run 只做检查、不写任何东西。",
                   "Move one **optical** Nanonis motor group (mirror, Beam Deflection, lens; see ListNanonisMotorGroups) "
                   "by `steps` steps along one axis. Only groups marked for optical use in the config are accepted; STM coarse groups are always refused; exceeding the group's per-command limit is refused outright. "
                   "Drive frequency/amplitude come only from the config: by default (verify_only) the drive is not written, and the move runs only if the read-back matches the config; otherwise it is refused and the operator is asked "
                   "to check in the Nanonis panel. Groups configured as write_and_restore first write the configured values, verify the read-back, move, then restore the original values and verify "
                   "(drive values are written via SetMotorFreqAmp: refused by the hard gate on autonomous paths, written only when a human runs it). "
                   "The tip is checked against the group's `requires` (by default the tip must be withdrawn). Moves are chunked with an abort check between chunks; use StopMotor for an emergency stop. "
                   "There is no position feedback; the returned value is the number of steps commanded. dry_run only checks and writes nothing.")),
            parameters=[
                ParameterSpec(name="group", type="str", required=True,
                              description=tr("组号（0-based，与 Motor_StartMove 的 Group 一致）或组名",
                                             "Group number (0-based, same as Group in Motor_StartMove) or group name")),
                ParameterSpec(name="axis", type="str", required=True,
                              description=tr("轴：配置里的轴名（例如 mirror_x）或 X / Y / Z",
                                             "Axis: an axis name from the config (e.g. mirror_x) or X / Y / Z")),
                ParameterSpec(name="direction", type="str", required=True,
                              allowed_values=["+", "-"], description=tr("方向", "Direction")),
                ParameterSpec(name="steps", type="int", required=True, min_value=1,
                              max_value=10_000, description=tr("步数；该组的上限见 ListNanonisMotorGroups",
                                                               "Number of steps; see ListNanonisMotorGroups for the group's limit")),
                ParameterSpec(name="dry_run", type="bool", required=False, default=False,
                              description=tr("**试跑**：校验组/轴/步数/针尖与驱动读回，不写任何 Nanonis 参数、不走步",
                                             "**Dry run**: validates group/axis/steps/tip and the drive read-back, writes no Nanonis parameter and takes no step")),
            ],
            estimated_duration_s=3.0, composition_level=0,
            tags=["optics", "nanonis_motor_group", "alignment", "move", "write",
                  "optional-hardware"])

    def execute(self, context, params: dict) -> SkillResult:
        name = "NanonisMotorGroupMove"
        try:
            g = mg.optical_group(params["group"])
            letter = g.axis_letter(str(params["axis"]))
        except Exception as exc:  # noqa: BLE001
            return fail(name, exc)
        steps = int(params["steps"])
        direction = str(params["direction"])
        if steps > g.max_steps_per_cmd:
            return SkillResult(skill_name=name, success=False,
                               error=(tr(f"{steps} 步超过组 {g.group}（{g.name}）的单次上限 "
                                      f"{g.max_steps_per_cmd} —— 拒绝（不会拆分或截断）",
                                         f"{steps} steps exceeds the per-command limit of group {g.group} ({g.name}) "
                                         f"{g.max_steps_per_cmd} - refused (will not split or truncate)")))
        code = mg.DIRECTION_CODES[(letter, direction)]
        base = {"group": g.group, "group_name": g.name, "role": g.role,
                "axis": g.axes[letter], "nanonis_axis": letter, "direction": direction,
                "direction_code": code, "steps_requested": steps, "requires": g.requires}
        calls: list = []
        refusal = check_drive_latch(context, calls)
        if refusal:
            return SkillResult(skill_name=name, success=False, error=refusal, data=base,
                               nanonis_calls=calls)
        snap = stm_snapshot(context, fresh=True)
        why = _tip_check(g.requires, snap)
        if why:
            return SkillResult(skill_name=name, success=False, error=why,
                               data={**base, "stm": {k: snap.get(k) for k in (
                                   "available", "withdrawn", "z_controller_on", "scan_running")}},
                               nanonis_calls=calls)
        probe = abort_probe(context)
        if probe is not None and probe():
            return SkillResult(skill_name=name, success=False, data=base,
                               error="aborted by operator — nothing moved")
        orig = read_drive(context, calls)
        if orig is None:
            return SkillResult(skill_name=name, success=False, data=base, nanonis_calls=calls,
                               error=tr("读不到当前驱动频率/幅度（Motor_FreqAmpGet）—— 读不到就不走步",
                                        "cannot read the current drive frequency/amplitude (Motor_FreqAmpGet) - no move if it cannot be read"))
        want = (g.freq_hz, g.amp_v)
        axis_idx = {"X": 1, "Y": 2, "Z": 3}[letter]
        live = orig.get(axis_idx) or orig[0]
        base["drive"] = {"before": {str(k): list(v) for k, v in orig.items()},
                         "configured": list(want), "policy": g.drive_policy,
                         "axis_index": axis_idx}
        if g.drive_policy == "verify_only":
            need_set = False
            if not drive_close(live, want):
                base["drive"]["write_needed"] = False
                return SkillResult(
                    skill_name=name, success=False, data=base, nanonis_calls=calls,
                    error=(tr(f"驱动读回 {live[0]:g} Hz / {live[1]:g} V 与组 {g.group}（{g.name}）的配置 "
                           f"{want[0]:g} Hz / {want[1]:g} V 不符 —— verify_only：不写驱动、不走步。"
                           "请在 Nanonis Motor Control 面板选中该组并核对它的频率/幅度"
                           "（或把配置改成该组的实际值）后再试。",
                              f"drive read-back {live[0]:g} Hz / {live[1]:g} V does not match the config of group {g.group} ({g.name}) "
                              f"{want[0]:g} Hz / {want[1]:g} V - verify_only: no drive write, no move. "
                              "Select this group in the Nanonis Motor Control panel and check its frequency/amplitude "
                              "(or change the config to the group's actual values), then retry.")))
        else:
            need_set = not all(drive_close(v, want) for v in orig.values())
            if need_set:
                # The STM's rig ceiling binds this write too: where the drive is
                # global (the only rigs this policy is for) the value lands on the
                # STM stack as well — that is what makes it "global".
                from mast.core import coarse_drive

                ok, why = coarse_drive.authorize(g.amp_v, g.freq_hz, axis="all")
                base["drive"]["authorize"] = why
                if not ok:
                    return SkillResult(
                        skill_name=name, success=False, data=base, nanonis_calls=calls,
                        error=(tr(f"组 {g.group}（{g.name}）是 write_and_restore，要写 {g.freq_hz:g} Hz / "
                               f"{g.amp_v:g} V，但本机粗动幅度上限不放行 —— 驱动是全局的机器上这个值"
                               f"同样落在 STM 叠堆上。{why}（或把该组改成 verify_only，由操作员在"
                               "面板上设驱动）",
                                  f"group {g.group} ({g.name}) is write_and_restore and would write {g.freq_hz:g} Hz / "
                                  f"{g.amp_v:g} V, but this machine's coarse-amplitude limit does not allow it - the drive is global, so on this machine this value "
                                  f"would also reach the STM stack. {why} (or change the group to verify_only and let the operator "
                                  "set the drive in the panel)")))
        base["drive"]["write_needed"] = need_set
        if params.get("dry_run"):
            return SkillResult(skill_name=name, success=True, data={**base, "dry_run": True},
                               nanonis_calls=calls,
                               summary=(f"[DRY RUN] group {g.group} ({g.name}) {g.axes[letter]} "
                                        f"{direction}{steps} steps: checks passed, nothing written"))

        done = 0
        aborted = False
        error = ""
        wrote = False
        try:
            if need_set:
                try:
                    r = _set_drive(context, g.freq_hz, g.amp_v, 0)
                except Exception:
                    wrote = True        # unknown whether it was sent: restore and verify
                    raise
                sent = list(getattr(r, "nanonis_calls", None) or [])
                calls.extend(sent)
                # A write that was SENT may have landed even if it errored, so the
                # restore-and-verify runs; a refusal at a gate (the hard gate keeps
                # drive writes off the autonomous path) sent nothing to undo.
                wrote = any(getattr(c, "method", "") == "Motor_FreqAmpSet" for c in sent)
                if not getattr(r, "success", False):
                    raise InstrumentError(
                        tr(f"驱动写入没有执行（经 SetMotorFreqAmp）：{getattr(r, 'error', '')}",
                           f"drive write was not executed (via SetMotorFreqAmp): {getattr(r, 'error', '')}"))
                rb = read_drive(context, calls, axes=(0,))
                base["drive"]["after_write"] = list(rb[0]) if rb else None
                if rb is None or not drive_close(rb[0], want):
                    raise InstrumentError(
                        tr(f"驱动读回 {rb[0] if rb else '读不到'} 与配置 {want} 不符 —— 不走步",
                           f"drive read-back {rb[0] if rb else 'unreadable'} does not match the config {want} - no move"))
            while done < steps:
                if probe is not None and probe():
                    aborted = True
                    calls.append(context.safe_call("Motor_StopMove"))
                    break
                n = min(g.chunk_steps, steps - done)
                rec = context.safe_call("Motor_StartMove", code, n, g.group, 1,
                                        recv_timeout_s=n / g.freq_hz * 3.0 + 5.0)
                calls.append(rec)
                if rec.error:
                    calls.append(context.safe_call("Motor_StopMove"))
                    raise InstrumentError(f"Motor_StartMove (group {g.group}) failed after "
                                          f"{done} steps: {rec.error}")
                done += n
                mg.note_steps(g.group, letter, n if direction == "+" else -n)
        except Exception as exc:  # noqa: BLE001 - restore first, report after
            error = f"{type(exc).__name__}: {exc}" if not isinstance(exc, InstrumentError) else str(exc)
        finally:
            restored = None
            if wrote:
                restored = self._restore(context, calls, orig)
                base["drive"]["restored"] = restored
            elif done:
                after = read_drive(context, calls, axes=(axis_idx,))
                base["drive"]["after"] = list(after[axis_idx]) if after else None
        base.update({"steps_done": done, "odometer": mg.odometer(), "aborted": aborted,
                     # Motor_PosGet is unsupported here: there is no position, only
                     # the steps commanded (odometer) — never a 0 that looks like one
                     "position_feedback": False,
                     "freq_amp_written": wrote,
                     "freq_amp_restored": (bool(restored and restored.get("verified"))
                                           if wrote else None),
                     "freq_amp_original": {str(k): {"frequency_hz": v[0], "amplitude_v": v[1]}
                                           for k, v in orig.items()}})
        if restored is not None and not restored.get("verified"):
            f0, a0 = orig[0]
            mg.latch_drive_not_restored(
                expected=orig[0], last_read=restored.get("read"),
                reason=(f"{name} group {g.group}: restore not verified"
                        + (" (abort latched: ctx.run refuses the restore)" if aborted else "")))
            per_axis = len(set(orig.values())) > 1
            note = (tr(f"驱动频率/幅度**未还原**（应设回 {f0:g} Hz / {a0:g} V",
                       f"drive frequency/amplitude **not restored** (should be set back to {f0:g} Hz / {a0:g} V")
                    + (tr("；各轴原值不同，见 data.freq_amp_original", "; the original values differ per axis, see data.freq_amp_original") if per_axis else "")
                    + tr("）—— 已锁住所有粗动（STM 与光学组），请在 Nanonis Motor Control 面板上"
                    "设回后再试",
                         ") - all coarse motion (STM and optical groups) is locked; set it back in the Nanonis Motor Control panel "
                         "and retry"))
            logger.warning("%s: %s", name, note)
            _alert(name, note, group=g.group, aborted=aborted,
                   expected=list(orig[0]), read=restored.get("read"))
            error = (error + "; " if error else "") + note
        if aborted:
            return SkillResult(skill_name=name, success=False, data=base, nanonis_calls=calls,
                               error=(f"aborted by operator after {done}/{steps} steps "
                                      f"(stop sent)" + (f"; {error}" if error else "")))
        if error:
            return SkillResult(skill_name=name, success=False, data=base, nanonis_calls=calls,
                               error=error)
        return SkillResult(skill_name=name, success=True, data=base, nanonis_calls=calls,
                           summary=(f"group {g.group} ({g.name}) {g.axes[letter]} {direction}{done} "
                                    f"steps{' (drive written and restored)' if wrote else ''}"))

    @staticmethod
    def _restore(context, calls: list, orig: dict[int, tuple[float, float]]) -> dict:
        """Put back exactly what was read (per axis when the axes differed),
        through SetMotorFreqAmp like the write.

        No escape hatch: after an abort ``ctx.run`` refuses every sub-skill, so
        this restore is refused. That is the contract (§10.3): the
        result says "not restored" with the values to put back, and the drive
        latch blocks every coarse move until a readback shows them again.
        Whether to open a narrow exception for "restore the value this call
        recorded" is the user's decision, not this skill's."""
        # axis 0 means "all" on a write: put the common value back first, then
        # every axis that differed from it.
        plan = [(0, orig[0])] + [(a, v) for a, v in sorted(orig.items())
                                 if a != 0 and v != orig[0]]
        errs = []
        for axis, (f, amp) in plan:
            try:
                r = _set_drive(context, f, amp, axis)
            except Exception as exc:  # noqa: BLE001 - report, verify below
                errs.append(f"axis {axis}: {type(exc).__name__}: {exc}")
                continue
            calls.extend(getattr(r, "nanonis_calls", None) or [])
            if not getattr(r, "success", False):
                errs.append(f"axis {axis}: {getattr(r, 'error', '')}")
        now = read_drive(context, calls)
        verified = bool(now) and all(drive_close(now[a], orig[a]) for a in orig)
        return {"verified": verified, "read": ({str(k): list(v) for k, v in now.items()}
                                               if now else None), "errors": errs,
                "to_restore": {str(k): {"frequency_hz": v[0], "amplitude_v": v[1]}
                               for k, v in orig.items()}}
