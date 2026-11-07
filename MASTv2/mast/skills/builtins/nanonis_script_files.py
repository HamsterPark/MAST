"""Nanonis script slot file I/O — gated, and STILL fenced when the gate is open.

These three (``Script_Load`` / ``Script_Save`` / ``Script_LUTSave``) were deliberately
left unwritten on 2026-07-12. The reason is in nanonis_script.py's docstring:

    A Nanonis script is compiled and run ON THE REAL-TIME CONTROLLER. It issues no
    safe_call. SafetyGate, the mode gate, the abort gate and HITL are all BLIND to
    what it does. The global bounds — bias ±10 V, the setpoint, Z, the scan size —
    do not apply inside it. The one thing that can stop it is Script_Stop.

    So the operator-maintained allow-list (config/nanonis_scripts.json) is the
    barrier — the ONLY one. And ``Script_Load(slot, file)`` would let the agent put a
    different file into a vetted slot, at which point "slot 1 is approved" means
    nothing.

They are now exposed anyway, off by default, behind 高级.
Fine — but "the gate is open" must not mean "the fence is gone".

WHAT THE ALLOW-LIST ACTUALLY APPROVES
=====================================
It approves **a script, in a slot**. Not a slot. The slot number is just how you
name it. So the rule that keeps the approval honest is one line:

    **LoadNanonisScript REFUSES to load into a slot that is on the allow-list.**

Loading into an *un-vetted* slot is harmless: ``RunNanonisScript`` only ever permits
allow-listed slots, so a script the agent loaded is a script the agent cannot run.
And that is not a dead end — it is the right division of labour:

    agent loads the file into a free slot  →  human reads it and vets it
    →  human adds the slot to config/nanonis_scripts.json  →  agent can run it

The agent does the mechanical work. The human does the approving. Which is the whole
argument for the allow-list in the first place.
"""

from __future__ import annotations

from mast.core.diagnostics import record
from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.skills.base import BaseSkill
from mast.skills.builtins.nanonis_script import load_allowlist
from mast.i18n import tr


def _fail(name: str, error: str, calls: list) -> SkillResult:
    return SkillResult(skill_name=name, success=False, error=error, nanonis_calls=calls)


class LoadNanonisScript(BaseSkill):
    """Load a script file into a slot — never into a vetted one."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="LoadNanonisScript",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.DANGEROUS,
            description=(
                tr("把一个 Nanonis 脚本文件载入某个脚本**槽位**。\n"
                "\n"
                "**它会拒绝载入已在受审白名单上的槽位。** 白名单的含义是「有人读过这个槽位里的脚本并批准了它」"
                "。往那个槽位里换一个别的脚本，会把这份批准变成一句假话 —— 而 Nanonis 脚本跑在实时控制器上，"
                "MAST 的安全闸门、模式闸门、abort 闸门和 HITL 在那里统统看不见它。\n"
                "\n"
                "请载入一个**空闲**槽位。agent 载入之后并不能运行它（RunNanonisScript 只允许白名单上的槽位）"
                "—— 这正是设计意图。流程是：你在这里载入，由人读过之后把该槽位加进 config/nanonis_scripts.json，"
                "只有到那时它才跑得起来。\n"
                "\n"
                "这里的文件路径是 **NANONIS 机器上的**，不是 MAST 这边的。", "Load a Nanonis script file into a script **slot**.\n"
"\n"
"**It refuses to load into a slot that is already on the vetted allow-list.** What the allow-list means is \"someone read the script in this slot and approved it\""
". Swapping another script into that slot would turn that approval into a lie -- and Nanonis scripts run on the real-time controller, "
"where MAST's safety gates, mode gate, abort gate and HITL cannot see them at all.\n"
"\n"
"Load into a **free** slot. After loading, the agent cannot run it (RunNanonisScript only allows slots on the allow-list)"
" -- that is the design intent. The flow is: you load here, a person reads it and adds the slot to config/nanonis_scripts.json, "
"and only then can it run.\n"
"\n"
"The file path here is **on the NANONIS machine**, not on MAST's side.")
            ),
            parameters=[
                ParameterSpec(name="slot", type="int",
                              description=tr("脚本槽位（**不能**是已经在白名单上的那些）", "Script slot (**not** one already on the allow-list)"),
                              required=True, min_value=1, max_value=15),
                ParameterSpec(name="file_path", type="str",
                              description=tr("**NANONIS 机器上** .ns 脚本文件的路径", "Path of the .ns script file **on the NANONIS machine**"),
                              required=True),
                ParameterSpec(name="load_session", type="bool",
                              description=tr("同时载入该脚本存下来的 session", "Also load the session saved with the script"),
                              required=False, default=False),
            ],
            estimated_duration_s=2.0,
            composition_level=0,
            tags=["script", "file", "advanced", "dangerous"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "LoadNanonisScript"
        slot = int(params["slot"])
        path = str(params["file_path"])

        allowed = load_allowlist()
        if slot in allowed:
            reason = (
                tr(f"槽位 {slot} 在已审白名单上——不能往里装别的脚本。\n"
                f"白名单认的是「**这个槽位里的那个脚本**已经被人读过、批过」，"
                f"不是「这个槽位号被批过」。换掉内容，批准就成了谎言，而脚本跑在实时控制器上，"
                f"MAST 的安全门/模式门/中止门一个都看不见它在做什么。\n"
                f"请装进一个未审槽位；装完由人审阅，再把它加进 config/nanonis_scripts.json。", f"Slot {slot} is on the vetted allow-list -- no other script can be loaded into it.\n"
f"What the allow-list recognises is \"**the script in this slot** has been read and approved by a person\", "
f"not \"this slot number has been approved\". Replace the content and the approval becomes a lie, and the script runs on the real-time controller, "
f"where MAST's safety gate / mode gate / abort gate cannot see what it is doing.\n"
f"Load into an unvetted slot; after loading, a person reviews it and then adds it to config/nanonis_scripts.json.")
            )
            record("safety_block", f"LoadNanonisScript[slot {slot}]", reason,
                   slot=slot, file_path=path, allowed_slots=sorted(allowed))
            return _fail(name, reason, [])

        rec = context.safe_call("Script_Load", slot, path,
                                1 if bool(params.get("load_session", False)) else 0)
        if rec.error:
            return _fail(name, f"Script_Load failed: {rec.error}", [rec])

        # Every load is on the record. A slot's contents changing is exactly the event
        # a human needs to see before they vet it — and after, if something goes wrong.
        record("note", f"LoadNanonisScript[slot {slot}]",
               "脚本已载入未审槽位（agent 还不能运行它——需人工加入白名单）",
               slot=slot, file_path=path, allowed_slots=sorted(allowed))

        return SkillResult(
            skill_name=name, success=True, nanonis_calls=[rec],
            data={"slot": slot, "file_path": path, "runnable": False,
                  "allowed_slots": sorted(allowed)},
            summary=(tr(f"脚本已载入槽位 {slot}。**agent 还不能运行它**——"
                     f"请人工审阅后把槽位 {slot} 加入 config/nanonis_scripts.json。", f"Script loaded into slot {slot}. **The agent cannot run it yet** -- "
f"after a person reviews it, add slot {slot} to config/nanonis_scripts.json.")),
        )


class SaveNanonisScript(BaseSkill):
    """Write a slot's script out to a file."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SaveNanonisScript",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("把某个槽位里当前的脚本导出成 **NANONIS 机器上**的一个文件。\n"
                "\n"
                "这件事读的是槽位、**写的是一个文件**。它改变不了仪器的行为 —— 但它会覆盖你给的那个路径上已有的文件，"
                "而 MAST 看不到那里原本是什么。请挑一个新路径。\n"
                "\n"
                "适合把一个受审槽位里实际装着什么，连同实验记录一起归档下来。", "Export the script currently in a slot to a file **on the NANONIS machine**.\n"
"\n"
"This reads the slot and **writes a file**. It cannot change the instrument's behaviour -- but it overwrites any existing file at the path you give, "
"and MAST cannot see what was there. Pick a new path.\n"
"\n"
"Suited for archiving what is actually in a vetted slot together with the experiment record.")
            ),
            parameters=[
                ParameterSpec(name="slot", type="int",
                              description=tr("要读的脚本槽位", "Script slot to read"),
                              required=True, min_value=1, max_value=15),
                ParameterSpec(name="file_path", type="str",
                              description=tr("**NANONIS 机器上**的目标路径（已存在则会被覆盖）", "Target path **on the NANONIS machine** (overwritten if it exists)"),
                              required=True),
                ParameterSpec(name="save_session", type="bool",
                              description=tr("同时保存该脚本的 session", "Also save the session of the script"),
                              required=False, default=False),
            ],
            estimated_duration_s=2.0,
            composition_level=0,
            tags=["script", "file", "advanced"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        slot = int(params["slot"])
        path = str(params["file_path"])
        rec = context.safe_call("Script_Save", slot, path,
                                1 if bool(params.get("save_session", False)) else 0)
        if rec.error:
            return _fail("SaveNanonisScript", f"Script_Save failed: {rec.error}", [rec])
        record("note", f"SaveNanonisScript[slot {slot}]", "槽位脚本已导出到文件",
               slot=slot, file_path=path)
        return SkillResult(
            skill_name="SaveNanonisScript", success=True, nanonis_calls=[rec],
            data={"slot": slot, "file_path": path},
            summary=tr(f"槽位 {slot} 的脚本已保存到 {path}", f"Script of slot {slot} saved to {path}"),
        )


class SaveNanonisScriptLut(BaseSkill):
    """Write a slot's LUT out to a file."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SaveNanonisScriptLut",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("把某个脚本槽位的**查找表**（LUT）导出成 Nanonis 机器上的一个文件。\n"
                "\n"
                "LUT 是脚本接收参数的途径 —— 它是一个受审脚本里 agent 唯一能改的东西（且只能在白名单声明的范围内改）"
                "。把它导出来是一次读取；它不改变仪器上的任何东西。它会覆盖给定路径上已有的文件。", "Export the **look-up table** (LUT) of a script slot to a file on the Nanonis machine.\n"
"\n"
"The LUT is how a script receives parameters -- it is the only thing in a vetted script that the agent can change (and only within the range declared on the allow-list)"
". Exporting it is a read; it changes nothing on the instrument. It overwrites any existing file at the given path.")
            ),
            parameters=[
                ParameterSpec(name="slot", type="int",
                              description=tr("要保存其 LUT 的脚本槽位", "Script slot whose LUT is to be saved"),
                              required=True, min_value=1, max_value=15),
                ParameterSpec(name="file_path", type="str",
                              description=tr("**NANONIS 机器上**的目标路径", "Target path **on the NANONIS machine**"),
                              required=True),
            ],
            estimated_duration_s=2.0,
            composition_level=0,
            tags=["script", "lut", "file", "advanced"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        slot = int(params["slot"])
        path = str(params["file_path"])
        rec = context.safe_call("Script_LUTSave", slot, path)
        if rec.error:
            return _fail("SaveNanonisScriptLut", f"Script_LUTSave failed: {rec.error}", [rec])
        record("note", f"SaveNanonisScriptLut[slot {slot}]", "槽位 LUT 已导出到文件",
               slot=slot, file_path=path)
        return SkillResult(
            skill_name="SaveNanonisScriptLut", success=True, nanonis_calls=[rec],
            data={"slot": slot, "file_path": path},
            summary=tr(f"槽位 {slot} 的 LUT 已保存到 {path}", f"LUT of slot {slot} saved to {path}"),
        )
