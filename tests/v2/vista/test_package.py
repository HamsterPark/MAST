"""The mast.vista package: import-light, and independent of STM-Bench (``stmbench`` / ``stmsim``).

Inside MAST nothing may need STM-Bench, although the development venv
has it installed. The import checks run in a fresh interpreter (this process has long imported
everything); the source scan also catches imports inside functions, where a loader
might import ``stmbench``.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import mast
import mast.vista

_MASTV2 = Path(mast.__file__).resolve().parents[1]
_PACKAGE = Path(mast.vista.__file__).resolve().parent
CORE_MODULES = ("protocol", "frames", "archive", "viewtools", "measure", "notes", "actions")


def _python(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(_MASTV2), PYTHONUTF8="1")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=300)


def test_importing_the_package_loads_no_submodule():
    r = _python("import sys, mast.vista; print(sorted(m for m in sys.modules if m.startswith('mast.vista.')))")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]"


def test_core_modules_import_with_stm_bench_unimportable():
    code = ("import sys\nsys.modules['stmbench'] = None\nsys.modules['stmsim'] = None\n"
            + "".join(f"import mast.vista.{m}\n" for m in CORE_MODULES) + "print('ok')")
    r = _python(code)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


def test_no_module_of_the_package_imports_stm_bench():
    offenders = []
    for path in sorted(_PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            else:
                continue
            offenders += [f"{path.name}:{node.lineno} {n}" for n in names if n.split(".")[0] in ("stmbench", "stmsim")]
    assert offenders == []
