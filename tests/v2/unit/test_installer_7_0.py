r"""7.0 打包：VISTA 进冻结包、客户端集成随安装目录分发、构建脚本的两道新闸门。

这里的东西在开发环境里全都「看起来没问题」，问题只在构建机或仪器电脑上出现：

* ``mast.vista`` 只被按需 import —— 漏进 spec 不会报错，``/vista/*`` 在打包版里一调用就失败；
* 客户端集成不进 ``dist\MAST`` —— 安装包照样能装，组员第一次连仪器时才发现插件是空的；
* ``api key\`` 里的 LAN 私钥随包发出 —— 每台仪器电脑共用同一把 TLS 私钥，没有任何症状。

构建脚本在没有 PyInstaller 的机器上整份跑不起来，所以两个新步骤写成了函数：这里用
PowerShell 自己的解析器把它们**原样**从脚本里取出来，对着临时目录真跑（pwsh 7 与
Windows PowerShell 5.1 各一遍，有哪个跑哪个），再用同一棵语法树核对**调用点**：
函数对了而没人调用、或者调用了却不在失败时 ``exit 1``，同样是坏的。
"""
from __future__ import annotations

import ast
import ctypes
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SPEC = REPO / "mast2.spec"
BUILD = REPO / "installer" / "mast2_build.ps1"
ISS = REPO / "installer" / "mast2_setup.iss"
VISTA = REPO / "MASTv2" / "mast" / "vista"


# ── mast2.spec ────────────────────────────────────────────────────────────────
def spec_lists(text: str) -> tuple[list[str], list[str]]:
    """``(collect_submodules 的那张名单, 直接写进 hiddenimports 的名单)``，从语法树取。

    不用字符串包含：名字出现在注释里、或者出现在别的列表里，都不等于被收集。
    """
    collected: list[str] = []
    direct: list[str] = []
    for node in ast.walk(ast.parse(text)):
        if (isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple)
                and "collect_submodules(sub)" in ast.unparse(node)):
            collected += [e.value for e in node.iter.elts if isinstance(e, ast.Constant)]
        if (isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name)
                and node.target.id == "mast_hidden" and isinstance(node.value, ast.List)):
            direct += [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
    return collected, direct


class TestSpec:
    def test_vista_and_the_ext_gateway_are_collected(self):
        collected, _ = spec_lists(SPEC.read_text(encoding="utf-8"))
        assert "mast.vista" in collected
        assert "mast.api.ext" in collected

    def test_vista_and_its_runner_are_named_directly(self):
        """直接写进 hiddenimports 的名字不要求构建机上能 import 它。"""
        _, direct = spec_lists(SPEC.read_text(encoding="utf-8"))
        assert {"mast.vista", "mast.vista.runner"} <= set(direct)

    def test_the_extraction_sees_the_whole_lists(self):
        """反向自检：取不到名单的检查和全部通过的检查长得一样。"""
        collected, direct = spec_lists(SPEC.read_text(encoding="utf-8"))
        assert len(collected) >= 40
        assert {"mast.agentruntime", "mast.gallery", "mast.api"} <= set(collected)
        assert {"mast._buildinfo", "mast.core.nanonis_patch"} <= set(direct)

    def test_a_name_in_a_comment_does_not_count(self):
        text = SPEC.read_text(encoding="utf-8")
        mutated = text.replace('    "mast.vista",\n', '    # "mast.vista",\n')
        mutated = mutated.replace('    "mast.vista",\r\n', '    # "mast.vista",\r\n')
        assert mutated != text
        collected, direct = spec_lists(mutated)
        assert "mast.vista" not in collected and "mast.vista" not in direct

    def test_the_runner_hook_ships_as_a_file_next_to_paths_py(self):
        """The runner hands ``compact_hook.py`` to Claude Code as a **script**; frozen, it
        looks for it beside ``mast/vista/runner/paths.py`` (``resolve_hook_script``) and
        refuses to start without it. Being compiled into the PYZ is not enough."""
        tree = ast.parse(SPEC.read_text(encoding="utf-8"))
        entries, optional = [], set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
                if any(isinstance(t, ast.Name) and t.id == "data_files" for t in node.targets):
                    entries += [ast.unparse(e) for e in node.value.elts]
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Set)
                    and any(isinstance(t, ast.Name) and t.id == "_OPTIONAL_DATA" for t in node.targets)):
                optional = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
        assert any("skill_zh.json" in e for e in entries), "反向自检：没取到 data_files"
        src = "os.path.join(MASTV2_ROOT, 'mast', 'vista', 'runner', 'compact_hook.py')"
        dst = "os.path.join('mast', 'vista', 'runner')"
        assert any(src in e and dst in e for e in entries), entries
        assert "compact_hook.py" not in optional
        # The destination is the folder of paths.py, which is where the runner looks.
        assert (VISTA / "runner" / "compact_hook.py").is_file()
        assert (VISTA / "runner" / "paths.py").is_file()

    def test_vista_ships_no_data_file_the_spec_does_not_name(self):
        """collect_submodules 只收 .py。VISTA 将来带的非 .py 资源要在 spec 里登记 datas。"""
        files = [p for p in VISTA.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        assert sum(p.suffix == ".py" for p in files) >= 5, f"扫描口径坏了：{VISTA}"
        spec = SPEC.read_text(encoding="utf-8")
        unregistered = sorted(p.relative_to(VISTA).as_posix() for p in files
                              if p.suffix not in (".py", ".pyc") and p.name not in spec)
        assert unregistered == [], (
            f"mast/vista 里有 spec 不知道的数据文件：{unregistered}。冻结版里它们不存在 —— "
            "在 mast2.spec 的 data_files 里登记。")

    def test_every_data_file_in_mast_ships_next_to_its_module(self):
        """mast/ 下的非 .py 文件都按 ``Path(__file__)`` 去旁边读（skill_zh.json、位点标注页
        模板……）。PyInstaller 只把 .py 编进 PYZ，所以每一个都要在 data_files 里登记，而且
        目标目录得是它所在的包目录，否则冻结版里那个路径上什么都没有。

        曾查出 site_marking_template.html 没登记：源码运行时标注页生成正常，打包版里
        PrepareSiteMarkingPage 会读模板失败。上面那条只管 mast/vista，这条管整个包。
        """
        root = REPO / "MASTv2"
        files = sorted(p for p in (root / "mast").rglob("*")
                       if p.is_file() and "__pycache__" not in p.parts
                       and p.suffix not in (".py", ".pyc"))
        assert len(files) >= 2, f"扫描口径坏了：{files}"
        entries = []
        for node in ast.walk(ast.parse(SPEC.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.List)
                    and any(isinstance(t, ast.Name) and t.id == "data_files" for t in node.targets)):
                entries += [ast.unparse(e) for e in node.value.elts]
        assert any("skill_zh.json" in e for e in entries), "反向自检：没取到 data_files"
        missing = []
        for p in files:
            parts = p.relative_to(root).parts
            src = "os.path.join(MASTV2_ROOT, " + ", ".join(map(repr, parts)) + ")"
            dst = "os.path.join(" + ", ".join(map(repr, parts[:-1])) + ")"
            if not any(src in e and dst in e for e in entries):
                missing.append(p.relative_to(root).as_posix())
        assert missing == [], (
            f"这些文件没有登记到它所在的包目录：{missing}。冻结版里它们不存在 —— 在 mast2.spec "
            "的 data_files 里加 (os.path.join(MASTV2_ROOT, <路径>), os.path.join(<包目录>))。")


# ── mast2_build.ps1, run by PowerShell itself ────────────────────────────────
SHELLS = [s for s in ("pwsh", "powershell") if shutil.which(s)]

HARNESS = r"""
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$src = [IO.File]::ReadAllText('__BUILD__', [Text.Encoding]::UTF8)
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errors)
$L = 'System.Management.Automation.Language'
$out = [ordered]@{}
$out['parse_errors'] = @($errors | ForEach-Object { $_.Message })

$fns = @{}
foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    $fns[$f.Name] = $f
}
$out['copy_fn_text'] = $fns['Copy-ClientIntegrations'].Extent.Text

function Test-InFunction($node) {
    $p = $node.Parent
    while ($p) {
        if ($p -is [System.Management.Automation.Language.FunctionDefinitionAst]) { return $true }
        $p = $p.Parent
    }
    return $false
}
$calls = @()
foreach ($c in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
    if (Test-InFunction $c) { continue }
    $assigned = ''
    $p = $c.Parent
    while ($p) {
        if ($p -is [System.Management.Automation.Language.AssignmentStatementAst]) { $assigned = $p.Left.Extent.Text; break }
        $p = $p.Parent
    }
    $calls += [ordered]@{ name = [string]$c.GetCommandName(); text = $c.Extent.Text;
                          offset = $c.Extent.StartOffset; assigned = $assigned }
}
$out['calls'] = $calls

$ifs = @()
foreach ($i in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.IfStatementAst] }, $true)) {
    foreach ($clause in $i.Clauses) {
        $exits = @($clause.Item2.FindAll({ param($n) $n -is [System.Management.Automation.Language.ExitStatementAst] }, $true) | ForEach-Object { $_.Extent.Text })
        $ifs += [ordered]@{ cond = $clause.Item1.Extent.Text; exits = $exits }
    }
}
$out['ifs'] = $ifs

$ver = @($ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'Version' })
$out['version_found'] = $ver.Count
$out['version_has_default'] = [bool]($ver | Where-Object { $_.DefaultValue })
$out['version_attributes'] = @($ver | ForEach-Object { $_.Attributes } | ForEach-Object { $_.Extent.Text })

# The two functions exactly as the script has them.
. ([ScriptBlock]::Create($fns['Copy-ClientIntegrations'].Extent.Text))
. ([ScriptBlock]::Create($fns['Get-ApiKeyStowaways'].Extent.Text))
$copy = [ordered]@{}
foreach ($case in @(__CASES__)) {
    $copy[$case[0]] = @(Copy-ClientIntegrations -RepoRoot $case[1] -DistRoot $case[2] 6>$null)
}
$out['copy'] = $copy
$keys = [ordered]@{}
$keys['strict'] = @(Get-ApiKeyStowaways -KeyDir '__KEYS__' | ForEach-Object { $_.Name } | Sort-Object)
$keys['keep_dev'] = @(Get-ApiKeyStowaways -KeyDir '__KEYS__' -AllowDevEnv | ForEach-Object { $_.Name } | Sort-Object)
$keys['absent'] = @(Get-ApiKeyStowaways -KeyDir '__KEYS__\does-not-exist')
$out['keys'] = $keys
ConvertTo-Json -InputObject $out -Depth 6 -Compress
"""

REQUIRED = [
    ".claude-plugin/marketplace.json",
    "integrations/claude-code/.claude-plugin/plugin.json",
    "integrations/claude-code/.mcp.json",
    "integrations/claude-code/server/run_server.py",
    "integrations/claude-code/server/mast_mcp/tools.py",
    "integrations/claude-code/skills/mast-operator/SKILL.md",
    "integrations/codex/AGENTS.md",
    "integrations/setup_clients.py",
    "docs/external/en/README.md",
    "docs/external/zh/README.md",
]


def _ps_literal(path: Path | str) -> str:
    return str(path).replace("'", "''")


def _fake_repo(root: Path, *, with_run_server: bool = True) -> Path:
    for rel in REQUIRED:
        if rel.endswith("run_server.py") and not with_run_server:
            continue
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {rel}\n", encoding="utf-8")
    junk = ["integrations/claude-code/server/mast_mcp/__pycache__/tools.cpython-313.pyc",
            "integrations/claude-code/server/.pytest_cache/v/cache",
            "integrations/claude-code/server/stray.pyc",
            "integrations/codex/.mast-fetch/frame.sxm",
            "integrations/claude-code/not-shipped.txt"]
    for rel in junk:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"junk")
    return root


def _set_hidden(path: Path) -> None:
    if os.name == "nt":
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)   # FILE_ATTRIBUTE_HIDDEN


@pytest.fixture(scope="module", params=SHELLS or ["(none)"])
def harness(request, tmp_path_factory):
    shell = request.param
    if shell == "(none)":
        pytest.skip("no PowerShell on this machine")
    tmp = tmp_path_factory.mktemp(f"ps-{shell}")
    fake = _fake_repo(tmp / "fake")
    broken = _fake_repo(tmp / "broken", with_run_server=False)
    stale_dist = tmp / "dist-fake"
    for rel in ["integrations/claude-code/server/mast_mcp/old_module.py",
                ".claude-plugin/stale.json", "docs/external/old.md", "MAST.exe"]:
        p = stale_dist / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"old")
    keys = tmp / "api key"
    (keys / "sub").mkdir(parents=True)
    for rel in ["lan_key.pem", "lan_cert.pem", "kimi.env", "sub/deep.env", "hidden.pem"]:
        (keys / rel).write_bytes(b"secret")
    _set_hidden(keys / "hidden.pem")

    cases = [("real", REPO, tmp / "dist-real"), ("fake", fake, stale_dist),
             ("broken", broken, tmp / "dist-broken")]
    script = (HARNESS.replace("__BUILD__", _ps_literal(BUILD))
              .replace("__KEYS__", _ps_literal(keys))
              .replace("__CASES__", ", ".join(
                  f"@('{n}', '{_ps_literal(r)}', '{_ps_literal(d)}')" for n, r, d in cases)))
    ps1 = tmp / "harness.ps1"
    ps1.write_text(script, encoding="utf-8-sig")       # BOM: 5.1 reads it as UTF-8 too
    proc = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(ps1)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)
    assert proc.returncode == 0, f"{shell} harness failed:\n{proc.stdout}\n{proc.stderr}"
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    return {"shell": shell, "tmp": tmp, "data": data, "cases": {n: d for n, _, d in cases}}


def _calls(data, name=None, contains=None):
    return [c for c in data["calls"]
            if (name is None or c["name"] == name) and (contains is None or contains in c["text"])]


def _tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


class TestBuildScript:
    def test_it_parses(self, harness):
        assert harness["data"]["parse_errors"] == []

    def test_version_is_mandatory_without_a_default(self, harness):
        d = harness["data"]
        assert d["version_found"] == 1
        assert d["version_has_default"] is False
        assert any("Mandatory" in a for a in d["version_attributes"]), d["version_attributes"]

    def test_the_copy_runs_after_pyinstaller_and_before_zip_and_installer(self, harness):
        d = harness["data"]
        [call] = _calls(d, "Copy-ClientIntegrations")
        [pyi] = _calls(d, contains="-m PyInstaller")
        zips = _calls(d, "Compress-Archive")
        [iscc] = _calls(d, contains="mast2_setup.iss")
        assert zips, "反向自检：找不到 Compress-Archive，调用点核对的口径坏了"
        assert pyi["offset"] < call["offset"] < min(z["offset"] for z in zips) < iscc["offset"]
        assert call["assigned"] == "$ciMissing"
        assert any(i["cond"] == "$ciMissing.Count -gt 0" and "exit 1" in i["exits"]
                   for i in d["ifs"]), "缺必需文件时构建必须失败"

    def test_the_key_gate_runs_after_the_env_cleanup_and_fails_the_build(self, harness):
        d = harness["data"]
        [gate] = _calls(d, "Get-ApiKeyStowaways")
        [cleanup] = _calls(d, contains="-Filter *.env")
        zips = _calls(d, "Compress-Archive")
        assert cleanup["offset"] < gate["offset"] < min(z["offset"] for z in zips)
        assert gate["assigned"] == "$keyStowaways"
        assert "-AllowDevEnv:$KeepDevKeys" in gate["text"]
        assert any(i["cond"] == "$keyStowaways.Count -gt 0" and "exit 1" in i["exits"]
                   for i in d["ifs"])

    def test_the_real_integrations_are_copied_byte_for_byte(self, harness):
        assert harness["data"]["copy"]["real"] == []
        dist = harness["cases"]["real"]
        for rel in REQUIRED:
            assert (dist / rel).read_bytes() == (REPO / rel).read_bytes(), rel
        shipped = _tree(dist)
        assert not [p for p in shipped if "__pycache__" in p or p.endswith(".pyc")]
        # 反向自检：整棵 server/ 和 skills/ 真的拷过去了，不只是点名的那几个
        assert len([p for p in shipped if p.startswith("integrations/claude-code/server/")]) >= 7
        assert len([p for p in shipped if p.startswith("integrations/claude-code/skills/")]) >= 10

    def test_junk_and_stale_files_do_not_ship(self, harness):
        assert harness["data"]["copy"]["fake"] == []
        shipped = _tree(harness["cases"]["fake"])
        assert set(REQUIRED) <= shipped
        assert shipped - set(REQUIRED) == {"MAST.exe"}, shipped - set(REQUIRED)

    def test_a_missing_run_server_is_reported(self, harness):
        missing = harness["data"]["copy"]["broken"]
        assert missing == ["integrations\\claude-code\\server\\run_server.py"]

    def test_every_file_left_in_api_key_counts(self, harness):
        keys = harness["data"]["keys"]
        assert keys["strict"] == sorted(["deep.env", "hidden.pem", "kimi.env",
                                         "lan_cert.pem", "lan_key.pem"])
        # -KeepDevKeys 只放行顶层的 .env；子目录里的 .env 和任何证书照样拦
        assert keys["keep_dev"] == sorted(["deep.env", "hidden.pem", "lan_cert.pem", "lan_key.pem"])
        assert keys["absent"] == []

    def test_the_shipped_tree_works_on_its_own(self, harness, tmp_path):
        """Run setup_clients.py from inside the copied tree with its default ``--app``, the
        way the installer's [Run] entry and the README run it: it must find the plugin, the
        bridge and AGENTS.md at their installed places."""
        app = tmp_path / "MAST"
        shutil.copytree(harness["cases"]["real"], app)
        runtime = app / "MASTv2" / "pyruntime" / "python.exe"
        runtime.parent.mkdir(parents=True)
        runtime.write_bytes(b"")
        script = app / "integrations" / "setup_clients.py"
        run = [sys.executable, "-I", "-X", "utf8", str(script)]

        r = subprocess.run(run + ["patch-plugin"], capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        assert r.returncode == 0, r.stderr
        plugin = json.loads((app / "integrations/claude-code/.claude-plugin/plugin.json")
                            .read_text(encoding="utf-8"))
        assert plugin["userConfig"]["python"]["default"] == str(runtime)

        r = subprocess.run(run + ["codex", "--dry-run", "--approve", "read_only",
                                  "--codex-home", str(tmp_path / "codex"),
                                  "--workspace", str(tmp_path / "ws")],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert r.returncode == 0, r.stderr
        assert "[mcp_servers.mast.tools.mast_status]" in r.stdout
        assert str(app / "integrations" / "claude-code" / "server" / "run_server.py") in r.stdout

    def test_the_installer_cleans_every_tree_the_build_copies(self, harness):
        items = re.findall(r'"([^"]+)"', harness["data"]["copy_fn_text"].split("$required")[0])
        assert "integrations\\claude-code\\server" in items, f"反向自检：没取到拷贝清单 {items}"
        deleted = {e.get("Name") for e in iss_entries("InstallDelete")}
        for rel in items:
            top = "docs\\external" if rel.startswith("docs\\external") else rel.split("\\")[0]
            assert "{app}\\" + top in deleted, f"{rel}: 升级时旧文件会留在安装目录里"


# ── mast2_setup.iss ───────────────────────────────────────────────────────────
def _split_params(line: str) -> list[str]:
    parts, buf, quoted = [], [], False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        if ch == ";" and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('""', '"')
    return value


def iss_entries(section: str) -> list[dict[str, str]]:
    text = ISS.read_text(encoding="utf-8")
    m = re.search(rf"^\[{section}\]\s*$(.*?)(?=^\[\w+\]\s*$)", text, re.M | re.S)
    assert m, f"[{section}] not found"
    entries = []
    for line in m.group(1).splitlines():
        s = line.strip()
        if not s or s.startswith(";"):
            continue
        entry = {}
        for part in _split_params(s):
            key, _, value = part.partition(":")
            entry[key.strip()] = _unquote(value)
        entries.append(entry)
    return entries


def _argv(cmdline: str) -> list[str]:
    """How Windows itself splits the command line Inno builds (CommandLineToArgvW)."""
    from ctypes import wintypes

    split = ctypes.windll.shell32.CommandLineToArgvW
    split.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    split.restype = ctypes.POINTER(wintypes.LPWSTR)
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes = [ctypes.c_void_p]
    n = ctypes.c_int()
    argv = split("python.exe " + cmdline, ctypes.byref(n))
    try:
        return [argv[i] for i in range(1, n.value)]
    finally:
        free(ctypes.cast(argv, ctypes.c_void_p))


class TestInnoScript:
    def test_upgrades_clear_the_integration_trees(self):
        entries = iss_entries("InstallDelete")
        names = {e.get("Name"): e.get("Type") for e in entries}
        assert names.get("{app}\\_internal\\mast\\_src") == "filesandordirs", "反向自检：解析坏了"
        for tree in ("{app}\\integrations", "{app}\\.claude-plugin", "{app}\\docs\\external"):
            assert names.get(tree) == "filesandordirs", tree

    def test_the_catch_all_still_ships_them(self):
        [catch_all] = [e for e in iss_entries("Files") if e.get("Source") == "..\\dist\\MAST\\*"]
        assert catch_all.get("DestDir") == "{app}"
        assert "recursesubdirs" in catch_all.get("Flags", "")
        excludes = [x.strip().lower() for x in catch_all.get("Excludes", "").split(",") if x.strip()]
        assert not [x for x in excludes
                    if x.startswith(("integrations", ".claude-plugin", "docs", "*"))], excludes

    def _patch_entry(self) -> dict[str, str]:
        [entry] = [e for e in iss_entries("Run") if "patch-plugin" in e.get("Parameters", "")]
        return entry

    def test_the_plugin_is_patched_with_the_bundled_runtime_when_present(self):
        entry = self._patch_entry()
        assert entry["Filename"] == "{app}\\MASTv2\\pyruntime\\python.exe"
        assert entry.get("Check") == "PyRuntimePresent"
        flags = entry.get("Flags", "").split()
        assert "runhidden" in flags and "waituntilterminated" in flags
        assert "postinstall" not in flags          # runs as part of the install, unasked
        code = ISS.read_text(encoding="utf-8").split("[Code]", 1)[1]
        m = re.search(r"function PyRuntimePresent\(\): Boolean;(.*?)\bend;", code, re.S)
        assert m, "the Check function is missing"
        assert "pyruntime\\python.exe" in m.group(1) and "setup_clients.py" in m.group(1)

    def test_every_question_is_suppressible_with_a_cautious_silent_default(self):
        """A plain MsgBox is not suppressed by /SUPPRESSMSGBOXES: an unattended install or
        uninstall waits on it, invisibly, forever. The two questions that do damage when
        answered "yes" unattended (force-close a running MAST, uninstall a legacy product)
        must default to "no"."""
        code = ISS.read_text(encoding="utf-8").split("[Code]", 1)[1]
        assert not re.findall(r"(?<!Suppressible)MsgBox\(", code)
        assert len(re.findall(r"SuppressibleMsgBox\(", code)) >= 5, "反向自检：一个都没找到"
        for head in ("function PrepareToInstall(", "procedure OfferRemoveLegacy("):
            m = re.search(re.escape(head) + r".*?\nend;", code, re.S)
            assert m, head
            assert "MB_YESNO, IDNO)" in m.group(0), f"{head}: the silent default must be no"

    def test_the_data_dir_can_be_given_on_the_command_line(self):
        code = ISS.read_text(encoding="utf-8").split("[Code]", 1)[1]
        assert "ExpandConstant('{param:DATADIR|{sd}\\MAST-data}')" in code
        m = re.search(r"procedure CurPageChanged\(.*?\nend;", code, re.S)
        assert m and "{param:DATADIR|}" in m.group(0), "an explicit /DATADIR must win over data_dir.txt"

    @pytest.mark.skipif(os.name != "nt", reason="CommandLineToArgvW is Windows only")
    @pytest.mark.parametrize("app", ["C:\\Program Files\\MAST 测试", "C:\\MAST", "D:\\"])
    def test_the_parameters_reach_python_as_separate_arguments(self, app):
        params = self._patch_entry()["Parameters"].replace("{app}", app)
        argv = _argv(params)
        assert argv[:3] == ["-I", "-X", "utf8"]
        assert argv[4:6] == ["patch-plugin", "--app"]
        assert len(argv) == 7, argv
        # A folder ending in a backslash comes through as D:" ; setup_clients.py undoes that.
        spec = importlib.util.spec_from_file_location(
            "setup_clients_for_iss", REPO / "integrations" / "setup_clients.py")
        sc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sc)
        assert sc._app_dir(argv[6]) == Path(app)
        assert Path(argv[3]) == Path(app) / "integrations" / "setup_clients.py"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
