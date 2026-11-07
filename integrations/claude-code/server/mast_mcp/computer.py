"""Computer tools: commands, files and screenshots on the MAST computer itself.

A port of Remote Hands (a small Go program that lets Claude Code or Codex on
another computer control this one over Tailscale) into MAST's Remote AI
endpoint, next to the ``mast_*`` instrument tools, so an AI that helps at the
instrument can also look after its computer the way a person at the keyboard
would: run a command, read or fix a file, see the screen. Tool names,
arguments, limits and answers follow Remote Hands (``tools.go``,
``textcodec.go``, ``sysinfo.go``), with "the remote computer" replaced by "the
MAST computer <name>". Commands and background jobs live in ``computer_jobs``.

``ComputerTools`` is a tool provider like ``tools.Toolbox`` (``list_tools``,
``has_tool``, ``call_tool``). The launcher owns two things: the operator's
switch (``allowed``, asked on every call; while it is off every tool refuses
and nothing runs) and the activity log (``activity(who, kind, detail)``, told
about every action with a plain, untranslated detail).

Time: no call waits longer than 55 s, because MCP clients stop waiting for an
HTTP answer after 60 s; slow commands keep running in the background and the
AI follows them with check_command. Waits watch the call's stop signal, so a
cancelled call or a server that is shutting down is never held.

Sizes: a command's output is capped per read (first 8 KB, last 24 KB) and
read_file answers with at most 40 KB, because Claude Code moves tool results
over 50,000 characters into a file; screenshots are JPEGs of at most 1568 px on
the long side, the largest image Claude looks at without scaling it down.

Text files: UTF-8 (with or without BOM), UTF-16 with BOM and, read-only, the
legacy Windows code pages (GBK on Chinese, windows-1250 on Czech systems).
edit_file only rewrites a file that would come back byte for byte, in its own
encoding and line endings.

MAST addition: run_command refuses commands that would stop Nanonis, LabVIEW or
MAST itself (``kill_guard``). Force-stopping Nanonis can leave its TCP
interface broken until it is restarted by hand; the instrument is stopped
through MAST's own tools.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import math
import os
import re
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from . import computer_jobs as jobs
from .tools import RESULT_SIZE_CHARS, ToolOutput

log = logging.getLogger("mast_mcp.computer")

MAX_FILE_SIZE = 20 << 20        # read_file and edit_file
MAX_READ_OUTPUT = 40 << 10      # bytes of lines in one read_file answer
MAX_LINE_LENGTH = 2000          # characters; longer lines are shortened
DEFAULT_LINES = 2000
MAX_LINES = 100_000
MAX_LIST_ENTRIES = 1000
MAX_WAIT_S = 55                 # MCP clients stop waiting for an HTTP answer after 60 s
RUN_WAIT_S = 45
CHECK_WAIT_S = 30
DEFAULT_TIMEOUT_S = 1800
MAX_TIMEOUT_S = 86400
MAX_SHOT_SIDE = 1568
JPEG_QUALITY = 80

#: "refused": the operator's switch is off (detail: the tool); "guarded": run_command
#: refused a command that would stop Nanonis, LabVIEW or MAST (detail: the command).
ACTIVITY_KINDS = frozenset({"info", "run", "check", "stop", "read", "write", "edit", "list",
                            "screenshot", "refused", "guarded"})

SWITCHED_OFF = (
    "Computer control is switched off on this MAST computer, so {tool} did not run. The "
    "operator can turn it on in the MAST launcher (section \"Remote AI\", option \"Also allow "
    "commands, files and screenshots\"); ask them with mast_ask_operator if you need it.")

_WINDOWS = os.name == "nt"
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_SURROGATE = re.compile("[\ud800-\udfff]")


class ArgError(Exception):
    """The tool was called with arguments it cannot use."""


class ToolError(Exception):
    """The tool failed in an expected way; the message is the whole answer."""


# ── arguments ────────────────────────────────────────────────────────────────
def _scrub(text: str) -> str:
    # JSON may carry lone surrogates; they cannot be encoded, so they become U+FFFD
    # (what Go's JSON decoder makes of them).
    return _SURROGATE.sub("\ufffd", text)


def _string(args: dict, key: str) -> str:
    value = args.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ArgError(f"{key} must be a string")
    return _scrub(value)


def _flex_int(args: dict, key: str) -> int | None:
    """A whole number, also when it came as a string or as 3.0 (Remote Hands' flexInt)."""
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().strip('"').strip()
        if text in ("", "null"):
            return None
        try:
            value = float(text)
        except ValueError:
            raise ArgError(f"{key}: {json.dumps(value)} is not a number") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArgError(f"{key}: {json.dumps(value)} is not a number")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ArgError(f"{key}: {value} is not a number")
        return int(value)
    return value


def _flex_bool(args: dict, key: str) -> bool:
    """true/false, also as "true", "yes", 1 or "0" (Remote Hands' flexBool)."""
    value = args.get(key)
    if value is None or isinstance(value, bool):
        return bool(value)
    text = str(value).strip().strip('"').lower() if isinstance(value, (str, int)) else None
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no", "", "null"):
        return False
    raise ArgError(f"{key}: {json.dumps(value)} is not true or false")


def _seconds(value: int | None, default: int, lo: int, hi: int) -> int:
    return default if value is None else min(max(value, lo), hi)


# ── paths ────────────────────────────────────────────────────────────────────
_GIT_BASH_DRIVE = re.compile(r"^/([a-zA-Z])(/.*)?$")
_DRIVE_RELATIVE = re.compile(r"^([a-zA-Z]):([^\\/].*)?$")
_WIN_ENV_VAR = re.compile(r"%([A-Za-z0-9_()]+)%")
_PS_ENV_VAR = re.compile(r"\$env:([A-Za-z0-9_]+)", re.IGNORECASE)
_PS_HOME = re.compile(r"^\$(home|\{home\})([\\/].*)?$", re.IGNORECASE)
_UNIX_ENV_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def home_dir() -> str:
    return os.path.expanduser("~")


def _join(base: str, rest: str) -> str:
    # Like Go's filepath.Join: a rest that starts with a separator stays inside base
    # (os.path.join would throw base away).
    return base + os.sep + rest if rest else base


def resolve_path(path: str) -> str:
    """An absolute path from what an AI wrote (Remote Hands' resolvePath).

    Understands surrounding quotes, "~" and relative paths (from the home folder);
    on Windows also %VAR%, $env:VAR, $HOME, Git Bash style "/c/Users/...",
    "\\Users" (on the home drive) and "C:Users"; elsewhere $VAR and ${VAR}.
    """
    p = path.strip()
    if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
        p = p[1:-1]
    if not p:
        raise ArgError("path is empty")
    home = home_dir()
    if _WINDOWS:
        p = _WIN_ENV_VAR.sub(lambda m: os.environ.get(m.group(1), m.group(0)), p)
        p = _PS_ENV_VAR.sub(lambda m: os.environ.get(m.group(1), m.group(0)), p)
        m = _PS_HOME.match(p)
        if m:
            p = home + (m.group(2) or "")
        if not p:
            raise ArgError("path is empty")
        drive = _GIT_BASH_DRIVE.match(p)
        if drive:
            p = drive.group(1).upper() + ":" + (drive.group(2) or "") + "\\"
        elif (drive := _DRIVE_RELATIVE.match(p)):
            p = drive.group(1) + ":\\" + (drive.group(2) or "")
        elif p[0] in "\\/" and not p.startswith(("\\\\", "//")):
            p = os.path.splitdrive(home)[0] + p
    else:
        p = _UNIX_ENV_VAR.sub(lambda m: os.environ.get(m.group(1) or m.group(2), m.group(0)), p)
    if p == "~":
        p = home
    elif p.startswith(("~/", "~\\")):
        p = _join(home, p[2:])
    if not os.path.isabs(p):
        p = _join(home, p)
    return os.path.normpath(p)


def _os_error(exc: OSError, path: str) -> str:
    if isinstance(exc, FileNotFoundError):
        return f"{path} does not exist"
    if isinstance(exc, PermissionError):
        return f"{path}: access denied"
    return f"{path}: {exc.strerror or exc}"


def human_size(n: int) -> str:
    if n < 1 << 10:
        return f"{n} B"
    if n < 1 << 20:
        return f"{n / (1 << 10):.1f} KB"
    if n < 1 << 30:
        return f"{n / (1 << 20):.1f} MB"
    return f"{n / (1 << 30):.1f} GB"


def _read_regular_file(p: str) -> bytes:
    """A normal file of at most MAX_FILE_SIZE bytes; devices and pipes could be endless."""
    try:
        st = os.stat(p)
    except OSError as exc:
        raise ToolError(_os_error(exc, p)) from None
    if stat.S_ISDIR(st.st_mode):
        raise ToolError(f"{p} is a folder; use list_directory")
    if not stat.S_ISREG(st.st_mode):
        raise ToolError(f"{p} is not a normal file; use run_command to inspect it")
    if st.st_size > MAX_FILE_SIZE:
        raise ToolError(f"{p} is too big ({human_size(st.st_size)}); use run_command to look at "
                        "parts of it")
    try:
        with open(p, "rb") as f:
            data = f.read(MAX_FILE_SIZE + 1)
    except OSError as exc:
        raise ToolError(_os_error(exc, p)) from None
    if len(data) > MAX_FILE_SIZE:
        raise ToolError(f"{p} is too big; use run_command to look at parts of it")
    return data


# ── text encodings ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TextEncoding:
    """How a file was stored, so edit_file can write it back the same way."""
    name: str
    writable: bool = False      # edit_file may rewrite files in this encoding
    bom: bool = False           # UTF-8 with byte order mark
    utf16: str = ""             # "le" or "be"


def decode_text(data: bytes) -> tuple[str, TextEncoding, bool]:
    """File contents as text, and how they were encoded; ok is False for binary files."""
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data[3:].decode("utf-8"), TextEncoding("utf-8 with BOM", True, bom=True), True
        except UnicodeDecodeError:
            text = data[3:].decode("utf-8", "replace")
            return text, TextEncoding("utf-8 with BOM", bom=True), True
    # Lone surrogates become U+FFFD; such a file then fails edit_file's round trip.
    if data.startswith(b"\xff\xfe"):
        text = data[2:].decode("utf-16-le", "replace")
        return text, TextEncoding("utf-16le", True, utf16="le"), True
    if data.startswith(b"\xfe\xff"):
        text = data[2:].decode("utf-16-be", "replace")
        return text, TextEncoding("utf-16be", True, utf16="be"), True
    if b"\x00" in data[:8000]:
        return "", TextEncoding("binary"), False
    try:
        return data.decode("utf-8"), TextEncoding("utf-8", True), True
    except UnicodeDecodeError:
        pass
    legacy = decode_ansi(data)
    if legacy is not None:
        return legacy[0], TextEncoding(legacy[1]), True
    return (data.decode("utf-8", "replace"),
            TextEncoding("invalid utf-8 (bad bytes shown as \ufffd)"), True)


def encode_text(text: str, enc: TextEncoding) -> bytes:
    if enc.utf16 == "le":
        return b"\xff\xfe" + text.encode("utf-16-le")
    if enc.utf16 == "be":
        return b"\xfe\xff" + text.encode("utf-16-be")
    return (b"\xef\xbb\xbf" if enc.bom else b"") + text.encode("utf-8")


def decode_ansi(data: bytes) -> tuple[str, str] | None:
    """Text in a legacy Windows code page, decoded strictly, and a name for it."""
    if not data:
        return None
    for cp in _legacy_code_pages():
        try:
            text = data.decode(f"cp{cp}")
        except (UnicodeDecodeError, LookupError):
            continue
        return text, f"Windows code page {cp} (edit_file will not change it)"
    return None


@lru_cache(maxsize=1)
def _legacy_code_pages() -> tuple[int, ...]:
    # The system's ANSI and OEM code pages, then the usual one of the display
    # language, which matters when Windows is set to use UTF-8 for both.
    if not _WINDOWS:
        return ()
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        pages = (k32.GetACP(), k32.GetOEMCP(),
                 _language_code_page(k32.GetUserDefaultUILanguage() & 0xFFFF))
    except (ImportError, OSError, AttributeError):
        return ()
    out: list[int] = []
    for cp in pages:
        if cp and cp != 65001 and cp not in out:
            out.append(cp)
    return tuple(out)


def _language_code_page(lang: int) -> int:
    if lang in (0x0804, 0x1004):        # Chinese (PRC, Singapore)
        return 936
    if lang & 0x3FF == 0x04:            # other Chinese
        return 950
    if lang & 0x3FF == 0x05:            # Czech
        return 1250
    return 1252


def to_crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


# ── the accident guard ───────────────────────────────────────────────────────
# Not security (an AI that means to can always find another way): it catches the
# honest mistake of "Nanonis hangs, kill it". Force-stopping Nanonis mid-call can
# leave its TCP interface accepting connections but never answering until
# Nanonis is restarted by hand, and stopping the NI Service Locator breaks the
# same interface. Verbs: taskkill, Stop-Process and its aliases spps and kill,
# kill/pkill/killall, .Kill(), WMI/CIM Terminate, wmic ... delete, and stopping
# a service.
_KILL_VERB = re.compile(
    r"\btaskkill\b|\bstop-process\b|\bspps\b|\bkill(?:all)?\b|\bpkill\b|\bterminate\b"
    r"|\bwmic\b.*?\bdelete\b|\bstop-service\b|\bspsv\b|\brestart-service\b|\bnet\s+stop\b"
    r"|\bsc(?:\.exe)?\s+stop\b",
    re.IGNORECASE | re.DOTALL)
_GUARDED_NAME = re.compile(
    r"nanonis|labview|lvrt|nisvcloc|ni service locator|\bmast(?:\.exe)?\b|mast2",
    re.IGNORECASE)
_GUARDED_PROGRAM = re.compile(r"nanonis|labview|lvrt|nisvcloc|^mast(?:\.exe)?$|mast2",
                              re.IGNORECASE)
_NUMBER = re.compile(r"\b\d{1,10}\b")


def kill_guard(command: str) -> str | None:
    """Why ``command`` is refused, or None. Case-insensitive.

    A command is refused when it holds a process-stopping verb and names
    Nanonis, LabVIEW (labview, lvrt), the NI Service Locator or MAST (MAST.exe,
    mast2, mast2_launcher), or the process id of one of them or of this process.
    """
    verb = _KILL_VERB.search(command)
    if verb is None:
        return None
    named = _GUARDED_NAME.search(command)
    if named:
        what = f"{named.group(0)!r} together with {verb.group(0)!r}"
    else:
        numbers = {int(n) for n in _NUMBER.findall(command)}
        guarded = _guarded_pids() if numbers else {}
        hit = sorted(numbers & set(guarded))
        if not hit:
            return None
        what = f"the process id {hit[0]} ({guarded[hit[0]]}) together with {verb.group(0)!r}"
    return (f"Refused, nothing was run: this command looks like it would stop Nanonis, LabVIEW, "
            f"the NI Service Locator or MAST (it has {what}). Force-stopping Nanonis can break "
            "its TCP interface until someone restarts Nanonis by hand, and stopping MAST cuts "
            "the instrument off in the middle of its work. Stop the instrument with the MAST "
            "tools instead: mast_run with StopScan or WithdrawTip, and mast_cancel for a running "
            "MAST job. MAST's service is restarted with the mast_service tool or by the "
            "operator; ask with mast_ask_operator if one of these programs really has to be "
            "stopped. If you only meant to read or search (a log that mentions them, say), use "
            "read_file or list_directory.")


def _guarded_pids() -> dict[int, str]:
    """Process id -> name of this process (MAST) and the running programs the guard protects."""
    found = {os.getpid(): "MAST itself"}
    try:
        for pid, exe in _process_names().items():
            if _GUARDED_PROGRAM.search(exe):
                found.setdefault(pid, exe)
    except Exception:  # noqa: BLE001 - without the process list the names still count
        log.debug("could not list processes for the kill guard", exc_info=True)
    return found


def _process_names() -> dict[int, str]:
    """Process id -> program file name of every running process (Windows; empty elsewhere)."""
    if not _WINDOWS:
        return {}
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = k32.CreateToolhelp32Snapshot(0x2, 0)           # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return {}
    names: dict[int, str] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        more = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            names[entry.th32ProcessID] = entry.szExeFile
            more = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return names


# ── system facts ─────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _sys_info() -> dict:
    home = home_dir()
    return {"os": _os_description(), "arch": _architecture(), "user": _user(), "home": home,
            "desktop": _desktop_dir(home)}


def _os_description() -> str:
    if _WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            class OSVERSIONINFOW(ctypes.Structure):
                _fields_ = [("dwOSVersionInfoSize", wintypes.DWORD),
                            ("dwMajorVersion", wintypes.DWORD), ("dwMinorVersion", wintypes.DWORD),
                            ("dwBuildNumber", wintypes.DWORD), ("dwPlatformId", wintypes.DWORD),
                            ("szCSDVersion", ctypes.c_wchar * 128)]

            v = OSVERSIONINFOW()
            v.dwOSVersionInfoSize = ctypes.sizeof(v)
            # RtlGetVersion, unlike GetVersionEx, does not depend on the program's manifest.
            if ctypes.WinDLL("ntdll").RtlGetVersion(ctypes.byref(v)) != 0:
                return "Windows"
        except (ImportError, OSError, AttributeError):
            return "Windows"
        name = f"Windows {v.dwMajorVersion}"
        if v.dwMajorVersion == 10 and v.dwBuildNumber >= 22000:
            name = "Windows 11"
        return (f"{name} (version {v.dwMajorVersion}.{v.dwMinorVersion}, "
                f"build {v.dwBuildNumber})")
    if sys.platform == "darwin":
        version = _program_output(["sw_vers", "-productVersion"])
        return f"macOS {version}" if version else "macOS"
    if sys.platform.startswith("linux"):
        try:
            with open("/etc/os-release", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip("\"'")
        except OSError:
            pass
        return "Linux"
    return sys.platform


def _program_output(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10,
                              stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _architecture() -> str:
    if _WINDOWS:
        arch = (os.environ.get("PROCESSOR_ARCHITEW6432")
                or os.environ.get("PROCESSOR_ARCHITECTURE") or "")
        return {"AMD64": "amd64", "ARM64": "arm64", "X86": "x86"}.get(arch.upper(),
                                                                     arch.lower() or "unknown")
    return os.uname().machine or "unknown"


def _user() -> str:
    name = os.environ.get("USERNAME") or os.environ.get("USER") or os.environ.get("LOGNAME")
    domain = os.environ.get("USERDOMAIN") if _WINDOWS else None
    if name and domain:
        return f"{domain}\\{name}"
    return name or "unknown"


def _desktop_dir(home: str) -> str:
    """Where the Desktop really is: with OneDrive backup it is not in the home folder."""
    if _WINDOWS:
        try:
            path = _known_folder_desktop()
        except (ImportError, OSError, AttributeError):
            path = ""
        if path:
            return path
    elif sys.platform.startswith("linux"):
        path = _program_output(["xdg-user-dir", "DESKTOP"])
        if path and path != home:
            return path
    fallback = os.path.join(home, "Desktop")
    return fallback if os.path.isdir(fallback) else ""


def _known_folder_desktop() -> str:
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    folder_id_desktop = GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                             (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD,
                                             wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    buf = ctypes.c_void_p()
    hr = shell32.SHGetKnownFolderPath(ctypes.byref(folder_id_desktop), 0, None, ctypes.byref(buf))
    try:
        return ctypes.wstring_at(buf.value) if hr == 0 and buf.value else ""
    finally:
        if buf.value:
            ole32.CoTaskMemFree(buf)


def _local_time() -> str:
    # Weekday spelled out here: strftime's %A follows the locale MAST may have set.
    now = datetime.now().astimezone()
    offset = now.utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return (f"{_WEEKDAYS[now.weekday()]} {now:%Y-%m-%d %H:%M:%S} "
            f"(UTC{sign}{minutes // 60:02d}:{minutes % 60:02d})")


# ── screenshots ──────────────────────────────────────────────────────────────
def _pil():
    # Imported on first use: MAST.exe bundles Pillow; the plain MCP server's Python
    # (stdlib only) may not have it, and then only this tool is missing.
    from PIL import Image, ImageGrab
    return Image, ImageGrab


@contextlib.contextmanager
def _dpi_aware_thread():
    """Per-monitor (v2) DPI awareness for this thread while the block runs, then the old one.

    Monitor rectangles and the grab are then both in physical pixels, even in a process
    that is not DPI aware (which would otherwise see scaled sizes and capture only part
    of a scaled screen).
    """
    set_context = None
    old = None
    try:
        import ctypes
        set_context = ctypes.WinDLL("user32").SetThreadDpiAwarenessContext
        set_context.restype = ctypes.c_void_p
        set_context.argtypes = [ctypes.c_void_p]
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, else ..._PER_MONITOR_AWARE.
        old = set_context(-4) or set_context(-3)
    except (ImportError, OSError, AttributeError):
        set_context = None          # before Windows 10 1607 the process setting applies
    try:
        yield
    finally:
        if set_context is not None and old:
            set_context(old)


def _monitors() -> list[tuple[int, int, int, int, bool]]:
    """Every monitor as (left, top, right, bottom, primary), in virtual-screen pixels."""
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                               ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), proto,
                                           wintypes.LPARAM]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    found = []

    def each(monitor, _dc, rect, _data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r, primary = info.rcMonitor, bool(info.dwFlags & 1)       # MONITORINFOF_PRIMARY
        else:
            r, primary = rect.contents, False
        found.append((r.left, r.top, r.right, r.bottom, primary))
        return True

    callback = proto(each)
    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        raise OSError(ctypes.get_last_error(), "EnumDisplayMonitors failed")
    return found


def _virtual_origin() -> tuple[int, int]:
    import ctypes
    metric = ctypes.WinDLL("user32").GetSystemMetrics
    return metric(76), metric(77)       # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN


def _grab_screens(grab) -> list[tuple[object, bool]]:
    """One (image, primary) per screen, primary first, in physical pixels."""
    if not _WINDOWS:
        return [(grab.grab(), False)]
    with _dpi_aware_thread():
        monitors = _monitors()
        left0, top0 = _virtual_origin()
        # The grab's top-left corner is the virtual screen's (SM_X/YVIRTUALSCREEN).
        full = grab.grab(all_screens=True)
    width, height = full.size
    shots = []
    for left, top, right, bottom, primary in sorted(monitors, key=lambda m: not m[4]):
        box = (max(0, left - left0), max(0, top - top0),
               min(width, right - left0), min(height, bottom - top0))
        if box[2] > box[0] and box[3] > box[1]:
            shots.append((full.crop(box), primary))
    return shots or [(full, False)]


def downscale(img, max_side: int = MAX_SHOT_SIDE):
    """``img`` with neither side above ``max_side``, averaging pixels (keeps text readable)."""
    w, h = img.size
    if w <= max_side and h <= max_side:
        return img
    scale = max_side / max(w, h)
    size = (max(1, int(w * scale + 0.5)), max(1, int(h * scale + 0.5)))
    image, _ = _pil()
    return img.resize(size, getattr(image, "Resampling", image).BOX)


def _jpeg_b64(img) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ── the provider ─────────────────────────────────────────────────────────────
def _tool(name: str, title: str, description: str, properties: dict, required: tuple = (), *,
          read_only: bool = False, open_world: bool = False, large: bool = False) -> dict:
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    out = {"name": name, "title": title, "description": description, "inputSchema": schema,
           "annotations": {"title": title, "readOnlyHint": read_only,
                           "destructiveHint": not read_only, "idempotentHint": read_only,
                           "openWorldHint": open_world}}
    if large:
        out["_meta"] = {"anthropic/maxResultSizeChars": RESULT_SIZE_CHARS}
    return out


def _p(kind: str, description: str) -> dict:
    return {"type": kind, "description": description}


def _error(text: str) -> ToolOutput:
    return ToolOutput(text, True)


class ComputerTools:
    """The computer tools of one MAST computer: what the MCP endpoint lists and calls."""

    def __init__(self, *, name: str, address: str = "", activity=None, allowed=None,
                 product: str = ""):
        self.name = name
        self.address = address
        self.product = product
        self._activity = activity
        self._allowed = allowed
        self._jobs = jobs.JobManager()
        self._handlers = {
            "system_info": self._system_info, "run_command": self._run_command,
            "check_command": self._check_command, "read_file": self._read_file,
            "write_file": self._write_file, "edit_file": self._edit_file,
            "list_directory": self._list_directory, "take_screenshot": self._take_screenshot,
        }

    # ── provider interface ───────────────────────────────────────────────
    def list_tools(self) -> list[dict]:
        where = f"the MAST computer {json.dumps(self.name, ensure_ascii=False)}"
        path = "File path: absolute, or relative to the home folder."
        if _WINDOWS:
            detach = "Start-Process"
        elif sys.platform == "darwin":
            detach = "open -a, or nohup program >/dev/null 2>&1 &"
        else:
            detach = "nohup program >/dev/null 2>&1 &"
        return [
            _tool("system_info", "System information",
                  f"Facts about {where}: operating system, user, home and Desktop folders, the "
                  "shell used by run_command, and local time. Call this first.",
                  {}, read_only=True),
            _tool("run_command", "Run a command",
                  f"Run a shell command on {where} and return its output (stdout and stderr "
                  f"together) and exit code. Shell: {jobs.shell_name()}. Every call starts a "
                  "fresh shell in cwd (default: the home folder); nothing carries over between "
                  "calls. If the command is still running after wait_seconds, you get the output "
                  "so far and a job_id, and it keeps running in the background: use check_command "
                  "to follow it. There is no keyboard input, so avoid commands that prompt. Start "
                  "GUI programs or servers that should stay open without waiting for them, and "
                  f"send their output elsewhere ({detach}). Commands that would stop Nanonis, "
                  "LabVIEW or MAST (taskkill, Stop-Process, kill and the like together with their "
                  "names or process ids) are refused: the instrument is stopped with the mast_* "
                  "tools.",
                  {"command": _p("string", "The command or script to run. Several lines are "
                                           "fine."),
                   "cwd": _p("string", "Folder to run in: absolute, or relative to the home "
                                       "folder. Default: the home folder."),
                   "wait_seconds": _p("integer", "How long to wait for the command to finish "
                                                 "before returning (1-55, default 45). Longer "
                                                 "commands simply continue in the background."),
                   "timeout_seconds": _p("integer", "Kill the command if it still runs after this "
                                                    "many seconds (default 1800, max 86400).")},
                  ("command",), open_world=True),
            _tool("check_command", "Check a running command",
                  "Follow a command that run_command left running in the background: waits up to "
                  "wait_seconds for it to finish and returns the output produced since the last "
                  "check, plus the exit code once it has finished. stop=true kills it together "
                  "with the processes it started. Without job_id, lists recent commands.",
                  {"job_id": _p("integer", "The job_id that run_command returned."),
                   "wait_seconds": _p("integer", "How long to wait for the command to finish "
                                                 "(0-55, default 30)."),
                   "stop": _p("boolean", "Kill the command instead of waiting.")}),
            _tool("read_file", "Read a text file",
                  f"Read a text file on {where}. Lines are returned as \"line number<TAB>text\" "
                  "(the numbers are not part of the file). Use offset and limit for long files. "
                  "Relative paths start in the home folder.",
                  {"path": _p("string", path),
                   "offset": _p("integer", "First line to return, counting from 1."),
                   "limit": _p("integer", f"Maximum number of lines to return (default "
                                          f"{DEFAULT_LINES}).")},
                  ("path",), read_only=True),
            _tool("write_file", "Write a text file",
                  f"Create or overwrite a text file on {where} (written as UTF-8). Missing "
                  "folders are created. append=true adds to the end of the file instead of "
                  "replacing it.",
                  {"path": _p("string", path),
                   "content": _p("string", "The complete text to write."),
                   "append": _p("boolean", "Add to the end of the file instead of replacing "
                                           "it.")},
                  ("path", "content")),
            _tool("edit_file", "Edit a text file",
                  f"Replace exact text in a text file on {where}. old_string must match the file "
                  "exactly (without the line numbers that read_file shows) and must occur exactly "
                  "once, unless replace_all=true. The file keeps its encoding and line endings.",
                  {"path": _p("string", path),
                   "old_string": _p("string", "The exact text to replace."),
                   "new_string": _p("string", "The text to put in its place."),
                   "replace_all": _p("boolean", "Replace every occurrence instead of exactly "
                                                "one.")},
                  ("path", "old_string", "new_string")),
            _tool("list_directory", "List a folder",
                  f"List a folder on {where} (default: the home folder): sub-folders first, then "
                  "files with size and last-modified time.",
                  {"path": _p("string", "Folder path: absolute, or relative to the home "
                                        "folder.")},
                  read_only=True),
            _tool("take_screenshot", "Take a screenshot",
                  f"Take a screenshot of every screen of {where}, one image per screen, scaled "
                  f"down to at most {MAX_SHOT_SIDE} pixels. Use it to see what the person there "
                  "sees, for example an error message or a dialog box. Fails while the screen is "
                  "locked or the computer sleeps.",
                  {}, read_only=True, large=True),
        ]

    def has_tool(self, name: str) -> bool:
        return name in self._handlers

    def call_tool(self, name: str, arguments: dict, signal, *, who: str = "") -> ToolOutput:
        handler = self._handlers.get(name)
        if handler is None:
            return _error(f"Unknown computer tool: {name}")
        if not self._is_allowed():
            self._report(who, "refused", name)
            return _error(SWITCHED_OFF.format(tool=name))
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _error(f"{name}: invalid arguments: they must be an object")
        started = time.monotonic()
        try:
            out = handler(dict(arguments), signal, who)
        except ArgError as exc:
            out = _error(f"{name}: invalid arguments: {exc}")
        except ToolError as exc:
            out = _error(str(exc))
        except Exception as exc:  # noqa: BLE001 - reported to the AI, logged in full
            log.exception("computer tool %s failed", name)
            out = _error(f"{name} hit an internal error in MAST's computer tools: {exc!r}")
        log.info("%s -> %s in %.1f s%s", name, "error" if out.is_error else "ok",
                 time.monotonic() - started,
                 f", {len(out.images)} image(s)" if out.images else "")
        return out

    def instructions(self) -> str:
        si = _sys_info()
        name = json.dumps(self.name, ensure_ascii=False)
        admin = ("a UAC prompt" if _WINDOWS else "sudo or a password")
        lines = [
            f"Computer tools: system_info, run_command, check_command, read_file, write_file, "
            f"edit_file, list_directory and take_screenshot act on the MAST computer {name} "
            f"({si['os']}, user {si['user']}, home folder {si['home']}) through MAST's Remote AI "
            "connection over Tailscale. Everything they do happens on THAT computer, not on the "
            "machine you are running on.",
            "- Call system_info first.",
            f"- run_command uses {jobs.shell_name()}. Every call starts a fresh shell: the current "
            "folder and variables do not carry over, so use the cwd argument. There is no "
            "keyboard, so commands that ask for input, a password or a confirmation fail or hang.",
            "- A command that runs longer than wait_seconds keeps running in the background; "
            "follow it with check_command.",
            "- The microscope is operated only through the mast_* tools. Never write or run "
            "scripts that talk to Nanonis directly (its TCP interface, nanonis_spm, LabVIEW): "
            "MAST holds that connection and applies its safety checks.",
            "- Never stop Nanonis, LabVIEW or MAST processes: force-stopping Nanonis can break its "
            "TCP interface until it is restarted by hand. run_command refuses commands that would "
            "(a process-stopping command together with their names or process ids). Stop the "
            "instrument with mast_run (StopScan, WithdrawTip) or mast_cancel; MAST's service is "
            "restarted with mast_service or by the operator.",
            "- People work at this instrument. Explain in plain words what you are going to do, "
            "prefer safe and reversible steps, and ask the operator with mast_ask_operator before "
            "deleting files, installing software or changing settings. Anything that needs "
            f"administrator rights ({admin}) must be approved by someone sitting at that computer.",
        ]
        if _WINDOWS:
            lines.append("- On Windows the Desktop and Documents folders are often inside "
                         "OneDrive; system_info shows the real Desktop path.")
        return "\n".join(lines) + "\n"

    def close(self) -> None:
        """Refuse new commands and kill the running ones, waiting about 3 s at most."""
        self._jobs.kill_all(close=True)

    def stop_all(self) -> int:
        """Kill the running commands but keep accepting new ones; how many were running.

        For the moment the operator switches computer control off: ``allowed`` then
        refuses new calls, and this ends what is already running.
        """
        return self._jobs.kill_all(close=False)

    # ── plumbing ─────────────────────────────────────────────────────────
    def _is_allowed(self) -> bool:
        if self._allowed is None:
            return True
        try:
            return bool(self._allowed())
        except Exception:  # noqa: BLE001 - a broken switch keeps the computer closed
            log.exception("the computer-control switch could not be read; refusing")
            return False

    def _report(self, who: str, kind: str, detail: str = "") -> None:
        if self._activity is None:
            return
        try:
            self._activity(who, kind, detail)
        except Exception:  # noqa: BLE001 - the activity log must never break a tool
            log.warning("activity callback failed for %s", kind, exc_info=True)

    # ── tools ────────────────────────────────────────────────────────────
    def _system_info(self, args: dict, signal, who: str) -> ToolOutput:
        self._report(who, "info")
        si = _sys_info()
        try:
            host = socket.gethostname()
        except OSError:
            host = ""
        lines = [f"Computer name: {self.name}"
                 + (f" (host name {host})" if host and host.lower() != self.name.lower() else ""),
                 f"Operating system: {si['os']}, {si['arch']}",
                 f"User: {si['user']}",
                 f"Home folder: {si['home']}"]
        if si["desktop"]:
            lines.append(f"Desktop folder: {si['desktop']}")
        lines.append(f"Shell used by run_command: {jobs.shell_name()}")
        if self.address:
            lines.append(f"Tailscale address: {self.address}")
        lines.append(f"Local time: {_local_time()}")
        if self.product:
            lines.append(f"Served by: {self.product}")
        return ToolOutput("\n".join(lines) + "\n")

    def _run_command(self, args: dict, signal, who: str) -> ToolOutput:
        command = _string(args, "command")
        cwd = _string(args, "cwd")
        wait_s = _seconds(_flex_int(args, "wait_seconds"), RUN_WAIT_S, 1, MAX_WAIT_S)
        timeout_s = _seconds(_flex_int(args, "timeout_seconds"), DEFAULT_TIMEOUT_S, 1,
                             MAX_TIMEOUT_S)
        if not command.strip():
            return _error("command is empty")
        refusal = kill_guard(command)
        if refusal:
            # Not "refused": that kind means the operator's switch is off.
            self._report(who, "guarded", jobs.one_line(command, 120))
            return _error(refusal)
        folder = home_dir()
        if cwd.strip():
            try:
                folder = resolve_path(cwd)
            except ArgError as exc:
                return _error(f"cwd: {exc}")
            if not os.path.isdir(folder):
                return _error(f"cwd {folder} is not an existing folder")
        self._report(who, "run", jobs.one_line(command, 120))
        try:
            job = self._jobs.start(command, folder, timeout_s)
        except (jobs.JobError, OSError, ValueError, subprocess.SubprocessError) as exc:
            return _error(f"Could not start the command: {exc}")
        return self._job_report(job, job.wait(signal, wait_s), signal)

    def _job_report(self, job: jobs.Job, finished: bool, signal) -> ToolOutput:
        # A call that was given up never shows its answer: keep the output for the
        # next check_command.
        if jobs.should_stop(signal):
            return _error(f"Request cancelled; the command (job_id {job.id}) continues in the "
                          "background.")
        return ToolOutput(job.report(finished))

    def _check_command(self, args: dict, signal, who: str) -> ToolOutput:
        job_id = _flex_int(args, "job_id")
        wait_s = _seconds(_flex_int(args, "wait_seconds"), CHECK_WAIT_S, 0, MAX_WAIT_S)
        stop = _flex_bool(args, "stop")
        if job_id is None:
            return ToolOutput(self._jobs.list())
        job = self._jobs.get(job_id)
        if job is None:
            return _error(f"There is no command with job_id {job_id} (old finished commands are "
                          "forgotten). Call check_command without job_id to list them.")
        if stop:
            self._report(who, "stop", str(job.id))
            return self._job_report(job, self._jobs.stop(job, signal), signal)
        self._report(who, "check", str(job.id))
        return self._job_report(job, job.wait(signal, wait_s), signal)

    def _read_file(self, args: dict, signal, who: str) -> ToolOutput:
        path = _string(args, "path")
        offset = _flex_int(args, "offset")
        limit = _flex_int(args, "limit")
        p = resolve_path(path)
        self._report(who, "read", p)
        data = _read_regular_file(p)
        text, enc, ok = decode_text(data)
        if not ok:
            return _error(f"{p} does not look like a text file ({human_size(len(data))}). Use "
                          "run_command to inspect it.")
        lines = text.split("\n")
        if len(lines) > 1 and lines[-1] == "":
            lines.pop()
        first = min(offset, len(lines) + 1) if offset is not None and offset > 1 else 1
        count = min(limit, MAX_LINES) if limit is not None and limit > 0 else DEFAULT_LINES
        body: list[str] = []
        size = 0
        last = first - 1
        for i in range(first, min(len(lines), first + count - 1) + 1):
            line = lines[i - 1].removesuffix("\r")
            if len(line) > MAX_LINE_LENGTH:
                line = line[:MAX_LINE_LENGTH] + " [... line shortened ...]"
            if size + len(line.encode("utf-8")) > MAX_READ_OUTPUT and i > first:
                break
            entry = f"{i:6d}\t{line}\n"
            body.append(entry)
            size += len(entry.encode("utf-8"))
            last = i
        if not lines or (len(lines) == 1 and lines[0] == ""):
            return ToolOutput(f"{p} is empty.\n")
        if last < first:
            return ToolOutput(f"{p} has only {len(lines)} lines.\n")
        head = f"{p}, lines {first}-{last} of {len(lines)}"
        if enc.name != "utf-8":
            head += f", encoding {enc.name}"
        if last < len(lines):
            head += f" (more: use offset {last + 1})"
        return ToolOutput(head + ":\n" + "".join(body))

    def _write_file(self, args: dict, signal, who: str) -> ToolOutput:
        path = _string(args, "path")
        content = args.get("content")
        if content is not None and not isinstance(content, str):
            raise ArgError("content must be a string")
        append = _flex_bool(args, "append")
        if content is None:
            return _error("content is missing")
        p = resolve_path(path)
        self._report(who, "write", p)
        if os.path.isdir(p):
            return _error(f"{p} is a folder")
        data = _scrub(content).encode("utf-8")
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "ab" if append else "wb") as f:
                f.write(data)
        except OSError as exc:
            return _error(_os_error(exc, p))
        return ToolOutput(f"{'Appended' if append else 'Wrote'} {len(data)} bytes to {p}.")

    def _edit_file(self, args: dict, signal, who: str) -> ToolOutput:
        path = _string(args, "path")
        old = _string(args, "old_string")
        new = _string(args, "new_string")
        replace_all = _flex_bool(args, "replace_all")
        if old == "":
            return _error("old_string is empty; use write_file to create or replace a whole file")
        if old == new:
            return _error("old_string and new_string are the same")
        p = resolve_path(path)
        self._report(who, "edit", p)
        data = _read_regular_file(p)
        text, enc, ok = decode_text(data)
        # Only rewrite files that come back byte for byte when nothing changes.
        if not ok or not enc.writable or encode_text(text, enc) != data:
            return _error(f"{p} is not a UTF-8 or UTF-16 text file that edit_file can rewrite "
                          f"safely ({enc.name}). Use run_command instead.")
        # The AI usually writes "\n" even when the file uses Windows line endings.
        crlf = "\r\n" in text
        if crlf:
            new = to_crlf(new)
        count = text.count(old)
        if count == 0 and crlf and to_crlf(old) != old:
            crlf_count = text.count(to_crlf(old))
            if crlf_count:
                old, count = to_crlf(old), crlf_count
        if count == 0:
            return _error(f"old_string was not found in {p}. Read the file again and copy the "
                          "text exactly, without the line numbers.")
        if count > 1 and not replace_all:
            return _error(f"old_string occurs {count} times in {p}. Include more of the "
                          "surrounding text so it is unique, or set replace_all=true.")
        text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        try:
            with open(p, "wb") as f:
                f.write(encode_text(text, enc))
        except OSError as exc:
            return _error(_os_error(exc, p))
        return ToolOutput(f"Edited {p}: replaced {count} occurrence(s).")

    def _list_directory(self, args: dict, signal, who: str) -> ToolOutput:
        path = _string(args, "path")
        p = resolve_path(path if path.strip() else "~")
        self._report(who, "list", p)
        try:
            with os.scandir(p) as it:
                entries = list(it)
        except OSError as exc:
            return _error(_os_error(exc, p))

        def is_dir(entry) -> bool:
            try:
                return entry.is_dir(follow_symlinks=False)
            except OSError:
                return False

        entries.sort(key=lambda e: (not is_dir(e), e.name.lower(), e.name))
        lines = [f"{p} ({len(entries)} items):"]
        for i, entry in enumerate(entries):
            if i == MAX_LIST_ENTRIES:
                lines.append(f"... and {len(entries) - i} more")
                break
            if is_dir(entry):
                lines.append(f"{'[folder]':<28} {entry.name}{os.sep}")
                continue
            try:
                st = entry.stat(follow_symlinks=False)
                when = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            except (OSError, ValueError, OverflowError):
                lines.append(f"{'?':<28} {entry.name}")
                continue
            lines.append(f"{human_size(st.st_size):>10}  {when}  {entry.name}")
        return ToolOutput("\n".join(lines) + "\n")

    def _take_screenshot(self, args: dict, signal, who: str) -> ToolOutput:
        self._report(who, "screenshot")
        try:
            _, grab = _pil()
        except ImportError as exc:
            return _error(f"take_screenshot needs Pillow, which this Python does not have ({exc}).")
        try:
            shots = _grab_screens(grab)
        except Exception as exc:  # noqa: BLE001 - Pillow raises OSError, ctypes others
            hint = " (is the screen locked or the computer asleep?)" if _WINDOWS else ""
            return _error(f"Could not take a screenshot: {exc}{hint}")
        images = []
        for i, (img, primary) in enumerate(shots, 1):
            small = downscale(img)
            label = (f"Screen {i} of {len(shots)}{' (primary)' if primary else ''}: "
                     f"{img.size[0]}x{img.size[1]} pixels")
            if small.size != img.size:
                label += f", shown at {small.size[0]}x{small.size[1]}"
            images.append({"label": label, "mime": "image/jpeg", "data_b64": _jpeg_b64(small)})
        return ToolOutput(f"{len(images)} screen(s), taken {time.strftime('%H:%M:%S')}.",
                          images=images)
