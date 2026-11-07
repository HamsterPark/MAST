"""Where the runner finds its executables and keeps its records, in a development checkout
and in a frozen install.

README
------
:func:`resolve_runtime` returns a :class:`RuntimePaths` (the ``claude`` command, the MCP
bridge's python and ``run_server.py``, the hook script) or raises :class:`RuntimeSetupError`
with a sentence that names every place it looked. Overrides (environment):

=======================  ==================================================================
``MAST_CLAUDE_EXE``      Claude Code executable, a path or a name on PATH. Default: ``claude``
                         (Windows: ``claude.exe``, then ``claude.cmd``, then ``claude``)
``MAST_BRIDGE_PYTHON``   the python that runs the bridge and the hook script
``MAST_BRIDGE_SCRIPT``   the bridge's ``run_server.py``
=======================  ==================================================================

Defaults. Development checkout: ``<repo>/integrations/claude-code/server/run_server.py`` run by
``sys.executable`` (the venv MAST runs in). Frozen install (``sys.frozen``):
``<install>/integrations/claude-code/server/run_server.py`` run by
``<install>/MASTv2/pyruntime/python.exe`` (``MAST_PYRUNTIME_DIR`` moves the runtime, as for
:mod:`mast.pyexec.runtime`). ``<install>`` is the directory of MAST's executable, never
``project_root()``: the launcher points that at the user's data directory.

The hook script is ``compact_hook.py`` next to this file. It runs as a script under the
bridge's python, so a frozen build must ship it as a file (PyInstaller ``datas`` entry for
``mast/vista/runner/compact_hook.py``); otherwise the runner refuses to start and says so.

Run records live under :func:`runs_root` = ``<project_root>/experiments/vista/runs``.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

BRIDGE_RELPATH = ("integrations", "claude-code", "server", "run_server.py")
HOOK_SCRIPT = Path(__file__).resolve().with_name("compact_hook.py")


class RuntimeSetupError(RuntimeError):
    """An executable or script the runner needs is missing (the message says where it looked)."""


@dataclass(frozen=True)
class RuntimePaths:
    claude: tuple[str, ...]          # command prefix; tests use (python, fake_claude.py)
    bridge_python: str
    bridge_script: str
    hook_script: str

    def summary(self) -> dict:
        return {"claude": list(self.claude), "bridge_python": self.bridge_python,
                "bridge_script": self.bridge_script, "hook_script": self.hook_script}


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_root() -> Path:
    """The directory that holds ``integrations/`` and ``MASTv2/``: the executable's directory
    when frozen, else the repository root (``<repo>/MASTv2/mast/vista/runner/paths.py``)."""
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[4]


def runs_root() -> Path:
    from mast._runtime_paths import project_root

    return project_root() / "experiments" / "vista" / "runs"


def _override(environ: Mapping[str, str], key: str) -> str:
    return str(environ.get(key) or "").strip().strip('"')


def resolve_bridge_script(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    raw = _override(env, "MAST_BRIDGE_SCRIPT")
    candidates = [Path(raw).expanduser()] if raw else [install_root().joinpath(*BRIDGE_RELPATH)]
    if not raw and frozen() and getattr(sys, "_MEIPASS", None):
        candidates.append(Path(sys._MEIPASS).joinpath(*BRIDGE_RELPATH))  # type: ignore[attr-defined]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    where = "MAST_BRIDGE_SCRIPT" if raw else "the default locations"
    raise RuntimeSetupError(f"MCP bridge script not found ({where}: "
                            + ", ".join(str(p) for p in candidates)
                            + "); set MAST_BRIDGE_SCRIPT to integrations/claude-code/server/run_server.py")


def resolve_bridge_python(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    raw = _override(env, "MAST_BRIDGE_PYTHON")
    if raw:
        path = Path(raw).expanduser()
        if not path.is_file():
            found = shutil.which(raw, path=env.get("PATH"))
            if found:
                return Path(found)
            raise RuntimeSetupError(f"MAST_BRIDGE_PYTHON {raw!r} is not a file and not on PATH")
        return path
    if not frozen():
        return Path(sys.executable)
    from mast.pyexec.runtime import bundled_dir

    path = bundled_dir() / ("python.exe" if sys.platform == "win32" else "python3")
    if not path.is_file():
        raise RuntimeSetupError(f"bundled python not found at {path}; set MAST_BRIDGE_PYTHON "
                                "(or MAST_PYRUNTIME_DIR) to a Python >= 3.10")
    return path


def resolve_claude(environ: Mapping[str, str] | None = None, *, windows: bool | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    raw = _override(env, "MAST_CLAUDE_EXE")
    if raw:
        names = [raw]
    elif (os.name == "nt") if windows is None else windows:
        names = ["claude.exe", "claude.cmd", "claude"]
    else:
        names = ["claude"]
    for name in names:
        path = Path(name).expanduser()
        if path.is_absolute() or any(sep in name for sep in ("/", "\\")):
            if path.is_file():
                return [str(path)]
            continue
        found = shutil.which(name, path=env.get("PATH"))
        if found:
            return [found]
    raise RuntimeSetupError("Claude Code executable not found (tried "
                            + ", ".join(repr(n) for n in names)
                            + " on PATH); install Claude Code or set MAST_CLAUDE_EXE")


def resolve_hook_script() -> Path:
    if not HOOK_SCRIPT.is_file():
        raise RuntimeSetupError(f"hook script {HOOK_SCRIPT} is not a file in this build; ship "
                                "mast/vista/runner/compact_hook.py as a data file")
    return HOOK_SCRIPT


def resolve_runtime(environ: Mapping[str, str] | None = None) -> RuntimePaths:
    """All four paths, or :class:`RuntimeSetupError` naming the first one missing."""
    return RuntimePaths(claude=tuple(resolve_claude(environ)),
                        bridge_python=str(resolve_bridge_python(environ)),
                        bridge_script=str(resolve_bridge_script(environ)),
                        hook_script=str(resolve_hook_script()))
