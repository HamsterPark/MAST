"""Apply Z-controller parameters BY NAME — the agent never types the numbers.

The agent says ``ApplyZCtrlPreset('approach')``. Code looks the values up in the
place the operator maintains them, checks them, writes them, reads them back and
compares — in Python. There is no numeric argument anywhere in this path, so
there is nothing for a lost exponent to land in.

This exists because of 2026-08-03: asked for ``p_gain=3e-12``, an agent emitted
``3``. See ``mast.core.zctrl_presets`` for the resolution rules and
``docs/v2/fixes/2026-08-03-tool-call-number-corruption.md`` for the incident.

3 skills: ApplyZCtrlPreset (CONFIRM), CreateZCtrlPreset (DANGEROUS),
ListZCtrlPresets (AUTO).
"""

from __future__ import annotations

from mast.core.si_quantity import format_si
from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.core.zctrl_presets import (
    PresetRejected,
    available_names,
    get_presets,
    resolve,
    upsert_preset,
)
from mast.i18n import tr
from mast.skills.base import BaseSkill


class ApplyZCtrlPreset(BaseSkill):
    """Write a named set of Z-controller parameters to the instrument."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ApplyZCtrlPreset",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr(
                "按**参数组名**把 Z 控制器参数(P/I 增益 + 设定点)写进硬件。\n\n"
                "**这是设置 Z 参数的首选方式**,优先于 SetZCtrlGain / SetSetpoint:"
                "具体数值由代码从用户维护的存储里取出并写入,你只需要说用哪一组,"
                "不需要(也不应该)自己写出任何数字。\n\n"
                "常用组名:\n"
                "- `approach` —— 进针参数(来自仪器档案,用户填写)\n"
                "- `scan` —— 扫图参数(按当前扫描帧尺寸自动选档,与 ScanAt 同源)\n"
                "- 也可以直接用扫描档名(如 `atomic`)或自定义组名\n\n"
                "用 ListZCtrlPresets 查看当前可用的全部组名与数值。"
                "写入后会自动回读比对,不一致会报失败。",
                "Write the Z controller parameters (P/I gains + setpoint) to the hardware by "
                "**preset name**.\n\n"
                "**This is the preferred way to set Z parameters**, ahead of SetZCtrlGain / "
                "SetSetpoint: the concrete values are taken by code from the user-maintained store "
                "and written; you only say which group to use, and do not need to (and should not) "
                "write any numbers yourself.\n\n"
                "Common group names:\n"
                "- `approach` -- approach parameters (from the instrument profile, filled in by the user)\n"
                "- `scan` -- scan parameters (tier chosen automatically from the current scan frame "
                "size, same source as ScanAt)\n"
                "- a scan tier name (e.g. `atomic`) or a custom group name also works\n\n"
                "Use ListZCtrlPresets to see all currently available group names and values. "
                "After writing, the values are read back and compared automatically; a mismatch "
                "is reported as a failure.",
            ),
            parameters=[
                ParameterSpec(
                    name="preset",
                    type="str",
                    description=tr(
                        "参数组名,如 'approach' / 'scan' / 档名 / 自定义组名。"
                        "不确定有哪些就先调 ListZCtrlPresets。",
                        "Preset name, e.g. 'approach' / 'scan' / a tier name / a custom group name. "
                        "If unsure which exist, call ListZCtrlPresets first.",
                    ),
                    required=True,
                ),
            ],
            estimated_duration_s=3.0,
            composition_level=1,
            tags=["z", "gain", "setpoint", "preset", "write", "readback"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = params["preset"]
        try:
            resolved = resolve(name, context=context)
        except PresetRejected as exc:
            # The name is validated here rather than as a schema enum: the legal
            # set changes at runtime (the operator can add a tier or a group),
            # while a tool schema is frozen when the agent is built. An error
            # that lists what IS available beats an enum that goes stale.
            return SkillResult(
                skill_name="ApplyZCtrlPreset", success=False, error=str(exc),
            )

        calls: list = []
        applied: dict = {}

        # Sub-steps rather than direct safe_call: this is the same path ScanAt
        # uses, so the leaf skills' own bounds, SafetyGate checks and — since
        # 2026-08-03 — their write-then-read-back verification all apply. It also
        # keeps the (P, T, I) argument order known in exactly one place.
        gain_res = context.run("SetZCtrlGain", resolved.gain_params())
        calls.extend(getattr(gain_res, "nanonis_calls", []) or [])
        if not gain_res.success:
            return SkillResult(
                skill_name="ApplyZCtrlPreset",
                success=False,
                error=tr(
                    f"参数组 '{resolved.name}' 的增益写入失败: {gain_res.error}",
                    f"Writing the gains of preset '{resolved.name}' failed: {gain_res.error}",
                ),
                data={"preset": resolved.name,
                      "trace": resolved.trace_lines()},
                nanonis_calls=calls,
            )
        applied["gains"] = resolved.gain_params()

        if resolved.setpoint_a is not None:
            sp_res = context.run("SetSetpoint", {"setpoint_a": resolved.setpoint_a})
            calls.extend(getattr(sp_res, "nanonis_calls", []) or [])
            if not sp_res.success:
                return SkillResult(
                    skill_name="ApplyZCtrlPreset",
                    success=False,
                    error=tr(
                        f"参数组 '{resolved.name}' 的增益已写入,但设定点写入失败: "
                        f"{sp_res.error}",
                        f"The gains of preset '{resolved.name}' were written, but writing the "
                        f"setpoint failed: {sp_res.error}",
                    ),
                    data={"preset": resolved.name, "applied": applied,
                          "trace": resolved.trace_lines()},
                    nanonis_calls=calls,
                )
            applied["setpoint_a"] = resolved.setpoint_a

        # Every leaf write above verified itself by reading back and comparing in
        # Python. Re-reading here would only add round-trips and a second place
        # for the comparison rule to drift.
        summary = "\n".join(resolved.trace_lines())
        notes = "\n".join(resolved.notes)
        return SkillResult(
            skill_name="ApplyZCtrlPreset",
            success=True,
            data={
                "preset": resolved.name,
                "applied": applied,
                "trace": resolved.trace_lines(),
                "sources": resolved.sources,
                "notes": resolved.notes,
                "message": (
                    tr(f"已按参数组 '{resolved.name}' 设置 Z 控制器,写入后回读已确认:\n",
                       f"Z controller set from preset '{resolved.name}'; readback after writing confirmed:\n")
                    + summary + (f"\n{notes}" if notes else "")
                ),
            },
            nanonis_calls=calls,
        )


class CreateZCtrlPreset(BaseSkill):
    """Create or replace a custom Z-parameter group."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="CreateZCtrlPreset",
            version="1.0.0",
            category=SkillCategory.WRITE,
            # DANGEROUS: this writes a value the system will keep trusting. A
            # wrong number here is not one bad frame, it is every future
            # ApplyZCtrlPreset of this group — so it goes through the human
            # approval card, where the operator sees the ARGUMENTS AS PARSED
            # (hitl_bridge mirrors them unchanged) and can edit or reject before
            # anything is stored. That card is the post-creation review step.
            safety_level=SafetyLevel.DANGEROUS,
            description=tr(
                "新建(或覆盖)一个自定义 Z 参数组,之后可以用 ApplyZCtrlPreset 按名应用。"
                "\n\n"
                "**数值必须写成带 SI 前缀的字符串**,例如 p_gain='3p'、i_gain='180n'、"
                "setpoint_a='150p' —— 与 Nanonis 面板上的写法一致。"
                "**前缀不可省略**:裸数字(如 '3')会被直接拒绝。原因是量级一旦丢失,"
                "裸数字仍然是一个合法的数,错一万亿倍也没人发现;而前缀掉了就解析失败,"
                "你会立刻收到一次明确的拒绝。\n\n"
                "不要用本技能去改进针参数或扫图档位表 —— 那两套由用户在设置界面维护。"
                "本技能只管自定义组。",
                "Create (or overwrite) a custom Z parameter group, which can then be applied by "
                "name with ApplyZCtrlPreset.\n\n"
                "**Values must be written as strings with an SI prefix**, e.g. p_gain='3p', "
                "i_gain='180n', setpoint_a='150p' -- the same notation as on the Nanonis panel. "
                "**The prefix must not be omitted**: a bare number (such as '3') is rejected "
                "outright. The reason is that once the magnitude is lost, a bare number is still a "
                "valid number, and being wrong by a factor of 10^12 goes unnoticed; whereas a "
                "dropped prefix fails to parse and you immediately get a clear rejection.\n\n"
                "Do not use this skill to change the approach parameters or the scan tier table -- "
                "those two are maintained by the user in the settings UI. "
                "This skill only manages custom groups.",
            ),
            parameters=[
                ParameterSpec(
                    name="name",
                    type="str",
                    description=tr(
                        "参数组名。不能与保留名(approach / scan)或扫描档名重复。",
                        "Preset name. Must not duplicate a reserved name (approach / scan) or a "
                        "scan tier name.",
                    ),
                    required=True,
                ),
                ParameterSpec(
                    name="p_gain",
                    type="str",
                    description=tr(
                        "Z 比例增益,带 SI 前缀的字符串,单位米。例如 '3p' = 3p m。"
                        "典型 1p ~ 10p。**必须带前缀**。",
                        "Z proportional gain, a string with an SI prefix, in metres. E.g. '3p' = 3p m. "
                        "Typical 1p ~ 10p. **The prefix is required**.",
                    ),
                    unit="m",
                    required=True,
                ),
                ParameterSpec(
                    name="i_gain",
                    type="str",
                    description=tr(
                        "Z 积分增益,带 SI 前缀的字符串,单位米每秒。"
                        "例如 '180n' = 180n m/s。典型 10n ~ 1u。**必须带前缀**。",
                        "Z integral gain, a string with an SI prefix, in metres per second. "
                        "E.g. '180n' = 180n m/s. Typical 10n ~ 1u. **The prefix is required**.",
                    ),
                    unit="m/s",
                    required=True,
                ),
                ParameterSpec(
                    name="setpoint_a",
                    type="str",
                    description=tr(
                        "可选。电流设定点,带 SI 前缀的字符串,单位安培。"
                        "例如 '150p' = 150p A。留空表示这组不改设定点。",
                        "Optional. Current setpoint, a string with an SI prefix, in amperes. "
                        "E.g. '150p' = 150p A. Leave empty if this group should not change the setpoint.",
                    ),
                    unit="A",
                    required=False,
                ),
                ParameterSpec(
                    name="note",
                    type="str",
                    description=tr("可选。这组参数的用途说明,给用户看。",
                                   "Optional. A description of what this group is for, shown to the user."),
                    required=False,
                ),
                ParameterSpec(
                    name="overwrite",
                    type="bool",
                    description=tr("同名组已存在时是否替换。默认 False(存在则拒绝)。",
                                   "Whether to replace an existing group of the same name. Default False (refuse if it exists)."),
                    required=False,
                    default=False,
                ),
            ],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["z", "gain", "preset", "config"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        try:
            stored = upsert_preset(
                {
                    "name": params.get("name"),
                    "p_gain": params.get("p_gain"),
                    "i_gain": params.get("i_gain"),
                    "setpoint_a": params.get("setpoint_a"),
                    "note": params.get("note"),
                },
                overwrite=bool(params.get("overwrite", False)),
            )
        except PresetRejected as exc:
            return SkillResult(
                skill_name="CreateZCtrlPreset", success=False, error=str(exc),
            )

        # Echo back what STORAGE now holds, re-resolved — not the arguments that
        # came in. If anything were altered on the way through, this is where it
        # shows, and the operator reads a fact rather than a repetition of the
        # request.
        readback = resolve(stored["name"])
        return SkillResult(
            skill_name="CreateZCtrlPreset",
            success=True,
            data={
                "preset": stored["name"],
                "stored": stored,
                "trace": readback.trace_lines(),
                "message": (
                    tr(f"参数组 '{stored['name']}' 已保存。存储里现在的值是:\n",
                       f"Preset '{stored['name']}' saved. The values now in the store are:\n")
                    + "\n".join(readback.trace_lines())
                    + tr(f"\n\n用 ApplyZCtrlPreset('{stored['name']}') 应用它。",
                         f"\n\nApply it with ApplyZCtrlPreset('{stored['name']}').")
                ),
            },
        )


class ListZCtrlPresets(BaseSkill):
    """List every Z-parameter group that can be applied right now."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ListZCtrlPresets",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr(
                "列出当前可用的全部 Z 参数组名及其数值与来源。"
                "在调用 ApplyZCtrlPreset 之前不确定有哪些组时使用。",
                "List all currently available Z preset names with their values and sources. "
                "Use it when unsure which groups exist before calling ApplyZCtrlPreset.",
            ),
            parameters=[],
            estimated_duration_s=0.5,
            composition_level=0,
            tags=["z", "gain", "preset", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        rows: list[dict] = []
        for name in available_names():
            entry: dict = {"name": name}
            try:
                # No context: the 'scan' alias needs the current frame size and
                # would otherwise refuse. Listing is not the place to do hardware
                # I/O — say what it resolves against instead.
                res = resolve(name)
                entry.update({
                    "p_gain": format_si(res.p_gain) + "m",
                    "i_gain": format_si(res.i_gain) + "m/s",
                    "time_constant_s": format_si(res.time_constant_s) + "s",
                    "setpoint_a": (format_si(res.setpoint_a) + "A"
                                   if res.setpoint_a is not None else None),
                    "sources": res.sources,
                    "usable": True,
                })
            except PresetRejected as exc:
                entry.update({"usable": False, "why": str(exc)})
            rows.append(entry)
        custom = {p["name"] for p in get_presets()}
        for row in rows:
            row["kind"] = (
                "reserved" if row["name"] in ("approach", "scan")
                else "custom" if row["name"] in custom
                else "scan-tier"
            )
        return SkillResult(
            skill_name="ListZCtrlPresets",
            success=True,
            data={
                "presets": rows,
                "note": tr(
                    "'scan' 按当前扫描帧尺寸选档,所以这里不显示它的具体数值 —— "
                    "应用时才确定。",
                    "'scan' picks its tier from the current scan frame size, so its concrete "
                    "values are not shown here -- they are determined when applied.",
                ),
            },
        )
