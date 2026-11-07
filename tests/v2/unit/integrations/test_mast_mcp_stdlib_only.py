"""The MAST MCP server imports the Python standard library and nothing else.

Why it matters: the server has to start on MAST's bundled embeddable Python
(no pip, isolated ``._pth`` path mode), on the development venv, and on any
Python >= 3.10 a user happens to have, without installing a package. One stray
third-party import breaks the first of those without breaking the other two.

``platform`` and ``uuid`` are banned outright: on some Windows Pythons
``platform`` queries WMI and has been seen to hang at import time.

One exception: ``take_screenshot`` of the computer tools needs Pillow. Those tools
run only inside MAST.exe (remote AI access), which bundles it, so Pillow may be
imported inside a function body, never at module level, where the stdio server
would need it to start.
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports (same bootstrap as test_wrap_skill_minimal) ──
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
_SERVER_DIR = _REPO / "integrations" / "claude-code" / "server"
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import ast
import json
import os
import subprocess

import pytest

PACKAGE = "mast_mcp"
BANNED = {"platform", "uuid"}
#: Third-party packages a function may import lazily, and the only file allowed to
#: (see the module docstring).
LAZY_ONLY = {"PIL": "mast_mcp/computer.py"}
#: The modules that speak the wire protocol (JSON-RPC framing and the MCP methods).
TRANSPORTS = {"stdio_rpc.py", "http_rpc.py"}
EMBEDDED_PYTHON = _REPO / "MASTv2" / "pyruntime" / "python.exe"


def _server_files() -> list[Path]:
    files = sorted(_SERVER_DIR.rglob("*.py"))
    assert len(files) >= 7, f"expected run_server.py and the {PACKAGE} package, got {files}"
    return files


def _imports(path: Path) -> list[tuple[str, bool]]:
    """Every imported top-level package, with whether the import is inside a function."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, bool]] = []

    def visit(node, in_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                found.extend((alias.name.split(".")[0], in_function) for alias in child.names)
            elif isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
                found.append((child.module.split(".")[0], in_function))
            elif isinstance(child, ast.Call) and getattr(child.func, "id", "") == "__import__":
                pytest.fail(f"{path.name}: dynamic __import__ hides imports from this check")
            visit(child, in_function or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))

    visit(tree, False)
    return found


def _top_level_imports(path: Path) -> set[str]:
    return {name for name, _ in _imports(path)}


def test_server_imports_only_the_standard_library():
    allowed = set(sys.stdlib_module_names) | {PACKAGE}
    bad = set()
    for p in _server_files():
        rel = p.relative_to(_SERVER_DIR).as_posix()
        for name, lazy in _imports(p):
            if name in allowed or (lazy and LAZY_ONLY.get(name) == rel):
                continue
            bad.add(f"{rel}: {name}" + (" (inside a function)" if lazy else " (at module level)"))
    assert not bad, "non-stdlib imports in the MCP server:\n  " + "\n  ".join(sorted(bad))


def test_the_lazy_only_rule_is_alive(tmp_path):
    """The exception must not swallow a module-level Pillow import."""
    eager = tmp_path / "eager.py"
    eager.write_text("from PIL import Image\n\ndef shot():\n    from PIL import ImageGrab\n",
                     encoding="utf-8")
    assert _imports(eager) == [("PIL", False), ("PIL", True)]


def test_server_never_imports_platform_or_uuid():
    bad = {f"{p.name}: {name}" for p in _server_files()
           for name in _top_level_imports(p) if name in BANNED}
    assert not bad, f"banned modules imported: {sorted(bad)}"


def test_platform_and_uuid_are_not_pulled_in_through_the_stdlib(tmp_path):
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import mast_mcp.stdio_rpc, mast_mcp.config, mast_mcp.client, mast_mcp.tools, "
        "mast_mcp.resources, mast_mcp.vista_surface, mast_mcp.http_rpc, mast_mcp.computer, "
        "mast_mcp.remote_server; "
        "print(sorted(m for m in ('platform', 'uuid', 'PIL') if m in sys.modules))")
    proc = subprocess.run([sys.executable, "-c", probe, str(_SERVER_DIR)], capture_output=True,
                          text=True, timeout=60, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]", proc.stdout


def test_only_the_transports_speak_the_wire_protocol():
    """Swapping in an official MCP SDK must touch the transports only: keep the protocol there."""
    pkg = _SERVER_DIR / PACKAGE
    assert TRANSPORTS <= {p.name for p in pkg.glob("*.py")}
    for p in sorted(pkg.glob("*.py")):
        if p.name in TRANSPORTS:
            continue
        text = p.read_text(encoding="utf-8")
        for marker in ('"jsonrpc"', "sys.stdin", "sys.stdout"):
            assert marker not in text, f"{p.name} mentions {marker}; keep it in {sorted(TRANSPORTS)}"


def _list_tools_with(command: list[str], cwd: Path) -> dict:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    payload = "".join(json.dumps(m) + "\n" for m in messages).encode("utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("MAST_")}
    env["MAST_URL"] = "http://127.0.0.1:9"
    proc = subprocess.run(command + [str(_SERVER_DIR / "run_server.py")],
                          input=payload, capture_output=True, timeout=60, env=env, cwd=str(cwd))
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    lines = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines() if line]
    return {m["id"]: m for m in lines}


@pytest.mark.skipif(not EMBEDDED_PYTHON.is_file(),
                    reason="MASTv2/pyruntime/python.exe not present (it is built by "
                           "MASTv2/scripts/build_pyruntime.py and not kept in git)")
def test_the_embedded_runtime_starts_the_server_and_lists_the_tools(tmp_path):
    """MAST's bundled Python runs in isolated ``._pth`` mode: the script directory is
    not on sys.path there, so ``run_server.py`` has to put it there itself."""
    by_id = _list_tools_with([str(EMBEDDED_PYTHON)], tmp_path)
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert len(by_id[2]["result"]["tools"]) == 41


def test_isolated_mode_starts_the_server_and_lists_the_tools(tmp_path):
    """``-I`` keeps the script directory off sys.path and ignores PYTHON* variables, as
    the embedded runtime's ``._pth`` does; it runs wherever that runtime is not built."""
    by_id = _list_tools_with([sys.executable, "-I"], tmp_path)
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert len(by_id[2]["result"]["tools"]) == 41
