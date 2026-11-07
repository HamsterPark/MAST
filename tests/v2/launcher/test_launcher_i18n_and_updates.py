"""Launcher language helpers, public releases URL, remote-AI handoff file."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_LAUNCHER = _REPO / "mast2_launcher.py"
#: MAST_LANG as the suite set it (tests/conftest.py: zh) before any test here changed it.
_PROCESS_LANG_AT_IMPORT = os.environ.get("MAST_LANG")


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("mast2_launcher_i18n", _LAUNCHER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Keep a clean module name so parallel launcher tests don't collide.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _keep_process_language(launcher, monkeypatch):
    """Give the process language back after every test.

    ``launcher._set_lang()`` also writes ``os.environ["MAST_LANG"]`` (``mast.i18n`` reads it).
    The "restore" at the end of the language test set it to "en", but the rest of the suite runs
    in Chinese (tests/conftest.py: ``MAST_LANG=zh``), so every test that pins Chinese wording and
    ran later in the same pytest process failed (test_skillpack x12, test_admin_launcher_entry x2)
    depending only on run order. monkeypatch puts the variable back after each test; the
    launcher module's own ``_LANG`` is restored too.
    """
    monkeypatch.setenv("MAST_LANG", os.environ.get("MAST_LANG", "zh"))
    saved = launcher._LANG
    yield
    launcher._LANG = saved


def test_supported_langs_and_public_releases(launcher):
    assert launcher._SUPPORTED_LANGS[0] == "en"
    assert launcher._SUPPORTED_LANGS == ("en", "zh", "ja", "de", "fr", "es", "cs")
    assert launcher.PUBLIC_RELEASES_URL.endswith("/HamsterPark/MAST/releases")
    assert launcher.PUBLIC_ISSUES_URL.endswith("/HamsterPark/MAST/issues")


def test_tr_catalog_covers_supported_alts(launcher):
    # A catalog that failed to load is {} (the loader swallows every error), so this also
    # checks that it did load; the exact size changes whenever the launcher's strings do.
    assert len(launcher._TR_CATALOG) >= 150
    sample = launcher._TR_CATALOG["Start main service"]
    for lang in ("ja", "de", "fr", "es", "cs"):
        assert sample[lang]


_CATALOG_LANGS = ("ja", "de", "fr", "es", "cs")


def _static_tr_english(source: str) -> dict[str, tuple[int, bool]]:
    """``{english text: (first line, every language given explicitly)}`` for each ``_tr(zh, en)``
    call whose English text is fixed: a literal, an f-string without placeholders, or literals
    joined with ``+``. Strings built from runtime values cannot have a catalog key."""
    import ast

    def fold(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr) and all(isinstance(v, ast.Constant) for v in node.values):
            return "".join(v.value for v in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = fold(node.left), fold(node.right)
            return None if left is None or right is None else left + right
        return None

    found: dict[str, tuple[int, bool]] = {}
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_tr" and len(node.args) >= 2):
            text = fold(node.args[1])
            if text is None:
                continue
            explicit = set(_CATALOG_LANGS) <= {kw.arg for kw in node.keywords}
            first, was_explicit = found.get(text, (node.lineno, True))
            found[text] = (min(first, node.lineno), was_explicit and explicit)
    return found


def test_every_fixed_tr_string_has_a_translation_in_every_catalog_language(launcher):
    """The catalog is the only source of ja/de/fr/es/cs text: a new UI string without an entry
    silently shows English there. Fails with the strings that are missing."""
    catalog = launcher._TR_CATALOG
    missing = []
    for text, (line, explicit) in sorted(
            _static_tr_english(_LAUNCHER.read_text(encoding="utf-8")).items(), key=lambda kv: kv[1]):
        if explicit:
            continue
        absent = [lang for lang in _CATALOG_LANGS if not (catalog.get(text) or {}).get(lang)]
        if absent:
            missing.append(f"line {line}: {text[:80]!r} has no {'/'.join(absent)} entry")
    assert not missing, (
        "add these English strings to launcher_i18n_catalog.CATALOG:\n  " + "\n  ".join(missing))


def test_the_catalog_coverage_check_sees_a_missing_entry(launcher):
    """Reverse self-check on a synthetic source: the scanner must find the string, and the
    catalog must not already know it."""
    found = _static_tr_english(
        '_tr("中", "Totally new launcher string")\n'
        '_tr("中", f"{x} built at run time")\n'
        '_tr("中", "Joined " + "literals")\n'
        '_tr("中", "Explicit", ja="a", de="b", fr="c", es="d", cs="e")\n')
    assert set(found) == {"Totally new launcher string", "Joined literals", "Explicit"}
    assert found["Explicit"][1] is True and found["Joined literals"][1] is False
    assert "Totally new launcher string" not in launcher._TR_CATALOG


def test_tr_uses_catalog_and_explicit_override(launcher):
    launcher._set_lang("en")
    assert launcher._tr("启动主服务", "Start main service") == "Start main service"
    launcher._set_lang("zh")
    assert launcher._tr("启动主服务", "Start main service") == "启动主服务"
    launcher._set_lang("ja")
    assert launcher._tr("启动主服务", "Start main service") == "メインサービスを開始"
    # Explicit kw overrides the catalog.
    assert launcher._tr("启动主服务", "Start main service", ja="開始") == "開始"
    # Unknown English key with no alt → English fallback.
    assert launcher._tr("未收录", "Totally unknown launcher string") == (
        "Totally unknown launcher string"
    )
    launcher._set_lang("en")  # restore for other tests in this module


def test_the_language_test_leaves_the_process_language_alone():
    """Runs right after test_tr_uses_catalog_and_explicit_override, which calls _set_lang()."""
    assert os.environ.get("MAST_LANG") == _PROCESS_LANG_AT_IMPORT


def test_lang_file_roundtrip(launcher, tmp_path: Path):
    assert launcher._read_lang_file(tmp_path) == "en"
    launcher._save_lang(tmp_path, "cs")
    assert launcher._read_lang_file(tmp_path) == "cs"
    launcher._save_lang(tmp_path, "nope")  # ignored
    assert launcher._read_lang_file(tmp_path) == "cs"


def test_remote_ai_handoff_roundtrip(launcher, tmp_path: Path, monkeypatch):
    handoff = tmp_path / "ai-remote.handoff.json"
    monkeypatch.setattr(launcher, "_remote_ai_handoff_path", lambda: handoff)
    assert launcher._load_remote_ai_handoff() == {}
    payload = {
        "prompt_line": "Control my computer X over SSH: ssh -i k -p 22 u@<rig-host>",
        "handover_note": "Carry this PRIVATE key file to the other computer: k",
        "host": "<rig-host>",
    }
    handoff.write_text(json.dumps(payload), encoding="utf-8")
    got = launcher._load_remote_ai_handoff()
    assert got["prompt_line"].startswith("Control my computer")
    assert "PRIVATE key" in got["handover_note"]
