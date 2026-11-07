"""PyInstaller entry point for the MAST desktop bundle.

The Tk desktop launcher: a thin Python control panel that boots/stops the
**FastAPI service** (which serves the typed API + the bundled TypeScript React
SPA) and opens the browser to it. The launcher itself stays Python/Tk — it is
the PyInstaller entry that bootstraps the whole Python core (FastAPI + torch +
LangGraph + nanonis_spm); the TS rewrite converted the in-browser UI, not this
window. (Gradio was fully removed in the TS rewrite.)

Runs in two modes:

  Default (no flags) — **Launcher mode**: opens a Tk control panel with
    - Start / Restart / Stop service buttons
    - GUI URLs (one-click open in the browser)
    - API key configuration (Kimi / DeepSeek / Alibaba DashScope / Anthropic / ...)
    - Status indicators (key present, Nanonis connected, service running)
    - Scrolling log of the service subprocess
    - UI language selector (English default; Chinese / Japanese / German /
      French / Spanish / Czech; persisted in ``config/launcher_lang.txt``;
      applied immediately without restarting the launcher)

  ``--service-mode``: skip the launcher window and boot the FastAPI service
    directly. The launcher Popen()'s the same exe with this flag to run the
    actual MAST service. In dev mode you can also call::

        .venv-v2-py313/Scripts/python.exe mast2_launcher.py --service-mode

Boot order in service mode (do not reorder):
    1. Resolve and chdir to the user-data root.
    2. Apply ``mast.core.platform_patch`` BEFORE heavy imports (Py3.14 +
       ``platform._wmi_query()`` hang).
    3. Make sure ``experiments/``, ``working-sessions/``, ``api key/``,
       ``artifacts/`` exist next to the exe so first-run is friction-free.
    4. Build the live FastAPI app (``mast.api.app.create_app`` +
       ``mast.api.bootstrap.build_live_context``) and serve it with uvicorn
       (HTTP/WS/SSE + the bundled ``frontend/dist`` SPA at ``/``).

This module is imported as the PyInstaller entry. It also runs fine in dev
(``.venv-v2-py313/Scripts/python.exe mast2_launcher.py``) which keeps the boot
path the same in both environments.
"""

from __future__ import annotations

import argparse
import datetime
import itertools
import json
import logging
import math
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable

# In dev mode (not frozen), prepend MASTv2/ so `import mast` finds the v2
# package instead of the v1 one at the repo root. PyInstaller's spec
# handles this for the frozen build.
if not getattr(sys, "frozen", False):
    _repo = Path(__file__).resolve().parent
    _v2_root = _repo / "MASTv2"
    if _v2_root.exists():
        sys.path.insert(0, str(_v2_root))

# Module logger. _graceful_stop_proc falls back to it when the caller passes no
# ``log`` callback; without this name that fallback raised NameError, which the
# surrounding ``except`` swallowed, so the stop steps were never logged.
logger = logging.getLogger("mast2_launcher")

# ── Per-provider key files (matches mast.config) ──────────────────────
PROVIDER_KEY_FILES: dict[str, str] = {
    "moonshot":  "kimi.env",       # Moonshot Kimi
    "deepseek":  "deepseek.env",
    "dashscope": "dashscope.env",  # Alibaba Bailian (speech / embeddings / fallback LLM)
    "anthropic": "api key.env",    # Claude — legacy filename
    "minimax":   "minimax.env",    # MiniMax (Anthropic-compatible endpoint)
    "zhipu":     "glm.env",        # Zhipu GLM
}

PROVIDER_LABELS: dict[str, str] = {
    "moonshot":  "Kimi (Moonshot)",
    "deepseek":  "DeepSeek",
    "dashscope": "Alibaba Bailian (speech/embeddings)",
    "anthropic": "Anthropic Claude",
    "minimax":   "MiniMax",
    "zhipu":     "Zhipu GLM",
}

PROVIDER_ENV_VARS: dict[str, tuple[str, ...]] = {
    "moonshot":  ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
    "deepseek":  ("DEEPSEEK_API_KEY",),
    "dashscope": ("DASHSCOPE_API_KEY", "ALIYUN_BAILIAN_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "minimax":   ("MINIMAX_API_KEY",),
    "zhipu":     ("ZHIPU_API_KEY", "GLM_API_KEY"),
}

# Nanonis main port from mast.config.NanonisConfig.port_main
NANONIS_HOST = "127.0.0.1"
NANONIS_PORT = 6501

# Gradio server — MAST2 uses 7862 / 7863 / 8766 to coexist with v1 on the
# same machine (v1 uses 7860/7861/8765). MASTConfig.server.server_port for v2
# remains 7860 by default (single-app dev workflow), but the launcher in
# bundled mode forces MAST2_LAUNCHER_PORT into the subprocess env so the
# service binds 7862.
GUI_HOST = "127.0.0.1"
GUI_PORT = 7862

# Push update server (intranet)
PUSH_SERVER_PORT = 8766

# Process-name signatures that suggest the Nanonis Mimea simulator (rather
# than a real V5e controller).  Compared case-insensitively against the
# names returned by `tasklist`.
_NANONIS_SIMULATOR_PROCESS_HINTS: tuple[str, ...] = (
    "MimeaV5e.exe",
    "Mimea_V5e.exe",
    "NanonisMimea.exe",
    "MimeaSimulator.exe",
    "Mimea-V5e.exe",
)

# Logo (used both for Tk window icon and PyInstaller exe icon). PNG path is
# what Tk's iconphoto() needs; ICO is what mast.spec / Inno Setup consume.
LOGO_PNG_RELPATH = Path("logo") / "MAST2_logo.png"
LOGO_ICO_RELPATH = Path("logo") / "MAST2_logo.ico"

# LAN access uses an auth file under `api key/` so it's preserved across
# upgrades (same as LLM provider keys). First non-comment line is the
# username, second non-comment line is the password.
LAN_AUTH_FILENAME = "lan_auth.env"

# Env vars passed from the launcher to its service / admin subprocesses to
# enable LAN binding + HTTP basic auth at the Gradio layer.
#
# SECURITY (v2.1.14): the LAN password must NOT travel through the child's
# environment block. On Windows a process's environment is readable by any
# other process running as the same user (Process Explorer, WMI
# Win32_Process, etc.) and lingers for the full lifetime of the long-running
# Gradio service. Instead we hand the child a *path* to a short-lived,
# owner-only-readable file (LAN_ENV_AUTH_FILE) holding `user\npassword`. The
# child reads it once at boot and immediately deletes it, so the exposure
# window is milliseconds and never touches the persistent env block.
#
# LAN_ENV_USER (the username — not a secret) and the legacy LAN_ENV_PASS are
# retained only so a *new* launcher can still drive an *old* child build; new
# launchers no longer populate LAN_ENV_PASS.
LAN_ENV_ENABLE    = "MAST2_LAUNCHER_LAN"
LAN_ENV_USER      = "MAST2_LAUNCHER_AUTH_USER"
LAN_ENV_PASS      = "MAST2_LAUNCHER_AUTH_PASS"        # legacy / back-compat only
LAN_ENV_AUTH_FILE = "MAST2_LAUNCHER_AUTH_FILE"        # path to ephemeral creds file

# Subdir (under user_root) holding ephemeral LAN-auth handoff files. Created
# owner-only where the OS supports it; swept clean on launcher start/exit.
LAN_AUTH_HANDOFF_DIR = ".mast2_lan_auth"

# Single-instance lock: <user_root>/.mast2_launcher.pid stores the active
# launcher PID. On startup, second instance finds existing PID, brings the
# original window forward via SendMessage WM_USER, then exits.
LAUNCHER_PID_FILE = ".mast2_launcher.pid"
LAUNCHER_PORT_FILE = ".mast2_launcher.port"     # win32 listener port for IPC
LAUNCHER_BRING_FOREGROUND_PORT_RANGE = (47860, 47880)

# Startup-with-Windows registry key (HKCU\Software\Microsoft\Windows\CurrentVersion\Run\MAST2)
STARTUP_REG_NAME = "MAST"

# Re-detect Tailscale this often. It may connect after the launcher started, and
# switching the account on a shared PC changes its 100.x address, so a URL
# detected once at start would point at nobody.
TAILSCALE_REDETECT_MS = 60_000


# ── UI language (English default; several optional languages) ─────────
# Choice lives in <user_root>/config/launcher_lang.txt. Changing it in the
# launcher rebuilds the widget tree immediately. Only user-visible text goes
# through _tr(); values written to files / env / the backend never do.
# Call sites pass zh+en; ja/de/fr/es/cs come from launcher_i18n_catalog.py
# (keyed by the English string) unless the call supplies an explicit alt.
LANG_FILENAME = "launcher_lang.txt"
_SUPPORTED_LANGS = ("en", "zh", "ja", "de", "fr", "es", "cs")
_LANG_LABELS: dict[str, str] = {
    "en": "English",
    "zh": "中文",
    "ja": "日本語",
    "de": "Deutsch",
    "fr": "Français",
    "es": "Español",
    "cs": "Čeština",
}
_LANG = "en"

# Public download / release notes + feedback (routine upgrades for operators).
PUBLIC_RELEASES_URL = "https://github.com/HamsterPark/MAST/releases"
PUBLIC_ISSUES_URL = "https://github.com/HamsterPark/MAST/issues"
GITHUB_RELEASES_API = "https://api.github.com/repos/HamsterPark/MAST/releases/latest"
# Automatic poll interval; manual "Check now" still has a short cooldown.
GITHUB_RELEASE_CHECK_MIN_S = 12 * 3600
GITHUB_RELEASE_CHECK_MANUAL_MIN_S = 60
GITHUB_RELEASE_CACHE_NAME = "github_release_check.json"

# Written by setup-ai-ssh.ps1 so the launcher can keep showing the two lines
# meant for Claude Code / Codex after the elevated window is closed.
REMOTE_AI_HANDOFF_NAME = "ai-remote.handoff.json"


def _load_tr_catalog() -> dict[str, dict[str, str]]:
    """ja/de/fr/es/cs strings keyed by English. Empty dict if unavailable."""
    try:
        from launcher_i18n_catalog import CATALOG as _cat
        return _cat if isinstance(_cat, dict) else {}
    except Exception:
        pass
    # Dev / tests: load the sibling file next to this launcher even if the
    # repo root is not on sys.path.
    try:
        import importlib.util as _ilu
        sib = Path(__file__).resolve().parent / "launcher_i18n_catalog.py"
        if not sib.is_file():
            return {}
        spec = _ilu.spec_from_file_location("launcher_i18n_catalog", sib)
        if spec is None or spec.loader is None:
            return {}
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cat = getattr(mod, "CATALOG", None)
        return cat if isinstance(cat, dict) else {}
    except Exception:
        return {}


_TR_CATALOG: dict[str, dict[str, str]] = _load_tr_catalog()


def _github_release_cache_path(root: Path) -> Path:
    return root / "config" / GITHUB_RELEASE_CACHE_NAME


def _load_github_release_cache(root: Path) -> dict:
    try:
        data = json.loads(_github_release_cache_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_github_release_cache(root: Path, data: dict) -> None:
    try:
        p = _github_release_cache_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        print(f"[launcher] could not save {GITHUB_RELEASE_CACHE_NAME}: {exc}")


def _normalize_release_version(tag: str) -> str:
    """Strip a leading ``v`` / ``V`` from a GitHub release tag."""
    s = (tag or "").strip()
    if len(s) >= 2 and s[0] in "vV" and s[1].isdigit():
        return s[1:]
    return s


def _version_tuple(ver: str) -> tuple:
    """Best-effort numeric tuple for ordering (non-numeric bits ignored)."""
    parts: list[int] = []
    for chunk in _normalize_release_version(ver).replace("-", ".").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            parts.append(int(digits))
        else:
            break
    return tuple(parts)


def _is_remote_version_newer(remote: str, local: str) -> bool:
    """True only when both sides parse as version numbers and the remote one is greater.

    A release tag that is not a version (``nightly``) or an unknown local version never counts
    as an update; the Updates row then shows the tag instead of claiming anything.
    """
    rt, lt = _version_tuple(remote), _version_tuple(local)
    if not rt or not lt:
        return False
    return rt > lt


def _should_check_github_release(cache: dict, *, now: float, force: bool = False) -> bool:
    """True when an automatic or manual poll is allowed."""
    last = cache.get("checked_at")
    try:
        last_f = float(last)
    except (TypeError, ValueError):
        return True
    # Only a check that really happened in the past counts as recent. A time in the future (the
    # clock was set back, or the cache is corrupt) or a non-finite one proves nothing, and
    # would block every automatic and manual check until the clock caught up.
    if not math.isfinite(last_f) or last_f > now:
        return True
    gap = GITHUB_RELEASE_CHECK_MANUAL_MIN_S if force else GITHUB_RELEASE_CHECK_MIN_S
    return (now - last_f) >= gap


def _fetch_latest_github_release(*, etag: str = "", timeout: float = 12.0) -> dict:
    """GET GitHub /releases/latest. Returns keys status (ok|not_modified|error), …

    On ok: tag, html_url, etag. On not_modified: etag only. On error: error text.
    """
    import urllib.error
    import urllib.request

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "MAST-launcher",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(GITHUB_RELEASES_API, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            new_etag = resp.headers.get("ETag") or etag
            tag = str(body.get("tag_name") or "").strip()
            html = str(body.get("html_url") or PUBLIC_RELEASES_URL).strip()
            return {
                "status": "ok",
                "tag": tag,
                "version": _normalize_release_version(tag),
                "html_url": html or PUBLIC_RELEASES_URL,
                "etag": new_etag,
            }
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return {"status": "not_modified", "etag": etag}
        return {"status": "error", "error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _tr(zh: str, en: str, **alts: str) -> str:
    """Return the string for the active UI language.

    *zh* and *en* are required. Optional keyword extras (``ja=``, ``de=``,
    ``fr=``, ``es=``, ``cs=``, …) override the catalog. Missing languages
    are filled from ``launcher_i18n_catalog.CATALOG`` (by English key);
    English remains the final fallback so every string stays readable.
    """
    table: dict[str, str] = {"zh": zh, "en": en}
    for key, value in alts.items():
        if value:
            table[key] = value
    for key, value in (_TR_CATALOG.get(en) or {}).items():
        if key not in table and value:
            table[key] = value
    return table.get(_LANG) or table["en"]


_UNKNOWN_GPU_NAME = "unknown"


def _lang_path(root: Path) -> Path:
    return root / "config" / LANG_FILENAME


def _read_lang_file(root: Path) -> str:
    """The persisted language ("en" on a missing, unreadable or unknown value)."""
    try:
        value = _lang_path(root).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return "en"
    return value if value in _SUPPORTED_LANGS else "en"


def _mast_lang_env(lang: str) -> str:
    """``mast.i18n`` only knows en/zh; map other launcher langs to English."""
    return lang if lang in ("en", "zh") else "en"


def _load_lang(root: Path) -> str:
    """Read the persisted language into the module state."""
    global _LANG
    _LANG = _read_lang_file(root)
    # mast.* code imported into the launcher (e.g. the Tailscale hint) speaks the
    # process language (mast/i18n.py); keep it the same as the launcher's.
    os.environ["MAST_LANG"] = _mast_lang_env(_LANG)
    return _LANG


def _save_lang(root: Path, lang: str) -> None:
    """Persist the language choice (best effort)."""
    if lang not in _SUPPORTED_LANGS:
        return
    try:
        p = _lang_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(lang + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"[launcher] could not save {LANG_FILENAME}: {exc}")


def _set_lang(lang: str, root: Path | None = None) -> str:
    """Set the process language (and optionally persist it). Returns the active code."""
    global _LANG
    if lang not in _SUPPORTED_LANGS:
        return _LANG
    _LANG = lang
    os.environ["MAST_LANG"] = _mast_lang_env(lang)
    if root is not None:
        _save_lang(root, lang)
    return _LANG


def _remote_ai_handoff_path() -> Path:
    """Where setup-ai-ssh.ps1 leaves the Claude Code / Codex paste lines."""
    return Path.home() / ".ssh" / REMOTE_AI_HANDOFF_NAME


def _load_remote_ai_handoff() -> dict:
    """Read the SSH handoff file; empty dict if missing/corrupt.

    Read as ``utf-8-sig``: setup-ai-ssh.ps1 runs under Windows PowerShell 5.1 (the
    launcher starts ``powershell.exe``), whose ``Set-Content -Encoding UTF8`` writes a
    BOM, and ``json.loads`` rejects a text that starts with one. Plain ``utf-8`` made
    every file the script really writes read as "corrupt" (-> ``{}``).
    """
    p = _remote_ai_handoff_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _rewrite_remote_ai_handoff_host(new_v4: str) -> bool:
    """Update host/prompt_line in ai-remote.handoff.json when Tailscale IP changes.

    Returns True when the file was rewritten. Missing/corrupt handoff, empty
    *new_v4*, or an already-matching host → False (no write). Other fields
    (key path, user, handover note, …) are preserved. Matches the prompt_line
    template written by ``setup-ai-ssh.ps1``.
    """
    new_v4 = (new_v4 or "").strip()
    if not new_v4:
        return False
    p = _remote_ai_handoff_path()
    try:
        # utf-8-sig: the PowerShell 5.1 writer puts a BOM in front (see _load_remote_ai_handoff).
        # The rewrite below writes plain UTF-8 without one.
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    old = str(data.get("host") or "").strip()
    if old == new_v4:
        return False
    computer = str(data.get("computer") or "").strip() or "computer"
    user = str(data.get("user") or "").strip() or "<user>"
    try:
        port = int(data.get("port", 22))
    except (TypeError, ValueError):
        port = 22
    key_path = str(data.get("key_path") or "").strip()
    key_for_line = key_path if key_path else "<the private key file>"
    data["host"] = new_v4
    data["prompt_line"] = (
        f"Control my computer {computer} over SSH:  "
        f"ssh -i {key_for_line} -p {port} {user}@{new_v4}"
    )
    data["updated_at"] = datetime.datetime.now().isoformat()
    try:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(p)
    except OSError:
        return False
    return True


# ── Network helpers ─────────────────────────────────────────────────

def _lan_ip() -> str:
    """Return the host's primary outbound IPv4 address (or '' if offline).

    Uses a UDP socket trick so we don't require any actual network round-trip.
    Filters out 127.* loopback addresses.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        try:
            # Doesn't matter that 8.8.8.8 isn't reachable — we just want the
            # interface the OS would route through to it.
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    # Fallback: gethostbyname_ex
    try:
        host = socket.gethostname()
        for ip in socket.gethostbyname_ex(host)[2]:
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return ""


def _tailscale_change_note(prev, cur) -> str:
    """Launcher log line when Tailscale changed between two detections, '' if
    nothing worth saying (first detection, or same state)."""
    if prev is None or cur is None:
        return ""

    def v4(st) -> str:
        return ", ".join(ip for ip in (getattr(st, "self_ips", None) or []) if ":" not in ip) or "—"

    was, now = bool(getattr(prev, "ready", False)), bool(getattr(cur, "ready", False))
    net = getattr(cur, "tailnet", "") or "?"
    if not was and now:
        return _tr(f"Tailscale 已连接：{v4(cur)}（网络 {net}）。",
                   f"Tailscale connected: {v4(cur)} (network {net}).")
    if was and not now:
        _why = getattr(cur, 'hint', '') or getattr(cur, 'backend_state', '')
        return _tr(f"Tailscale 已断开：{_why or '未就绪'}",
                   f"Tailscale disconnected: {_why or 'not ready'}")
    if was and now and (list(getattr(prev, "self_ips", []) or []) != list(getattr(cur, "self_ips", []) or [])
                        or getattr(prev, "tailnet", "") != getattr(cur, "tailnet", "")):
        return _tr(
            f"Tailscale 地址变了：{v4(prev)} → {v4(cur)}（网络 {net}）—— 远程地址已更新；"
            "服务的证书在下次启动时补上新地址，私钥和指纹不变。",
            f"Tailscale address changed: {v4(prev)} -> {v4(cur)} (network {net}) - "
            "the remote URL was updated; the service certificate picks up the new "
            "address at its next start (private key and fingerprint unchanged).")
    return ""


# ── Remote AI: Claude Code / Codex on another computer, over Tailscale ──
# The server is mast_mcp.remote_server (integrations/claude-code/server, bundled into
# MAST.exe by mast2.spec). The launcher hosts it, so it outlives restarts of the main
# service and can start that service for an AI that cannot click this window.
# Its key, port and name are kept in config/remote_ai.json; the two switches below in
# config/remote_ai_prefs.json.
REMOTE_AI_SETTINGS = "remote_ai.json"
REMOTE_AI_PREFS = "remote_ai_prefs.json"

# What the launcher log says for each kind of remote AI activity ({d}: the detail).
_REMOTE_AI_ACTIVITY = {
    "connected": ("已连接（{d}）", "connected ({d})"),
    "badkey": ("用错误或过期的密钥尝试连接", "tried to connect with a wrong or old key"),
    "mast": ("调用了 {d}", "called {d}"),
    "info": ("查看了系统信息", "looked at the system information"),
    "run": ("执行了命令：{d}", "ran a command: {d}"),
    "check": ("查看了命令 #{d} 的进度", "checked on command #{d}"),
    "stop": ("停止了命令 #{d}", "stopped command #{d}"),
    "read": ("读取了文件 {d}", "read the file {d}"),
    "write": ("写入了文件 {d}", "wrote the file {d}"),
    "edit": ("修改了文件 {d}", "changed the file {d}"),
    "list": ("查看了文件夹 {d}", "looked into the folder {d}"),
    "screenshot": ("截取了屏幕画面", "took a screenshot"),
    "refused": ("想用 {d}，但没有允许命令、文件和截屏",
                "tried {d}, but commands, files and screenshots are not allowed"),
    "guarded": ("想运行的命令被拦下（它会停掉 Nanonis、LabVIEW 或 MAST）：{d}",
                "was stopped from running a command that would end Nanonis, LabVIEW or MAST: {d}"),
}
_REMOTE_AI_SERVICE_ACTIONS = {
    "start": ("启动了主服务", "started the main service"),
    "restart": ("重启了主服务", "restarted the main service"),
    "stop": ("关闭了主服务", "stopped the main service"),
}


def _remote_ai_activity_text(kind: str, detail: str) -> str:
    if kind == "service":
        zh, en = _REMOTE_AI_SERVICE_ACTIONS.get(detail, (f"对主服务执行了 {detail}",
                                                         f"asked the main service to {detail}"))
        return _tr(zh, en)
    zh, en = _REMOTE_AI_ACTIVITY.get(kind, ("{d}", "{d}"))
    return _tr(zh, en).replace("{d}", detail or kind)


def _remote_ai_prefs_path(root: Path) -> Path:
    return root / "config" / REMOTE_AI_PREFS


def _load_remote_ai_prefs(root: Path) -> dict:
    """``{"enabled": bool, "computer": bool}``; both off unless switched on before."""
    prefs = {"enabled": False, "computer": False}
    try:
        data = json.loads(_remote_ai_prefs_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return prefs
    if isinstance(data, dict):
        for key in prefs:
            if isinstance(data.get(key), bool):
                prefs[key] = data[key]
    return prefs


def _save_remote_ai_prefs(root: Path, prefs: dict) -> None:
    try:
        p = _remote_ai_prefs_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"enabled": bool(prefs.get("enabled")),
                                 "computer": bool(prefs.get("computer"))}) + "\n",
                     encoding="utf-8")
    except OSError as exc:
        print(f"[launcher] could not save {REMOTE_AI_PREFS}: {exc}")


def _app_dir() -> Path:
    """The installation folder (the checkout when not frozen)."""
    return Path(sys.executable).resolve().parent if _frozen() else Path(__file__).resolve().parent


def _remote_ai_module():
    """``mast_mcp.remote_server``: bundled into MAST.exe; from the checkout otherwise."""
    if not _frozen():
        server = _app_dir() / "integrations" / "claude-code" / "server"
        if server.is_dir() and str(server) not in sys.path:
            sys.path.insert(0, str(server))
    from mast_mcp import remote_server
    return remote_server


def _tailscale_v4(status) -> str:
    """This computer's Tailscale IPv4 address when Tailscale is ready, else ''."""
    if status is None or not getattr(status, "ready", False):
        return ""
    return next((ip for ip in (getattr(status, "self_ips", None) or []) if ":" not in ip), "")


def _tailscale_machine(status) -> str:
    """The name the Tailscale apps show for this computer (MagicDNS label, else host name)."""
    magic = getattr(status, "magic_dns", "") or ""
    return magic.split(".", 1)[0] if magic else (getattr(status, "device_name", "") or "")


def _tailscale_peer_names() -> dict:
    from mast.net.tailscale import peer_names
    return peer_names()


class _RemoteService:
    """What ``mast_service`` may do: the launcher's service buttons, without the dialogs.

    Called on the remote AI server's threads; anything touching Tk runs on the Tk
    thread (``win.after``) while the caller waits. Each call ends within ~45 s, below
    the 55 s an HTTP client waits for an answer.
    """

    BUDGET_S = 45.0

    def __init__(self, app: "LauncherApp"):
        self.app = app

    def status(self) -> dict:
        proc = self.app.proc
        owned = proc is not None and proc.poll() is None
        up = gui_running()
        return {"running": up, "starting": owned and not up, "managed": owned}

    def _on_tk(self, fn, timeout: float):
        done = threading.Event()
        box: dict = {}

        def run():
            try:
                box["value"] = fn()
            except Exception as exc:  # noqa: BLE001 - handed back to the caller
                box["error"] = exc
            finally:
                done.set()

        self.app.win.after(0, run)
        if not done.wait(timeout):
            raise TimeoutError("the launcher did not get to it in time")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    @staticmethod
    def _wait_up(deadline: float) -> bool:
        while time.monotonic() < deadline:
            if gui_running():
                return True
            time.sleep(1.0)
        return gui_running()

    def _external(self) -> bool:
        st = self.status()
        return st["running"] and not st["managed"]

    def start(self) -> str:
        deadline = time.monotonic() + self.BUDGET_S
        if gui_running():
            return "MAST's main service was already running."

        def go():
            app = self.app
            if app.proc is not None and app.proc.poll() is None:
                return
            app._log(_tr("远程 AI 正在启动主服务。", "The remote AI is starting the main service."))
            app._do_start_service()      # no load-warning dialog: nobody is there to click it

        self._on_tk(go, 10.0)
        if self._wait_up(deadline):
            return "Started MAST's main service."
        return ("MAST's main service is starting; ask for action status again in "
                "half a minute.")

    def restart(self) -> str:
        if self._external():
            return ("MAST's main service was started outside this launcher, so it cannot be "
                    "restarted from here; ask the operator (mast_ask_operator).")
        deadline = time.monotonic() + self.BUDGET_S
        self._on_tk(self.app.restart_service, 30.0)
        if self._wait_up(deadline):
            return "Restarted MAST's main service."
        return ("MAST's main service was stopped and is starting again; ask for action status "
                "again in half a minute.")

    def stop(self) -> str:
        if self._external():
            return ("MAST's main service was started outside this launcher, so it cannot be "
                    "stopped from here; ask the operator (mast_ask_operator).")
        if not gui_running() and not self.status()["starting"]:
            return "MAST's main service was not running."
        self._on_tk(self.app.stop_service, 30.0)
        return ("Stopped MAST's main service." if not gui_running() else
                "MAST's main service did not stop; ask the operator (mast_ask_operator).")


# ── Single-instance lock (PID file + local IPC port) ──────────────────

def _pid_alive(pid: int) -> bool:
    """Check if a process with this PID is still alive (Windows-correct).

    NOTE: `os.kill(pid, 0)` does NOT work as a probe on Windows — it raises
    WinError 87 even for live processes that we lack TERMINATE rights on.
    The correct method is OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) +
    GetExitCodeProcess; STILL_ACTIVE (259) ⇒ alive.
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_uint()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _read_launcher_lock(user_root: Path) -> tuple[int, int] | None:
    """Return (pid, ipc_port) from <user_root>/.mast2_launcher.pid or None."""
    f = user_root / LAUNCHER_PID_FILE
    if not f.exists():
        return None
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
        pid = int(lines[0].strip())
        port = int(lines[1].strip()) if len(lines) > 1 else 0
    except (OSError, ValueError, IndexError):
        return None
    if not _pid_alive(pid):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return (pid, port)


def _write_launcher_lock(user_root: Path, ipc_port: int) -> None:
    f = user_root / LAUNCHER_PID_FILE
    try:
        f.write_text(f"{os.getpid()}\n{ipc_port}\n", encoding="utf-8")
    except OSError as exc:
        print(f"[launcher] could not write lock {f}: {exc}")


def _clear_launcher_lock(user_root: Path) -> None:
    f = user_root / LAUNCHER_PID_FILE
    try:
        f.unlink(missing_ok=True)
    except OSError:
        pass


def _bind_ipc_port() -> tuple[socket.socket, int] | None:
    """Bind a TCP socket on a free port in BRING_FOREGROUND_PORT_RANGE.

    Returns (socket, port) or None if no port is free. The socket listens on
    127.0.0.1; second instance sends "BRING_FOREGROUND\n" to ask the first
    to surface its window.
    """
    lo, hi = LAUNCHER_BRING_FOREGROUND_PORT_RANGE
    for port in range(lo, hi):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            s.listen(4)
            s.setblocking(False)
            return (s, port)
        except OSError:
            continue
    return None


def _send_bring_foreground(port: int) -> bool:
    if port <= 0:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0) as c:
            c.sendall(b"BRING_FOREGROUND\n")
        return True
    except OSError:
        return False


# ── Startup-with-Windows toggle (HKCU registry) ─────────────────────────

def _startup_enabled() -> bool:
    """Check if MAST2 launcher is registered for HKCU Run."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_READ,
        )
        try:
            value, _ = winreg.QueryValueEx(key, STARTUP_REG_NAME)
            return bool(value)
        finally:
            winreg.CloseKey(key)
    except (FileNotFoundError, OSError):
        return False


def _startup_set(enabled: bool) -> bool:
    """Toggle startup with Windows. Returns True on success."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        try:
            if enabled:
                exe = sys.executable if _frozen() else f'"{sys.executable}" "{Path(__file__).resolve()}"'
                # Quote the path to handle spaces
                if _frozen():
                    exe = f'"{exe}"'
                # Boot-time launch starts MINIMIZED to the tray so it doesn't pop
                # a window on every login (resolves the auto-start ↔ ×-to-tray
                # interaction). The flag is consumed in main() / LauncherApp.
                winreg.SetValueEx(key, STARTUP_REG_NAME, 0, winreg.REG_SZ,
                                  exe + " --minimized")
            else:
                try:
                    winreg.DeleteValue(key, STARTUP_REG_NAME)
                except FileNotFoundError:
                    pass
            return True
        finally:
            winreg.CloseKey(key)
    except OSError as exc:
        print(f"[launcher] startup toggle failed: {exc}")
        return False


def _detect_nanonis_simulator() -> bool:
    """Return True if a Mimea-simulator process appears to be running locally.

    Uses Windows `tasklist /FO CSV /NH` so we don't pull in psutil. Returns
    False on any failure (caller treats unknown == real / external).
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return False
    if out.returncode != 0:
        return False
    text = out.stdout.lower()
    for hint in _NANONIS_SIMULATOR_PROCESS_HINTS:
        if hint.lower() in text:
            return True
    return False


# ── GPU detection (for the vision-model start warning) ──────────────────
# These helpers shell out (nvidia-smi / PowerShell WMI) so they must NEVER be
# called on the Tk mainloop — the caller runs them on a background thread. They
# never raise: any failure returns the "unknown" shape so the dialog can still
# render an honest "no GPU detected" message.

def _detect_gpu() -> dict:
    """Best-effort GPU probe WITHOUT importing torch.

    Returns a dict ``{"vendor","name","vram_gb","driver","source"}``.
    Tries ``nvidia-smi`` first (authoritative for NVIDIA: exact VRAM + driver),
    then falls back to Windows WMI (Win32_VideoController) which works for any
    adapter but caps AdapterRAM at ~4 GiB (uint32). On total failure returns the
    "unknown" shape. Swallows ALL exceptions — must never raise.
    """
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # 1. nvidia-smi — exact for NVIDIA cards.
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6,
            creationflags=no_window,
        )
        if out.returncode == 0 and out.stdout.strip():
            first = out.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first.split(",")]
            if len(parts) >= 3:
                name = parts[0] or "NVIDIA GPU"
                try:
                    mib = float(parts[1])
                    vram_gb = round(mib / 1024.0, 1)
                except (ValueError, TypeError):
                    vram_gb = 0.0
                driver = parts[2]
                return {
                    "vendor": "NVIDIA", "name": name, "vram_gb": vram_gb,
                    "driver": driver, "source": "nvidia-smi",
                }
    except Exception:
        pass

    # 2. WMI fallback (any adapter, but VRAM is uint32-capped at ~4 GiB).
    try:
        ps = (
            "Get-CimInstance Win32_VideoController | "
            "Select-Object -First 1 Name,AdapterRAM,DriverVersion | "
            "ForEach-Object { \"$($_.Name)|$($_.AdapterRAM)|$($_.DriverVersion)\" }"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=8,
            creationflags=no_window,
        )
        if out.returncode == 0 and out.stdout.strip():
            line = out.stdout.strip().splitlines()[0]
            bits = line.split("|")
            name = (bits[0].strip() if len(bits) > 0 else "") or _UNKNOWN_GPU_NAME
            ram_raw = bits[1].strip() if len(bits) > 1 else ""
            driver = bits[2].strip() if len(bits) > 2 else ""
            lname = name.lower()
            if "nvidia" in lname:
                vendor = "NVIDIA"
            elif "amd" in lname or "radeon" in lname:
                vendor = "AMD"
            elif "intel" in lname:
                vendor = "Intel"
            else:
                vendor = "unknown"
            try:
                ram_bytes = float(ram_raw) if ram_raw else 0.0
                gib = round(ram_bytes / (1024.0 ** 3), 1)
            except (ValueError, TypeError):
                gib = 0.0
            # AdapterRAM is a uint32 → anything ≥ ~4 GiB reports as ~4.0 and is
            # not trustworthy; signal "unknown" with the >=4 sentinel.
            vram_gb = -1.0 if gib >= 4.0 else gib
            return {
                "vendor": vendor, "name": name, "vram_gb": vram_gb,
                "driver": driver, "source": "wmi",
            }
    except Exception:
        pass

    # 3. Nothing worked.
    return {"vendor": "unknown", "name": _UNKNOWN_GPU_NAME, "vram_gb": 0.0,
            "driver": "", "source": "none"}


def _vision_support(gpu: dict) -> tuple[str, str]:
    """Map a ``_detect_gpu()`` result to (level, message).

    level ∈ {"ok","marginal","need_driver","unsupported"}. Message is a ready-to
    -display sentence in the active UI language. Never raises.
    """
    try:
        vendor = str(gpu.get("vendor", "unknown"))
        name = str(gpu.get("name", _UNKNOWN_GPU_NAME)) or _UNKNOWN_GPU_NAME
        driver = str(gpu.get("driver", "") or "")
        source = str(gpu.get("source", "none"))
        try:
            vram_gb = float(gpu.get("vram_gb", 0.0))
        except (ValueError, TypeError):
            vram_gb = 0.0

        # Non-NVIDIA (or nothing detected) → CPU/Mock fallback only.
        if vendor != "NVIDIA" or source == "none":
            return ("unsupported", _tr(
                f"未检测到 NVIDIA CUDA 显卡（{name}）。视觉模型将以 CPU/Mock "
                "回退，速度很慢、能力受限。建议保持关闭。",
                f"No NVIDIA CUDA GPU detected ({name}). The vision model will fall "
                "back to CPU/Mock, which is slow and limited."))

        # VRAM unknown (WMI uint32 cap) — can't size it precisely.
        if vram_gb < 0:
            return ("marginal", _tr(
                f"检测到 {name}，但无法精确读取显存/驱动（非 NVIDIA 工具）。"
                "若为较新的 NVIDIA 显卡通常可用；如加载失败会自动回退 Mock。",
                f"Detected {name}, but VRAM/driver could not be read precisely "
                "(non-NVIDIA tool). A recent NVIDIA card normally works; on a "
                "load failure it falls back to Mock automatically."))

        # Driver too old for CUDA 12.x. Parse leading int of e.g. "527.41".
        major = None
        try:
            m = "".join(ch for ch in driver.split(".")[0] if ch.isdigit())
            if m:
                major = int(m)
        except (ValueError, TypeError, IndexError):
            major = None
        if major is not None and major < 528:
            return ("need_driver", _tr(
                f"驱动过旧（当前 {driver}）。本项目 CUDA 12.x 需要 NVIDIA "
                "驱动 ≥ 528（建议 ≥ 555）。请到 NVIDIA 官网下载并自行更新驱动"
                "后再开启视觉模型。",
                f"Driver too old (current {driver}). CUDA 12.x needs NVIDIA driver "
                ">= 528 (>= 555 recommended). Update the driver from the NVIDIA "
                "website before enabling the vision model."))

        # Driver OK → size on VRAM.
        if vram_gb >= 6:
            return ("ok", _tr(
                f"支持 ✓  {name} · 显存 {vram_gb} GB · 驱动 {driver}。"
                "可加载完整 DINOv3(M12) 视觉模型。",
                f"Supported ✓  {name} · VRAM {vram_gb} GB · driver {driver}. "
                "The full DINOv3 (M12) vision model can be loaded."))
        if vram_gb >= 4:
            return ("marginal", _tr(
                f"可用但显存偏紧（{vram_gb} GB）。建议使用 legacy 后端或小批量；"
                "DINOv3 主干可能接近显存上限。",
                f"Usable but VRAM is tight ({vram_gb} GB). Prefer the legacy "
                "backend or small batches; the DINOv3 backbone may approach the "
                "VRAM limit."))
        if vram_gb > 0:
            return ("unsupported", _tr(
                f"显存不足（{vram_gb} GB < 4 GB），无法稳定加载视觉模型，"
                "将回退 CPU/Mock。",
                f"Insufficient VRAM ({vram_gb} GB < 4 GB); the vision model cannot "
                "load reliably and will fall back to CPU/Mock."))
        # vram == 0 but NVIDIA detected (rare) — treat as unknown/marginal.
        return ("marginal", _tr(
            f"检测到 {name}，但无法读取显存大小。若为较新的 NVIDIA 显卡通常"
            "可用；如加载失败会自动回退 Mock。",
            f"Detected {name}, but the VRAM size could not be read. A recent "
            "NVIDIA card normally works; on a load failure it falls back to "
            "Mock automatically."))
    except Exception:
        return ("unsupported", _tr(
            "显卡检测异常，视觉模型可能回退 CPU/Mock。建议保持关闭。",
            "GPU detection failed; the vision model may fall back to CPU/Mock."))


# ── Vision on/off persistence (config/vision.json) ────────────────────
# The launcher remembers the user's choice across runs. DEFAULT True preserves
# the historical behaviour (the service always pre-warmed the vision model).

def _vision_config_path(root: Path) -> Path:
    return root / "config" / "vision.json"


def _legacy_features_enabled_from_disk(root: Path) -> bool:
    """Read ``config/ui_settings.json`` → ``legacy_features_enabled`` (default False).

    Used by the launcher before spawning the service so we can skip the
    "about to load the vision model" warning when vision is gated off.
    Mirrors ``mast.webui.settings_store.legacy_features_enabled`` without
    importing the service stack into the Tk process. Any read failure → off.
    """
    p = root / "config" / "ui_settings.json"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return bool(raw.get("legacy_features_enabled"))
    except Exception:  # noqa: BLE001 — missing/corrupt → default off
        pass
    return False


def _parse_vision_load_marker(line: str) -> tuple[str, str] | None:
    """Parse a service log line for vision warm-up markers.

    Returns ``(kind, detail)`` where *kind* is one of
    ``start`` / ``done`` / ``failed`` / ``skipped``, or ``None`` if the line
    is not a marker. *detail* is the remainder after the marker token
    (e.g. ``\"mock\"``, ``\"disabled\"``, or an error message).
    """
    if not line:
        return None
    for token, kind in (
        ("MAST_VISION_LOAD_START", "start"),
        ("MAST_VISION_LOAD_DONE", "done"),
        ("MAST_VISION_LOAD_FAILED", "failed"),
        ("MAST_VISION_LOAD_SKIPPED", "skipped"),
    ):
        idx = line.find(token)
        if idx >= 0:
            return kind, line[idx + len(token):].strip()
    return None


def _load_vision_warn_suppressed(root: Path) -> bool:
    """Read config/vision.json → has the user ticked "don't show again" for the vision
    load warning? Best-effort; DEFAULT False (show the warning) on any error.

    The warning dialog only appears when legacy features are on; this flag only
    governs whether that dialog is suppressed on start."""
    try:
        import json as _json
        p = _vision_config_path(root)
        if not p.exists():
            return False
        data = _json.loads(p.read_text(encoding="utf-8"))
        return bool(data.get("warn_suppressed", False))
    except Exception:
        return False


def _save_vision_warn_suppressed(root: Path, suppressed: bool) -> None:
    """Persist the "don't show again" choice to config/vision.json. Swallows errors."""
    try:
        import json as _json
        p = _vision_config_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps({"warn_suppressed": bool(suppressed)}),
                     encoding="utf-8")
    except Exception as exc:
        print(f"[launcher] could not save vision.json: {exc}")


# ── Persistent file logging ──────────────────────────────────────────
# Critical for diagnosing bundle startup failures. PyInstaller --noconsole
# (console=False in mast.spec) means sys.stdout/stderr go to NUL when the
# user double-clicks MAST.exe, so any uncaught exception during launcher
# init is silently swallowed. We tee everything to experiments/logs/ so
# the next launch can show the user "what went wrong last time".

class _Tee:
    """Forward writes to multiple file-like objects.

    Used to mirror stdout/stderr to a real file even when sys.__stdout__
    is None (PyInstaller --noconsole). Failures on any single stream are
    swallowed so logging never breaks the main code path.
    """

    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, data):
        n = 0
        for stream in self._streams:
            try:
                stream.write(data)
                stream.flush()
                n = len(data)
            except Exception:
                pass
        return n

    def flush(self):
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:
                pass

    def isatty(self):
        return False


_LAUNCHER_LOG_FH = None  # kept open for the lifetime of the process


def _log_dir_for(root: Path) -> Path:
    """Return (and create) the per-install log directory."""
    p = root / "experiments" / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _today_stamp() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y%m%d")


def _fmt_release_date(iso: str) -> str:
    """Trim an ISO-8601 stamp (e.g. RELEASED_AT / manifest.published_at) to a
    human YYYY-MM-DD. Returns 'unknown' for an empty/garbled value so the version
    dialog always renders an honest cell."""
    s = (iso or "").strip()
    if not s:
        return _tr("未知", "unknown")
    # Both RELEASED_AT and published_at are ISO-8601 (date part = first 10 chars).
    return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else s


def _setup_persistent_logging(user_root: Path) -> Path:
    """Tee sys.stdout/stderr → experiments/logs/launcher-YYYYMMDD.log.

    Idempotent — calling twice is a no-op (we keep one open file handle).
    Returns the log file path so the launcher can show it to the user.
    """
    global _LAUNCHER_LOG_FH
    if _LAUNCHER_LOG_FH is not None:
        return Path(_LAUNCHER_LOG_FH.name)

    import datetime, faulthandler, platform
    log_dir = _log_dir_for(user_root)
    log_path = log_dir / f"launcher-{_today_stamp()}.log"

    fh = open(log_path, "a", encoding="utf-8", buffering=1)
    fh.write(
        "\n"
        + "=" * 70
        + "\n"
        + f"=== Launcher session start: {datetime.datetime.now().isoformat()}\n"
        + f"=== sys.executable : {sys.executable}\n"
        + f"=== sys.frozen     : {getattr(sys, 'frozen', False)}\n"
        + f"=== _MEIPASS       : {getattr(sys, '_MEIPASS', '(none)')}\n"
        + f"=== platform       : {platform.platform()}\n"
        + f"=== python         : {sys.version.split()[0]}\n"
        + f"=== user_root      : {user_root}\n"
        + f"=== argv           : {sys.argv}\n"
        + "=" * 70
        + "\n"
    )
    fh.flush()
    _LAUNCHER_LOG_FH = fh

    # Tee the original sys.stdout/stderr (which may be None in --noconsole
    # bundle) into our file. After this point, print() and uncaught
    # exceptions reach the log file.
    sys.stdout = _Tee(sys.__stdout__, fh)
    sys.stderr = _Tee(sys.__stderr__, fh)

    # Hard-crash hook: catches segfaults and similar that don't go through
    # Python's exception machinery. faulthandler writes the C-level traceback
    # straight to fh.
    try:
        faulthandler.enable(file=fh, all_threads=True)
    except Exception:
        pass

    return log_path


# ── Path helpers ─────────────────────────────────────────────────────

def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _resource_path(relpath: Path | str) -> Path:
    """Resolve a path relative to the app, working in dev AND in PyInstaller.

    PyInstaller --onedir lays the source tree under sys._MEIPASS at runtime;
    in dev we just resolve relative to this file's parent. Caller passes a
    repo-relative path like ``Path("logo") / "MAST_logo.png"``.
    """
    base = Path(getattr(sys, "_MEIPASS", "")) if _frozen() else Path(__file__).resolve().parent
    p = base / Path(relpath)
    if p.exists():
        return p
    # Fallback: maybe the user moved the asset next to the .exe (e.g. zip
    # extract). Look there too so the launcher doesn't crash on a missing
    # resource — the logo is purely cosmetic.
    if _frozen():
        alt = Path(sys.executable).resolve().parent / Path(relpath)
        if alt.exists():
            return alt
    return p  # may not exist; caller checks


# When the Inno Setup installer runs it asks the user to pick a data
# directory (separate from the binary install path). The chosen path is
# written to <install>/data_dir.txt — one absolute path per file. Launcher
# reads this and uses it as the user_root so user data lives independently
# of the binary install (binary in C:\MAST\, data wherever the user picked).
DATA_DIR_MARKER = "data_dir.txt"


def _read_data_dir_marker(install_dir: Path) -> Path | None:
    """Read <install>/data_dir.txt, return resolved Path or None."""
    marker = install_dir / DATA_DIR_MARKER
    if not marker.exists():
        return None
    try:
        line = marker.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    if not line:
        return None
    p = Path(line).expanduser()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return p.resolve()


def _user_root() -> Path:
    """Directory holding experiments/, api key/, models/, config/.

    Resolution order:
      1. ``$MAST2_USER_ROOT`` env var (escape hatch for testing / portable mode)
      2. ``<exe-parent>/data_dir.txt`` (written by the Inno Setup installer)
      3. ``<exe-parent>`` (legacy layout — v0.2.0..0.2.4 default; also zip/dev)
    """
    env = os.environ.get("MAST2_USER_ROOT", "").strip()
    if env:
        p = Path(env).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p.resolve()
        except OSError:
            pass

    install_dir = (
        Path(sys.executable).resolve().parent if _frozen()
        else Path(__file__).resolve().parent
    )
    marker_target = _read_data_dir_marker(install_dir)
    if marker_target is not None:
        return marker_target
    return install_dir


def _ensure_user_dirs(root: Path) -> None:
    for sub in ("experiments", "working-sessions", "api key", "models"):
        (root / sub).mkdir(parents=True, exist_ok=True)


# ── Key loading / saving ─────────────────────────────────────────────

def _read_key_file(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        pass
    return ""


def get_key(provider: str, root: Path) -> str:
    """env var first → file fallback. Empty string when missing."""
    for var in PROVIDER_ENV_VARS.get(provider, ()):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    fname = PROVIDER_KEY_FILES.get(provider)
    if not fname:
        return ""
    return _read_key_file(root / "api key" / fname)


def save_key(provider: str, root: Path, key: str) -> Path:
    fname = PROVIDER_KEY_FILES[provider]
    path = root / "api key" / fname
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {PROVIDER_LABELS[provider]} API key. First non-comment line is used.\n"
        f"{key.strip()}\n",
        encoding="utf-8",
    )
    return path


# ── LAN auth (username + password) ───────────────────────────────────

def get_lan_auth(root: Path) -> tuple[str, str]:
    """Return (username, password) from `api key/lan_auth.env`, or ('','')."""
    path = root / "api key" / LAN_AUTH_FILENAME
    if not path.exists():
        return ("", "")
    user = ""
    pwd = ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not user:
                user = line
            elif not pwd:
                pwd = line
                break
    except OSError:
        return ("", "")
    return (user, pwd)


def save_lan_auth(root: Path, username: str, password: str) -> Path:
    """Persist LAN auth to `api key/lan_auth.env` (preserved across upgrade)."""
    path = root / "api key" / LAN_AUTH_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# MAST LAN access credentials. First non-comment line = username, second = password.\n"
        "# Used only for HTTP basic auth when LAN access is enabled; other API keys are unaffected.\n"
        f"{username.strip()}\n"
        f"{password.strip()}\n",
        encoding="utf-8",
    )
    return path


def clear_lan_auth(root: Path) -> None:
    """Remove the LAN auth file (used when user disables LAN access)."""
    path = root / "api key" / LAN_AUTH_FILENAME
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ── LAN auth handoff (ephemeral file → child, instead of env var) ─────
#
# These functions implement the secure launcher→child credential handoff:
# the password never enters the child's environment block. See the
# LAN_ENV_AUTH_FILE comment near the top of this module for the rationale.

def _restrict_file_to_owner(path: Path) -> None:
    """Best-effort: make ``path`` readable only by the current user.

    Windows: ``icacls`` removes inheritance and grants only the current
    user. POSIX: ``chmod 0600``. Any failure is swallowed — the file is
    short-lived and lives under the user's own data dir, so this is
    defence-in-depth, not the sole protection.
    """
    try:
        if sys.platform == "win32":
            user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
            if not user:
                return
            subprocess.run(
                ["icacls", str(path), "/inheritance:r",
                 "/grant:r", f"{user}:F"],
                capture_output=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.chmod(path, 0o600)
    except Exception:
        pass


def _lan_auth_handoff_dir(user_root: Path) -> Path:
    """Return (creating if needed) the ephemeral LAN-auth handoff dir."""
    d = user_root / LAN_AUTH_HANDOFF_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _sweep_lan_auth_handoff(user_root: Path) -> None:
    """Delete any leftover handoff files (e.g. a child that died pre-read).

    Called on launcher start and exit so stale credential files never
    accumulate on disk.
    """
    d = user_root / LAN_AUTH_HANDOFF_DIR
    if not d.exists():
        return
    try:
        for f in d.iterdir():
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def write_lan_auth_handoff(user_root: Path, username: str, password: str) -> Path:
    """Write `username\\npassword` to a unique owner-only temp file.

    Returns the file path; the consuming child is responsible for reading
    then deleting it (``consume_lan_auth_handoff``). The launcher also sweeps
    the directory on exit as a backstop against leaks.
    """
    import secrets
    d = _lan_auth_handoff_dir(user_root)
    path = d / f"auth-{os.getpid()}-{secrets.token_hex(8)}.tmp"
    # Create with restrictive mode up-front on POSIX (mkstemp-style); on
    # Windows we tighten the ACL right after writing.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, f"{username}\n{password}\n".encode("utf-8"))
    finally:
        os.close(fd)
    _restrict_file_to_owner(path)
    return path


def consume_lan_auth_handoff(path_str: str) -> tuple[str, str]:
    """Read (username, password) from a handoff file, then delete it.

    Returns ('', '') if the path is empty/unreadable. The file is unlinked
    in all cases (best-effort) so the secret does not outlive boot.
    """
    if not path_str:
        return ("", "")
    p = Path(path_str)
    user = ""
    pwd = ""
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        if lines:
            user = lines[0].strip()
        if len(lines) > 1:
            pwd = lines[1].strip()
    except OSError:
        pass
    finally:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    return (user, pwd)


def _read_lan_auth_from_env() -> tuple[str, str]:
    """Resolve LAN (username, password) for a service/admin child process.

    Preference order:
      1. ``LAN_ENV_AUTH_FILE`` — the secure ephemeral file (v2.1.14+). Read
         once and deleted; the password never lived in the env block.
      2. ``LAN_ENV_PASS`` — legacy plaintext env var, kept only so a new
         launcher can drive an old child or vice-versa.

    Username comes from the file when present, else ``LAN_ENV_USER``.
    Returns ('', '') when nothing usable is configured.
    """
    auth_file = os.environ.get(LAN_ENV_AUTH_FILE, "").strip()
    if auth_file:
        user, pwd = consume_lan_auth_handoff(auth_file)
        if user and pwd:
            return (user, pwd)
        # File missing/empty (already consumed by a sibling, or race) — fall
        # through to the legacy env path if present.
    user = os.environ.get(LAN_ENV_USER, "").strip()
    pwd  = os.environ.get(LAN_ENV_PASS, "").strip()
    return (user, pwd)


# ── Status checks ────────────────────────────────────────────────────

def port_open(host: str, port: int, *, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def gui_running() -> bool:
    return port_open(GUI_HOST, GUI_PORT)


def _pid_on_port(port: int) -> int | None:
    """Return the PID LISTENING on a local TCP *port* (Windows), else None.

    Used by the launcher's "stop service and quit launcher" action to stop a service that was
    started OUTSIDE this launcher (no Popen handle) — e.g. a directly-launched
    C:\\MAST\\MAST.exe. Parses `netstat -ano`; best-effort, never raises.
    """
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" \
                    and parts[-1].isdigit() and "LISTEN" in line.upper() \
                    and (parts[1].endswith(f":{port}")):
                return int(parts[-1])
    except Exception:
        pass
    return None


def _http_shutdown_request(port: int, *, log=None, auth=None) -> bool:
    """Best-effort POST /api/admin/shutdown to the LOCAL service so it closes the
    Nanonis pool + stops daemons + asks uvicorn to exit gracefully. A windowless
    (console=False) frozen service has no top-level window, so ``taskkill``
    WITHOUT ``/F`` can't reach it and the launcher falls through to a hard
    TerminateProcess mid-TCP — this HTTP path is what actually lets it stop
    cleanly. Loopback-only endpoint.

    In LAN mode the service sits behind HTTP basic auth over TLS, so the request
    carries *auth* ``(user, password)``; without it the service answers 401, this
    returns False and the caller falls back to a hard kill of the service that
    holds the Nanonis connection. The body is ``{}`` with a JSON content type,
    and success means the endpoint said ``ok`` — it answers 200 + ``ok: false``
    for a wrong shutdown token."""
    import base64
    import json as _json
    import ssl as _ssl
    import urllib.error
    import urllib.request

    def _say(m: str) -> None:
        if log is not None:
            try:
                log(m)
            except Exception:
                pass

    user, pwd = auth or ("", "")
    headers = {"Content-Type": "application/json"}
    if user and pwd:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()
    token = os.environ.get("MAST2_SHUTDOWN_TOKEN", "")
    if token:
        headers["x-mast-shutdown-token"] = token
    # LAN mode is TLS — ask over https first rather than throwing plaintext at it.
    for scheme in (("https", "http") if user and pwd else ("http", "https")):
        url = f"{scheme}://127.0.0.1:{port}/api/admin/shutdown"
        try:
            req = urllib.request.Request(url, method="POST", data=b"{}", headers=headers)
            ctx = _ssl._create_unverified_context() if scheme == "https" else None
            with urllib.request.urlopen(req, timeout=3, context=ctx) as resp:  # noqa: S310 (loopback)
                raw = resp.read(4096)
        except urllib.error.HTTPError as exc:
            _say(_tr(f"HTTP 优雅关闭被拒（{exc.code}）"
                     + ("：缺少或不匹配的局域网账号密码" if exc.code == 401 else "") + "。",
                     f"HTTP graceful shutdown refused ({exc.code})"
                     + (": missing or mismatched LAN username/password" if exc.code == 401 else "") + "."))
            continue
        except TimeoutError:
            # Only the RESPONSE read raises a bare TimeoutError (connect and send
            # errors arrive wrapped in URLError), so the request went out. The
            # endpoint answers only after the shutdown hooks finish (optics safe +
            # ack wait, ingest flush, external jobs), which can outlast 3 s: that is
            # a shutdown in progress, not a refusal — escalating to taskkill / a
            # hard kill here would cut the Nanonis link mid-teardown.
            _say(_tr("优雅关闭请求已送达，服务仍在执行关停步骤…",
                     "Graceful shutdown request delivered; the service is still running its shutdown steps..."))
            return True
        except Exception:
            continue
        try:
            body = _json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            return True        # 2xx without JSON: an older service took the request
        if isinstance(body, dict) and body.get("ok") is False:
            _say(_tr(f"HTTP 优雅关闭被拒：{body.get('error') or body}",
                     f"HTTP graceful shutdown refused: {body.get('error') or body}"))
            return False
        return True
    return False


def _graceful_stop_proc(proc, *, graceful_timeout: float = 15.0,
                        force_timeout: float = 5.0, log=None,
                        auth=None, http_shutdown: bool = True) -> str:
    """Stop a launcher-OWNED ``Popen`` GRACEFULLY, hard-killing only as a last
    resort.

    Why this is not just ``proc.terminate()``: on Windows ``Popen.terminate()``
    is ``TerminateProcess`` — an immediate HARD kill that gives the main service
    NO chance to run its shutdown handlers. The service holds the Nanonis V5e TCP
    connection, and a force-kill mid-TCP can corrupt the Nanonis port until
    Nanonis itself restarts (see _make_pid_stopper). So we mirror the
    safe external-service path: first ask the process to stop *gracefully* via
    ``taskkill /PID`` WITHOUT ``/F`` (which posts a normal termination request the
    target can intercept to graceful-shutdown the TCP link), wait up to
    *graceful_timeout* seconds, and only if it refuses do we fall back to a hard
    kill — and even then we log it so the (rare) port-corruption window is
    auditable. On POSIX, ``taskkill`` does not exist, so we send SIGTERM
    (proc.terminate) as the graceful step and SIGKILL (proc.kill) as the fallback.

    Step 0 (``/api/admin/shutdown``) is for the MAIN service only — pass
    ``http_shutdown=False`` for any other process, or it would ask the main
    service to exit. *auth* is the LAN ``(user, password)``; ``None`` reads the
    saved ``lan_auth.env`` (LAN mode answers 401 without it).

    Returns one of ``"graceful"`` / ``"forced"`` / ``"already-dead"`` / ``"error"``.
    """
    def _say(m: str) -> None:
        try:
            (log or logger.info)(m)
        except Exception:
            pass

    if proc is None or proc.poll() is not None:
        return "already-dead"
    pid = getattr(proc, "pid", None)

    # ── Graceful step 0: HTTP self-shutdown (works for a windowless service,
    #    which taskkill /PID without /F cannot reach). ─────────────────
    if http_shutdown:
        try:
            if auth is None:
                auth = get_lan_auth(_user_root())
            if _http_shutdown_request(GUI_PORT, log=log, auth=auth):
                _say(_tr("已请求主服务优雅自关闭（/api/admin/shutdown）。",
                         "Asked the main service to shut itself down gracefully (/api/admin/shutdown)."))
                for _ in range(max(1, int(min(graceful_timeout, 8.0) / 0.2))):
                    if proc.poll() is not None:
                        return "graceful"
                    time.sleep(0.2)
        except Exception as exc:
            _say(_tr(f"HTTP 优雅关闭请求失败（改用 taskkill）: {exc}",
                     f"HTTP graceful shutdown request failed (falling back to taskkill): {exc}"))

    # ── Graceful step ────────────────────────────────────────────────
    try:
        if sys.platform == "win32" and pid:
            # taskkill WITHOUT /F → cooperative stop; the service can close its
            # Nanonis TCP link cleanly before exiting (no TerminateProcess).
            subprocess.run(
                ["taskkill", "/PID", str(pid)],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            proc.terminate()  # POSIX: SIGTERM (graceful) — or Win fallback if no pid
    except Exception as exc:
        _say(_tr(f"优雅停止请求发送失败（将等待并在必要时强制结束）: {exc}",
                 f"Could not send the graceful stop request (will wait, then force-kill if needed): {exc}"))

    # Give it a GENEROUS window to flush + close the Nanonis port.
    deadline = max(1, int(graceful_timeout / 0.2))
    for _ in range(deadline):
        if proc.poll() is not None:
            return "graceful"
        time.sleep(0.2)

    # ── Forced fallback — only after graceful failed. Narrow the window. ──
    _say(_tr(f"主服务在 {graceful_timeout:.0f}s 内未优雅退出 — 将强制结束"
             f"（PID {pid}；注意：此时若正与 Nanonis 通信，端口可能需 Nanonis 重启恢复）。",
             f"Main service did not exit gracefully within {graceful_timeout:.0f}s - force-killing "
             f"(PID {pid}; if it was talking to Nanonis, the port may need a Nanonis restart to recover)."))
    try:
        proc.kill()  # Windows: TerminateProcess; POSIX: SIGKILL
    except Exception as exc:
        _say(_tr(f"强制结束失败: {exc}", f"Force-kill failed: {exc}"))
        return "error"
    for _ in range(max(1, int(force_timeout / 0.2))):
        if proc.poll() is not None:
            return "forced"
        time.sleep(0.2)
    return "forced"


#: Short cache for ``nanonis_connected()``: (wall-clock time, result). Status
#: refreshes call it often in the first two minutes after the service starts;
#: without the cache each call would spawn a netstat subprocess.
_NANONIS_PROBE: list = [0.0, False]
_NANONIS_PROBE_TTL_S = 1.5


def nanonis_connected() -> bool:
    """Whether the Nanonis TCP server is up (for the status indicator).

    Deliberately does NOT use ``port_open()``: that opens a real TCP connection
    to port 6501 and closes it again, and this function is polled dozens of times
    in the two minutes after the service starts (every 2 s for the first 30 s).
    Repeatedly connecting to and dropping the Nanonis main port is risky — a
    connection interrupted mid-TCP can leave the port unusable until the Nanonis
    software restarts — and an indicator light is not worth that.

    Instead it checks whether any local process is LISTENING on port 6501. That
    answers the same question (is Nanonis running) without sending a single byte
    to it. ``NANONIS_HOST`` is fixed to 127.0.0.1, so a local netstat always sees it.

    Cached for 1.5 s: ``_pid_on_port`` spawns a netstat subprocess, so polling
    without the cache would cost one process per call.
    """
    now = time.time()
    if (now - _NANONIS_PROBE[0]) < _NANONIS_PROBE_TTL_S:
        return bool(_NANONIS_PROBE[1])
    ok = _pid_on_port(NANONIS_PORT) is not None
    _NANONIS_PROBE[0], _NANONIS_PROBE[1] = now, ok
    return ok


# ── Service-mode entry (existing GUI boot path) ──────────────────────

def _lan_cert_files(user_root: Path) -> tuple[Path, Path]:
    """(cert, key) of the LAN certificate: :func:`mast.net.tls.lan_cert_paths`,
    with the same literal as a fallback so boot never hinges on that import."""
    try:
        from mast.net.tls import lan_cert_paths
        return lan_cert_paths(user_root)
    except Exception:  # noqa: BLE001
        return user_root / "api key" / "lan_cert.pem", user_root / "api key" / "lan_key.pem"


def _ensure_lan_cert(cert: Path, key: Path, log, extra_hosts=None) -> bool:
    """Make sure the self-signed LAN cert+key exist and the cert lists every
    host a client dials (loopback + this machine's hostname/IPs + *extra_hosts*,
    e.g. the Tailscale host or the push-server address).

    The logic lives in :func:`mast.net.tls.ensure_lan_cert`: a missing host is
    added by re-signing with the SAME private key, so the SPKI that satellites
    pin survives IP changes. Minting a new key on every re-sign would cut every
    pinned satellite off at the next start.

    Returns True when a usable cert/key pair exists afterward. Needs
    `cryptography` (pinned in requirements); if absent, returns False and the
    caller falls back to the cleartext-warning path."""
    try:
        from mast.net.tls import ensure_lan_cert
    except Exception as exc:  # noqa: BLE001 — a broken import must not block boot
        log.warning("LAN TLS helper unavailable (%s) — using %s as is.", exc, cert)
        return cert.exists() and key.exists()
    res = ensure_lan_cert(cert, key, extra_hosts=extra_hosts or (), log=log)
    if res.ok and res.spki_sha256:
        log.info("LAN TLS SPKI sha256=%s (satellite hub.spki_sha256)", res.spki_sha256)
    return res.ok


def run_service() -> int:
    """Boot the MAST service (FastAPI + TS SPA). Called when ``--service-mode`` is set."""
    user_root = _user_root()
    os.chdir(user_root)
    _ensure_user_dirs(user_root)
    os.environ.setdefault("MAST2_PROJECT_ROOT", str(user_root))

    # Belt (frozen service): the DINOv3 vision backbone is bundled offline next to
    # the exe, so timm/HF must NEVER reach the network. A blocked huggingface.co
    # (restricted network) otherwise makes timm.create_model(pretrained=True)
    # retry-hang for minutes → the background vision warm never signals DONE/FAILED
    # → the launcher's 3-min vision watcher times out and the user sees
    # "failed to start" even though the service is up.
    # configure_backbone_cache also forces this; set it process-wide here in case
    # any code path imports timm before that runs.
    if getattr(sys, "frozen", False):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    # MUST precede heavy imports (Py3.14 platform._wmi_query() hang on import).
    import mast.core.platform_patch  # noqa: F401

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log = logging.getLogger("mast.service")

    # Surface Anthropic key into env if file exists (legacy compat).
    if not os.environ.get("ANTHROPIC_API_KEY"):
        k = get_key("anthropic", user_root)
        if k:
            os.environ["ANTHROPIC_API_KEY"] = k

    from mast.config import MASTConfig

    config = MASTConfig()
    # MAST2 binds 7862 to avoid colliding with v1 at 7860
    config.server.server_port = GUI_PORT
    host = "127.0.0.1"
    auth = None
    if os.environ.get(LAN_ENV_ENABLE) == "1":
        user, pwd = _read_lan_auth_from_env()
        if user and pwd:
            host = "0.0.0.0"
            auth = (user, pwd)
            log.info("LAN access ON — binding 0.0.0.0 with HTTP basic auth (user=%s).", user)
        else:
            log.warning("LAN access requested but auth empty — staying on 127.0.0.1.")
    # Scheme logged accurately after the TLS decision below; bind host/port here.
    log.info("Starting MAST service (FastAPI + TS SPA) on %s:%s ...", host, GUI_PORT)
    # Background update-checker. Always starts; reads server URL + token
    # from <data>/api key/ each cycle, no-op when neither is configured.
    try:
        from mast import __version__ as _mast_version
        from mast.update.client import UpdateChecker
        checker = UpdateChecker(
            user_root, _mast_version,
            interval_min=config.update.check_interval_min,
        )
        checker.start()
        log.info("Update checker started (interval=%dmin)",
                 config.update.check_interval_min)
    except Exception as exc:
        log.warning("Could not start update checker: %s", exc)

    # Pre-warm the M12 vision model in the background so the scan-progress
    # vision monitor's 12.5%→100% milestones fire promptly on the first scan
    # (the ~30s backbone cold-load otherwise races a short acquisition).
    #
    # Vision loads WITH the service (always on). Warm it in a daemon thread that
    # emits MAST_VISION_LOAD_{START,DONE,FAILED} markers so the launcher's progress
    # bar can track the load. CRITICAL: use the LOGGING channel, NOT print(). In the
    # frozen --noconsole build, the launcher Popen's the service with stdout=PIPE;
    # a bare print() to sys.stdout there interleaves unreliably with the logging
    # StreamHandler's stderr writes and the DONE marker was being LOST in the pipe
    # (the service loaded vision fine but the launcher never saw DONE → a 3-min
    # "vision model load timed out" on any GPU). Logging goes through the
    # same handler as every other line that DOES reach the pipe; the launcher
    # matches the marker as a substring, so "[INFO] mast.service: MAST_VISION_LOAD_DONE"
    # still triggers _vision_progress_done.
    try:
        from mast.vision.module import vision_models_enabled as _vision_on
        _vision_enabled = _vision_on()
    except Exception:  # noqa: BLE001
        _vision_enabled = False
    # Always emit a terminal MAST_VISION_LOAD_* marker the launcher can match.
    # Without it, a skipped warm (legacy features off, default) left the
    # progress bar spinning until the 3-min timeout — operators read that as
    # "视觉模型加载失败" even though the service was fine.
    if not _vision_enabled:
        log.info("vision models disabled (legacy_features_enabled off) — skipping pre-warm")
        log.info("MAST_VISION_LOAD_SKIPPED disabled")
    elif os.environ.get("MAST_VISION_BACKEND", "").strip().lower() != "mock":
        def _vision_warm() -> None:
            try:
                log.info("MAST_VISION_LOAD_START")
                from mast.vision.module import VisionModule
                vm = VisionModule.get()
                be = getattr(vm, "_backend", None)
                backend_name = str(
                    getattr(vm, "_backend_name", "") or type(be).__name__
                ).lower()
                if be is not None and hasattr(be, "preload"):
                    be.preload()
                # Mock = weights missing / init fell back. Still a terminal
                # success for the service; the launcher shows a calm status
                # instead of a green "loaded" or a red failure.
                if backend_name in ("mock", "mockbackend") or type(be).__name__ == "MockBackend":
                    log.info("MAST_VISION_LOAD_DONE mock")
                else:
                    log.info("MAST_VISION_LOAD_DONE")
            except Exception as exc:  # never crash the service
                log.error("MAST_VISION_LOAD_FAILED %s", exc)
        try:
            threading.Thread(target=_vision_warm, daemon=True).start()
            log.info("vision model pre-warm kicked off (background)")
        except Exception as exc:
            log.info("vision pre-warm not started: %s", exc)
            log.info("MAST_VISION_LOAD_SKIPPED error")
    else:
        log.info("vision backend forced to mock via env — skipping pre-warm")
        log.info("MAST_VISION_LOAD_SKIPPED mock")

    # Build the live FastAPI app (gradio-free CoreRuntime core + bundled TS SPA)
    # and serve it. This REPLACES the deleted Gradio app — same URL, same port.
    import uvicorn

    from mast.api.app import create_app
    from mast.api.bootstrap import build_live_context

    ctx = build_live_context(config)
    app = create_app(context=ctx, dev_cors=False, auth=auth)

    ssl_kw: dict = {}
    if host != "127.0.0.1":
        cert, key = _lan_cert_files(user_root)
        # Fold this machine's Tailscale host(s) into the cert SAN so a peer that
        # dials the MagicDNS name / 100.x IP gets a name match, not a mismatch
        # warning. Best-effort: no Tailscale → empty → cert unchanged.
        ts_extra: list[str] = []
        try:
            from mast.net.tailscale import extra_cert_hosts, get_status
            ts_extra = extra_cert_hosts(get_status())
            if ts_extra:
                log.info("Tailscale remote access ready - adding to certificate SAN: %s", ts_extra)
        except Exception as exc:  # never block boot on detection
            log.info("Tailscale host detection skipped: %s", exc)
        if _ensure_lan_cert(cert, key, log, extra_hosts=ts_extra):
            ssl_kw = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
            log.info("TLS enabled for LAN (https).")
        else:
            log.warning(
                "LAN binding WITHOUT TLS — basic-auth creds travel in cleartext. "
                "Install `cryptography` (auto-gen) or drop lan_cert.pem/lan_key.pem "
                "in <data>/api key/ to enable HTTPS."
            )

    # Use uvicorn.Server (not uvicorn.run) so the in-process /api/admin/shutdown
    # endpoint can set server.should_exit for a clean graceful stop — the
    # launcher POSTs it before any taskkill fallback.
    #
    # timeout_graceful_shutdown: a streaming response (an SSE stream with a client
    # still reading) ends only when that client goes away, so without a bound uvicorn
    # waits for it forever and the process dies by the endpoint's 20 s os._exit
    # backstop instead. The Nanonis pool is already closed by then (the endpoint
    # calls app.shutdown() before setting should_exit; the lifespan shutdown runs
    # after this bound on every other path), so cancelling the leftover connection
    # tasks after 5 s loses nothing that matters.
    #
    # proxy_headers=False: uvicorn's default trusts X-Forwarded-For from a loopback client and
    # replaces the connection address with it, so any program on this machine could choose the
    # "peer" that the operator API records for an answer. MAST is not behind a reverse proxy.
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=GUI_PORT, proxy_headers=False,
                                           timeout_graceful_shutdown=5, **ssl_kw))
    try:
        app.state.uvicorn_server = server
    except Exception:
        pass
    server.run()
    return 0


def _run_publish(setup_exe: str, version: str | None) -> int:
    """Admin CLI: publish a new MAST version to the push server's data dir.

    Mirrors ``python -m mast.update publish`` so admins can use the bundle's
    own MAST.exe without needing a separate Python install.
    """
    if not version:
        print("ERROR: --publish requires --version X.Y.Z", file=sys.stderr)
        return 2
    user_root = _user_root()
    _ensure_user_dirs(user_root)
    os.environ.setdefault("MAST2_PROJECT_ROOT", str(user_root))

    from mast.update.manifest import make_manifest, write_manifest
    from mast.update.server import push_dir
    import shutil

    src = Path(setup_exe).resolve()
    if not src.exists():
        print(f"ERROR: not found: {src}", file=sys.stderr)
        return 2
    pdir = push_dir(user_root)
    target = pdir / src.name
    if src.resolve() != target.resolve():
        print(f"Copying {src.name} -> {target}")
        shutil.copy2(src, target)
    print(f"Computing SHA256 of {target.name} ...")
    m = make_manifest(target, version=version.strip())
    write_manifest(pdir / "manifest.json", m)
    print(f"\nPublished v{m.version}")
    print(f"  filename:    {m.filename}")
    print(f"  sha256:      {m.sha256}")
    print(f"  size_bytes:  {m.size_bytes}")
    print(f"  manifest:    {pdir / 'manifest.json'}")
    return 0


def run_push_server() -> int:
    """Boot the intranet push-update server. Called via ``--push-server-mode``."""
    user_root = _user_root()
    os.chdir(user_root)
    _ensure_user_dirs(user_root)
    os.environ.setdefault("MAST2_PROJECT_ROOT", str(user_root))

    import mast.core.platform_patch  # noqa: F401
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log = logging.getLogger("mast.update_server")

    from mast.config import MASTConfig
    from mast.update.server import run_server

    cfg = MASTConfig().update
    host = cfg.server_host
    # LAN TLS: encrypt the Bearer token + manifest + installer/delta bytes in
    # flight. Reuse the self-signed LAN cert, but ensure its SAN covers the
    # push-server address clients dial (server_host) so they don't get a hostname
    # mismatch. A loopback-only bind stays plaintext http (local dev). Clients
    # pin this cert as their CA — the admin copies lan_cert.pem to each client's
    # <data>/api key/update_server_ca.pem (out-of-band; avoids TOFU).
    ssl_cert = ssl_key = None
    if host not in ("127.0.0.1", "localhost", "::1", ""):
        cert, key = _lan_cert_files(user_root)
        if _ensure_lan_cert(cert, key, log, extra_hosts=[host]):
            ssl_cert, ssl_key = str(cert), str(key)
            log.info("push server TLS enabled (https). Distribute %s to clients as "
                     "'update_server_ca.pem' so they trust it.", cert)
            # Auto-pin the serving cert as the LOCAL client's trusted CA. The
            # client trusts a build-bundled CA that does NOT match this machine's
            # runtime self-signed cert, so without a pin the same-machine wishlist
            # relay + OTA download fail TLS verification (CERTIFICATE_VERIFY_FAILED
            # — the update inbox / OTA download silently failing). Remote clients still
            # get the cert out-of-band; this only self-heals the admin's own box.
            try:
                ca_pin = user_root / "api key" / "update_server_ca.pem"
                if (not ca_pin.exists()) or ca_pin.read_bytes() != cert.read_bytes():
                    ca_pin.write_bytes(cert.read_bytes())
                    log.info("auto-pinned serving cert → %s (local client now trusts this server)", ca_pin)
            except Exception as exc:  # best-effort; remote clients unaffected
                log.warning("could not auto-pin serving cert for the local client: %s", exc)
        else:
            log.warning("push server WITHOUT TLS — token + update bytes travel in "
                        "cleartext; install `cryptography` to auto-gen a LAN cert.")
    # MAST2 binds 8766 to avoid colliding with v1's 8765 on the same admin host
    log.info("MAST push server starting on %s://%s:%d  (data: %s)",
             "https" if ssl_cert else "http", host, PUSH_SERVER_PORT, user_root)
    run_server(user_root, host=host, port=PUSH_SERVER_PORT,
               ssl_certfile=ssl_cert, ssl_keyfile=ssl_key)
    return 0


def _clear_pending_delta(pdir: Path) -> None:
    """Discard a staged pending delta (marker + zip + offline stage) so a stale /
    bad orphan isn't retried."""
    import shutil as _sh
    try:
        from mast.update.client import DELTA_MARKER
        (pdir / DELTA_MARKER).unlink(missing_ok=True)
        for z in pdir.glob("delta_*.zip"):
            z.unlink(missing_ok=True)
        stage = pdir / "offline_stage"
        if stage.exists():
            _sh.rmtree(stage, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[launcher] could not clear stale delta: {exc}")


def _pending_delta_from_current(man: dict, pdir: Path) -> bool:
    """True iff the pending delta's ``from_version`` matches our CURRENT version,
    so it is the right delta to apply here. A stale orphan (from a prior version —
    e.g. left in pending after a full-installer upgrade) has a mismatched
    from_version; applying it would mis-patch / downgrade the new install, so we
    skip AND clear it. Also requires a plausible newer
    to_version."""
    try:
        from mast import __version__ as cur
    except Exception:  # pragma: no cover
        cur = ""
    frm = str(man.get("from_version", ""))
    to = str(man.get("to_version", ""))
    if frm and frm == str(cur) and to and to != str(cur):
        return True
    print(f"[launcher] pending delta {frm!r}→{to!r} does not match current {cur!r} "
          f"— clearing stale orphan")
    _clear_pending_delta(pdir)
    return False


def _apply_pending_delta_if_safe(user_root: Path) -> None:
    """Hot-apply a staged OTA delta IFF it only refreshes data assets (the
    literature index / vision checkpoint) — safe to overwrite while MAST.exe is
    running. A delta touching code/exe/dll is left for the full-installer path
    (Windows can't replace the running exe in place). Fail-safe: any error leaves
    the delta untouched so the full installer remains the fallback.

    Applied BEFORE the full-installer check; a data-only delta leaves no .exe in
    pending, so load_pending() returns None and the launcher continues normally
    into the freshly-updated app.
    """
    try:
        import json as _json
        from mast.update.client import (
            DELTA_MARKER, apply_pending_delta, delta_is_data_only, pending_dir,
        )
        from mast.update.delta import read_delta_manifest
    except Exception as exc:
        print(f"[launcher] delta module unavailable: {exc}")
        return
    try:
        pdir = pending_dir(user_root)
        marker = pdir / DELTA_MARKER
        if not marker.exists():
            return
        info = _json.loads(marker.read_text(encoding="utf-8"))
        dz = pdir / str(info.get("filename", ""))
        if not dz.exists():
            return
        man = read_delta_manifest(dz)
        if not _pending_delta_from_current(man, pdir):
            return  # stale orphan (from a prior version) — skipped + cleared
        paths = (list(man.get("added", [])) + list(man.get("changed", []))
                 + list(man.get("removed", [])))
        if not delta_is_data_only(paths):
            # code/exe/base_library delta → handled by the offline applier next
            return
        install_root = (Path(sys.executable).resolve().parent if _frozen()
                        else user_root)
        status, detail = apply_pending_delta(user_root, install_root)
        print(f"[launcher] OTA delta apply: {status} — {detail}")
    except Exception as exc:
        print(f"[launcher] delta apply skipped (fail-safe): {exc}")


# Self-updater run AFTER MAST exits, so it can replace the locked exe /
# base_library.zip that a code delta touches (Windows can't overwrite a running
# image in place). Python-free (system PowerShell) so it doesn't depend on the
# _internal it's patching. Reads a plan JSON (paths already sha256-verified +
# traversal-checked by stage_offline_delta), backs up each file before
# overwriting, rolls back on any failure, then relaunches MAST either way
_OFFLINE_APPLY_PS1 = r"""
param([Parameter(Mandatory=$true)][string]$PlanPath)
$ErrorActionPreference = 'Stop'
try {
  $plan  = Get-Content -Raw -LiteralPath $PlanPath | ConvertFrom-Json
  $stage = $plan.stage_dir
  $root  = $plan.install_root
  $exe   = Join-Path $root 'MAST.exe'
  $rootPrefix = if ($root.EndsWith('\')) { $root } else { $root + '\' }
  $log   = Join-Path $root ('_delta_apply_{0}.log' -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
  $backup = Join-Path $root '_delta_backup'
  function Log($m) { try { Add-Content -LiteralPath $log -Value ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $m) } catch {} }
  Log ('offline delta apply: {0} -> {1}' -f $plan.from_version, $plan.to_version)

  # 1) Wait for every MAST process under the install root to exit (max 180s).
  $deadline = (Get-Date).AddSeconds(180)
  while ((Get-Date) -lt $deadline) {
    $running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
      $_.Path -and $_.Path.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase) })
    if ($running.Count -eq 0) { break }
    Start-Sleep -Milliseconds 500
  }
  Start-Sleep -Seconds 2   # let file handles fully release

  # 2) Backup then overwrite staged files; backup then delete removed.
  if (Test-Path -LiteralPath $backup) { Remove-Item -Recurse -Force -LiteralPath $backup }
  New-Item -ItemType Directory -Force -Path $backup | Out-Null
  $failed = $null
  try {
    foreach ($rel in $plan.copies) {
      $src = Join-Path $stage $rel; $dst = Join-Path $root $rel
      $dstDir = Split-Path -Parent $dst
      if (-not (Test-Path -LiteralPath $dstDir)) { New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }
      if (Test-Path -LiteralPath $dst) {
        $bdst = Join-Path $backup $rel; $bdir = Split-Path -Parent $bdst
        if (-not (Test-Path -LiteralPath $bdir)) { New-Item -ItemType Directory -Force -Path $bdir | Out-Null }
        Copy-Item -LiteralPath $dst -Destination $bdst -Force
      }
      Copy-Item -LiteralPath $src -Destination $dst -Force
    }
    foreach ($rel in $plan.removed) {
      $dst = Join-Path $root $rel
      if (Test-Path -LiteralPath $dst) {
        $bdst = Join-Path $backup $rel; $bdir = Split-Path -Parent $bdst
        if (-not (Test-Path -LiteralPath $bdir)) { New-Item -ItemType Directory -Force -Path $bdir | Out-Null }
        Copy-Item -LiteralPath $dst -Destination $bdst -Force
        Remove-Item -LiteralPath $dst -Force
      }
    }
  } catch { $failed = $_.Exception.Message }

  if ($failed) {
    Log ('apply FAILED: {0} -- rolling back' -f $failed)
    Get-ChildItem -Recurse -File -LiteralPath $backup -ErrorAction SilentlyContinue | ForEach-Object {
      $rel = $_.FullName.Substring($backup.Length).TrimStart('\')
      $dst = Join-Path $root $rel; $dstDir = Split-Path -Parent $dst
      if (-not (Test-Path -LiteralPath $dstDir)) { New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }
      Copy-Item -LiteralPath $_.FullName -Destination $dst -Force
    }
    Log 'rollback complete; relaunching previous version'
  } else {
    Log ('apply OK: {0} copied, {1} removed -> {2}' -f $plan.copies.Count, $plan.removed.Count, $plan.to_version)
    Remove-Item -Recurse -Force -LiteralPath $stage -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force -LiteralPath $backup -ErrorAction SilentlyContinue
    $pdir = Split-Path -Parent $stage
    Remove-Item -Force -LiteralPath (Join-Path $pdir 'delta.json') -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $pdir -Filter 'delta_*.zip' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    Remove-Item -Force -LiteralPath $PlanPath -ErrorAction SilentlyContinue
  }

  # 3) Relaunch MAST (new version on success, old on rollback).
  Log ('relaunching {0}' -f $exe)
  Start-Process -FilePath $exe -WorkingDirectory $root
} catch {
  try { Add-Content -LiteralPath (Join-Path $env:TEMP 'mast_delta_apply_error.log') -Value $_.Exception.Message } catch {}
  try { Start-Process -FilePath (Join-Path $plan.install_root 'MAST.exe') } catch {}
}
""".lstrip()


def _notify_offline_apply(version: str) -> None:
    """Best-effort 2.5s toast so the user knows MAST is restarting to update."""
    try:
        import tkinter as tk
        r = tk.Tk()
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        tk.Label(r, text=_tr(f"正在应用增量更新 → v{version}\nMAST 即将自动重启…",
                             f"Applying incremental update -> v{version}\nMAST will restart automatically..."),
                 font=("Segoe UI", 11), padx=26, pady=18).pack()
        r.update_idletasks()
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        w, h = r.winfo_reqwidth(), r.winfo_reqheight()
        r.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        r.after(2500, r.destroy)
        r.mainloop()
    except Exception:  # noqa: BLE001 — cosmetic only
        pass


def _apply_pending_delta_offline_if_needed(user_root: Path) -> bool:
    """A pending delta that is NOT hot-appliable (touches code / exe /
    base_library.zip) can't be written while MAST runs. Stage it (verified) and
    spawn a self-updater that applies it AFTER MAST exits, then relaunches.
    Returns True iff the launcher should exit (applier spawned). Fail-safe: any
    problem returns False so the full-installer fallback still runs."""
    try:
        import json as _json
        from mast.update.client import (
            DELTA_MARKER, delta_is_data_only, pending_dir, stage_offline_delta,
        )
        from mast.update.delta import read_delta_manifest
    except Exception as exc:
        print(f"[launcher] offline delta module unavailable: {exc}")
        return False
    try:
        pdir = pending_dir(user_root)
        marker = pdir / DELTA_MARKER
        if not marker.exists():
            return False
        info = _json.loads(marker.read_text(encoding="utf-8"))
        dz = pdir / str(info.get("filename", ""))
        if not dz.exists():
            return False
        man = read_delta_manifest(dz)
        if not _pending_delta_from_current(man, pdir):
            return False  # stale orphan (from a prior version) — skipped + cleared
        paths = (list(man.get("added", [])) + list(man.get("changed", []))
                 + list(man.get("removed", [])))
        if delta_is_data_only(paths):
            return False  # already hot-applied live by _apply_pending_delta_if_safe
        install_root = (Path(sys.executable).resolve().parent if _frozen()
                        else user_root)
        status, plan = stage_offline_delta(user_root, install_root)
        if status != "staged" or not isinstance(plan, dict):
            print(f"[launcher] offline delta stage failed ({status}: {plan}) — full-installer fallback")
            return False
        plan_path = pdir / "apply_plan.json"
        ps1_path = pdir / "_offline_apply.ps1"
        plan_path.write_text(_json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        ps1_path.write_text(_OFFLINE_APPLY_PS1, encoding="utf-8")
        # Spawn the self-updater ROBUSTLY. The old DETACHED_PROCESS (no console) +
        # `-WindowStyle Hidden` + PyInstaller's injected env + no std handles made
        # the spawned powershell die with ERROR_BAD_EXE_FORMAT before it could run
        # Fix: full path to powershell.exe, a CLEAN env (drop
        # the PyInstaller _MEI/_PYI vars), CREATE_NO_WINDOW (a hidden console — the
        # child still gets valid handles and survives the launcher exiting), and
        # DEVNULL std handles.
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        ps_exe = os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if not os.path.exists(ps_exe):
            ps_exe = "powershell"
        child_env = {k: v for k, v in os.environ.items()
                     if not (k.startswith("_MEI") or k.startswith("_PYI"))}
        CREATE_NO_WINDOW = 0x08000000
        flags = CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            [ps_exe, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ps1_path), str(plan_path)],
            cwd=str(user_root), creationflags=flags, close_fds=True, env=child_env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(f"[launcher] offline delta applier spawned for v{plan.get('to_version')} — exiting to let it patch")
        _notify_offline_apply(str(plan.get("to_version", "")))
        return True
    except Exception as exc:
        print(f"[launcher] offline delta apply skipped (fail-safe): {exc}")
        return False


def _check_and_run_pending_update(user_root: Path) -> bool:
    """If a verified pending update exists, show modal + run installer.

    Returns True if the launcher should exit (because the installer was
    started); False if no update was applied and the launcher should
    continue normally.
    """
    try:
        from mast.update.client import archive_installed, load_pending
    except Exception as exc:
        # Module missing in older bundles or import error — never block startup.
        print(f"[launcher.main] update client unavailable: {exc}")
        return False

    manifest, setup_path = load_pending(user_root)
    if manifest is None or setup_path is None:
        return False

    # Check that we're actually older than the pending version (defensive —
    # avoid prompting if user manually downgrades or the file is stale).
    try:
        from mast.update.manifest import is_newer
        from mast import __version__ as current_version
    except Exception:
        return False
    if not is_newer(manifest.version, current_version):
        # Old or equal — clean it up so we don't keep nagging.
        archive_installed(user_root)
        return False

    # Show a Tk modal. Single button, no close.
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        # No Tk → just run the installer directly.
        _run_installer_and_exit(setup_path, user_root, manifest.version)
        return True

    _enable_dpi_awareness()

    root = tk.Tk()
    root.title(_tr("MAST 强制更新", "MAST mandatory update"))
    root.resizable(False, False)
    # No window-close button: this is a forced update.
    root.protocol("WM_DELETE_WINDOW", lambda: None)
    try:
        logo = tk.PhotoImage(file=str(_resource_path(LOGO_PNG_RELPATH)))
        root.iconphoto(True, logo)
    except Exception:
        logo = None  # noqa: F841

    frm = ttk.Frame(root, padding=20)
    frm.grid()
    ttk.Label(
        frm, text=_tr(f"MAST 有新版本 v{manifest.version}",
                      f"A new MAST version is available: v{manifest.version}"),
        font=("Segoe UI", 14, "bold"),
    ).grid(row=0, column=0, sticky="w")
    ttk.Label(
        frm,
        text=_tr(
            f"当前版本: v{current_version}\n"
            f"待安装版本: v{manifest.version}\n"
            f"发布时间: {manifest.published_at}\n"
            f"文件大小: {manifest.size_bytes // (1024*1024)} MB\n\n"
            "本次更新为**强制更新**，必须安装后才能继续使用 MAST。\n"
            "升级会保留所有用户数据（API key / 实验 / 模型 / 配置）。\n",
            f"Current version: v{current_version}\n"
            f"Version to install: v{manifest.version}\n"
            f"Published: {manifest.published_at}\n"
            f"File size: {manifest.size_bytes // (1024*1024)} MB\n\n"
            "This is a **mandatory update**; it must be installed before MAST can be used.\n"
            "The upgrade keeps all user data (API keys / experiments / models / settings).\n"
        ),
        justify="left",
    ).grid(row=1, column=0, sticky="w", pady=(8, 12))

    def _install():
        root.destroy()
        _run_installer_and_exit(setup_path, user_root, manifest.version)

    ttk.Button(
        frm, text=_tr("立即安装", "Install now"), command=_install,
    ).grid(row=2, column=0, sticky="e")

    # Center on screen
    root.update_idletasks()
    w = root.winfo_reqwidth(); h = root.winfo_reqheight()
    sw = root.winfo_screenwidth(); sh = root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
    root.mainloop()
    return True


def _run_installer_and_exit(setup_path: Path, user_root: Path, version: str) -> None:
    """Spawn the Inno Setup installer (elevated) and exit the launcher.

    v0.3.5 fix: previously used ``subprocess.Popen([..., "/SILENT", ...])``
    which loses the UAC prompt to the background and silently fails when
    the user dismisses it (or doesn't see it). Now uses ShellExecuteW with
    the "runas" verb — Windows's standard "request elevation" call —
    which pops UAC to the foreground with focus. We also drop ``/SILENT``
    so the user sees the Inno Setup wizard's progress bar (and any error
    dialogs); ``/SUPPRESSMSGBOXES`` still auto-confirms the data-dir page.
    """
    print(f"[launcher.main] running installer: {setup_path} (v{version})")
    args = "/SP- /SUPPRESSMSGBOXES /NORESTART"
    rc = -1
    try:
        import ctypes
        # ShellExecuteW(hwnd, verb, file, params, dir, show_cmd)
        # Returns >32 on success (the HINSTANCE-shaped result).
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", str(setup_path), args, str(setup_path.parent), 1,
        )
    except Exception as exc:
        print(f"[launcher.main] ShellExecuteW failed: {exc}")
        rc = 0  # force fallback below

    if rc <= 32:
        # ShellExecuteW failed (UAC denied, file not found, etc.). Fall back
        # to plain Popen — the OS will still try its own UAC flow, just
        # without the foreground guarantee.
        print(f"[launcher.main] ShellExecuteW returned {rc}; falling back to Popen")
        try:
            subprocess.Popen(
                [str(setup_path), "/SP-", "/SUPPRESSMSGBOXES", "/NORESTART"],
                cwd=str(setup_path.parent),
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except Exception as exc:
            print(f"[launcher.main] Popen fallback also failed: {exc}")
            return
    # IMPORTANT: do NOT call archive_installed here. ShellExecuteW returns
    # asynchronously; the Inno Setup process hasn't actually opened the
    # .exe file yet. Moving the file out from under it instantly causes
    # Windows to fail elevation with ERROR_FILE_NOT_FOUND.
    # The next launcher start handles cleanup via _check_and_run_pending_update's
    # "is_newer(remote, current_version)" check — if we're already on the
    # version that pending_update advertises, archive_installed runs there
    # instead, well after the file is no longer needed.


def _enable_dpi_awareness() -> None:
    """Tell Windows we draw at native pixel scale so 200% DPI doesn't blur Tk.

    On Windows 10+ this asks for per-monitor v2 DPI awareness; older builds
    silently fall back to system-DPI awareness. Failure is non-fatal — we
    just live with bitmap-stretched fonts.
    """
    try:
        import ctypes
        # Per-monitor v2 (best on Win10+)
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            )
            return
        except Exception:
            pass
        # Per-monitor v1 (Win8.1+)
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        # System-DPI (Win 7+)
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ══ Operator prompts: the desktop popup for structured human-in-the-loop requests ══
#
# An agent (Claude Code over /api/ext/v1, a protocol that needs a half-wave plate
# changed, a composite ``human`` node...) posts a structured *operator prompt* and
# waits; only a person at the instrument may answer it (design contract:
# docs/v2/design/optical_station_witec.md, D8). The web page shows the same prompts,
# but an operator watching the vendor software never looks at a browser tab. This
# launcher runs in the operator's own desktop session, so it polls the local service
# and puts a topmost window plus a sound in front of them.
#
# Layers, bottom first. Everything except the last needs no display and is tested in
# tests/v2/launcher/test_operator_popup.py:
#   normalize_operator_prompt / operator_prompt_fields / build_operator_answer   pure
#   OperatorPromptQueue        what is open, what is snoozed, in what order
#   OperatorApi                the HTTP calls (scheme, Basic auth, JSON bodies)
#   OperatorPoller             asks what the station is, then (optical only) polls, with quiet
#                              back-off, on its own thread
#   OperatorPopupController    Tk-thread glue: queue <-> view <-> API results
#   OperatorPromptView         the Tk window; it draws and collects, never decides
#
# Threads: the poller and every answer/dismiss call run on their own daemon thread
# and reach the Tk thread only through ``controller.inbox`` (a queue.Queue drained by
# a ``win.after`` pump), so a slow or dead service can never freeze a window.
#
# Identity strings (kind, status, option ids, slot ids) are compared as they arrive
# and never go through _tr(); only display text does.

OPERATOR_PROMPTS_API = "/api/operator/prompts"
OPERATOR_OPTICAL_STATE_API = "/api/operator/optical-state"
#: Where the station profile lives: GET /api/health -> ``station_profile`` ("stm" | "optical").
OPERATOR_HEALTH_API = "/api/health"
OPERATOR_SOURCE = "launcher"
OPERATOR_POLL_S = 2.0
#: Delay after the 1st, 2nd, ... consecutive failed poll; the last value repeats.
OPERATOR_BACKOFF_S = (2.0, 4.0, 8.0, 15.0, 30.0)
#: An older service has no operator API at all: ask again rarely.
OPERATOR_UNSUPPORTED_RETRY_S = 60.0
#: "Ignore for now" hides a prompt this long; it then comes back with the sound.
OPERATOR_SNOOZE_S = 60.0
#: A fresh window ignores clicks this long (a click or key meant for the window
#: underneath must never answer a prompt).
OPERATOR_ARM_MS = 700
OPERATOR_PREFS_NAME = "operator_popup.json"
OPERATOR_SEVERITIES = ("info", "action", "safety")
OPERATOR_NAME_MAX = 80
OPERATOR_NOTE_MAX = 2000
#: The longest answer the service keeps: a text prompt (operator_hitl.prompts) and an
#: attestation of a text optical-state slot (operator_hitl.optical_state). A longer one
#: is refused here with the limit, not cut short: the operator must see what is kept.
OPERATOR_TEXT_MAX = 4000
OPERATOR_SLOT_TEXT_MAX = 500


def _operator_user_agent() -> str:
    """``MAST-launcher/<version>``. The operator API records the client of every answer,
    so this popup's answers can be told apart from a script's (``Python-urllib/3.x``). It is
    an audit marker, not a credential: anything on this computer can send the same header."""
    try:
        from mast import __version__ as ver
    except Exception:  # noqa: BLE001 - the header must never stop a request
        ver = "unknown"
    return f"MAST-launcher/{ver}"


class OperatorFormError(ValueError):
    """The operator's input cannot become a valid answer. The text is already
    translated and is shown in the window as is."""


class OperatorApiError(Exception):
    """A failed call to the operator API.

    ``kind``: ``network`` (nothing answered), ``auth`` (401: the LAN credentials),
    ``forbidden`` (403: the service's write guard refused, ``str(exc)`` says why),
    ``invalid`` (400/422: it rejected the input, ``str(exc)`` says why), ``gone``
    (404/409/410: the prompt is no longer open), ``unsupported`` (this service has
    no operator API), ``server`` (anything else, e.g. 503 "could not save")."""

    def __init__(self, message: str, *, status: int | None = None, kind: str = "server") -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind


# ── prompts: normalisation, widgets, answers (pure) ──────────────────

def _op_float(value: object) -> float | None:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def clean_operator_name(name: object) -> str:
    """The operator's name as it is sent and remembered: one line, no padding."""
    return " ".join(str(name or "").split())[:OPERATOR_NAME_MAX]


def normalize_operator_prompt(raw: object) -> dict | None:
    """One prompt from the service (``OperatorPrompt.to_dict()`` plus ``answer``) as the
    popup uses it, or None when it cannot be told apart from another prompt (no id)."""
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get("id") or "").strip()
    if not pid:
        return None
    options = []
    for o in raw.get("options") or []:
        if isinstance(o, dict) and str(o.get("id") or "") != "":
            options.append({"id": str(o["id"]), "label": str(o.get("label") or o["id"])})
    severity = raw.get("severity")
    return {
        "id": pid,
        "kind": str(raw.get("kind") or ""),
        "title": str(raw.get("title") or "").strip() or pid,
        "message": str(raw.get("message") or ""),
        "options": options,
        "number_min": _op_float(raw.get("number_min")),
        "number_max": _op_float(raw.get("number_max")),
        "unit": str(raw.get("unit") or ""),
        "attest_slots": [str(s) for s in (raw.get("attest_slots") or []) if str(s)],
        "severity": severity if severity in OPERATOR_SEVERITIES else "action",
        "job_id": raw.get("job_id") or None,
        "skill": raw.get("skill") or None,
        "agent_id": raw.get("agent_id") or None,
        "timeout_s": _op_float(raw.get("timeout_s")),
        "created_at": str(raw.get("created_at") or ""),
        "status": str(raw.get("status") or "open"),
    }


def operator_prompt_fields(prompt: dict) -> dict:
    """What the window must collect for *prompt*; the Tk layer draws exactly this.

    ``mode``: ``confirm`` (Yes / No), ``options`` (one of ``options``; ``send`` says
    whether the id goes out as ``choice`` or as ``value``), ``number`` (``min`` /
    ``max`` / ``unit``), ``text``, ``flag`` (tick a box: an attestation with nothing
    to choose or type) or ``unsupported`` (a kind this launcher does not know: the
    window says to use the web page).

    An ``attest`` prompt with neither options nor a unit sets the slots to a bare
    ``value``; which kind of value depends on the slot, which the poller attaches as
    ``prompt["slots"]`` (looked up from the optical-state API). Without that
    information the safe reading is the common one, a signed flag."""
    kind = prompt.get("kind")
    options = [(o["id"], o["label"]) for o in prompt.get("options") or []]
    if kind == "confirm":
        return {"mode": "confirm"}
    if kind == "choice":
        return {"mode": "options", "options": options, "send": "choice"}
    if kind == "number":
        return {"mode": "number", "min": prompt.get("number_min"),
                "max": prompt.get("number_max"), "unit": prompt.get("unit") or ""}
    if kind == "text":
        return {"mode": "text", "max_len": OPERATOR_TEXT_MAX}
    if kind != "attest":
        return {"mode": "unsupported"}
    if options:
        return {"mode": "options", "options": options, "send": "choice"}
    if prompt.get("unit"):
        return {"mode": "number", "min": prompt.get("number_min"),
                "max": prompt.get("number_max"), "unit": prompt["unit"]}
    slots = prompt.get("slots") or {}
    first = slots.get(next(iter(prompt.get("attest_slots") or []), ""), None) or {}
    slot_kind = first.get("kind")
    if slot_kind == "text":
        return {"mode": "text", "max_len": OPERATOR_SLOT_TEXT_MAX}
    if slot_kind == "number":
        return {"mode": "number", "min": _op_float(first.get("min")),
                "max": _op_float(first.get("max")), "unit": str(first.get("unit") or "")}
    if slot_kind == "choice" and first.get("options"):
        return {"mode": "options", "send": "value",
                "options": [(str(o.get("id")), str(o.get("label") or o.get("id")))
                            for o in first["options"] if isinstance(o, dict) and o.get("id") is not None]}
    return {"mode": "flag"}


def _fmt_op_number(v: float) -> str:
    return f"{v:g}"


def parse_operator_number(text: object) -> float:
    """A number typed by the operator ('12.5', '12,5', '-3e-2'); never NaN or infinity."""
    s = str(text if text is not None else "").strip().replace("−", "-")
    if not s:
        raise OperatorFormError(_tr("请输入一个数值。", "Enter a number."))
    if "," in s and "." not in s and s.count(",") == 1:
        s = s.replace(",", ".")
    try:
        if "_" in s:
            raise ValueError(s)
        v = float(s)
    except ValueError:
        raise OperatorFormError(_tr("“{v}”不是数值。", "\"{v}\" is not a number.").format(v=s[:40])) from None
    if not math.isfinite(v):
        raise OperatorFormError(_tr("“{v}”不是有效的数值。", "\"{v}\" is not a valid number.").format(v=s[:40]))
    return v


def operator_range_hint(lo: float | None, hi: float | None, unit: str = "") -> str:
    """'Allowed range: 0 – 100 mW' for the box next to a number field ('' if unbounded)."""
    u = f" {unit}" if unit else ""
    if lo is not None and hi is not None:
        return _tr("允许范围：{lo} – {hi}{u}", "Allowed range: {lo} – {hi}{u}").format(
            lo=_fmt_op_number(lo), hi=_fmt_op_number(hi), u=u)
    if lo is not None:
        return _tr("下限：{lo}{u}", "Minimum: {lo}{u}").format(lo=_fmt_op_number(lo), u=u)
    if hi is not None:
        return _tr("上限：{hi}{u}", "Maximum: {hi}{u}").format(hi=_fmt_op_number(hi), u=u)
    return _tr("单位：{unit}", "Unit: {unit}").format(unit=unit) if unit else ""


def build_operator_answer(prompt: dict, form: dict, *, by: object,
                          source: str = OPERATOR_SOURCE) -> dict:
    """The ``POST .../answer`` body for what the operator filled in, or OperatorFormError.

    ``form``: ``choice`` (clicked Yes/No or the selected option id), ``text``,
    ``confirmed`` (the attest box) and ``note``. Mirrors ``prompts.check_answer`` on the
    service so a bad number or option is refused here, before any request is made;
    the service validates again."""
    name = clean_operator_name(by)
    if not name:
        raise OperatorFormError(_tr("请先填写你的姓名。", "Enter your name first."))
    fields = operator_prompt_fields(prompt)
    mode = fields["mode"]
    body: dict = {"by": name, "source": source}
    note = str(form.get("note") or "").strip()[:OPERATOR_NOTE_MAX]
    if note:
        body["note"] = note
    if mode == "unsupported":
        raise OperatorFormError(_tr("这类提示请在网页上回答。", "Answer this kind of prompt in the web page."))
    if mode == "confirm":
        if form.get("choice") not in ("yes", "no"):
            raise OperatorFormError(_tr("请选择“是”或“否”。", "Choose Yes or No."))
        body["choice"] = form["choice"]
    elif mode == "options":
        choice = form.get("choice")
        if not choice:
            raise OperatorFormError(_tr("请先选一项。", "Choose an option first."))
        if choice not in [i for i, _label in fields["options"]]:
            raise OperatorFormError(_tr("这不是可选项之一。", "That is not one of the options."))
        body["value" if fields.get("send") == "value" else "choice"] = choice
    elif mode == "number":
        v = parse_operator_number(form.get("text"))
        lo, hi, unit = fields.get("min"), fields.get("max"), fields.get("unit") or ""
        u = f" {unit}" if unit else ""
        if lo is not None and v < lo:
            raise OperatorFormError(_tr("{v}{u} 低于下限 {lo}{u}。", "{v}{u} is below the minimum {lo}{u}.").format(
                v=_fmt_op_number(v), lo=_fmt_op_number(lo), u=u))
        if hi is not None and v > hi:
            raise OperatorFormError(_tr("{v}{u} 高于上限 {hi}{u}。", "{v}{u} is above the maximum {hi}{u}.").format(
                v=_fmt_op_number(v), hi=_fmt_op_number(hi), u=u))
        body["value"] = v
    elif mode == "text":
        text = str(form.get("text") or "").strip()
        if not text:
            raise OperatorFormError(_tr("请先输入内容。", "Type your answer first."))
        limit = int(fields.get("max_len") or OPERATOR_TEXT_MAX)
        if len(text) > limit:
            raise OperatorFormError(_tr("回答有 {n} 个字符，最多只能 {m} 个；请删短一些。",
                                        "The answer is {n} characters long; at most {m} are allowed. "
                                        "Shorten it.").format(n=len(text), m=limit))
        body["value"] = text
    else:  # flag
        if not form.get("confirmed"):
            raise OperatorFormError(_tr("请先勾选确认框。", "Tick the box to confirm first."))
        body["value"] = True
    return body


def build_operator_dismiss(by: object, reason: object = "", *, source: str = OPERATOR_SOURCE) -> dict:
    """The ``POST .../dismiss`` body ("I cannot / will not do this"). ``source`` is sent
    explicitly: the service records ``web`` when it is left out."""
    name = clean_operator_name(by)
    if not name:
        raise OperatorFormError(_tr("请先填写你的姓名。", "Enter your name first."))
    body: dict = {"by": name, "source": source}
    why = str(reason or "").strip()[:OPERATOR_NOTE_MAX]
    if why:
        body["reason"] = why
    return body


def operator_severity_style(severity: str) -> dict:
    """Banner colours and text for a severity; ``safety`` must be unmistakable."""
    if severity == "safety":
        return {"bg": "#b71c1c", "fg": "#ffffff",
                "label": _tr("安全相关 — 需要你处理", "SAFETY — YOUR ACTION IS NEEDED")}
    if severity == "info":
        return {"bg": "#1565c0", "fg": "#ffffff", "label": _tr("供你知悉", "FOR YOUR INFORMATION")}
    return {"bg": "#a85a00", "fg": "#ffffff", "label": _tr("需要你操作", "OPERATOR ACTION NEEDED")}


def operator_alert_beeps(severity: str) -> list[tuple[int, int]]:
    """(frequency Hz, duration ms) pairs: urgent for safety, a double beep for an action."""
    if severity == "safety":
        return [(1000, 200), (760, 200), (1000, 200), (760, 200), (1000, 200)]
    if severity == "info":
        return [(660, 150)]
    return [(880, 170), (660, 170)]


def _load_winsound():
    """The stdlib ``winsound`` (Windows only); None anywhere else or if it is unusable."""
    try:
        import winsound
        return winsound
    except Exception:  # noqa: BLE001 - no sound is a valid environment, not an error
        return None


def play_operator_alert(severity: str, *, backend=None, bell: Callable[[], None] | None = None,
                        in_thread: bool = True) -> bool:
    """Play the alert for *severity*; True when a sound backend took it.

    ``winsound.Beep`` blocks for its duration, so it runs on a short-lived daemon
    thread. Without ``winsound`` (another OS, a stripped build) the optional *bell*
    (Tk's, called on the caller's thread) is the fallback; with neither, nothing
    happens and nothing raises."""
    ws = backend if backend is not None else _load_winsound()
    if ws is None:
        if bell is not None:
            try:
                bell()
                return True
            except Exception:  # noqa: BLE001
                return False
        return False
    beeps = operator_alert_beeps(severity)

    def run() -> None:
        # No bell fallback in here: Tk's bell belongs to the Tk thread, this is not it.
        try:
            for hz, ms in beeps:
                ws.Beep(int(hz), int(ms))
                time.sleep(0.04)
        except Exception:  # noqa: BLE001 - e.g. a machine with no audio device
            pass

    if in_thread:
        threading.Thread(target=run, daemon=True, name="operator-alert-sound").start()
    else:
        run()
    return True


def flash_taskbar(hwnd: int, *, user32=None, count: int = 6) -> bool:
    """Flash the taskbar button of *hwnd* until the window is brought forward
    (``FlashWindowEx`` ALL | TIMERNOFG). False when it cannot be done; never raises."""
    try:
        import ctypes
        from ctypes import wintypes

        class _FlashInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                        ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                        ("dwTimeout", wintypes.DWORD)]

        info = _FlashInfo(ctypes.sizeof(_FlashInfo), hwnd, 0x3 | 0xC, int(count), 0)
        u32 = user32 if user32 is not None else ctypes.windll.user32  # type: ignore[attr-defined]
        u32.FlashWindowEx(ctypes.byref(info))
        return True
    except Exception:  # noqa: BLE001 - cosmetic
        return False


def format_operator_time(iso: str, *, now: datetime.datetime | None = None) -> str:
    """An ISO timestamp (the service writes UTC) as local 'HH:MM:SS', with the date
    when it is not today. Unparseable text comes back unchanged."""
    if not iso:
        return ""
    try:
        t = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    local = t.astimezone()
    today = (now or datetime.datetime.now().astimezone()).date()
    return local.strftime("%H:%M:%S" if local.date() == today else "%m-%d %H:%M:%S")


def format_operator_source(prompt: dict) -> str:
    """'Agent: x · Job: y · Skill: z' for whichever of the three the prompt names."""
    parts = []
    if prompt.get("agent_id"):
        parts.append(_tr("智能体：{v}", "Agent: {v}").format(v=prompt["agent_id"]))
    if prompt.get("job_id"):
        parts.append(_tr("作业：{v}", "Job: {v}").format(v=prompt["job_id"]))
    if prompt.get("skill"):
        parts.append(_tr("技能：{v}", "Skill: {v}").format(v=prompt["skill"]))
    return " · ".join(parts)


def operator_status_text(*, enabled: bool, link: str, open_count: int, profile: str | None = None) -> str:
    """The one-line state under the launcher's "Operator prompts" switch. ``profile`` is the
    station the service reported ("optical" / "stm"; None = not heard yet)."""
    if not enabled:
        return _tr("已关闭：不会弹出提示窗。", "Off: no popup will appear.")
    if profile == "stm":
        return _tr("仅光学站使用：这是 STM 站，启动器不监听操作员提示。",
                   "Only used on optical stations: this is an STM station, so the launcher does not "
                   "listen for operator prompts.")
    if link == "ok":
        if open_count:
            return _tr("正在监听 — 有 {n} 条等你处理。", "Listening — {n} waiting for you.").format(n=open_count)
        return _tr("正在监听 — 暂时没有等你处理的提示。", "Listening — nothing is waiting for you.")
    if link == "unsupported":
        return _tr("这个版本的主服务还没有操作员提示功能。", "This main service has no operator prompts yet.")
    if link == "auth":
        return _tr("主服务拒绝了启动器的局域网账号密码。", "The main service refused the launcher's LAN credentials.")
    return _tr("等待主服务启动…", "Waiting for the main service...")


def operator_backoff_s(failures: int) -> float:
    """Seconds to wait after *failures* consecutive failed polls (0 = the normal rate)."""
    if failures <= 0:
        return OPERATOR_POLL_S
    return OPERATOR_BACKOFF_S[min(failures, len(OPERATOR_BACKOFF_S)) - 1]


def _operator_prefs_path(root: Path) -> Path:
    return root / "config" / OPERATOR_PREFS_NAME


def _load_operator_prefs(root: Path) -> dict:
    """``{"enabled": bool, "sound": bool, "operator_name": str}``; popups and sound on
    unless switched off, the name empty until the operator has typed one."""
    prefs = {"enabled": True, "sound": True, "operator_name": ""}
    try:
        data = json.loads(_operator_prefs_path(root).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return prefs
    if isinstance(data, dict):
        for key in ("enabled", "sound"):
            if isinstance(data.get(key), bool):
                prefs[key] = data[key]
        if isinstance(data.get("operator_name"), str):
            prefs["operator_name"] = clean_operator_name(data["operator_name"])
    return prefs


def _save_operator_prefs(root: Path, prefs: dict) -> None:
    try:
        p = _operator_prefs_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"enabled": bool(prefs.get("enabled")),
                                 "sound": bool(prefs.get("sound")),
                                 "operator_name": clean_operator_name(prefs.get("operator_name"))},
                                ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"[launcher] could not save {OPERATOR_PREFS_NAME}: {exc}")


# ── the queue (pure) ─────────────────────────────────────────────────

class OperatorPromptQueue:
    """The open prompts the launcher knows about, in the order they should be shown.

    ``update`` takes the service's whole open list (authoritative): ids it has not
    seen are *new*, ids that are no longer listed are *gone* (answered elsewhere, or
    cancelled by the agent), and a prompt answered or dismissed from here is
    remembered as closed for a while so a poll that was already in flight cannot
    bring it back. ``head`` is the next prompt to show: ``safety`` before ``action``
    before ``info``, oldest first within a severity; a snoozed prompt is skipped
    until its time is up. Not thread-safe by design: the Tk thread owns it."""

    _RANK = {"safety": 0, "action": 1, "info": 2}

    def __init__(self, *, snooze_s: float = OPERATOR_SNOOZE_S, closed_ttl_s: float = 300.0) -> None:
        self.snooze_s = float(snooze_s)
        self._closed_ttl_s = float(closed_ttl_s)
        self._entries: dict[str, dict] = {}
        self._closed: dict[str, float] = {}
        self._seq = itertools.count(1)

    def __len__(self) -> int:
        return len(self._entries)

    def ids(self) -> list[str]:
        return list(self._entries)

    def has(self, prompt_id: str) -> bool:
        return prompt_id in self._entries

    def get(self, prompt_id: str) -> dict | None:
        e = self._entries.get(prompt_id)
        return e["prompt"] if e else None

    def _add(self, prompt: dict, *, local: bool) -> None:
        self._entries[prompt["id"]] = {"prompt": prompt, "seq": next(self._seq),
                                       "snoozed_until": 0.0, "shown": 0, "local": local}

    def update(self, prompts: list[dict], now: float) -> dict:
        """Fold in a poll. Returns ``{"new": [ids], "gone": [ids]}``."""
        self._closed = {k: t for k, t in self._closed.items() if now - t < self._closed_ttl_s}
        incoming: dict[str, dict] = {}
        for p in prompts:
            if p.get("status", "open") == "open" and p["id"] not in self._closed:
                incoming[p["id"]] = p
        new = [pid for pid in incoming if pid not in self._entries]
        gone = [pid for pid, e in self._entries.items() if not e["local"] and pid not in incoming]
        for pid in new:
            self._add(incoming[pid], local=False)
        for pid, p in incoming.items():
            self._entries[pid]["prompt"] = p
        for pid in gone:
            del self._entries[pid]
        new.sort(key=self._priority)
        return {"new": new, "gone": gone}

    def add_local(self, prompt: dict) -> None:
        """A prompt that exists only here (the 'Test popup' button)."""
        self._add(prompt, local=True)

    def _priority(self, pid: str) -> tuple:
        e = self._entries[pid]
        p = e["prompt"]
        return (self._RANK.get(p.get("severity"), 1), p.get("created_at", ""), e["seq"])

    def head(self, now: float) -> dict | None:
        ready = [pid for pid, e in self._entries.items() if e["snoozed_until"] <= now]
        if not ready:
            return None
        return self._entries[min(ready, key=self._priority)]["prompt"]

    def waiting(self, exclude: str | None = None) -> int:
        """Open prompts other than *exclude* (snoozed ones included: they will be back)."""
        return len([pid for pid in self._entries if pid != exclude])

    def is_snoozed(self, prompt_id: str, now: float) -> bool:
        e = self._entries.get(prompt_id)
        return bool(e and e["snoozed_until"] > now)

    def snooze(self, prompt_id: str, now: float) -> None:
        e = self._entries.get(prompt_id)
        if e is not None:
            e["snoozed_until"] = now + self.snooze_s

    def mark_shown(self, prompt_id: str) -> int:
        """Count a presentation; returns how many times it has been shown before."""
        e = self._entries.get(prompt_id)
        if e is None:
            return 0
        e["shown"] += 1
        return e["shown"] - 1

    def resolve(self, prompt_id: str, now: float) -> None:
        """Answered or dismissed from here: drop it and refuse stale re-listings."""
        self._entries.pop(prompt_id, None)
        self._closed[prompt_id] = now


# ── the HTTP client ──────────────────────────────────────────────────

def _operator_detail_text(detail: object) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        msgs = [str(d.get("msg") if isinstance(d, dict) else d) for d in detail]
        return "; ".join(m for m in msgs if m)
    if isinstance(detail, dict):
        return str(detail.get("msg") or detail.get("message") or json.dumps(detail, ensure_ascii=False))
    return ""


class OperatorApi:
    """The operator endpoints of the local service, from this launcher.

    Mirrors how the launcher already talks to its own service (``_http_shutdown_request``):
    https with HTTP Basic when LAN mode is on (the self-signed certificate is not
    verified, the peer is 127.0.0.1), plain http otherwise. The scheme that last worked
    is remembered and the other one is only tried by reads. Writes carry
    ``Content-Type: application/json`` and a JSON body, and nothing a browser would send
    (no ``Origin``, no ``Sec-Fetch-*``): ``api/routes/operator.py::write_refusal`` treats
    a request without those headers as a programmatic client and only insists on the JSON
    body, whereas an ``Origin`` would put it through the browser checks (same origin, a
    loopback/IP ``Host``) it has no use for. No system proxy is ever used for the loopback."""

    def __init__(self, port: int = GUI_PORT, *, auth=None, host: str = GUI_HOST,
                 timeout: float = 4.0, post_timeout: float = 8.0) -> None:
        self.port = int(port)
        self.host = host
        self._auth = auth
        self.timeout = float(timeout)
        self.post_timeout = float(post_timeout)
        self._scheme: str | None = None
        self._openers: dict[str, object] = {}
        #: the last request found nobody listening (or none has been made yet): the next
        #: one starts with a quick TCP probe. Connecting to a closed port can take ~2 s on
        #: Windows, and both schemes would be tried; a poll must not spend 4 s on that.
        self._down = True

    # -- plumbing

    def _credentials(self) -> tuple[str, str]:
        a = self._auth() if callable(self._auth) else self._auth
        user, pwd = a or ("", "")
        return str(user or ""), str(pwd or "")

    def _scheme_order(self) -> list[str]:
        first = self._scheme or ("https" if all(self._credentials()) else "http")
        return [first, "http" if first == "https" else "https"]

    def _opener(self, scheme: str):
        """An opener with exactly the handlers this client needs, built by hand.

        ``urllib.request.build_opener`` adds a default ``HTTPSHandler`` unless it is given
        one, and that constructor creates a default SSL context, which loads the whole
        Windows certificate store: slow, and on the polling thread of a window that is
        already up it has crashed the interpreter (0x80000003 inside ``ssl.load_default_certs``).
        The https opener gets an unverified context instead (the peer is 127.0.0.1 with a
        self-signed certificate), which loads nothing; the http one has no TLS at all.
        Redirects are not followed: nothing here is ever redirected, and a 3xx must not
        carry Basic credentials somewhere else."""
        import ssl
        import urllib.request as ur

        op = self._openers.get(scheme)
        if op is None:
            op = ur.OpenerDirector()
            handlers: list = [ur.ProxyHandler({}), ur.UnknownHandler(), ur.HTTPDefaultErrorHandler(),
                              ur.HTTPErrorProcessor()]
            handlers.append(ur.HTTPSHandler(context=ssl._create_unverified_context()) if scheme == "https"
                            else ur.HTTPHandler())
            for h in handlers:
                op.add_handler(h)
            self._openers[scheme] = op
        return op

    @staticmethod
    def _http_error(exc) -> OperatorApiError:
        try:
            raw = exc.read(8192).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            raw = ""
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        detail = ""
        if isinstance(data, dict):
            detail = _operator_detail_text(data.get("detail", data.get("error", data.get("message"))))
        detail = detail or raw[:200].strip() or f"HTTP {exc.code}"
        code = exc.code
        kind = ("auth" if code == 401 else "forbidden" if code == 403 else "invalid" if code in (400, 422)
                else "gone" if code in (404, 409, 410) else "server")
        return OperatorApiError(detail, status=code, kind=kind)

    def _request(self, method: str, path: str, body: dict | None = None, *,
                 timeout: float | None = None, any_scheme: bool = True):
        import base64
        import http.client
        import urllib.error
        import urllib.request

        if self._down and not port_open(self.host, self.port, timeout=0.6):
            raise OperatorApiError(f"nothing is listening on {self.host}:{self.port}", kind="network")
        user, pwd = self._credentials()
        headers = {"Accept": "application/json", "User-Agent": _operator_user_agent()}
        if user and pwd:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        order = self._scheme_order()
        if not any_scheme:
            order = order[:1]   # a write is never replayed on the other scheme
        problem: BaseException | None = None
        for scheme in order:
            base = f"{scheme}://{self.host}:{self.port}"
            req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
            try:
                with self._opener(scheme).open(req, timeout=timeout or self.timeout) as resp:  # noqa: S310
                    raw = resp.read(4_000_000)
            except urllib.error.HTTPError as exc:
                raise self._http_error(exc) from None
            except (OSError, http.client.HTTPException) as exc:   # URLError is an OSError
                problem = exc
                if not port_open(self.host, self.port, timeout=0.3):
                    break          # nobody is listening: the other scheme would only wait again
                continue
            self._scheme = scheme
            self._down = False
            try:
                return json.loads(raw.decode("utf-8", "replace") or "null")
            except ValueError:
                raise OperatorApiError("the service sent something that is not JSON", kind="server") from None
        self._down = True
        raise OperatorApiError(f"{type(problem).__name__}: {problem}", kind="network")

    # -- the endpoints

    def list_open(self, limit: int = 50) -> list[dict]:
        """The open prompts, normalised, oldest first."""
        try:
            data = self._request("GET", f"{OPERATOR_PROMPTS_API}?status=open&limit={int(limit)}")
        except OperatorApiError as exc:
            if exc.status == 404:
                raise OperatorApiError(str(exc), status=404, kind="unsupported") from None
            raise
        items = data.get("prompts") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise OperatorApiError("unexpected answer to the open-prompts request", kind="server")
        out = []
        for raw in items:
            p = normalize_operator_prompt(raw)
            if p is not None and p["status"] == "open":
                out.append(p)
        return out

    def station_profile(self) -> str:
        """``"optical"`` or ``"stm"``: the profile the running service IS (``GET /api/health``).

        The operator API exists only on an optical station, so this decides whether there is
        anything to listen to at all. A service that answers but does not say (an older build, a
        garbled body, an error status) reads as ``"stm"``: the behaviour every machine had before
        profiles. Nothing answering (``network``) or refusing the credentials (``auth``) raises,
        because then the profile is simply not known yet."""
        try:
            data = self._request("GET", OPERATOR_HEALTH_API)
        except OperatorApiError as exc:
            if exc.kind in ("network", "auth"):
                raise
            return "stm"
        profile = data.get("station_profile") if isinstance(data, dict) else None
        return "optical" if str(profile or "").strip().lower() == "optical" else "stm"

    def slots(self) -> dict[str, dict]:
        """``{slot_id: slot}`` from the optical-state API (what an attestation sets)."""
        data = self._request("GET", OPERATOR_OPTICAL_STATE_API)
        slots = data.get("slots") if isinstance(data, dict) else None
        return {str(s["id"]): s for s in slots or [] if isinstance(s, dict) and s.get("id")}

    def answer(self, prompt_id: str, body: dict) -> dict:
        from urllib.parse import quote
        got = self._request("POST", f"{OPERATOR_PROMPTS_API}/{quote(prompt_id, safe='')}/answer",
                            body, timeout=self.post_timeout, any_scheme=False)
        return got if isinstance(got, dict) else {}

    def dismiss(self, prompt_id: str, body: dict) -> dict:
        from urllib.parse import quote
        got = self._request("POST", f"{OPERATOR_PROMPTS_API}/{quote(prompt_id, safe='')}/dismiss",
                            body, timeout=self.post_timeout, any_scheme=False)
        return got if isinstance(got, dict) else {}


# ── the poller ───────────────────────────────────────────────────────

class OperatorPoller:
    """Finds out what the station is, then polls ``api.list_open`` and hands what it learned
    to *sink* (a thread-safe put).

    The operator API is only installed on an optical station, so nothing asks for prompts
    until ``GET /api/health`` has said ``optical``. On an STM station, or a service that does
    not say, the poller does not poll at all: it only reads the profile again every
    ``PROFILE_RECHECK_S`` (a service restarted into another profile is noticed within that
    long), and an optical one is re-read the same way while it is polled.

    ``sink`` receives ``("profile", "optical" | "stm")`` when the profile is first heard or
    changes, ``("poll", [prompts])`` on a successful poll and ``("link", state)`` on a failure,
    where state is the ``OperatorApiError.kind``. A service that is not up yet is the normal
    case, so ``network`` failures are silent and just slow the polling down (2, 4, 8, 15,
    then 30 s); anything else is logged once until the link recovers. After a failure to reach
    the service the profile is asked again first: whatever answers next may be another service."""

    SLOT_TTL_S = 60.0
    PROFILE_RECHECK_S = 60.0

    def __init__(self, api: OperatorApi, sink: Callable[[tuple], None], *, enabled=None,
                 log: Callable[[str], None] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.api = api
        self.sink = sink
        self._enabled = enabled
        self._log = log
        self._clock = clock
        self.failures = 0
        self._logged: set[str] = set()
        self._slots: dict[str, dict] = {}
        self._slots_at = -1e9
        #: "optical" | "stm" once read, None until then (and after the service was lost)
        self.profile: str | None = None
        self._profile_at = -1e9
        self._announced: str | None = None      # the last profile handed to the sink

    def _say(self, msg: str) -> None:
        if self._log is not None:
            try:
                self._log(msg)
            except Exception:  # noqa: BLE001
                pass

    def _attach_slots(self, prompts: list[dict]) -> None:
        """An attestation with nothing to choose or type sets slots to a bare value;
        what kind of value depends on the slot, so look the slots up (cached)."""
        need = [p for p in prompts if p["kind"] == "attest" and not p["options"] and not p["unit"]
                and p["attest_slots"]]
        if not need:
            return
        now = self._clock()
        if now - self._slots_at > self.SLOT_TTL_S:
            try:
                self._slots = self.api.slots()
                self._slots_at = now
            except Exception:  # noqa: BLE001 - without it a plain flag is the safe reading
                self._slots_at = now - self.SLOT_TTL_S + 5.0   # try again in 5 s
        for p in need:
            p["slots"] = {sid: self._slots[sid] for sid in p["attest_slots"] if sid in self._slots}

    def _failed(self, kind: str, text: str) -> float:
        """A failed round: count it, say so once per kind (a service that is not up yet is
        normal and stays silent) and return how long to wait before the next."""
        self.failures += 1
        if kind in ("network", "auth"):
            self.profile = None        # whatever answers next may be a different service
        if kind != "network" and kind not in self._logged:
            self._logged.add(kind)
            self._say(f"[operator prompts] {kind}: {text}")
        self.sink(("link", kind))
        return OPERATOR_UNSUPPORTED_RETRY_S if kind == "unsupported" else operator_backoff_s(self.failures)

    def _recovered(self) -> None:
        if self._logged:
            self._say("[operator prompts] the connection to the main service is back")
            self._logged.clear()
        self.failures = 0

    def step(self) -> float:
        """One round; returns the seconds to wait before the next.

        First the station profile (when it is not known, or a minute old); an STM station
        stops there and is only looked at again later. An optical one goes on to ask for the
        open prompts."""
        now = self._clock()
        if self.profile is None or now - self._profile_at >= self.PROFILE_RECHECK_S:
            try:
                profile = self.api.station_profile()
            except OperatorApiError as exc:
                return self._failed(exc.kind, str(exc))
            except Exception as exc:  # noqa: BLE001 - the thread must outlive any surprise
                return self._failed("server", f"unexpected error while reading the station profile: "
                                              f"{type(exc).__name__}: {exc}")
            self.profile, self._profile_at = profile, now
            if profile != self._announced:
                self._announced = profile
                self.sink(("profile", profile))
        if self.profile != "optical":
            self._recovered()
            return max(self.PROFILE_RECHECK_S - (now - self._profile_at), 0.5)
        try:
            prompts = self.api.list_open()
        except OperatorApiError as exc:
            return self._failed(exc.kind, str(exc))
        except Exception as exc:  # noqa: BLE001 - the thread must outlive any surprise
            return self._failed("server", f"unexpected error while polling: {type(exc).__name__}: {exc}")
        self._recovered()
        self._attach_slots(prompts)
        self.sink(("poll", prompts))
        return OPERATOR_POLL_S

    def run(self, stop: threading.Event) -> None:
        delay = 0.0
        while not stop.wait(delay):
            if self._enabled is not None and not self._enabled():
                delay = 1.0
                continue
            try:
                delay = self.step()
            except Exception:  # noqa: BLE001
                delay = OPERATOR_BACKOFF_S[-1]


# ── the Tk-thread controller ─────────────────────────────────────────

class OperatorPopupController:
    """Glue on the Tk thread: poller results -> queue -> window -> answer calls.

    *app* is the LauncherApp (or anything with ``win``, ``root_dir``, ``tk``, ``ttk``,
    ``_lan_auth_pair`` and ``_log``). The API, clock, view factory, sound and thread
    starter are arguments so a test can run the whole flow without a display, a
    network or a real thread."""

    PUMP_MS = 250

    def __init__(self, app, *, api: OperatorApi | None = None,
                 clock: Callable[[], float] = time.monotonic, view_factory=None, play=None,
                 start_thread: Callable[[Callable[[], None]], None] | None = None,
                 snooze_s: float = OPERATOR_SNOOZE_S) -> None:
        self.app = app
        self.win = app.win
        self.tk = getattr(app, "tk", None)
        self.ttk = getattr(app, "ttk", None)
        self.dpi_scale = float(getattr(app, "_dpi_scale", 1.0) or 1.0)
        self.root_dir = app.root_dir
        self.clock = clock
        self.prefs = _load_operator_prefs(self.root_dir)
        self.api = api or OperatorApi(GUI_PORT, auth=app._lan_auth_pair)
        self.snooze_s = snooze_s
        self.prompts = OperatorPromptQueue(snooze_s=snooze_s)
        self.inbox: queue.Queue = queue.Queue()
        self.view = None
        self.link = "network" if self.prefs["enabled"] else "off"
        #: the station the service reported: "optical" | "stm"; None = not heard yet
        self.profile: str | None = None
        self.status_var = None
        self._profile_view: Callable[[bool], None] | None = None
        self._view_factory = view_factory or self._make_view
        self._play = play or play_operator_alert
        self._start_thread = start_thread or self._spawn
        self._stopped = False
        self._poll_stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pump_id = None
        self._demo_n = 0
        self.poller = OperatorPoller(self.api, self.inbox.put, enabled=lambda: self.prefs["enabled"],
                                     log=lambda m: print(f"[launcher] {m}"))

    # -- lifecycle

    @staticmethod
    def _spawn(fn: Callable[[], None]) -> threading.Thread:
        t = threading.Thread(target=fn, daemon=True, name="operator-popup")
        t.start()
        return t

    def start(self) -> None:
        """Begin polling (the thread idles while the switch is off) and draining results."""
        self._stopped = False
        self._poll_stop = threading.Event()
        stop = self._poll_stop
        self._thread = self._spawn(lambda: self.poller.run(stop))   # always a real thread: run() blocks
        self._schedule_pump()
        self._refresh_status()

    def stop(self) -> None:
        self._stopped = True
        self._poll_stop.set()
        self._close_view()
        if self._pump_id is not None:
            try:
                self.win.after_cancel(self._pump_id)
            except Exception:  # noqa: BLE001 - the window may be gone already
                pass
            self._pump_id = None

    def _schedule_pump(self) -> None:
        try:
            self._pump_id = self.win.after(self.PUMP_MS, self._pump)
        except Exception:  # noqa: BLE001
            self._pump_id = None

    def _pump(self) -> None:
        if self._stopped:
            return
        try:
            self.pump_once()
        except Exception as exc:  # noqa: BLE001 - the pump must never die over one bad event
            print(f"[launcher] operator popup error: {type(exc).__name__}: {exc}")
        self._schedule_pump()

    # -- results arriving from the worker threads

    def pump_once(self) -> None:
        """Handle everything the poller and the call threads have reported."""
        while True:
            try:
                event = self.inbox.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "profile":
                self._on_profile(event[1])
            elif kind == "poll":
                self._on_poll(event[1])
            elif kind == "link":
                if self.prefs["enabled"]:
                    self.link = event[1]
                    self._refresh_status()
            elif kind == "result":
                self._on_result(*event[1:])
        if not self._stopped and self.prefs["enabled"]:
            self._sync_view()      # a snoozed prompt may be due back

    def _on_profile(self, profile: str) -> None:
        """The service said what the station is. Only an optical station has operator prompts:
        on an STM one nothing is listened for, whatever an earlier service left on screen goes,
        and the launcher's section folds away (see ``bind_profile_view``)."""
        if not self.prefs["enabled"]:
            return
        if profile != self.profile:
            self.profile = profile
            if profile != "optical":
                self._close_view()
                self.prompts = OperatorPromptQueue(snooze_s=self.snooze_s)
            self._apply_profile_view()
        self._refresh_status()

    def _controls_visible(self) -> bool:
        """Whether the launcher shows its operator controls: on an optical station, and always
        while popups are switched off (the switch must stay reachable). A profile that is not
        known yet counts as STM: the controls appear when the service says it is an optical one."""
        return (not self.prefs["enabled"]) or self.profile == "optical"

    def _apply_profile_view(self) -> None:
        cb = self._profile_view
        if cb is None:
            return
        try:
            cb(self._controls_visible())
        except Exception:  # noqa: BLE001 - the widgets behind it were rebuilt
            self._profile_view = None

    def _on_poll(self, prompts: list[dict]) -> None:
        if not self.prefs["enabled"]:
            return
        now = self.clock()
        self.link = "ok"
        res = self.prompts.update(prompts, now)
        if self.view is not None and self.view.prompt_id in res["gone"]:
            self._note(_tr("提示“{t}”已不再待处理（已在别处答复、被取消或已超时），窗口已关闭。",
                           "The prompt \"{t}\" is no longer open (answered elsewhere, cancelled or timed out); "
                           "its window was closed.").format(
                               t=self.view.prompt.get("title", self.view.prompt_id)))
            self._close_view()
        if res["new"]:     # one sound per poll, for the most severe newcomer
            severities = [(self.prompts.get(pid) or {}).get("severity", "action") for pid in res["new"]]
            self._alert(min(severities, key=lambda s: OperatorPromptQueue._RANK.get(s, 1)))
        self._sync_view()
        self._refresh_status()

    def _on_result(self, action: str, prompt_id: str, ok: bool, kind: str, detail: str) -> None:
        view = self.view if (self.view is not None and self.view.prompt_id == prompt_id) else None
        if ok or kind == "gone":
            title = (self.prompts.get(prompt_id) or {}).get("title", prompt_id)
            self.prompts.resolve(prompt_id, self.clock())
            if view is not None:
                self._close_view()
            if kind == "gone":
                self._note(_tr("提示“{t}”已在别处处理。", "The prompt \"{t}\" was already handled elsewhere.").format(t=title))
            elif action == "dismiss":
                self._note(_tr("已拒绝提示“{t}”。", "Declined the prompt \"{t}\".").format(t=title))
            else:
                self._note(_tr("已回答提示“{t}”。", "Answered the prompt \"{t}\".").format(t=title))
            self._sync_view(quiet=True)
            self._refresh_status()
            return
        if view is None:
            return
        view.set_busy(False)
        if kind in ("invalid", "server", "forbidden") and detail:
            view.show_error(detail)          # the service said why (a bad value, a refused write, no disk space)
        elif kind == "auth":
            view.show_error(_tr("主服务拒绝了启动器的账号密码，请检查局域网账号密码。",
                                "The main service refused the launcher's LAN credentials; check them."))
        else:
            view.show_error(_tr("无法联系主服务，请稍后再试。", "Could not reach the main service; try again.")
                            + (f" ({detail})" if detail else ""))

    # -- what is on screen

    def _make_view(self, prompt: dict, waiting: int):
        return OperatorPromptView(self, prompt, waiting=waiting, name=self.prefs["operator_name"])

    def _close_view(self) -> None:
        view, self.view = self.view, None
        if view is not None:
            try:
                view.close()
            except Exception:  # noqa: BLE001
                pass

    def _sync_view(self, *, quiet: bool = False) -> None:
        """Make the window match the queue: keep the current one while its prompt is
        open and not snoozed, otherwise show the next. A prompt coming back from a snooze
        sounds again unless *quiet*."""
        now = self.clock()
        if self.view is not None:
            pid = self.view.prompt_id
            if self.prompts.has(pid) and not self.prompts.is_snoozed(pid, now):
                self.view.update_waiting(self.prompts.waiting(exclude=pid))
                return
            self._close_view()
        head = self.prompts.head(now)
        if head is None:
            return
        returning = self.prompts.mark_shown(head["id"]) > 0
        try:
            self.view = self._view_factory(head, self.prompts.waiting(exclude=head["id"]))
            self.view.show()
        except Exception as exc:  # noqa: BLE001 - a broken window must not kill the pump
            print(f"[launcher] could not show the operator prompt window: {type(exc).__name__}: {exc}")
            self._close_view()
            self.prompts.snooze(head["id"], now)     # retry later, not every pump tick
            return
        if returning and not quiet:
            self._alert(head["severity"])

    def _alert(self, severity: str) -> None:
        if not self.prefs["sound"]:
            return
        try:
            self._play(severity, bell=getattr(self.win, "bell", None))
        except Exception:  # noqa: BLE001 - no sound is never an error
            pass

    def _note(self, msg: str) -> None:
        try:
            self.app._log(msg)
        except Exception:  # noqa: BLE001
            print(f"[launcher] {msg}")

    def _refresh_status(self) -> None:
        var = self.status_var
        if var is None:
            return
        try:
            var.set(operator_status_text(enabled=bool(self.prefs["enabled"]), link=self.link,
                                         open_count=len(self.prompts), profile=self.profile))
        except Exception:  # noqa: BLE001 - the widget behind it was rebuilt
            self.status_var = None

    # -- what the window asks for

    def submit(self, prompt_id: str, form: dict, name: str) -> None:
        """The window's answer button. A bad input is refused here, before any request."""
        prompt = self.prompts.get(prompt_id)
        if prompt is None:
            self._close_view()
            return
        try:
            body = build_operator_answer(prompt, form, by=name)
        except OperatorFormError as exc:
            self._show_error(prompt_id, str(exc))
            return
        self.remember_name(body["by"])
        self._send(prompt, "answer", body)

    def reject(self, prompt_id: str, name: str, reason: str = "") -> None:
        """"I cannot do this": the service closes the prompt as dismissed."""
        prompt = self.prompts.get(prompt_id)
        if prompt is None:
            self._close_view()
            return
        try:
            body = build_operator_dismiss(name, reason)
        except OperatorFormError as exc:
            self._show_error(prompt_id, str(exc))
            return
        self.remember_name(body["by"])
        self._send(prompt, "dismiss", body)

    def ignore(self, prompt_id: str) -> None:
        """"Ignore for now": hide the window, answer nothing; it comes back later."""
        self.prompts.snooze(prompt_id, self.clock())
        self._close_view()
        self._sync_view(quiet=True)

    def _show_error(self, prompt_id: str, text: str) -> None:
        if self.view is not None and self.view.prompt_id == prompt_id:
            self.view.show_error(text)

    def _send(self, prompt: dict, action: str, body: dict) -> None:
        pid = prompt["id"]
        if prompt.get("_demo"):      # the Test popup button: nothing leaves the launcher
            self.inbox.put(("result", action, pid, True, "", ""))
            return
        if self.view is not None and self.view.prompt_id == pid:
            self.view.set_busy(True)
        call = self.api.answer if action == "answer" else self.api.dismiss

        def work() -> None:
            try:
                call(pid, body)
                self.inbox.put(("result", action, pid, True, "", ""))
            except OperatorApiError as exc:
                self.inbox.put(("result", action, pid, False, exc.kind, str(exc)))
            except Exception as exc:  # noqa: BLE001 - the window must always get an answer
                self.inbox.put(("result", action, pid, False, "network", f"{type(exc).__name__}: {exc}"))

        self._start_thread(work)

    # -- settings and the launcher's own section

    def remember_name(self, name: str) -> None:
        clean = clean_operator_name(name)
        if clean and clean != self.prefs.get("operator_name"):
            self.prefs["operator_name"] = clean
            _save_operator_prefs(self.root_dir, self.prefs)

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.prefs["enabled"]:
            return
        self.prefs["enabled"] = enabled
        _save_operator_prefs(self.root_dir, self.prefs)
        if enabled:
            self.link = "network"
        else:
            self._close_view()
            self.prompts = OperatorPromptQueue(snooze_s=self.snooze_s)
            self.link = "off"
        self._apply_profile_view()
        self._refresh_status()

    def set_sound(self, sound: bool) -> None:
        self.prefs["sound"] = bool(sound)
        _save_operator_prefs(self.root_dir, self.prefs)

    def bind_status_var(self, var) -> None:
        self.status_var = var
        self._refresh_status()

    def bind_profile_view(self, callback: Callable[[bool], None]) -> None:
        """*callback(visible)* shows or hides the launcher's operator controls (see
        ``_controls_visible``); it is called at once with what is known so far and again
        whenever that changes."""
        self._profile_view = callback
        self._apply_profile_view()

    def show_demo(self) -> None:
        """The "Test popup" button: a local prompt, to check the window and the sound."""
        self._demo_n += 1
        demo = normalize_operator_prompt({
            "id": f"demo-{self._demo_n}", "kind": "confirm", "severity": "action",
            "title": _tr("测试提示", "Test prompt"),
            "message": _tr("这是启动器的测试弹窗：没有任何东西发给主服务。填好姓名后点“是”或“否”即可关闭。",
                           "This is the launcher's test popup: nothing is sent to the main service. "
                           "Enter your name and click Yes or No to close it."),
            "agent_id": "launcher", "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        assert demo is not None
        demo["_demo"] = True
        self.prompts.add_local(demo)
        self._alert(demo["severity"])
        self._sync_view(quiet=True)

    def prepare_ui_rebuild(self) -> None:
        """The launcher is about to destroy and rebuild its widgets (language switch):
        close the window first so nothing touches a dead widget."""
        self._close_view()
        self.status_var = None
        self._profile_view = None

    def after_ui_rebuild(self) -> None:
        if self.prefs["enabled"]:
            self._sync_view(quiet=True)
        self._refresh_status()


# ── the window ───────────────────────────────────────────────────────

class OperatorPromptView:
    """One topmost window for one prompt. It draws what ``operator_prompt_fields``
    describes and collects the operator's input; every decision (validation, the
    request, what happens next) belongs to the controller."""

    def __init__(self, controller: OperatorPopupController, prompt: dict, *,
                 waiting: int = 0, name: str = "") -> None:
        tk, ttk = controller.tk, controller.ttk
        self.controller = controller
        self.prompt = prompt
        self.prompt_id = prompt["id"]
        self.fields = operator_prompt_fields(prompt)
        self.choice_var = tk.StringVar(value="")
        self.confirm_var = tk.BooleanVar(value=False)
        self.value_var = tk.StringVar(value="")
        self.name_var = tk.StringVar(value=name or "")
        self.note_var = tk.StringVar(value="")
        self.waiting_var = tk.StringVar(value="")
        self.message_var = tk.StringVar(value="")
        self.text_widget = None
        self._action_buttons: list = []
        self._busy = False
        self._armed = False
        self._arm_id = None
        scale = controller.dpi_scale
        wrap = int(520 * scale)
        sev = operator_severity_style(prompt["severity"])

        top = self.top = tk.Toplevel(controller.win)
        top.withdraw()
        top.title(_tr("MAST — 需要操作员", "MAST — operator action needed"))
        logo = getattr(controller.app, "_logo_img", None)
        if logo is not None:
            try:
                top.iconphoto(False, logo)
            except Exception:  # noqa: BLE001 - cosmetic
                pass
        top.protocol("WM_DELETE_WINDOW", self._on_ignore)
        top.minsize(int(480 * scale), int(260 * scale))

        band = tk.Frame(top, background=sev["bg"])
        band.pack(fill="x")
        tk.Label(band, text=sev["label"], background=sev["bg"], foreground=sev["fg"],
                 font=("Segoe UI", 11, "bold"), padx=14, pady=7).pack(side="left")
        tk.Label(band, textvariable=self.waiting_var, background=sev["bg"], foreground=sev["fg"],
                 font=("Segoe UI", 10), padx=14).pack(side="right")
        self.update_waiting(waiting)

        body = ttk.Frame(top, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        row = 0
        ttk.Label(body, text=prompt["title"], font=("Segoe UI", 13, "bold"),
                  wraplength=wrap, justify="left").grid(row=row, column=0, sticky="we")
        row += 1
        message = prompt.get("message", "")
        if message:
            lines = sum(max(1, math.ceil(len(ln) / 70)) for ln in message.splitlines() or [""])
            msg = tk.Text(body, wrap="word", height=min(12, max(2, lines)), width=1, relief="flat",
                          borderwidth=0, background=top.cget("background"), font=("Segoe UI", 10),
                          cursor="arrow", takefocus=0)
            msg.insert("1.0", message)
            msg.configure(state="disabled")
            msg.grid(row=row, column=0, sticky="we", pady=(8, 0))
            row += 1
        meta = [format_operator_source(prompt)]
        posted = format_operator_time(prompt.get("created_at", ""))
        if posted:
            when = _tr("发出时间：{t}", "Posted: {t}").format(t=posted)
            if prompt.get("timeout_s"):
                when += " · " + _tr("智能体最多等待 {s} 秒", "The agent waits up to {s} s").format(
                    s=_fmt_op_number(prompt["timeout_s"]))
            meta.append(when)
        if prompt.get("attest_slots"):
            meta.append(_tr("将记录到光学状态：{slots}", "Recorded in the optical state: {slots}").format(
                slots=", ".join(prompt["attest_slots"])))
        meta_text = "\n".join(m for m in meta if m)
        if meta_text:
            ttk.Label(body, text=meta_text, foreground="#666", wraplength=wrap,
                      justify="left").grid(row=row, column=0, sticky="we", pady=(8, 0))
            row += 1

        mode = self.fields["mode"]
        ctrl = ttk.Frame(body)
        ctrl.grid(row=row, column=0, sticky="we", pady=(12, 0))
        ctrl.columnconfigure(0, weight=1)
        row += 1
        self.focus_widget = None
        if mode == "options":
            for i, (oid, label) in enumerate(self.fields["options"]):
                ttk.Radiobutton(ctrl, text=label, value=oid, variable=self.choice_var).grid(
                    row=i, column=0, sticky="w", pady=2)
        elif mode == "number":
            line = ttk.Frame(ctrl)
            line.grid(row=0, column=0, sticky="w")
            entry = ttk.Entry(line, textvariable=self.value_var, width=16)
            entry.pack(side="left")
            self.focus_widget = entry
            unit = self.fields.get("unit") or ""
            if unit:
                ttk.Label(line, text=unit).pack(side="left", padx=(6, 0))
            hint = operator_range_hint(self.fields.get("min"), self.fields.get("max"), unit)
            if hint:
                ttk.Label(ctrl, text=hint, foreground="#666").grid(row=1, column=0, sticky="w", pady=(4, 0))
        elif mode == "text":
            self.text_widget = tk.Text(ctrl, height=4, width=1, wrap="word", font=("Segoe UI", 10))
            self.text_widget.grid(row=0, column=0, sticky="we")
            self.focus_widget = self.text_widget
        elif mode == "flag":
            ttk.Checkbutton(ctrl, variable=self.confirm_var,
                            text=_tr("我确认以上情况属实", "I confirm that the above is true")).grid(
                row=0, column=0, sticky="w")
        elif mode == "unsupported":
            ttk.Label(ctrl, text=_tr("这类提示请在网页上回答。", "Answer this kind of prompt in the web page."),
                      foreground=sev["bg"], wraplength=wrap, justify="left").grid(row=0, column=0, sticky="w")

        ident = ttk.Frame(body)
        ident.grid(row=row, column=0, sticky="we", pady=(14, 0))
        ident.columnconfigure(1, weight=1)
        row += 1
        ttk.Label(ident, text=_tr("你的姓名（必填）", "Your name (required)")).grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        self.name_entry = ttk.Entry(ident, textvariable=self.name_var)
        self.name_entry.grid(row=0, column=1, sticky="we")
        ttk.Label(ident, text=_tr("备注（可选）", "Note (optional)")).grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ttk.Entry(ident, textvariable=self.note_var).grid(row=1, column=1, sticky="we", pady=(6, 0))

        self.message_label = tk.Label(body, textvariable=self.message_var, foreground="#b71c1c",
                                      wraplength=wrap, justify="left", anchor="w")
        self.message_label.grid(row=row, column=0, sticky="we", pady=(8, 0))
        row += 1

        btns = ttk.Frame(body)
        btns.grid(row=row, column=0, sticky="we", pady=(10, 0))
        btns.columnconfigure(0, weight=1)
        left = ttk.Frame(btns)
        left.grid(row=0, column=0, sticky="w")
        right = ttk.Frame(btns)
        right.grid(row=0, column=1, sticky="e")
        if mode == "confirm":
            self._action_button(left, _tr("是", "Yes"), lambda: self._on_answer("yes"))
            self._action_button(left, _tr("否", "No"), lambda: self._on_answer("no"))
        elif mode != "unsupported":
            self._action_button(left, _tr("确认并提交", "Confirm and submit") if mode == "flag"
                                else _tr("提交", "Submit"), self._on_answer)
        if mode != "unsupported":
            self._action_button(right, _tr("无法完成 / 拒绝", "Can't do it / decline"), self._on_reject)
        ttk.Button(right, text=_tr("暂时忽略", "Ignore for now"), command=self._on_ignore).pack(
            side="left", padx=(8, 0))

    # -- construction helpers

    def _action_button(self, parent, text: str, command) -> None:
        btn = self.controller.ttk.Button(parent, text=text, command=command)
        btn.pack(side="left", padx=(0, 8))
        btn.state(["disabled"])       # armed after OPERATOR_ARM_MS
        self._action_buttons.append(btn)

    # -- showing and hiding

    def show(self) -> None:
        top = self.top
        top.update_idletasks()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        w = max(top.winfo_reqwidth(), int(540 * self.controller.dpi_scale))
        top.geometry(f"+{max(0, (sw - w) // 2)}+{max(0, int(sh * 0.10))}")
        top.deiconify()
        try:
            top.attributes("-topmost", True)
            top.lift()
        except Exception:  # noqa: BLE001 - not every window manager has it
            pass
        self._flash()
        self._arm_id = top.after(OPERATOR_ARM_MS, self._arm)
        if not self.name_var.get().strip():
            self.name_entry.focus_set()
        elif self.focus_widget is not None:
            self.focus_widget.focus_set()

    def _flash(self) -> None:
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.top.winfo_id()) or self.top.winfo_id()  # type: ignore[attr-defined]
            flash_taskbar(int(hwnd))
        except Exception:  # noqa: BLE001 - cosmetic
            pass

    def _arm(self) -> None:
        self._arm_id = None
        self._armed = True
        if not self._busy:
            for b in self._action_buttons:
                b.state(["!disabled"])

    def close(self) -> None:
        try:
            if self._arm_id is not None:
                self.top.after_cancel(self._arm_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.top.destroy()
        except Exception:  # noqa: BLE001
            pass

    # -- state the controller pushes in

    def update_waiting(self, n: int) -> None:
        self.waiting_var.set(_tr("还有 {n} 个在排队", "{n} more waiting").format(n=n) if n > 0 else "")

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        for b in self._action_buttons:
            b.state(["disabled"] if (self._busy or not self._armed) else ["!disabled"])
        if self._busy:
            self.message_label.configure(foreground="#444")
            self.message_var.set(_tr("正在发送…", "Sending..."))

    def show_error(self, text: str) -> None:
        self.message_label.configure(foreground="#b71c1c")
        self.message_var.set(text)

    # -- what the window sends back

    def _form(self, choice: str | None = None) -> dict:
        text = self.value_var.get()
        if self.text_widget is not None:
            text = self.text_widget.get("1.0", "end-1c")
        return {"choice": choice if choice is not None else (self.choice_var.get() or None),
                "text": text, "confirmed": bool(self.confirm_var.get()), "note": self.note_var.get()}

    def _on_answer(self, choice: str | None = None) -> None:
        if not self._armed or self._busy:
            return
        self.controller.submit(self.prompt_id, self._form(choice), self.name_var.get())

    def _on_reject(self) -> None:
        if not self._armed or self._busy:
            return
        if not clean_operator_name(self.name_var.get()):
            self.show_error(_tr("请先填写你的姓名。", "Enter your name first."))
            return
        from tkinter import messagebox
        if not messagebox.askyesno(
                _tr("无法完成", "Decline"),
                _tr("确定这一步你无法完成吗？智能体会收到“已拒绝”。",
                    "Are you sure you cannot do this? The agent will be told it was declined."),
                parent=self.top):
            return
        self.controller.reject(self.prompt_id, self.name_var.get(), self.note_var.get())

    def _on_ignore(self) -> None:
        self.controller.ignore(self.prompt_id)


# ── Launcher window ──────────────────────────────────────────────────

class LauncherApp:
    """Tk control panel for the MAST service."""

    def __init__(self, root_dir: Path, *, start_minimized: bool = False) -> None:
        self.root_dir = root_dir
        # Auto-start (open-on-boot) launches with --minimized so the window
        # doesn't pop every login — it goes straight to the tray (resolves the
        # auto-start ↔ ×-to-tray interaction). Applied at the end of __init__.
        self._start_minimized = bool(start_minimized)
        # Clear any LAN-auth handoff files left behind by a prior session that
        # crashed between writing creds and the child consuming them.
        _sweep_lan_auth_handoff(root_dir)
        self.proc: subprocess.Popen | None = None
        self.push_proc: subprocess.Popen | None = None
        self.log_lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._push_reader_thread: threading.Thread | None = None
        # LAN IP is detected once at start-up; manual refresh re-reads it.
        self._lan_ip: str = _lan_ip()
        # Tailscale status (cross-network remote access) is detected off-thread —
        # shelling out to the CLI can take up to a few seconds, so we never do it
        # on the Tk thread. None = "not yet detected"; a TailscaleStatus once known.
        self._ts_status = None
        # LAN access toggle is sticky across runs (persisted via lan_auth.env
        # presence). Treat "have credentials" as "user wants LAN enabled".
        u, p = get_lan_auth(root_dir)
        self._lan_enabled: bool = bool(u and p)
        self._lan_user: str = u
        self._lan_pass: str = p

        # Remote AI (Claude Code / Codex on another computer): its server runs in this
        # process, on the Tailscale address, while the switch is on and Tailscale is ready.
        # The remote AI's threads read _remote_ai_prefs, never the Tk variables.
        self._remote_ai_prefs: dict = _load_remote_ai_prefs(root_dir)
        self._remote_ai = None          # mast_mcp.remote_server.RemoteServer while serving
        self._remote_ai_error: str = ""
        self._remote_ai_failed_ip: str = ""

        # Vision model warm-up is gated by legacy_features_enabled (default off).
        # The launcher no longer shows a Vision panel; marker helpers stay for
        # the rare legacy-on path and for unit tests of stdout markers.
        self._vision_warn_suppressed: bool = _load_vision_warn_suppressed(root_dir)
        # Set while we're tracking a service warm-up via stdout markers.
        self._vision_watching: bool = False

        # Build UI
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.ttk = ttk

        self.win = tk.Tk()
        # Title shows version + release date for at-a-glance "what am I running"
        try:
            from mast import __version__ as _ver, __release_date__ as _rel
            self.win.title(_tr(f"MAST 启动器 — v{_ver}  ({_rel})",
                               f"MAST Launcher — v{_ver}  ({_rel})"))
        except Exception:
            self.win.title(_tr("MAST 启动器", "MAST Launcher"))

        # ── DPI-aware sizing & font scaling ─────────────────────────
        # When DPI awareness is enabled (called from main() before us),
        # winfo_fpixels('1i') reports the OS-side DPI. Default is 96; 200%
        # display = 192. We use this to:
        #   1. set Tk's internal scaling so fonts auto-grow
        #   2. scale our default geometry (was 820 fixed)
        # We cap the result so a 4K screen doesn't get a giant window.
        try:
            dpi = float(self.win.winfo_fpixels("1i"))
        except Exception:
            dpi = 96.0
        self._dpi_scale = max(1.0, dpi / 96.0)  # 1.0=100%, 2.0=200%
        # Tk's default scaling is ~1.333 (96 dpi → 1.333 px/pt); we want it
        # proportional to actual DPI so text doesn't stay 96-dpi-sized.
        try:
            self.win.tk.call("tk", "scaling", 1.333 * self._dpi_scale)
        except Exception:
            pass
        # NOTE: ttk fonts are sized in *points*; Tk's scaling factor (set
        # above) auto-converts pt → px for the active DPI, so we just use
        # natural point sizes (10, 12, …) below and don't pre-multiply.
        # Scale-derived default geometry.  Base = 820×820 logical px @ 96 dpi.
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        base_w = int(820 * self._dpi_scale)
        base_h = int(820 * self._dpi_scale)
        # Cap at ~70% of screen so it never fills the monitor.
        win_w = max(820, min(base_w, int(sw * 0.70)))
        win_h = max(680, min(base_h, int(sh * 0.85)))
        self.win.geometry(f"{win_w}x{win_h}")
        self.win.minsize(int(720 * self._dpi_scale), int(680 * self._dpi_scale))
        # × never exits (Nanonis-style) — it minimizes to tray. The only real
        # exit is the "stop service and quit launcher" button / tray "Quit launcher", which gracefully stops the
        # service + frees VRAM (see _minimize_to_tray / _shutdown_launcher).
        self.win.protocol("WM_DELETE_WINDOW", self._minimize_to_tray)

        # ── Logo (window icon + taskbar) ────────────────────────────
        # iconphoto needs a tk.PhotoImage. Failing silently is fine — the
        # logo is purely cosmetic; missing asset shouldn't crash startup.
        try:
            logo_path = _resource_path(LOGO_PNG_RELPATH)
            if logo_path.exists():
                self._logo_img = tk.PhotoImage(file=str(logo_path))
                self.win.iconphoto(True, self._logo_img)
        except Exception:
            self._logo_img = None  # noqa: F841

        # ── Bind IPC listener for single-instance "bring foreground" ──
        self._ipc_sock = None
        self._ipc_port = 0
        bind = _bind_ipc_port()
        if bind is not None:
            self._ipc_sock, self._ipc_port = bind
            # Write lock with our IPC port so a future second instance can
            # connect to us instead of starting up.
            _write_launcher_lock(self.root_dir, self._ipc_port)
            # Schedule periodic IPC poll
            self.win.after(500, self._poll_ipc)
        else:
            print("[launcher] could not bind IPC port — single-instance ping disabled")

        # ── Tray icon support (pystray + Pillow). Optional. ──
        self._tray_icon = None
        self._tray_thread = None
        self._init_tray()

        # Operator prompts: poll the local service and pop up what needs a person.
        # Built before the UI because the launcher's "Operator prompts" section reads it.
        self._operator_popup = OperatorPopupController(self)

        self._build_ui()
        self._refresh_status()
        # Kick off Tailscale detection in the background (updates the remote-access
        # status/URL rows once it returns, without blocking UI startup), then keep
        # re-detecting slowly so a late connect / account switch shows up.
        self._detect_tailscale_async()
        self._schedule_tailscale_redetect()
        # Low-frequency GitHub releases poll (cached; min 12 h between auto checks).
        self.win.after(1500, lambda: self._check_github_release(force=False))
        self._operator_popup.start()

        # Open-on-boot: go straight to the tray instead of popping the window.
        # Deferred so the tray icon thread + window are fully up first.
        if self._start_minimized:
            self.win.after(400, self._minimize_to_tray)

    # ── Layout ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        ttk = self.ttk
        tk = self.tk
        pad = {"padx": 12, "pady": 6}
        # Label wraplengths must scale with DPI (the window is ~820*dpi_scale wide).
        # A fixed 100%-px wraplength forces ugly early wrapping at 200%.
        wl = lambda base: int(base * getattr(self, "_dpi_scale", 1.0))  # noqa: E731

        # Wrap the entire launcher UI in a Canvas + vertical Scrollbar so
        # the layout is usable on small / low-DPI screens (1366×768 laptop
        # was clipping the bottom buttons before this).
        scroll_container = ttk.Frame(self.win)
        scroll_container.pack(fill="both", expand=True)

        canvas = tk.Canvas(scroll_container, highlightthickness=0,
                           borderwidth=0)
        canvas.pack(side="left", fill="both", expand=True)

        vscroll = ttk.Scrollbar(scroll_container, orient="vertical",
                                command=canvas.yview)
        vscroll.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=vscroll.set)

        outer = ttk.Frame(canvas, padding=12)
        outer_window = canvas.create_window((0, 0), window=outer, anchor="nw")

        def _on_outer_configure(_event=None):
            # Inner frame changed → recompute scroll region.
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            # Stretch the inner frame to match canvas width so widgets that
            # use `fill="x"` actually span the available width.
            canvas.itemconfigure(outer_window, width=event.width)

        outer.bind("<Configure>", _on_outer_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Mouse-wheel scrolling via self._scroll_canvas so a language-switch
        # rebuild can retarget without stacking bind_all handlers.
        self._scroll_canvas = canvas

        def _on_mousewheel(event):
            c = getattr(self, "_scroll_canvas", None)
            if c is None:
                return
            try:
                c.yview_scroll(int(-event.delta / 120), "units")
            except Exception:
                pass

        self.win.bind_all("<MouseWheel>", _on_mousewheel)

        # ── Language selector (applies immediately; no restart) ──────
        lang_bar = ttk.Frame(outer)
        lang_bar.pack(fill="x", padx=12, pady=(0, 2))
        self._lang_codes = list(_SUPPORTED_LANGS)
        self._lang_display = [_LANG_LABELS[c] for c in self._lang_codes]
        self._lang_combo_var = self.tk.StringVar(
            value=_LANG_LABELS.get(_LANG, _LANG_LABELS["en"])
        )
        lang_combo = ttk.Combobox(
            lang_bar, textvariable=self._lang_combo_var,
            values=self._lang_display, state="readonly", width=12,
        )
        # side=right: first packed sits at the right edge.
        lang_combo.pack(side="right")
        lang_combo.bind("<<ComboboxSelected>>", self._on_language_selected)
        ttk.Label(
            lang_bar, text=_tr("语言 / Language:", "Language:",
                               ja="言語 / Language:", de="Sprache / Language:",
                               fr="Langue / Language:", es="Idioma / Language:",
                               cs="Jazyk / Language:"),
            foreground="#666",
        ).pack(side="right", padx=(0, 6))

        # ── Service controls (main GUI only) ─────────────────────────
        svc = ttk.LabelFrame(outer, text=_tr("服务", "Service"), padding=12)
        svc.pack(fill="x", **pad)
        for col in range(3):
            svc.columnconfigure(col, weight=1)

        self.svc_status_var = self.tk.StringVar(value=_tr("● 主服务: 检测中…", "● Main service: checking..."))
        self.svc_status_lbl = ttk.Label(
            svc, textvariable=self.svc_status_var, font=("Segoe UI", 10, "bold")
        )
        self.svc_status_lbl.grid(row=0, column=0, columnspan=3, sticky="w")

        # Start / restart / stop the main GUI service.
        self.start_btn = ttk.Button(svc, text=_tr("启动主服务", "Start main service"), command=self.start_service)
        self.start_btn.grid(row=1, column=0, padx=(0, 6), pady=(8, 0), sticky="we")
        self.restart_btn = ttk.Button(svc, text=_tr("重启主服务", "Restart main service"), command=self.restart_service)
        self.restart_btn.grid(row=1, column=1, padx=6, pady=(8, 0), sticky="we")
        self.stop_btn = ttk.Button(svc, text=_tr("关闭主服务", "Stop main service"), command=self.stop_service)
        self.stop_btn.grid(row=1, column=2, padx=(6, 0), pady=(8, 0), sticky="we")

        # Full-exit affordance lives HERE in the Service frame (the window × only minimizes to
        # tray — see _minimize_to_tray — so this is the only real way out).
        # Gracefully stops the main service (frees VRAM) behind a progress bar.
        self.shutdown_btn = ttk.Button(
            svc, text=_tr("关闭主服务并退出启动器", "Stop main service and quit launcher"), command=self._shutdown_launcher)
        self.shutdown_btn.grid(row=2, column=0, columnspan=3, padx=0, pady=(8, 0),
                               sticky="we")

        # Vision model panel removed from the launcher UI (warm-up is still
        # gated server-side by legacy_features_enabled; see run_service).

        # ── LAN access toggle (binds 0.0.0.0 + HTTP basic auth) ────
        lan = ttk.LabelFrame(outer, text=_tr("局域网 / 远程访问", "LAN / remote access"), padding=12)
        lan.pack(fill="x", **pad)
        lan.columnconfigure(1, weight=1)

        self.lan_enabled_var = self.tk.BooleanVar(value=self._lan_enabled)
        self.lan_chk = ttk.Checkbutton(
            lan, text=_tr("启用 (其它电脑可通过局域网 IP 或 Tailscale 远程访问 MAST)", "Enable (other computers can reach MAST via the LAN IP or Tailscale)"),
            variable=self.lan_enabled_var, command=self._on_lan_toggle,
        )
        self.lan_chk.grid(row=0, column=0, columnspan=3, sticky="w")

        self.lan_info_var = self.tk.StringVar(value=_tr("(未启用)", "(disabled)"))
        ttk.Label(lan, textvariable=self.lan_info_var, foreground="#666",
                  wraplength=wl(320), justify="left").grid(
            row=1, column=0, sticky="w", pady=(4, 0),
        )
        # LAN HTTP auth + web admin PIN live together — both are "passwords
        # for this machine", and the admin PIN used to be buried under options.
        lan_pwd = ttk.Frame(lan)
        lan_pwd.grid(row=1, column=1, columnspan=2, sticky="e", pady=(4, 0))
        ttk.Button(
            lan_pwd, text=_tr("修改局域网账号密码", "Change LAN username/password"),
            command=self._configure_lan_auth,
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            lan_pwd, text=_tr("设置高级管理密码", "Set admin password"),
            command=self._set_admin_pin,
        ).pack(side="left")
        # Tailscale (cross-network remote access) status line — set by _refresh_status from the
        # off-thread detection. Reuses the SAME 0.0.0.0 bind as LAN access; this
        # line just tells the user the address to open on the OTHER computer.
        self.ts_info_var = self.tk.StringVar(value=_tr("🌐 Tailscale: 检测中…", "🌐 Tailscale: checking..."))
        ttk.Label(lan, textvariable=self.ts_info_var, foreground="#666",
                  wraplength=wl(470), justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 0),
        )

        # ── Remote AI access (SSH over Tailscale) ───────────────────
        # Install / uninstall wrappers around setup-ai-ssh.ps1. After install the
        # script writes ~/.ssh/ai-remote.handoff.json; we show those two paste
        # lines here so closing the elevated window does not lose them.
        rai = ttk.LabelFrame(outer, text=_tr("AI 远程访问（Claude Code / Codex）",
                                             "Remote AI access (Claude Code / Codex)"), padding=12)
        rai.pack(fill="x", **pad)
        rai.columnconfigure(0, weight=1)
        ttk.Label(
            rai, wraplength=wl(560), justify="left",
            text=_tr("让另一台电脑上的 Claude Code / Codex 通过你自己的 Tailscale 网络、用 SSH "
                     "控制这台电脑。「一键安装」会打开设置向导（需要管理员权限）；装好后下面会保留要发给 "
                     "Claude Code / Codex 的两行，关掉脚本窗口也不会丢。「一键卸载」撤销密钥并关闭 SSH。",
                     "Let Claude Code / Codex on another computer control this one over SSH through "
                     "your own Tailscale network. \"One-click install\" opens the setup wizard "
                     "(administrator rights required); afterwards the two lines for Claude Code / "
                     "Codex stay below even if you close the script window. \"One-click uninstall\" "
                     "revokes the key and turns SSH off."),
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        rai_btns = ttk.Frame(rai)
        rai_btns.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(
            rai_btns, text=_tr("一键安装 AI 远程访问…", "One-click install AI remote access..."),
            command=self._setup_ai_ssh,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            rai_btns, text=_tr("一键卸载…", "One-click uninstall..."),
            command=self._uninstall_ai_ssh,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            rai_btns, text=_tr("刷新连接信息", "Refresh connection info"),
            command=self._refresh_remote_ai_handoff,
        ).pack(side="left")

        self.rai_prompt_var = self.tk.StringVar(value="")
        self.rai_note_var = self.tk.StringVar(value="")
        self.rai_status_var = self.tk.StringVar(
            value=_tr("尚未安装（或还没有连接信息）。",
                      "Not installed yet (or no connection info on disk).")
        )
        ttk.Label(rai, textvariable=self.rai_status_var, foreground="#666",
                  wraplength=wl(560), justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(rai, text=_tr("发给 Claude Code / Codex 的话：",
                                "Line for Claude Code / Codex:"),
                  foreground="#444").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(rai, textvariable=self.rai_prompt_var, state="readonly").grid(
            row=4, column=0, sticky="we", pady=(2, 0))
        ttk.Button(rai, text=_tr("复制", "Copy"),
                   command=lambda: self._copy_string_var(self.rai_prompt_var)).grid(
            row=4, column=1, sticky="e", padx=(6, 0), pady=(2, 0))
        ttk.Label(rai, text=_tr("私钥说明：", "Private-key note:"),
                  foreground="#444").grid(row=5, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(rai, textvariable=self.rai_note_var, state="readonly").grid(
            row=6, column=0, sticky="we", pady=(2, 0))
        ttk.Button(rai, text=_tr("复制", "Copy"),
                   command=lambda: self._copy_string_var(self.rai_note_var)).grid(
            row=6, column=1, sticky="e", padx=(6, 0), pady=(2, 0))
        self._refresh_remote_ai_handoff()

        # ── Operator prompts (desktop popup + sound for structured requests) ──
        # An AI that needs a person (change a waveplate, confirm the laser area is
        # safe, read a value off a dial) posts a prompt; this section decides whether
        # a window and a sound reach the operator on THIS computer. The popup itself
        # is OperatorPopupController / OperatorPromptView.
        pop = self._operator_popup
        opf = ttk.LabelFrame(outer, text=_tr("操作员提示", "Operator prompts"), padding=12)
        opf.pack(fill="x", **pad)
        opf.columnconfigure(0, weight=1)
        # Everything but the status line sits in one frame so that it can fold away on an
        # STM station (_show_operator_controls): operator prompts exist on optical stations only.
        body = ttk.Frame(opf)
        body.grid(row=0, column=0, sticky="we")
        body.columnconfigure(1, weight=1)
        self._op_body = body
        ttk.Label(
            body, wraplength=wl(560), justify="left",
            text=_tr("AI 需要你动手或确认（换镜片、确认激光区安全……）时，在这台电脑上弹出置顶窗口并响铃，"
                     "不必一直盯着网页。网页上的「操作员请求」页会同时显示同样的提示，谁先答谁算。",
                     "When an AI needs you to do or confirm something (change a waveplate, confirm the "
                     "laser area is safe...), a window pops up on top of everything on this computer "
                     "with a sound, so nobody has to keep watching a web page. The Operator Requests "
                     "page shows the same prompts; whoever answers first counts."),
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self.op_enabled_var = self.tk.BooleanVar(value=bool(pop.prefs["enabled"]))
        self.op_sound_var = self.tk.BooleanVar(value=bool(pop.prefs["sound"]))
        self.op_name_var = self.tk.StringVar(value=pop.prefs["operator_name"])
        self.op_status_var = self.tk.StringVar(value="")
        ttk.Checkbutton(
            body, text=_tr("弹出提示窗", "Pop up a window"),
            variable=self.op_enabled_var, command=self._on_operator_toggle,
        ).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            body, text=_tr("同时响铃", "Play a sound"),
            variable=self.op_sound_var, command=self._on_operator_toggle,
        ).grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(8, 0))
        ttk.Button(
            body, text=_tr("测试弹窗", "Test popup"), command=pop.show_demo,
        ).grid(row=1, column=2, sticky="e", pady=(8, 0))
        ttk.Label(body, text=_tr("你的姓名（弹窗里自动填入）", "Your name (filled in for you in the popup)")).grid(
            row=2, column=0, sticky="w", pady=(8, 0))
        name_entry = ttk.Entry(body, textvariable=self.op_name_var)
        name_entry.grid(row=2, column=1, columnspan=2, sticky="we", padx=(12, 0), pady=(8, 0))
        name_entry.bind("<FocusOut>", lambda _e: pop.remember_name(self.op_name_var.get()))
        name_entry.bind("<Return>", lambda _e: pop.remember_name(self.op_name_var.get()))
        ttk.Label(opf, textvariable=self.op_status_var, foreground="#666",
                  wraplength=wl(560), justify="left").grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        pop.bind_status_var(self.op_status_var)
        pop.bind_profile_view(self._show_operator_controls)

        # ── GUI URLs (2 rows: main local/LAN) ──────────────────────
        gui = ttk.LabelFrame(outer, text=_tr("GUI 网址", "GUI URLs"), padding=12)
        gui.pack(fill="x", **pad)
        gui.columnconfigure(1, weight=1)

        self.url_var     = self.tk.StringVar(value=f"{self._gui_scheme()}://{GUI_HOST}:{GUI_PORT}")
        self.lan_url_var = self.tk.StringVar(value="")
        # Tailscale (cross-network) address to open on the OTHER computer; filled by
        # _refresh_status only when Tailscale is ready AND remote access is on.
        self.ts_url_var  = self.tk.StringVar(value="")

        # Each row: label, var, "open" callback (with running-check), "copy" callback.
        # require_running=None means we don't gate (browser will just fail).
        url_rows = [
            (_tr("主服务 - 本机", "Main service - this PC"),     self.url_var,
             lambda: self._open_url(self.url_var.get(), require_running=gui_running),
             lambda: self._copy_url_var(self.url_var)),
            (_tr("主服务 - 局域网", "Main service - LAN"),   self.lan_url_var,
             lambda: self._open_url(self.lan_url_var.get(), require_running=gui_running),
             lambda: self._copy_url_var(self.lan_url_var)),
            (_tr("远程 - Tailscale", "Remote - Tailscale"),  self.ts_url_var,
             lambda: self._open_url(self.ts_url_var.get(), require_running=gui_running),
             lambda: self._copy_url_var(self.ts_url_var)),
        ]
        for i, (label, var, open_cb, copy_cb) in enumerate(url_rows):
            ttk.Label(gui, text=label, width=24, anchor="w").grid(
                row=i, column=0, sticky="w", padx=(0, 6), pady=2,
            )
            entry = ttk.Entry(gui, textvariable=var, state="readonly")
            entry.grid(row=i, column=1, sticky="we", padx=(0, 8), pady=2)
            ttk.Button(gui, text=_tr("浏览器打开", "Open in browser"), command=open_cb).grid(
                row=i, column=2, padx=(0, 6), pady=2,
            )
            ttk.Button(gui, text=_tr("复制", "Copy"), command=copy_cb).grid(
                row=i, column=3, padx=(0, 0), pady=2,
            )

        # ── Status indicators ───────────────────────────────────────
        st = ttk.LabelFrame(outer, text=_tr("状态指示", "Status"), padding=12)
        st.pack(fill="x", **pad)
        st.columnconfigure(1, weight=1)

        # Header row: hint on left, manual refresh button on right.
        st_header = ttk.Frame(st)
        st_header.grid(row=0, column=0, columnspan=3, sticky="we", pady=(0, 6))
        st_header.columnconfigure(0, weight=1)
        ttk.Label(
            st_header,
            text=_tr("（不自动刷新；启停服务后会自动更新；其它情况点「刷新」）", "(Not refreshed automatically; updates after the service starts or stops; otherwise click Refresh)"),
            foreground="#888", wraplength=wl(460), justify="left",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(st_header, text=_tr("刷新", "Refresh"), command=self._manual_refresh).grid(
            row=0, column=1, sticky="e",
        )

        # Recommended mode uses Claude Code / Codex via the MAST plugin — no
        # per-provider API keys in the launcher. Keep Nanonis connectivity only.
        self.indicators: dict[str, dict] = {}
        rows: list[tuple[str, str]] = [
            ("nanonis", _tr("Nanonis 仪器", "Nanonis instrument")),
        ]
        for i, (key, label) in enumerate(rows, start=1):
            dot = ttk.Label(st, text="●", font=("Segoe UI", 12, "bold"), foreground="#888")
            dot.grid(row=i, column=0, sticky="w", padx=(0, 8), pady=2)
            text = ttk.Label(st, text=label)
            text.grid(row=i, column=1, sticky="w", pady=2)
            value = ttk.Label(st, text="—", foreground="#888")
            value.grid(row=i, column=2, sticky="e", pady=2)
            self.indicators[key] = {"dot": dot, "value": value, "text": text}

        # API key configuration UI is intentionally not shown: recommended
        # operation goes through Claude Code / Codex. Keys can still be placed
        # under api key/ by hand if a lab needs the legacy built-in agents.

        # ── Updates → public GitHub releases (HamsterPark/MAST) ──────
        upd = ttk.LabelFrame(outer, text=_tr("更新", "Updates"), padding=12)
        upd.pack(fill="x", **pad)
        for col in (0, 1):
            upd.columnconfigure(col, weight=1)
        self.update_status_var = self.tk.StringVar(
            value=_tr("● 正在检查 GitHub 新版本…", "● Checking GitHub for a new version...")
        )
        self.update_status_lbl = ttk.Label(
            upd, textvariable=self.update_status_var, font=("Segoe UI", 10, "bold"),
            wraplength=wl(560), justify="left",
        )
        self.update_status_lbl.grid(row=0, column=0, columnspan=2, sticky="w")
        upd_btns = ttk.Frame(upd)
        upd_btns.grid(row=1, column=0, columnspan=2, sticky="we", pady=(8, 0))
        for col in range(4):
            upd_btns.columnconfigure(col, weight=1)
        ttk.Button(
            upd_btns, text=_tr("检查更新", "Check for updates"),
            command=lambda: self._check_github_release(force=True),
        ).grid(row=0, column=0, padx=(0, 4), sticky="we")
        ttk.Button(
            upd_btns, text=_tr("打开发布页…", "Open releases…"),
            command=self._open_public_releases,
        ).grid(row=0, column=1, padx=4, sticky="we")
        ttk.Button(
            upd_btns, text=_tr("提意见…", "Send feedback…"),
            command=self._open_public_issues,
        ).grid(row=0, column=2, padx=4, sticky="we")
        ttk.Button(
            upd_btns, text=_tr("复制发布页链接", "Copy releases URL"),
            command=lambda: self._copy_text(PUBLIC_RELEASES_URL),
        ).grid(row=0, column=3, padx=(4, 0), sticky="we")
        try:
            from mast import __version__ as _cv, __release_date__ as _cd
        except Exception:
            _cv, _cd = "?", ""
        ttk.Label(
            upd,
            text=_tr(f"当前版本 v{_cv}  ({_fmt_release_date(_cd)})",
                     f"Current version v{_cv}  ({_fmt_release_date(_cd)})"),
            foreground="#666",
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Button(
            upd, text=_tr("当前版本更新说明", "Release notes for this version"),
            command=self._open_release_notes,
        ).grid(row=2, column=1, padx=(4, 0), pady=(8, 0), sticky="we")
        self._apply_github_release_cache_to_ui()

        # ── Launcher options: system tray + start with Windows ────────────────────────
        opts = ttk.LabelFrame(outer, text=_tr("启动器选项", "Launcher options"), padding=12)
        opts.pack(fill="x", **pad)
        for col in (0, 1):
            opts.columnconfigure(col, weight=1)
        self.autostart_var = self.tk.BooleanVar(value=_startup_enabled())
        ttk.Checkbutton(
            opts,
            text=_tr("开机自启 (HKCU Run)", "Start with Windows (HKCU Run)"),
            variable=self.autostart_var,
            command=self._on_autostart_toggle,
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))

        ttk.Button(
            opts, text=_tr("最小化到托盘", "Minimize to tray"), command=self._minimize_to_tray,
        ).grid(row=0, column=1, sticky="we", padx=4)
        # Admin password lives next to the LAN password (see LAN frame above).

        ttk.Label(
            opts,
            text=_tr(
                "× 关闭窗口 = 缩到状态栏，主服务继续运行（避免误关泄漏显存）；"
                "要退出请点「服务」栏的「关闭主服务并退出启动器」。"
                "局域网账号密码与高级管理密码在上方「局域网 / 远程访问」里一起设置。",
                "× closes the window to the system tray and keeps the main service running "
                "(so VRAM is not leaked by an accidental close); to quit, use \"Stop main "
                "service and quit launcher\" in the Service frame. LAN credentials and the "
                "admin password are set together under \"LAN / remote access\" above."),
            foreground="#888",
            wraplength=wl(600),
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # ── Service log (scrolling text — large by default) ─────────
        log_frame = ttk.LabelFrame(outer, text=_tr("服务日志", "Service log"), padding=4)
        log_frame.pack(fill="both", expand=True, padx=12, pady=(8, 4))

        # Header row with "open logs dir" button — survives launcher restarts
        # because logs are also written to disk under experiments/logs/.
        log_header = ttk.Frame(log_frame)
        log_header.grid(row=0, column=0, columnspan=2, sticky="we", pady=(0, 4))
        log_header.columnconfigure(0, weight=1)
        ttk.Label(
            log_header,
            text=_tr(f"（同时持久化到 {self.root_dir / 'experiments' / 'logs'}）",
                     f"(Also saved to {self.root_dir / 'experiments' / 'logs'})"),
            foreground="#888", wraplength=wl(480), justify="left",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            log_header, text=_tr("打开日志目录", "Open log folder"), command=self._open_logs_dir,
        ).grid(row=0, column=1, sticky="e", padx=(0, 6))
        ttk.Button(
            log_header, text=_tr("清屏", "Clear"), command=self._clear_log_widget,
        ).grid(row=0, column=2, sticky="e")

        # height=20 makes the log meaningfully readable without scrolling.
        # fill="both" + expand=True lets it grow when the user enlarges the
        # window (was previously capped by sibling frames absorbing slack).
        self.log_text = self.tk.Text(
            log_frame, height=20, wrap="none", font=("Consolas", 10),
            state="disabled", background="#1e1e1e", foreground="#d4d4d4",
            insertbackground="#d4d4d4",
        )
        self.log_text.grid(row=1, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        yscroll.grid(row=1, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(log_frame, orient="horizontal", command=self.log_text.xview)
        xscroll.grid(row=2, column=0, sticky="we")
        self.log_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        log_frame.rowconfigure(1, weight=1)
        log_frame.columnconfigure(0, weight=1)

        # Skip the bootstrap lines when rebuilding after a language switch —
        # the preserved log already has them, and re-emitting clutters it.
        if not getattr(self, "_ui_bootstrapped", False):
            self._log(_tr("MAST 启动器就绪。", "MAST launcher ready."))
            self._log(_tr(f"用户数据目录: {self.root_dir}", f"User data folder: {self.root_dir}"))
            if self._lan_ip:
                self._log(_tr(f"检测到本机局域网 IP: {self._lan_ip}",
                              f"Detected LAN IP of this PC: {self._lan_ip}"))
            else:
                self._log(_tr("未检测到局域网 IP（可能未联网或仅本地接口）。",
                              "No LAN IP detected (possibly offline or loopback-only)."))
            if self._lan_enabled:
                self._log(_tr(f"局域网访问已启用 (用户名: {self._lan_user})",
                              f"LAN access enabled (username: {self._lan_user})"))
            self._ui_bootstrapped = True

    # ── Service control ──────────────────────────────────────────────

    def _make_subprocess_env(self) -> dict[str, str]:
        """Env for a child subprocess. Hands off LAN auth securely when enabled.

        The password is NOT placed in the env block (see LAN_ENV_AUTH_FILE
        rationale near the top of the module). Instead we write the creds to a
        fresh owner-only temp file per call and pass only its path; the child
        reads + deletes it on boot. Each call gets a unique file, so the main
        service and admin children never contend for the same one.
        """
        env = os.environ.copy()
        # The service answers in the launcher's language when a client does not
        # name one (the web UI does, per browser; see mast/i18n.py).
        env.setdefault("MAST_LANG", _LANG)
        # Always strip any inherited LAN vars first so a stale parent env can
        # never leak into a child after the user toggles LAN off.
        for k in (LAN_ENV_ENABLE, LAN_ENV_USER, LAN_ENV_PASS, LAN_ENV_AUTH_FILE):
            env.pop(k, None)
        if self._lan_enabled and self._lan_user and self._lan_pass:
            env[LAN_ENV_ENABLE] = "1"
            env[LAN_ENV_USER]   = self._lan_user   # username is not a secret
            try:
                handoff = write_lan_auth_handoff(
                    self.root_dir, self._lan_user, self._lan_pass
                )
                env[LAN_ENV_AUTH_FILE] = str(handoff)
            except Exception as exc:
                # If we can't write the secure file, fall back to the legacy
                # plaintext env var so LAN access still works (the child
                # otherwise sees no creds and safely stays on 127.0.0.1, but
                # then the user's LAN toggle silently does nothing). Log the
                # downgrade so the weaker secret handling is visible.
                self._log(_tr(f"⚠️ 安全降级：无法写入临时凭据文件 ({exc})，"
                              "改用环境变量传递密码。",
                              f"⚠️ Security downgrade: could not write the temporary credentials file "
                              f"({exc}); passing the password via an environment variable instead."))
                env[LAN_ENV_PASS] = self._lan_pass

        # Do not force a vision backend here — VisionModule selects / falls back
        # itself when legacy features are on. Strip any stale inherited value so
        # a parent env can't pin the backend unexpectedly.
        env.pop("MAST_VISION_BACKEND", None)
        # Force UTF-8 for the child's stdio + logging FileHandlers. Without this,
        # Windows defaults the service/push log files to the cp936 locale codec, so
        # Chinese + em-dashes were written as GBK bytes and rendered as mojibake
        # whenever a UTF-8 reader (our log viewer / grep) read them back.
        # PYTHONUTF8=1 flips the whole child process to UTF-8 mode — stdout
        # pipe AND every open()/FileHandler.
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    def start_service(self) -> None:
        """Start the main GUI service.

        Vision warms up inside the service only when legacy features are on.
        Default-off installs skip the GPU/load warning; legacy-on still shows it
        (unless the user ticked "don't show again"). The launcher no longer
        shows a Vision status panel or load progress bar.
        """
        if self.proc is not None and self.proc.poll() is None:
            self._log(_tr("主服务已在运行。", "Main service is already running."))
            self._refresh_status()
            return
        if gui_running():
            self._log(_tr(f"端口 {GUI_PORT} 已被占用 — 可能有外部进程在跑 MAST 主服务。",
                          f"Port {GUI_PORT} is already in use - another process may be running the MAST main service."))
            self._refresh_status()
            return
        # Vision loads only when legacy features are on; default-off installs
        # should not pop a "loading DINOv3" warning for a warm that is skipped.
        if self._vision_warn_suppressed or not _legacy_features_enabled_from_disk(self.root_dir):
            self._do_start_service()
        else:
            # Warn first (GPU detect + load caution); "Continue" → _do_start_service.
            self._show_vision_warning_dialog(on_continue=self._do_start_service)

    def _do_start_service(self) -> None:
        """Actually spawn the service subprocess."""
        cmd = self._build_service_cmd()
        self._log(_tr(f"启动主服务子进程: {' '.join(cmd)}",
                      f"Starting main service subprocess: {' '.join(cmd)}"))
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(self.root_dir),
                env=self._make_subprocess_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except Exception as exc:
            self._log(_tr(f"主服务启动失败: {type(exc).__name__}: {exc}",
                          f"Main service failed to start: {type(exc).__name__}: {exc}"))
            self.proc = None
        else:
            self._reader_thread = threading.Thread(
                target=self._read_proc_output, daemon=True
            )
            self._reader_thread.start()
            # Vision panel removed — do not arm a load progress bar.

        self._refresh_status()
        # Service takes ~5–15 s to bind its Gradio port. We removed the
        # always-on poll in v0.2.5 (UI hitch from tasklist), but for the
        # first 30 s after Start the status should flip from "starting" to
        # "running" without a manual Refresh.
        self._schedule_post_start_refresh()

    def stop_service(self, *, quiet: bool = False) -> None:
        """Stop the main GUI service."""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            if not quiet:
                self._log(_tr("主服务未在运行。", "Main service is not running."))
            self.proc = None
        else:
            if not quiet:
                self._log(_tr("正在优雅关闭主服务（让其先安全断开 Nanonis 连接）…", "Shutting down the main service gracefully (so it can disconnect from Nanonis safely)..."))
            try:
                # Graceful-first stop: taskkill WITHOUT /F so the service can
                # close the Nanonis TCP link cleanly. Hard-kill ONLY if it
                # refuses within the timeout (a force-kill mid-TCP can corrupt
                # the Nanonis port until Nanonis restarts).
                outcome = _graceful_stop_proc(
                    proc, graceful_timeout=15.0,
                    log=(None if quiet else self._log_threadsafe),
                    auth=self._lan_auth_pair(),
                )
                if not quiet and outcome == "forced":
                    self._log(_tr("主服务未优雅退出，已强制结束。", "Main service did not exit gracefully and was force-killed."))
            except Exception as exc:
                self._log(_tr(f"主服务关闭异常: {exc}", f"Error while stopping the main service: {exc}"))
            self.proc = None
            if not quiet:
                self._log(_tr("主服务已关闭。", "Main service stopped."))

        self._refresh_status()

    def restart_service(self) -> None:
        """Restart the main GUI service (vision reloads; progress shown, no dialog)."""
        self._log(_tr("重启主服务…", "Restarting main service..."))
        self.stop_service(quiet=True)
        time.sleep(0.3)
        self._do_start_service()

    # ── Vision model load warning + progress ─────────────────────────

    def _show_vision_warning_dialog(self, on_continue=None) -> None:
        """Toplevel shown before starting the service: GPU detect (background
        thread) + load caution + a "don't show again" checkbox + Continue / Cancel.

        Mirrors the LAN auth dialog (transient + grab_set + centered, logo icon).
        On Continue: persist the suppress choice if ticked, close, then call
        ``on_continue`` (which actually starts the service + the progress bar).
        On Cancel: just close (the service is NOT started).
        """
        tk = self.tk
        ttk = self.ttk

        dlg = tk.Toplevel(self.win)
        dlg.title(_tr("启动主服务 · 加载视觉模型", "Start main service · load vision model"))
        dlg.transient(self.win)
        dlg.grab_set()
        dlg.resizable(False, False)
        try:
            if getattr(self, "_logo_img", None) is not None:
                dlg.iconphoto(False, self._logo_img)
        except Exception:
            pass

        frm = ttk.Frame(dlg, padding=14)
        frm.grid()

        ttk.Label(
            frm, text=_tr("即将启动主服务并加载视觉模型 (DINOv3 / M12)", "About to start the main service and load the vision model (DINOv3 / M12)"),
            font=("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        # GPU info block — filled in once detection (on a worker thread) returns.
        gpu_var = tk.StringVar(value=_tr("检测显卡中…", "Detecting GPU..."))
        ttk.Label(frm, textvariable=gpu_var, justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))

        support_var = tk.StringVar(value="")
        support_lbl = ttk.Label(frm, textvariable=support_var, justify="left",
                                wraplength=460)
        support_lbl.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(
            frm,
            text=_tr(
                "加载视觉模型（DINOv3 主干 ~1.2 GB + 检查点）进显存并预热需要约 "
                "1–3 分钟，期间会占用约 2–4 GB 显存；加载在后台进行，界面可继续"
                "使用。关闭主服务即可释放这部分显存。",
                "Loading the vision model (DINOv3 backbone ~1.2 GB + checkpoint) into "
                "VRAM and warming it up takes about 1–3 minutes and uses about 2–4 GB "
                "of VRAM. Loading runs in the background and the window stays usable. "
                "Stopping the main service releases this VRAM."
            ),
            justify="left", wraplength=460, foreground="#666",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 8))

        driver_note_var = tk.StringVar(value="")
        driver_note_lbl = ttk.Label(frm, textvariable=driver_note_var,
                                    justify="left", wraplength=460,
                                    foreground="#dc2626")
        driver_note_lbl.grid(row=4, column=0, columnspan=2, sticky="w")

        # "Don't show again" — when ticked + Continue, future starts skip this dialog
        # (the load progress bar still shows every start).
        suppress_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frm, text=_tr("下次启动不再显示此警告（仍会显示加载进度条）", "Don't show this warning on future starts (the load progress bar still appears)"),
            variable=suppress_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))

        state = {"closed": False}

        def _confirm() -> None:
            if suppress_var.get():
                self._vision_warn_suppressed = True
                _save_vision_warn_suppressed(self.root_dir, True)
                self._log(_tr("已记住：下次启动不再显示视觉模型警告。", "Remembered: the vision model warning will not be shown on future starts."))
            state["closed"] = True
            try:
                dlg.destroy()
            except Exception:
                pass
            if callable(on_continue):
                try:
                    on_continue()
                except Exception as exc:
                    self._log(_tr(f"启动主服务异常: {exc}", f"Error while starting the main service: {exc}"))

        def _cancel() -> None:
            state["closed"] = True
            try:
                dlg.destroy()
            except Exception:
                pass
            self._log(_tr("已取消启动主服务。", "Main service start cancelled."))

        confirm_btn = ttk.Button(btns, text=_tr("继续启动", "Continue"), command=_confirm)
        confirm_btn.grid(row=0, column=0, padx=4)
        ttk.Button(btns, text=_tr("取消", "Cancel"), command=_cancel).grid(row=0, column=1, padx=4)
        dlg.bind("<Escape>", lambda _e: _cancel())

        # Center over the launcher window.
        dlg.update_idletasks()
        w = dlg.winfo_reqwidth(); h = dlg.winfo_reqheight()
        x = self.win.winfo_rootx() + (self.win.winfo_width() - w) // 2
        y = self.win.winfo_rooty() + 80
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        # ── Run _detect_gpu() on a worker thread (it shells out) ──
        def _detect_worker() -> None:
            gpu = _detect_gpu()
            level, msg = _vision_support(gpu)

            def _apply() -> None:
                if state["closed"]:
                    return
                try:
                    vram = gpu.get("vram_gb", 0.0)
                    if isinstance(vram, (int, float)) and vram < 0:
                        vram_txt = _tr("未知(>=4)", "unknown (>=4)")
                    elif vram:
                        vram_txt = f"{vram} GB"
                    else:
                        vram_txt = _tr("未知", "unknown")
                    _unk = _tr("未知", "unknown")
                    gpu_var.set(_tr(
                        f"显卡：{gpu.get('name', _unk)}\n"
                        f"显存：{vram_txt}\n"
                        f"驱动：{gpu.get('driver') or _unk}\n"
                        f"来源：{gpu.get('source', 'none')}",
                        f"GPU: {gpu.get('name', _unk)}\n"
                        f"VRAM: {vram_txt}\n"
                        f"Driver: {gpu.get('driver') or _unk}\n"
                        f"Source: {gpu.get('source', 'none')}"
                    ))
                    support_var.set(msg)
                    color = {
                        "ok": "#22c55e", "marginal": "#d97706",
                        "need_driver": "#dc2626", "unsupported": "#dc2626",
                    }.get(level, "#666")
                    support_lbl.configure(foreground=color)
                    if level == "need_driver":
                        driver_note_var.set(_tr(
                            "注意：需要你先到 NVIDIA 官网下载并自行更新显卡驱动。"
                            "仍可继续开启，但很可能回退到 Mock 后端。",
                            "Note: update the GPU driver from the NVIDIA website first. "
                            "You can still continue, but it will most likely fall back "
                            "to the Mock backend."
                        ))
                except Exception:
                    pass

            try:
                self.win.after(0, _apply)
            except Exception:
                pass

        threading.Thread(target=_detect_worker, daemon=True).start()

    def _begin_vision_progress(self) -> None:
        """No-op: Vision panel removed from the launcher UI."""
        self._vision_watching = False

    def _vision_progress_stage(self, text: str) -> None:
        """No-op when the Vision panel is absent."""
        return

    def _vision_progress_skipped(self, detail: str = "") -> None:
        """No-op UI; clear watcher and optionally log a skip reason."""
        self._vision_watching = False
        detail_l = (detail or "").lower()
        try:
            if "disabled" in detail_l:
                self._log(_tr(
                    "视觉模型未启用（设置里打开「启用旧版功能」并重启服务后才会加载）。",
                    "Vision model is off (enable legacy features in settings and restart the service to load it)."))
            elif "mock" in detail_l:
                self._log(_tr(
                    "视觉模型未加载（后端为 Mock）。主服务可正常使用。",
                    "Vision model was not loaded (Mock backend). The main service still runs normally."))
            elif detail:
                self._log(detail)
        except Exception:
            pass

    def _vision_progress_done(self, ok: bool, detail: str = "", mock: bool = False) -> None:
        """No-op UI; clear watcher and log the outcome for the rare legacy path."""
        self._vision_watching = False
        try:
            if ok and mock:
                self._log(_tr(
                    "视觉模型未加载真权重，已回退 Mock；主服务可正常使用。",
                    "Vision model weights were not loaded; fell back to Mock. The main service still runs normally."))
            elif ok:
                self._log(_tr("视觉模型加载完成。", "Vision model loaded."))
            else:
                self._log(detail or _tr(
                    "视觉模型加载失败（已回退 Mock）；主服务可正常使用。",
                    "Vision model failed to load (fell back to Mock); the main service still runs normally."))
        except Exception:
            pass

    def _build_service_cmd(self) -> list[str]:
        if _frozen():
            # In a frozen build sys.executable IS MAST.exe.
            return [sys.executable, "--service-mode"]
        # In dev, run the same .py file with the current interpreter.
        return [sys.executable, str(Path(__file__).resolve()), "--service-mode"]

    def _read_proc_output(self) -> None:
        """Pump the child stdout into the log widget AND service-*.log."""
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        log_path = _log_dir_for(self.root_dir) / f"service-{_today_stamp()}.log"
        try:
            with open(log_path, "a", encoding="utf-8", buffering=1) as fh:
                fh.write(f"\n=== service start: {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        self._log_threadsafe(line)
                        try:
                            fh.write(line + "\n")
                        except Exception:
                            pass
                        # Refresh status the moment we see Gradio bind the port —
                        # the user no longer has to wait for the polling tick.
                        # v0.3.5 polled every 2s for 30s, but slow Tk repaint or
                        # cold-start >30s left the status stuck at "starting".
                        if "Running on local URL" in line or "Uvicorn running on" in line:
                            self.win.after(0, self._refresh_status)
                        # ── Vision warm-up progress (markers from run_service) ──
                        # All Tk updates marshalled via win.after — this runs on
                        # a background thread and must not touch widgets directly.
                        if self._vision_watching:
                            marker = _parse_vision_load_marker(line)
                            if marker is not None:
                                kind, detail = marker
                                if kind == "start":
                                    self.win.after(
                                        0, self._vision_progress_stage,
                                        _tr("加载视觉模型权重…", "Loading vision model weights..."))
                                elif kind == "done":
                                    self.win.after(
                                        0, self._vision_progress_done, True, "",
                                        detail.lower().startswith("mock"))
                                elif kind == "failed":
                                    self.win.after(
                                        0, self._vision_progress_done, False, line)
                                elif kind == "skipped":
                                    self.win.after(
                                        0, self._vision_progress_skipped, detail)
                            elif ("backbone" in line or "DINOv3" in line) and (
                                    "load" in line.lower()):
                                self.win.after(
                                    0, self._vision_progress_stage, _tr("加载主干网络…", "Loading backbone network..."))
        except Exception as exc:
            self._log_threadsafe(_tr(f"日志读取异常: {exc}", f"Log reader error: {exc}"))
        finally:
            rc = proc.poll()
            if rc is not None:
                self._log_threadsafe(_tr(f"子进程已退出 (rc={rc})", f"Subprocess exited (rc={rc})"))
                # Final status refresh on subprocess exit so the GUI reflects
                # the dead state.
                self.win.after(0, self._refresh_status)

    # ── Browser / clipboard ──────────────────────────────────────────

    def _gui_scheme(self) -> str:
        """"http", or "https" when the service is actually bound for LAN.

        run_service binds 0.0.0.0 with auto-TLS (https) exactly when LAN access is
        enabled AND creds exist; otherwise it stays 127.0.0.1 over plain http. The
        launcher URLs MUST match: opening http:// against the TLS listener makes the
        server drop the plaintext request with no reply → the browser shows
        ERR_EMPTY_RESPONSE / a blank page."""
        return "https" if (getattr(self, "_lan_enabled", False)
                           and getattr(self, "_lan_user", "")) else "http"

    def _open_url(self, url: str, *, require_running: Callable[[], bool] | None = None) -> None:
        if not url:
            self._log(_tr("URL 为空（可能未检测到局域网 IP 或服务未启动）。", "URL is empty (no LAN IP detected, or the service is not running)."))
            return
        if require_running is not None and not require_running():
            self._log(_tr(f"对应服务未运行，无法打开: {url}", f"The service is not running, cannot open: {url}"))
            return
        try:
            webbrowser.open(url)
        except Exception as exc:
            self._log(_tr(f"打开浏览器失败: {exc}", f"Failed to open the browser: {exc}"))

    def _copy_url_var(self, var) -> None:
        url = var.get() if hasattr(var, "get") else str(var)
        if not url:
            self._log(_tr("URL 为空，无法复制。", "URL is empty, nothing to copy."))
            return
        try:
            self.win.clipboard_clear()
            self.win.clipboard_append(url)
            self._log(_tr(f"已复制到剪贴板: {url}", f"Copied to clipboard: {url}"))
        except Exception as exc:
            self._log(_tr(f"复制失败: {exc}", f"Copy failed: {exc}"))

    def _open_logs_dir(self) -> None:
        log_dir = _log_dir_for(self.root_dir)
        try:
            os.startfile(str(log_dir))  # Windows: opens File Explorer at path
        except Exception as exc:
            self._log(_tr(f"打开日志目录失败: {exc} (路径: {log_dir})", f"Failed to open the log folder: {exc} (path: {log_dir})"))

    def _clear_log_widget(self) -> None:
        # Only clears the in-memory Text widget — the on-disk log file is
        # untouched, so we never lose history. Useful when the visible
        # buffer gets too noisy.
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
            self._log(_tr("(屏幕日志已清；磁盘日志未动)", "(On-screen log cleared; the log files on disk are untouched)"))
        except Exception as exc:
            self._log(_tr(f"清屏失败: {exc}", f"Failed to clear the log: {exc}"))

    def _schedule_post_start_refresh(self) -> None:
        """After Start / Restart, keep refreshing status until the service is up.

        v0.3.6 fix: previous 30 s window left the status stuck at "starting"
        when:
          - cold-start exceeded 30 s (rare but real on slow disks)
          - the polling tick fired between widget repaints

        Now we poll every 2 s for the first 30 s, then every 5 s up to 120 s
        total — covers very-slow boots without burning cycles when idle.
        Also self-cancelling: the moment the main subprocess looks fully alive,
        we stop. The stdout reader's "Running on local URL" detector fires a
        refresh immediately too (much faster than waiting for any tick).
        """
        # (interval_ms, tick_count)
        phases = [(2000, 15), (5000, 18)]   # 30 s + 90 s = 120 s window

        def _tick(phase_idx: int, remaining: int) -> None:
            try:
                self._refresh_status()
            except Exception:
                return
            main_up = (
                self.proc is not None and self.proc.poll() is None
                and gui_running()
            )
            if main_up:
                return
            if remaining > 0:
                self.win.after(phases[phase_idx][0],
                               lambda: _tick(phase_idx, remaining - 1))
            elif phase_idx + 1 < len(phases):
                self.win.after(phases[phase_idx + 1][0],
                               lambda: _tick(phase_idx + 1, phases[phase_idx + 1][1]))

        self.win.after(phases[0][0], lambda: _tick(0, phases[0][1]))

    def _detect_tailscale_async(self) -> None:
        """Detect Tailscale off the Tk thread (the CLI can block a few seconds),
        then marshal a status refresh back onto the UI thread — plus a log line
        when the address / network changed since the previous detection."""
        prev = self._ts_status

        def work() -> None:
            try:
                from mast.net.tailscale import get_status
                st = get_status()
            except Exception:  # detection is best-effort; never crash the UI
                st = None
            self._ts_status = st
            note = _tailscale_change_note(prev, st)
            rewritten = False
            new_ip = _tailscale_v4(st)
            if new_ip:
                try:
                    rewritten = _rewrite_remote_ai_handoff_host(new_ip)
                except Exception:
                    rewritten = False
            try:
                if note:
                    self.win.after(0, self._log, note)
                if rewritten:
                    self.win.after(0, self._log, _tr(
                        f"AI 远程 SSH 粘贴行已更新为新地址 {new_ip}。",
                        f"AI remote SSH paste line updated to the new address {new_ip}."))
                self.win.after(0, self._refresh_status)
            except Exception:
                pass
        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            pass

    def _schedule_tailscale_redetect(self) -> None:
        """Re-run :meth:`_detect_tailscale_async` every TAILSCALE_REDETECT_MS."""
        def tick() -> None:
            self._detect_tailscale_async()
            try:
                self.win.after(TAILSCALE_REDETECT_MS, tick)
            except Exception:
                pass
        try:
            self.win.after(TAILSCALE_REDETECT_MS, tick)
        except Exception:
            pass

    def _manual_refresh(self) -> None:
        # Re-detect LAN IP — laptop may have swapped Wi-Fi networks since boot.
        new_lan = _lan_ip()
        if new_lan != self._lan_ip:
            self._lan_ip = new_lan
            if new_lan:
                self._log(_tr(f"局域网 IP 更新为 {new_lan}", f"LAN IP updated to {new_lan}"))
            else:
                self._log(_tr("局域网 IP 已失效（可能断网）。", "LAN IP is no longer valid (network may be down)."))
        # Tailscale may have come up / logged in / changed IP since last check.
        self._detect_tailscale_async()
        self._refresh_status()
        self._log(_tr("已手动刷新状态。", "Status refreshed manually."))

    # ── LAN access toggle / auth dialog ──────────────────────────────

    def _on_lan_toggle(self) -> None:
        """User clicked the "enable LAN" checkbox."""
        if self.lan_enabled_var.get():
            # Turning ON: prompt for credentials if we don't already have them.
            if not (self._lan_user and self._lan_pass):
                creds = self._prompt_lan_auth_dialog()
                if creds is None:
                    # Cancelled — revert checkbox.
                    self.lan_enabled_var.set(False)
                    self._refresh_status()
                    return
                user, pwd = creds
                save_lan_auth(self.root_dir, user, pwd)
                self._lan_user = user
                self._lan_pass = pwd
            self._lan_enabled = True
            self._log(_tr(f"局域网访问已启用 (用户名: {self._lan_user})。"
                          "下次启动主服务时生效；如服务在跑请重启。",
                          f"LAN access enabled (username: {self._lan_user}). Takes effect the "
                          "next time the main service starts; restart it if it is running."))
        else:
            # Turning OFF: keep credentials on disk so re-enabling doesn't
            # need re-typing, but stop advertising LAN URLs / setting env vars.
            self._lan_enabled = False
            self._log(_tr("局域网访问已关闭。重启服务后生效。", "LAN access disabled. Takes effect after the service restarts."))
        self._refresh_status()

    def _configure_lan_auth(self) -> None:
        """User clicked "Change username/password" — re-prompt and overwrite."""
        creds = self._prompt_lan_auth_dialog()
        if creds is None:
            return
        user, pwd = creds
        save_lan_auth(self.root_dir, user, pwd)
        self._lan_user = user
        self._lan_pass = pwd
        if not self._lan_enabled:
            # Auto-enable when user explicitly sets credentials.
            self._lan_enabled = True
            self.lan_enabled_var.set(True)
        self._log(_tr(f"局域网凭据已更新 (用户名: {self._lan_user})。重启服务后生效。", f"LAN credentials updated (username: {self._lan_user}). Takes effect after the service restarts."))
        self._refresh_status()

    def _prompt_lan_auth_dialog(self) -> tuple[str, str] | None:
        """Modal dialog returning (user, pwd) or None if cancelled."""
        tk = self.tk
        ttk = self.ttk

        dlg = tk.Toplevel(self.win)
        dlg.title(_tr("局域网访问 — 设置账号密码", "LAN access — set username and password"))
        dlg.transient(self.win)
        dlg.grab_set()
        dlg.resizable(False, False)

        result: dict[str, tuple[str, str] | None] = {"creds": None}

        frm = ttk.Frame(dlg, padding=14)
        frm.grid()

        ttk.Label(
            frm,
            text=_tr(
                "设置局域网访问的账号密码（HTTP basic auth）。\n"
                "其他电脑访问主服务 GUI 时会要求输入。\n"
                "凭据保存到 api key/lan_auth.env，升级时保留。\n"
                f"⚠️ 安全：开启后 {GUI_PORT} 会绑定 0.0.0.0，仅在可信网络使用。",
                "Set the username and password for LAN access (HTTP basic auth).\n"
                "Other computers must enter them to open the main service GUI.\n"
                "Credentials are saved to api key/lan_auth.env and kept across upgrades.\n"
                f"⚠️ Security: when enabled, port {GUI_PORT} binds to 0.0.0.0; use only on a trusted network."
            ),
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(frm, text=_tr("用户名:", "Username:")).grid(row=1, column=0, sticky="w", pady=(0, 4))
        user_entry = ttk.Entry(frm, width=36)
        user_entry.insert(0, self._lan_user or "admin")
        user_entry.grid(row=1, column=1, sticky="we", pady=(0, 4))
        user_entry.focus_set()

        ttk.Label(frm, text=_tr("密码:", "Password:")).grid(row=2, column=0, sticky="w", pady=(0, 4))
        pwd_show_var = tk.BooleanVar(value=False)
        pwd_entry = ttk.Entry(frm, width=36, show="*")
        if self._lan_pass:
            pwd_entry.insert(0, self._lan_pass)
        pwd_entry.grid(row=2, column=1, sticky="we", pady=(0, 4))

        def _toggle_pwd_show() -> None:
            pwd_entry.configure(show="" if pwd_show_var.get() else "*")
        ttk.Checkbutton(frm, text=_tr("显示密码", "Show password"), variable=pwd_show_var,
                        command=_toggle_pwd_show).grid(row=3, column=1, sticky="w")

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))

        def _save() -> None:
            u = user_entry.get().strip()
            p = pwd_entry.get()
            if not u or not p:
                self._log(_tr("LAN 配置：用户名 / 密码不能为空，已取消。", "LAN setup: username / password must not be empty; cancelled."))
                result["creds"] = None
            else:
                result["creds"] = (u, p)
            dlg.destroy()

        def _cancel() -> None:
            result["creds"] = None
            dlg.destroy()

        ttk.Button(btns, text=_tr("保存", "Save"), command=_save).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text=_tr("取消", "Cancel"), command=_cancel).grid(row=0, column=1, padx=4)
        dlg.bind("<Return>", lambda _e: _save())
        dlg.bind("<Escape>", lambda _e: _cancel())

        dlg.update_idletasks()
        w = dlg.winfo_reqwidth(); h = dlg.winfo_reqheight()
        x = self.win.winfo_rootx() + (self.win.winfo_width() - w) // 2
        y = self.win.winfo_rooty() + 100
        dlg.geometry(f"{w}x{h}+{x}+{y}")
        dlg.wait_window()
        return result["creds"]

    # ── Intranet update push: server side ─────────────────────────────

    def toggle_push_server(self) -> None:
        if self.push_proc is not None and self.push_proc.poll() is None:
            self.stop_push_server()
        else:
            self.start_push_server()

    def start_push_server(self) -> None:
        if self.push_proc is not None and self.push_proc.poll() is None:
            self._log(_tr("推送服务器已在运行。", "Push server is already running."))
            return
        if port_open(GUI_HOST, PUSH_SERVER_PORT):
            self._log(_tr(f"端口 {PUSH_SERVER_PORT} 已被占用 — 可能有外部进程在跑推送服务器。",
                          f"Port {PUSH_SERVER_PORT} is already in use - another process may be running the push server."))
            return
        cmd = self._build_push_server_cmd()
        self._log(_tr(f"启动推送服务器: {' '.join(cmd)}", f"Starting push server: {' '.join(cmd)}"))
        try:
            self.push_proc = subprocess.Popen(
                cmd, cwd=str(self.root_dir), env=self._make_subprocess_env(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except Exception as exc:
            self._log(_tr(f"推送服务器启动失败: {type(exc).__name__}: {exc}", f"Push server failed to start: {type(exc).__name__}: {exc}"))
            self.push_proc = None
            return
        self._push_reader_thread = threading.Thread(
            target=self._read_push_output, daemon=True,
        )
        self._push_reader_thread.start()
        self._refresh_status()

    def stop_push_server(self, *, quiet: bool = False) -> None:
        proc = self.push_proc
        if proc is None or proc.poll() is not None:
            if not quiet:
                self._log(_tr("推送服务器未在运行。", "Push server is not running."))
            self.push_proc = None
            self._refresh_status()
            return
        if not quiet:
            self._log(_tr("正在关闭推送服务器…", "Stopping the push server..."))
        try:
            proc.terminate()
            for _ in range(20):
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
            else:
                proc.kill()
        except Exception as exc:
            self._log(_tr(f"推送服务器关闭异常: {exc}", f"Error while stopping the push server: {exc}"))
        self.push_proc = None
        if not quiet:
            self._log(_tr("推送服务器已关闭。", "Push server stopped."))
        self._refresh_status()

    def _build_push_server_cmd(self) -> list[str]:
        if _frozen():
            return [sys.executable, "--push-server-mode"]
        return [sys.executable, str(Path(__file__).resolve()), "--push-server-mode"]

    def _read_push_output(self) -> None:
        proc = self.push_proc
        if proc is None or proc.stdout is None:
            return
        log_path = _log_dir_for(self.root_dir) / f"push-server-{_today_stamp()}.log"
        try:
            with open(log_path, "a", encoding="utf-8", buffering=1) as fh:
                fh.write(f"\n=== push server start: {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        self._log_threadsafe(f"[push] {line}")
                        try:
                            fh.write(line + "\n")
                        except Exception:
                            pass
        except Exception as exc:
            self._log_threadsafe(_tr(f"推送服务器日志异常: {exc}", f"Push server log error: {exc}"))
        finally:
            rc = proc.poll()
            if rc is not None:
                self._log_threadsafe(_tr(f"推送服务器已退出 (rc={rc})", f"Push server exited (rc={rc})"))

    def _publish_new_version_dialog(self) -> None:
        """Pick a setup.exe + version, then run mast.update.publish."""
        from tkinter import filedialog, simpledialog
        path = filedialog.askopenfilename(
            title=_tr("选择 MAST setup.exe", "Select MAST setup.exe"),
            initialdir=str(Path("dist").resolve() if Path("dist").exists() else Path.home()),
            filetypes=[("Installer", "*.exe"), ("All", "*.*")],
        )
        if not path:
            return
        version = simpledialog.askstring(
            _tr("发布版本号", "Publish version"),
            _tr("输入版本号 (例 0.3.1，注意要和 setup.exe 文件名里一致):",
                "Enter the version (e.g. 0.3.1; it must match the setup.exe file name):"),
            parent=self.win,
        )
        if not version:
            return
        try:
            from mast.update.manifest import make_manifest, write_manifest
            from mast.update.server import push_dir
            import shutil
            setup_src = Path(path).resolve()
            target = push_dir(self.root_dir) / setup_src.name
            if setup_src != target.resolve():
                self._log(_tr(f"复制 {setup_src.name} → {target}", f"Copying {setup_src.name} -> {target}"))
                shutil.copy2(setup_src, target)
            self._log(_tr(f"计算 SHA256 of {target.name} ...", f"Computing SHA256 of {target.name} ..."))
            m = make_manifest(target, version=version.strip())
            write_manifest(push_dir(self.root_dir) / "manifest.json", m)
            self._log(_tr(f"已发布 v{m.version}: sha256={m.sha256[:16]}…  size={m.size_bytes//(1024*1024)} MB",
                          f"Published v{m.version}: sha256={m.sha256[:16]}…  size={m.size_bytes//(1024*1024)} MB"))
        except Exception as exc:
            self._log(_tr(f"发布失败: {type(exc).__name__}: {exc}", f"Publish failed: {type(exc).__name__}: {exc}"))
        self._refresh_status()

    def _show_server_token_dialog(self) -> None:
        """Show / regenerate the server-side push token."""
        try:
            from mast.update.server import _read_token, write_token, generate_token
        except Exception as exc:
            self._log(_tr(f"无法加载更新模块: {exc}", f"Could not load the update module: {exc}"))
            return
        cur = _read_token(self.root_dir)
        if not cur:
            cur = generate_token()
            write_token(self.root_dir, cur)
            self._log(_tr("已自动生成新的推送 token。", "Generated a new push token automatically."))

        tk = self.tk
        ttk = self.ttk
        dlg = tk.Toplevel(self.win)
        dlg.title(_tr("推送服务器 token", "Push server token"))
        dlg.transient(self.win); dlg.grab_set(); dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=14); frm.grid()
        ttk.Label(frm, text=_tr("共享 token (客户端需填同一个值):", "Shared token (clients must enter the same value):"), justify="left").grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        entry = ttk.Entry(frm, width=60)
        entry.insert(0, cur); entry.configure(state="readonly")
        entry.grid(row=1, column=0, sticky="we")

        def _copy():
            self.win.clipboard_clear(); self.win.clipboard_append(cur)
            self._log(_tr("token 已复制到剪贴板。", "Token copied to the clipboard."))

        def _regen():
            new_tok = generate_token()
            write_token(self.root_dir, new_tok)
            entry.configure(state="normal"); entry.delete(0, "end"); entry.insert(0, new_tok)
            entry.configure(state="readonly")
            self._log(_tr("token 已重新生成 — 所有客户端需要更新它们的 token 才能继续接收推送。", "Token regenerated - all clients must update their token to keep receiving pushes."))

        btns = ttk.Frame(frm); btns.grid(row=2, column=0, sticky="we", pady=(8, 0))
        ttk.Button(btns, text=_tr("复制", "Copy"), command=_copy).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text=_tr("重新生成", "Regenerate"), command=_regen).grid(row=0, column=1, padx=4)
        ttk.Button(btns, text=_tr("关闭", "Close"), command=dlg.destroy).grid(row=0, column=2, padx=4)
        dlg.update_idletasks()
        w = dlg.winfo_reqwidth(); h = dlg.winfo_reqheight()
        x = self.win.winfo_rootx() + (self.win.winfo_width() - w) // 2
        y = self.win.winfo_rooty() + 100
        dlg.geometry(f"{w}x{h}+{x}+{y}")

    # ── Intranet update push: client side ─────────────────────────────

    def _configure_update_client_dialog(self) -> None:
        """Set the remote push server URL + token (writes update_*.env files)."""
        try:
            from mast.update.client import (
                read_server_url, write_server_url,
                _read_token as _read_client_token, write_token as _write_client_token,
            )
        except Exception as exc:
            self._log(_tr(f"无法加载更新模块: {exc}", f"Could not load the update module: {exc}"))
            return

        tk = self.tk
        ttk = self.ttk
        dlg = tk.Toplevel(self.win)
        dlg.title(_tr("配置远程推送服务器", "Configure remote push server"))
        dlg.transient(self.win); dlg.grab_set(); dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=14); frm.grid()

        ttk.Label(frm, text=_tr(
            "配置本机作为「客户端」，从内网推送服务器拉取 MAST 升级。\n"
            "服务器 URL 例: http://<推送服务器地址>:8765\n"
            "Token 必须和服务器侧 update_server_token.env 中的值一致。",
            "Configure this PC as a client that pulls MAST upgrades from the intranet push server.\n"
            "Server URL example: http://<push-server-address>:8765\n"
            "The token must match the value in update_server_token.env on the server side."
        ), justify="left").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(frm, text=_tr("服务器 URL:", "Server URL:")).grid(row=1, column=0, sticky="w", pady=4)
        url_entry = ttk.Entry(frm, width=44)
        url_entry.insert(0, read_server_url(self.root_dir))
        url_entry.grid(row=1, column=1, sticky="we", pady=4)

        ttk.Label(frm, text="Token:").grid(row=2, column=0, sticky="w", pady=4)
        tok_entry = ttk.Entry(frm, width=44)
        tok_entry.insert(0, _read_client_token(self.root_dir))
        tok_entry.grid(row=2, column=1, sticky="we", pady=4)

        btns = ttk.Frame(frm); btns.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def _save():
            url = url_entry.get().strip()
            tok = tok_entry.get().strip()
            if url:
                write_server_url(self.root_dir, url)
            if tok:
                _write_client_token(self.root_dir, tok)
            _empty = _tr("(空)", "(empty)")
            self._log(_tr(f"客户端配置已保存：URL={url or _empty} / token={'***' if tok else _empty}",
                          f"Client settings saved: URL={url or _empty} / token={'***' if tok else _empty}"))
            self._log(_tr("下次主服务启动时（或点「立即检查更新」）生效。",
                          "Takes effect the next time the main service starts (or click \"Check for updates now\")."))
            dlg.destroy()
            self._refresh_status()

        ttk.Button(btns, text=_tr("保存", "Save"), command=_save).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text=_tr("取消", "Cancel"), command=dlg.destroy).grid(row=0, column=1, padx=4)
        dlg.update_idletasks()
        w = dlg.winfo_reqwidth(); h = dlg.winfo_reqheight()
        x = self.win.winfo_rootx() + (self.win.winfo_width() - w) // 2
        y = self.win.winfo_rooty() + 100
        dlg.geometry(f"{w}x{h}+{x}+{y}")

    def _check_update_now(self) -> None:
        """One-shot client check with a download progress dialog.

        The check + download run in a worker thread; a Tk modal shows an
        indeterminate bar while fetching the manifest and a determinate
        MB progress bar during the (162 MB full / ~48 MB delta) download, so the
        user always sees what's happening instead of a silent wait.
        """
        import tkinter as tk
        from tkinter import ttk

        dlg = tk.Toplevel(self.win)
        dlg.title(_tr("检查更新", "Check for updates"))
        dlg.resizable(False, False)
        dlg.transient(self.win)
        frm = ttk.Frame(dlg, padding=16)
        frm.grid()
        head = ttk.Label(frm, text=_tr("正在检查更新…", "Checking for updates..."), font=("Segoe UI", 11, "bold"))
        head.grid(row=0, column=0, sticky="w")
        pb = ttk.Progressbar(frm, length=340, mode="indeterminate")
        pb.grid(row=1, column=0, pady=(10, 4))
        pb.start(12)
        info = ttk.Label(frm, text=_tr("联系推送服务器…", "Contacting the push server..."))
        info.grid(row=2, column=0, sticky="w")
        dlg.update_idletasks()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        dlg.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

        def _progress(done, total):
            def _u():
                try:
                    if total and total > 0:
                        if str(pb["mode"]) != "determinate":
                            pb.stop()
                            pb.config(mode="determinate", maximum=total)
                        pb["value"] = done
                        info.config(text=_tr(
                            f"下载中… {done // (1024*1024)} / "
                            f"{total // (1024*1024)} MB "
                            f"({100*done//max(total,1)}%)",
                            f"Downloading... {done // (1024*1024)} / "
                            f"{total // (1024*1024)} MB "
                            f"({100*done//max(total,1)}%)"))
                    else:
                        info.config(text=_tr(f"下载中… {done // (1024*1024)} MB",
                                             f"Downloading... {done // (1024*1024)} MB"))
                except Exception:
                    pass
            try:
                self.win.after(0, _u)
            except Exception:
                pass

        def _bg():
            status, detail = "error", ""
            remote_ver = remote_date = ""
            cur_ver = cur_date = ""
            try:
                from mast.update.client import (
                    _read_token, read_server_url, check_and_download,
                )
                from mast import __version__, __release_date__
                cur_ver, cur_date = __version__, __release_date__
                url = read_server_url(self.root_dir)
                tok = _read_token(self.root_dir)
                if not url or not tok:
                    status, detail = "no-config", _tr(
                        "未配置 URL / token，先点「修改服务器/Token…」。",
                        "URL / token not configured; click \"Change server / token...\" first.")
                else:
                    # Lightweight manifest peek for the latest version + release
                    # date to DISPLAY (independent of whether a download happens —
                    # check_and_download only returns status strings).
                    try:
                        import httpx
                        from mast.update.manifest import Manifest
                        with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0)) as _c:
                            _r = _c.get(url.rstrip("/") + "/manifest.json",
                                        headers={"Authorization": f"Bearer {tok}"})
                            if _r.status_code == 200:
                                _m = Manifest.from_dict(_r.json())
                                remote_ver = _m.version or ""
                                remote_date = getattr(_m, "published_at", "") or ""
                    except Exception as _exc:
                        self._log_threadsafe(_tr(f"manifest 预览失败: {_exc}", f"Manifest preview failed: {_exc}"))
                    self._log_threadsafe(_tr(f"立即检查 {url} ...", f"Checking {url} now ..."))
                    status, detail = check_and_download(
                        url, tok, self.root_dir, current_version=__version__,
                        progress_cb=_progress,
                    )
                    self._log_threadsafe(_tr(f"检查结果：{status}  {detail}", f"Check result: {status}  {detail}"))
            except Exception as exc:
                status, detail = "error", f"{type(exc).__name__}: {exc}"
                self._log_threadsafe(_tr(f"检查异常: {detail}", f"Check error: {detail}"))
            finally:
                def _done():
                    try:
                        pb.stop()
                    except Exception:
                        pass
                    nice = {
                        "downloaded": _tr("新版本已下载,下次启动将提示安装。",
                                          "The new version was downloaded; you will be prompted to install it at the next start."),
                        "downloaded-delta": _tr("增量更新已下载,下次启动将应用。",
                                                "The incremental update was downloaded and will be applied at the next start."),
                        "no-update": _tr("已是最新版本。", "Already up to date."),
                        "no-server": _tr("无法连接推送服务器。", "Cannot reach the push server."),
                        "no-config": detail,
                        "error": _tr(f"出错：{detail}", f"Error: {detail}"),
                    }.get(status, f"{status}: {detail}")
                    head.config(text=nice)
                    pb.grid_remove()
                    info.grid_remove()
                    # ── 4-field version table ──
                    # On "no-update" the manifest peek may be empty / equal to
                    # current → fall back to current so "latest version" is still honest.
                    _rv = remote_ver or cur_ver
                    _rd = remote_date or cur_date
                    tbl = ttk.Frame(frm)
                    tbl.grid(row=3, column=0, sticky="w", pady=(10, 0))
                    _rows = (
                        (_tr("当前版本", "Current version"), f"v{cur_ver}"),
                        (_tr("当前版本发布时间", "Current version released"), _fmt_release_date(cur_date)),
                        (_tr("最新版本", "Latest version"), f"v{_rv}"),
                        (_tr("最新版本发布时间", "Latest version released"), _fmt_release_date(_rd)),
                    )
                    for _i, (_k, _v) in enumerate(_rows):
                        ttk.Label(tbl, text=_k + _tr("：", ":"), foreground="#666").grid(
                            row=_i, column=0, sticky="w", padx=(0, 8))
                        ttk.Label(tbl, text=_v, font=("Segoe UI", 9, "bold")).grid(
                            row=_i, column=1, sticky="w")
                    ttk.Button(frm, text=_tr("关闭", "Close"), command=dlg.destroy).grid(
                        row=4, column=0, sticky="e", pady=(10, 0))
                    self._refresh_status()
                try:
                    self.win.after(0, _done)
                except Exception:
                    pass
        threading.Thread(target=_bg, daemon=True).start()

    def _reset_update_overrides(self) -> None:
        """Delete the per-machine URL/token files so the bundle defaults take effect."""
        for fname in ("update_server_url.env", "update_client_token.env"):
            p = self.root_dir / "api key" / fname
            if p.exists():
                try:
                    p.unlink()
                    self._log(_tr(f"已删除自定义: {p}", f"Deleted custom setting: {p}"))
                except OSError as exc:
                    self._log(_tr(f"删除失败 {p}: {exc}", f"Delete failed {p}: {exc}"))
        self._log(_tr("自动更新配置已恢复为安装包默认值。", "Auto-update settings restored to the installer defaults."))
        self._refresh_status()

    def _open_release_notes(self) -> None:
        """Open the CURRENT version's release notes — ALWAYS a real .txt.

        Resolution order:
          1. bundled ``docs/release_notes_current.txt`` (stable name, current
             version — emitted by the build pipeline).
          2. ``docs/release_notes_v*.txt`` matching __version__ (then newest).
          3. (transition) the legacy ``*release_notes*.md`` if no .txt exists.
          4. Fallback: GENERATE a real .txt at runtime under
             experiments/logs/ with a version header so the button never
             dead-ends — it always opens a real .txt with os.startfile().
        """
        import os
        import re
        try:
            from mast import __version__ as cur_ver, __release_date__ as cur_date
        except Exception:
            cur_ver, cur_date = "?", ""

        cand_dir = None
        try:
            d = _resource_path("docs")
            if d and d.is_dir():
                cand_dir = d
        except Exception:
            cand_dir = None

        def _vkey(p: Path):
            m = re.findall(r"v?(\d+(?:\.\d+)+)", p.name)
            return tuple(int(x) for x in m[-1].split(".")) if m else (0,)

        target: Path | None = None

        # 1. Stable per-version current file.
        if cand_dir is not None:
            cur_txt = cand_dir / "release_notes_current.txt"
            if cur_txt.exists():
                target = cur_txt

        # 2. release_notes_v*.txt matching __version__, else newest.
        if target is None and cand_dir is not None:
            txts = sorted(cand_dir.glob("release_notes_v*.txt"))
            if txts:
                mm = ".".join(cur_ver.split(".")[:2]) if cur_ver != "?" else ""
                for key in (cur_ver, mm):
                    if not key:
                        continue
                    hit = [p for p in txts if key in p.name]
                    if hit:
                        target = sorted(hit, key=_vkey)[-1]
                        break
                if target is None:
                    target = sorted(txts, key=_vkey)[-1]

        # 3. (transition) legacy markdown release notes if no .txt exists.
        if target is None and cand_dir is not None:
            mds: list[Path] = []
            seen: set = set()
            for pat in ("*release_notes*.md", "mast2_release_notes*.md"):
                for p in sorted(cand_dir.glob(pat)):
                    if p not in seen:
                        seen.add(p)
                        mds.append(p)
            if mds:
                mm = ".".join(cur_ver.split(".")[:2]) if cur_ver != "?" else ""
                for key in (cur_ver, mm):
                    if not key:
                        continue
                    hit = [p for p in mds if key in p.name]
                    if hit:
                        target = sorted(hit, key=_vkey)[-1]
                        break
                if target is None:
                    target = sorted(mds, key=_vkey)[-1]

        # 4. Nothing bundled — generate a real .txt at runtime so the button
        #    NEVER dead-ends.
        if target is None or not target.exists():
            try:
                out_dir = _log_dir_for(self.root_dir)
                target = out_dir / f"release_notes_v{cur_ver}.txt"
                target.write_text(
                    _tr(f"MAST v{cur_ver} 更新说明\n"
                        f"发布日期: {_fmt_release_date(cur_date)}\n",
                        f"MAST v{cur_ver} release notes\n"
                        f"Release date: {_fmt_release_date(cur_date)}\n")
                    + "=" * 40
                    + _tr("\n\n(本地未打包详细更新说明，完整说明见 docs/。)\n",
                          "\n\n(Detailed release notes are not bundled locally; see docs/.)\n"),
                    encoding="utf-8",
                )
            except Exception as exc:
                self._log(_tr(f"生成更新说明失败: {exc}", f"Failed to generate the release notes: {exc}"))
                return

        try:
            os.startfile(str(target))  # noqa: S606 — Windows default opener
            self._log(_tr(f"打开更新说明: {target.name}", f"Opened release notes: {target.name}"))
        except Exception as exc:
            self._log(_tr(f"打开更新说明失败: {exc}", f"Failed to open the release notes: {exc}"))

    def _open_update_log(self) -> None:
        """Open the push server's access.log for inspection."""
        try:
            from mast.update.server import push_dir
            log_path = push_dir(self.root_dir) / "access.log"
            if not log_path.exists():
                self._log(_tr(f"日志文件不存在: {log_path}", f"Log file does not exist: {log_path}"))
                return
            os.startfile(str(log_path))
        except Exception as exc:
            self._log(_tr(f"打开日志失败: {exc}", f"Failed to open the log: {exc}"))

    # ── API key config dialog ────────────────────────────────────────

    def _configure_provider(self, provider: str) -> None:
        existing = get_key(provider, self.root_dir)
        new_key = self._prompt_key_dialog(provider, existing)
        if new_key is None:
            return  # cancelled
        if not new_key.strip():
            self._log(_tr(f"{PROVIDER_LABELS[provider]}: 留空 — 未保存。", f"{PROVIDER_LABELS[provider]}: left empty - not saved."))
            return
        try:
            path = save_key(provider, self.root_dir, new_key)
            self._log(_tr(f"{PROVIDER_LABELS[provider]} key 已保存到 {path}", f"{PROVIDER_LABELS[provider]} key saved to {path}"))
        except Exception as exc:
            self._log(_tr(f"保存失败: {exc}", f"Save failed: {exc}"))
        self._refresh_status()

    def _prompt_key_dialog(self, provider: str, existing: str) -> str | None:
        tk = self.tk
        ttk = self.ttk
        label = PROVIDER_LABELS[provider]
        fname = PROVIDER_KEY_FILES[provider]
        env_vars = ", ".join(PROVIDER_ENV_VARS.get(provider, ()))

        dlg = tk.Toplevel(self.win)
        dlg.title(_tr(f"配置 {label}", f"Configure {label}"))
        dlg.transient(self.win)
        dlg.grab_set()
        dlg.resizable(False, False)

        result: dict[str, str | None] = {"key": None}

        frm = ttk.Frame(dlg, padding=14)
        frm.grid()

        ttk.Label(
            frm,
            text=_tr(
                f"为 {label} 配置 API key。\n"
                f"保存到：api key/{fname}\n"
                f"环境变量优先级（如已设则覆盖文件）：{env_vars}",
                f"Configure the API key for {label}.\n"
                f"Saved to: api key/{fname}\n"
                f"Environment variable priority (if set, it overrides the file): {env_vars}"
            ),
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(frm, text="API key:").grid(row=1, column=0, sticky="w", pady=(0, 4))
        show_var = tk.BooleanVar(value=False)
        entry = ttk.Entry(frm, width=52, show="*")
        if existing:
            entry.insert(0, existing)
        entry.grid(row=1, column=1, sticky="we", pady=(0, 4))
        entry.focus_set()

        def _toggle_show() -> None:
            entry.configure(show="" if show_var.get() else "*")

        ttk.Checkbutton(frm, text=_tr("显示", "Show"), variable=show_var, command=_toggle_show).grid(
            row=2, column=1, sticky="w"
        )

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))

        def _save() -> None:
            result["key"] = entry.get()
            dlg.destroy()

        def _cancel() -> None:
            result["key"] = None
            dlg.destroy()

        ttk.Button(btns, text=_tr("保存", "Save"), command=_save).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text=_tr("取消", "Cancel"), command=_cancel).grid(row=0, column=1, padx=4)
        dlg.bind("<Return>", lambda _e: _save())
        dlg.bind("<Escape>", lambda _e: _cancel())

        dlg.update_idletasks()
        w = dlg.winfo_reqwidth(); h = dlg.winfo_reqheight()
        x = self.win.winfo_rootx() + (self.win.winfo_width() - w) // 2
        y = self.win.winfo_rooty() + 80
        dlg.geometry(f"{w}x{h}+{x}+{y}")
        dlg.wait_window()
        return result["key"]

    # ── Status polling ───────────────────────────────────────────────

    def _refresh_status(self) -> None:
        # API-key indicators are hidden in recommended mode; only Nanonis remains.

        # ── Nanonis (simulator vs real) ─────────────────────────────
        # We can only tell "simulator vs real" by process name on the local
        # machine, so the answer is approximate when the controller lives on
        # a LAN host. Treat any TCP-open + no Mimea-process as "real hardware / external".
        nan = self.indicators["nanonis"]
        if nanonis_connected():
            if _detect_nanonis_simulator():
                nan["dot"].configure(foreground="#eab308")  # amber
                nan["value"].configure(
                    text=_tr(f"已连接 — 模拟器 ({NANONIS_HOST}:{NANONIS_PORT})",
                             f"connected — simulator ({NANONIS_HOST}:{NANONIS_PORT})"),
                    foreground="#eab308",
                )
            else:
                nan["dot"].configure(foreground="#22c55e")
                nan["value"].configure(
                    text=_tr(f"已连接 — 实机 ({NANONIS_HOST}:{NANONIS_PORT})",
                             f"connected — real controller ({NANONIS_HOST}:{NANONIS_PORT})"),
                    foreground="#22c55e",
                )
        else:
            nan["dot"].configure(foreground="#888")
            nan["value"].configure(
                text=_tr(f"未连接 (TCP {NANONIS_PORT} 未开)",
                         f"not connected (TCP {NANONIS_PORT} closed)"),
                foreground="#888",
            )

        # ── URL strings (2 rows) ────────────────────────────────────
        # Scheme MUST reflect the service's real binding — https in LAN mode
        # (0.0.0.0 + TLS), else http. A stale http:// against the TLS listener is
        # the blank-page / ERR_EMPTY_RESPONSE failure.
        _scheme = self._gui_scheme()
        self.url_var.set(f"{_scheme}://{GUI_HOST}:{GUI_PORT}")
        if not self._lan_enabled:
            self.lan_url_var.set(_tr("(局域网访问未启用 — 勾上方复选框)",
                                     "(LAN access disabled — tick the checkbox above)"))
        elif not self._lan_ip:
            self.lan_url_var.set(_tr("(未检测到局域网 IP)", "(no LAN IP detected)"))
        else:
            self.lan_url_var.set(f"{_scheme}://{self._lan_ip}:{GUI_PORT}")

        # ── LAN info line ───────────────────────────────────────────
        if self._lan_enabled:
            if self._lan_user:
                self.lan_info_var.set(_tr(
                    f"已启用 — 用户名: {self._lan_user} | "
                    f"绑定 0.0.0.0 (主 {GUI_PORT}) | 重启服务后生效",
                    f"Enabled — username: {self._lan_user} | "
                    f"binds 0.0.0.0 (main {GUI_PORT}) | applies after a service restart"
                ))
            else:
                self.lan_info_var.set(_tr(
                    "已启用但缺少凭据 — 点「修改账号密码」设置",
                    "Enabled but credentials are missing — click \"Change username/password\""))
        else:
            self.lan_info_var.set(_tr("未启用（仅本机可访问）", "Disabled (this PC only)"))

        # ── Tailscale remote-access line + URL ──────────────────────
        # Renders the cached off-thread detection. The SAME 0.0.0.0 bind + TLS as
        # LAN mode already makes the console reachable over the tailnet; this only
        # tells the user which address to open on the OTHER computer.
        st = self._ts_status
        if st is None:
            self.ts_info_var.set(_tr("🌐 Tailscale: 检测中…", "🌐 Tailscale: checking..."))
            self.ts_url_var.set("")
        elif not getattr(st, "installed", False):
            self.ts_info_var.set(_tr(
                "🌐 Tailscale: 未安装 —— 在两台电脑都装 Tailscale 并登录同一账号 "
                "(tailscale.com/download)",
                "🌐 Tailscale: not installed — install Tailscale on both computers and sign in "
                "to the same account (tailscale.com/download)"
            ))
            self.ts_url_var.set("")
        elif not getattr(st, "ready", False):
            self.ts_info_var.set(
                f"🌐 Tailscale: {st.hint or st.backend_state or _tr('未就绪', 'not ready')}")
            self.ts_url_var.set("")
        elif not self._lan_enabled:
            dev = st.device_name or st.preferred_host
            self.ts_info_var.set(_tr(
                f"🌐 Tailscale: 就绪（本机 {dev}）—— 勾选上方复选框即可开启跨网远程访问",
                f"🌐 Tailscale: ready (this PC: {dev}) — tick the checkbox above to enable "
                "cross-network remote access"
            ))
            self.ts_url_var.set("")
        else:
            try:
                from mast.net.tailscale import remote_urls
                urls = remote_urls(st, GUI_PORT, _scheme)
            except Exception:
                urls = []
            self.ts_url_var.set(urls[0]["url"] if urls else "")
            # The other computer must be in the SAME tailnet (a shared PC can carry
            # several accounts and move between them), and a system proxy that does
            # not bypass 100.64.0.0/10 makes the browser fail on this address even
            # though the tunnel is fine.
            net = (_tr(f"网络 {st.tailnet}", f"network {st.tailnet}")
                   if getattr(st, "tailnet", "") else _tr("同一网络", "same network"))
            self.ts_info_var.set(_tr(
                f"🌐 Tailscale: 就绪（{net}）—— 另一台电脑登录同一个 Tailscale 网络后，"
                "浏览器打开下方「远程 - Tailscale」地址，首次提示证书不受信任点「继续」。"
                "那台电脑开着 Clash 等代理时，把 100.64.0.0/10 设为直连 / 绕过代理。",
                f"🌐 Tailscale: ready ({net}) — after the other computer signs in to the same "
                "Tailscale network, open the \"Remote - Tailscale\" address below in its browser "
                "and click \"Continue\" at the first untrusted-certificate warning. If that "
                "computer uses a proxy (e.g. Clash), route 100.64.0.0/10 direct / bypass the proxy."
            ))

        # ── Main service status ─────────────────────────────────────
        proc = self.proc
        if proc is not None and proc.poll() is None:
            if gui_running():
                self.svc_status_var.set(_tr(f"● 主服务: 运行中 (端口 {GUI_PORT})",
                                            f"● Main service: running (port {GUI_PORT})"))
                self.svc_status_lbl.configure(foreground="#22c55e")
            else:
                self.svc_status_var.set(_tr("● 主服务: 启动中… (子进程已起，等待端口)",
                                            "● Main service: starting... (subprocess up, waiting for the port)"))
                self.svc_status_lbl.configure(foreground="#eab308")
        elif gui_running():
            self.svc_status_var.set(_tr("● 主服务: 外部进程占用端口 (非启动器管理)",
                                        "● Main service: port held by an external process (not managed by this launcher)"))
            self.svc_status_lbl.configure(foreground="#eab308")
        else:
            self.svc_status_var.set(_tr("● 主服务: 未运行", "● Main service: not running"))
            self.svc_status_lbl.configure(foreground="#888")

        # ── Main button enabled-state ───────────────────────────────
        # Enable based purely on the main GUI service state.
        owned_main = proc is not None and proc.poll() is None
        any_alive  = owned_main or gui_running()
        self.start_btn.configure(
            state="normal" if not gui_running() else "disabled"
        )
        self.restart_btn.configure(state="normal" if owned_main else "disabled")
        self.stop_btn.configure(state="normal" if any_alive else "disabled")

        # Updates status comes from the GitHub release cache / checker — do not
        # overwrite it on every status refresh.

        # ── Remote AI paste lines (survive closing the elevated window) ──
        self._refresh_remote_ai_handoff()
        self._sync_remote_ai()

    # ── Remote AI (Claude Code / Codex on another computer) ──────────

    def _sync_remote_ai(self) -> None:
        """Retired: the launcher no longer runs an in-process remote-AI server.

        Remote AI access is now set up by setup-ai-ssh.ps1 (the "Set up AI remote
        access" button), which turns on Windows' own SSH over Tailscale. This stays
        as a no-op because the status refresh still calls it; the old
        _start/_stop/_render/_on_* helpers below are unused and will be removed in a
        follow-up.
        """
        return

    def _run_ai_ssh_script(self, *extra_args: str, action_label: str) -> None:
        """Elevate and run setup-ai-ssh.ps1 with optional switches."""
        from tkinter import messagebox
        if sys.platform != "win32":
            messagebox.showinfo(
                _tr("仅限 Windows", "Windows only"),
                _tr("AI 远程访问的设置脚本目前只支持 Windows。",
                    "The AI remote-access setup script currently supports Windows only."),
                parent=self.win)
            return
        script = _resource_path("setup-ai-ssh.ps1")
        if not script.is_file():
            messagebox.showerror(
                _tr("找不到脚本", "Script not found"),
                _tr(f"没有找到设置脚本：\n{script}", f"The setup script was not found:\n{script}"),
                parent=self.win)
            return
        args = " ".join(extra_args)
        ps_args = f'-ExecutionPolicy Bypass -NoProfile -File "{script}"'
        if args:
            ps_args = f"{ps_args} {args}"
        try:
            import ctypes
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "powershell.exe", ps_args, str(script.parent), 1)
            if int(rc) <= 32:
                self._log(_tr(
                    f"AI 远程访问{action_label}未启动（可能取消了管理员授权）。",
                    f"AI remote-access {action_label} did not start (the administrator prompt may have been declined)."))
            else:
                self._log(_tr(
                    f"已打开 AI 远程访问{action_label}向导（在新的管理员窗口里）。完成后点「刷新连接信息」。",
                    f"Opened the AI remote-access {action_label} wizard (in a new administrator window). "
                    f"Click \"Refresh connection info\" when it finishes."))
                # Poll a few times so the handoff file shows up without a manual refresh.
                for delay_ms in (4000, 8000, 15000, 30000):
                    self.win.after(delay_ms, self._refresh_remote_ai_handoff)
        except Exception as exc:
            self._log(_tr(f"打开 AI 远程访问{action_label}失败：{exc}",
                          f"Could not open AI remote-access {action_label}: {exc}"))

    def _setup_ai_ssh(self) -> None:
        """One-click install: elevate setup-ai-ssh.ps1 (SSH over Tailscale)."""
        self._run_ai_ssh_script(action_label=_tr("安装", "install"))

    def _uninstall_ai_ssh(self) -> None:
        """One-click uninstall: elevate setup-ai-ssh.ps1 -Uninstall."""
        self._run_ai_ssh_script("-Uninstall", action_label=_tr("卸载", "uninstall"))

    def _refresh_remote_ai_handoff(self) -> None:
        """Load ~/.ssh/ai-remote.handoff.json into the Remote AI paste fields.

        If Tailscale is ready and the handoff host is stale, rewrite host /
        prompt_line first so a Tailscale account switch does not leave an
        outdated SSH line for Claude Code / Codex.
        """
        if not hasattr(self, "rai_prompt_var"):
            return
        ip = _tailscale_v4(getattr(self, "_ts_status", None))
        if ip:
            try:
                if _rewrite_remote_ai_handoff_host(ip):
                    self._log(_tr(
                        f"AI 远程 SSH 粘贴行已更新为新地址 {ip}。",
                        f"AI remote SSH paste line updated to the new address {ip}."))
            except Exception:
                pass
        data = _load_remote_ai_handoff()
        prompt = str(data.get("prompt_line") or "").strip()
        note = str(data.get("handover_note") or "").strip()
        host = str(data.get("host") or "").strip()
        updated = str(data.get("updated_at") or "").strip()
        self.rai_prompt_var.set(prompt)
        self.rai_note_var.set(note)
        if prompt:
            extra = f" · {host}" if host else ""
            when = f" · {updated}" if updated else ""
            self.rai_status_var.set(_tr(
                f"已保存连接信息{extra}{when}（关掉设置窗口后仍可在此复制）。",
                f"Connection info saved{extra}{when} (still available here after the setup window closes)."))
        else:
            self.rai_status_var.set(_tr(
                "尚未安装（或还没有连接信息）。装好后点「刷新连接信息」。",
                "Not installed yet (or no connection info on disk). After install, click \"Refresh connection info\"."))

    def _open_public_releases(self) -> None:
        """Open the GitHub MAST releases page in the default browser."""
        try:
            webbrowser.open(PUBLIC_RELEASES_URL)
            self._log(_tr(f"已打开发布页：{PUBLIC_RELEASES_URL}",
                          f"Opened releases page: {PUBLIC_RELEASES_URL}"))
        except Exception as exc:
            self._log(_tr(f"打开发布页失败：{exc}", f"Could not open the releases page: {exc}"))

    def _open_public_issues(self) -> None:
        """Open the GitHub MAST issues page for operator feedback."""
        try:
            webbrowser.open(PUBLIC_ISSUES_URL)
            self._log(_tr(f"已打开意见反馈页：{PUBLIC_ISSUES_URL}",
                          f"Opened feedback page: {PUBLIC_ISSUES_URL}"))
        except Exception as exc:
            self._log(_tr(f"打开意见反馈页失败：{exc}", f"Could not open the feedback page: {exc}"))

    def _local_mast_version(self) -> str:
        try:
            from mast import __version__ as ver
            return str(ver)
        except Exception:
            return "?"

    def _apply_github_release_cache_to_ui(self) -> None:
        """Paint the Updates row from the on-disk cache (no network)."""
        if not hasattr(self, "update_status_var"):
            return
        cache = _load_github_release_cache(self.root_dir)
        remote = str(cache.get("version") or cache.get("tag") or "").strip()
        local = self._local_mast_version()
        if not remote:
            self.update_status_var.set(_tr(
                f"● 尚未检查过更新。发布页：\n  {PUBLIC_RELEASES_URL}",
                f"● No update check yet. Releases page:\n  {PUBLIC_RELEASES_URL}",
            ))
            try:
                self.update_status_lbl.configure(foreground="#888")
            except Exception:
                pass
            return
        if not (_version_tuple(remote) and _version_tuple(local)):
            # Not comparable (the tag is not a version number, or this build's version is
            # unknown): say what GitHub lists instead of claiming "up to date" or "new".
            self.update_status_var.set(_tr(
                f"● GitHub 上的最新发布：{remote}（无法与当前版本 {local} 比较）。发布页：\n  {PUBLIC_RELEASES_URL}",
                f"● Latest release on GitHub: {remote} (cannot be compared with your version {local}). Releases page:\n  {PUBLIC_RELEASES_URL}",
            ))
            try:
                self.update_status_lbl.configure(foreground="#888")
            except Exception:
                pass
            return
        if _is_remote_version_newer(remote, local):
            self.update_status_var.set(_tr(
                f"● 有新版本 v{remote}（当前 v{local}）。请到发布页下载：\n  {PUBLIC_RELEASES_URL}",
                f"● New version v{remote} available (you have v{local}). Download from:\n  {PUBLIC_RELEASES_URL}",
            ))
            try:
                self.update_status_lbl.configure(foreground="#ca8a04")
            except Exception:
                pass
        else:
            self.update_status_var.set(_tr(
                f"● 已是最新（v{local}）。发布页：\n  {PUBLIC_RELEASES_URL}",
                f"● Up to date (v{local}). Releases page:\n  {PUBLIC_RELEASES_URL}",
            ))
            try:
                self.update_status_lbl.configure(foreground="#22c55e")
            except Exception:
                pass

    def _check_github_release(self, *, force: bool = False) -> None:
        """Poll GitHub /releases/latest off the Tk thread (rate-limited, one at a time)."""
        if not hasattr(self, "update_status_var"):
            return
        if getattr(self, "_github_check_inflight", False):
            # A request is already on its way (a double click, or the automatic check still
            # running): do not start a second one. Its result repaints the row.
            if force:
                self._log(_tr("更新检查正在进行，请稍候。",
                              "An update check is already running."))
            return
        cache = _load_github_release_cache(self.root_dir)
        now = time.time()
        if not _should_check_github_release(cache, now=now, force=force):
            self._apply_github_release_cache_to_ui()
            gap_h = GITHUB_RELEASE_CHECK_MIN_S // 3600
            if force:
                self._log(_tr(
                    "刚才已检查过，请稍后再试（手动检查最少间隔约 1 分钟）。",
                    "Checked recently; try again shortly (manual checks need about 1 minute between runs)."))
            else:
                self._log(_tr(
                    f"跳过自动检查更新（间隔未满约 {gap_h} 小时）；沿用缓存结果。",
                    f"Skipped automatic update check (under ~{gap_h} h since last); using the cache."))
            return

        self.update_status_var.set(_tr("● 正在检查 GitHub 新版本…",
                                       "● Checking GitHub for a new version..."))
        try:
            self.update_status_lbl.configure(foreground="#888")
        except Exception:
            pass
        etag = str(cache.get("etag") or "")

        def work() -> None:
            result = _fetch_latest_github_release(etag=etag)
            try:
                self.win.after(0, self._on_github_release_result, result, force)
            except Exception:
                # The window is gone (or Tk refused the call): nobody will deliver the result,
                # so do not leave the guard set.
                self._github_check_inflight = False

        self._github_check_inflight = True
        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception as exc:
            self._github_check_inflight = False
            self._log(_tr(f"检查更新失败：{exc}", f"Update check failed: {exc}"))

    def _on_github_release_result(self, result: dict, force: bool) -> None:
        self._github_check_inflight = False
        cache = _load_github_release_cache(self.root_dir)
        now = time.time()
        status = str(result.get("status") or "")
        if status == "ok":
            cache["checked_at"] = now
            cache["etag"] = result.get("etag") or cache.get("etag") or ""
            cache["tag"] = result.get("tag") or ""
            cache["version"] = result.get("version") or _normalize_release_version(
                str(result.get("tag") or ""))
            cache["html_url"] = result.get("html_url") or PUBLIC_RELEASES_URL
            _save_github_release_cache(self.root_dir, cache)
            self._apply_github_release_cache_to_ui()
            ver = cache.get("version") or "?"
            self._log(_tr(f"GitHub 最新发布：v{ver}", f"Latest GitHub release: v{ver}"))
        elif status == "not_modified":
            cache["checked_at"] = now
            if result.get("etag"):
                cache["etag"] = result["etag"]
            _save_github_release_cache(self.root_dir, cache)
            self._apply_github_release_cache_to_ui()
            if force:
                self._log(_tr("发布页无变化（已是缓存中的最新）。",
                              "Releases page unchanged (still the cached latest)."))
        else:
            err = str(result.get("error") or "unknown")
            # Keep prior cache for display; still bump checked_at on force so we
            # do not hammer a failing endpoint.
            if force:
                cache["checked_at"] = now
                _save_github_release_cache(self.root_dir, cache)
            self._apply_github_release_cache_to_ui()
            self.update_status_var.set(_tr(
                f"● 检查更新失败（{err}）。可稍后重试或打开发布页：\n  {PUBLIC_RELEASES_URL}",
                f"● Update check failed ({err}). Retry later or open the releases page:\n  {PUBLIC_RELEASES_URL}",
            ))
            try:
                self.update_status_lbl.configure(foreground="#dc2626")
            except Exception:
                pass
            self._log(_tr(f"检查更新失败：{err}", f"Update check failed: {err}"))

    def _copy_text(self, text: str) -> None:
        """Copy *text* to the clipboard (best effort)."""
        text = (text or "").strip()
        if not text:
            self._log(_tr("没有可复制的内容。", "Nothing to copy."))
            return
        try:
            self.win.clipboard_clear()
            self.win.clipboard_append(text)
            self._log(_tr("已复制到剪贴板。", "Copied to the clipboard."))
        except Exception as exc:
            self._log(_tr(f"复制失败：{exc}", f"Copy failed: {exc}"))

    def _copy_string_var(self, var) -> None:
        self._copy_text(var.get() if var is not None else "")

    def _start_remote_ai(self, ip: str) -> bool:
        try:
            mod = _remote_ai_module()
            try:
                from mast import __version__ as ver
                product = f"MAST {ver}"
            except Exception:
                product = "MAST"
            settings = self.root_dir / "config" / REMOTE_AI_SETTINGS
            plugin = _app_dir() / "integrations" / "claude-code"
            srv = mod.RemoteServer(
                settings_path=str(settings), host=ip,
                name=mod.default_name(_tailscale_machine(self._ts_status) or socket.gethostname()),
                fetch_dir=str(self.root_dir / "remote-ai" / "fetch"),
                guide_dir=str(plugin / "skills" / "mast-operator" / "references"),
                mast_port=GUI_PORT, mast_login=self._lan_auth_pair,
                computer_allowed=lambda: bool(self._remote_ai_prefs.get("computer")),
                service=_RemoteService(self), activity=self._remote_ai_activity,
                peer_names=_tailscale_peer_names, product=product)
            srv.start()
        except Exception as exc:
            self._remote_ai_failed_ip = ip
            self._remote_ai_error = f"{type(exc).__name__}: {exc}"
            self._log(_tr(f"远程 AI 启动失败：{self._remote_ai_error}",
                          f"Remote AI could not start: {self._remote_ai_error}"))
            return False
        _restrict_file_to_owner(settings)    # it holds the key
        self._remote_ai_failed_ip = self._remote_ai_error = ""
        self._remote_ai = srv
        self._log(_tr(f"远程 AI 已开启：{srv.name}，地址 {ip}:{srv.port}。",
                      f"Remote AI is on: {srv.name} at {ip}:{srv.port}."))
        return True

    def _stop_remote_ai(self, *, wait: bool = False) -> None:
        """Stop serving. Off the Tk thread unless ``wait`` (running calls get a few seconds)."""
        srv, self._remote_ai = self._remote_ai, None
        if srv is None:
            return

        def stop():
            try:
                srv.stop()
            except Exception as exc:
                print(f"[launcher] remote AI stop failed: {exc}")
        if wait:
            stop()
        else:
            threading.Thread(target=stop, name="remote-ai-stop", daemon=True).start()

    def _render_remote_ai(self) -> None:
        enabled = bool(self._remote_ai_prefs.get("enabled"))
        computer = bool(self._remote_ai_prefs.get("computer"))
        srv = self._remote_ai
        st = self._ts_status
        lines = ""
        if not enabled:
            status = _tr("未开启。勾选后，另一台电脑上的 Claude Code 或 Codex 只要贴入这里给出的两行，"
                         "就能通过 Tailscale 使用这台电脑上的 MAST，那台电脑上什么都不用装。",
                         "Off. Once ticked, Claude Code or Codex on another computer can use this "
                         "MAST over Tailscale after you paste the two lines shown here into it; "
                         "nothing has to be installed on that computer.")
        elif srv is not None:
            what = (_tr("MAST 工具 + 命令、文件和截屏", "MAST tools + commands, files and screenshots")
                    if computer else _tr("只有 MAST 工具", "MAST tools only"))
            status = _tr(
                f"● 已开启：{srv.name}，{srv.host}:{srv.port}（{what}）。把下面两行贴给另一台电脑上的 "
                "Claude Code 或 Codex（两台电脑登录同一个 Tailscale 网络），它会自己完成设置，"
                "重启它一次后即可使用。它做的每件事都会记在下面的日志里。",
                f"● On: {srv.name} at {srv.host}:{srv.port} ({what}). Paste the two lines below "
                "into Claude Code or Codex on the other computer (both signed in to the same "
                "Tailscale network); it sets itself up and works after one restart. Everything "
                "it does is listed in the log below.")
            lines = "\n".join(srv.lines())
        elif self._remote_ai_error and self._remote_ai_failed_ip == _tailscale_v4(st):
            status = _tr(f"启动失败：{self._remote_ai_error}。取消勾选再勾选可重试。",
                         f"Could not start: {self._remote_ai_error}. Untick and tick again to retry.")
        elif st is None:
            status = _tr("正在检测 Tailscale…", "Checking Tailscale...")
        else:
            hint = (getattr(st, "hint", "") or getattr(st, "backend_state", "")
                    or _tr("未就绪", "not ready"))
            status = _tr(f"等待 Tailscale：{hint}", f"Waiting for Tailscale: {hint}")
        self.remote_ai_status_var.set(status)
        self.remote_ai_text.configure(state="normal")
        self.remote_ai_text.delete("1.0", "end")
        self.remote_ai_text.insert("1.0", lines)
        self.remote_ai_text.configure(state="disabled")
        state = "normal" if srv is not None else "disabled"
        self.remote_ai_copy_btn.configure(state=state)
        self.remote_ai_newkey_btn.configure(state=state)

    def _on_remote_ai_toggle(self) -> None:
        enabled = bool(self.remote_ai_var.get())
        self._remote_ai_prefs["enabled"] = enabled
        _save_remote_ai_prefs(self.root_dir, self._remote_ai_prefs)
        self._remote_ai_failed_ip = self._remote_ai_error = ""
        if not enabled:
            self._stop_remote_ai()
            self._log(_tr("远程 AI 已关闭：另一台电脑上的 AI 不能再连接。",
                          "Remote AI is off: an AI on another computer can no longer connect."))
        self._sync_remote_ai()
        if enabled and self._remote_ai is not None:
            self._copy_remote_ai_lines()

    def _on_remote_ai_computer_toggle(self) -> None:
        from tkinter import messagebox
        allow = bool(self.remote_ai_computer_var.get())
        if allow and not messagebox.askyesno(
                _tr("允许完全控制这台电脑？", "Allow full control of this computer?"),
                _tr("连上来的 AI 将能做你的 Windows 账户能做的一切：运行程序、修改和删除文件、"
                    "看到屏幕。仪器本身仍然只通过 MAST 的工具操作。\n\n"
                    "只把那两行交给你信任的人。要撤销，取消这个勾选，或点「换新密钥」。\n\n继续？",
                    "A connected AI can then do everything your Windows account can do on this "
                    "computer: run programs, change and delete files, see the screen. The "
                    "instrument itself is still operated only through MAST's tools.\n\n"
                    "Give the two lines only to people you trust. To take it back, untick this "
                    "box or click \"New key\".\n\nContinue?"),
                parent=self.win):
            self.remote_ai_computer_var.set(False)
            return
        self._remote_ai_prefs["computer"] = allow
        _save_remote_ai_prefs(self.root_dir, self._remote_ai_prefs)
        srv = self._remote_ai
        if not allow and srv is not None:
            # Taking the permission back also ends the commands it already started.
            threading.Thread(target=srv.computer.stop_all, name="remote-ai-stop-commands",
                             daemon=True).start()
        self._log(_tr("远程 AI：已允许命令、文件和截屏。" if allow else
                      "远程 AI：已禁止命令、文件和截屏，正在运行的命令已结束（MAST 工具不受影响）。",
                      "Remote AI: commands, files and screenshots are allowed." if allow else
                      "Remote AI: commands, files and screenshots are no longer allowed, and "
                      "running commands were ended (MAST tools are unaffected)."))
        self._render_remote_ai()

    def _show_operator_controls(self, visible: bool) -> None:
        """Show the Operator prompts controls, or fold them away and leave the status line.

        They are there on an optical station and always while popups are switched off; on an STM
        station, or before the service has said what it is, nothing is listened for and the
        status line says so (OperatorPopupController._controls_visible)."""
        body = getattr(self, "_op_body", None)
        if body is None:
            return
        try:
            if visible:
                body.grid()
            else:
                body.grid_remove()
        except Exception:  # noqa: BLE001 - the widget was destroyed by a rebuild
            pass

    def _on_operator_toggle(self) -> None:
        """The "Pop up a window" / "Play a sound" checkboxes of the Operator prompts section."""
        self._operator_popup.set_enabled(self.op_enabled_var.get())
        self._operator_popup.set_sound(self.op_sound_var.get())

    def _copy_remote_ai_lines(self) -> None:
        srv = self._remote_ai
        if srv is None:
            return
        try:
            self.win.clipboard_clear()
            self.win.clipboard_append("\r\n".join(srv.lines()))
            self._log(_tr("远程 AI 的两行已复制到剪贴板。", "The two remote AI lines were copied to the clipboard."))
        except Exception as exc:
            self._log(_tr(f"复制失败：{exc}", f"Copy failed: {exc}"))

    def _renew_remote_ai_key(self) -> None:
        from tkinter import messagebox
        srv = self._remote_ai
        if srv is None:
            return
        if not messagebox.askyesno(
                _tr("换新密钥", "New key"),
                _tr("用旧的两行连接的 AI 会立即失去访问，需要把新的两行重新交给它们。继续？",
                    "Every AI connected with the old lines loses access at once; give them the "
                    "new two lines again. Continue?"),
                parent=self.win):
            return
        srv.renew_key()
        self._log(_tr("远程 AI 已换新密钥：旧的两行立即失效。", "Remote AI has a new key: the old lines no longer work."))
        self._render_remote_ai()
        self._copy_remote_ai_lines()

    def _remote_ai_activity(self, who: str, kind: str, detail: str) -> None:
        """The remote AI server's activity log (called on its threads)."""
        text = _remote_ai_activity_text(kind, detail)
        self._log_threadsafe(_tr(f"[远程 AI] {who}：{text}", f"[Remote AI] {who}: {text}"))

    # ── Logging ──────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        with self.log_lock:
            ts = time.strftime("%H:%M:%S")
            line = f"[{ts}] {msg}"
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
            # sys.stdout was teed to launcher-*.log in main(); print routes
            # the same message to the persistent on-disk log so it survives
            # launcher restarts / crashes.
            try:
                print(f"[launcher] {line}")
            except Exception:
                pass

    def _log_threadsafe(self, msg: str) -> None:
        # Tk widget calls must happen on the main thread.
        self.win.after(0, self._log, msg)

    # ── Lifecycle ────────────────────────────────────────────────────

    def _minimize_to_tray(self) -> None:
        """Window-close (×) handler — NEVER exits (Nanonis-style). Hides to the
        system tray if available, else iconifies. The ONLY real exit is the
        "stop main service and quit launcher" button / tray "Quit launcher", which gracefully stops the
        service + frees VRAM behind a progress bar (see _shutdown_launcher). This
        prevents an accidental × from orphaning the service and leaking its VRAM."""
        try:
            if self._tray_icon is not None:
                self.win.withdraw()
                self._log(_tr("启动器已最小化到系统托盘（右下角图标双击/右键可恢复）。", "Launcher minimized to the system tray (double-click or right-click the tray icon to restore)."))
            else:
                # No tray → iconify so the window is never lost.
                self.win.iconify()
                self._log(_tr("启动器已最小化（托盘不可用）。关闭请用「关闭启动器」按钮。", "Launcher minimized (tray unavailable). To quit, use the Stop main service and quit launcher button."))
        except Exception as exc:
            self._log(_tr(f"最小化失败: {exc}", f"Minimize failed: {exc}"))

    def _lan_auth_pair(self) -> tuple[str, str]:
        """LAN basic-auth credentials for talking to our own service (the
        shutdown request): the ones in memory, else the saved file."""
        if self._lan_user and self._lan_pass:
            return (self._lan_user, self._lan_pass)
        return get_lan_auth(self.root_dir)

    def _make_proc_stopper(self, proc, *, main: bool = True):
        """Graceful-then-force stopper for a launcher-OWNED Popen.

        Uses :func:`_graceful_stop_proc`: the HTTP self-shutdown (main service
        only — *main* False for the push server) and a cooperative ``taskkill``
        (no ``/F``) first so the service closes its Nanonis TCP link cleanly,
        hard-killing ONLY if it refuses within the timeout (force-kill mid-TCP can
        corrupt the Nanonis port until Nanonis restarts)."""
        def _stop():
            try:
                _graceful_stop_proc(proc, graceful_timeout=15.0,
                                    log=self._log_threadsafe,
                                    auth=self._lan_auth_pair(), http_shutdown=main)
            except Exception:
                pass
        return _stop

    def _make_pid_stopper(self, pid):
        """Graceful stopper for an EXTERNAL service (started outside this launcher,
        e.g. C:\\MAST\\MAST.exe). No Popen handle: ask it over HTTP first (the
        only thing a windowless service hears), then taskkill by PID — WITHOUT /F.
        We deliberately do NOT force-kill: a force-kill mid-TCP can corrupt the
        Nanonis port until Nanonis restarts. If it won't stop gracefully we leave
        it (the caller reports that)."""
        def _stop():
            try:
                if _http_shutdown_request(GUI_PORT, log=self._log_threadsafe,
                                          auth=self._lan_auth_pair()):
                    for _ in range(60):   # up to ~12 s for a graceful exit
                        if not _pid_alive(pid):
                            return
                        time.sleep(0.2)
                subprocess.run(["taskkill", "/PID", str(pid)],
                               capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for _ in range(60):       # up to ~12 s for a graceful exit
                    if not _pid_alive(pid):
                        break
                    time.sleep(0.2)
            except Exception:
                pass
        return _stop

    def _shutdown_launcher(self) -> None:
        """The ONLY full-exit path (the "Stop main service and quit launcher" button + tray "Quit launcher").
        Confirms, gracefully stops the main service (frees VRAM) behind a progress
        bar, then exits. The window × only minimizes — so this is the real exit."""
        from tkinter import messagebox
        stoppers: list[tuple[str, object]] = []
        _main_label = _tr("主服务", "Main service")
        for label, attr in ((_main_label, "proc"), (_tr("推送服务器", "Push server"), "push_proc")):
            proc = getattr(self, attr, None)
            if proc and proc.poll() is None:
                stoppers.append((label, self._make_proc_stopper(proc, main=(attr == "proc"))))
        external = False
        if not any(lbl == _main_label for lbl, _ in stoppers) and gui_running():
            # Service is running but NOT owned by this launcher (external start).
            pid = _pid_on_port(GUI_PORT)
            if pid:
                external = True
                stoppers.append((_tr(f"外部主服务 (PID {pid})", f"External main service (PID {pid})"),
                                 self._make_pid_stopper(pid)))

        if not stoppers:
            if messagebox.askyesno(_tr("退出启动器", "Quit launcher"),
                                   _tr("未检测到运行中的服务。确定退出启动器？",
                                       "No running service detected. Quit the launcher?"),
                                   parent=self.win):
                self._exit_now()
            return

        if external:
            msg = _tr(
                "检测到主服务由外部启动（非本启动器托管）。将尝试优雅停止它并释放"
                "显存，然后退出。\n注意：为避免损坏 Nanonis 端口，不会强制结束——"
                "若它拒绝优雅退出会保留运行（届时请到启动它的窗口关闭）。\n\n继续？",
                "The main service was started externally (not managed by this launcher). "
                "It will be stopped gracefully to free VRAM, then the launcher quits.\n"
                "Note: to avoid corrupting the Nanonis port it is never force-killed — if it "
                "refuses to exit gracefully it keeps running (close it from the window that "
                "started it).\n\nContinue?")
        else:
            msg = _tr(
                "将优雅停止主服务并释放显存（约数秒），然后退出启动器。\n"
                "若只想隐藏窗口、让服务继续运行，请点窗口右上角 ×（缩到状态栏）。\n\n确定？",
                "The main service will be stopped gracefully and its VRAM freed (takes a few "
                "seconds), then the launcher quits.\nTo just hide the window and keep the "
                "service running, click × at the top right of the window (minimizes to the "
                "tray).\n\nProceed?")
        if not messagebox.askyesno(_tr("关闭主服务并退出启动器", "Stop main service and quit launcher"),
                                   msg, parent=self.win):
            return
        self._run_shutdown_with_progress(stoppers)

    def _run_shutdown_with_progress(self, stoppers) -> None:
        """Modal progress dialog while each (label, stop_callable) runs on a worker
        thread; on completion the launcher exits. Tk updates marshalled via
        win.after (the worker never touches widgets directly)."""
        # A remote AI must not start the service again while it is being stopped.
        self._stop_remote_ai()
        tk = self.tk
        ttk = self.ttk
        dlg = tk.Toplevel(self.win)
        dlg.title(_tr("正在关闭 MAST…", "Shutting down MAST..."))
        dlg.transient(self.win)
        dlg.resizable(False, False)
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)  # can't dismiss mid-shutdown
        try:
            if getattr(self, "_logo_img", None) is not None:
                dlg.iconphoto(False, self._logo_img)
        except Exception:
            pass
        frm = ttk.Frame(dlg, padding=18)
        frm.grid()
        status = tk.StringVar(value=_tr("正在优雅停止主服务、释放显存…",
                                            "Stopping the main service gracefully and freeing VRAM..."))
        ttk.Label(frm, textvariable=status, font=("Segoe UI", 10),
                  wraplength=360, justify="left").grid(row=0, column=0, pady=(0, 10))
        bar = ttk.Progressbar(frm, mode="indeterminate", length=320)
        bar.grid(row=1, column=0)
        bar.start(12)
        dlg.update_idletasks()
        w = dlg.winfo_reqwidth(); h = dlg.winfo_reqheight()
        x = self.win.winfo_rootx() + (self.win.winfo_width() - w) // 2
        y = self.win.winfo_rooty() + 100
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        def _worker() -> None:
            for label, stop in stoppers:
                try:
                    self.win.after(0, lambda l=label: status.set(_tr(f"正在停止：{l} …", f"Stopping: {l} ...")))
                except Exception:
                    pass
                try:
                    stop()
                except Exception:
                    pass
            # External service may still be up (we never force-kill it).
            leftover = gui_running() and (self.proc is None or self.proc.poll() is not None)

            def _finish() -> None:
                try:
                    bar.stop()
                except Exception:
                    pass
                if leftover:
                    from tkinter import messagebox
                    messagebox.showinfo(
                        _tr("外部服务仍在运行", "External service still running"),
                        _tr("外部主服务未能优雅停止（已避免强制结束以保护 Nanonis 端口）。\n"
                            "启动器将退出；如需释放显存，请到启动该服务的窗口手动关闭。",
                            "The external main service did not stop gracefully (it was not "
                            "force-killed, to protect the Nanonis port).\nThe launcher will "
                            "quit; to free VRAM, close the service manually from the window "
                            "that started it."),
                        parent=self.win)
                self._exit_now()

            try:
                self.win.after(0, _finish)
            except Exception:
                self._exit_now()

        threading.Thread(target=_worker, name="launcher-shutdown",
                         daemon=True).start()

    def _exit_now(self) -> None:
        """Final teardown — stop any still-alive child, clear lock/IPC/tray,
        destroy the window. Idempotent (safe to call from the mainloop-exit
        finally as a backstop).

        Normally reached AFTER _run_shutdown_with_progress already graceful-stopped
        the main service, so the main service is already gone here. But this is
        also the mainloop-exit backstop, so a service may still be alive: give the
        Nanonis-holding main service a SHORT graceful window (taskkill no /F)
        before any hard kill to narrow the port-corruption window. push_proc (push
        server) holds no Nanonis port → plain terminate is fine."""
        # The remote AI goes first: no call may start the service while it is stopped.
        self._stop_remote_ai(wait=True)
        pop = getattr(self, "_operator_popup", None)
        if pop is not None:      # stop polling before the service goes away
            try:
                pop.stop()
            except Exception:  # noqa: BLE001
                pass
        proc = getattr(self, "proc", None)
        if proc and proc.poll() is None:
            try:
                # Keep this snappy (exit path): short graceful attempt, then kill.
                _graceful_stop_proc(proc, graceful_timeout=5.0, force_timeout=2.0,
                                    auth=self._lan_auth_pair())
            except Exception:
                pass
        push = getattr(self, "push_proc", None)
        if push and push.poll() is None:
            try:
                push.terminate()
            except Exception:
                pass
        _clear_launcher_lock(self.root_dir)
        _sweep_lan_auth_handoff(self.root_dir)
        self._close_ipc()
        self._destroy_tray()
        try:
            self.win.destroy()
        except Exception:
            pass

    # ── IPC: second instance asks first to bring window to foreground ──

    def _poll_ipc(self) -> None:
        """Non-blocking accept-loop drained from Tk's after() ticker."""
        if self._ipc_sock is None:
            return
        try:
            while True:
                try:
                    conn, _ = self._ipc_sock.accept()
                except BlockingIOError:
                    break
                try:
                    data = conn.recv(64).decode("ascii", errors="ignore")
                finally:
                    conn.close()
                if data.startswith("BRING_FOREGROUND"):
                    self._bring_to_foreground()
        except Exception as exc:
            print(f"[launcher] IPC poll error: {exc}")
        finally:
            self.win.after(500, self._poll_ipc)

    def _bring_to_foreground(self) -> None:
        """Surface the launcher window — useful when 2nd instance asked us."""
        try:
            self.win.deiconify()
            self.win.lift()
            self.win.attributes("-topmost", True)
            self.win.after(200, lambda: self.win.attributes("-topmost", False))
            self.win.focus_force()
        except Exception:
            pass

    def _close_ipc(self) -> None:
        try:
            if self._ipc_sock is not None:
                self._ipc_sock.close()
                self._ipc_sock = None
        except Exception:
            pass

    # ── System tray (pystray + Pillow). Optional dependency. ──

    def _init_tray(self) -> None:
        """Set up the system-tray icon. No-op if pystray/PIL unavailable."""
        try:
            import pystray
            from PIL import Image
        except Exception as exc:
            print(f"[launcher] tray disabled (pystray/PIL missing): {exc}")
            return
        try:
            logo_path = _resource_path(LOGO_PNG_RELPATH)
            if not logo_path.exists():
                print(f"[launcher] tray disabled (logo not found at {logo_path})")
                return
            image = Image.open(str(logo_path))
        except Exception as exc:
            print(f"[launcher] tray icon load failed: {exc}")
            return

        def _show(_icon=None, _item=None):
            self.win.after(0, self._bring_to_foreground)

        def _quit(_icon=None, _item=None):
            # Graceful full shutdown (warning + progress + VRAM release).
            self.win.after(0, self._shutdown_launcher)

        menu = pystray.Menu(
            pystray.MenuItem(_tr("显示窗口", "Show window"), _show, default=True),
            pystray.MenuItem(_tr("退出启动器", "Quit launcher"), _quit),
        )
        self._tray_icon = pystray.Icon("MAST2", image, _tr("MAST 启动器", "MAST Launcher"), menu)
        # Run in background thread (Icon.run is blocking)
        self._tray_thread = threading.Thread(
            target=self._tray_icon.run, daemon=True,
        )
        self._tray_thread.start()

    def _destroy_tray(self) -> None:
        try:
            if self._tray_icon is not None:
                self._tray_icon.stop()
                self._tray_icon = None
        except Exception:
            pass

    def _on_autostart_toggle(self) -> None:
        enabled = self.autostart_var.get()
        ok = _startup_set(enabled)
        if ok:
            self._log(_tr(f"开机自启 {'已启用' if enabled else '已关闭'}",
                          f"Start with Windows {'enabled' if enabled else 'disabled'}"))
        else:
            self._log(_tr("开机自启切换失败，请检查注册表写入权限",
                          "Could not change start-with-Windows; check registry write permission"))
            self.autostart_var.set(_startup_enabled())  # revert UI

    def _on_language_selected(self, _event=None) -> None:
        """Combobox changed: map the display label back to a language code."""
        label = ""
        try:
            label = (self._lang_combo_var.get() or "").strip()
        except Exception:
            return
        code = None
        for c, disp in zip(self._lang_codes, self._lang_display):
            if disp == label:
                code = c
                break
        if code is None or code == _LANG:
            return
        self._apply_language(code)

    def _apply_language(self, lang: str) -> None:
        """Persist *lang*, rebuild the launcher UI immediately (no restart)."""
        if lang not in _SUPPORTED_LANGS or lang == _LANG:
            return
        # Preserve scrollable log text across the rebuild.
        prior_log = ""
        try:
            prior_log = self.log_text.get("1.0", "end-1c")
        except Exception:
            prior_log = ""
        _set_lang(lang, self.root_dir)
        notice = _tr(
            f"界面语言已切换为 {_LANG_LABELS.get(lang, lang)}。",
            f"UI language switched to {_LANG_LABELS.get(lang, lang)}.",
        )
        if prior_log:
            prior_log = prior_log.rstrip("\n") + "\n" + notice
        else:
            prior_log = notice
        try:
            from mast import __version__ as _ver, __release_date__ as _rel
            self.win.title(_tr(f"MAST 启动器 — v{_ver}  ({_rel})",
                               f"MAST Launcher — v{_ver}  ({_rel})"))
        except Exception:
            self.win.title(_tr("MAST 启动器", "MAST Launcher"))
        # An open operator prompt window is a child of this one: close it first (it is
        # shown again, in the new language, once the widgets are rebuilt).
        self._operator_popup.prepare_ui_rebuild()
        for child in list(self.win.winfo_children()):
            try:
                child.destroy()
            except Exception:
                pass
        self._build_ui()
        self._operator_popup.after_ui_rebuild()
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("end", prior_log + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except Exception:
            pass
        try:
            self._refresh_status()
        except Exception:
            pass

    def _admin_pin_path(self) -> Path:
        return self.root_dir / "config" / "admin_pin.txt"

    def _set_admin_pin(self) -> None:
        """Set / clear the web "Advanced admin" tab unlock PIN. Stored LOCAL-ONLY as the
        sha256 hex in <root>/config/admin_pin.txt; the web tab reads + compares it
        (the raw PIN never leaves this machine / never enters the browser)."""
        from tkinter import simpledialog, messagebox
        import hashlib
        p = self._admin_pin_path()
        has = p.exists() and p.read_text(encoding="utf-8").strip()
        _title = _tr("高级管理密码", "Admin password")
        pin = simpledialog.askstring(
            _title,
            _tr("设置网页「高级管理」标签的解锁密码"
                + ("（当前已设置）" if has else "（当前未设置）") + "。\n"
                "留空并确定 = 清除密码（届时该标签会提示未设置）。",
                "Set the unlock password for the web \"Advanced admin\" tab"
                + (" (currently set)" if has else " (currently not set)") + ".\n"
                "Leave empty and confirm = clear the password (the tab then reports it as not set)."),
            show="*", parent=self.win)
        if pin is None:
            return  # cancelled
        pin = pin.strip()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if not pin:
                p.unlink(missing_ok=True)
                self._log(_tr("高级管理密码已清除。", "Admin password cleared."))
                messagebox.showinfo(_title, _tr("已清除密码。", "Password cleared."), parent=self.win)
                return
            confirm = simpledialog.askstring(
                _title, _tr("再次输入以确认：", "Enter it again to confirm:"),
                show="*", parent=self.win)
            if confirm is None:
                return
            if confirm.strip() != pin:
                messagebox.showerror(_title, _tr("两次输入不一致，未保存。",
                                                 "The two entries do not match; not saved."),
                                     parent=self.win)
                return
            p.write_text(hashlib.sha256(pin.encode("utf-8")).hexdigest(), encoding="utf-8")
            self._log(_tr("高级管理密码已更新（sha256 写入 config/admin_pin.txt）。",
                          "Admin password updated (sha256 written to config/admin_pin.txt)."))
            messagebox.showinfo(
                _title,
                _tr("密码已保存。\n（网页「高级管理」标签将用它解锁；重启主服务后生效。）",
                    "Password saved.\n(The web \"Advanced admin\" tab will use it to unlock; "
                    "takes effect after the main service restarts.)"),
                parent=self.win)
        except Exception as exc:
            self._log(_tr(f"设置高级管理密码失败: {exc}", f"Failed to set the admin password: {exc}"))
            messagebox.showerror(_title, _tr(f"保存失败：{exc}", f"Save failed: {exc}"), parent=self.win)

    def run(self) -> int:
        try:
            self.win.mainloop()
        finally:
            # Backstop: if the mainloop ever exits without going through
            # _shutdown_launcher (e.g. an unexpected destroy), still tear down.
            self._exit_now()
        return 0


# ── Headless vision self-test (frozen-bundle verification) ───────────

def _run_selftest_vision() -> int:
    """Headless M12 vision self-test. Verifies the FROZEN bundle can load
    torch + timm + peft + the offline DINOv3 backbone and run real inference.

    Writes a one-line PASS/FAIL summary to ``<project_root>/vision_selftest_result.txt``
    (console is suppressed under --noconsole) and returns 0 on PASS, 1 on FAIL.
    Run via ``MAST2.exe --selftest-vision``.
    """
    import traceback

    # Same line as run_service / _run_install_skillpack: all one-shot entry points
    # must agree on where the user data lives. Without it a frozen build's
    # project_root() falls back to the exe directory, so result files land in the
    # install directory instead of the data root.
    os.environ.setdefault("MAST2_PROJECT_ROOT", str(_user_root()))

    lines: list[str] = ["=== MAST M12 vision self-test ==="]
    rc = 1
    try:
        import numpy as np

        from mast.vision.module import VisionModule
        from mast.vision.vigil_backend import VIGILBackend

        VisionModule._instance = None
        vm = VisionModule(backend="vigil")  # explicit → honored even pre-flight
        be = vm._backend
        lines.append(f"backend: {type(be).__name__}")
        if not isinstance(be, VIGILBackend):
            lines.append(
                "FAIL: backend is not VIGILBackend — torch/timm/peft or the M12 "
                "ckpt / DINOv3 backbone cache is missing from the frozen bundle."
            )
        else:
            vm.set_scan_size_nm(10.0)
            H = W = 256
            terr = (np.mgrid[0:H, 0:W][1] // 85).astype(np.float32) * 30.0
            img = terr + np.random.RandomState(7).randn(H, W).astype(np.float32) * 2.0
            c = vm.assess_tip_coarse(img)
            s = vm.segment(img)
            lines.append(
                f"coarse: label={c.label} conf={c.confidence:.3f} "
                f"R_tip_nm={c.tip_radius_nm} sharp_log10={c.sharpness_log10}"
            )
            lines.append(
                f"segment: level={s.level} classes={s.classes} "
                f"px={sum(s.class_counts.values())}"
            )
            ok = (
                be.is_loaded()
                and c.label in ("good", "bad")
                and s.level == 1
                and len(s.classes) == 4
            )
            # Also verify the scan-progress auto-trigger pipeline is bundled +
            # functional in the frozen runtime: the deterministic translator
            # and the monitor module must import and produce a Chinese narration.
            try:
                from mast.agents.buffer_summarizer.node import describe
                from mast.vision.scan_monitor import ScanVisionMonitor  # noqa: F401
                narration = describe("tip_coarse", c.model_dump())
                lines.append(f"scan-vision: describe -> {narration}")
                ok = ok and isinstance(narration, str) and len(narration) > 0
            except Exception as exc:  # noqa: BLE001
                lines.append(f"scan-vision pipeline FAIL: {type(exc).__name__}: {exc}")
                ok = False
            lines.append("PASS: M12 + scan-vision pipeline OK in the frozen runtime" if ok
                         else "FAIL: backend loaded but outputs failed sanity checks")
            rc = 0 if ok else 1
    except Exception as exc:  # noqa: BLE001
        lines.append(f"FAIL: {type(exc).__name__}: {exc}")
        lines.append(traceback.format_exc())
        rc = 1

    summary = "\n".join(lines)
    for ln in lines:
        print(ln)
    try:
        from mast._runtime_paths import project_root
        out = project_root() / "vision_selftest_result.txt"
        out.write_text(summary + f"\n\nexit={rc}\n", encoding="utf-8")
        print(f"[selftest] wrote {out}")
    except Exception:
        pass
    return rc


def _run_selftest_pyruntime() -> int:
    """Headless self-test for the DP analysis runtime (bundled CPython 3.13).

    Verifies the SHIPPED ``MASTv2/pyruntime`` can import numpy/scipy/matplotlib,
    render a figure through Agg and write it to disk — and that it CANNOT import
    ``nanonis_spm`` or ``mast``.

    That last check is not a nice-to-have. "The data-processing agent can't touch
    the instrument" is a claim whose ONLY mechanism is that those packages are
    absent from this interpreter — DP has no SafetyGate and no HITL. So a leak
    here is a FAIL, not a warning.

    Writes ``<project_root>/pyruntime_selftest_result.txt`` and returns 0/1.
    Run via ``MAST.exe --selftest-pyruntime``.
    """
    import traceback

    # Same line as run_service / _run_install_skillpack: all one-shot entry points
    # must agree on where the user data lives. Without it a frozen build's
    # project_root() falls back to the exe directory, so result files land in the
    # install directory instead of the data root.
    os.environ.setdefault("MAST2_PROJECT_ROOT", str(_user_root()))

    lines: list[str] = ["=== MAST pyruntime self-test ==="]
    rc = 1
    try:
        from mast.pyexec import runtime as R

        rep = R.probe_runtime(refresh=True)
        rt = rep.runtime
        if rt is None:
            lines.append("FAIL: no usable analysis runtime found")
            lines.append(rep.why_not())
        else:
            lines.append(f"runtime: {rt.kind} @ {rt.exe}")
            res = R.selftest(rt, refresh=True)
            lines.append(f"python : {res.python}")
            for k, v in sorted(res.packages.items()):
                lines.append(f"  {k} {v}")
            if res.missing:
                lines.append("optional missing: " + ", ".join(res.missing))
            lines.append(f"figure : {res.figure}")
            lines.append(f"isolated: {res.isolated}")
            if res.leaks:
                # B1 is broken: far more serious than a missing optional library,
                # so this must fail rather than just warn.
                lines.append(
                    "FAIL: B1 BROKEN — the analysis child can import "
                    + ", ".join(res.leaks)
                    + ". That is the whole mechanism behind 'DP cannot reach the "
                      "instrument'; DP has no SafetyGate and no HITL.")
            elif not res.ok:
                lines.append("FAIL: " + "; ".join(res.problems))
                if res.stderr:
                    lines.append("--- child stderr ---")
                    lines.append(res.stderr)
            elif rt.kind != "bundled":
                # The release smoke test must run against the real artifact:
                # resolving to dev/system means the bundled runtime is missing.
                lines.append(
                    f"FAIL: resolved to '{rt.kind}', not the bundled runtime — "
                    "MASTv2/pyruntime is missing from this build (build.ps1 "
                    "Step 4.7). It would still work here, but a target "
                    "machine has no dev venv to fall back to.")
            else:
                lines.append("PASS: bundled analysis runtime OK, no instrument leak")
                rc = 0
    except Exception as exc:  # noqa: BLE001
        lines.append(f"FAIL: {type(exc).__name__}: {exc}")
        lines.append(traceback.format_exc())
        rc = 1

    summary = "\n".join(lines)
    for ln in lines:
        print(ln)
    try:
        from mast._runtime_paths import project_root
        out = project_root() / "pyruntime_selftest_result.txt"
        out.write_text(summary + f"\n\nexit={rc}\n", encoding="utf-8")
        print(f"[selftest] wrote {out}")
    except Exception:
        pass
    return rc


def _run_install_skillpack(pack_path: str) -> int:
    """Install a signed skill pack from a local .zip and report as JSON.

    This is the entry point an out-of-band push script drives over SSH.
    It exists so that script does NOT have to poke at ``_internal`` to import
    mast — that would put ``nanonis_spm`` on the child's path, which is
    exactly the thing B1 rests on NOT happening.

    Prints one ``INSTALL{json}`` line so the caller can parse a result rather
    than scrape prose. Returns 0 only if the pack verified AND installed.

    ``MAST2_PROJECT_ROOT`` must point at the user root first, exactly as in
    :func:`run_service`. Otherwise ``mast._runtime_paths.project_root()`` falls
    back to the exe directory in a frozen build and the overlay resolves to
    ``<install dir>\\config\\skill_overlay``, while the running service uses
    ``<user root>\\config\\skill_overlay`` (the root the launcher resolves via
    ``data_dir.txt``). The install would then report success while the service
    never sees the pack (``/api/skill-overlay/packs`` stays empty).
    """
    import json
    import traceback

    # Same line as run_service: both entry points must agree on the user data root.
    _root = _user_root()
    os.environ.setdefault("MAST2_PROJECT_ROOT", str(_root))

    rc = 1
    try:
        from mast.update import skillpack_client as SC

        res = SC.install_pack_file(pack_path)
        print("INSTALL" + json.dumps(res.as_dict(), ensure_ascii=False))
        print(res.describe())
        rc = 0 if res.ok else 1
    except Exception as exc:  # noqa: BLE001
        print("INSTALL" + json.dumps(
            {"ok": False, "reasons": [f"{type(exc).__name__}: {exc}"]},
            ensure_ascii=False))
        print(traceback.format_exc())
        rc = 1
    return rc


def _run_satellite_admin(argv: list[str]) -> int:
    """``MAST.exe --satellite-admin <new-token|list|show-pin|enable|disable> ...``:
    satellite token and pin management (:mod:`mast.satellite.admin`). A frozen
    build has no ``python -m``, so this is the entry point.

    :func:`main` dispatches here BEFORE the launcher log is attached: the token
    printed by ``new-token`` must appear only on the caller's stdout and never be
    teed into ``experiments/logs/launcher-*.log``.

    Uses the same user root as run_service; without it a frozen build's
    ``project_root()`` falls back to the exe directory and the token hash would be
    written to ``<install dir>\\config\\satellites.json`` while the running service
    reads the one under the user root (same trap as _run_install_skillpack).

    MAST.exe is built with console=False: stdout exists only when started via
    ``cmd /c`` (PowerShell's ``&`` does not pass handles, see
    push_skillpack_to_rig.py); without stdout admin changes nothing and returns 3.
    """
    os.environ.setdefault("MAST2_PROJECT_ROOT", str(_user_root()))
    from mast.satellite.admin import launcher_main

    return launcher_main(argv)


# ── Top-level entry ──────────────────────────────────────────────────

def _main_inner() -> int:
    """Real main, wrapped by main() in a try/except for crash logging."""
    parser = argparse.ArgumentParser(
        prog="MAST",
        description="MAST desktop launcher / service",
        add_help=True,
    )
    parser.add_argument(
        "--service-mode",
        action="store_true",
        help="skip the launcher window and boot the main Gradio service directly",
    )
    parser.add_argument(
        "--push-server-mode",
        action="store_true",
        help="boot the intranet push-update server (mast.update.server) on port 8765",
    )
    parser.add_argument(
        "--publish", metavar="SETUP_EXE",
        help="(admin) publish a new version: copy the given setup.exe + write manifest "
             "into <data>/push_distribution/. Requires --version.",
    )
    parser.add_argument(
        "--version", dest="publish_version",
        help="version string for --publish (e.g. 0.3.2)",
    )
    parser.add_argument(
        "--selftest-vision",
        action="store_true",
        help="headless M12 vision self-test (verify the frozen bundle loads "
             "torch/timm/peft + the DINOv3 backbone and infers); writes "
             "vision_selftest_result.txt and exits",
    )
    parser.add_argument(
        "--install-skillpack", metavar="ZIP", default="",
        help="install a signed skill pack from a local .zip (verifies the "
             "Ed25519 signature and every file hash) and exit; prints an "
             "INSTALL{json} line. Used by push_skillpack_to_rig.py",
    )
    parser.add_argument(
        "--selftest-pyruntime",
        action="store_true",
        help="headless self-test of the bundled DP analysis runtime (numpy/"
             "scipy/matplotlib import + Agg figure + NO nanonis_spm leak); "
             "writes pyruntime_selftest_result.txt and exits",
    )
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="start the launcher minimized to the system tray (used by the "
             "open-on-boot auto-start entry so it doesn't pop a window each login)",
    )
    args, _unknown = parser.parse_known_args()

    if args.selftest_vision:
        return _run_selftest_vision()
    if args.selftest_pyruntime:
        return _run_selftest_pyruntime()
    if args.install_skillpack:
        return _run_install_skillpack(args.install_skillpack)
    if args.service_mode:
        return run_service()
    if args.push_server_mode:
        return run_push_server()
    if args.publish:
        return _run_publish(args.publish, args.publish_version)

    # Launcher mode (default). Before opening Tk, check for a pending update.
    # If one is sitting in <data>/pending_update/ + verifies, the user MUST
    # accept it before they can use MAST — that's the "force update" UX.
    user_root = _user_root()
    _load_lang(user_root)

    # ── Single-instance check: if another launcher is already running, ping
    # it to bring its window to the foreground and exit cleanly.
    existing = _read_launcher_lock(user_root)
    if existing is not None:
        existing_pid, existing_port = existing
        print(f"[launcher.main] another MAST launcher is running (PID {existing_pid}); "
              f"asking it to come to foreground")
        if _send_bring_foreground(existing_port):
            print("[launcher.main] foreground request sent — exiting this instance.")
            return 0
        else:
            # IPC failed but lock is fresh — best to surface a message and exit
            # rather than start a competing launcher
            print(f"[launcher.main] could not reach existing launcher on port {existing_port}; "
                  f"exiting to avoid duplicate. Run with --force-launch to override.")
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0,
                    _tr(f"MAST 启动器 (PID {existing_pid}) 已在运行，但无法被唤起。\n"
                        f"请在任务管理器关闭旧进程，或重启 MAST 启动器。",
                        f"MAST Launcher (PID {existing_pid}) is already running but could not be "
                        f"brought to the foreground.\nClose the old process in Task Manager, "
                        f"or restart the MAST Launcher."),
                    _tr("MAST 启动器", "MAST Launcher"),
                    0x30,  # MB_ICONWARNING
                )
            except Exception:
                pass
            return 0

    # OTA apply order (each step no-ops if nothing pending):
    #   1. Hot-apply a data-only / SPA / docs delta live (safe while MAST runs).
    #   2. Else if a CODE delta is pending, spawn the offline self-updater (waits
    #      for MAST to exit → swaps locked files → relaunches) and exit now.
    #   3. Else run a pending full installer.
    _apply_pending_delta_if_safe(user_root)
    if _apply_pending_delta_offline_if_needed(user_root):
        return 0
    if _check_and_run_pending_update(user_root):
        return 0

    # Launcher mode (default). DPI awareness MUST be set before Tk init,
    # otherwise Windows will bitmap-stretch all our widgets at 200% scale.
    _enable_dpi_awareness()

    try:
        import tkinter  # noqa: F401
    except Exception:
        print("tkinter unavailable — falling back to direct service mode.")
        return run_service()

    app = LauncherApp(user_root, start_minimized=bool(getattr(args, "minimized", False)))
    return app.run()


def main() -> int:
    """Top-level entry. Sets up file logging FIRST so any later crash is
    captured to experiments/logs/launcher-YYYYMMDD.log, then dispatches.

    Critical for diagnosing bundled-app startup failures: with PyInstaller
    --noconsole, sys.stdout/stderr go to NUL by default, so we have no
    other way to learn what crashed.
    """
    argv = sys.argv[1:]
    if "--satellite-admin" in argv:
        # Dispatch before the log tee: the token must never enter the launcher log
        # (see _run_satellite_admin).
        try:
            return _run_satellite_admin(argv[argv.index("--satellite-admin") + 1:])
        except Exception:  # noqa: BLE001 — in a frozen build an uncaught exception pops a dialog nobody can dismiss over ssh
            import traceback
            traceback.print_exc()
            return 1

    user_root = _user_root()
    _load_lang(user_root)
    try:
        _ensure_user_dirs(user_root)
        log_path = _setup_persistent_logging(user_root)
        print(f"[launcher.main] logging to: {log_path}")
    except Exception as exc:
        # If logging setup itself crashes, there is nothing useful we can do
        # except let the original error propagate.
        print(f"[launcher.main] logging setup failed: {exc}")
        log_path = None

    try:
        return _main_inner()
    except SystemExit:
        raise
    except BaseException:
        import traceback, datetime
        print(
            f"[launcher.main] FATAL EXCEPTION at "
            f"{datetime.datetime.now().isoformat()}:"
        )
        traceback.print_exc()
        # In bundled mode the user may not see any window at all, so try to
        # surface the log path via a Windows MessageBox before dying.
        if _frozen() and log_path is not None:
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0,
                    _tr(f"MAST 启动失败。详细日志已写入：\n\n{log_path}\n\n"
                        "请把这个文件发给开发者诊断。",
                        f"MAST failed to start. A detailed log was written to:\n\n{log_path}\n\n"
                        "Please send this file to the developers for diagnosis."),
                    _tr("MAST 启动失败", "MAST failed to start"),
                    0x10,  # MB_ICONERROR
                )
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
