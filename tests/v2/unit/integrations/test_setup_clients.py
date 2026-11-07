"""``integrations/setup_clients.py``: connecting Claude Code and Codex to an installed MAST.

What is pinned here, and why each one matters:

* ``patch-plugin`` changes the plugin's ``python`` default and **nothing else** in
  ``plugin.json`` (the installer runs it on every install, so it must also be idempotent).
* ``codex`` edits a file that belongs to the user. Everything outside the
  ``[mcp_servers.mast...]`` tables must survive byte for byte, an old entry must be
  replaced rather than duplicated, ``--uninstall`` must give the original file back, and
  ``--dry-run`` must write nothing at all.
* Windows paths (spaces, Chinese, a quote) must reach Codex unchanged.
* ``--approve read_only`` pre-approves exactly the tools the bridge marks read-only.
* The script runs under ``python -I`` (MAST's embeddable runtime): standard library only.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
SETUP = REPO / "integrations" / "setup_clients.py"
PLUGIN_JSON = REPO / "integrations" / "claude-code" / ".claude-plugin" / "plugin.json"
AGENTS_MD = REPO / "integrations" / "codex" / "AGENTS.md"
EXAMPLE = REPO / "integrations" / "codex" / "mast_mcp.example.toml"


@pytest.fixture(scope="module")
def sc():
    spec = importlib.util.spec_from_file_location("setup_clients_under_test", SETUP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _no_codex_keys(monkeypatch):
    for var in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)


def make_app(root: Path, *, runtime: bool = True, bridge: str = "stub") -> Path:
    """A MAST installation folder with just what setup_clients.py touches."""
    plugin = root / "integrations" / "claude-code" / ".claude-plugin" / "plugin.json"
    plugin.parent.mkdir(parents=True)
    shutil.copy2(PLUGIN_JSON, plugin)
    server = root / "integrations" / "claude-code" / "server"
    if bridge == "real":
        shutil.copytree(REPO / "integrations" / "claude-code" / "server", server,
                        ignore=shutil.ignore_patterns("__pycache__"))
    else:
        server.mkdir(parents=True)
        (server / "run_server.py").write_text("# stub\n", encoding="utf-8")
    codex = root / "integrations" / "codex"
    codex.mkdir(parents=True)
    shutil.copy2(AGENTS_MD, codex / "AGENTS.md")
    if runtime:
        rt = root / "MASTv2" / "pyruntime" / "python.exe"
        rt.parent.mkdir(parents=True)
        rt.write_bytes(b"")
    return root


def codex_args(app: Path, tmp: Path, *extra: str) -> list[str]:
    return ["codex", "--app", str(app), "--codex-home", str(tmp / "codex"),
            "--workspace", str(tmp / "ws"), *extra]


def backups(folder: Path, name: str = "config.toml") -> list[Path]:
    return sorted(folder.glob(name + ".bak-*"))


# ── patch-plugin ──────────────────────────────────────────────────────────────
class TestPatchPlugin:
    def test_only_the_python_default_changes(self, sc, tmp_path):
        app = make_app(tmp_path / "MAST")
        path = app / sc.PLUGIN_JSON
        before = path.read_text(encoding="utf-8")
        assert before.count('"default": "python"') == 1     # the precondition of the check below

        assert sc.main(["patch-plugin", "--app", str(app)]) == 0

        want = str(app / sc.PYRUNTIME)
        after = path.read_text(encoding="utf-8")
        assert after == before.replace('"default": "python"', '"default": ' + json.dumps(want))
        assert json.loads(after)["userConfig"]["python"]["default"] == want

    def test_running_it_again_changes_nothing(self, sc, tmp_path, capsys):
        app = make_app(tmp_path / "MAST")
        path = app / sc.PLUGIN_JSON
        assert sc.main(["patch-plugin", "--app", str(app)]) == 0
        first = path.read_bytes()
        capsys.readouterr()
        assert sc.main(["patch-plugin", "--app", str(app)]) == 0
        assert path.read_bytes() == first
        assert "already" in capsys.readouterr().out

    def test_a_folder_with_spaces_and_chinese(self, sc, tmp_path):
        app = make_app(tmp_path / "MAST 安装 目录")
        path = app / sc.PLUGIN_JSON
        assert sc.main(["patch-plugin", "--app", str(app)]) == 0
        raw = path.read_bytes()
        assert json.loads(raw)["userConfig"]["python"]["default"] == str(app / sc.PYRUNTIME)
        assert b"\r\n" not in raw          # integrations/** is LF in git; keep it that way

    def test_without_a_bundled_runtime_it_refuses_and_leaves_the_file(self, sc, tmp_path, capsys):
        app = make_app(tmp_path / "MAST", runtime=False)
        path = app / sc.PLUGIN_JSON
        before = path.read_bytes()
        assert sc.main(["patch-plugin", "--app", str(app)]) == 1
        assert path.read_bytes() == before
        assert "no bundled runtime" in capsys.readouterr().err

    def test_a_drive_root_mangled_by_the_command_line_is_recovered(self, sc):
        # Inno passes --app "D:\" ; the Windows command line turns \" into a quote: D:"
        assert sc._app_dir('D:"') == Path("D:\\")
        assert sc._app_dir(r"C:\MAST") == Path(r"C:\MAST")

    def test_the_json_walker_finds_the_value_and_not_a_lookalike(self, sc):
        text = ('{"a": "x}\\"{", "userConfig": {"z": [1, {"q": "]"}], '
                '"python": {"title": "default", "default": "python", "x": 1}}}')
        start, end = sc.json_value_span(text, ("userConfig", "python", "default"))
        assert text[start:end] == '"python"'
        start, end = sc.json_value_span(text, ("userConfig", "z"))
        assert text[start:end] == '[1, {"q": "]"}]'


# ── codex: merging into config.toml ───────────────────────────────────────────
EXISTING = (
    "# my Codex settings\n"
    'model = "gpt-6.1-sol"\n'
    'approval_policy = "on-request"\n'
    "\n"
    "[mcp_servers.github]\n"
    'command = "npx"\n'
    "args = [\n"
    '  "-y",\n'
    '  "@modelcontextprotocol/server-github",\n'
    "]\n"
    "\n"
    '[mcp_servers.mastodon]   # another server whose name starts with "mast"\n'
    'command = "mastodon-mcp"\n'
    'notes = """\n'
    "[mcp_servers.mast]\n"
    "this line is inside a multi-line string, not a table\n"
    '"""\n'
    "matrix = [\n"
    "  [1, 2],\n"
    "  [3, 4],\n"
    "]\n"
    "\n"
    "[projects.'C:\\Users\\me\\工作 目录']\n"
    'trust_level = "trusted"\n'
)


class TestCodexMerge:
    def _block(self, sc, app, tmp_path, nl="\n"):
        table = sc.codex_table(python=app / sc.PYRUNTIME, run_server=app / sc.RUN_SERVER,
                               workspace=tmp_path / "ws", mast_url=sc.DEFAULT_MAST_URL,
                               actor="codex")
        return table, sc.render_codex_block(table, nl)

    @pytest.mark.parametrize("nl", ["\n", "\r\n"])
    def test_everything_else_stays_byte_for_byte_and_uninstall_restores_it(self, sc, tmp_path, nl):
        app = make_app(tmp_path / "MAST")
        cfg = tmp_path / "codex" / "config.toml"
        cfg.parent.mkdir()
        original = EXISTING.replace("\n", nl).encode("utf-8")
        cfg.write_bytes(original)

        assert sc.main(codex_args(app, tmp_path)) == 0

        table, block = self._block(sc, app, tmp_path, nl)
        written = cfg.read_bytes()
        assert written == original + nl.encode() + block.encode("utf-8")
        doc = tomllib.loads(written.decode("utf-8"))
        assert doc["mcp_servers"]["mast"] == table
        assert "[mcp_servers.mast]" in doc["mcp_servers"]["mastodon"]["notes"]
        assert len(backups(cfg.parent)) == 1
        assert backups(cfg.parent)[0].read_bytes() == original

        assert sc.main(codex_args(app, tmp_path, "--uninstall")) == 0
        assert cfg.read_bytes() == original
        assert len(backups(cfg.parent)) == 2

    def test_an_old_entry_is_replaced_in_place_and_its_leftovers_go(self, sc, tmp_path):
        app = make_app(tmp_path / "MAST")
        cfg = tmp_path / "codex" / "config.toml"
        cfg.parent.mkdir()
        cfg.write_text(
            'model = "x"\n'
            "\n"
            "[mcp_servers.mast]\n"
            'command = "python"\n'
            'args = ["old/run_server.py"]\n'
            "supports_parallel_tool_calls = true\n"
            "\n"
            "[mcp_servers.mast.env]\n"
            'MAST_URL = "http://127.0.0.1:9999"\n'
            "\n"
            "# --- the GitHub server ---\n"
            "[mcp_servers.github]\n"
            'command = "npx"\n'
            "\n"
            "[mcp_servers.mast.tools.mast_run]\n"
            'approval_mode = "prompt"\n', encoding="utf-8")

        assert sc.main(codex_args(app, tmp_path)) == 0

        table, block = self._block(sc, app, tmp_path)
        assert cfg.read_text(encoding="utf-8") == (
            'model = "x"\n'
            "\n" + block +
            "\n"
            "# --- the GitHub server ---\n"
            "[mcp_servers.github]\n"
            'command = "npx"\n')
        doc = tomllib.loads(cfg.read_text(encoding="utf-8"))
        assert doc["mcp_servers"]["mast"] == table
        assert "supports_parallel_tool_calls" not in doc["mcp_servers"]["mast"]

    def test_running_it_twice_writes_once(self, sc, tmp_path, capsys):
        app = make_app(tmp_path / "MAST")
        cfg = tmp_path / "codex" / "config.toml"
        cfg.parent.mkdir()
        cfg.write_text(EXISTING, encoding="utf-8")
        assert sc.main(codex_args(app, tmp_path)) == 0
        first = cfg.read_bytes()
        capsys.readouterr()
        assert sc.main(codex_args(app, tmp_path)) == 0
        assert cfg.read_bytes() == first
        assert len(backups(cfg.parent)) == 1
        assert "already has" in capsys.readouterr().out

    def test_a_missing_config_is_created_without_a_backup(self, sc, tmp_path):
        app = make_app(tmp_path / "MAST")
        assert sc.main(codex_args(app, tmp_path)) == 0
        cfg = tmp_path / "codex" / "config.toml"
        _, block = self._block(sc, app, tmp_path)
        assert cfg.read_text(encoding="utf-8") == block
        assert backups(cfg.parent) == []

    def test_dry_run_writes_nothing(self, sc, tmp_path, capsys):
        app = make_app(tmp_path / "MAST")
        cfg = tmp_path / "codex" / "config.toml"
        cfg.parent.mkdir()
        cfg.write_text(EXISTING, encoding="utf-8")
        before = sorted(p.name for p in tmp_path.rglob("*"))

        assert sc.main(codex_args(app, tmp_path, "--dry-run")) == 0
        assert sc.main(codex_args(app, tmp_path, "--dry-run", "--uninstall")) == 0

        assert sorted(p.name for p in tmp_path.rglob("*")) == before
        assert cfg.read_text(encoding="utf-8") == EXISTING
        out = capsys.readouterr().out
        assert "dry run" in out and "[mcp_servers.mast]" in out

    def test_uninstall_without_an_entry_changes_nothing(self, sc, tmp_path):
        app = make_app(tmp_path / "MAST")
        cfg = tmp_path / "codex" / "config.toml"
        cfg.parent.mkdir()
        cfg.write_text(EXISTING, encoding="utf-8")
        assert sc.main(codex_args(app, tmp_path, "--uninstall")) == 0
        assert cfg.read_text(encoding="utf-8") == EXISTING
        assert backups(cfg.parent) == []

    @pytest.mark.parametrize("text", [
        '[mcp_servers]\nmast = { command = "python" }\n',          # inline table
        '[mcp_servers]\nmast.command = "python"\n',                # dotted keys
        "this is = not [valid toml\n",
    ])
    @pytest.mark.parametrize("extra", [(), ("--uninstall",)])
    def test_a_form_it_does_not_edit_is_refused_untouched(self, sc, tmp_path, text, extra):
        app = make_app(tmp_path / "MAST")
        cfg = tmp_path / "codex" / "config.toml"
        cfg.parent.mkdir()
        cfg.write_text(text, encoding="utf-8")
        assert sc.main(codex_args(app, tmp_path, *extra)) == 1
        assert cfg.read_text(encoding="utf-8") == text
        assert backups(cfg.parent) == []

    def test_never_parallel_tool_calls(self, sc, tmp_path):
        app = make_app(tmp_path / "MAST")
        for mode in ("all", "read_only"):
            table = sc.codex_table(python=app / sc.PYRUNTIME, run_server=app / sc.RUN_SERVER,
                                   workspace=tmp_path, mast_url=sc.DEFAULT_MAST_URL,
                                   actor="codex",
                                   read_only_tools=["mast_status"] if mode == "read_only" else None)
            doc = tomllib.loads(sc.render_codex_block(table))
            assert "supports_parallel_tool_calls" not in doc["mcp_servers"]["mast"]


class TestCodexEntry:
    def test_the_entry_has_the_agreed_shape(self, sc, tmp_path):
        app = make_app(tmp_path / "MAST")
        assert sc.main(codex_args(app, tmp_path)) == 0
        mast = tomllib.loads((tmp_path / "codex" / "config.toml").read_text("utf-8"))["mcp_servers"]["mast"]
        assert mast["command"] == str(app / sc.PYRUNTIME)
        assert mast["args"] == ["-I", "-X", "utf8", str(app / sc.RUN_SERVER)]
        assert mast["cwd"] == str(tmp_path / "ws")
        assert mast["startup_timeout_sec"] == 30
        assert mast["tool_timeout_sec"] == 900
        assert mast["default_tools_approval_mode"] == "approve"
        assert "tools" not in mast
        assert mast["env"] == {"MAST_URL": "http://127.0.0.1:7862", "MAST_ACTOR": "codex",
                               "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    def test_windows_paths_reach_codex_unchanged(self, sc, tmp_path):
        app = make_app(tmp_path / "MAST 安装 O'Neil")       # a quote rules out a literal string
        assert sc.main(codex_args(app, tmp_path)) == 0
        text = (tmp_path / "codex" / "config.toml").read_text("utf-8")
        mast = tomllib.loads(text)["mcp_servers"]["mast"]
        assert mast["command"] == str(app / sc.PYRUNTIME)
        assert mast["args"][-1] == str(app / sc.RUN_SERVER)

        plain = make_app(tmp_path / "MAST 安装")
        (tmp_path / "codex" / "config.toml").unlink()
        assert sc.main(codex_args(plain, tmp_path)) == 0
        text = (tmp_path / "codex" / "config.toml").read_text("utf-8")
        assert f"command = '{plain / sc.PYRUNTIME}'" in text    # readable: backslashes as they are

    def test_without_a_bundled_runtime_it_uses_this_python(self, sc, tmp_path, capsys):
        app = make_app(tmp_path / "MAST", runtime=False)
        assert sc.main(codex_args(app, tmp_path)) == 0
        mast = tomllib.loads((tmp_path / "codex" / "config.toml").read_text("utf-8"))["mcp_servers"]["mast"]
        assert mast["command"] == sys.executable
        assert "no bundled runtime" in capsys.readouterr().out

    def test_actor_and_url_are_checked(self, sc, tmp_path, capsys):
        app = make_app(tmp_path / "MAST")
        assert sc.main(codex_args(app, tmp_path, "--actor", "Codex Lab")) == 1
        assert sc.main(codex_args(app, tmp_path, "--mast-url", "ftp://x")) == 1
        assert not (tmp_path / "codex" / "config.toml").exists()
        assert sc.main(codex_args(app, tmp_path, "--mast-url", "http://192.168.1.5:7862")) == 0
        assert "MAST_ALLOW_REMOTE" in capsys.readouterr().out

    def test_an_api_key_in_the_environment_is_called_out(self, sc, tmp_path, capsys, monkeypatch):
        app = make_app(tmp_path / "MAST")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert sc.main(codex_args(app, tmp_path)) == 0
        out = capsys.readouterr().out
        assert "warning: OPENAI_API_KEY" in out
        assert "codex mcp list" in out

    def test_the_example_file_is_what_the_tool_writes(self, sc):
        from pathlib import PureWindowsPath as W

        table = sc.codex_table(python=W(r"C:\MAST\MASTv2\pyruntime\python.exe"),
                               run_server=W(r"C:\MAST\integrations\claude-code\server\run_server.py"),
                               workspace=W(r"C:\Users\<you>\Documents\MAST-agent"),
                               mast_url=sc.DEFAULT_MAST_URL, actor="codex")
        text = EXAMPLE.read_text(encoding="utf-8")
        assert sc.render_codex_block(table) in text
        assert tomllib.loads(text)["mcp_servers"]["mast"] == table


# ── --approve read_only ───────────────────────────────────────────────────────
FAKE_TOOLS = '''
from dataclasses import dataclass
from .helper import ANNOTATION          # a relative import, as in the real bridge


@dataclass
class Tool:
    name: str
    read_only: bool

    def as_mcp(self):
        return {"name": self.name, "annotations": {ANNOTATION: self.read_only}}


TOOLS = {n: Tool(n, ro) for n, ro in [("mast_status", True), ("mast_act", False),
                                       ("mast_observe", True), ("odd tool", True)]}
'''


class TestReadOnlyApproval:
    def test_exactly_the_read_only_tools_are_pre_approved(self, sc, tmp_path):
        app = make_app(tmp_path / "MAST")
        pkg = app / sc.BRIDGE_PKG
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "helper.py").write_text('ANNOTATION = "readOnlyHint"\n', encoding="utf-8")
        (pkg / "tools.py").write_text(FAKE_TOOLS, encoding="utf-8")
        mast_mcp_before = sys.modules.get("mast_mcp")

        assert sc.main(codex_args(app, tmp_path, "--approve", "read_only")) == 0

        text = (tmp_path / "codex" / "config.toml").read_text("utf-8")
        mast = tomllib.loads(text)["mcp_servers"]["mast"]
        assert mast["tools"] == {name: {"approval_mode": "approve"}
                                 for name in ("mast_observe", "mast_status", "odd tool")}
        assert "default_tools_approval_mode" not in mast
        assert '[mcp_servers.mast.tools."odd tool"]' in text
        # The bridge was imported under a private name and left no trace.
        assert not [m for m in sys.modules if m.startswith("_mast_setup_bridge_")]
        assert sys.modules.get("mast_mcp") is mast_mcp_before
        assert not list(pkg.rglob("__pycache__"))

    def test_the_real_bridge(self, sc, tmp_path):
        app = make_app(tmp_path / "MAST", bridge="real")
        names = set(sc.read_only_tools(app))
        assert {"mast_briefing", "mast_status"} <= names
        assert not names & {"mast_run", "mast_emergency_stop", "mast_handover"}

    def test_a_bridge_it_cannot_read_is_an_error_not_an_empty_list(self, sc, tmp_path):
        app = make_app(tmp_path / "MAST")
        pkg = app / sc.BRIDGE_PKG
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "tools.py").write_text("raise RuntimeError('half-edited')\n", encoding="utf-8")
        assert sc.main(codex_args(app, tmp_path, "--approve", "read_only")) == 1
        assert not (tmp_path / "codex" / "config.toml").exists()


# ── the agent workspace ───────────────────────────────────────────────────────
class TestWorkspace:
    def test_a_fresh_workspace_gets_this_versions_agents_md(self, sc, tmp_path):
        app = make_app(tmp_path / "MAST")
        assert sc.main(codex_args(app, tmp_path)) == 0
        assert (tmp_path / "ws" / "AGENTS.md").read_bytes() == AGENTS_MD.read_bytes()

    def test_an_unedited_old_copy_is_updated_without_a_backup(self, sc, tmp_path):
        app = make_app(tmp_path / "MAST")
        (app / sc.CODEX_AGENTS_MD).write_text("rules v1\n", encoding="utf-8")
        assert sc.main(codex_args(app, tmp_path)) == 0
        (app / sc.CODEX_AGENTS_MD).write_text("rules v2\n", encoding="utf-8")   # an upgrade
        assert sc.main(codex_args(app, tmp_path)) == 0
        ws = tmp_path / "ws"
        assert (ws / "AGENTS.md").read_text(encoding="utf-8") == "rules v2\n"
        assert backups(ws, "AGENTS.md") == []

    def test_an_edited_copy_is_backed_up_before_it_is_replaced(self, sc, tmp_path, capsys):
        app = make_app(tmp_path / "MAST")
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "AGENTS.md").write_text("my own rules\n", encoding="utf-8")
        assert sc.main(codex_args(app, tmp_path)) == 0
        assert (ws / "AGENTS.md").read_bytes() == AGENTS_MD.read_bytes()
        [saved] = backups(ws, "AGENTS.md")
        assert saved.read_text(encoding="utf-8") == "my own rules\n"
        assert saved.name in capsys.readouterr().out

    def test_uninstall_leaves_the_workspace(self, sc, tmp_path):
        app = make_app(tmp_path / "MAST")
        assert sc.main(codex_args(app, tmp_path)) == 0
        assert sc.main(codex_args(app, tmp_path, "--uninstall")) == 0
        assert (tmp_path / "ws" / "AGENTS.md").is_file()


# ── as the installer and the user run it ──────────────────────────────────────
def test_it_runs_isolated_on_the_standard_library(tmp_path):
    """``-I``: no PYTHONPATH, no user site, no script folder on sys.path, like MAST's
    embeddable runtime. Both subcommands, as the installer and the README run them."""
    app = make_app(tmp_path / "MAST 安装")
    run = [sys.executable, "-I", "-X", "utf8", str(SETUP)]
    r = subprocess.run(run + ["patch-plugin", "--app", str(app)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    plugin = json.loads((app / "integrations/claude-code/.claude-plugin/plugin.json").read_text("utf-8"))
    assert plugin["userConfig"]["python"]["default"] == str(app / "MASTv2/pyruntime/python.exe")

    r = subprocess.run(run + codex_args(app, tmp_path, "--dry-run"),
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    assert "[mcp_servers.mast]" in r.stdout
    assert not (tmp_path / "codex").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
