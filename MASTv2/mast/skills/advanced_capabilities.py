"""Advanced capabilities — powers that can bypass a protection. All ship OFF.

DIFFERENT FROM ``hardware_modules``, AND THE DIFFERENCE IS THE WHOLE POINT
=========================================================================
``hardware_modules`` is about hardware you may not OWN. A KPFM controller you do
not have is switched off because calling it can only ever fail.

These are about powers you DO have and mostly should not hand to an agent. Every
one of them can, in some way, **step around a protection that the rest of this
system relies on**:

  * ``script_files``   — the Nanonis script allow-list vets *what is in a slot*. A
                         Load can put a different script into a vetted slot, and the
                         vetting becomes a lie. (Mitigated even when enabled — see
                         nanonis_script_files.py.)
  * ``quit_nanonis``   — ends the session. Everything MAST knows about the
                         instrument stops being true at that instant.
  * ``multipass_files``— loads a scan configuration from a file MAST cannot inspect.
  * ``blocking_wait``  — holds the main TCP connection for the whole duration of a
                         scan. Nothing else can talk to the instrument on that role.

So the gate is the same MECHANISM as the hardware modules (off ⇒ the skills are not
in the agent's tool list at all — it cannot call what it cannot see) with a
different LOCATION: these live in 高级 (the admin page), not in 设置, and turning one
on is a deliberate act with a confirmation, not a checkbox you brush past.

WHAT IS **NOT** IN HERE, AND WHY
================================
There is no capability for "let the agent widen its own guardrails". Everything in
this file is a power the operator can lend to the agent for a session. None of it is
a power over the safety system itself — the abort allow-list, the mode gate, the
HITL map and the Layer-0 checks are not configurable from any UI, by anyone, and
that is on purpose.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from mast.i18n import tr

logger = logging.getLogger(__name__)

SETTINGS_KEY = "advanced_capabilities"


@dataclass(frozen=True)
class Capability:
    id: str
    name: str          # operator-facing, Chinese
    risk: str          # the ONE sentence that says what protection this steps around
    description: str   # what it lets the agent do
    skills: tuple[str, ...]
    default_on: bool = False


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        id="script_files",
        name=tr("Nanonis 脚本文件读写", "Nanonis script file read/write"),
        risk=tr("脚本白名单认的是「某个槽位里的那个脚本已被人审过」。往已审槽位里装别的脚本，"
                "审核就成了谎言——而脚本跑在实时控制器上，MAST 的安全门/模式门/中止门"
                "**一个都管不到它**。",
                "The script allow-list only knows that \"the script in a given slot has been reviewed "
                "by a person\". Loading a different script into a reviewed slot makes that review a lie, "
                "and scripts run on the real-time controller, where **none** of MAST's safety gate, "
                "mode gate or abort gate can reach them."),
        description=tr("让 agent 把脚本文件载入槽位、把槽位存成文件、导出 LUT。"
                       "**即使打开，也仍然拒绝载入任何已在白名单上的槽位**——"
                       "agent 只能往未审槽位里装，装完你审、审完它才能跑。",
                       "Lets the agent load script files into slots, save slots to files and export LUTs. "
                       "**Even when enabled, loading into any slot that is already on the allow-list is "
                       "still refused**: the agent can only load into unreviewed slots, and the script "
                       "can run only after you have reviewed it."),
        skills=("LoadNanonisScript", "SaveNanonisScript", "SaveNanonisScriptLut"),
    ),
    Capability(
        id="quit_nanonis",
        name=tr("退出 Nanonis", "Quit Nanonis"),
        risk=tr("退出后 MAST 对仪器的一切认知立刻失效，连接断开。"
                "（这是 Nanonis 自己的优雅退出——比强杀好：强杀会永久损坏 TCP 端口。）",
                "After quitting, everything MAST knows about the instrument is instantly invalid and "
                "the connection drops. (This is Nanonis's own graceful exit, which is better than a "
                "force-kill: a force-kill permanently damages the TCP port.)"),
        description=tr("让 agent 退出 Nanonis 软件。**执行前强制先停扫描、再退针**——"
                       "带着进针状态退出软件，等于让针尖在没有任何软件看管的情况下留在表面。",
                       "Lets the agent quit the Nanonis software. **Scanning is always stopped first and "
                       "the tip withdrawn**: quitting while the tip is approached would leave the tip on "
                       "the surface with no software watching it."),
        skills=("QuitNanonis",),
    ),
    Capability(
        id="multipass_files",
        name=tr("多程扫描配置文件读写", "Multi-pass scan config file read/write"),
        risk=tr("从 MAST 看不见内容的文件里载入扫描配置——每一程的偏压/Z 偏移由那个文件决定。",
                "Loads a scan configuration from a file whose contents MAST cannot see; the bias and "
                "Z offset of every pass are decided by that file."),
        description=tr("让 agent 载入/保存多程扫描（Multi-Pass）的配置文件。"
                       "多程扫描本身的开关（SetMultiPass）不在此门内——那是普通扫描功能。",
                       "Lets the agent load/save multi-pass scan configuration files. The multi-pass "
                       "switch itself (SetMultiPass) is not behind this gate; that is an ordinary scan "
                       "feature."),
        skills=("LoadMultiPassConfig", "SaveMultiPassConfig"),
    ),
    Capability(
        id="coarse_drive",
        name=tr("设置粗动马达驱动电压/频率", "Set coarse motor drive voltage/frequency"),
        risk=tr("控制器能输出的电压 ≠ 这台机器的压电叠堆能承受的电压。有些 Nanonis "
                "控制器支持 400 V,而有的叠堆 300 V 就烧了 —— **没有任何读数会告诉你"
                "是哪一种**,设错了叠堆就废了,没有重试。",
                "The voltage a controller can output is not the voltage this machine's piezo stack can "
                "withstand. Some Nanonis controllers support 400 V, while some stacks burn out at 300 V "
                "-- **no readout tells you which kind you have**, and once it is set wrong the stack is "
                "ruined, with no retry."),
        description=tr("让 agent 调用 SetMotorFreqAmp。**即使打开,写入仍受本机声明上限"
                       "约束**(在【高级】页填写,需 admin PIN):没声明就一律拒绝,"
                       "超上限直接拒绝而不是降到上限。另外 Layer-0 安全门仍会在自主路径上"
                       "拦下它 —— 这一项打开只是让手动/审批路径可用,"
                       "**不会**让 agent 自主设置驱动电压。",
                       "Lets the agent call SetMotorFreqAmp. **Even when enabled, writes are still bound "
                       "by this machine's declared limit** (set on the Advanced page, needs the admin "
                       "PIN): with no declared limit everything is refused, and a value above the limit "
                       "is refused rather than clamped to it. In addition, the Layer-0 safety gate still "
                       "blocks it on the autonomous path -- enabling this only makes the manual/approval "
                       "path available and **does not** let the agent set the drive voltage on its own."),
        skills=("SetMotorFreqAmp",),
    ),
    Capability(
        id="blocking_wait",
        name=tr("阻塞式等待扫描结束", "Blocking wait for scan end"),
        risk=tr("整个扫描期间占住 main 这条 TCP 连接，其它 main 角色的调用全部排队。"
                "中止仍然有效（走 emergency 端口，是独立 socket），监控也仍然有效。",
                "Holds the main TCP connection for the whole scan, so calls from every other main-role "
                "client queue up. Abort still works (it goes through the emergency port, a separate "
                "socket), and so does monitoring."),
        description=tr("让 agent 用 Nanonis 原生的阻塞等待，而不是轮询。"
                       "**通常不需要**：WaitScanComplete（轮询版）不占连接、可中止、有进度。"
                       "只有在需要精确的扫描结束时刻时才用得上。",
                       "Lets the agent use Nanonis's native blocking wait instead of polling. **Usually "
                       "not needed**: WaitScanComplete (the polling version) does not hold the connection, "
                       "can be aborted and reports progress. It is only useful when you need the exact "
                       "moment the scan ends."),
        skills=("WaitForScanEndBlocking",),
    ),
)

CAPABILITY_BY_ID: dict[str, Capability] = {c.id: c for c in CAPABILITIES}
SKILL_OWNER: dict[str, str] = {s: c.id for c in CAPABILITIES for s in c.skills}
DEFAULT_ENABLED: frozenset[str] = frozenset(c.id for c in CAPABILITIES if c.default_on)


# ─────────────────────────────────────────────────────────────────────────────
# The holder + the gate (same shape as hardware_modules — see its docstring)
# ─────────────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_enabled: frozenset[str] = DEFAULT_ENABLED


def enabled_ids() -> frozenset[str]:
    return _enabled


def set_enabled(raw) -> frozenset[str]:
    """Install the active set. Fail-closed on anything we cannot read: an unknown
    stored state is not "everything is granted"."""
    global _enabled
    if isinstance(raw, dict):
        wanted = {str(k) for k, v in raw.items() if bool(v)}
    elif isinstance(raw, (list, tuple, set, frozenset)):
        wanted = {str(x) for x in raw}
    else:
        if raw is not None:
            logger.warning("advanced_capabilities: 无法解析的开关状态 %r；按默认（全关）",
                           type(raw))
        wanted = set(DEFAULT_ENABLED)

    unknown = wanted - set(CAPABILITY_BY_ID)
    if unknown:
        logger.warning("advanced_capabilities: 忽略未知能力 id %s", sorted(unknown))
    new = frozenset(wanted & set(CAPABILITY_BY_ID))
    with _lock:
        _enabled = new
    if new:
        # Loud on purpose. Every one of these steps around a protection; the fact
        # that one is live belongs in the log, not only in a settings JSON.
        logger.warning("advanced_capabilities: 已授予 agent 高级能力 %s", sorted(new))
    else:
        logger.info("advanced_capabilities: 全部关闭（默认）")
    return new


def disabled_skill_names() -> frozenset[str]:
    on = enabled_ids()
    return frozenset(s for s, cap in SKILL_OWNER.items() if cap not in on)


def capability_states() -> list[dict]:
    on = enabled_ids()
    return [
        {
            "id": c.id,
            "name": c.name,
            "risk": c.risk,
            "description": c.description,
            "enabled": c.id in on,
            "skill_count": len(c.skills),
            "skills": list(c.skills),
        }
        for c in CAPABILITIES
    ]


__all__ = [
    "Capability", "CAPABILITIES", "CAPABILITY_BY_ID", "SKILL_OWNER", "SETTINGS_KEY",
    "DEFAULT_ENABLED", "enabled_ids", "set_enabled", "disabled_skill_names",
    "capability_states",
]
