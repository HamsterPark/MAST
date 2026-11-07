r"""Connect AI clients (Claude Code, Codex) to this MAST installation.

Standard library only. It runs under MAST's bundled runtime
(``<install>\MASTv2\pyruntime\python.exe``, an embeddable CPython in isolated mode
without site-packages), under the development venv, or under any Python >= 3.11.

``patch-plugin [--app DIR]``
    Point the Claude Code plugin's ``python`` option at this installation's bundled
    runtime. The plugin ships with plain ``python``, which on Windows may open the
    Microsoft Store. The installer runs this after every install; Claude Code users then
    run ``/plugin marketplace add <install dir>`` and ``/plugin install mast@mast``.

``codex [--app DIR] [--codex-home DIR] [--mast-url URL] [--actor NAME]
        [--workspace DIR] [--approve {all,read_only}] [--dry-run] [--uninstall]``
    Write the ``[mcp_servers.mast]`` tables into Codex's ``config.toml`` (replacing the
    ones an earlier run wrote; everything else in the file stays byte for byte) and set
    up an agent workspace folder with the operating rules in ``AGENTS.md``. Codex needs
    an entry of its own: it expands neither ``${CLAUDE_PLUGIN_ROOT}`` nor
    ``${user_config.*}``, so the plugin's ``.mcp.json`` cannot be reused.

Exit status: 0 done or nothing to do, 1 refused or failed, 2 bad command line.
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import hashlib
import importlib
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent

PYRUNTIME = Path("MASTv2", "pyruntime", "python.exe")
PLUGIN_JSON = Path("integrations", "claude-code", ".claude-plugin", "plugin.json")
BRIDGE_PKG = Path("integrations", "claude-code", "server", "mast_mcp")
RUN_SERVER = Path("integrations", "claude-code", "server", "run_server.py")
CODEX_AGENTS_MD = Path("integrations", "codex", "AGENTS.md")

SERVER_ID = "mast"
DEFAULT_MAST_URL = "http://127.0.0.1:7862"
DEFAULT_ACTOR = "codex"
STARTUP_TIMEOUT_SEC = 30
#: ``mast_wait`` blocks inside MAST for up to 840 s; the client has to outlast it.
TOOL_TIMEOUT_SEC = 900
#: When one of these is set, Codex signs in with it instead of the ChatGPT account.
CODEX_KEY_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")
#: What the bridge keeps of MAST_ACTOR (it lower-cases and replaces everything else).
_ACTOR_RX = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")
_BOM = b"\xef\xbb\xbf"
#: Remembers which AGENTS.md this tool wrote, to tell an old copy from an edited one.
WORKSPACE_STAMP = ".mast-setup.json"


class SetupError(Exception):
    """Refused or failed; the message says why and what to do."""


# ── files ─────────────────────────────────────────────────────────────────────
def _app_dir(raw: str | None) -> Path:
    if not raw:
        return HERE.parent
    raw = raw.strip()
    # An installer that quotes a folder ending in a backslash ("D:\") hands us D:"
    # because the Windows command line reads \" as an escaped quote. Undo that.
    if raw.endswith('"'):
        raw = raw[:-1] + "\\"
    return Path(os.path.abspath(raw))


def _stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _backup(path: Path) -> Path:
    """Copy ``path`` to ``<name>.bak-<timestamp>`` next to it, never over an older backup."""
    base = f"{path.name}.bak-{_stamp()}"
    dst, n = path.with_name(base), 1
    while dst.exists():
        dst, n = path.with_name(f"{base}-{n}"), n + 1
    shutil.copy2(path, dst)
    return dst


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    try:
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise SetupError(f"could not replace {path} ({exc}); is it open in another program?") from exc


def _decode(raw: bytes, path: Path) -> tuple[bool, str]:
    bom = raw.startswith(_BOM)
    try:
        return bom, raw[len(_BOM) if bom else 0:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SetupError(f"{path} is not UTF-8 text ({exc}); nothing was changed") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── patch-plugin ──────────────────────────────────────────────────────────────
def _json_string_end(s: str, i: int) -> int:
    """Index just past the JSON string that opens at ``s[i] == '"'``."""
    i += 1
    while s[i] != '"':
        i += 2 if s[i] == "\\" else 1
    return i + 1


def _json_value_end(s: str, i: int) -> int:
    if s[i] == '"':
        return _json_string_end(s, i)
    if s[i] in "{[":
        depth = 0
        while True:
            c = s[i]
            if c == '"':
                i = _json_string_end(s, i)
                continue
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
    j = i
    while j < len(s) and s[j] not in ",}] \t\r\n":
        j += 1
    return j


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def json_value_span(text: str, path: tuple[str, ...]) -> tuple[int, int]:
    """``(start, end)`` of the value at ``path`` (object keys) in valid JSON ``text``.

    Editing the text in place keeps the rest of the file exactly as it was;
    ``json.dump`` would re-indent and re-escape it.
    """
    i = _skip_ws(text, 0)
    for key in path:
        if text[i] != "{":
            raise KeyError(key)
        i = _skip_ws(text, i + 1)
        while True:
            if text[i] == "}":
                raise KeyError(key)
            k_end = _json_string_end(text, i)
            name = json.loads(text[i:k_end])
            v_start = _skip_ws(text, _skip_ws(text, k_end) + 1)    # past the ':'
            v_end = _json_value_end(text, v_start)
            if name == key:
                i = v_start
                break
            i = _skip_ws(text, v_end)
            if text[i] == ",":
                i = _skip_ws(text, i + 1)
    return i, _json_value_end(text, i)


def patch_plugin(app: Path) -> int:
    path = app / PLUGIN_JSON
    python = app / PYRUNTIME
    if not path.is_file():
        raise SetupError(f"{path} not found; is {app} a MAST installation folder?")
    if not python.is_file():
        raise SetupError(f"no bundled runtime at {python}; set the plugin option 'python' "
                         "in Claude Code to a Python 3.10 or newer instead")
    raw = path.read_bytes()
    bom, text = _decode(raw, path)
    try:
        data = json.loads(text)
        current = data["userConfig"]["python"]["default"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SetupError(f"{path} has no userConfig.python.default ({exc!r}); left as it was") from exc
    want = str(python)
    if current == want:
        print(f"{path}: python already points at {want}")
        return 0
    start, end = json_value_span(text, ("userConfig", "python", "default"))
    new_text = text[:start] + json.dumps(want, ensure_ascii=False) + text[end:]
    expected = copy.deepcopy(data)
    expected["userConfig"]["python"]["default"] = want
    if json.loads(new_text) != expected:
        raise SetupError(f"editing {path} would change more than the python default; left as it was")
    _atomic_write(path, (_BOM if bom else b"") + new_text.encode("utf-8"))
    print(f"{path}: python -> {want}")
    return 0


# ── TOML text surgery ─────────────────────────────────────────────────────────
def _split_lines(text: str) -> list[str]:
    """Lines with their endings. Only ``\\n`` ends a line: ``str.splitlines`` would also
    split at characters such as U+2028 that may sit inside a TOML string."""
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _scan(line: str, ml: str | None, depth: int) -> tuple[str | None, int]:
    """Carry the multi-line state across one physical line of a key/value statement.

    ``ml`` is the open multi-line string delimiter (or None); ``depth`` counts open
    array / inline-table brackets. Strings and comments are skipped, so brackets
    inside them do not count.
    """
    i, n = 0, len(line)
    while i < n:
        if ml is not None:
            if ml == '"""' and line[i] == "\\":
                i += 2
                continue
            if line.startswith(ml, i):
                i += 3
                extra = 0                       # up to two quotes may precede the closing ones
                while i < n and line[i] == ml[0] and extra < 2:
                    i, extra = i + 1, extra + 1
                ml = None
                continue
            i += 1
            continue
        c = line[i]
        if c == "#":
            break
        if line.startswith('"""', i) or line.startswith("'''", i):
            ml, i = line[i:i + 3], i + 3
            continue
        if c == '"':
            i += 1
            while i < n and line[i] != '"':
                i += 2 if line[i] == "\\" else 1
            i += 1
            continue
        if c == "'":
            j = line.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        i += 1
    return ml, depth


def _header_path(line: str, tomllib) -> tuple[str, ...] | None:
    """Key path of a table header line, read by tomllib itself: ``[a."b.c"]`` -> ("a", "b.c")."""
    try:
        node = tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return None
    path: list[str] = []
    while isinstance(node, dict) and len(node) == 1:
        (key, node), = node.items()
        path.append(key)
        if isinstance(node, list):              # [[array.of.tables]]
            node = node[-1] if node else {}
    return tuple(path)


def classify_lines(text: str, tomllib) -> list[tuple[str, str, tuple[str, ...] | None]]:
    """``(line, kind, header path)`` per physical line; kind is header / blank / comment /
    other. Lines inside multi-line strings and arrays are "other", so ``[1, 2]`` on its
    own line in an array is never taken for a table header."""
    out = []
    ml, depth = None, 0
    for line in _split_lines(text):
        if ml is None and depth == 0:
            s = line.strip()
            if not s:
                out.append((line, "blank", None))
                continue
            if s.startswith("#"):
                out.append((line, "comment", None))
                continue
            if s.startswith("["):
                out.append((line, "header", _header_path(s, tomllib)))
                continue
        ml, depth = _scan(line, ml, depth)
        out.append((line, "other", None))
    return out


def _is_ours(path: tuple[str, ...] | None) -> bool:
    return path is not None and path[:2] == ("mcp_servers", SERVER_ID)


def merge_server_tables(text: str, block: str | None, tomllib) -> tuple[str, bool]:
    """Replace every ``[mcp_servers.mast]`` / ``[mcp_servers.mast.*]`` table in ``text``
    with ``block`` (put where the first one was, else appended), or remove them when
    ``block`` is None. Returns ``(new text, whether any table was found)``.

    Comment and blank lines just above the next table are left in place: they belong
    to whatever follows. Every other byte of ``text`` is kept.
    """
    rows = classify_lines(text, tomllib)
    lines = [r[0] for r in rows]
    heads = [i for i, r in enumerate(rows) if r[1] == "header"] + [len(rows)]
    runs: list[list[int]] = []                  # [start, end) of consecutive tables of ours
    for h, nxt in zip(heads, heads[1:]):
        if not _is_ours(rows[h][2]):
            continue
        if runs and runs[-1][1] == h:
            runs[-1][1] = nxt
        else:
            runs.append([h, nxt])
    for run in runs:
        while rows[run[1] - 1][1] in ("blank", "comment"):
            run[1] -= 1
    if not runs:
        if block is None:
            return text, False
        if not text:
            return block, False
        nl = "\r\n" if "\r\n" in text else "\n"
        return text + (nl if text.endswith("\n") else nl + nl) + block, False

    def blank(i: int) -> bool:
        return 0 <= i < len(lines) and not lines[i].strip()

    out: list[str] = []
    pos = 0
    for n, (start, end) in enumerate(runs):
        chunk = lines[pos:start]
        if n == 0 and block is not None:
            out += chunk
            out.append(block)
        else:
            # Removed for good: do not leave two blank lines where the tables were.
            if chunk and not chunk[-1].strip() and (end == len(lines) or blank(end)):
                chunk = chunk[:-1]
            out += chunk
        pos = end
    out += lines[pos:]
    return "".join(out), True


# ── the Codex entry ───────────────────────────────────────────────────────────
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_basic(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def toml_str(s: str) -> str:
    """A string with backslashes (a Windows path) as a literal string, so it reads as
    itself: ``'C:\\MAST'``; anything else, or a path holding a quote, as a basic string."""
    literal_ok = "'" not in s and not any((ord(c) < 0x20 and c != "\t") or ord(c) == 0x7F
                                          for c in s)
    return f"'{s}'" if "\\" in s and literal_ok else _toml_basic(s)


def _toml_key(k: str) -> str:
    return k if _BARE_KEY.match(k) else _toml_basic(k)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return toml_str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"no TOML rendering for {v!r}")


def codex_table(*, python: Path, run_server: Path, workspace: Path, mast_url: str,
                actor: str, read_only_tools: list[str] | None = None) -> dict:
    """What ``config.toml`` should hold under ``mcp_servers.mast`` (as tomllib reads it)."""
    table: dict = {
        "command": str(python),
        "args": ["-I", "-X", "utf8", str(run_server)],
        "cwd": str(workspace),
        "startup_timeout_sec": STARTUP_TIMEOUT_SEC,
        "tool_timeout_sec": TOOL_TIMEOUT_SEC,
    }
    if read_only_tools is None:
        table["default_tools_approval_mode"] = "approve"
    else:
        table["tools"] = {name: {"approval_mode": "approve"} for name in read_only_tools}
    table["env"] = {"MAST_URL": mast_url, "MAST_ACTOR": actor,
                    "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return table


_HEADER_NOTE = (
    "Written by MAST's integrations/setup_clients.py (codex). Running it again rewrites",
    "every [mcp_servers.mast...] table and --uninstall removes them, so whatever you add",
    "inside these tables is lost then.",
)
_KEY_NOTES = {
    "tool_timeout_sec": (
        "mast_wait blocks inside MAST for up to 840 s; 900 s lets it come back by itself.",
        "supports_parallel_tool_calls stays unset: Codex then queues MAST's write tools.",
    ),
    "default_tools_approval_mode": (
        "Every MAST tool runs without a Codex prompt. The protection is on MAST's side:",
        "its safety gates check every action, and an act is refused unless it carries",
        "the token of the latest observation. setup_clients.py codex --approve read_only",
        "pre-approves only the read-only tools instead.",
    ),
}
_READ_ONLY_NOTE = (
    "Only MAST's read-only tools are pre-approved (the tools tables below); Codex asks",
    "before any other MAST tool. Run setup_clients.py again after upgrading MAST.",
)


def render_codex_block(table: dict, nl: str = "\n") -> str:
    out = [f"[mcp_servers.{SERVER_ID}]", *("# " + c for c in _HEADER_NOTE)]
    for key in ("command", "args", "cwd", "startup_timeout_sec", "tool_timeout_sec",
                "default_tools_approval_mode"):
        if key in table:
            out += ["# " + c for c in _KEY_NOTES.get(key, ())]
            out.append(f"{key} = {_toml_value(table[key])}")
    if "tools" in table:
        out += ["# " + c for c in _READ_ONLY_NOTE]
    out += ["", f"[mcp_servers.{SERVER_ID}.env]"]
    out += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in table["env"].items()]
    for name, conf in table.get("tools", {}).items():
        out += ["", f"[mcp_servers.{SERVER_ID}.tools.{_toml_key(name)}]"]
        out += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in conf.items()]
    return nl.join(out) + nl


def read_only_tools(app: Path) -> list[str]:
    """Names of the bridge's tools annotated ``readOnlyHint: true``.

    The bridge is imported from its own folder under a private package name, so it
    never collides with another ``mast_mcp`` already imported in this process.
    """
    pkg_dir = app / BRIDGE_PKG
    if not (pkg_dir / "tools.py").is_file():
        raise SetupError(f"the MCP bridge is missing ({pkg_dir / 'tools.py'})")
    alias = "_mast_setup_bridge_" + _sha256(str(pkg_dir).encode())[:10]
    keep_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True              # no __pycache__ inside the install folder
    try:
        spec = importlib.util.spec_from_file_location(
            alias, pkg_dir / "__init__.py", submodule_search_locations=[str(pkg_dir)])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[alias] = pkg
        spec.loader.exec_module(pkg)
        tools = importlib.import_module(alias + ".tools")
        listed = [t.as_mcp() for t in tools.TOOLS.values()]
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot tell"
        raise SetupError(f"could not read the tool list from {pkg_dir} ({exc!r}); "
                         "use --approve all, or fix the bridge") from exc
    finally:
        sys.dont_write_bytecode = keep_bytecode
        for name in [m for m in sys.modules if m == alias or m.startswith(alias + ".")]:
            del sys.modules[name]
    names = sorted(t["name"] for t in listed
                   if (t.get("annotations") or {}).get("readOnlyHint") is True)
    if not names:
        raise SetupError(f"the bridge in {pkg_dir} marks no tool read-only; use --approve all")
    return names


def _without_server(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    servers = doc.get("mcp_servers")
    if isinstance(servers, dict):
        servers.pop(SERVER_ID, None)
        if not servers:
            del doc["mcp_servers"]
    return doc


def _check_merge(old_doc: dict, new_text: str, expected: dict | None, cfg: Path, tomllib) -> None:
    """The edit must parse, leave every other setting as it was, and say exactly ``expected``."""
    try:
        new_doc = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(
            f"the edited {cfg} would not be valid TOML ({exc}). It probably sets "
            f"mcp_servers.{SERVER_ID} as an inline table or with dotted keys, which this tool "
            "does not edit: remove that by hand and run it again. Nothing was changed.") from exc
    if _without_server(new_doc) != _without_server(old_doc):
        raise SetupError(f"editing {cfg} would change settings outside [mcp_servers.{SERVER_ID}]; "
                         "nothing was changed")
    if (new_doc.get("mcp_servers") or {}).get(SERVER_ID) != expected:
        raise SetupError(f"editing {cfg} did not give the intended [mcp_servers.{SERVER_ID}]; "
                         "nothing was changed")


def _bridge_python(app: Path) -> tuple[Path, bool]:
    bundled = app / PYRUNTIME
    if bundled.is_file():
        return bundled, True
    if not sys.executable:
        raise SetupError(f"no bundled runtime at {bundled}, and this Python does not know "
                         "its own path")
    return Path(sys.executable), False


def default_workspace() -> Path:
    return Path.home() / "Documents" / "MAST-agent"


def _is_loopback(host: str) -> bool:
    """The bridge's rule (``mast_mcp.config.is_loopback_host``): names are not resolved."""
    h = host.strip("[]").rstrip(".").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _check_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError as exc:
        raise SetupError(f"--mast-url {url!r} is not a valid URL ({exc})") from exc
    if parts.scheme not in ("http", "https") or not host:
        raise SetupError(f"--mast-url {url!r} is not a MAST address such as {DEFAULT_MAST_URL}")
    if not _is_loopback(host):
        print(f"note: {url} is not this machine. The bridge refuses a remote MAST until "
              f"MAST_ALLOW_REMOTE = \"true\" is in [mcp_servers.{SERVER_ID}.env] (LAN mode also "
              "needs MAST_USER and MAST_PASSWORD there); add them by hand after this run.")


def _read_stamp(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def setup_workspace(app: Path, workspace: Path) -> str:
    """Create the agent workspace with this version's AGENTS.md. An AGENTS.md that someone
    edited is backed up first; a copy this tool wrote earlier is simply updated."""
    src = app / CODEX_AGENTS_MD
    if not src.is_file():
        raise SetupError(f"{src} is missing; the MAST installation looks incomplete")
    new = src.read_bytes()
    workspace.mkdir(parents=True, exist_ok=True)
    dst = workspace / "AGENTS.md"
    stamp_path = workspace / WORKSPACE_STAMP
    stamp = _read_stamp(stamp_path)
    if not dst.is_file():
        _atomic_write(dst, new)
        message = f"wrote {dst}"
    else:
        current = dst.read_bytes()
        if current == new:
            message = f"{dst} is up to date"
        elif _sha256(current) == stamp.get("agents_md_sha256"):
            _atomic_write(dst, new)
            message = f"updated {dst} to this MAST version"
        else:
            saved = _backup(dst)
            _atomic_write(dst, new)
            message = (f"replaced the edited {dst}; your copy is {saved.name} in the same "
                       "folder: move your own additions into the new file")
    stamp["agents_md_sha256"] = _sha256(new)
    _atomic_write(stamp_path, (json.dumps(stamp, indent=2) + "\n").encode("utf-8"))
    return message


def setup_codex(args) -> int:
    try:
        import tomllib
    except ModuleNotFoundError as exc:          # Python 3.10 and older
        raise SetupError("Python 3.11 or newer is needed (tomllib); run this with MAST's "
                         r"bundled runtime, <install>\MASTv2\pyruntime\python.exe") from exc
    app = _app_dir(args.app)
    home = Path(os.path.abspath(args.codex_home or os.environ.get("CODEX_HOME")
                                or Path.home() / ".codex"))
    cfg = home / "config.toml"
    raw = cfg.read_bytes() if cfg.is_file() else b""
    bom, old_text = _decode(raw, cfg)
    try:
        old_doc = tomllib.loads(old_text)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"{cfg} is not valid TOML ({exc}); fix it first. Nothing was changed.") from exc
    nl = "\r\n" if "\r\n" in old_text else "\n"
    prefix = _BOM if bom else b""

    if args.uninstall:
        new_text, found = merge_server_tables(old_text, None, tomllib)
        if not found:
            if SERVER_ID in (old_doc.get("mcp_servers") or {}):
                raise SetupError(f"{cfg} sets mcp_servers.{SERVER_ID} as an inline table or with "
                                 "dotted keys, which this tool does not edit; remove it by hand")
            print(f"no [mcp_servers.{SERVER_ID}] table in {cfg}; nothing to remove")
            return 0
        _check_merge(old_doc, new_text, None, cfg, tomllib)
        if args.dry_run:
            print(f"dry run: would remove the [mcp_servers.{SERVER_ID}] tables from {cfg}")
            return 0
        saved = _backup(cfg)
        _atomic_write(cfg, prefix + new_text.encode("utf-8"))
        print(f"removed the [mcp_servers.{SERVER_ID}] tables from {cfg} (backup: {saved.name})")
        print("the agent workspace and its AGENTS.md were left in place")
        return 0

    if not _ACTOR_RX.match(args.actor):
        raise SetupError(f"--actor {args.actor!r}: use lower-case letters, digits, '.', '-' "
                         "and '_' (MAST records actions under this name)")
    _check_url(args.mast_url)
    run_server = app / RUN_SERVER
    if not run_server.is_file():
        raise SetupError(f"the MCP bridge is missing ({run_server}); is {app} a MAST "
                         "installation folder?")
    if not (app / CODEX_AGENTS_MD).is_file():
        raise SetupError(f"{app / CODEX_AGENTS_MD} is missing; the MAST installation looks incomplete")
    python, bundled = _bridge_python(app)
    workspace = Path(os.path.abspath(args.workspace)) if args.workspace else default_workspace()
    tools = read_only_tools(app) if args.approve == "read_only" else None
    table = codex_table(python=python, run_server=run_server, workspace=workspace,
                        mast_url=args.mast_url, actor=args.actor, read_only_tools=tools)
    block = render_codex_block(table, nl)
    new_text, replaced = merge_server_tables(old_text, block, tomllib)
    _check_merge(old_doc, new_text, table, cfg, tomllib)

    if not bundled:
        print(f"note: no bundled runtime at {app / PYRUNTIME}; the server will run on {python}")
    if args.dry_run:
        verb = "replace the" if replaced else "add these"
        print(f"dry run: would {verb} [mcp_servers.{SERVER_ID}] tables in {cfg}:\n")
        print(block.replace("\r\n", "\n"))
        print(f"and set up the workspace {workspace} with AGENTS.md. Nothing was written.")
        return 0

    workspace_msg = setup_workspace(app, workspace)
    if new_text == old_text:
        config_msg = f"{cfg} already has this [mcp_servers.{SERVER_ID}]"
    else:
        home.mkdir(parents=True, exist_ok=True)
        saved = _backup(cfg) if cfg.is_file() else None
        _atomic_write(cfg, prefix + new_text.encode("utf-8"))
        config_msg = (f"{'updated' if replaced else 'added'} [mcp_servers.{SERVER_ID}] in {cfg}"
                      + (f" (backup: {saved.name})" if saved else ""))
    print(config_msg)
    print(workspace_msg)
    print("\nNext:\n"
          f"  1. Start MAST on this PC (its service answers at {args.mast_url}).\n"
          "  2. Check that Codex sees the server:  codex mcp list   (\"mast\" is listed)\n"
          f"  3. Open the workspace in Codex: the Codex app (open folder {workspace})\n"
          f"     or  codex -C \"{workspace}\"\n"
          "  4. Codex must be signed in with ChatGPT (codex login), not with an API key.")
    for var in CODEX_KEY_VARS:
        if os.environ.get(var, "").strip():
            print(f"warning: {var} is set in this environment; Codex then uses that key instead "
                  "of the ChatGPT sign-in. Remove it from your environment variables.")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")   # a path the console cannot show
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        prog="setup_clients.py",
        description="Connect Claude Code and Codex to this MAST installation.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("patch-plugin",
                       help="point the Claude Code plugin's python option at the bundled runtime")
    p.add_argument("--app", help="MAST installation folder (default: the folder above this script)")
    c = sub.add_parser("codex", help="add MAST to Codex's config.toml and set up a workspace")
    c.add_argument("--app", help="MAST installation folder (default: the folder above this script)")
    c.add_argument("--codex-home", help="Codex's folder (default: CODEX_HOME, else ~/.codex)")
    c.add_argument("--mast-url", default=DEFAULT_MAST_URL, help=f"default {DEFAULT_MAST_URL}")
    c.add_argument("--actor", default=DEFAULT_ACTOR, help=f"name in MAST's records (default {DEFAULT_ACTOR})")
    c.add_argument("--workspace", help="agent workspace folder (default ~/Documents/MAST-agent)")
    c.add_argument("--approve", choices=("all", "read_only"), default="all",
                   help="pre-approve every MAST tool (default) or only the read-only ones")
    c.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    c.add_argument("--uninstall", action="store_true", help="remove the [mcp_servers.mast] tables")
    args = parser.parse_args(argv)
    try:
        if args.command == "patch-plugin":
            return patch_plugin(_app_dir(args.app))
        return setup_codex(args)
    except SetupError as exc:
        print(f"setup_clients.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
