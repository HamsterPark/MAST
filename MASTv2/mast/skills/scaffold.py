"""Local-skill scaffold — 把「实战中验证过的做法」存成本地技能的最小工具。

习惯（见 ``docs/external/{zh,en}/06-contributing-skills.md``）：agent 在真实工作里写出了
被验证有用的流程/小工具，就把它存成**本地**技能并告诉用户；要不要 PR 由用户决定。

本地技能 = ``config/custom_skills/<Name>.py``（见 :mod:`mast.skills.custom_loader`）：
文件名 = 技能名 = ``enabled.json`` 的启用键；拷进去不会被执行，启用是操作员的显式动作；
该目录在 ``.gitignore`` 里，不会误进提交。本模块只做三件事：

* :func:`skill_template` —— 一份能直接通过合规检查的最小模板（纯计算、不碰仪器）；
* :func:`scaffold_local_skill` —— 把模板写进本地目录（不覆盖已有文件）；
* :func:`check_local_skill` —— 对本地文件跑与加载器/投稿同一套检查，把「加载时会静默跳过」
  的坑提前变成明确的话。

它不绕过任何安全门：模板默认 ``SafetyLevel.AUTO`` + 无硬件能力；要碰仪器的技能必须如实
声明安全级别与能力标签，合规检查（P04/V03 等）照常拦。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from mast.skills.custom_loader import custom_skills_dir

_NAME_RE = re.compile(r"^[A-Z][A-Za-z0-9]{2,60}$")
EVIDENCE_PLACEHOLDER = "TODO-EVIDENCE"

_TEMPLATE = '''"""{name} -- {summary}

Validated on / 验证依据: {evidence}
Origin / 来源: captured by an agent during real work with MAST ({date_hint}).

Edit execute() and the parameters; keep ONE skill class per file, and keep the file name
equal to the skill name. Pure computation (no instrument access) is the safe default; if
this must touch the instrument, declare the honest safety_level / capabilities and expect
the compliance checks to hold you to them.
"""

from __future__ import annotations

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.skills.base import BaseSkill

_NAME = "{name}"


class {name}(BaseSkill):
    """{summary}"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=_NAME,
            version="0.1.0",
            category=SkillCategory.ANALYSIS,
            safety_level=SafetyLevel.AUTO,
            description="{summary}",
            parameters=[
                ParameterSpec(
                    name="value",
                    type="float",
                    description="Example input; replace it. Write dimensional values as SI-prefixed strings such as '50n'.",
                    unit="m",
                    min_value=1e-12,
                    max_value=1e-3,
                ),
            ],
            preconditions=[],
            capabilities=frozenset(),
            estimated_duration_s=0.01,
            composition_level=2,
            tags=["captured"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        try:
            value = float(params.get("value"))
        except (TypeError, ValueError) as exc:
            return SkillResult(skill_name=_NAME, success=False, error=f"cannot read 'value': {{exc}}")
        return SkillResult(skill_name=_NAME, success=True, data={{"value": value}},
                           summary=f"value = {{value:g}}")
'''


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            f"bad skill name {name!r}: use CamelCase letters/digits only, starting with a capital "
            "(the file name, the class name, metadata().name and the enabled.json key are all this one word) / "
            "技能名只能是以大写字母开头的驼峰字母数字（文件名、类名、metadata().name、enabled.json 键同名）")


def skill_template(name: str, summary: str = "What this skill does, in one line.",
                   evidence: str = EVIDENCE_PLACEHOLDER + " (data / instrument run that proved it)",
                   date_hint: str = "") -> str:
    """The skill source for ``name`` (a CamelCase identifier)."""
    _check_name(name)
    summary = " ".join(str(summary).split()).replace('"', "'").replace("\\", "/")
    evidence = " ".join(str(evidence).split())
    return _TEMPLATE.format(name=name, summary=summary, evidence=evidence, date_hint=date_hint or "date unknown")


def scaffold_local_skill(name: str, summary: str = "What this skill does, in one line.",
                         evidence: str = EVIDENCE_PLACEHOLDER + " (data / instrument run that proved it)",
                         directory: Path | None = None, overwrite: bool = False) -> Path:
    """Write the template to ``<custom_skills>/<name>.py``; never overwrites unless asked."""
    d = Path(directory) if directory is not None else custom_skills_dir()
    code = skill_template(name, summary, evidence)
    path = d / f"{name}.py"
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists; pick another name or pass overwrite=True / 文件已存在，换个名字或显式覆盖")
    d.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
    return path


def check_local_skill(name: str, directory: Path | None = None, registry=None) -> dict:
    """Why would this local skill NOT load (or not be accepted upstream)?

    Returns ``{"ok", "problems", "warnings", "enabled", "path"}``. Runs the same compliance
    checks as a proposal or a contribution (deny-list, shape, file name = skill name, units,
    footprint, smoke run) plus the loader-level facts: the file exists, is listed in
    ``enabled.json``, and the evidence line was filled in.
    """
    d = Path(directory) if directory is not None else custom_skills_dir()
    path = d / f"{name}.py"
    out: dict = {"ok": False, "problems": [], "warnings": [], "enabled": False, "path": str(path)}
    try:
        _check_name(name)
    except ValueError as exc:
        out["problems"].append(str(exc))
        return out
    if not path.is_file():
        out["problems"].append(f"{path} does not exist / 文件不存在")
        return out
    code = path.read_text(encoding="utf-8")
    from mast.skills.compliance import check_python_source

    rep = check_python_source(code, filename=f"{name}.py", registry=registry, run_smoke=True).to_dict()
    for f in rep.get("findings", []):
        sev = str(f.get("level", "")).upper()
        text = f"{f.get('code', '')}: {f.get('message', '')}"
        if sev == "FAIL":
            out["problems"].append(text)
        elif sev == "WARN":
            out["warnings"].append(text)
    for sk in rep.get("skipped", []):
        out["warnings"].append(f"SKIPPED (not checked, not a pass): {sk}")
    if EVIDENCE_PLACEHOLDER in code:
        out["warnings"].append("the evidence line still says TODO-EVIDENCE: say what data or instrument run validated this "
                               "/ 验证依据还没填：写明是哪份数据或哪次仪器运行验证了它")
    enabled = path.parent / "enabled.json"
    try:
        names = json.loads(enabled.read_text(encoding="utf-8")).get("enabled", []) if enabled.is_file() else []
    except (OSError, ValueError):
        names = []
    out["enabled"] = name in names
    if not out["enabled"]:
        out["warnings"].append(f'not enabled yet: a person adds "{name}" to enabled.json and restarts MAST '
                               "/ 尚未启用：由操作员把名字加进 enabled.json 并重启")
    out["ok"] = not out["problems"]
    return out


__all__ = ["EVIDENCE_PLACEHOLDER", "check_local_skill", "scaffold_local_skill", "skill_template"]
