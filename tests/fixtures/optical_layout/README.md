# 光路图追迹夹具

后端（`MASTv2/mast/optical_layout/trace.py`）与前端（TypeScript）两份追迹实现共用这一批文件。
契约是 `docs/v2/design/optical_layout.md` §2–§4，契约没写到的细节已经写进同一份文档的
**§11（以那里为准）**；本文是它在夹具这一侧的摘要。算法的完整规则也在 `trace.py` 顶部的
文档字符串里。

## 文件格式

```json
{"name": "…", "description": "…", "layout": {…}, "expected_trace": […], "expected_misses": […]}
```

- `layout`：§3 的光路图，**已经是规范形式**（后端校验后的样子：角度已规范到 [0, 360)、坐标 0.01、
  每个字段都在、`props` 只含给出的键）。直接拿来追迹，不需要再规范化。测试断言
  `Layout.model_validate(layout).to_dict() == layout`。
- `expected_trace`：§4.6 的输出（见下）。**由 Python 实现生成，再逐个人工核对物理**（每个文件的
  `description` 写了它在钉什么；核对方法见 `tests/v2/unit/optical_layout/test_trace_physics.py`）。
- `expected_misses`：可选，只有后端的"险些没中"（`near_misses`）用到；前端追迹测试可以忽略它。
- `trace_options`、`default_max_trace_events`：可选，只有 `trace_budget_*` 两个夹具有（见"整条追迹的预算"）。
  `trace_options.max_trace_events` 是追迹这个夹具时给的预算（比契约的小，好让预期输出保持很小）；没有这个键就用
  默认预算。`default_max_trace_events` 是契约的默认预算（5000）：两边的测试各拿自己的常数对它。
- `name`、`description`：说明用，比对时忽略。

## 比对规则

- 数字：绝对容差 1e-6（`fraction` 同）；字符串、布尔、`null`、数组长度、键集合：严格相等。
- `-0.0` 一律写成 `0.0`。取整：坐标、长度 0.001 mm、角度 0.001°、`fraction` 1e-6
  （Python `round`）。夹具里没有任何值落在取整的临界点上（生成时检查过），所以 JS 的
  `Math.round(x * 1000) / 1000` 与 Python 的 `round(x, 3)` 在这批文件上结果相同。
- 度 → 弧度：`deg * (π / 180)`（两边同一个表达式）；弧度 → 度：`rad * (180 / π)`。

## 输出格式（契约 §4.6 + 细则）

`expected_trace` 是树的数组。每棵树 = 一个光源 × 一个波长：

```json
{"source": "las1", "wavelength_nm": 532.0, "branches": [ … ]}
```

- 顺序：先按器件在 `layout.components` 里的顺序遍历光源（`wavelengths_nm` 列表里的波长按所列顺序
  各一棵树），再是光纤续接出来的树（见下）。没有光源 → `[]`。
- `wavelength_nm` 为 `null`：灯 / LED 没填波长。未知波长的规则：二向色镜透射、滤光片通过。
- 分支 `{"branch", "fraction", "segments", "events", "end"}`：
  - `branch`：根 `"0"`，分束时在父 id 后加 `".r"`（反射支）/ `".t"`（透射支）。
  - 分支列表按**深度优先、父在前、反射支先于透射支**排列：`0, 0.r, 0.r.r, 0.r.t, 0.t, …`。
  - `fraction`：这一支开头的相对强度（分束器 R 与 1−R 逐级相乘；强度 < 1e-3 的子支**不生成**）。
  - `segments`：折线。`segments[i]` 从分支的上一个点（光源，或父支分束的点）走到 `events[i]` 的点；
    以 `left_table` 或 `budget` 结束的分支多一段最后的线段（走到离开点 / 走到那次没有记下来的命中点）。一个分支至少有一段。
    不合并共线的段（每个事件一段）。
  - `events`：每次作用一条，字段 `component, action, x, y, offset_mm, incidence_deg, path_mm`。
    `action` ∈ `reflect | transmit | split | stop | blocked | back_hit`。
    `offset_mm` = 命中点到器件中心、沿作用面的有符号距离，局部 +y 方向 `(-sin a, cos a)` 为正；
    `incidence_deg` = `acos(min(1, |d·n|))`，0–90；`path_mm` = 从光源起的累计路径长
    （子支接着父支累计）。
  - `end` ∈ `stop`（终止类器件的正面）`| blocked`（滤光片 / 光阑 / 快门挡住）`| back_hit`
    `| split`（**本分支在分束点结束，后面是 .r 和 .t**）`| left_table | max_events | budget`
    （整条追迹的事件预算用完了，见下）。

## 规则要点（逐条见 trace.py）

- 交点：`t = ((C−P)·n)/(d·n)`，`|d·n| < 1e-9` 当平行（无交点）；`t > 1e-6`；刚作用过的器件在 `t < 1e-3`
  内忽略；`|offset| ≤ aperture/2 + 1e-9` 才算命中；取最近的；`t` 相差不超过 `EPS_TIE` = 1e-9 mm 的算并列，并列取列表里靠前的（后面的候选要 `t < 当前最优 − 1e-9` 才取代它）。光源不会被命中。
- 反射（mirror、grating、sample 反射时、custom 的 reflect）：正面（`d·n < 0`）`reflect`，
  `d' = d − 2(d·n)n`；背面 `back_hit` 并结束。终止类：正面 `stop`，背面 `back_hit`，都结束。
- 分束器两面都分：`split`（本支结束），`.r` 用上面的反射公式，`.t` 方向不变。`reflect_fraction` 缺省 0.5。
- 二向色镜两面都按波长：`reflects` 缺省 `short`；`short` 时波长 < edge 反射，`long` 时波长 > edge 反射，
  等于 edge 透射。
- 滤光片：`pass_min_nm ≤ λ ≤ pass_max_nm` 透射（缺哪个边界就不限），否则 `blocked`。光阑：有 `open_mm` 且
  `|offset| > open_mm/2 + 1e-9` → `blocked`。快门 `closed` → `blocked`。
- 其余透射类（透镜、物镜、波片、偏振片、窗片、ND、AOTF、棱镜、潜望镜、custom 的 transmit）：`transmit`，
  不偏折、不衰减。
- 每次作用算一次事件；一条光线谱系（含分束前的父支）累计到 64 次、且这次没有终止分支，分支以
  `max_events` 结束（第 64 次事件之后没有线段）。第 64 次恰是分束则父支以 `max_events` 结束、不生成子支。
- 离开：光线跑出"平台外扩 1 m"的矩形（`x ∈ [−1000, W+1000]`、`y ∈ [−1000, H+1000]`），或单段超过 10 m
  （10000 mm）而没有命中：`left_table`，终点是那个边界交点 / 10 m 处。光源在矩形外：立即 `left_table`，
  一段零长度线段。
- 光纤：`fiber_in` 的 `props.continues_at` 指向某个 `fiber_out`；光线**正面**停在这个 `fiber_in` 上时，那个
  `fiber_out` 作为同一波长的光源（`fraction` 1.0）发出新的一棵树。被某个 `continues_at` 指到的
  `fiber_out` 只在被照到时才发光（不用自己的 `props` 发光）；同一个（`fiber_out`、波长）只发一次树
  （所以光纤环路会收敛）。续接的树排在所有直接光源的树之后，按发现顺序。
- 光栅：v1 按零级反射，和 mirror 一样。
- **整条追迹的预算**（`MAX_TRACE_EVENTS`，契约 §11.2 第 13 条）：所有树合计最多记 5000 次事件，按追迹产生的顺序数
  （树的顺序；一棵树里深度优先、反射支先于透射支；一支里一次接一次）。**第 5001 次命中不记**：这一支得到一段走到
  那次命中点的线段（比事件多一段，没有对应的事件），`end = "budget"`，追迹到此为止——后面的支、后面的树（含光纤
  续接）都不再追。所以 `budget` 支是最后一棵树的最后一支，全追迹只有一支；恰好需要 5000 次的追迹是完整的。
  截断没有额外字段：任何一支以 `budget` 结束就是被截断了。

## 夹具清单

| 文件 | 钉什么 |
|---|---|
| `single_fold_mirror` | 135° 的镜子把 +x 折到 +y；探测器终止 |
| `two_mirror_dogleg` | 两镜（俯视的潜望 / 折两次）：+x→+y（135°）→+x（315°） |
| `beam_splitter_two_branches` | 分束器两支、R=0.3、分支 id 与 fraction |
| `beam_splitter_both_faces` | 分束器从正反两面分 |
| `beam_splitter_cascade` | 两级分束，深度优先的分支顺序 |
| `beam_splitter_pruned_branches` | < 1e-3 的子支不生成 |
| `dichroic_by_wavelength` | 二向色镜按波长分路，等于 edge 透射，`short` 与 `long` |
| `filter_blocks_and_passes` | 滤光片挡掉（`blocked`）/ 通过，只给一端的带通 |
| `back_side_hits` | 镜子背面 `back_hit`、探测器背面 `back_hit` |
| `off_center_hits` | 偏离中心命中，`offset_mm` 的符号与大小 |
| `miss_aperture` | 错过口径：不命中，继续走；`expected_misses` 给出险些没中 |
| `aperture_edge_hits` | 作用面端点属于口径：正好偏离 ±半口径命中（`offset_mm` = ∓10），多 0.01 mm 就错过；`expected_misses` 给出那次险些没中 |
| `fiber_coupling_continues` | 光纤续接、按波长各一棵树、重复到达不重复发 |
| `fiber_loop` | 光纤环路收敛 |
| `leaves_table` | 离开平台外扩 1 m 的矩形，三个方向 |
| `long_segment_limit` | 单段 10 m 上限 |
| `start_outside_table` | 光源在矩形外：零长度线段 |
| `iris_open_diameter` | 光阑：刚好 open/2 通过、更大的挡住 |
| `shutter_closed_and_open` | 快门关 / 开 |
| `max_events_ping_pong` | 两面镜对打，64 次作用后 `max_events` |
| `transmit_chain` | 所有只透射的类型；样品不反射时终止 |
| `reflecting_variants` | 光栅（零级）、样品（反射）、custom（reflect）像镜子一样 |
| `unknown_wavelength` | 无波长的灯：滤光片通过、二向色镜透射 |
| `parallel_to_surface` | 作用面与光线平行：没有交点 |
| `oblique_incidence` | 非 45° 的角度，非整数坐标 |
| `no_sources` | 没有光源 → `[]` |
| `nearest_hit_ties` | 并列：作用面共线且重叠的两个 40 mm 探测器（225°，中心沿作用面相距 10 mm），一束光在同一点穿过两者——列表里谁靠前谁中（B、A 的顺序时没有容差会因最后一位让 A 中，A、B 的顺序时给 a）；两个器件在完全相同的位置（s2 靠前）；相距 0.01 mm 的两个不算并列（近的中，不管列表顺序） |
| `trace_budget_exact` | 预算（`trace_options.max_trace_events` = 5）恰好等于追迹需要的事件数（两个光源共 5 次）：追迹完整，没有 `budget` |
| `trace_budget_cut` | 同一张图，预算 3：分束（1）、反射支的 M1（2）和 D_R（3）之后，透射支 `0.t` 在 D_T 的命中是第 4 次，不记：`0.t` 得到一段走到 D_T 的线段、没有事件，`end = "budget"`；L2 完全没追 |

## 改夹具

改了追迹规则就要改夹具，并通知前端：先用 Python 重新生成 `expected_trace`，再人工核对物理，
最后让两边的测试都过。不要只为了让测试变绿而重生成。
