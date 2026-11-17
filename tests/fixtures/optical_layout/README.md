# 光路图追迹夹具

后端（`MASTv2/mast/optical_layout/trace.py`）与前端（TypeScript）两份追迹实现共用这一批文件。
几何的契约是 `docs/v2/design/optical_layout.md` §2–§4，契约没写到的细节已经写进同一份文档的
**§11（以那里为准）**；光束（功率、偏振、高斯光束）的契约是 `docs/v2/design/optical_layout_v2.md`
§3–§4，细则在它的 **§12**；高度（2.5 维）的契约是 `docs/v2/design/optical_layout_3d.md` §2–§5，细则在它的
**§12**。本文是它们在夹具这一侧的摘要。算法的完整规则也在 `trace.py` 顶部的文档字符串里。

## 文件格式

```json
{"name": "…", "description": "…", "layout": {…}, "expected_trace": […], "expected_misses": […]}
```

- `layout`：§3 的光路图，**已经是规范形式**（后端校验后的样子：角度已规范到 [0, 360)、坐标 0.01、
  每个字段都在、`props` 只含给出的键）。直接拿来追迹，不需要再规范化。测试断言
  `Layout.model_validate(layout).to_dict() == layout`。
- `expected_trace`：§4.6 的输出（见下）。**由 Python 实现生成（`regenerate.py`），再逐个人工核对物理**
  （每个文件的 `description` 写了它在钉什么；核对方法见 `tests/v2/unit/optical_layout/test_layout_trace_physics.py`
  ——几何——与 `test_layout_beam_physics.py`——光束，期望值用闭式公式和 Jones 矩阵独立算）。
- `expected_misses`：可选，只有后端的"险些没中"（`near_misses`）用到；前端追迹测试可以忽略它。
- `trace_options`、`default_max_trace_events`：可选，只有 `trace_budget_*` 两个夹具有（见"整条追迹的预算"）。
  `trace_options.max_trace_events` 是追迹这个夹具时给的预算（比契约的小，好让预期输出保持很小）；没有这个键就用
  默认预算。`default_max_trace_events` 是契约的默认预算（5000）：两边的测试各拿自己的常数对它。
- `name`、`description`：说明用，比对时忽略。
- **`power_text_cases.json` 不是追迹夹具**：它钉的是功率怎么写成文字（契约 v2 §12.3），`{"cases": [{"mw": …, "zh", "en"} |
  {"p": …, "zh", "en"}]}`，Python（`summary.power_text` / `share_text`）与 TS（`beam.ts`）的单测都读它。生成器和两边的
  夹具测试都按文件名跳过它。

## 比对规则

- 数字：绝对容差 1e-6（`fraction` 同）；字符串、布尔、`null`、数组长度、键集合：严格相等。
- **`beam` 里的数**（树、段、事件三处，键 `beam` 以下的一切）用**混合容差**
  `|a − b| ≤ 1e-6 · max(1, |a|, |b|)`（高斯光束的 zR 可以上万 mm）；`null` 与数组长度照样严格相等。
- `-0.0` 一律写成 `0.0`。取整：坐标、长度 0.001 mm、角度 0.001°、`fraction` 1e-6
  （Python `round`）。夹具里没有任何值落在取整的临界点上（`regenerate.py` 生成时检查，落在临界点附近就拒绝写），
  所以 JS 的 `Math.round(x * 1000) / 1000` 与 Python 的 `round(x, 3)` 在这批文件上结果相同。`beam` 里的数取到 1e-9
  （`mw`、`source_mw` 取 `MW_DECIMALS` 位，现为 9；`q` 的两个数取 9 位**有效数字**——Python `float(f"{x:.9g}")`，
  TS `Number(x.toPrecision(9))`——极端但合法的 zR 可以远小于 1e-9 mm，不能取整成 0），比容差细三个数量级，两边不必避开平局。
- 度 → 弧度：`deg * (π / 180)`（两边同一个表达式）；弧度 → 度：`rad * (180 / π)`。

## 输出格式（契约 §4.6 + 细则）

`expected_trace` 是树的数组。每棵树 = 一个光源 × 一个波长：

```json
{"source": "las1", "wavelength_nm": 532.0, "beam": {"source_mw": 50.0, "m2": 1.0}, "branches": [ … ]}
```

- 树的 `beam`：`source_mw` 光源的绝对功率 mW（`power_mw`；没给为 `null`；光纤续接的树见“光束”），`m2` 这棵树的 M²
  （没有高斯光束时为 `null`）。

- 顺序：先按器件在 `layout.components` 里的顺序遍历光源（`wavelengths_nm` 列表里的波长按所列顺序
  各一棵树），再是光纤续接出来的树（见下）。没有光源 → `[]`。
- `wavelength_nm` 为 `null`：灯 / LED 没填波长。未知波长的规则：二向色镜透射、滤光片通过。
- 分支 `{"branch", "fraction", "segments", "events", "end"}`：
  - `branch`：根 `"0"`，分束时在父 id 后加 `".r"`（反射支）/ `".t"`（透射支）。
  - 分支列表按**深度优先、父在前、反射支先于透射支**排列：`0, 0.r, 0.r.r, 0.r.t, 0.t, …`。
  - `fraction`：这一支开头的相对强度（分束器 R 与 1−R 逐级相乘；强度 < 1e-3 的子支**不生成**）。
  - `segments`：折线。`segments[i]` 从分支的上一个点（光源，或父支分束的点）走到 `events[i]` 的点；
    以 `left_table` 或 `budget` 结束的分支多一段最后的线段（走到离开点 / 走到那次没有记下来的命中点）。一个分支至少有一段。
    不合并共线的段（每个事件一段）。字段 `x0, y0, x1, y1, z_mm, beam, passes`；`beam = {p, s, q}` 是这一段**起点**的状态
    （= 上一次作用之后；根的第一段 = 光源初态；子支第一段 = 分束后的子支状态；`left_table` / `budget` 那一段 = 最后一次作用之后）：
    `p` 相对功率（= S0），`s` = `[S1/S0, S2/S0, S3/S0]`（S0 = 0 时 `null`），`q` = `[z, zR]` mm（没有高斯光束时 `null`）。
    `z_mm` 这一段光的高度、`passes` 它越过的器件（见“高度”）。
  - `events`：每次作用一条，字段 `component, action, x, y, z_mm, offset_mm, dz_mm, incidence_deg, path_mm, beam`；
    `beam = {p, mw, d_mm, s}` 是**到达**这个器件、作用之前的状态：`mw` = `source_mw × p`（`source_mw` 为 `null` 时 `null`），
    `d_mm` 到达处的 1/e² 直径（没有高斯光束时 `null`）。
    `action` ∈ `reflect | transmit | split | stop | blocked | back_hit`。
    `offset_mm` = 命中点到器件中心、沿作用面的有符号距离，局部 +y 方向 `(-sin a, cos a)` 为正；
    `incidence_deg` = `acos(min(1, |d·n|))`，0–90；`path_mm` = 从光源起的累计路径长
    （子支接着父支累计；含潜望镜里的竖直段）。`z_mm` 到达时光的高度，`dz_mm` = `z_mm` − 器件光轴的高度（潜望镜按入口）。
  - `end` ∈ `stop`（终止类器件的正面）`| blocked`（滤光片 / 光阑 / 快门挡住）`| back_hit`
    `| split`（**本分支在分束点结束，后面是 .r 和 .t**）`| left_table | max_events | budget`
    （整条追迹的事件预算用完了，见下）。

## 规则要点（逐条见 trace.py）

- 交点：`t = ((C−P)·n)/(d·n)`，`|d·n| < 1e-9` 当平行（无交点）；`t > 1e-6`；刚作用过的器件在 `t < 1e-3`
  内忽略；`|offset| ≤ aperture/2 + 1e-9` 才算命中；取最近的；`t` 相差不超过 `EPS_TIE` = 1e-9 mm 的算并列，并列取列表里靠前的（后面的候选要 `t < 当前最优 − 1e-9` 才取代它）。光源不会被命中。
- 反射（mirror、grating、sample 反射时、custom 的 reflect）：正面（`d·n < 0`）`reflect`，
  `d' = d − 2(d·n)n`；背面 `back_hit` 并结束。终止类：正面 `stop`，背面 `back_hit`，都结束。
- 分束器两面都分：`split`（本支结束），`.r` 用上面的反射公式，`.t` 方向不变。`reflect_fraction` 缺省 0.5。
  偏振分束器 `pbs` 几何与分束器相同，但两支的 `fraction` 按偏振分（见“光束”）。
- 二向色镜两面都按波长：`reflects` 缺省 `short`；`short` 时波长 < edge 反射，`long` 时波长 > edge 反射，
  等于 edge 透射。
- 滤光片：`pass_min_nm ≤ λ ≤ pass_max_nm` 透射（缺哪个边界就不限），否则 `blocked`。光阑：有 `open_mm` 且
  `|offset| > open_mm/2 + 1e-9` → `blocked`。快门 `closed` → `blocked`。
- 其余透射类（透镜、物镜、波片、偏振片、窗片、ND、AOTF、棱镜、潜望镜、custom 的 transmit）：`transmit`，
  不偏折（功率、偏振、光斑怎么变见“光束”）。
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

## 光束（契约 v2 §4、§12；逐条见 trace.py 的 “Beam”）

- 光线带 Stokes 向量 `S = (S0, S1, S2, S3)`（S0 = 相对功率，光源处 1）与高斯光束的 `q = (z, zR)` mm（或没有）。
  横向基 `e_h = ẑ × d = (−dy, dx)`（平台平面内、传播方向左侧）、`e_v = ẑ`；所有偏振角从 `e_h` 量向 `e_v`：0° = p，90° = s。
- 光源：`polarization: linear` → `(1, cos 2α, sin 2α, 0)`（α = `pol_angle_deg`，缺省 0），否则（含灯 / LED）`(1, 0, 0, 0)`；
  `q = (0, π w0² / (M² λ))`，`w0 = beam_diameter_mm / 2`，`λ = wavelength_nm × 1e−6` mm，`M² = m2`（缺省 1）；没有直径或没有波长 → 没有 `q`。
  `source_mw = power_mw`（每个波长都是全部的 `power_mw`）。
- 光纤续接的树：`S0` 重新从 1 起，偏振与光束取那个 `fiber_out` 自己的 props；`source_mw` = 第一支（分支顺序）停在 `fiber_in`
  上的绝对功率（未取整）× 那个 `fiber_in` 的 `coupling`（缺省 1）；来光绝对功率未知 → `null`。
- 自由传播 L：S 不变，`z += L`。直径 `d = 2·sqrt(λ M² (z² + zR²) / (π zR))`。
- 作用（`T` = `throughput`，缺省 1；`R_m = diag(1, 1, −1, −1)`）：反射 `T·R_m·S`；透镜 / 物镜 `T·S`，`focal_mm` 有且 ≠ 0 时
  `1/q' = 1/q − 1/f`；波片 `T·W(θ_eff, δ)·S`（half: (cos δ, sin δ) = (−1, 0)，quarter: (0, 1)，用精确常数；没有 `retardance` → `T·S`）；
  偏振片 `T·k·(1, c, s, 0)`，`k = (S0 + c·S1 + s·S2)/2`，`(c, s) = (cos, sin)(2θ_eff)`；ND `10^(−od)·S`；其余透射 `T·S`
  （二向色镜、光阑、快门没有 `throughput`）；分束器 `.r` = `R·R_m·S`、`.t` = `(1 − R)·S`；PBS `.t` = `k_t·(1, 1, 0, 0)`，
  `k_t = (S0 + S1)/2`，`.r` = `k_r·(1, −1, 0, 0)`，`k_r = (S0 − S1)/2`（= `R_m·P(90°)·S`），两支 `fraction` = 父 `fraction × k/S0`，
  S0 = 0 时各 0.5；终止 / 挡住 / 背面：事件记到达状态，光线结束。
- 轴角：`θ_eff = atan2(sin θ · (d·n), cos θ)`（θ = `fast_axis_deg` / `axis_deg`，缺省 0；0° 在平台面内，90° 竖直向上）：正面 θ，
  背面 −θ（≡ 180° − θ），斜入射按真实器件——吸收型偏振片透过 `d × a`、正单轴晶体波片的快波沿 `d × c`（`a`、`c` 在器件面内、与给定的
  轴垂直；契约 v2 §4.5 的推导）。PBS 不投影。
- **无光是精确的 0**：偏振片与 PBS 的每个输出，`k ≤ EPS_DARK × 到达的 S0`（`EPS_DARK` = 1e-12）时输出恰为 `(0, 0, 0, 0)`
  ——正交偏振片在浮点里剩下 1e-16 量级的残差（甚至是负数），不这样做 `s = S/S0` 和 PBS 的分支比例就是噪声除噪声，两边对不上。
  功率为 0 的光照样往下追（几何不因功率改变）。
- 输出取整：`p`、`s`、`d_mm`、`m2` 取 1e-9；`q` 取 9 位有效数字；`mw`、`source_mw` 取 `MW_DECIMALS` 位；`-0.0` 写 `0.0`。

## 高度（契约 3D §2–§5、§12；逐条见 trace.py 的 “Heights”）

- 高度都是离台面的 mm：台面光束高度 H = `table.beam_height_mm`（没给 = 75）；器件光轴 `z(c)` = `props.z_mm`（没给 = H）；
  潜望镜 `z_in` = `z(c)`、`z_out` = `props.z_out_mm`（没给 = `z_in`）。光源从自己的 `z(c)` 出发，光纤续接的树从那个 `fiber_out` 的 `z(c)` 出发。
- 光线始终水平，只有潜望镜改变高度。平面上的候选（`|offset| ≤ A/2 + 1e-9`）再查高度：`dz = z光 − z_port`，`|dz| ≤ A/2 + 1e-9`
  才可能命中（工作面按 A × A 方形）；否则**越过**：不作用、照直走。`z_port` 是器件光轴；潜望镜沿 +x（`d·n > 0`）从 `z_in` 进、
  反向从 `z_out` 进。
- 光阑有 `open_mm` 时：`|dz| > A/2` 越过；否则 `|dz| > open/2 + 1e-9` → `blocked`；否则从开口穿过。
- 潜望镜命中后：`z' = z + (z_exit − z_entry)`（保留入射偏离）；竖直段 `|z_exit − z_entry|` 加进 `path_mm`（下一个事件起）和 `q`。
- 一段的 `passes = [{component, x, y, dz_mm}]`：命中之前（`t < t_hit − 1e-9`；与命中点重合的不算）、或离开平台那一段到终点为止
  （`t ≤ 长度`）的越过；按 `t` 排，`t` 与一串里第一个相差 ≤ 1e-9 的按器件 id（码位）排；全图最多 5000 条。越过不是事件。
- “险些没中”（`expected_misses`）只看在器件高度上的段：`|z − z_port| ≤ A/2 + 1e-9`。
- 取整：`z_mm`、`dz_mm`、越过的 `x`、`y`、`dz_mm` 取 0.001。高度都在 0.01 网格上、只做加减，两边逐位一致。

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

v2 的光束夹具（契约 v2 §7；锚点字母同契约，期望值在 `test_layout_beam_physics.py` 里独立算）：

| 文件 | 钉什么 |
|---|---|
| `pol_malus_two_polarizers` | A：H → 偏振片 30° → 偏振片 90°：`p` 0.75（ψ 30°）→ 0.1875（V），10 mW 的绝对功率 |
| `pol_hwp_rotates` | B：λ/2 快轴 22.5° 把 H 转到 +45°，PBS 50/50；快轴 15° → 30°，PBS 0.75 / 0.25（`fraction` 里也是这个比例） |
| `pol_qwp_isolator` | C、C2：λ/4（45°）→ 圆偏 `s = [0, 0, −1]`，正入射镜子翻手性，同一片从背面看是 135° → V，PBS 回程全走反射口（`0.t.r`，`fraction` 1）；去程反射支被剪掉 |
| `pol_mirror_flips_diagonal` | D：+45° 经镜子成 −45°；分束器的反射支同样翻（R = 0.3），透射支不变 |
| `pol_pbs_unpolarized_split` | E、E3：+45° 与非偏振光进 PBS 各半、出来是 H / V；灯（非偏振、无绝对功率）过 60° 偏振片：`p` 0.5、线偏 60° |
| `pol_pbs_p_only` | p 进 PBS 只有透射支、s 只有反射支（另一支功率 0 < 1e-3 不生成） |
| `pol_waveplate_oblique` | 斜入射看到的轴角：40° 斜入射看到 23.858655°（λ/2 出射 47.717310°）、背面看到 −30°（≡ 150°）、正面 30°；非偏振光斜过偏振片，出来沿它看到的轴 23.858655° |
| `power_nd_throughput_fibre` | 100 mW → OD1 → 镜子 0.9 → 光纤耦合 0.6：续接的树 `source_mw` 5.4，偏振 / 直径 / M² 取 `fiber_out` 自己的 |
| `power_fibre_first_arrival` | 同一束光的两支到达同一个光纤耦合：只按第一支（分支顺序）算 `source_mw`，不累加 |
| `power_throughput_chain` | 每一种收 `throughput` 的器件（含不带焦距的透镜 / 物镜、不带相位延迟的波片）连乘；光阑、快门开着时不扣 |
| `gauss_free_space` | F：633 nm Ø1 mm M² 1，zR 1240.755392 mm，1000 mm 处 Ø1.284357；M² = 2 的 zR 减半 |
| `gauss_kepler_3x` | G：f 50 / 150 开普勒扩束 3×：两透镜处 Ø1.002291、Ø3.000765，之后 1000 mm 处 Ø3.014325；两镜之间有焦点 |
| `gauss_objective_focus` | H：Ø4 mm 准直、f 4 mm 物镜：腰在后 4.000002 mm、Ø0.805958 µm；样品在后焦面（腰前 1.9 nm），Ø0.805961 µm = λf/(πw0) |
| `gauss_multi_wavelength` | 一个光源两个波长：每棵树都是全部的 `power_mw`、各自的 zR；二向色镜反射的那支 +45° 变 −45° |
| `beam_crossed_polarizers_no_light` | 30° 与 120° 两片偏振片正交：残差被“无光”规则置成精确的 0（`p` 0、`s` null、`mw` 0），几何照走；无光进 PBS 各 0.5 |

高度的夹具（契约 3D §9；期望值在 `test_layout_heights.py` 里不经过 trace.py 独立算）：

| 文件 | 钉什么 |
|---|---|
| `height_periscope_up` | 潜望镜把 75 mm 的光升到 250：之后的段 `z_mm` 250，越过 75 mm 的 M_LOW（dz +175）；竖直段 175 mm 进光程（M_HIGH `path_mm` 725）和高斯光束（Ø1.114077，不算竖直段是 1.067143） |
| `height_periscope_down` | 降的潜望镜，正反两向穿过，偏离保留：高度 203 → 63 → 63 → 203、每个事件 dz +3；`path_mm` 150 / 490 / 690 / 1010；探测器处 Ø2.040994 |
| `height_passes_over_and_under` | 从 100 mm 的镜子下方、50 mm 的镜子上方越过（按 t 排）；87.7 mm 的镜子正好在口径边上算命中；87.71 mm 的越过；77 mm、开口 5 的光阑穿过 |
| `height_source_heights` | 台面光束高度 120；光源从各自高度出发；光纤续接的树从 `fiber_out` 的 90 mm 出发 |
| `height_iris_in_height` | 光阑在高度方向的三种情形：越过（dz +35）、挡住（dz −3 > 2.5）、正好在开口边上穿过（dz −2.5） |
| `height_mixed` | 分束、高斯光束、叠放的两面镜子（打中同高的那面，另一面与命中点重合不算越过）、光阑在高度方向挡光、潜望镜升 225（偏离 +3 保留）、只有同高的那面算险些没中（`expected_misses`）、口径 6 的探测器 dz +3 正好在边上算命中，光斑在高度方向超出 0.878838 |

## 改夹具

改了追迹规则就要改夹具，并通知前端：

```powershell
$env:PYTHONPATH = "MASTv2"
.\.venv-v2-py313\Scripts\python.exe tests\fixtures\optical_layout\regenerate.py           # 重写
.\.venv-v2-py313\Scripts\python.exe tests\fixtures\optical_layout\regenerate.py --check   # 只比较（有变化 exit 1）
```

脚本重导每个文件的 `expected_trace`（有 `expected_misses` 的一并重导），`layout`、`name`、`description`、`trace_options`
原样保留；v2 光束夹具的图在脚本里定义（改那些图就改脚本）。脚本拒绝写出两边可能读出不同结果的数（落在取整临界点附近、
PBS 分支比例贴着 1e-3、偏振输出贴着无光门槛）。重导后人工核对物理，最后让两边的测试都过。不要只为了让测试变绿而重生成。
