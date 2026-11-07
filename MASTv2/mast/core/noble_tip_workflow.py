"""贵金属表面上把针尖修到「好针尖基础态」的**流程**参数。

实操流程:大偏压进针 → 低偏压大电流建结 → ±10 V 脉冲
大修(看 Z 前后稳定值跳变,每发换地方,向上几十 nm 算成功)→ 扫图看正反扫描线重
不重合 → 找台阶、挑平区调平 → 先深后浅地扎针尖看 cluster 圆不圆 → 在恰好跳变
的临界深度反复扎 → 台阶近阶跃即完工。

## 与 ``tip_conditioning_policy`` 的分工

那张表回答「**这根针**能承受多大的脉冲、多深的下压」—— 按材料/制法/形态查,带
安全包络(``max_abs_pulse_v`` / ``max_poke_depth_m``),超上限拒绝不夹紧。
这里回答「**这套流程**每一步的判据是什么」—— 成功阈值、预算、扫描尺寸、步进。

两者相乘才是一次真实的动作,而且**包络永远赢**:本模块的 ``pulse_v`` 默认 10 V
是金属丝针尖的常规做法,把它用在铂铱针(包络 8 V)或 qPlus(3 V)上会被那张表
拒绝 —— 那不是 bug,是保护。流程遇到拒绝要如实报告并停下,不许自己降压重试:
「悄悄改成 8 V 接着打」会让使用者误以为做的仍是原定实验。

## 与 ``experiment_prefs`` 的分工

那一层刻意「空即无值」(不替用户发明数字,只作提示文本注入)。这里每个值都必须
有出厂默认,否则流程根本跑不起来;它们也不是给模型读的文本,是代码消费的判据。

污染半径不在这里:``avoid_radius_pulse_nm`` / ``avoid_radius_tip_shape_nm`` 已经
在 instrument_profile 里,扫描地图的避让圆用的就是它们。同一个物理量有两个来源,
迟早各自漂移。针尖是不是 qPlus 也不在这里 —— 那是 ``tip_state.is_qplus()``。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, fields, replace
from typing import Any

from mast.i18n import tr

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NobleTipWorkflow:
    """一次针尖修整流程的全部判据与预算。单位写在字段名里。"""

    # ── 进针 ────────────────────────────────────────────────────────────
    #: 进针偏压。坏针尖导电性差,几百 mV 下反馈可能看不到电流而一路压下去;
    #: 几伏先把隧穿建立起来(常规 3~6 V)。
    approach_bias_v: float = 4.0

    # ── 结条件 ──────────────────────────────────────────────────────────
    #: 修针期间:低偏压 + 大电流 = 针尖离表面很近,脉冲/扎针才作用得上
    #: (常规 20~100 mV,电流到 1 nA)。
    junction_bias_v: float = 0.05
    junction_setpoint_a: float = 1.0e-9

    # ── A 大修(电脉冲)─────────────────────────────────────────────────
    #: 起始脉冲电压。**会过针尖方案表的包络**,不合适的针尖上会被拒绝。
    pulse_v: float = 10.0
    pulse_width_s: float = 0.5
    #: 判「这一发有没有打动针尖」的 Z 跳变门槛,**取绝对值,方向不限**。
    #:
    #: 出厂 20 nm 取「向上跳变几十 nm」的下沿。
    #: 判据为方向不限:判的是针尖**被改变
    #: 了没有**,不是改变的方向。消费方 ``_tip_phases.pulse_phase`` 因此用
    #: ``direction in ("up","down") and abs(dz) >= 本值``。
    #:
    #: ⚠️ **down 方向沿用同一个数没有数据支持** —— 这个数是照向上跳变定的,
    #: 而 up/down 的幅度分布可能不同。要分开就加第二个字段,别把一个数当两个用。
    #:
    #: **出厂取 5 nm**,脉冲成功的判定
    #: 规则改为「变化超过 5 nm 即成功」。
    #: 依据:曾有一批脉冲全部判为 ``no_effect``,而其间
    #: ``|dz|`` 的读数大多在 0.1 nm 量级以下 —— 20 nm 那条线几乎不会被越过。
    #: 5 nm 仍然远高于噪声(``step_tol_nm=0.5``),但把「明显动了一下」收进来。
    #: down 方向沿用的阈值未标定；目标仪器需独立核验。
    pulse_success_dz_nm: float = 5.0
    #: 同一极性连打这么多发没效果就翻极性(先一种极性,无效再换)。
    pulses_per_polarity: int = 4
    #: **同一个落点**上最多连打这么多发 —— 只在「打了没效果」时才用得上。
    #:
    #: 规则:一发脉冲**即使没成功也要标记、也要换地方**;
    #: 但连续脉冲场景可以豁免 —— 没打动针尖时,允许在原地再来若干发,
    #: 攒够这个数才被迫挪窝。
    #:
    #: 打**成功**了永不豁免:成功意味着确实有材料动了,那个点从此是坑
    #: (脉冲阶段本来也就此 break,而落点已经进了脏点表,verify 会躲开它)。
    #:
    #: 这个数取 4,与 ``pulses_per_polarity`` 同值,**不是巧合也不是测量**:
    #: 「同一极性连打 4 发无效就翻极性」已经是一个既定的「连打几发」,
    #: 让两者对齐,一轮同极性连打正好用掉一个落点。要改就单独改这里 ——
    #: 它和翻极性是两件事,只是初值借了同一个数。
    pulse_same_spot_budget: int = 4
    #: 大修阶段的总发数上限。行动预算,不是重试次数 —— 打不动就该报告。
    pulse_budget: int = 20
    #: 扫描验证不过关时补的递减脉冲(常用 7 → 5 → 3 V)。
    #: qPlus 针尖不做 —— 见 :func:`descend_sequence`。
    descend_pulse_v: tuple[float, ...] = (7.0, 5.0, 3.0)

    # ── B 扫描验证 ──────────────────────────────────────────────────────
    #: 回正常成像条件(常规 500 mV~2 V,50~200 pA)。
    verify_bias_v: float = 1.0
    verify_setpoint_a: float = 1.0e-10

    verify_scan_nm: float = 50.0

    verify_pixels: "int | None" = 128
    #: 验证帧的每线时间。``None`` = **走档位表**(历史行为,``PrepareNobleTip``
    #: 这条路径行为不变)。
    #:
    #: 一个**不对称缺口**:forge 的三张评估图里,台阶图和簇图
    #: 各有专属线时(``forge_step_line_time_s`` / ``forge_cluster_line_time_s``),
    #: 唯独 verify 那张没有 —— 它一直靠档位表。而档位表是**成像**档,
    #: 可随时修改;改了之后 verify 的帧时会跟着变,而没有任何地方约束它。
    #:
    #: 帧时 = 行数 × 每线时间 × 2(**视野不在这个式子里**);
    #: 针尖速度 = 视野 ÷ 每线时间。
    verify_line_time_s: "float | None" = None
    #: 快扫方向上 z-forward 与 z-backward 的相似度下限。允许整体 shift ——
    #: 扫得快时两个方向本来就差一个滞后,看的是对齐之后还重不重合。
    fwdbwd_threshold: float = 0.80
    #: 验证↔大修之间来回几次就认输并如实报告。
    max_verify_rounds: int = 3

    # ── C 台阶与调平 ────────────────────────────────────────────────────
    #: 找台阶的视野(常规 100~200 nm)。
    step_scan_nm: float = 200.0
    #: 这个视野上找不到台阶时,退到多大再找一次。``None`` = **不回退**
    #: (历史行为,``PrepareNobleTip`` 这条路径行为不变);forge 覆写成 200 nm。
    #:
    #: 找不到台阶就换 200 nm 接着找,
    #: 不把小起伏硬算作台阶。回退图**只用来找台阶** —— 不重判正反扫
    #: (那一条在 100 nm 那张上已经判过),也不当验收图(验收图的尺度必须钉死,
    #: 见 ``forge_step_scan_nm``)。
    step_fallback_nm: "float | None" = None
    #: 在其中挑一块无台阶的平区调平(常规 20~100 nm)。
    flat_region_nm: float = 50.0

    step_pixels: "int | None" = None
    step_line_time_s: "float | None" = None

    accept_sharp_edge_nm: "float | None" = None

    # ── D 精修(扎针尖)─────────────────────────────────────────────────
    #: D1 首扎深度。**500 pm**(操作范本,`docs/v2/design/
    #: operator_tip_forging_reference_20260810.md` §一.6)。
    #:
    #: 早期默认是 1.5 nm —— 范本里实际起手是 −500 pm,收敛到 200 pm,
    #: 旧默认深 3 倍。改小是按范本对齐:浅扎对针尖的破坏远小于脉冲,
    #: 大深度扎针才更具破坏性。因此默认浅扎、多次;真正该省着用的是脉冲。
    poke_depth_nm: float = 0.5
    #: qPlus 传感器上扎针前把**偏压**降到这个量级,**降低**音叉起振的概率。
    #:
    #: 操作范本:扎针时 Z 大幅弹起,是 qPlus 针尖在 Q 很高时的特殊现象
    #: (**音叉起振**);对 qPlus 针尖改用 20 mV 偏压扎针后不再弹起。
    #:
    #: ⚠️ **不是必然,别写成必然**:只有 qPlus 针尖有这个
    #: 音叉起振的问题,且只在 Q 高时才发生,并非每次都有;普通针尖没有。
    #:
    #: 三条约束照此:① 只对 qPlus(``poke_bias_v`` 出厂 None = 普通针尖不动偏压
    #: —— 一刀切会把好用的成像偏压条件也砍掉);② 起振是**概率性**的,与 Q 有关,
    #: 所以「扎完没起振」**不能**当成"这个偏压是对的"的证据,反过来也不行;
    #: ③ 措辞一律是「降低概率」,不是「防止」。
    #:
    #: 起振了的后果:Z 大幅弹起 → 原地扫图满屏高团簇、平台形貌消失 → 判定针尖坏了
    #: → 去打脉冲。**流程会陷进一个自己造出来的循环,而且每一步判断都是对的** ——
    #: 针尖确实坏了,只是坏在它自己手上。检测与应对见 ``poke_ringdown_*``。
    #:
    #: **机制是猜测,标明为猜测**:激励很可能是**静电力 ∝ V²**,1 V → 20 mV 是
    #: 2500 倍力差;若如此,关键量是**冲量**而不是「20 mV 这个数有魔力」——
    #: 这也解释了为什么要先慢慢改 bias,再做 z 的扎入。
    #: 未经验证。
    #:
    #: 只对 qPlus 生效,判据是针尖登记表里**声明的 form 字段**
    #: (``mast.core.tip_state.is_qplus``),不是名字里有没有 "qplus"
    #: —— 子串匹配这个仓栽过(``"on"`` 是 ``"z_controller_off"`` 的子串)。
    poke_bias_qplus_v: float = 0.020
    #: 非 qPlus 针尖扎针时的偏压。``None`` = **不动偏压**(跟随当前值),这正是
    #: 范本参数组里的「change bias 关、bias value 跟随现在的 bias」。
    #: 范本只标定了 qPlus 那一个数,所以这里不替别的针尖发明数字。
    poke_bias_v: "float | None" = None
    #: 走哪条路把 20 mV 加到结上。见 ``_tip_phases._poke_step`` 的长注释。
    #: False(默认,也是**唯一还开着的**)= 先缓慢把偏压改到 20 mV,再用
    #: change_bias=关 扎 —— 这就是范本的做法。
    #: True 那条(shaper 自己 change-bias)**已停用**:它做不到「缓变」,见
    #: ``_poke_step``。留着字段是为了让"为什么不能走那条"有地方写。
    poke_bias_via_shaper: bool = False
    #: 改到扎针偏压时的斜率。要求**先缓慢改变 bias**,z 的扎入排在
    #: 它之后。阶跃改偏压本身就是一次冲量,而冲量正是(猜测的)激励来源。
    #: 0.2 V/s ⇒ 1 V → 20 mV 约 5 s。**未标定**:没有给定数值,这是按「慢」
    #: 取的一个明显偏慢的值,真机上可调。
    poke_bias_slew_v_per_s: float = 0.2
    #: 改完偏压之后、下压之前的静置时间。同上,**未标定**。
    poke_bias_settle_s: float = 1.0

    # ── 音叉起跳(ring-up)的检测与等待 ────────────────────────────────
    #
    # 需求:能发现并应对起跳。
    #
    # 办法:用**振幅通道**。本仓已有一条事实 —— STM 模式下 qPlus 振幅通道是噪声,
    # 唯一用处是进针时「振幅归零 = 撞上了」。**平时是噪声,音叉一响它就变成真信号**,
    # 所以它天然是这个事件的探测器:扎之前连读取基线(噪声底),扎之后连读一段,
    # 看有没有远高于基线的包络、以及它什么时候衰减回去。
    #
    # 衰减时标 τ = Q/(π·f₀);典型 qPlus(f₀ 数十 kHz、Q 数 10⁴)⇒ τ 约零点几秒,几个 τ 才算完 ——
    # 百毫秒到秒量级,现有读取频率看得见**包络**(谐振本身(数十 kHz)远超采样率,
    # 只会混叠,所以看包络不看波形)。
    #
    # ⚠️ **阈值全部未标定,而且这里不下硬判定。** 待采集:扎针前后的振幅
    # 序列、故意高偏压扎的正例、20 mV 的负例、以及 f₀ 与 Q。拿到之前,这套东西
    # **只测量、只报数**:``ring_up_detected`` 恒为 None(判不了),不是 False。
    # 把「没标定」写成「没起跳」正是本仓反复栽的那一跤。
    #: 扎之前采基线的时长。
    poke_amp_baseline_s: float = 0.3
    #: 扎之后盯振幅的时长(≈ 几个 τ)。
    poke_amp_watch_s: float = 1.0
    #: 采样率(振幅走 ``Signals_ValGet`` 通用路,实际上限约 50 Hz)。
    poke_amp_poll_hz: float = 50.0
    #: 等 ring-down 回到基线的超时。**超时不判失败**,只如实记「没等到」。
    poke_ringdown_timeout_s: float = 5.0
    #: 判「已经衰减回去」的相对判据:后段 RMS ≤ 基线 RMS × 这个倍数。
    #: **未标定** —— 1.5 是个占位数,待采集基线数据。
    poke_ringdown_settle_ratio: float = 1.5
    #: 扎上了但不够圆时,**同一深度换地方**再试几次才加深。
    #: 范本序列是 500 → 500 → 500 → 200 pm:不圆是**位置**的问题,不是深度
    #: 不够。加深是等这几次都不圆之后才做的事。
    poke_same_depth_retries: int = 3
    #: 断反馈状态下的驻留时间(常用 500 ms)。
    poke_dwell_s: float = 0.5
    #: 没扎上就加深这么多比例再来。
    #:
    #: ⚠️ 这条只服务「**没碰到**」。碰到了但簇不好是另一回事,那时候要
    #: **变浅**(见 `poke_shallow_frac`)—— 两种失败长得像,处置正相反。
    poke_deepen_frac: float = 0.20
    #: 接触上了、但簇太大/不圆时,深度**乘**这个数(< 1 ⇒ 变浅)。
    #:
    #: 0.4 = 范本序列 500 → 500 → 500 → **200** pm 的那一跳,不是随意选的数。
    #:
    #: 标定给出的支持:
    #:   · 深度 → 轴比      呈显著负相关(只算真接触上的)
    #:   · 同一检验放到没接触的那组:不显著 —— 没有。
    #:
    #: **要推翻这个默认值,需要回答**:换一根针、换一块样品之后,
    #: 「接触上但不圆 → 变浅会更圆」还成立吗?(现有证据只来自一次标定。)
    poke_shallow_frac: float = 0.40

    poke_depth_min_nm: float = 0.2
    #: D1 深扎的深度上限 —— 再深就不是修针尖而是拿针尖犁地了。
    #: (方案表的 ``max_poke_depth_m`` 仍然是更硬的那道闸。)
    #: 「没扎上就加深」的放弃点。**2 nm。**
    #:
    #: 流程是 500 pm 起、每次 +20%(`poke_deepen_frac`),到这个数还没接触就放弃。
    #: 2 nm ≈ 8 档。出厂原值是 8 nm,而它一直被针尖包络夹到 5 nm ——
    #: 现在包络已拉满(10 nm),这个数**不会再被夹**,流程走的就是这里写的值。
    poke_depth_max_nm: float = 2.0
    #: 看 cluster 的小图视野。
    cluster_scan_nm: float = 10.0
    # ── D 的落点平坦性──────────────────────────────────
    #
    # 背景:扎针之前应当先找台面并调平 —— 查下来 C 相确实找了平区
    # 也调了平,但**扎针落点没用那个结果**:``FindCleanSpot`` 是纯几何避让
    # (参数只有 purpose / exclude_spots / from / max_distance / count),
    # 不含任何地形信息 ⇒ **可能扎在台阶边缘上**,而那是结果最不可控的地方。
    # C 相算出的 ``flat_center`` 全仓只有一处写、零处读。
    #
    # 方案:扎完四个之后再扫一张 100 nm 图找台面 —— 落点用完就地重扫,
    # 不必换站。
    #
    #: 扎针落点要求的平区边长。**50 nm。**
    #:
    #: 要求:必须找到一个 50 nm 的台面,找不到就扩大范围继续找,
    #: 在台面中间扎针尖 —— 这样可以排除其余干扰判断的因素。
    #:
    #: 20 nm 那版的理由是「簇图只有 10 nm 视野,20 nm 窗足够把它整个装进平台」——
    #: 那只保证**不跨台阶**。但标定显示,判据曾误杀若干帧,它们
    #: 都是「针尖能用,可画面里本来就有别的东西」:其中一帧的三块
    #: 相距比整个簇图还宽。判据分不出「这块是这次扎出来的」和
    #: 「这块本来就在」—— 而 50 nm 的干净台面**从源头上不给它这个机会**。
    #:
    #: ⚠️ 代价:窗口越大越难找。台阶密集区里窗口越小每图给出的点越多
    #: —— 窗口大小是主导因素。50 nm 会更稀,
    #: 所以 ``poke_flat_dry_refills`` 同时从 3 提到 12(找不到就扩大范围继续找)。
    #:
    #: 与 ``flat_region_nm`` 的 50 nm **同值但不同来源**,不要合并:那一个是
    #: 调平要的尺度(测倾斜需要更大的面),这一个是「视野里不许有别的东西」。
    #: ⭐ 标定后改成 **35 nm**(数据支持)。
    #:
    #: 在若干张真 200 nm 找台面图上逐档试:**35 nm 是拐点**,
    #: 再放宽收益为零,收紧则有解率开始下降。
    #:
    #: 更大窗口的理由仍然成立(视野里不许有别的东西),只是受限于样品的台面
    #: 宽度。**这个数是跟着样品走的,换样品要重测。**
    poke_flat_window_nm: float = 35.0
    #: 一片里找不到平区时,**换一块重扫**几次才认输。
    #:
    #: 3 → **12**(配 50 nm 窗)。找不到就扩大范围继续找。
    #: 一次重扫 = 一张 100 nm / 128 px / 0.586 s ≈ **2.5 min**,12 次 ≈ 30 min ——
    #: 在 12 小时总预算里可以接受,而真正的闸门本来就是时间预算,这个数只是
    #: 失控保险(与外环 ``_HARD_SITE_CAP`` 同一个角色)。
    #: 后又从 12 提到 **20**(同时加大搜索力度)。
    #:
    #: ⚠️ 早期版本**从来没生效过** —— ``FindFlatRegion`` 把「这一片没有可用
    #: 平区」以 ``success=False`` 返回,而 ``flat_poke_sites`` 把它读成「判不了」,
    #: 于是走几何兜底而不是重扫,直到那条分类被修好之后这个数才第一次真正管事。
    #:
    #: 20 次 × 5 min/张 ≈ 100 min。按标定时多数 200 nm 图都有解,
    #: 20 次几乎不可能全空 —— 真空了就是这一大片确实该换区,不是"再找找"。
    poke_flat_dry_refills: int = 20
    #: 落点之间的最小间距。默认取针尖整形的避让半径(``avoid_radius_tip_shape_nm``
    #: 出厂 30 nm):语义是「这一点周围 30 nm 内不能再用」,所以两点相距 30 nm
    #: 就够 —— **不是 60**。(按 2× 算会把可用点数砍掉一多半。)
    #:
    #: ⚠️⚠️ **这个数只管住「同一批」,管不住「下一批」(需配合 exclude_used_spots)。**
    #:
    #: 它被送进 ``FindFlatRegion.min_separation_m``,而那个键只用来让**这一次**
    #: 交出来的 N 个点互相隔开。重扫一张图再要一批时,图上最平的还是那一块 ——
    #: 于是同一个坐标被反复交出来。曾复现:
    #:
    #:     连续多针落在同一坐标上,坐标一位不差。
    #:     相邻两针间距中位数很小,多次为 **0**
    #:
    #: 后果不是「多扎了几针」,是**判据被自己的坑骗了**:簇图视野只有 10 nm,
    #: 上一针的坑就留在画面里,于是 ``AssessClusterRoundness`` 数到两坨、
    #: 报「疑似多针尖」。逐帧核查后确认:判成 multi_tip 的帧,额外那一坨
    #: (坑量级 h ≥ 150 pm)**全部**落在此前记录过的旧落点上,距离很近。
    #:     其中一组的第二坨与更早一帧里的坑同位置、同面积、同高度 ——
    #:     不是「像」,是同一个物体;而扎针之前扫的对照帧只有一坨。
    #:
    #: 修法是把 ``exclude_used_spots``(``FindFlatRegion`` 一直就有的参数)接上,
    #: 见 ``_tip_phases.flat_poke_sites``。30 nm > 簇图 10 nm ⇒ 旧坑不可能再进簇图。
    poke_site_separation_nm: float = 30.0
    #: 找落点那张图的视野 / 行数 / 每线时间。
    #:
    #: **200 nm**。窗口提到 50 nm 之后必须跟着放大 ——
    #: 算术是这样的:50 nm 的窗要整块落在图里,窗心只能待在离边 25 nm 以内,
    #: 于是可放窗的区域在 100 nm 图上只有中间 **50×50**(全图 1/4),
    #: 在 200 nm 图上是 **150×150**(**9 倍**)。
    #: 帧时 2.5 → 5 min,而每分钟的找到率反而高一倍多。
    #: 采用此值。
    #:
    #: **128 px 而不是 256**:这张图只用来找台面,台阶宽度是几 nm;
    #: 200 nm / 128 px = 1.56 nm/px 仍够辨台阶边缘的位置。
    poke_site_scan_nm: float = 200.0
    poke_site_pixels: int = 128
    #: **1.172 s = 200 / 170.65**,由 ``forge_v_tip_nm_s`` 派生,不是挑的数 ——
    #: 视野翻倍而线时不变就等于把针尖速度也翻倍(341 nm/s)。
    #:
    #: 不靠 ``forge_line_time_s()`` 自动限速来兜:那条路会**每次跑都吐一条
    #: 「已放慢」的说明**,而说明是给「出乎意料」用的。存着的值和实际跑的值
    #: 不一致本身就是下一个人对不上账的起点(89 张分割图那次的教训)。
    poke_site_line_time_s: float = 1.172
    #: 簇小图的分辨率与每线时间。语义同 ``step_pixels`` / ``step_line_time_s``:
    #: ``None`` = 走档位表。**与台阶图分开是有意的** —— 10 nm 的簇图和 100 nm 的
    #: 台阶图不该共用一个数,哪怕今天它们的值恰好相同(只测过一个工作点)。
    cluster_pixels: "int | None" = None
    cluster_line_time_s: "float | None" = None

    min_axis_ratio: float = 0.75
    #: D2 临界浅扎:从这个深度起步,每次加这么多,直到 Z 恰好开始跳
    #: (范本:100 pm 起,150、200……)。
    #: **出厂取 300 pm。** 曾从 100 起步的两针
    #: (100 / 150 pm)什么都没扎上,等于白扎,故取 300 起。
    critical_start_pm: float = 300.0
    critical_step_pm: float = 50.0
    #: 找到临界深度后在那里反复扎几次(每次换地方),cluster 会越来越小越圆。
    #:
    #: ═══════════════════════════════════════════════════════════════════════
    #: **取 3。曾用的 5 没有任何出处** —— 这一条依据真机标定。
    #: ═══════════════════════════════════════════════════════════════════════
    #:
    #: 5 是这张表里少数几个**光秃秃**的数之一:没有来源记录、没有标定、没有
    #: 实测批次,只有上面那一行「反复扎几次」。同文件里 ``poke_shallow_frac``
    #: 有一百针标定、``poke_flat_window_nm`` 有六张真图逐档试 —— 对照之下
    #: 这个 5 是**被发明出来的**。而它是一道会否决人的闸门。
    #:

    #:
    #: **最长 4,从没到过 5。** 这样的运行以 time_budget_exhausted 收场,
    #: 「精修未达标」—— 而它其实**结构上不可能达标**。
    #:

    #:
    #: ── 为什么方向是「调小」而不是「多给点预算」────────────────────────
    #:
    #: 关键在于第 N 针是**在一根已经连续达标 N−1 次的针尖上再赌一次**,而扎针
    #: 不可逆。上面四段的结尾是同一句话:**紧跟在达标之后的那一针把针尖弄坏了**
    #: (圆度分数均明显下降)。
    #: 标定中,这一针圆之后下一针也圆的概率明显低于一,所以每多要一次连续,
    #: 拿到手的好针尖就有相当大的概率在认证它的过程中被毁掉。
    #:
    #: 「时间不是问题、宁可多试几次」在这道闸门上**不能读成把 N 调大** ——
    #: 这里的「一次尝试」不是免费观测,是一次会改变被测对象的动作。该慷慨的是
    #: **预算**(``poke_budget`` / ``poke_pulse_rescues``:允许多找几轮),
    #: 不是**连续达标次数**(要求在同一根针上多冒几次险)。
    #:
    #: ── 为什么取 3 而不是 2(范本给的区间是 2–3)────────────────────────
    #:
    #: ① 3 是给定区间里**最保守**的一端 —— 判据松了多少,松到给定区间的边上为止;
    #: ② 3 在真机上**够得着**,而 5 够不着;比 2 只多花一针;
    #: ③ 保住 ``SPECTROSCOPY_TIP.critical_repeat_n (=2) <= 本值`` 这条钉着的
    #:    不变式(``test_make_special_tip``):特异化针尖那条线**刻意**要得更少,
    #:    取 2 会把这个区别抹平。
    #:
    #: ⚠️ **这仍然是一根针、一次会话、少量临界期读数的弱标定。**
    #: 它比 5 强的地方只在于「有」——要推翻它,需要回答:换一根针之后,
    #: 「紧跟达标之后那一针把针尖弄坏」的高概率还成立吗?
    critical_repeat_n: int = 3
    #: 精修阶段的总扎针次数上限。
#: 连续这么多针都做不出「单峰且圆」的簇 ⇒ **退回去打脉冲**。
    #:
    #: 连续不出圆团簇时回退到 pulse:扎针改的是**形状**,
    #: 而一根被脉冲打钝/打歪的针,再怎么换地方扎都出不来圆簇 ——
    #: 一直扎下去是拿位置的问题当形状的问题解,而且每一针都在消耗表面。
    #:
    #: 取 8:同一深度换 3 个地方(``poke_same_depth_retries``)之后会变浅一档,
    #: 8 针≈两档都试过了还不行,那就不是深度和位置的事。
    poke_unround_streak_to_pulse: int = 8
    #: 「回退打脉冲」在一站里最多做几次。用完就换区 —— 同一片表面上反复
    #: 「打一轮脉冲再扎一轮」不收敛的话,问题多半不在这一片表面上。
    poke_pulse_rescues: int = 2
    poke_budget: int = 30

    # ── 修针外环的快扫档(ForgeAuTip)────────────────────────────────────
    #
    # 快速扫图。这几个数**只在外环里生效**:
    # ForgeAuTip 用它们覆写 verify_scan_nm / step_scan_nm / scan_timeout_s /
    # cluster_scan_nm,再把覆写过的表交给各阶段。正常成像的档位表不受影响
    # —— survey 一帧 34 分钟对成像是对的、对修针评估是荒谬的,而这两件事共用一个
    # 数字迟早害了其中一件。
    #
    # ═══ 实测依据 ═══
    # **100 nm / 256 px / 0.15 s 线 = 77 s/帧**:形貌、扎坑、锐度三个判据
    # 全部可用;噪声与慢扫相近,与慢扫图相似度高。而且快扫下
    # **锐度判得更准** —— 热漂移涂抹小,对验收是利好。
    #
    # ⚠️ **50 nm 档是外推,不是实测。** 帧时只由 线数 × 线时 决定,所以 50 nm 与
    # 100 nm 同为 77 s;取 50 nm 是因为 0.195 nm/px 与慢扫同分辨率、更适合看坑簇。
    # 这条外推若被推翻(例如 50 nm 下判据反而不可用),换回 100 nm 即可,帧时不变。
    #
    # ── 50 → 20 nm,而**当初选 50 的那个理由被保住了** ──────
    #
    # 选 50 的依据是「0.195 nm/px 与慢扫同分辨率、更适合看坑簇」。
    # 现在 verify 有了自己的行数(``verify_pixels = 128``,那是唯一能缩短一帧的
    # 旋钮),于是:
    #     旧 50 nm / 256 px = 0.195 nm/px
    #     新 20 nm / 128 px = 0.156 nm/px   ← 比原来更细
    # 像素尺度没退,反而进了一点。
    #
    # 而尺寸这个旋钮真正买到的是**针尖速度** = 视野 / 每线时间:
    # 机上档位表给这个尺度 2.0 s/线 ⇒ 50 nm 是 25 nm/s,20 nm 是 10 nm/s。
    # (⚠️ 改小尺寸**不省时间** —— 上面那句「帧时只由 线数 × 线时 决定」正是这个
    #  意思;「窄条能更快走完」是常见的误解。)
    # ── 20 → 100 nm,而且 verify 与找台阶**合并成一张图** ──────
    #
    # 决定:B 相保留这个步骤(一张图约七分钟),但这一张改成
    # 100 nm 视野、5 分钟。同时把两张图合并。
    #
    # 两条理由:
    # ① **20 nm 判不出台阶**(20 nm 帧 `no_step`,整帧起伏
    #    很小 —— 视野落在单个平台里)。verify 判的是正反扫重合、不需要台阶,
    #    所以 20 nm 对 verify 本身没错;但它**不可能兼任找台阶**。
    # ② 合并前 verify 和 level 在同一片区域各扫一张图(20 nm + 100 nm),
    #    而两张都是「拍一张看看」。合并成 100 nm 一张,verify 判正反扫、
    #    level 在同一帧上找台阶挑平区 —— 一圈省一张图。
    #
    # 找不到台阶时退到 ``forge_step_fallback_nm`` 再找一次(找不到
    # 台阶就换 200 nm 接着找,不把小起伏硬算作台阶)。**正反扫不重判** ——
    # 那一条在 100 nm 这张上已经判过了,回退图只为找台阶。
    forge_scan_nm: float = 100.0

    forge_step_scan_nm: float = 100.0
    #: 100 nm 上找不到台阶时的回退视野。
    #:
    #: 只用来**找台阶**,不重判正反扫、也不当验收图。找不到就找不到 ——
    #: 不把小起伏硬算作台阶。
    forge_step_fallback_nm: float = 200.0
    #: 看簇的小图。深扎坑尺度远小于验证帧,单独给一档。
    forge_cluster_scan_nm: float = 10.0


    forge_step_pixels: int = 256
    forge_step_line_time_s: float = 0.15
    #: verify 帧(兼任找台阶)的行数与每线时间。
    #:
    #: **256 px 而不是 ``verify_pixels`` 的 128**:这张图现在还要用来找台阶和挑
    #: 平区,0.781 nm/px(100 nm/128 px)对找台阶偏粗;256 px ⇒ **0.391 nm/px**,
    #: 是现场标定过、有据可比的分辨率。
    #:
    #: **0.586 s/线**由 5 分钟的帧时目标反推:
    #:     帧时 = 行数 × 线时 × 2  ⇒  300 s = 256 × 0.586 × 2
    #: 针尖速度 = 100 nm ÷ 0.586 s = **171 nm/s** —— 远低于曾
    #: 刮坏针尖的 488 nm/s。
    #:
    #: ⚠️ 这两个是**测量/换算值,不是技能参数** —— 与 step/cluster 那两对同理:
    #: 要改就改这里,改这里的人会先读到上面这段依据。
    forge_verify_pixels: int = 256
    forge_verify_line_time_s: float = 0.586
    forge_cluster_pixels: int = 256
    forge_cluster_line_time_s: float = 0.15

    # ── 线时不再是四个各自拍下来的常数,而是**由速度上限派生** ────
    #
    # 上面那四个数每一个都有自己的理由,而**没有任何一处把它们放在一起按速度看
    # 过**。把「视野 ÷ 线时」算出来才看得见:
    #
    #     verify   100 nm / 0.586 s = 171 nm/s   ← 已确认的那一帧
    #     step     100 nm / 0.15  s = **667 nm/s**  ← 比刮坏针尖那次还快 37%
    #     回退图   200 nm / 0.30  s = **667 nm/s**  ← 按视野缩放,忠实地继承了超速
    #     cluster   10 nm / 0.15  s =  67 nm/s   ← 小视野替它兜住了,是运气不是设计
    #     poke_site 100 nm / 0.586 s = 171 nm/s
    #
    # 而曾实测:50 nm 图 0.1024 s/线 = **488 nm/s** 过快,
    # 刮坏了针尖。验收图正是在这个速度上判「针尖够不够锐」——
    # **在自己刚刮出来的痕迹上判针尖**,这是一种自我污染的判据形状。
    #
    # ── 这个上限的数是哪来的:取自一帧已确认的评估图 ──────────
    #
    # 已确认的一帧:100 nm 图,帧时 5 分钟。
    #     300 s = 256 行 × 0.586 s × 2  ⇒  100 nm ÷ 0.586 s = **170.6 nm/s**
    # 这是唯一一帧明确指定过速度的 forge 评估图。
    # 取它当上限即「forge 的图不得比该帧更快」,不引入新的数字。
    #
    # ⚠️ **它不是「Au 上的安全速度」,那个数仍然缺席。** 现有数据只有:
    #   * 488 nm/s —— 实测**不行**(唯一的负例);
    #   * 170.6 / 100 / 50 nm/s —— 分别是上述已确认的那帧、成像档位表的 100 nm 档、
    #     某次靠 ``forge_line_time_s=2.0`` 绕过去时实际跑的速度(都跑成了,
    #     但**没有一次是为了标定速度而做的**——证据回答的不是被问的那个问题)。
    # 真要标定,需要的是「同一根针在若干速度下扫同一片表面,看锐度判据怎么变」
    # 这样一组对照,需要操作员在真机上做。该标定项
    # 因此**不结案**,只是从「全表最该动的数」降级成「已按上述已确认的帧收口,
    # 等标定」。
    #
    # **为什么是上限而不是等式**:等式会把 cluster 图从 67 nm/s **提速到** 171,
    # 而簇的圆度/峰数判据本来就悬(实测轴比偏低)。上限只拽慢超速的那些,
    # 已经更慢的一个不碰 —— 「只在违法时动」,与包络对账同一条规矩。
    #
    # ⚠️ **为什么是 170.65 而不是 170.6**:100/0.586 = 170.6484…,写 170.6 会让
    # verify 那一帧**自己超过自己派生出来的上限**,于是每一跑都被「修正」成
    # 0.58617 s —— 一个 0.03% 的无意义改动,却会在报告里留下一条「已限速」说明,
    # 把真正的超速项淹掉。多这一位小数买的是「该已确认的帧原样通过」。
    #
    # ═══════════════════════════════════════════════════════════════════════
    # 会推翻这套推理的三个观测(**唯一真源,别的地方只许指过来**)
    # ═══════════════════════════════════════════════════════════════════════
    #
    # 整条推理压在一句话上:**488 是我们唯一知道结果的速度**。这句话是**会过期
    # 的**,而过期时没有任何东西会报警 —— 本仓管这个叫「修好之后,旧的理由会静静
    # 变成假话」。所以把翻盘条件写在这里,而不是留在对话里。
    #
    # ── ① 往下:出现**第二个实测负例、且低于 488** ────────────────────────
    #
    # 「488 是唯一的点」这个前提当场没了,而它**同时**推翻三样东西,一样都不能漏:
    #     (a) ``forge_v_tip_nm_s = 170.65`` 这个锚点 —— 它的全部依据是「已确认的
    #         那一帧比唯一的负例慢得多,所以取它是保守的」。新负例若落在 170.65
    #         附近甚至以下,这句话就不成立了;
    #     (b) ``_BOUNDS`` 的上界 488(见 ``_EXCLUSIVE_UPPER``)—— 它排除的是
    #         「那个测过的坏点」,坏点换了,边界就换了;
    #     (c) ``forge_line_time_s`` 那段「≥488 才有依据拒绝」的分档 —— 分档线正是
    #         由「哪个点是实测的」定的。
    # ⇒ 三处都要重做,**不是改一个数**。
    #
    # ── ② 往上:把上限抬到 488 以上 ──────────────────────────────────────
    #
    # 要回答:**凭哪一次观测认为比刮坏针尖那次更快是安全的?** 没有答案就别抬。
    # 这一条已经钉在 ``test_the_ceiling_does_not_claim_to_be_a_calibration``。
    #
    # ── ③ 往上,但是好消息:出现一个**高于 170.65 的正例** ────────────────
    #
    # 例如标定时发现 300 nm/s 扫出来的针尖照样锐。这**不推翻安全性**,它解锁的是
    # (170.65, 488) 那一整段**现在零观测**的区间 —— 也就是让修针快回去。
    #
    # ⚠️ 这一条与前两条**性质不同,别混在一起读**:①② 是「出事了才会知道」,而
    # ③ 是**正常标定工作里顺手就会产生的**。一个要等事故才触发的条件,和一个日常
    # 工作就能填上的条件,后者才是真正可能被填上的那个 —— 所以它最该被写出来,
    # 也最该让使用者知道。(它同时是唯一能把 5 min/帧再降下来的路,而慢一倍是
    # 每天都在付的成本。)
    #
    # 三条在参考文档里有对应的人话版本,
    # 而且 ①③ **刻意用不同语气写**:① 是再刮坏一次针时需要上报,
    # ③ 是标定时若发现可以更快,则可上调、让修针恢复速度。
    forge_v_tip_nm_s: float = 170.65

    #: 用户对**所有**评估图的一次性覆写(``None`` = 用上面那四个按阶段的值)。
    #:
    #: 这一对**是** ForgeAuTip 的声明参数,而上面四个不是 —— 分界是实际要求
    #: 的形式:「扫图**一律** 256 px / 0.15 s」,而不是「台阶图 0.15、簇图 0.2」。
    #:
    #: 这一对参数的由来:早先 ForgeAuTip 没有任何 pixels /
    #: line_time 参数,agent 为了保住扫描参数控制权**放弃这个技能**、改用原语手工
    #: 拼外环,一个原语一次模型调用,容易触到调用上限,仪器停在中途。
    #: **没有参数不等于模型不会填数字 —— 它会绕过整个技能去填。**
    #: 那比一个带 explicit-only 契约的参数坏得多(ScanAt 自己的 line_time_s /
    #: pixels 就是这么处理同一个张力的:开出来,但规定「只有用户明确给出才传」,
    #: 并把来源记进 trace)。
    forge_pixels: "int | None" = None
    forge_line_time_s: "float | None" = None

    forge_scan_timeout_s: float = 1300.0

    # ── 通用 ────────────────────────────────────────────────────────────
    #: 换点之后等一下再动手 —— 一次横向移动会重新激起压电蠕变。
    move_settle_s: float = 0.3
    #: 单次扫描的等待上限。
    scan_timeout_s: float = 300.0


#: 出厂基线:Au / Ag / Cu 单晶,或 mica 上这三者的膜。
NOBLE_METAL_BASELINE = NobleTipWorkflow()


#: 每个字段的可接受范围。**越界拒绝,不夹紧** —— 夹了调用方会以为自己设的是 X
#: 而实际跑的是 Y,而这里每个数字都会变成真实的硬件动作。
_BOUNDS: dict[str, tuple[float, float]] = {
    "approach_bias_v": (-10.0, 10.0),
    "junction_bias_v": (-10.0, 10.0),
    "junction_setpoint_a": (1e-12, 1e-7),
    "pulse_v": (-10.0, 10.0),
    "pulse_width_s": (1e-3, 2.0),
    "pulse_success_dz_nm": (0.1, 1000.0),
    "pulses_per_polarity": (1, 50),
    "pulse_same_spot_budget": (1, 50),
    "pulse_budget": (1, 200),
    "verify_bias_v": (-10.0, 10.0),
    "verify_setpoint_a": (1e-12, 1e-7),
    "verify_scan_nm": (1.0, 5000.0),
    # 与 SetScanBuffer 的 ParameterSpec 对齐 —— 表里存一个下游必然拒绝的值
    # 没有意义(同 scan_policy._TIER_FIELD_SPEC 那条自述)。
    "verify_pixels": (16, 4096),
    "fwdbwd_threshold": (0.0, 1.0),
    "max_verify_rounds": (1, 20),
    "step_scan_nm": (1.0, 5000.0),
    "flat_region_nm": (1.0, 1000.0),
    # 与 SetScanBuffer / ConfigureScan 的 ParameterSpec 对齐 —— 表里存一个下游
    # 必然拒绝的值没有意义(同 scan_policy._TIER_FIELD_SPEC 那条自述)。
    "step_pixels": (16, 4096),
    "step_line_time_s": (1e-4, 600.0),
    "cluster_pixels": (16, 4096),
    "cluster_line_time_s": (1e-4, 600.0),
    "forge_step_pixels": (16, 4096),
    "forge_step_line_time_s": (1e-4, 600.0),
    "forge_cluster_pixels": (16, 4096),
    "forge_cluster_line_time_s": (1e-4, 600.0),
    "forge_pixels": (16, 4096),
    "forge_line_time_s": (1e-4, 600.0),

    "forge_v_tip_nm_s": (1.0, 488.0),
    "poke_depth_nm": (0.01, 100.0),
    # 扎针偏压。上限 1 V 是刻意的:范本里这个数是 **20 mV**,而它存在的理由是
    # 「别把音叉激起来」—— 一个几伏的"扎针偏压"不是调参,是把这条物理反过来用。
    # 真要在高偏压下动表面,那是 pulse 的活,走 pulse 的闸门。
    "poke_bias_qplus_v": (-1.0, 1.0),
    "poke_bias_v": (-1.0, 1.0),
    "poke_same_depth_retries": (0, 20),
    "poke_bias_slew_v_per_s": (0.01, 100.0),
    "poke_bias_settle_s": (0.0, 30.0),
    "poke_amp_baseline_s": (0.05, 10.0),
    "poke_amp_watch_s": (0.05, 30.0),
    "poke_amp_poll_hz": (1.0, 2000.0),
    "poke_ringdown_timeout_s": (0.0, 60.0),
    "poke_ringdown_settle_ratio": (1.0, 100.0),
    "poke_dwell_s": (0.0, 10.0),
    "poke_deepen_frac": (0.01, 5.0),
    # 上界 1.0 **开区间**(见 `_EXCLUSIVE_UPPER`)—— 填 1.0 就是「不变浅」、
    # 填 >1 就是偷偷变回加深。这条分支的**方向**是它存在的全部理由,
    # 而 `depth * frac` 这行代码本身认不出方向,所以闸门必须认。
    "poke_shallow_frac": (0.01, 1.0),
    "poke_depth_min_nm": (0.01, 100.0),
    # 平区窗上界取找台面那张图的视野:窗比图还大,``FindFlatRegion`` 永远
    # 找不到,而它会如实报「这里没有」⇒ 流程会一直重扫到认输。所以这里拦住。
    "poke_flat_window_nm": (1.0, 5000.0),
    "poke_flat_dry_refills": (1, 100),
    "poke_depth_max_nm": (0.05, 100.0),
    "cluster_scan_nm": (0.5, 500.0),
    "min_axis_ratio": (0.0, 1.0),
    "critical_start_pm": (1.0, 10000.0),
    "critical_step_pm": (1.0, 5000.0),
    "critical_repeat_n": (1, 50),
    "poke_unround_streak_to_pulse": (1, 100),
    "poke_pulse_rescues": (0, 10),
    "poke_budget": (1, 200),
    "forge_scan_nm": (1.0, 5000.0),
    "forge_step_scan_nm": (1.0, 5000.0),
    "forge_cluster_scan_nm": (0.5, 500.0),
    "forge_scan_timeout_s": (1.0, 3600.0),
    "move_settle_s": (0.0, 10.0),
    "scan_timeout_s": (1.0, 3600.0),
}

#: 上界**开区间**的字段 —— ``_BOUNDS`` 其余一律闭区间。
#:
#: **入会条件**(不是「觉得该严一点」就能加):这个字段的上界不是「合理范围的
#: 边缘」,而是**一个已知会出事的具体值本身**。边缘可以取到,出事的那个点不能 ——
#: 「再高不行」与「到它为止都行」之间差的正是那一个点,而那恰恰是唯一有结论的点。
#: 加成员时把**你那一条的理由写在下面**,别只往集合里塞个名字:这个集合破的是
#: 「全表闭区间」这条隐含约定,而破例必须自己说得出为什么。
#:
#: ⚠️ 上界**不往下挪**。挪一截就是凭空发明一个数;**排除一个测过的坏点**和
#: **猜一个没测过的好点**是两件事,这里只做前者。
#:
#: 为什么不用 ``math.nextafter``:读 ``_BOUNDS`` 的人不该需要懂浮点末位。
#:
#: ── 成员逐条 ────────────────────────────────────────────────────────────
#:
#: * ``forge_v_tip_nm_s`` 上界 **488 nm/s**:曾在这个速度上
#:   刮坏了针尖。闭区间等于「到那个刮坏过针的速度为止都可以配」,
#:   没有任何一种读法是对的。
#:   ⚠️ 「488 是唯一一次实测的负例」这句话**会过期**。翻盘条件不在这里,在
#:   ``NobleTipWorkflow.forge_v_tip_nm_s`` 的注释里(三条,单一真源)——
#:   出现第二个低于 488 的实测负例时,**这个上界也要跟着重做**,别只改那边。
#: * ``poke_shallow_frac`` 上界 **1.0**:**这一条另有来源**(
#:   随「浅扎」一起加的),理由由那条线负责说明。从形状上它是对的 —— 「浅扎系数」
#:   取到 1.0 就是「一点也不更浅」,那个动作叫加深不叫浅扎,所以 1.0 本身要排除。
#:   ⚠️ 这条注释是**按语义推断的**;要确证请核对原始来源。
_EXCLUSIVE_UPPER = frozenset({"forge_v_tip_nm_s", "poke_shallow_frac"})

_INT_FIELDS = frozenset({"pulses_per_polarity", "pulse_same_spot_budget",
                        "poke_unround_streak_to_pulse", "poke_pulse_rescues",
                        "pulse_budget", "max_verify_rounds",
                         "critical_repeat_n", "poke_budget",
                         "poke_same_depth_retries", "poke_flat_dry_refills",
                         "step_pixels", "cluster_pixels",
                         "forge_step_pixels", "forge_cluster_pixels",
                         "forge_pixels", "verify_pixels"})


def resolve(overrides: "dict[str, Any] | None" = None,
            base: NobleTipWorkflow = NOBLE_METAL_BASELINE) -> NobleTipWorkflow:
    """出厂基线 + 用户显式给的值。

    ``None`` 当作没给 —— 技能的可选参数不填时就是 None,那正是「按流程表来」的
    意思,不是「设成 0」。越界的值**丢弃并写日志**,不夹紧。
    """
    if not overrides:
        return base
    known = {f.name for f in fields(base)}
    clean: dict[str, Any] = {}
    for key, val in overrides.items():
        if key not in known or val is None:
            continue
        if key == "descend_pulse_v":
            try:
                raw = tuple(val)
            except TypeError:
                logger.warning("修针流程: descend_pulse_v=%r 不是序列,忽略", val)
                continue
            seq = tuple(float(v) for v in raw
                        if isinstance(v, (int, float)) and -10.0 <= float(v) <= 10.0)
            if len(seq) != len(raw):
                logger.warning("修针流程: descend_pulse_v 有超出 ±10 V 的值,已丢弃")
            clean[key] = seq
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            logger.warning("修针流程: %s=%r 不是数字,忽略", key, val)
            continue
        lo, hi = _BOUNDS.get(key, (float("-inf"), float("inf")))
        hi_open = key in _EXCLUSIVE_UPPER
        if not (lo <= num <= hi) or (hi_open and num >= hi):
            logger.warning("修针流程: %s=%g 超出 [%g, %g%s,忽略(不夹紧)",
                           key, num, lo, hi, ")" if hi_open else "]")
            continue
        clean[key] = int(round(num)) if key in _INT_FIELDS else num
    return replace(base, **clean) if clean else base


# ── forge 评估帧:每线时间由针尖速度上限派生 ─────────────────────────────────
#
# **唯一入口。** 修针流程里每一张评估图的线时都要过这里 —— 与 ``scan_at_params``
# 之于 ScanAt 实参是同一个理由:「这一帧到底跑多快」必须是一个只有一个答案的
# 问题。分散在四个调用点上各自判一次,就是「同一个动作 N 份实现」。
#
# 与 ``scan_resolver.py`` 那道 ``v_tip_max`` 组合约束(2 µm/s)是**两层**,不是
# 两份:那一层是全仪器的护栏,兜的是「明显荒谬」;这一层是 forge 场景自己的,
# 兜的是「比已确认的那帧还快」。外层的 2000 nm/s 拦不住 667 nm/s —— 一道确实
# 存在、确实会动作、而阈值恰好在事故值之上的闸门,正是本仓说的「看着在防护
# 其实没有」。两层各自独立,谁先咬到听谁的。


def forge_line_time_s(wf: NobleTipWorkflow, size_nm: float,
                      line_time_s: "float | None"
                      ) -> "tuple[float | None, str | None]":
    """这张 forge 评估图该用多少每线时间,外加**为什么**。

    返回 ``(线时, 说明)``。说明非空时必须交到报告里 —— 只写 log 等于静默替换,
    而静默替换与静默夹紧一样坏(见 ``_forge_wf_notes``)。

    三条规矩,与包络对账同构:

    1. ``None`` 进 ``None`` 出。``None`` = 不下发这个键 = 走 ``ScanAt`` 的档位表,
       那是 ``PrepareNobleTip`` 的历史路径,**不能**在这里被替换成一个具体的数
       —— 「没传」与「传了 X」是这一层唯一在区分的两件事
       (``prescan_check.resolve_line_time_s`` 记着这个坑的原形:一个
       ``default=0.1`` 让查表那一行永远到不了,真机针尖恒 488 nm/s)。
    2. **只在超速时动**。已经比上限慢的一律原样返回(簇图 67 nm/s 不该被
       "修正"到 171 —— 上限是上限,不是目标值)。
    3. **用户显式指定的值不夹紧,改成出声**。``forge_line_time_s`` 的自述是
       「扫图**一律** N s/线」;夹它会让「一律」这个词自己成为假话,而一贯
       的原则是显式传入的值照旧过闸、超了就拒绝或如实报告,不静默改。
       (回退图那段缩放逻辑早就是这条规矩,这里只是把它抬成通则。)

    ── 为什么第 3 条选「报告」而不是「拒绝」(两支都想过)────────────────

    包络那一族的规矩是**拒绝不夹紧**,照搬过来会得出「超速就拒绝」。没照搬,
    因为两个阈值的**证据地位不一样**:

    * 包络(``max_abs_pulse_v`` / ``max_poke_depth_m``)背后有一张按针尖类型分的
      表,是关于**这根针承受得住什么**的物理声明。它有资格否决一个人。
    * ``forge_v_tip_nm_s`` **没有标定过** —— 它取自一帧已确认评估图的速度,
      是有据可依的保守选择,不是测量结论。**用一个未标定的数去否决
      操作员显式给出的值**,等于让一道没有依据的闸门获得否决权。

    另外,针尖风险由操作员自行承担、必要时可手动处理;而
    回退图那段早就是「显式给了数就按该数跑,只是说出来」——
    这里选拒绝会让**同一个动作在相隔两个函数的地方有两种规矩**。

    ⚠️ **这条推理在 488 上不成立,而那一支没有实现。** 488 nm/s 不是未标定的
    猜测,是唯一一次实测的负例;对它拒绝是有依据的。
    (⚠️「唯一」会过期 —— 出现第二个低于 488 的实测负例时,**这条分档线也要跟着
    重做**,不是只改上面那个上限。三条翻盘条件的单一真源在 ``forge_v_tip_nm_s``
    的注释里。)真要做,落点是
    ``ForgeAuTip.validate_params``(计划期快失败、告知该改哪个键),不是这里——
    这个函数只回答「用多少线时」,它没有拒绝这个动作的语法。

    ⚠️⚠️ **「针尖风险可手动处理」这一约束有一个隐含前提** ——
    成立的条件是**有人在**。conduct 这一层存在的理由正是
    **多天无人值守**,夜间无人时无法手动处理任何事。所以 ≥488 要不要拒绝,答案很可能**取决于有没有人在**,而 conduct
    恰好有「是否有人值守」这一位。

    **这个耦合刻意没有接**:修针流程去读 conduct 状态是把两条设计线焊死,而且
    会让 ``ForgeAuTip`` 的行为依赖一个它看不见也管不着的外部状态。要接也该由
    conduct 在**下发参数时**表态,不是由这里去问。
    **要动这一支,先与机器主人确认,并把「有人/无人」这个区别一起确认**——
    ``config/forge_tip_reference.txt`` 里已写明这个问题。
    """
    if line_time_s is None:
        return None, None
    lt = float(line_time_s)
    fov = float(size_nm)
    ceiling = float(wf.forge_v_tip_nm_s)
    if lt <= 0 or fov <= 0 or ceiling <= 0:
        # 这几个值不合法是上游出错的信号,不是「一个很快的扫描」。这一层不替
        # 上游发明数字,原样放行 —— 下游 ``validate_params`` / ``scan_resolver``
        # 有各自的拒绝口,它们比这里更知道该说什么。
        return lt, None
    speed = fov / lt
    if speed <= ceiling:
        return lt, None
    if wf.forge_line_time_s is not None:
        return lt, tr(
            f"{fov:.0f} nm 图按指定的 {lt:g} s/线跑 ⇒ 针尖 {speed:.0f} nm/s,"
            f"超过 forge 速度上限 {ceiling:g} nm/s(**未夹紧**:显式指定的值照旧执行)。"
            f"参考:488 nm/s 曾刮坏针尖。",
            f"The {fov:.0f} nm image at the specified {lt:g} s/line gives a tip speed of "
            f"{speed:.0f} nm/s, above the forge speed ceiling of {ceiling:g} nm/s "
            f"(**not clamped**: an explicitly specified value is still executed). "
            f"Reference: a run at 488 nm/s once scratched the tip.")
    capped = fov / ceiling
    return capped, tr(
        f"{fov:.0f} nm 图的每线时间由 {lt:g} s 派生为 {capped:.4g} s —— "
        f"照流程表的数会跑到针尖 {speed:.0f} nm/s,超过 forge 速度上限 "
        f"{ceiling:g} nm/s(= 已确认的那一帧:"
        f"100 nm / 5 min / 256 px)。帧时因此变长,**视野不在帧时的式子里**。",
        f"The line time of the {fov:.0f} nm image is derived from {lt:g} s to "
        f"{capped:.4g} s - the value in the workflow table would give a tip speed of "
        f"{speed:.0f} nm/s, above the forge speed ceiling of {ceiling:g} nm/s "
        f"(= the previously confirmed frame: 100 nm / 5 min / 256 px). The frame time "
        f"therefore gets longer; **the field of view is not in the frame-time formula**.")


def forge_fallback_line_time_s(wf: NobleTipWorkflow
                               ) -> "tuple[float | None, bool]":
    """回退图(更大视野)的每线时间。返回 ``(线时, 是否按用户指定的值原样跑)``。

    **唯一入口。** 这个公式原来只写在 ``level_phase`` 里,而报告那一侧要说
    「回退图跑多快」时又需要它 —— 抄一份的代价是:用户指定过数的那条分支
    (不缩放)会在抄件里丢掉,于是**报告里的数与真正下发的数不一样**。
    一份会说谎的报告比没有报告坏,所以这里只留一份。

    按视野比例放大 ⇒ 针尖速度与基准那张**完全相同**,代价只是帧时翻倍
    (帧时 = 行数 × 线时 × 2,视野不在式子里)。不放大的话速度会跟着视野一起涨:
    100 → 200 nm 沿用 0.15 s 就是 667 → **1333 nm/s**。

    ⚠️ **用户显式指定的数不缩放。** ``forge_line_time_s`` 的约定是
    「扫图**一律** N s/线」;缩放它会让「一律」不再成立(verify 曾因收不到
    这个覆写而出过问题)。显式给了数就按该数跑,由调用方出声。
    """
    lt = wf.step_line_time_s
    if lt is None or not wf.step_scan_nm or not wf.step_fallback_nm:
        return lt, False
    if wf.forge_line_time_s is not None:
        return float(lt), True
    return (float(lt) * float(wf.step_fallback_nm) / float(wf.step_scan_nm),
            False)


def forge_speed_notes(wf: NobleTipWorkflow) -> list[str]:
    """计算每种评估图的针尖横向速度，并生成与执行参数一致的说明。
    
    速度由视野除以线时得到；缺线时表示走档位表，不编造具体速度。
    回退图复用统一的线时派生函数。
    """
    frames = (
        (tr("验证帧", "Verification frame"), wf.verify_scan_nm, wf.verify_line_time_s),
        (tr("台阶/验收图", "Step / acceptance image"), wf.step_scan_nm, wf.step_line_time_s),
        # ⚠️ 回退图的线时**不能在这里重算一遍** —— 那个公式有一条「用户指定的值
        # 不缩放」的分支,抄件里丢掉它,报告说的数就与真正下发的数不是一个。
        (tr("台阶回退图", "Step fallback image"), wf.step_fallback_nm,
         forge_fallback_line_time_s(wf)[0]),
        (tr("找台面图", "Terrace-finding image"), wf.poke_site_scan_nm,
         wf.poke_site_line_time_s),
        (tr("簇图", "Cluster image"), wf.cluster_scan_nm, wf.cluster_line_time_s),
    )
    notes: list[str] = []
    for label, fov, raw_lt in frames:
        lt, _why = forge_line_time_s(wf, float(fov or 0.0), raw_lt)
        if not lt or not fov:
            # 「走档位表」和「跑多快」是两件事,而这一层看不见档位表的解析结果。
            # 报成一个具体的 nm/s 就是把「读不到」折叠成一个值 —— 本仓一天记过
            # 四次的那个形状。如实说不知道。
            notes.append(tr(f"{label} {float(fov or 0):.0f} nm:每线时间走档位表,"
                            "针尖速度在这一层算不出来",
                            f"{label} {float(fov or 0):.0f} nm: the line time comes from "
                            "the tier table, so the tip speed cannot be computed at this "
                            "layer"))
            continue
        notes.append(tr(f"{label} {float(fov):.0f} nm / {lt:.4g} s/线 ⇒ 针尖 "
                        f"{float(fov) / lt:.0f} nm/s",
                        f"{label} {float(fov):.0f} nm / {lt:.4g} s/line -> tip "
                        f"{float(fov) / lt:.0f} nm/s"))
    return notes


# ── 出厂默认 × 针尖包络:对账 ────────────────────────────────────────────────
#
# 曾发生:qPlus 针上 ForgeAuTip 用**默认参数**连撞两道包络门,
# 一次都没跑起来:
#
# ⚠️ 下面这张表是**历史快照,数字已全部过期**,留着只为记住那次教训。
#    这张表误导过一次:qPlus 针连吃三发 10 V,照这张表
#    断定「包络门失效了」——**而 10 V 正是后来把 qPlus 上限从 3 V
#    改上去的**(改动就写在 tip_conditioning_policy 那一档的注释里)。
#    纠正它的是**去跑一遍 resolve_policy**,不是再读一遍注释。
#    ⇒ 现在两个上限已被统一拉满(10 V / 10 nm),这张表的每个数都不再成立。
#    **修好之后,旧的理由会静静变成假话** —— 而它比没有理由更危险。
#
#     门          qPlus 包络(当时)                      本表默认(当时)  超
#     扎针深度     max_poke_depth_m = 5.0e-10           1.5 nm       3×
#     脉冲电压     max_abs_pulse_v  = 3.0 V             10 V         3.3×
#
# 两张表各自都自洽、各自都有理由(10 V 是金属丝针尖的常规做法;3 V 是音叉的
# 不可逆损伤线),而**从来没有任何一处把它们放在一起看过**。于是在 qPlus 针上,
# 这条流程的出厂默认是不可执行的 —— 而且 ``allow_on_qplus=true`` 一道门都解不开,
# 它解的是 qPlus **策略**门,包络门是另一套。
#
# 「拒绝不夹紧」是对的,不改:夹了会让人以为只扎了 3 nm。要修的是
# **默认值本身就违法**这件事。
#
# 修法与 :func:`descend_sequence` 同一个形状(那里已经在按针尖类型分叉了):
# **一个这根针接受不了的默认值不是默认值,是一次必然的失败**,所以在把它送进硬件
# 之前,换成这根针**自己那张表**推荐的值(方案表每一档都带 ``pulse_v`` /
# ``poke_deep_depth_m`` —— 按构造它们落在自己的包络内)。
#
# 三条边界,都写下来免得下一个人当成夹紧:
#   1. **只动没被用户指定的默认值**。用户显式给的数照旧送去过包络,超了就拒绝;
#   2. **只在违法时动**。合法的默认一个不碰(金属丝针上仍然是 10 V);
#   3. **每一次替换都出声**(返回 notes),静默替换与静默夹紧一样坏。
#
#: 比较包络时的相对容差。
#:
#: 没有它会出一个**这道闸门第一次跑就抓到的** bug:本表的深度是 nm、方案表是米,
#: 而 ``3.0 * 1e-9 == 3.0000000000000004e-09``,比 ``3e-9`` 大一个最低位 ——
#: 于是「换成正好等于包络的值」之后,再比一次仍然判成超限,替换永远收敛不了。
#: 与 ``scan_policy._BOUND_REL_TOL`` 同一个形状、同一个理由(那边是边界
#: 设成 100 nm 而 100.0*1e-9 落进了下一档)。1e-9 的相对容差在 5 nm 上是 5e-18 m,
#: 物理上毫无意义,但比双精度往返误差大七个数量级。
_ENVELOPE_REL_TOL = 1e-9


def within_envelope(value_in_policy_units: float, limit: float) -> bool:
    """``|value| <= limit``,吸收单位换算的浮点末位。见 :data:`_ENVELOPE_REL_TOL`。

    判据只有一份:运行时的对账与结构闸门共用它。两份「差不多相等」的实现迟早
    在最低位上各自漂移,而那正是这条容差要挡的东西。
    """
    return abs(float(value_in_policy_units)) <= float(limit) * (1.0 + _ENVELOPE_REL_TOL)


#: 工作流字段 → (方案表的包络键, 方案表的推荐值键, 本表单位→方案表单位的换算)
_ENVELOPE_PAIRS: tuple[tuple[str, str, "str | None", float], ...] = (
    # 脉冲电压:两边都是伏特。
    ("pulse_v", "max_abs_pulse_v", "pulse_v", 1.0),
    # 深扎首深:本表 nm,方案表米。
    ("poke_depth_nm", "max_poke_depth_m", "poke_deep_depth_m", 1e-9),
    # 深扎的加深上限。方案表没有对应的「上限推荐值」,所以违法时取包络本身 ——
    # 那不是夹紧一个请求,是把一个够不着的天花板降到实际够得着的高度。
    ("poke_depth_max_nm", "max_poke_depth_m", None, 1e-9),
)


def _tip_policy_now() -> "dict[str, Any] | None":
    """当前针尖的方案表(含包络)。整条链坏掉才返回 None。

    ⚠️ **「未登记针尖」不是「没有包络」**(2026-08-10 更正)。这个函数的第一版在
    ``current_tip_facts()`` 返回空时直接 return None、不对账,理由写的是
    「系统不知道装的是什么针,没资格替用户改他的默认值」——**那条理由用错了地方**:

    未登记时 ``resolve_policy(None)`` 给的是**通用保守档**,而那一档有自己的包络
    (``max_abs_pulse_v = 6.0``),``_check_envelope`` 照样对着它拒绝。
    于是「不对账」在这里不是 fail-open,是**保证失败**:出厂 ``pulse_v = 10 V``
    > 6 V ⇒ **未登记针尖上一发脉冲都打不出去**,而未登记是本系统最常见的状态。

    真正 fail-open 的是另一道门:``_tip_policy.qplus_gate``(qPlus **策略**门)在
    读不到针尖时放行。两道门、两条哲学,第一版把其中一道的性质安到了另一道头上
    ——``tip_conditioning_resolver`` 的模块注释犯的是同一个错,已一并改掉。

    只有**整条链不可用**(import 失败、解析器抛异常)才 return None:那时确实没有
    包络可对,而一个坏掉的诊断链不该让修针失败。
    """
    try:
        from mast.core.tip_conditioning_policy import resolve_policy
        from mast.core.tip_state import current_tip_facts

        try:
            facts = current_tip_facts()
        except Exception as exc:  # noqa: BLE001 — 读不到登记 = 按未登记处理
            logger.debug("修针流程: 读针尖登记失败(按未登记的通用档对账): %s", exc)
            facts = None
        # facts 为空 → 通用保守档(**不是**「没有包络」)。
        return resolve_policy(facts or None)
    except Exception as exc:  # noqa: BLE001 — 对账不可用绝不能让修针失败
        logger.debug("修针流程: 读针尖方案表失败(不对账): %s", exc)
        return None


def reconcile_with_tip_envelope(
    workflow: NobleTipWorkflow,
    *,
    specified: "frozenset[str] | set[str] | tuple[str, ...]" = (),
    policy: "dict[str, Any] | None" = None,
) -> "tuple[NobleTipWorkflow, list[str]]":
    """把**违反当前针尖包络的默认值**换成这根针自己那张表推荐的值。

    ``specified`` 是用户显式给过的字段名 —— 这些**一个都不动**,它们照旧送去过
    包络门,超了就被拒绝(那正是「拒绝不夹紧」要保住的东西)。

    返回 ``(工作流, 说明列表)``。说明列表为空 = 什么都没换。
    """
    pol = _tip_policy_now() if policy is None else policy
    if not pol:
        return workflow, []
    given = set(specified or ())
    changes: dict[str, float] = {}
    notes: list[str] = []
    for field_name, limit_key, rec_key, scale in _ENVELOPE_PAIRS:
        if field_name in given:
            continue
        limit = _num_or_none(pol.get(limit_key))
        if limit is None or limit <= 0:
            continue
        current = _num_or_none(getattr(workflow, field_name, None))
        if current is None:
            continue
        if within_envelope(current * scale, limit):
            continue                      # 合法,不碰
        # 优先用方案表自己的推荐值;它按构造落在包络内,而且是**针对这根针**的。
        replacement = None
        if rec_key:
            rec = _num_or_none(pol.get(rec_key))
            if rec is not None and within_envelope(rec, limit):
                replacement = abs(rec) / scale
        if replacement is None:
            replacement = limit / scale
        lo, hi = _BOUNDS.get(field_name, (float("-inf"), float("inf")))
        if not (lo <= replacement <= hi):
            continue                      # 换出去也会被本表自己的边界拒,那就别换
        changes[field_name] = replacement
        notes.append(tr(
            f"{field_name}: 出厂默认 {current:g} 超出当前针尖的 {limit_key}="
            f"{limit:g}（超 {abs(current) * scale / limit:.2g}×），"
            f"已改用这根针方案表里的 {replacement:g}。"
            "（用户显式指定的值不会被这样替换——那种情况仍然是拒绝，不夹紧。）",
            f"{field_name}: the factory default {current:g} exceeds the current tip's "
            f"{limit_key}={limit:g} (by {abs(current) * scale / limit:.2g}x), so the value "
            f"from this tip's policy table, {replacement:g}, is used instead. "
            "(A value explicitly specified by the user is not replaced this way - that "
            "case is still refused, not clamped.)"
        ))
    if not changes:
        return workflow, []
    for note in notes:
        logger.warning("修针流程与针尖包络对账: %s", note)
    return replace(workflow, **changes), notes


def _num_or_none(value: Any) -> "float | None":
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return None if num != num else num


def descend_sequence(workflow: NobleTipWorkflow) -> tuple[float, ...]:
    """扫描验证失败后要补的递减脉冲 —— qPlus 上是空的。

    针尖类型的唯一真源是针尖登记(``tip_state.is_qplus``),不是另设一个设置项。
    读不到就当不是 qPlus:未登记针尖时系统不知道装的是什么,没资格替用户否决
    —— 与 ``_tip_policy.qplus_gate`` 同一条论证(而真正会戳到音叉的下压类动作,
    那道软门仍然默认拒绝)。
    """
    try:
        from mast.core.tip_state import is_qplus
        if is_qplus():
            return ()
    except Exception as exc:  # noqa: BLE001
        logger.debug("读针尖类型失败(按非 qPlus 处理): %s", exc)
    return tuple(workflow.descend_pulse_v)


__all__ = [
    "NobleTipWorkflow",
    "NOBLE_METAL_BASELINE",
    "resolve",
    "descend_sequence",
    "reconcile_with_tip_envelope",
    "within_envelope",
]
