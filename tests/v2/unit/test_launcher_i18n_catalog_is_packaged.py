"""The launcher's translation catalog only reaches the frozen launcher if mast2.spec names it.

``launcher_i18n_catalog.py`` is a sibling of ``mast2_launcher.py`` (repo root, outside the
``mast`` tree) and the launcher imports it inside ``try: ... except Exception: return {}``
(``_load_tr_catalog``). If the spec stops naming it, nothing fails: the freeze finishes, the
launcher starts, and ja/de/fr/es/cs silently show English. None of the other packaging tests
look at this module, so this one pins:

* the module name is an entry of the ``extra_hidden`` list in mast2.spec, read from the syntax
  tree (a name in a comment, or in some other list, does not count);
* ``extra_hidden`` really feeds ``Analysis(hiddenimports=...)``;
* the file exists next to the launcher and the launcher imports it under that name.

The compile gate (test_every_module_compiles) and the release-hygiene gate
(test_release_hygiene) list the file as well, so a broken or identity-leaking catalog is caught
before the build.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SPEC = REPO / "mast2.spec"
LAUNCHER = REPO / "mast2_launcher.py"
MODULE = "launcher_i18n_catalog"


def _extra_hidden(spec_text: str) -> list[str]:
    """String entries of the ``extra_hidden`` list literal(s) in the spec."""
    names: list[str] = []
    for node in ast.walk(ast.parse(spec_text)):
        target = value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name)):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target == "extra_hidden" and isinstance(value, ast.List):
            names += [e.value for e in value.elts
                      if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return names


def _hiddenimports_expression(spec_text: str) -> str:
    """Source text of the ``hiddenimports=`` argument of the ``Analysis(...)`` call."""
    for node in ast.walk(ast.parse(spec_text)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Analysis"):
            for kw in node.keywords:
                if kw.arg == "hiddenimports":
                    return ast.unparse(kw.value)
    raise AssertionError("no Analysis(..., hiddenimports=...) call found in mast2.spec")


def test_the_spec_names_the_catalog_module():
    assert MODULE in _extra_hidden(SPEC.read_text(encoding="utf-8")), (
        f"mast2.spec no longer lists {MODULE!r} in extra_hidden: the frozen launcher would "
        "start without its ja/de/fr/es/cs strings and show English, with no error.")


def test_extra_hidden_feeds_the_analysis():
    expr = _hiddenimports_expression(SPEC.read_text(encoding="utf-8"))
    assert "extra_hidden" in expr, (
        f"Analysis(hiddenimports={expr}) no longer includes extra_hidden")


def test_the_catalog_file_exists_and_the_launcher_imports_it_by_that_name():
    assert (REPO / f"{MODULE}.py").is_file()
    assert f"from {MODULE} import" in LAUNCHER.read_text(encoding="utf-8")


def test_the_extraction_reads_string_entries_and_skips_comments():
    """Self-check on a synthetic spec (so it does not depend on the entry under test): a parser
    that returned [] would make the checks above indistinguishable from passing ones, and a name
    in a comment must not count."""
    synthetic = 'extra_hidden: list[str] = [\n    "alpha",\n    # "beta",\n    "gamma",\n]\n'
    assert _extra_hidden(synthetic) == ["alpha", "gamma"]


def test_a_name_in_another_list_does_not_count():
    assert _extra_hidden(f'mast_hidden = ["{MODULE}"]\nextra_hidden = []\n') == []
