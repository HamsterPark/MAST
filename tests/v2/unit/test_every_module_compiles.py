"""Every Python file that ships has to compile.

PyInstaller only prints one warning when a module does not compile ("Syntax error while
compiling ...") and builds on without it; pytest only notices when some test imports the module.
A build once found ``mast/admin/html_builders.py`` broken because a refactor put
``from mast.i18n import tr`` above ``from __future__ import annotations``. Nothing imports that
module, so every test had stayed green.
"""
from __future__ import annotations

import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
#: What ends up in the package: the mast tree (PYZ + the _src copy), the client integrations
#: copied into the install folder, and the launcher compiled into MAST.exe together with its
#: sibling module launcher_i18n_catalog (the ja/de/fr/es/cs strings; mast2.spec names it in
#: hiddenimports, and a catalog that does not compile makes the launcher fall back to English
#: without any error).
ROOTS = (REPO / "MASTv2" / "mast", REPO / "integrations")
EXTRA = (REPO / "mast2_launcher.py", REPO / "launcher_i18n_catalog.py")


def _sources() -> list[Path]:
    files = [p for root in ROOTS for p in root.rglob("*.py") if "__pycache__" not in p.parts]
    return files + [p for p in EXTRA if p.is_file()]


def _compile_errors(paths: list[Path]) -> list[str]:
    bad = []
    for p in paths:
        try:
            with warnings.catch_warnings():
                # A SyntaxWarning (say, an invalid escape) still compiles and still ships.
                warnings.simplefilter("ignore", SyntaxWarning)
                compile(p.read_bytes(), str(p), "exec", dont_inherit=True)
        except SyntaxError as exc:
            name = p.relative_to(REPO).as_posix() if p.is_relative_to(REPO) else p.as_posix()
            bad.append(f"{name}:{exc.lineno}: {exc.msg}")
    return bad


def test_every_shipped_python_file_compiles():
    files = _sources()
    assert len(files) > 900, f"扫描口径坏了：只找到 {len(files)} 个文件"
    bad = _compile_errors(files)
    assert bad == [], ("这些文件编译不过；打包时 PyInstaller 只警告一句就把它们丢掉：\n  "
                       + "\n  ".join(bad))


def test_the_check_catches_an_import_above_the_future_import(tmp_path):
    p = tmp_path / "m.py"
    p.write_text('from mast.i18n import tr\n"""doc"""\n\nfrom __future__ import annotations\n',
                 encoding="utf-8")
    ok = tmp_path / "ok.py"
    ok.write_text('"""doc"""\n\nfrom __future__ import annotations\n\nfrom mast.i18n import tr\n',
                  encoding="utf-8")
    assert [e.rsplit(":", 2)[1] for e in _compile_errors([p, ok])] == ["4"]
