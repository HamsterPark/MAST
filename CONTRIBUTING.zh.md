# 为 MAST 投稿

[English](CONTRIBUTING.md)

**欢迎为 MAST 扩展可复用的实验技能。** 投稿可以把新的数据分析方法、仪器操作或多步流程接入现有的
参数校验、执行约束与操作界面。无论由人编写还是由编码 agent 辅助完成，都采用同一套检查与验证流程。

本仓是 MAST 的公开精简源码版，技能投稿集中在 `contrib/skills/`，经社区检查与维护者真机验证逐步进入官方树。
对其余源码的 PR（缺陷修复、文档、测试、前端）同样欢迎，处理方式见
[PR 怎么合并](#pr-怎么合并)。漏洞详情请按 [`SECURITY.zh.md`](SECURITY.zh.md) 私密报告。

## 投稿范围

- `contrib/skills/<名>/` 下的新技能：优先用 **CompositeSpec JSON** 复用现有官方技能；
  需要现有组合无法表达的新执行逻辑时，再编写 **Python 技能**。
- 对 `contrib/` 里已有投稿的修正。
- 仓库其他任何位置的 PR：缺陷修复、文档勘误、测试、前端改动。请保持聚焦；较大或涉及设计的改动请先开 issue。

## 范围与约束

- **已发表工作的移植。** 重新实现某篇论文里的方法、或从别人的仓库移植过来的代码。这份快照刻意不带它们；
  检查器会拒掉 citations、DOI 与 arXiv 编号（代号 `M03`）。
- **描述某一台仪器的默认值。** 在你那台机器上测出来的标定值、压电量程、控制器增益、偏压 / 电流工作点。
  参数的默认值要与仪器无关（或者不给），并写明 `min_value` / `max_value`。
- **本快照没有收录的内容。** 开发仓里有一部分内容刻意不在这里（快照说明列出了它们）。针对这些部分的改动
  无法回流；请改开 issue 说明。

## PR 怎么合并

开发仓是真源，本仓在每次发布时由它重新生成。因此一个 PR 这样处理：

1. 维护者在 GitHub 上审阅并合并。
2. 维护者把合并后的改动回流到开发仓（逐文件三路合并），核对发布导出能原样复现合并后的文件，并在开发仓跑全量测试。
3. 改动随下一次发布出去。
4. 公开 `main` 每次发布都会重建，所以你的提交会被发布提交取代；开发仓的提交里会用 `Co-authored-by:` 给你署名，
   投稿的技能毕业时还会记入 [`contrib/GRADUATED.md`](contrib/GRADUATED.md)。

改动若落在发布导出会排除的路径、或导出无法复现，会连同原因退回给你。这里的 CI 只跑一部分测试；
全量测试在发布前由维护者那边跑。

## 两级制

| 级别 | 位置 | 投稿进入该层的要求 |
|---|---|---|
| 社区 | `contrib/skills/<名>/` | 通过检查器与 CI；使用者自行安装、显式启用。尚未经过维护者真机验证。 |
| 官方 | `MASTv2/mast/skills/builtins/`、`config/composite_skills/` | 社区技能转入官方树前，需经过维护者真机验证。 |

此表说明投稿流程，不代表现有 builtin 或复合技能中的每项功能都已通过真机验证。
已公开的验证边界见 [README.zh.md](README.zh.md)，本次快照的检查记录见[快照说明](docs/OPEN_SOURCE_NOTES.md)。

目录约定、manifest 格式、每个检查代号的含义、怎么在本地装一个社区技能，见
[`contrib/README.zh.md`](contrib/README.zh.md)。

## PR 清单

使用 Python 3.13 环境；仅检查投稿时安装 `MASTv2/requirements-ci.txt` 即可。
以下 `python` 指所选环境的解释器，命令在仓库根目录运行。环境准备与更广的验证路线见
[AGENTS.zh.md](AGENTS.zh.md#无需硬件的验证)。

- [ ] 技能投稿只改 `contrib/skills/<名>/` 下的文件；其他 PR 保持聚焦于一件事，并同步更新它动到的测试与文档。
- [ ] 有 `manifest.json`，有 `skill.py` **或** `spec.json`，至少一个测试（`test_<名>.py`，文件名在整个
      `contrib/` 里没被用过）。
- [ ] `python scripts/skill_check.py contrib/skills/<名>` 退出码为 `0`。
- [ ] `python -m pytest contrib -q` 通过。
- [ ] manifest 里 `policy` 的几项声明都属实。
- [ ] 每个提交都签了 DCO（见下）。

以上为投稿者在本地完成的检查。CI 执行 `python scripts/skill_check.py --all-contrib`，
随后运行投稿合规测试、检查器变异测试与文档守卫；另有一个作业运行范围更宽的后端测试子集，
以及前端类型检查与单元测试。当前命令见
[`.github/workflows/ci.yml`](.github/workflows/ci.yml)。

## 验证级别

| 级别 | 谁定 | 含义 |
|---|---|---|
| `unit-tested` | 投稿者 | 通过检查器与自带测试，未声明真机验证。这是最低要求。 |
| `contributor-hardware` | 投稿者 | 还在投稿者自己的仪器上跑过；`hardware_notes` 写明在什么仪器 / 控制器版本上、跑了什么、看到了什么。 |
| 毕业 | 维护者 | 维护者在自己的仪器上验证过，并移进官方树。 |

## 毕业流程

1. 维护者在真实仪器上跑这个技能。
2. 它移进 `MASTv2/mast/skills/builtins/`（Python）或 `config/composite_skills/`（spec），`contrib/` 里的
   那份删掉。途中代码可能按内部约定改写。
3. **主版本号 +1** —— 从此按官方标准要求它。
4. [`contrib/GRADUATED.md`](contrib/GRADUATED.md) 记下这个技能与它的作者。署名一直留着。

## 开发者原创声明（DCO）

每个提交都要带一行 `Signed-off-by: 你的名字 <邮箱>`（`git commit -s`），表示你认可
[Developer Certificate of Origin 1.1](https://developercertificate.org/)：这份投稿是你写的，或者你有权
按下面的许可提交它。

## 投稿的许可

- **入站 = 出站：** 投稿按 MIT 许可接收（[`LICENSE`](LICENSE)）。
- **投稿可能同时被收进 MAST 的闭源或商业发行版。** MIT 许可允许这样做，提交即表示你同意。
  如果你不希望这样，请不要提交。

## 技术细节

- [`docs/external/zh/05-authoring-skills.md`](docs/external/zh/05-authoring-skills.md) —— 怎么写一个技能：
  参数与单位、安全级、能力标签、足迹。
- [`docs/external/zh/06-contributing-skills.md`](docs/external/zh/06-contributing-skills.md) —— 投稿流程，一步一步。
- [`contrib/README.zh.md`](contrib/README.zh.md) —— 目录约定、manifest、检查代号、本地安装。
