"""坐标代次(``coord_epoch``)的权威查询与核对 —— 强制点共用的那一份。

一次横向 XY 粗动之后,同一个 ``(x_m, y_m)`` 指的是**另一片表面**。地图标记靠
``map_markers.coord_epoch`` 记住这件事(见 :mod:`mast.io.map_analysis`),但记住
不等于有人拦:一份在第 3 代排出来的计划,粗动之后照样能被原样执行,每一帧都扫在
错的地方 —— 而且扫得很成功。这个模块就是被拦的那一下。

设计文档:``docs/v2/design/`` 的 P0 修复设计,修复项 节(文件名按「P0 修复设计」检索;
通用层注释不写样品名前缀)。

三条约束,都是踩出来的:

**一、代次要查,不要从行窗口里数。** ``storage.current_epoch()`` 是对整个作用域
COUNT ``kind='coarse_move'``,廉价且权威;而 ``get_markers()`` 只回最新 ``limit``
行,在那个窗口里数 coarse_move 会**漏报**代次(作用域一长就漏),于是陈旧坐标被
判成当前。``api/routes/vision.py`` 的 ``_current_epoch_or_derived`` 注释里记着
后果:前端因此把陈旧标记按全不透明画了出来。

**二、读不到返回 None,不是 0。** 0 是一个**真实答案**(「这个作用域还没粗动
过」)。把「读不到」折叠进 0,一份盖着第 3 代章的计划会在存储不可用时被判成陈旧
而整批拒绝 —— 「读不到」不是答案,更不是「出故障」。所以这里有四态而不是两态,
调用方必须分开处理 :data:`UNVERIFIABLE`。

**三、不换算。** 跨代次坐标换算在本仓**有意不存在**(``io/coarse_map.py`` 的
「WHY STEPS, NOT METRES」:粗动步进是开环的,步长随驱动幅度、负载、温度漂移,
同样 100 步在 300 K 能比 4 K 远五倍;``xy_motor_step_m`` 只是标注,没有任何东西
拿它计算,也没有任何标记会被它重投影)。所以陈旧坐标的处置只有一个:**拒绝,
让人重新规划**。既不夹紧,也不换算 —— 一个换算出来的坐标看上去和真坐标一模
一样,而它是编的。
"""

from __future__ import annotations

import logging
from mast.i18n import tr
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: 核对结果的闭集。四个值互不折叠 —— 「对得上」「陈旧」「查不了」「没盖章」是
#: 四件不同的事,把后两个并进前两个正是本模块存在的理由。
MATCH = "coord_epoch_match"
STALE = "coord_epoch_stale"
UNVERIFIABLE = "coord_epoch_unverifiable"
UNSTAMPED = "coord_epoch_unstamped"

STATES = (MATCH, STALE, UNVERIFIABLE, UNSTAMPED)

#: 拒绝时给下游看的原因码(闭集里唯一一个「拒绝」态)。
REFUSAL_CODE = STALE

#: 陈旧时统一的下一步指引。**不提供换算**,只提供重新规划 —— 见模块文档第三条。
_REPLAN_HINT = (tr("粗动使旧坐标失效(它们现在指向另一片表面),"
                "请按当前代次重新规划后再执行。**不做跨代次换算**:"
                "粗动步进是开环的,换算出来的坐标看上去和真坐标一样,而它是编的。", "Coarse motion invalidated the old coordinates (they now point at a different part of the surface); re-plan for the current generation before executing. **No cross-generation conversion is done**: coarse steps are open-loop, and a converted coordinate looks just like a real one but is made up."))


@dataclass(frozen=True)
class EpochVerdict:
    """一次核对的结论。``state`` 取自本模块的闭集。"""

    state: str
    #: 被核对的那份产物盖的章(没盖章时 None)。
    stamped: "int | None"
    #: 核对当时的权威代次(读不到时 None)。
    current: "int | None"
    #: 给人和模型看的话。``MATCH`` 时为空串。
    message: str = ""

    @property
    def stale(self) -> bool:
        """**只有确凿的不匹配才是 True。** 查不到、没盖章都不是陈旧。"""
        return self.state == STALE

    @property
    def verified(self) -> bool:
        return self.state == MATCH


def read_current_epoch() -> "int | None":
    """当前作用域的权威坐标代次;**读不到返回 None**(见模块文档第二条)。

    只读、不取仪器令牌、不抛异常 —— 强制点会在每一帧之前调它。
    """
    try:
        from mast.logging.experiment_log import get_active_log

        log = get_active_log()
        storage = getattr(log, "_storage", None) if log is not None else None
        if storage is None:
            return None
        fn = getattr(storage, "current_epoch", None)
        if not callable(fn):
            return None
        value = fn(getattr(log, "current_experiment_id", None),
                   getattr(log, "current_sample_id", None))
    except Exception as exc:  # noqa: BLE001 —— 查不到就是查不到,不是出故障
        logger.debug("坐标代次读取失败(按「查不了」处理): %s", exc)
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        # 存储回了个不是整数的东西 —— 同样是「查不了」,不是 0。
        logger.debug("坐标代次不是整数(按「查不了」处理): %r", value)
        return None


def _as_epoch(value) -> "int | None":
    """把盖在产物上的章读成整数;读不出来当没盖(``None``)。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def verify(stamped, *, current: "int | None" = None,
           what: str = tr("这份产物的坐标", "the coordinates of this artifact")) -> EpochVerdict:
    """核对一个盖了章的坐标产物是否还属于当前代次。

    ``stamped`` 是产物上的章(计划 JSON 顶层的 ``coord_epoch`` / 技能参数);
    ``current`` 传进来就用,不传就现查(每帧前查一次是廉价的 COUNT)。

    调用方按 ``state`` 分四路处理 —— 尤其**不要**把 ``UNVERIFIABLE`` 和
    ``UNSTAMPED`` 当成陈旧拒掉:那会让「读不到记录」变成一道解不开的闸。
    """
    plan_epoch = _as_epoch(stamped)
    if plan_epoch is None:
        return EpochVerdict(
            state=UNSTAMPED, stamped=None, current=current,
            message=(tr(f"{what}没有 coord_epoch 章,无法判断它是不是粗动之前排的 —— "
                     f"按旧格式放行,但这一次没有代次保护。", f"{what} has no coord_epoch stamp, so it cannot be judged whether it was planned before a coarse move. Let through in the old format, but this time there is no generation protection.")))

    now = read_current_epoch() if current is None else _as_epoch(current)
    if now is None:
        return EpochVerdict(
            state=UNVERIFIABLE, stamped=plan_epoch, current=None,
            message=(tr(f"{what}盖的是第 {plan_epoch} 代,但**当前代次查不到**"
                     f"(没有活动实验 / 记录存储不可用)。查不到不等于陈旧,"
                     f"也不等于当前 —— 这一次没有代次保护。", f"{what} is stamped generation {plan_epoch}, but **the current generation cannot be found** (no active experiment / record storage unavailable). Not found does not mean stale, and does not mean current either; this time there is no generation protection.")))

    if now != plan_epoch:
        return EpochVerdict(
            state=STALE, stamped=plan_epoch, current=now,
            message=(tr(f"{what}属于第 {plan_epoch} 代坐标系,当前已是第 {now} 代。"
                     f"{_REPLAN_HINT}", f"{what} belongs to coordinate-system generation {plan_epoch}, and the current one is {now}. {_REPLAN_HINT}")))

    return EpochVerdict(state=MATCH, stamped=plan_epoch, current=now)


__all__ = [
    "MATCH", "STALE", "UNVERIFIABLE", "UNSTAMPED", "STATES", "REFUSAL_CODE",
    "EpochVerdict", "read_current_epoch", "verify",
]
