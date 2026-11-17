# 光路图

操作员可以画**光路图**：光学平台的俯视图。图上有每面镜子、透镜、波片、激光器、探测器放在哪、朝哪个方向；它是什么（型号、厂家、作用）；是不是电动的、电机接到哪里；对应 MAST 里的哪台设备或哪个手动光学状态槽位。MAST 把同一张图、一次穿过它的光线追迹和一份文字叙述给你，这样你和操作员讨论的是真实的光路，而不是猜。

读一张光路图不会打开任何设备，也不会改任何东西。你可以在上面留评论；你不能改图，也不能解决评论（见[为什么你不能改图](#为什么你不能改图)）。

## 找到并读一张图

- 操作员画过图之后，`mast_briefing` 多一段 `optical_layouts`：有几张图、各自多少个器件、多少条未解决评论。没有图时这一段是空的，不占简报文字的篇幅。
- `mast_optical_layout` 不带 `layout_id` 时列出所有图。带 `layout_id` 时先返回文字叙述（先读它，它就是追迹的文字版），再是一段紧凑的 JSON，给出每个器件的几何。`image=true` 附一张 PNG，缩放到器件和光束所在的地方。
- 走 HTTP 就是 `GET /optical-layouts`、`GET /optical-layouts/{layout_id}`（含 `layout`、`trace`、`summary`、`links_state`、`comments`）和 `GET /optical-layouts/{layout_id}/render.png`；见 [API 参考](07-api-reference.md)。

叙述用请求的语言写（`X-MAST-Lang` 头，没有就用 MAST 运行的语言），总长不超过 8000 字符。“需要注意”里每一类最多列 3 行，其余用一行计数，任何一类都不会整类消失；太长时先省略光斑和焦点，再把每一类压到 1 行，再截断光路叙述（并写明截在哪里）。英文版长这样：

```text
Optical layout "SHG excitation" (id shg, revision 12)
Table 1200 x 900 mm, hole pitch 25 mm (metric); 5 components, 1 light source; last saved by ana 2026-11-03 12:00 UTC
Coordinates: origin at the table's lower left, x to the right, y up, in mm; angles counter-clockwise from +x.

Beam paths (in the order along the beam):
1) L1 laser (532 nm, Cobolt Samba, power 50 mW, linear 90 deg, Ø0.7 mm beam at the exit, at (50, 62.5) mm, motorized: no)
   → M1 mirror (Thorlabs BB1-E02, throughput 99%, at (112.5, 62.5) mm, hit 0.0 mm off center, incidence 45.0 deg, path 62.5 mm) · arriving 50 mW · spot Ø0.70 mm · linear 90° (s)
   → HWP1 waveplate (half-wave, fast axis 45 deg, at (112.5, 200) mm, hit 0.0 mm off center, incidence 0.0 deg, path 200.0 mm, motorized: yes, K10CR1, connected to COM12, MAST device hwp_rot (open), slot excitation_waveplate currently = hwp_532) · arriving 49.5 mW · spot Ø0.73 mm · linear 90° (s)
   → F1 lens (focal length 50 mm, at (112.5, 250) mm, hit 0.0 mm off center, incidence 0.0 deg, path 250.0 mm) · arriving 49.5 mW · spot Ø0.74 mm · linear 0° (p)
     focus: 50.89 mm after F1, Ø46.63 µm
   → ends at D1 detector (at (112.5, 350) mm, hit 0.0 mm off center, incidence 0.0 deg, path 350.0 mm) · arriving 49.5 mW · spot Ø0.71 mm · linear 0° (p)

Things to look at: none found.
```

沿光束的每个器件那一行末尾写着到达那里的光：功率（`入射 12.3 mW`；光源没填 `power_mw` 时写占光源的 `相对 24.6 %`）、光斑（`光斑 Ø1.20 mm`，小于 0.1 mm 用 µm；只有光源填了 `beam_diameter_mm` 才有）和偏振文字（`非偏振`、`线偏 30°`，靠近 0° / 90° 时加 `（p）` / `（s）`，`圆偏（χ = -45°）`、`椭圆偏 …`、`无光`；英文叙述里分别是 `unpolarised`、`linear 30°` 等）。一段光路里有腰时多一行，例如 `焦点：OBJ 之后 4.00 mm，Ø0.81 µm`。有适用的假设时开头多一行 `假设：`；“需要注意”里还会列出光斑超出器件口径、光被某个器件消光、波片没填相位延迟。用到高度的图（见[高度](#高度)）里，叙述还会写出台面光束高度、填了高度的器件的高度（潜望镜的入口和出口）、每次命中处光的高度；光越过某个器件时单独一行，例如 `↷ 从 M3 反射镜 上方越过（光高 250 mm，它的光轴在 75 mm）`（每段最多 3 行，其余一行计数）；“需要注意”多三类：高度偏离光轴超过口径的 20%、没被照到而有光从它上方或下方越过的器件、光斑在高度方向超出口径。

## 约定

长度单位 mm，角度单位度，波长单位 nm。原点在平台左下角，x 向右，y 向上（俯视），角度从 +x 逆时针量，取值在 [0, 360)。每个器件有中心、方向 `angle_deg`（它的局部 +x 轴指向哪里）和口径：**作用面**是过中心、垂直于方向、长度等于口径的线段。方向的含义因类型而异：

| 器件类别 | `angle_deg` 表示什么、追迹怎么处理 |
|---|---|
| 光源（`laser`、`fiber_out`、`lamp`、`led`） | 出射方向。每个波长从中心发一条光线。 |
| 类反射镜（`mirror`、`grating`、反射时的 `sample`、`behavior` 为 `reflect` 的 `custom`） | 反射面朝向。从这一面来的光线被反射；从背面来的被挡住（`back_hit`）。光栅按镜子处理（零级）。 |
| 分光（`beam_splitter`、`pbs`、`dichroic`） | 作用面的法线；两面都起作用。分束器把 `reflect_fraction` 的光反射、其余透射；偏振分束器（`pbs`）把来光的 p 分量透射、s 分量反射；二向色镜按波长（`edge_nm`、`reflects`）整束反射或整束透射。 |
| 透射类（`lens`、`objective`、`waveplate`、`polarizer`、`window`、`nd`、`aotf`、`prism`、`periscope`、`behavior` 为 `transmit` 的 `custom`） | 光轴。光线笔直穿过，哪个方向来都一样（透镜不使光线偏折；填了 `focal_mm` 时它会聚焦高斯光束）。 |
| 有条件的（`filter`、`iris`、`shutter`） | 滤光片让 `pass_min_nm`..`pass_max_nm` 内的光通过；光阑挡住偏离中心超过 `open_mm / 2` 的光线；关着的快门挡住一切。 |
| 终止类（`detector`、`camera`、`spectrometer`、`power_meter`、`beam_dump`、`blocker`、`fiber_in`、不反射时的 `sample`） | 接收光的那一面。光线到这里就结束；从背面来的被挡住（`back_hit`）。`fiber_in` 的 `continues_at` 指向某个 `fiber_out` 时把光接过去：那个输出成为同一波长的光源。 |

## 功率、偏振与光斑

追迹沿每条光线带着功率、偏振，以及（光源说了光束多粗时）高斯光束。它用到下面这些 props；都不是必填的，不填的图照旧追迹。

| prop | 用在 | 含义 |
|---|---|---|
| `power_mw` | `laser`、`fiber_out` | 输出功率 mW。有多个 `wavelengths_nm` 的光源在每个波长上都发全部功率。 |
| `polarization` | `laser`、`fiber_out` | `linear` 或 `unpolarized`。不填按非偏振光算：过偏振片或 `pbs` 时功率减半，叙述里会写明。 |
| `pol_angle_deg` | `laser`、`fiber_out` | `linear` 偏振的方向（只能和 `linear` 一起给；缺省 0）。 |
| `beam_diameter_mm` | `laser`、`fiber_out` | 出口处的 1/e² 直径，腰就在出口。不填就没有高斯光束。 |
| `m2` | `laser`、`fiber_out` | 光束质量因子 M²（只能和 `beam_diameter_mm` 一起给；缺省 1）。 |
| `throughput` | 反射镜、光栅、透镜、物镜、波片、偏振片、滤光片、窗片、AOTF、棱镜、潜望镜、样品、`custom` | 作用之后继续走的功率比例：镜子的反射率、透射件的透过率、偏振片沿轴的透过率、光栅的零级效率。缺省 1。 |
| `coupling` | `fiber_in` | 到达的光耦合进光纤的比例（缺省 1）。 |

偏振在每条光线自己的坐标系里描述：`e_h` 在平台平面内、指向传播方向的左侧，`e_v` 垂直平台向上。所有角度从 `e_h` 量向 `e_v`，所以 0° 在平台平面内（对平台上的任何反射都是 p），90° 垂直平台（s）。坐标系跟着光转，所以镜子会把 +45° 变成 −45°（135°）：这是反射的物理，不是错误。波片的轴（`fast_axis_deg`）和偏振片的轴（`axis_deg`）在器件自己的坐标系里量：0° 在平台平面内、沿器件面（局部 +y），90° 竖直向上。沿器件 `angle_deg` 方向传播的光（d·n > 0）从正面来，看到的就是给定的角；从背面来的光看到它的镜像（30° 读作 150°，即 −30°）。斜入射时光线看到的是真实器件的轴：偏振片按吸收型建模（线栅、二向色偏振片），透过的场垂直于吸收轴和光线；波片按正单轴晶体建模（石英、MgF₂），快波垂直于光轴和光线。负晶体（方解石）斜入射时修正方向相反；斜入射引起的相位延迟变化不建模。波片要有 `retardance`（`half` 或 `quarter`）才作用于偏振；没填就按透明处理。

功率 = 光源的 `power_mw` × 沿途每一处损耗：`throughput`、ND 的 10^-`od`、偏振片上的投影、分束器的比例、`pbs` 的 p 或 s 分量。由 `fiber_in` 续接的光纤输出发出的是到达那个耦合器的功率（按第一支到达的算）× 它的 `coupling`，偏振与光束用它自己的 `polarization`、`beam_diameter_mm`、`m2`。完全消光的光（正交偏振片之后）照样往下追，功率为 0：几何不因功率而变。

高斯光束的腰在光源出口，按瑞利长度 π w0² / (M² λ) 展开；填了 `focal_mm` 的 `lens` 或 `objective` 会改变它（薄透镜，1/q' = 1/q − 1/f），但不让光线偏折。

## 高度

光路图是俯视图，但器件可以装在不同高度，潜望镜可以把光升高或降低。下面三个可选的键描述这些；一个都没填的图追迹和叙述都和以前一模一样（所有东西都在同一高度）。

| 键 | 用在 | 含义 |
|---|---|---|
| `beam_height_mm` | `table` | 台面光束高度，mm（缺省 75）：没填 `z_mm` 的器件和光源都在这个高度。 |
| `z_mm` | 任何器件的 `props` | 器件光轴中心（光源：出口）离台面的高度，mm，±10000 以内；负数表示低于台面。 |
| `z_out_mm` | `periscope` 的 `props` | 潜望镜出口的高度（缺省等于入口高度）。 |

光线始终水平，只有潜望镜改变它的高度。光在平面上会打到一个器件，但它的高度离器件光轴超过口径的一半，就从器件上方或下方越过：不发生作用，光继续往前走。器件的工作面在高度方向也按口径算（口径 × 口径的方形）。填了 `open_mm` 的光阑还会挡住高度偏离超过开口一半的光；偏离超过口径一半的光整个越过光阑。

潜望镜有方向：沿它的 `angle_deg` 方向传播的光（d·n > 0）从 `z_mm` 进、从 `z_out_mm` 出；反方向的光从 `z_out_mm` 进、从 `z_mm` 出。光束保留它的偏离（比入口高 3 mm 进去，就比出口高 3 mm 出来，两面平行的 45° 镜就是这样），潜望镜里的竖直段算光程：计入 `path_mm`，高斯光束也在这一段里传播。

不建模：离开水平面的光线（潜望镜内部除外）、潜望镜以外的竖直光路、绕水平轴倾斜的镜子、三维折射、会转向的潜望镜（画成潜望镜加一面镜子）、潜望镜的镜筒挡光，以及圆形工作面（方形的四角也算口径内）。

## 追迹说了什么

追迹是一组树，每个光源每个波长一棵：`source`、`wavelength_nm`（不知道时是 `null`）、`beam` 和 `branches`。一个分支是一条折线路径（`segments`），沿途有各次作用（`events`）、`fraction`（沿途分束比的连乘，不是功率），以及它怎样结束（`end`）。分束器会产生子分支 `0.r`（反射）和 `0.t`（透射）。每个事件有 `component`、`action`、坐标 `x`、`y`，以及对准时要看的数：

- `offset_mm`：光束打在离器件中心多远的地方，沿作用面量，有符号（局部 +y 方向为正）。偏离中心超过口径 20% 的命中会被列为问题。
- `incidence_deg`：光束与作用面法线的夹角（0 = 正入射）。
- `path_mm`：从光源到这个器件的路径长。延迟线、泵浦探测的时序用它。
- `z_mm` 和 `dz_mm`：光到达时的高度，以及它离器件光轴（潜望镜按入口）多远；大于 0 表示光在光轴上方。每一段也有它的高度 `z_mm` 和 `passes`：这一段越过的器件，每条有 `component`、越过点 `x`、`y` 和 `dz_mm`（大于 0 表示从上方越过）。越过不是事件，也不会让器件算作被照到；整条追迹最多记 5000 条。

光束在每棵树（`source_mw` 光源功率、`m2`）、每一段（这一段起点的状态：`p`、`s`、`q`）和每个事件（到达、器件作用之前的状态：`p`、`mw`、`d_mm`、`s`）的 `beam` 键里。`p` 是相对光源的功率，含一切损耗；`mw` 是同一个量的 mW 数（光源没填 `power_mw` 时为 `null`）；`s` 是光线自己坐标系里归一化的 Stokes 向量 [S1, S2, S3] / S0（S1 = 水平 − 竖直，S2 = +45° − −45°；没有光到达时为 `null`）；`q` 是 [z, zR] mm，高斯光束参数（z > 0 表示已过腰）；`d_mm` 是 1/e² 直径。

| 取值 | 出现在 | 含义 |
|---|---|---|
| `reflect`、`transmit`、`split` | `action` | 光束被反射、穿过、或被分成两束。 |
| `stop`、`blocked`、`back_hit` | `action` 与 `end` | 光束在终止类器件上结束；被滤光片、光阑或关着的快门挡住；打在了器件的背面。 |
| `left_table` | `end` | 光束没碰到别的东西，离开了平台。 |
| `max_events` | `end` | 光束经过了 64 个器件：多半是个回环。 |
| `budget` | `end` | 整条追迹的 5000 次事件用完了：这是最后一个分支，它之后的都没有追。 |
| `split` | `end` | 这个分支在分束器上结束，后面是它的子分支。 |

整条追迹（所有光源合计）最多记 5000 次事件。一张图如果把光一次次分开（很多分束器、很多光源和波长），追迹本来可以大到没有边。到了上限追迹就停下：最后一支以 `budget` 结束，后面的支和光路都没有追，叙述开头写“追迹被截断：…”，并且不再下只有完整追迹才能下的结论（没被照到的器件、错过口径、光纤没有光进来）。真实的图离上限很远；看到它多半是分束器堆在了一起，请操作员看一下图。

追迹是带高度的俯视模型（见[高度](#高度)）：没有折射（透镜不让光线偏折），没有像差、色散，镜子是理想的（s、p 反射一样），口径切到光束只报告不扣功率，不算光纤模场匹配；光栅只按零级反射。叙述里说某束光“从某器件旁边经过”，意思是一条光线在器件的高度上、在离中心不到一个口径但落在口径之外的地方穿过了那个器件作用面所在的直线：那个器件多半是想被打中而没被打中。

## 链接说了什么

器件可以写上一台 MAST 设备（`links.device_id`）和一个手动光学状态槽位（`links.slot`）。链接只是引用；叙述（走 HTTP 是 `links_state`）会补上它们此刻的状态：设备是否启用、是否打开，进程里已经打开的设备还有它的状态，槽位当前签署的值。读取从不会打开设备。

## 评论

`mast_optical_layout_comment` 给一张图加一条评论，可以挂在某个器件上（`component_id`，必须在图里）。用它来问某个器件是什么、指出你在叙述里看到的问题（一束光没打到镜子、缺型号、需要知道的路径长），或者提建议。作者记成你的 agent id；操作员在光路图页面看到这条评论，标着来自 agent，由操作员来解决。

一张图上你最多有 50 条未解决的评论，一张图最多 1000 条评论；一条评论最长 2000 字符。

| 错误 | HTTP | 含义 |
|---|---|---|
| `unknown_layout` | 404 | 没有这个 id 的图。 |
| `layout_unreadable` | 500 | 这张图的文件读不了；文件原样保留。告诉操作员。 |
| `invalid_comment` | 422 | 文字是空的或太长，或者 `component_id` 不在图里。 |
| `too_many_open_comments` | 429 | 你在这里已经有 50 条未解决的评论：等操作员处理。 |
| `layout_comments_full` | 429 | 这张图已有 1000 条评论，一条都没解决。 |

## 为什么你不能改图

光路图记录的是台面上实际摆着什么，只有操作员知道：今天早上换了哪面镜子，光纤到底接到哪里。如果 agent 能在图里挪一面镜子、改一个型号，这张图回答的每个问题就都成了猜测。所以图只能在操作员的页面里改。这和你不能回答操作员提示、不能签署光学状态是同一条规则（见[光学站](09-optical-station.md)）：图里有哪里看着不对，用评论说出来；事情等不得，就用 `mast_ask_operator` 问，再用 `mast_wait` 等答复。

| 工具 | 作用 |
|---|---|
| `mast_optical_layout` | 列出所有图，或读一张（叙述、几何，可选附图）。 |
| `mast_optical_layout_comment` | 给一张图加一条评论。 |
