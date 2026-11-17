# 投稿技能

你为自己这次会话造的技能，也可以经由社区贡献树 `contrib/skills/` 变成 MAST 里所有人
都能用的一部分。这一篇走一遍那个流程；spec 格式与提议机制本身见
[造技能](05-authoring-skills.md)。

## 习惯：好用的做法先存本地

如果在 MAST 的真实工作里你写出了被验证有用的流程或小工具——在真实数据或仪器上跑过、结果对——
就在会话结束前把它存成可复用技能，并告诉用户，不必等人来问。

1. **选形式。** 官方技能能表达就做组合技能（`mast_composite_save`，存在 `config/composite_skills/`）；
   否则写 Python 技能。
2. **默认存本地。** Python 技能用脚手架在本地目录生成并自检：

   ```bash
   python scripts/skill_scaffold.py new MySkill --summary "one line" --evidence "what validated it"
   python scripts/skill_scaffold.py check MySkill
   ```

   （或用 `mast_propose_skill` / `POST /skills/proposals` 送代码；同一目录、同一套检查。）
   文件是 `config/custom_skills/MySkill.py`：文件名、类名、`metadata().name`、`enabled.json`
   的键是同一个驼峰单词，一个文件一个技能类。该目录已 gitignore；在操作员把名字写进
   `config/custom_skills/enabled.json` 并重启 MAST 之前，里面什么都不会运行。`check` 会说出
   它为什么加载不了（加载器自己只留一行日志）：拒绝名单、形状、文件名、量纲与范围、硬件足迹、
   冒烟运行，以及验证依据是否还是占位符。以这种方式保存的组合技能落在 `config/composite_skills/`，
   那里也有受版本控制的文件：按路径逐个 stage，不要 `git add -A`。
3. **说明用什么验证的。** 在 docstring 的 `Validated on` 行填写数据集、帧或仪器运行，
   并向用户说明存了什么、放在哪、依据是什么。
4. **要不要 PR 由用户定。** 按其已表达的偏好；没有就问一次。要提升时：按下文把它放进
   `contrib/skills/<Name>/`，跑 `python scripts/skill_check.py contrib/skills/<Name>`，再开 PR。
5. **存下来的技能不绕过任何安全门。** 如实声明 `safety_level` 与能力标签；运行模式、令牌、
   占用锁对它与对其他技能一视同仁，合规检查会拒绝隐藏硬件足迹的技能。

## 两个层级

| 层级 | 在真实仪器上验证过吗 | 存放在哪 |
|---|---|---|
| 社区 | 没有 —— 它通过了自动检查和自己的测试；每个用户都要自己显式安装并启用它 | `contrib/skills/<Name>/` |
| 官方 | 是的，由维护者验证过 | 内置技能树 |

一份社区投稿从不会被自动发现，也从不会随包发出时就是启用状态：社区层里没有任何东西会
在谁的仪器上跑起来，除非那个人主动把它打开 —— 与一份提议的 Python 技能只有在操作员
审过、启用之后才会跑，是同一个道理。

## 一份投稿长什么样

每份投稿是一个目录，`contrib/skills/<Name>/`，里面有一份 `manifest.json`（名字、安全级、
足迹、作者、许可证与验证级别）、`skill.py` 或 `spec.json` 二选一、至少一个测试文件。
能表达成 `CompositeSpec` 的场合优先用 spec 而不是 Python 源码：它是数据，不是代码，不
新增任何执行路径，它的安全级与能力标签是从它经过的官方技能**继承**来的，而不是手工
声明的 —— 格式本身见[造技能](05-authoring-skills.md)。只有在任何官方技能组合都表达
不了你要的东西时才写 Python。

## 检查你的成果

一份提议回执里带的那套合规检查，同样能在一份代码检出上从命令行对着磁盘运行，好让你
在开 pull request 之前先在本地过几轮：

```bash
export PYTHONPATH=$PWD/MASTv2
python scripts/skill_check.py contrib/skills/<Name>
python -m pytest contrib -q
```

退出码 `0` 表示全部检查通过；`1` 表示至少有一项失败。一项工具根本没能跑起来的检查
（比如因为缺一个可选依赖）会被单独报出来，**不算通过**——把缺的东西装上再跑一遍，
不要把一项跳过的检查当成放行。

## 不接受的东西

| 不接受 | 为什么 |
|---|---|
| 移植自某篇已发表论文的方法，或者从别人仓库里拷来的代码 | 这棵树收的是原创工作；检查也会拒绝源码里出现的引用、DOI 与 arXiv 编号。 |
| 描述某一台具体仪器的默认值（某次标定、某个压电范围、某个控制器增益、某个实测工作点） | 默认值必须与具体仪器无关，或者干脆不设默认值、只给出明确的范围。 |
| 夹在技能 PR 里的其他改动 | 技能 PR 只改 `contrib/skills/<Name>/`；别处的改动请单独发 PR（见仓库根目录的 `CONTRIBUTING.zh.md`），合并后回流开发仓、在那里测试、随下一次发布出去。 |

## 许可证与签署

投稿按 MIT 许可证接受，与项目其余部分同一份许可证；每个 commit 都需要一行签署
（`git commit -s`），证明[开发者原创声明](https://developercertificate.org/)：这份贡献
是你写的，或者你确实有权在这份许可证下提交它。你的贡献也可能被收进基于这个项目构建的
闭源或商业发行版 —— MIT 许可证允许这样做，提交即表示你同意。如果这一点你不能接受，
请不要提交。

## 毕业

一个社区技能在维护者把它跑上真实仪器并验证过之后就会毕业。它随后会离开 `contrib/`，
进入官方树，主版本号也会被调高 —— 从那一刻起它被要求达到官方标准，而不再是社区标准 ——
并且会被记进 `contrib/GRADUATED.md`，把这份功劳留给写它的人。
