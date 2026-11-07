"""The frozen build must carry comtypes (WITec optical station).

``mast2.spec`` gathers lazily imported packages in one ``collect_all`` loop whose
``except Exception: pass`` silently skips a package that is not installed. For
comtypes that would ship a build whose ``witec_com`` device can only report
"dependency missing". This pins comtypes into that loop and into
``requirements-v2.txt``, and checks that the spec warns when the build
environment lacks it.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _collect_all_packages() -> set[str]:
    tree = ast.parse((ROOT / "mast2.spec").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple)):
            continue
        calls = [n for n in ast.walk(node)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "collect_all"]
        if calls:
            return {e.value for e in node.iter.elts if isinstance(e, ast.Constant)}
    raise AssertionError("mast2.spec has no collect_all loop")


def test_the_spec_collects_comtypes():
    pkgs = _collect_all_packages()
    assert "docx" in pkgs, "self-check: this must be the collect_all loop"
    assert "comtypes" in pkgs


def test_requirements_list_comtypes_for_windows_only():
    text = (ROOT / "MASTv2" / "requirements-v2.txt").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("comtypes")]
    assert len(lines) == 1, lines
    assert 'sys_platform == "win32"' in lines[0]


def test_the_spec_warns_when_the_build_env_lacks_comtypes():
    src = (ROOT / "mast2.spec").read_text(encoding="utf-8")
    assert 'find_spec("comtypes")' in src
    assert "WARNING: comtypes is not installed" in src
