"""工具面的四道门 —— **唯一定义处**。

哪些技能不该出现在 agent 的工具表里，这个问题有四个互相独立的答案（第四道门「站点
画像」在下面的表后面单独说）：

============== ================================== =========================
门              问的是什么                          谁拥有它
============== ================================== =========================
硬件模块        这台机器有没有这个硬件               ``hardware_modules``
高级能力        这个动作能不能绕过一层保护           ``advanced_capabilities``
**订阅**        **这位用户这段时间在做什么**       ``subscription``
站点画像        这是一台什么站（STM 站 / 光学站）   ``station_face``
============== ================================== =========================

**第四道门：站点画像**（``mast.station``，设计 ``optical_station_witec.md`` D1）。光学站
没有 Nanonis，约 400 个 STM 技能在那里只会失败、只会淹没 AI 的工具表。光学画像下这道门
是一张**白名单**（光学模块 + 通用技能 + 用户自己造的，见 ``station_face``），名单外的
技能被收进 ``FaceSkip.station``。STM 画像下这道门只收**光学站自己的技能模块**
（``station_face.STATION_ONLY_MODULES``：WITec、光源/温控/功率计、测量协议、操作员提示）
—— 它们是因为光学站才存在的技能，别的门都不会替 STM 站挡住它们；除此之外一个不收，
这些模块里还没有技能时它就是空集，STM 站的工具面逐字节不变。

它**并入 ``hardware``**：外部面的门（``api/ext/jobs.py::_disabled_names``）只读
``hardware | advanced``，所以放进 ``hardware`` 才能让光学站上外部 agent 调 STM 技能被
拒绝，而不用去改那个文件。这个并入有语义依据 —— 光学站上这些技能缺的正是它们要的硬件
（STM / Nanonis）；``station`` 同时单独留着，给技能卡和 ``describe()`` 说清真正的原因。
这道门读不出来（光学画像下没有绑定注册表）时与其他门一样 fail-open 并记进
``unreadable``。

四道门的语义完全不同，但**并集只该算一次**。在这个模块存在之前它被算了三遍：

* ``agents/instrument_control/tools.py::build_instrument_skill_tools`` —— 装配真身；
* 同文件 ``expected_wrap_fingerprint`` —— 生效探针；
* ``webui/agents_api.py::_compute_agent_tools`` —— UI 镜像。

三份里漏改任何一份的后果都不一样，而且都不报错：漏改探针 ⇒ ``fingerprint_matches``
永远说「没跟上」；漏改镜像 ⇒ 界面上列的工具表**不是模型手上那份**（那个文件自己的
注释把这件事叫做「同一件事的另一种说谎方式」）。所以并集搬到这里，三处都调它。

**这个模块永不抛。** 一道读不出来的门不是一道门 —— 它 fail-open（不过滤）并把门名
记进 ``unreadable``，让调用方能如实说出「本次没过滤，因为某道门读不出来」。这条与
``agents_api`` 原来那句 "a gate we cannot read is not a gate" 是同一条，只是现在
三处共享同一份实现，不会有一处忘了 try。

注意三道门的**失败方向不同**，这是有意的：硬件/高级能力门在**自己模块内部**
fail-closed（读不懂的持久化状态回默认全关），订阅门 fail-open（读不懂就全订阅）。
理由见 ``subscription`` 的模块 docstring：前两者关错了只是少几个必然失败的工具，
订阅关错了是 agent 一夜之间失能，而订阅本就不是保护。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from mast.i18n import tr

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FaceSkip:
    """一次并集计算的结果 —— 以及它是**怎么**算出来的。"""

    hardware: frozenset[str] = frozenset()
    advanced: frozenset[str] = frozenset()
    unsubscribed: frozenset[str] = frozenset()
    #: 第四道门：这个站点画像下不属于它的技能（光学站上的 STM 技能；STM 站上光学站自己
    #: 的技能）。``compute`` 把它**并入了 ``hardware``**（见模块说明），这个字段是同一批
    #: 名字，留着说清原因。
    station: frozenset[str] = frozenset()
    #: 读不出来的门名（``()`` = 所有门都读到了）。非空即表示本次**少过滤**了。
    unreadable: tuple[str, ...] = ()

    @property
    def names(self) -> frozenset[str]:
        return self.hardware | self.advanced | self.unsubscribed | self.station

    def __contains__(self, name: str) -> bool:
        return (name in self.hardware or name in self.advanced
                or name in self.unsubscribed or name in self.station)

    def describe(self) -> str:
        n_hw = len(self.hardware - self.station)
        bits = [tr(f"{n_hw} 因硬件模块关闭",
                   f"{n_hw} off by hardware module"),
                tr(f"{len(self.advanced)} 因高级能力关闭",
                   f"{len(self.advanced)} off by advanced capability"),
                tr(f"{len(self.unsubscribed)} 因未订阅",
                   f"{len(self.unsubscribed)} not subscribed")]
        if self.station:
            bits.append(tr(f"{len(self.station)} 因站点画像（不属于这个站）",
                           f"{len(self.station)} not part of this station profile"))
        if self.unreadable:
            bits.append(tr(f"⚠ 读不出来的门：{'/'.join(self.unreadable)}（本次未过滤）",
                           f"⚠ unreadable gates: {'/'.join(self.unreadable)} (not filtered this time)"))
        return tr("、", ", ").join(bits)


def compute(all_names: Iterable[str] | None = None, *, registry=None) -> FaceSkip:
    """四道门的并集。``all_names`` = 市场全集（订阅门要靠它算补集）。

    不传 ``all_names`` 时订阅门不过滤（它没法在不知道全集的情况下算补集），
    硬件/高级能力两道门照常 —— 这样一个只关心那两道门的老调用点仍然拿到正确答案。

    站点画像门要的是技能**类**（按模块归属判），不是名字。**手里有注册表的调用方应当显式
    传 ``registry=``**（装配、指纹探针、``agents_api`` 镜像、外部门、技能卡都这样做）：
    不传才退回 ``station_face.bind_registry`` 绑定的活注册表（``CoreRuntime.setup`` 绑，
    一次性 CLI 在 discover 之后也绑）。两样都没有时：STM 画像下这道门什么也不找；光学
    画像下它读不出来，记进 ``unreadable`` 并不过滤 —— 失效开放，STM 技能会漏出去，
    所以别让一个拿得到注册表的调用方走到这一步。STM 画像下这道门只找出光学站自己的
    技能模块里的技能，那些模块里没有技能时结果与它存在之前逐字节相同。
    """
    hw: frozenset[str] = frozenset()
    adv: frozenset[str] = frozenset()
    unsub: frozenset[str] = frozenset()
    stn: frozenset[str] = frozenset()
    bad: list[str] = []

    try:
        from mast.skills.hardware_modules import disabled_skill_names as _hw
        hw = _hw()
    except Exception as exc:  # noqa: BLE001
        logger.warning("工具面：硬件模块门读不出来（%s）—— 本次不按它过滤", exc)
        bad.append(tr("硬件模块", "hardware module"))

    try:
        from mast.skills.advanced_capabilities import disabled_skill_names as _adv
        adv = _adv()
    except Exception as exc:  # noqa: BLE001
        logger.warning("工具面：高级能力门读不出来（%s）—— 本次不按它过滤", exc)
        bad.append(tr("高级能力", "advanced capability"))

    try:
        from mast.skills.subscription import unloaded_skill_names as _sub
        unsub = _sub(all_names)
    except Exception as exc:  # noqa: BLE001
        logger.warning("工具面：订阅门读不出来（%s）—— 本次不按它过滤", exc)
        bad.append(tr("订阅", "subscription"))

    try:
        from mast.skills.station_face import hidden_names as _station_hidden
        stn = _station_hidden(registry)
    except Exception as exc:  # noqa: BLE001
        logger.warning("工具面：站点画像门读不出来（%s）—— 本次不按它过滤", exc)
        bad.append(tr("站点画像", "station profile"))
    if stn:
        hw = hw | stn      # 外部面的门只读 hardware | advanced（见模块说明）

    return FaceSkip(hardware=hw, advanced=adv, unsubscribed=unsub, station=stn,
                    unreadable=tuple(bad))


def skip_names(all_names: Iterable[str] | None = None) -> frozenset[str]:
    """只要并集本身的调用方用这个。"""
    return compute(all_names).names


def bind_registry(registry) -> None:
    """把活的技能注册表交给第四道门（``CoreRuntime.setup`` 在注册表建好之后调；不走 setup
    的一次性 CLI 在 discover 之后调）。这是**后备**：``compute(registry=…)`` 显式传的优先。
    不绑又没有显式传，光学画像下站点画像门读不出来、本次不过滤；STM 画像下不绑就什么也
    不挡（没有注册表可分类），绑了也只挡光学站自己的技能模块里的技能。"""
    from mast.skills import station_face

    station_face.bind_registry(registry)


def names_from_registry(registry) -> frozenset[str]:
    """注册表里现在有哪些技能名（= 订阅门要用的「市场全集」）。"""
    try:
        return frozenset(m.name for m in registry.list_skills())
    except Exception as exc:  # noqa: BLE001
        logger.warning("工具面：注册表列不出技能名（%s）", exc)
        return frozenset()


__all__ = ["FaceSkip", "bind_registry", "compute", "skip_names", "names_from_registry"]
