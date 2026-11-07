# 光路图

操作员可以画**光路图**：光学平台的俯视图。图上有每面镜子、透镜、波片、激光器、探测器放在哪、朝哪个方向；它是什么（型号、厂家、作用）；是不是电动的、电机接到哪里；对应 MAST 里的哪台设备或哪个手动光学状态槽位。MAST 把同一张图、一次穿过它的光线追迹和一份文字叙述给你，这样你和操作员讨论的是真实的光路，而不是猜。

读一张光路图不会打开任何设备，也不会改任何东西。你可以在上面留评论；你不能改图，也不能解决评论（见[为什么你不能改图](#为什么你不能改图)）。

## 找到并读一张图

- 操作员画过图之后，`mast_briefing` 多一段 `optical_layouts`：有几张图、各自多少个器件、多少条未解决评论。没有图时这一段是空的，不占简报文字的篇幅。
- `mast_optical_layout` 不带 `layout_id` 时列出所有图。带 `layout_id` 时先返回文字叙述（先读它，它就是追迹的文字版），再是一段紧凑的 JSON，给出每个器件的几何。`image=true` 附一张 PNG，缩放到器件和光束所在的地方。
- 走 HTTP 就是 `GET /optical-layouts`、`GET /optical-layouts/{layout_id}`（含 `layout`、`trace`、`summary`、`links_state`、`comments`）和 `GET /optical-layouts/{layout_id}/render.png`；见 [API 参考](07-api-reference.md)。

叙述用请求的语言写（`X-MAST-Lang` 头，没有就用 MAST 运行的语言），超过 8000 字符会被截断。英文版长这样：

```text
Optical layout "SHG excitation" (id shg, revision 12)
Table 1200 x 900 mm, hole pitch 25 mm (metric); 4 components, 1 light source; last saved by ana 2026-11-03 12:00 UTC
Coordinates: origin at the table's lower left, x to the right, y up, in mm; angles counter-clockwise from +x.

Beam paths (in the order along the beam):
1) L1 laser (532 nm, Cobolt Samba, at (50, 62.5) mm, motorized: no)
   → M1 mirror (Thorlabs BB1-E02, at (112.5, 62.5) mm, hit 0.0 mm off center, incidence 45.0 deg, path 62.5 mm)
   → HWP1 waveplate (half-wave, at (112.5, 200) mm, hit 0.0 mm off center, incidence 0.0 deg, path 200.0 mm, motorized: yes, K10CR1, connected to COM12, MAST device hwp_rot (open), slot excitation_waveplate currently = hwp_532)
   → ends at D1 detector (at (112.5, 300) mm, hit 0.0 mm off center, incidence 0.0 deg, path 300.0 mm)

Things to look at: none found.
```

## 约定

长度单位 mm，角度单位度，波长单位 nm。原点在平台左下角，x 向右，y 向上（俯视），角度从 +x 逆时针量，取值在 [0, 360)。每个器件有中心、方向 `angle_deg`（它的局部 +x 轴指向哪里）和口径：**作用面**是过中心、垂直于方向、长度等于口径的线段。方向的含义因类型而异：

| 器件类别 | `angle_deg` 表示什么、追迹怎么处理 |
|---|---|
| 光源（`laser`、`fiber_out`、`lamp`、`led`） | 出射方向。每个波长从中心发一条光线。 |
| 类反射镜（`mirror`、`grating`、反射时的 `sample`、`behavior` 为 `reflect` 的 `custom`） | 反射面朝向。从这一面来的光线被反射；从背面来的被挡住（`back_hit`）。光栅按镜子处理（零级）。 |
| 分光（`beam_splitter`、`dichroic`） | 作用面的法线；两面都起作用。分束器把 `reflect_fraction` 的光反射、其余透射；二向色镜按波长（`edge_nm`、`reflects`）整束反射或整束透射。 |
| 透射类（`lens`、`objective`、`waveplate`、`polarizer`、`window`、`nd`、`aotf`、`prism`、`periscope`、`behavior` 为 `transmit` 的 `custom`） | 光轴。光线笔直穿过，哪个方向来都一样。 |
| 有条件的（`filter`、`iris`、`shutter`） | 滤光片让 `pass_min_nm`..`pass_max_nm` 内的光通过；光阑挡住偏离中心超过 `open_mm / 2` 的光线；关着的快门挡住一切。 |
| 终止类（`detector`、`camera`、`spectrometer`、`power_meter`、`beam_dump`、`blocker`、`fiber_in`、不反射时的 `sample`） | 接收光的那一面。光线到这里就结束；从背面来的被挡住（`back_hit`）。`fiber_in` 的 `continues_at` 指向某个 `fiber_out` 时把光接过去：那个输出成为同一波长的光源。 |

## 追迹说了什么

追迹是一组树，每个光源每个波长一棵：`source`、`wavelength_nm`（不知道时是 `null`）和 `branches`。一个分支是一条折线路径（`segments`），沿途有各次作用（`events`）、占光源强度的份额 `fraction`，以及它怎样结束（`end`）。分束器会产生子分支 `0.r`（反射）和 `0.t`（透射）。每个事件有 `component`、`action`、坐标 `x`、`y`，以及三个对准时要看的数：

- `offset_mm`：光束打在离器件中心多远的地方，沿作用面量，有符号（局部 +y 方向为正）。偏离中心超过口径 20% 的命中会被列为问题。
- `incidence_deg`：光束与作用面法线的夹角（0 = 正入射）。
- `path_mm`：从光源到这个器件的路径长。延迟线、泵浦探测的时序用它。

| 取值 | 出现在 | 含义 |
|---|---|---|
| `reflect`、`transmit`、`split` | `action` | 光束被反射、穿过、或被分成两束。 |
| `stop`、`blocked`、`back_hit` | `action` 与 `end` | 光束在终止类器件上结束；被滤光片、光阑或关着的快门挡住；打在了器件的背面。 |
| `left_table` | `end` | 光束没碰到别的东西，离开了平台。 |
| `max_events` | `end` | 光束经过了 64 个器件：多半是个回环。 |
| `budget` | `end` | 整条追迹的 5000 次事件用完了：这是最后一个分支，它之后的都没有追。 |
| `split` | `end` | 这个分支在分束器上结束，后面是它的子分支。 |

整条追迹（所有光源合计）最多记 5000 次事件。一张图如果把光一次次分开（很多分束器、很多光源和波长），追迹本来可以大到没有边。到了上限追迹就停下：最后一支以 `budget` 结束，后面的支和光路都没有追，叙述开头写“追迹被截断：…”，并且不再下只有完整追迹才能下的结论（没被照到的器件、错过口径、光纤没有光进来）。真实的图离上限很远；看到它多半是分束器堆在了一起，请操作员看一下图。

追迹是俯视的几何模型：没有高度，不模拟透镜聚焦、折射、偏振和色散；光栅只按零级反射。叙述里说某束光“从某器件旁边经过”，意思是一条光线在离中心不到一个口径、但落在口径之外的地方穿过了那个器件作用面所在的直线：那个器件多半是想被打中而没被打中。

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
