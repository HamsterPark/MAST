"""The skill-capture habit: the guidance reaches external agents, and the local path works.

* the rule is in what GET /guide serves (both languages) and in the MCP server instructions;
* the docs the plugin ships carry the section, and say local-first / user decides the PR;
* a scaffolded skill passes the compliance checks, loads through ``custom_loader`` once enabled
  and is NOT loaded before; a malformed one is rejected with a clear message;
* ``config/custom_skills/`` is gitignored.
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports ──
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
_MASTV2_ROOT = str(_REPO / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import json  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

from mast.api.ext import guide  # noqa: E402
from mast.skills import custom_loader  # noqa: E402
from mast.skills import scaffold  # noqa: E402

_SERVER_DIR = _REPO / "integrations" / "claude-code" / "server"


def test_guide_rule_in_both_languages():
    for lang in ("en", "zh"):
        r = {x["id"]: x for x in guide.rules(lang)}
        assert "capture-skills" in r
        text = r["capture-skills"]["text"]
        assert "config/custom_skills" in text and "contrib/skills" in text
    assert "LOCAL" in guide.instructions_text("en")
    assert "本地" in guide.instructions_text("zh")


def test_mcp_instructions_carry_the_habit():
    if str(_SERVER_DIR) not in sys.path:
        sys.path.insert(0, str(_SERVER_DIR))
    from mast_mcp.tools import INSTRUCTIONS

    rule = next(l for l in INSTRUCTIONS.splitlines() if l.startswith("16. "))
    for needle in ("LOCAL", "evidence", "only if the user wants", "safety gate", "mast_propose_skill"):
        assert needle in rule


@pytest.mark.parametrize("lang,needles", [
    ("en", ("keep what worked, locally first", "skill_scaffold.py", "enabled.json", "choice")),
    ("zh", ("好用的做法先存本地", "skill_scaffold.py", "enabled.json", "由用户定")),
])
def test_docs_and_plugin_copy_have_the_section(lang, needles):
    src = (_REPO / "docs" / "external" / lang / "06-contributing-skills.md").read_text(encoding="utf-8")
    ref = (_REPO / "integrations/claude-code/skills/mast-operator/references" / lang
           / "06-contributing-skills.md").read_text(encoding="utf-8")
    assert src == ref
    for n in needles:
        assert n in src
    author = (_REPO / "integrations/claude-code/skills/mast-skill-author/SKILL.md").read_text(encoding="utf-8")
    assert "capture what worked" in author


def test_custom_skills_dir_is_gitignored():
    r = subprocess.run(["git", "check-ignore", "-q", "config/custom_skills/Foo.py"], cwd=_REPO)
    assert r.returncode == 0, "config/custom_skills/ must stay out of git"


@pytest.mark.parametrize("bad", ["lower", "Has Space", "../Evil", "X", "9Lead", ""])
def test_bad_names_rejected_with_a_clear_message(bad):
    with pytest.raises(ValueError, match="bad skill name"):
        scaffold.skill_template(bad)


@pytest.fixture
def store(tmp_path, monkeypatch):
    d = tmp_path / "custom_skills"
    monkeypatch.setattr(custom_loader, "custom_skills_dir", lambda: d)
    monkeypatch.setattr(scaffold, "custom_skills_dir", lambda: d)
    return d


def test_scaffold_passes_checks_and_loads_only_after_enable(store):
    from mast.core.registry import SkillRegistry

    p = scaffold.scaffold_local_skill("CaptureProbe", "probe skill", "unit test fixture")
    assert p == store / "CaptureProbe.py"
    with pytest.raises(FileExistsError):
        scaffold.scaffold_local_skill("CaptureProbe")
    rep = scaffold.check_local_skill("CaptureProbe")
    assert rep["ok"], rep["problems"]
    assert any("not enabled" in w for w in rep["warnings"])
    assert not any("TODO-EVIDENCE" in w for w in rep["warnings"])

    reg = SkillRegistry()
    assert custom_loader.load_custom_skills(reg) == []          # copied, not enabled
    (store / "enabled.json").write_text(json.dumps({"enabled": ["CaptureProbe"]}), encoding="utf-8")
    assert scaffold.check_local_skill("CaptureProbe")["enabled"] is True
    reg = SkillRegistry()
    assert custom_loader.load_custom_skills(reg) == ["CaptureProbe"]
    res = reg.get("CaptureProbe")().execute(None, {"value": 2.0})
    assert res.success and res.data["value"] == 2.0


def test_placeholder_evidence_is_flagged(store):
    scaffold.scaffold_local_skill("CaptureProbe")
    rep = scaffold.check_local_skill("CaptureProbe")
    assert any("TODO-EVIDENCE" in w for w in rep["warnings"])


def test_malformed_local_skills_are_rejected_clearly(store):
    store.mkdir(parents=True)
    (store / "Missing.py").write_text("x = 1\n", encoding="utf-8")
    r = scaffold.check_local_skill("Missing")
    assert not r["ok"] and r["problems"]                          # no BaseSkill subclass
    (store / "Evil.py").write_text("import os\n" + scaffold.skill_template("Evil"), encoding="utf-8")
    r = scaffold.check_local_skill("Evil")
    assert not r["ok"] and any("S03" in p for p in r["problems"])  # deny-list
    (store / "Syntax.py").write_text("def (:\n", encoding="utf-8")
    assert not scaffold.check_local_skill("Syntax")["ok"]
    r = scaffold.check_local_skill("Nope")
    assert not r["ok"] and "does not exist" in r["problems"][0]
    # file name different from the skill name: the enable key would point at the wrong thing
    (store / "Other.py").write_text(scaffold.skill_template("Different"), encoding="utf-8")
    assert not scaffold.check_local_skill("Other")["ok"]
