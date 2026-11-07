"""The computer tools of MAST's Remote AI endpoint (a port of Remote Hands).

Most tests drive ``ComputerTools.call_tool`` the way the MCP endpoint does and
read the answer. Commands run for real in the platform's shell (PowerShell on
Windows) and take a second or two each; the job machinery's corner cases
(output held open by a leftover process, the running-commands limit) use a
plain Python child or a lowered constant so they stay fast. No screen is ever
captured: the screenshot tests replace Pillow's grab and the monitor list.
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

import base64
import contextlib
import io
import os
import subprocess
import threading
import time

import pytest

from mast_mcp import computer, computer_jobs
from mast_mcp.stdio_rpc import CallSignal

WINDOWS = os.name == "nt"
TOOL_NAMES = {"system_info", "run_command", "check_command", "read_file", "write_file",
              "edit_file", "list_directory", "take_screenshot"}
FAILING = r"Get-Item -LiteralPath 'C:\definitely\not\here'" if WINDOWS else "false"


def sleep_cmd(seconds: int) -> str:
    return f"Start-Sleep -Seconds {seconds}" if WINDOWS else f"sleep {seconds}"


def a_signal() -> CallSignal:
    return CallSignal(threading.Event())


@pytest.fixture
def activity() -> list:
    return []


@pytest.fixture
def tools(activity):
    ct = computer.ComputerTools(
        name="mast-test", address="<rig-host>", product="MAST 0.0-test",
        activity=lambda who, kind, detail: activity.append((who, kind, detail)))
    yield ct
    ct.close()


def call(ct, name: str, args: dict | None = None, signal=None):
    return ct.call_tool(name, dict(args or {}), signal or a_signal(), who="pytest")


def ok(ct, name: str, args: dict | None = None) -> str:
    out = call(ct, name, args)
    assert not out.is_error, f"{name}({args}) failed: {out.text}"
    return out.text


def err(ct, name: str, args: dict | None = None) -> str:
    out = call(ct, name, args)
    assert out.is_error, f"{name}({args}) did not fail: {out.text}"
    return out.text


# ── arguments ────────────────────────────────────────────────────────────────
def test_numbers_and_booleans_may_come_as_strings():
    assert computer._flex_int({"n": "12"}, "n") == 12
    assert computer._flex_int({"n": 3.0}, "n") == 3
    assert computer._flex_int({"n": "7.9"}, "n") == 7
    assert computer._flex_int({"n": ""}, "n") is None
    assert computer._flex_int({}, "n") is None
    for bad in ("soon", True, [1], float("nan")):
        with pytest.raises(computer.ArgError):
            computer._flex_int({"n": bad}, "n")
    for yes in (True, "true", "TRUE", "yes", 1, "1"):
        assert computer._flex_bool({"b": yes}, "b") is True
    for no in (False, "false", "no", 0, "0", "", None):
        assert computer._flex_bool({"b": no}, "b") is False
    for bad in ("maybe", 2, 1.0):
        with pytest.raises(computer.ArgError):
            computer._flex_bool({"b": bad}, "b")
    assert computer._seconds(None, 45, 1, 55) == 45
    assert computer._seconds(999, 45, 1, 55) == 55
    assert computer._seconds(0, 45, 1, 55) == 1


def test_wrong_argument_types_are_tool_errors(tools):
    assert "invalid arguments" in err(tools, "run_command", {"command": 5})
    assert "invalid arguments" in err(tools, "run_command", {"command": "echo x",
                                                            "wait_seconds": "soon"})
    assert "invalid arguments" in err(tools, "read_file", {"path": "  "})
    assert "invalid arguments" in err(tools, "check_command", {"job_id": "one"})
    assert "Unknown computer tool" in err(tools, "mast_status")
    assert not tools.has_tool("mast_status") and all(tools.has_tool(n) for n in TOOL_NAMES)


# ── paths ────────────────────────────────────────────────────────────────────
@pytest.mark.skipif(not WINDOWS, reason="Windows path forms")
def test_resolve_path_understands_the_windows_forms(monkeypatch):
    home = r"E:\Homes\tester"
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.setenv("MAST_TEST_DIR", r"F:\data")
    monkeypatch.delenv("NOT_SET_ANYWHERE_42", raising=False)
    cases = {
        "~": home,
        "Desktop": home + r"\Desktop",
        "~/Documents/a.txt": home + r"\Documents\a.txt",
        "~\\x": home + r"\x",
        f'"{home}"': home,
        "'C:\\Program Files'": r"C:\Program Files",
        "/c/Users": r"C:\Users",
        "/d": "D:\\",
        "%USERPROFILE%\\Desktop": home + r"\Desktop",
        "%MAST_TEST_DIR%\\x": r"F:\data\x",
        "$env:MAST_TEST_DIR\\x": r"F:\data\x",
        "$ENV:mast_test_dir/x": r"F:\data\x",
        "$HOME\\x": home + r"\x",
        "${home}/x": home + r"\x",
        "\\Users\\Public": r"E:\Users\Public",
        "/Users/Public": r"E:\Users\Public",
        "C:Users": r"C:\Users",
        "c:": "c:\\",
        "\\\\server\\share\\x": r"\\server\share\x",
        "D:\\a\\..\\b": r"D:\b",
        "%NOT_SET_ANYWHERE_42%\\x": home + r"\%NOT_SET_ANYWHERE_42%\x",
    }
    for given, want in cases.items():
        assert computer.resolve_path(given) == want, given


@pytest.mark.skipif(WINDOWS, reason="POSIX path forms")
def test_resolve_path_understands_the_posix_forms(monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("MAST_TEST_DIR", "/srv/data")
    monkeypatch.delenv("NOT_SET_ANYWHERE_42", raising=False)
    cases = {"~": "/home/tester", "Desktop": "/home/tester/Desktop", "~/a/b": "/home/tester/a/b",
             "$HOME/x": "/home/tester/x", "${MAST_TEST_DIR}/x": "/srv/data/x",
             "'/tmp/a b'": "/tmp/a b",
             "$NOT_SET_ANYWHERE_42/x": "/home/tester/$NOT_SET_ANYWHERE_42/x"}
    for given, want in cases.items():
        assert computer.resolve_path(given) == want, given


def test_resolve_path_refuses_an_empty_path():
    for blank in ("", "   ", '""', "''"):
        with pytest.raises(computer.ArgError):
            computer.resolve_path(blank)


# ── commands ─────────────────────────────────────────────────────────────────
def test_run_command_returns_output_and_exit_codes(tools):
    out = ok(tools, "run_command", {"command": "echo hello-mast", "wait_seconds": 55})
    assert "Exit code: 0" in out and "hello-mast" in out
    assert "Exit code: 3" in ok(tools, "run_command", {"command": "exit 3", "wait_seconds": 55})
    assert "Exit code: 1" in ok(tools, "run_command", {"command": FAILING, "wait_seconds": 55})
    out = ok(tools, "run_command", {"command": "echo 'Příliš žluťoučký 你好'", "wait_seconds": 55})
    assert "Příliš žluťoučký 你好" in out


def test_run_command_uses_cwd(tools, tmp_path):
    where = "(Get-Location).Path" if WINDOWS else "pwd"
    out = ok(tools, "run_command", {"command": where, "cwd": str(tmp_path), "wait_seconds": 55})
    assert tmp_path.name.lower() in out.lower()
    assert "is not an existing folder" in err(tools, "run_command", {
        "command": "echo x", "cwd": str(tmp_path / "missing")})
    assert "command is empty" in err(tools, "run_command", {"command": "  "})


@pytest.mark.skipif(not WINDOWS, reason="Windows PowerShell 5.1")
def test_windows_powershell_5_exit_codes(tools, monkeypatch):
    """5.1 counts stderr of a program as failure; the epilogue ignores it when the program
    itself returned 0."""
    ps5 = os.path.join(computer_jobs.system_root(), "System32", "WindowsPowerShell", "v1.0",
                       "powershell.exe")
    monkeypatch.setenv("MAST_REMOTE_POWERSHELL", ps5)
    assert "5.1" in computer_jobs.shell_name()
    out = ok(tools, "run_command", {"command": 'cmd /c "echo to-stderr 1>&2" 2>&1',
                                    "wait_seconds": 55})
    assert "to-stderr" in out and "Exit code: 0" in out
    out = ok(tools, "run_command", {"command": "throw 'boom'", "wait_seconds": 55})
    assert "boom" in out and "Exit code: 1" in out


def test_a_long_command_continues_in_the_background(tools, activity):
    command = (f"echo started; {sleep_cmd(3)}; echo finished  # " + "x" * 150)
    out = ok(tools, "run_command", {"command": command, "wait_seconds": 1})
    assert "Still running" in out and "job_id 1" in out
    assert "job_id 1: running for" in ok(tools, "check_command")
    out = ok(tools, "check_command", {"job_id": 1, "wait_seconds": 30})
    assert "Exit code: 0" in out and "finished" in out
    assert "job_id 1: finished, exit code 0" in ok(tools, "check_command")
    run = [d for _, kind, d in activity if kind == "run"]
    assert len(run) == 1 and len(run[0]) == 120 and run[0].endswith("\u2026")
    assert ("pytest", "check", "1") in activity
    assert "There is no command with job_id 99" in err(tools, "check_command", {"job_id": 99})


def test_stop_kills_a_running_command(tools, activity):
    started = time.monotonic()
    assert "job_id 1" in ok(tools, "run_command", {"command": sleep_cmd(60), "wait_seconds": 1})
    out = ok(tools, "check_command", {"job_id": "1", "stop": "true"})
    assert "Stopped on request" in out and time.monotonic() - started < 30
    assert ("pytest", "stop", "1") in activity


def test_timeout_kills_a_command(tools):
    started = time.monotonic()
    out = ok(tools, "run_command", {"command": sleep_cmd(60), "timeout_seconds": 2,
                                    "wait_seconds": 55})
    assert "reached its timeout" in out and time.monotonic() - started < 30


def test_running_commands_are_limited(tools, monkeypatch):
    monkeypatch.setattr(computer_jobs, "MAX_RUNNING", 1)
    assert "Still running" in ok(tools, "run_command", {"command": sleep_cmd(30),
                                                        "wait_seconds": 1})
    out = err(tools, "run_command", {"command": "echo late"})
    assert "Could not start the command: 1 commands are still running" in out
    assert "Stopped on request" in ok(tools, "check_command", {"job_id": 1, "stop": True})
    assert "Exit code: 0" in ok(tools, "run_command", {"command": "echo now", "wait_seconds": 55})


def test_a_cancelled_call_returns_at_once_and_keeps_the_output(tools):
    signal = a_signal()
    threading.Timer(0.5, signal.cancelled.set).start()
    started = time.monotonic()
    out = call(tools, "run_command", {"command": f"echo kept-for-later; {sleep_cmd(30)}",
                                      "wait_seconds": 55}, signal)
    assert out.is_error and time.monotonic() - started < 5
    assert "Request cancelled; the command (job_id 1) continues in the background." == out.text
    out = ok(tools, "check_command", {"job_id": 1, "wait_seconds": 3})
    assert "Still running" in out and "kept-for-later" in out
    assert "Stopped on request" in ok(tools, "check_command", {"job_id": 1, "stop": True})


def test_commands_get_a_clean_environment(tools, monkeypatch):
    """PyInstaller's own variables stay behind (a child started with them can die with
    ERROR_BAD_EXE_FORMAT); git never prompts and nothing prints colour codes."""
    monkeypatch.setenv("_MEIPASS2", r"C:\frozen\mast")
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", r"C:\frozen")
    monkeypatch.setenv("MAST_TEST_KEPT", "kept")
    if WINDOWS:
        probe = ('echo "[$env:_MEIPASS2][$env:_PYI_APPLICATION_HOME_DIR][$env:NO_COLOR]'
                 '[$env:GIT_TERMINAL_PROMPT][$env:MAST_TEST_KEPT]"')
    else:
        probe = ('echo "[$_MEIPASS2][$_PYI_APPLICATION_HOME_DIR][$NO_COLOR]'
                 '[$GIT_TERMINAL_PROMPT][$MAST_TEST_KEPT]"')
    assert "[][][1][0][kept]" in ok(tools, "run_command", {"command": probe, "wait_seconds": 55})


def test_close_kills_running_commands_and_refuses_new_ones(tools):
    assert "Still running" in ok(tools, "run_command", {"command": sleep_cmd(60),
                                                        "wait_seconds": 1})
    started = time.monotonic()
    tools.close()
    assert time.monotonic() - started < 10
    assert "job_id 1: finished" in ok(tools, "check_command")
    assert "MAST is shutting down" in err(tools, "run_command", {"command": "echo late"})


def test_stop_all_ends_commands_but_accepts_new_ones(tools):
    assert "Still running" in ok(tools, "run_command", {"command": sleep_cmd(60),
                                                        "wait_seconds": 1})
    assert tools.stop_all() == 1
    assert "Stopped on request" in ok(tools, "check_command", {"job_id": 1, "wait_seconds": 10})
    assert "Exit code: 0" in ok(tools, "run_command", {"command": "echo again", "wait_seconds": 55})


def test_output_held_open_by_a_leftover_process_is_not_waited_for(tools, monkeypatch):
    """The command ends but a process it started keeps the output pipe: the job still
    finishes about EOF_GRACE_S later, with Remote Hands' note."""
    child = ("import subprocess, sys; "
             "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'], stdout=1); "
             "print('parent done', flush=True)")
    monkeypatch.setattr(computer_jobs, "shell_command", lambda command: ([sys.executable, "-c",
                                                                          child], None))
    monkeypatch.setattr(computer_jobs, "EOF_GRACE_S", 0.5)
    started = time.monotonic()
    out = ok(tools, "run_command", {"command": "anything", "wait_seconds": 30})
    assert "Exit code: 0" in out and "parent done" in out
    assert "still holding its output open" in out
    assert time.monotonic() - started < 4


def test_capped_buffer_keeps_head_and_tail():
    head, tail = computer_jobs.OUTPUT_HEAD, computer_jobs.OUTPUT_TAIL
    b = computer_jobs.CappedBuffer()
    b.write(b"a" * head)
    b.write(b"b" * 100_000)
    b.write(b"END")
    s = b.take()
    assert s.startswith("a" * head) and s.endswith("bEND")
    assert f"[... {100_003 - tail} bytes of output skipped ...]" in s
    assert len(s) <= head + tail + 100
    assert b.take() == ""
    b.write(b"small")
    assert b.take() == "small"


def test_capped_buffer_cleans_the_text():
    b = computer_jobs.CappedBuffer()
    b.write(b"\x1b[32;1mName\x1b[0m plain \x1b]0;title\x07done")
    assert b.take() == "Name plain done"
    b.write(b"\xef\xbb\xbfline1\r\nline2\r\n")
    assert b.take() == "line1\nline2\n"
    b.write(b"bad \xff byte")
    assert b.take() == "bad \ufffd byte"
    b.close()
    b.write(b"after close")
    assert b.take() == ""


# ── the accident guard ───────────────────────────────────────────────────────
@pytest.mark.parametrize("command", [
    "taskkill /IM Nanonis.exe /F",
    "TASKKILL /im NANONIS.EXE",
    "Stop-Process -Name nanonis -Force",
    "Get-Process *LabVIEW* | Stop-Process",
    "spps -Name lvrt",
    "kill -Name MAST",
    "taskkill /F /IM MAST.exe",
    "Get-Process python | Where-Object { $_.CommandLine -like '*mast2_launcher*' } | Stop-Process",
    "pkill -f mast2_launcher.py",
    "killall Nanonis",
    "wmic process where \"name='Nanonis.exe'\" delete",
    "(Get-Process Nanonis).Kill()",
    "Get-CimInstance Win32_Process -Filter \"Name='Nanonis.exe'\" | "
    "Invoke-CimMethod -MethodName Terminate",
    "Stop-Service NiSvcLoc",
    'net stop "NI Service Locator"',
])
def test_kill_guard_refuses_stopping_nanonis_labview_or_mast(command, monkeypatch):
    monkeypatch.setattr(computer, "_process_names", lambda: {})
    reason = computer.kill_guard(command)
    assert reason and reason.startswith("Refused, nothing was run")
    for must in ("TCP interface", "StopScan", "WithdrawTip", "mast_cancel", "mast_service"):
        assert must in reason


@pytest.mark.parametrize("command", [
    "Get-Process | Sort-Object CPU",
    "Get-Process Nanonis",
    "Get-Help Stop-Process",
    "Get-ChildItem D:\\Nanonis\\logs",
    "pkill mastodon",
    "echo skill",
    "Stop-Process -Id 5150",
])
def test_kill_guard_lets_other_commands_through(command, monkeypatch):
    monkeypatch.setattr(computer, "_process_names", lambda: {4242: "Nanonis.exe",
                                                             5150: "notepad.exe"})
    assert computer.kill_guard(command) is None


def test_kill_guard_knows_the_process_ids(monkeypatch):
    monkeypatch.setattr(computer, "_process_names", lambda: {4242: "Nanonis.exe",
                                                             5150: "notepad.exe"})
    assert "4242 (Nanonis.exe)" in computer.kill_guard("Stop-Process -Id 4242 -Force")
    assert "MAST itself" in computer.kill_guard(f"kill -9 {os.getpid()}")


def test_a_refused_command_runs_nothing(tools, activity, tmp_path):
    marker = tmp_path / "ran.txt"
    make = (f"New-Item -ItemType File -Path '{marker}'" if WINDOWS else f"touch '{marker}'")
    out = err(tools, "run_command", {"command": f"{make}; taskkill /IM Nanonis.exe /F",
                                     "wait_seconds": 55})
    assert "Refused, nothing was run" in out
    assert not marker.exists()
    # Its own kind: "refused" would tell the operator that the switch is off.
    command = f"{make}; taskkill /IM Nanonis.exe /F"
    assert activity == [("pytest", "guarded", computer.jobs.one_line(command, 120))]
    assert "No commands have been started yet." in ok(tools, "check_command")


@pytest.mark.skipif(not WINDOWS, reason="the process list is read on Windows only")
def test_the_process_list_is_read():
    names = computer._process_names()
    assert os.path.basename(sys.executable).lower() == names[os.getpid()].lower()


# ── files ────────────────────────────────────────────────────────────────────
def test_read_file_pages_through_lines(tools, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_bytes(b"alpha\nbeta\ngamma\nbeta\ndelta\n")
    out = ok(tools, "read_file", {"path": str(p)})
    assert f"{p}, lines 1-5 of 5:\n" in out and "     2\tbeta\n" in out
    out = ok(tools, "read_file", {"path": str(p), "offset": 2, "limit": "2"})
    assert "lines 2-3 of 5 (more: use offset 4)" in out and "alpha" not in out
    assert "has only 5 lines" in ok(tools, "read_file", {"path": str(p), "offset": 99})
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    assert ok(tools, "read_file", {"path": str(empty)}) == f"{empty} is empty.\n"


def test_read_file_shortens_long_lines_and_caps_the_answer(tools, tmp_path):
    p = tmp_path / "wide.txt"
    p.write_text("x" * 2500 + "\n" + ("y" * 1000 + "\n") * 100, encoding="utf-8")
    out = ok(tools, "read_file", {"path": str(p)})
    assert "x" * 2000 + " [... line shortened ...]" in out and "x" * 2001 not in out
    assert "(more: use offset " in out
    assert len(out.encode("utf-8")) < computer.MAX_READ_OUTPUT + 3000


def test_read_file_understands_the_encodings(tools, tmp_path, monkeypatch):
    bom = tmp_path / "bom.txt"
    bom.write_bytes(b"\xef\xbb\xbfGr\xc3\xbc\xc3\x9fe\n")
    out = ok(tools, "read_file", {"path": str(bom)})
    assert "Grüße" in out and "encoding utf-8 with BOM" in out
    for name, data in (("utf-16le", b"\xff\xfe" + "Grüße 你好\r\n".encode("utf-16-le")),
                       ("utf-16be", b"\xfe\xff" + "Grüße 你好\r\n".encode("utf-16-be"))):
        p = tmp_path / f"{name}.txt"
        p.write_bytes(data)
        out = ok(tools, "read_file", {"path": str(p)})
        assert "     1\tGrüße 你好\n" in out and f"encoding {name}" in out
    gbk = tmp_path / "gbk.txt"
    gbk.write_bytes("中文\n".encode("gbk"))
    monkeypatch.setattr(computer, "_legacy_code_pages", lambda: (936,))
    out = ok(tools, "read_file", {"path": str(gbk)})
    assert "中文" in out and "Windows code page 936 (edit_file will not change it)" in out
    monkeypatch.setattr(computer, "_legacy_code_pages", lambda: ())
    assert "invalid utf-8" in ok(tools, "read_file", {"path": str(gbk)})


@pytest.mark.skipif(not WINDOWS, reason="legacy code pages are Windows only")
def test_legacy_code_pages_skip_utf8_and_end_with_the_display_language():
    pages = computer._legacy_code_pages()
    assert 65001 not in pages and len(pages) == len(set(pages)) and pages
    assert computer._language_code_page(0x0804) == 936
    assert computer._language_code_page(0x0404) == 950
    assert computer._language_code_page(0x0405) == 1250
    assert computer._language_code_page(0x0409) == 1252


def test_read_file_refuses_binary_folders_and_big_files(tools, tmp_path, monkeypatch):
    binary = tmp_path / "data.bin"
    binary.write_bytes(b"\x01\x02\x00\x03")
    assert "does not look like a text file" in err(tools, "read_file", {"path": str(binary)})
    assert "is a folder; use list_directory" in err(tools, "read_file", {"path": str(tmp_path)})
    assert "does not exist" in err(tools, "read_file", {"path": str(tmp_path / "nope.txt")})
    monkeypatch.setattr(computer, "MAX_FILE_SIZE", 2)
    assert "is too big" in err(tools, "read_file", {"path": str(binary)})


def test_write_file_creates_folders_and_appends(tools, tmp_path):
    p = tmp_path / "sub" / "deeper" / "notes.txt"
    assert ok(tools, "write_file", {"path": str(p), "content": "alpha\n"}) == \
        f"Wrote 6 bytes to {p}."
    assert ok(tools, "write_file", {"path": str(p), "content": "你好\n", "append": "true"}) == \
        f"Appended 7 bytes to {p}."
    assert p.read_bytes() == "alpha\n你好\n".encode("utf-8")
    ok(tools, "write_file", {"path": str(p), "content": "a\ud800b"})          # lone surrogate
    assert p.read_bytes() == "a\ufffdb".encode("utf-8")
    assert "is a folder" in err(tools, "write_file", {"path": str(tmp_path), "content": "x"})
    assert "content is missing" in err(tools, "write_file", {"path": str(p)})


def test_edit_file_replaces_exactly_once_unless_told_otherwise(tools, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_bytes(b"alpha\nbeta\ngamma\nbeta\n")
    assert "2 times" in err(tools, "edit_file", {"path": str(p), "old_string": "beta",
                                                 "new_string": "BETA"})
    assert "replaced 1 occurrence(s)" in ok(tools, "edit_file", {
        "path": str(p), "old_string": "beta\ngamma", "new_string": "beta\nGAMMA"})
    assert "replaced 2 occurrence(s)" in ok(tools, "edit_file", {
        "path": str(p), "old_string": "beta", "new_string": "BETA", "replace_all": "true"})
    assert p.read_bytes() == b"alpha\nBETA\nGAMMA\nBETA\n"
    assert "was not found" in err(tools, "edit_file", {"path": str(p), "old_string": "nothere",
                                                       "new_string": "x"})
    assert "old_string is empty" in err(tools, "edit_file", {"path": str(p), "old_string": "",
                                                             "new_string": "x"})
    assert "are the same" in err(tools, "edit_file", {"path": str(p), "old_string": "BETA",
                                                      "new_string": "BETA"})


def test_edit_file_keeps_crlf_and_utf16(tools, tmp_path):
    crlf = tmp_path / "crlf.txt"
    crlf.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    ok(tools, "edit_file", {"path": str(crlf), "old_string": "one\ntwo", "new_string": "one\n2"})
    assert crlf.read_bytes() == b"one\r\n2\r\nthree\r\n"
    # A one-line old_string with a multi-line new_string must not bring bare "\n" in.
    ok(tools, "edit_file", {"path": str(crlf), "old_string": "2", "new_string": "two\nand a half"})
    assert crlf.read_bytes() == b"one\r\ntwo\r\nand a half\r\nthree\r\n"

    for enc, bom in (("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")):
        p = tmp_path / f"{enc}.txt"
        p.write_bytes(bom + "Grüße 你好\r\n".encode(enc))
        ok(tools, "edit_file", {"path": str(p), "old_string": "你好", "new_string": "Ahoj"})
        assert p.read_bytes() == bom + "Grüße Ahoj\r\n".encode(enc)

    bom8 = tmp_path / "bom8.txt"
    bom8.write_bytes(b"\xef\xbb\xbfkey = 1\n")
    ok(tools, "edit_file", {"path": str(bom8), "old_string": "1", "new_string": "2"})
    assert bom8.read_bytes() == b"\xef\xbb\xbfkey = 2\n"


def test_edit_file_leaves_files_it_cannot_write_back_alone(tools, tmp_path, monkeypatch):
    monkeypatch.setattr(computer, "_legacy_code_pages", lambda: (936,))
    cases = {
        "odd16.txt": b"\xff\xfea\x00\x00\xd8b\x00",         # UTF-16 with a lone surrogate
        "gbk.txt": "中文 abc\n".encode("gbk"),                # legacy code page: shown, not changed
        "data.bin": b"a\x00bc",
    }
    for name, data in cases.items():
        p = tmp_path / name
        p.write_bytes(data)
        out = err(tools, "edit_file", {"path": str(p), "old_string": "a", "new_string": "c"})
        assert "cannot" in out or "rewrite safely" in out
        assert p.read_bytes() == data, name
    assert "Windows code page 936" in err(tools, "edit_file", {
        "path": str(tmp_path / "gbk.txt"), "old_string": "abc", "new_string": "xyz"})
    monkeypatch.setattr(computer, "_legacy_code_pages", lambda: ())
    bad = tmp_path / "latin.txt"
    bad.write_bytes(b"caf\xe9 \xe8\n")
    assert "invalid utf-8" in err(tools, "edit_file", {"path": str(bad), "old_string": "caf",
                                                       "new_string": "tea"})


def test_list_directory_lists_folders_first_and_caps_entries(tools, tmp_path, monkeypatch):
    folder = tmp_path / "listing"           # tests/v2/conftest.py puts _sidecars in tmp_path
    for d in ("beta", "Alpha"):
        (folder / d).mkdir(parents=True)
    for f in ("b.txt", "A.txt", "c.txt"):
        (folder / f).write_bytes(b"12345")
    out = ok(tools, "list_directory", {"path": str(folder)})
    lines = out.splitlines()
    assert lines[0] == f"{folder} (5 items):"
    assert lines[1].startswith("[folder]") and lines[1].endswith("Alpha" + os.sep)
    assert lines[2].endswith("beta" + os.sep)
    assert [line.split()[-1] for line in lines[3:]] == ["A.txt", "b.txt", "c.txt"]
    assert lines[3].lstrip().startswith("5 B")
    monkeypatch.setattr(computer, "MAX_LIST_ENTRIES", 3)
    out = ok(tools, "list_directory", {"path": str(folder)})
    assert out.splitlines()[-1] == "... and 2 more" and "b.txt" not in out
    assert "does not exist" in err(tools, "list_directory", {"path": str(folder / "nope")})


def test_list_directory_defaults_to_the_home_folder(tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE" if WINDOWS else "HOME", str(home))
    (home / "here.txt").write_bytes(b"x")
    out = ok(tools, "list_directory", {})
    assert out.startswith(f"{home} (1 items):") and "here.txt" in out


# ── screenshots ──────────────────────────────────────────────────────────────
@pytest.fixture
def pil():
    pytest.importorskip("PIL")
    from PIL import Image, ImageGrab
    return Image, ImageGrab


def _decode(image: dict):
    from PIL import Image
    assert image["mime"] == "image/jpeg"
    return Image.open(io.BytesIO(base64.b64decode(image["data_b64"])))


def test_screenshot_gives_one_image_per_monitor_primary_first(tools, activity, pil, monkeypatch):
    Image, ImageGrab = pil
    # A secondary 1920x1080 monitor left of a primary 3840x2160 one.
    full = Image.new("RGB", (5760, 2160), (0, 0, 0))
    full.paste((255, 0, 0), (0, 0, 1920, 1080))
    full.paste((0, 0, 255), (1920, 0, 5760, 2160))
    grabs = []
    monkeypatch.setattr(computer, "_WINDOWS", True)
    monkeypatch.setattr(computer, "_dpi_aware_thread", contextlib.nullcontext)
    monkeypatch.setattr(computer, "_monitors", lambda: [(-1920, 0, 0, 1080, False),
                                                       (0, 0, 3840, 2160, True)])
    monkeypatch.setattr(computer, "_virtual_origin", lambda: (-1920, 0))
    monkeypatch.setattr(ImageGrab, "grab", lambda **kw: grabs.append(kw) or full)
    out = call(tools, "take_screenshot")
    assert not out.is_error, out.text
    assert grabs == [{"all_screens": True}]
    assert out.text.startswith("2 screen(s), taken ")
    first, second = out.images
    assert first["label"] == "Screen 1 of 2 (primary): 3840x2160 pixels, shown at 1568x882"
    assert second["label"] == "Screen 2 of 2: 1920x1080 pixels, shown at 1568x882"
    blue, red = _decode(first), _decode(second)
    assert blue.size == red.size == (1568, 882)
    r, g, b = blue.getpixel((784, 441))
    assert b > 200 and r < 50
    r, g, b = red.getpixel((784, 441))
    assert r > 200 and b < 50
    assert activity == [("pytest", "screenshot", "")]


def test_screenshot_elsewhere_grabs_once(tools, pil, monkeypatch):
    Image, ImageGrab = pil
    monkeypatch.setattr(computer, "_WINDOWS", False)
    monkeypatch.setattr(ImageGrab, "grab", lambda **kw: Image.new("RGB", (800, 600)))
    out = call(tools, "take_screenshot")
    assert not out.is_error and len(out.images) == 1
    assert out.images[0]["label"] == "Screen 1 of 1: 800x600 pixels"
    assert _decode(out.images[0]).size == (800, 600)


def test_screenshot_reports_a_failed_grab(tools, pil, monkeypatch):
    _, ImageGrab = pil

    def locked(**kw):
        raise OSError("screen grab failed")

    monkeypatch.setattr(ImageGrab, "grab", locked)
    if WINDOWS:
        monkeypatch.setattr(computer, "_monitors", lambda: [(0, 0, 100, 100, True)])
    out = call(tools, "take_screenshot")
    assert out.is_error and "Could not take a screenshot: screen grab failed" in out.text
    assert not out.images


def test_screenshot_without_pillow_is_a_clear_error(tools, monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    out = call(tools, "take_screenshot")
    assert out.is_error and "needs Pillow" in out.text


def test_downscale_keeps_the_aspect_and_leaves_small_images(pil):
    Image, _ = pil
    assert computer.downscale(Image.new("RGB", (3000, 2000))).size == (1568, 1045)
    small = Image.new("RGB", (800, 600))
    assert computer.downscale(small) is small


def test_monitor_boxes_outside_the_grab_are_clipped(pil, monkeypatch):
    Image, ImageGrab = pil
    monkeypatch.setattr(computer, "_WINDOWS", True)
    monkeypatch.setattr(computer, "_dpi_aware_thread", contextlib.nullcontext)
    monkeypatch.setattr(computer, "_monitors", lambda: [(0, 0, 2000, 1000, True),
                                                       (5000, 0, 6000, 1000, False)])
    monkeypatch.setattr(computer, "_virtual_origin", lambda: (0, 0))
    monkeypatch.setattr(ImageGrab, "grab", lambda **kw: Image.new("RGB", (1500, 900)))
    shots = computer._grab_screens(ImageGrab)
    assert [(img.size, primary) for img, primary in shots] == [((1500, 900), True)]


@pytest.mark.skipif(not WINDOWS, reason="Windows monitor enumeration")
def test_the_monitor_list_is_read():
    with computer._dpi_aware_thread():
        monitors = computer._monitors()
        origin = computer._virtual_origin()
    assert monitors and sum(1 for m in monitors if m[4]) == 1
    assert origin == (min(m[0] for m in monitors), min(m[1] for m in monitors))


# ── the switch, the activity log, the description ────────────────────────────
def test_switched_off_refuses_every_tool_and_runs_nothing(activity, tmp_path):
    ct = computer.ComputerTools(name="mast-test", allowed=lambda: False,
                                activity=lambda *a: activity.append(a))
    try:
        target = tmp_path / "never.txt"
        args = {"write_file": {"path": str(target), "content": "x"},
                "run_command": {"command": "echo x"}, "read_file": {"path": str(target)},
                "edit_file": {"path": str(target), "old_string": "a", "new_string": "b"}}
        for name in sorted(TOOL_NAMES):
            out = call(ct, name, args.get(name))
            assert out.is_error and "switched off" in out.text, name
            assert '"Remote AI"' in out.text and "Also allow commands, files and screenshots" \
                in out.text
        assert not target.exists()
        assert activity == [("pytest", "refused", n) for n in sorted(TOOL_NAMES)]
        assert "No commands have been started yet." in ct._jobs.list()
    finally:
        ct.close()


def test_the_switch_is_read_on_every_call():
    state = {"on": True}

    def broken():
        raise RuntimeError("settings unreadable")

    ct = computer.ComputerTools(name="mast-test", allowed=lambda: state["on"])
    unreadable = computer.ComputerTools(name="mast-test", allowed=broken)
    try:
        assert not call(ct, "system_info").is_error
        state["on"] = False
        assert call(ct, "system_info").is_error
        state["on"] = True
        assert not call(ct, "system_info").is_error
        # A switch that cannot be read keeps the computer closed.
        assert "switched off" in call(unreadable, "system_info").text
    finally:
        ct.close()
        unreadable.close()


def test_activity_callback_errors_never_break_a_tool(tmp_path):
    def boom(who, kind, detail):
        raise RuntimeError("log window gone")

    ct = computer.ComputerTools(name="mast-test", activity=boom)
    try:
        p = tmp_path / "a.txt"
        assert not call(ct, "write_file", {"path": str(p), "content": "x\n"}).is_error
        assert "     1\tx" in call(ct, "read_file", {"path": str(p)}).text
        assert "Computer name: mast-test" in call(ct, "system_info").text
    finally:
        ct.close()


def test_activity_names_the_paths(tools, activity, tmp_path):
    p = tmp_path / "a.txt"
    ok(tools, "write_file", {"path": str(p), "content": "one\n"})
    ok(tools, "read_file", {"path": str(p)})
    ok(tools, "edit_file", {"path": str(p), "old_string": "one", "new_string": "two"})
    ok(tools, "list_directory", {"path": str(tmp_path)})
    ok(tools, "system_info")
    assert activity == [("pytest", "write", str(p)), ("pytest", "read", str(p)),
                        ("pytest", "edit", str(p)), ("pytest", "list", str(tmp_path)),
                        ("pytest", "info", "")]
    assert {kind for _, kind, _ in activity} <= computer.ACTIVITY_KINDS


def test_system_info_states_the_facts(tools):
    out = ok(tools, "system_info")
    assert out.startswith("Computer name: mast-test")
    for must in ("Operating system: ", "User: ", "Home folder: ",
                 f"Shell used by run_command: {computer_jobs.shell_name()}",
                 "Tailscale address: <rig-host>", "Local time: ", "(UTC",
                 "Served by: MAST 0.0-test"):
        assert must in out
    if WINDOWS:
        assert "Operating system: Windows" in out and "build " in out


def test_instructions_carry_the_rules(tools):
    text = tools.instructions()
    for must in ('MAST computer "mast-test"', "not on the machine you are running on",
                 "Call system_info first", "fresh shell", "no keyboard", "check_command",
                 "mast_* tools", "Never stop Nanonis, LabVIEW or MAST", "refuses",
                 "mast_ask_operator", "administrator rights", "reversible",
                 computer_jobs.shell_name()):
        assert must in text, must
    assert ("OneDrive" in text) == WINDOWS


def test_tool_list_is_well_formed(tools):
    listed = tools.list_tools()
    assert {t["name"] for t in listed} == TOOL_NAMES and len(listed) == len(TOOL_NAMES)
    for t in listed:
        schema = t["inputSchema"]
        assert schema["type"] == "object" and isinstance(schema["properties"], dict), t["name"]
        for req in schema.get("required", []):
            assert req in schema["properties"], t["name"]
        for prop, spec in schema["properties"].items():
            assert spec.get("description"), f"{t['name']}.{prop}"
        assert len(t["description"]) > 40 and "the remote computer" not in t["description"]
        if t["name"] != "check_command":
            assert 'the MAST computer "mast-test"' in t["description"], t["name"]
        assert set(t["annotations"]) == {"title", "readOnlyHint", "destructiveHint",
                                         "idempotentHint", "openWorldHint"}
        assert "structuredContent" not in str(t)
        meta = t.get("_meta") or {}
        if t["name"] == "take_screenshot":
            assert meta == {"anthropic/maxResultSizeChars": 200000}
        else:
            assert not meta, t["name"]
    run = next(t for t in listed if t["name"] == "run_command")
    assert "Nanonis" in run["description"] and computer_jobs.shell_name() in run["description"]


def test_importing_the_tools_loads_no_pillow_platform_or_uuid(tmp_path):
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); "
             "import mast_mcp.computer, mast_mcp.computer_jobs; "
             "print(sorted(m for m in ('PIL', 'platform', 'uuid') if m in sys.modules))")
    proc = subprocess.run([sys.executable, "-I", "-c", probe, str(_SERVER_DIR)],
                          capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"
