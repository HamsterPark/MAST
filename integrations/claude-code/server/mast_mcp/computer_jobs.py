"""Commands for the computer tools: shells, background jobs and capped output.

A port of Remote Hands' ``jobs.go`` and the command half of its platform files.
Every run_command starts one job: a fresh shell running the command, a thread
that reads its output (stdout and stderr together) into a ``CappedBuffer``, and
a thread that waits for it to end. A command still running when run_command
stops waiting keeps going in the background; check_command waits for it again,
takes the output produced since the last look, or kills it.

Windows: PowerShell 7 from Program Files if installed, else Windows PowerShell
5.1 (``MAST_REMOTE_POWERSHELL`` names another one). The command is saved as a
temporary ``.ps1`` with a UTF-8 byte order mark, the only Unicode form 5.1
reads, between Remote Hands' prelude and epilogue, which avoids every quoting
problem of the Windows command line. It runs in a hidden console of its own
(``CREATE_NO_WINDOW``), so it can neither draw on MAST's window nor wait for keys
typed there. Stop and timeout use ``taskkill /T /F``, which also ends the
processes the command started.

POSIX (best effort; MAST runs on Windows): the user's shell if it is bash, zsh,
dash, ksh or sh, else /bin/bash or /bin/sh, with ``-c`` (a script file for
commands over 64 KB), in a new session without a controlling terminal, so sudo,
ssh and git fail at once instead of waiting for a password; stop kills the
whole process group.

Everywhere: stdin is empty, ``GIT_TERMINAL_PROMPT=0`` and ``NO_COLOR=1`` are set,
and the variables PyInstaller sets for MAST.exe (``_MEI*``, ``_PYI*``) are left
out: a child started with them can die with ERROR_BAD_EXE_FORMAT (see the
offline update in ``mast2_launcher.py``). At most ``MAX_RUNNING`` commands run
at once and ``MAX_JOBS`` are remembered.

A kill only ever reaches a process that has not been reaped, so a process id
that the system has already given to another program is never hit: on Windows
the ``Popen`` handle keeps the id taken until the job is forgotten; on POSIX the
waiter learns of the exit without reaping (``waitid`` with ``WNOWAIT``) and
reaps only after taking the job's lock, which every kill holds.
"""
from __future__ import annotations

import logging
import os
import re
import signal as _signal
import subprocess
import tempfile
import threading
import time
from functools import lru_cache

log = logging.getLogger("mast_mcp.computer")

OUTPUT_HEAD = 8 << 10       # bytes kept from the start of unread output
OUTPUT_TAIL = 24 << 10      # bytes kept from the end of unread output
MAX_RUNNING = 16
MAX_JOBS = 64               # remembered, running or finished
EOF_GRACE_S = 3.0           # how long the output may stay open after the command ended
STOP_WAIT_S = 10.0          # check_command stop=true: how long to wait for the end
CLOSE_WAIT_S = 3.0
KILL_TIMEOUT_S = 15.0       # taskkill itself
POLL_S = 0.25               # how often a waiting call looks at its stop signal
LONG_COMMAND = 64 << 10     # POSIX: one argument may not be longer than 128 KiB on Linux
READ_CHUNK = 64 << 10
CREATE_NO_WINDOW = 0x08000000

_WINDOWS = os.name == "nt"

# The command runs in a script block whose output goes through Out-Default
# before the script exits (a bare exit drops tables PowerShell is still
# formatting). Afterwards PowerShell's success flag becomes the exit code: the
# program's own code for native programs, 1 for failed cmdlets. Windows
# PowerShell 5.1 also counts stderr output of a program as failure
# (NativeCommandError); that is ignored when the program itself returned 0.
# Both texts are Remote Hands' own, byte for byte.
PS_PRELUDE = (
    "$ProgressPreference = 'SilentlyContinue'\r\n"
    "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }\r\n"
    "try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}\r\n"
    "$OutputEncoding = [System.Text.UTF8Encoding]::new($false)\r\n"
    "try { $Host.UI.RawUI.BufferSize = New-Object System.Management.Automation.Host.Size(300, 9999) } catch {}\r\n"
    "$global:__rhOk = $true; $global:__rhCode = $null; $global:__rhNative = $false\r\n"
    "& {\r\n")
PS_EPILOGUE = (
    "\r\n$global:__rhOk = $?; $global:__rhCode = $LASTEXITCODE; "
    "$global:__rhNative = ($Error.Count -gt 0 -and $Error[0].FullyQualifiedErrorId -like 'NativeCommandError*')\r\n"
    "} | Out-Default\r\n"
    "if ($global:__rhOk -or ($global:__rhNative -and -not $global:__rhCode)) { exit 0 } "
    "elseif ($global:__rhCode) { exit $global:__rhCode } else { exit 1 }\r\n")


class JobError(Exception):
    """A command could not be started; the message says why."""


# ── shells ───────────────────────────────────────────────────────────────────
def system_root() -> str:
    return os.environ.get("SystemRoot") or r"C:\Windows"


def powershell() -> str:
    return os.environ.get("MAST_REMOTE_POWERSHELL") or _default_powershell()


@lru_cache(maxsize=1)
def _default_powershell() -> str:
    # PowerShell 7 only when installed normally: the Microsoft Store build
    # virtualises parts of the file system.
    for var in ("ProgramW6432", "ProgramFiles"):
        base = os.environ.get(var)
        if base:
            path = os.path.join(base, "PowerShell", "7", "pwsh.exe")
            if os.path.exists(path):
                return path
    return os.path.join(system_root(), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


@lru_cache(maxsize=1)
def _unix_shell() -> str:
    shell = os.environ.get("SHELL", "")
    if os.path.basename(shell) in ("bash", "zsh", "sh", "dash", "ksh") and _is_executable(shell):
        return shell
    for shell in ("/bin/bash", "/usr/bin/bash", "/bin/zsh", "/bin/sh"):
        if _is_executable(shell):
            return shell
    return "/bin/sh"


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def shell_name() -> str:
    if _WINDOWS:
        if os.path.basename(powershell()).lower() == "pwsh.exe":
            return "PowerShell 7 (pwsh.exe)"
        return "Windows PowerShell 5.1 (powershell.exe)"
    shell = _unix_shell()
    return f"{os.path.basename(shell)} ({shell} -c)"


def shell_command(command: str) -> tuple[list[str], str | None]:
    """The program that runs ``command``, and the script file to delete when it ends."""
    if _WINDOWS:
        script = _write_script(".ps1", "\ufeff" + PS_PRELUDE + command + PS_EPILOGUE)
        return [powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                "Bypass", "-File", script], script
    if len(command.encode("utf-8", "replace")) < LONG_COMMAND:
        return [_unix_shell(), "-c", command], None
    script = _write_script(".sh", command)
    return [_unix_shell(), script], script


def _write_script(suffix: str, text: str) -> str:
    fd, path = tempfile.mkstemp(prefix="mast-remote-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8", "replace"))
    except BaseException:
        _remove(path)
        raise
    return path


def _remove(path: str | None) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def clean_environ() -> dict:
    """MAST's environment without PyInstaller's own variables."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(("_MEI", "_PYI"))}


def child_env(cwd: str) -> dict:
    env = clean_environ()
    # No password prompts from git, and no colour codes (they only cost the AI attention).
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["NO_COLOR"] = "1"
    if not _WINDOWS:
        env["PWD"] = cwd
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a command together with every process it started."""
    if _WINDOWS:
        taskkill = os.path.join(system_root(), "System32", "taskkill.exe")
        try:
            subprocess.run([taskkill, "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW, env=clean_environ(),
                           timeout=KILL_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("taskkill of command process %s failed: %s", proc.pid, exc)
    else:
        try:
            os.killpg(proc.pid, _signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _wait_for_exit(proc: subprocess.Popen) -> None:
    """Block until ``proc`` has ended, without reaping it where the system allows."""
    if not _WINDOWS and hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
        try:
            os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
            return
        except ChildProcessError:
            return                      # reaped already (by a kill's poll)
        except OSError:
            pass
    proc.wait()


# ── output ───────────────────────────────────────────────────────────────────
# Terminal colour and cursor codes, which some programs print even when their
# output is not a terminal.
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])")


class CappedBuffer:
    """Command output between two reads.

    Keeps the first ``OUTPUT_HEAD`` and the last ``OUTPUT_TAIL`` bytes and counts
    what it had to drop in between, so a chatty command can neither eat memory
    nor flood the AI.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._head = bytearray()
        self._tail = bytearray()
        self._dropped = 0
        self._closed = False

    def write(self, data: bytes) -> None:
        with self._lock:
            if self._closed:
                return
            room = OUTPUT_HEAD - len(self._head)
            if room > 0:
                self._head += data[:room]
                data = data[room:]
            if not data:
                return
            if len(data) >= OUTPUT_TAIL:
                self._dropped += len(self._tail) + len(data) - OUTPUT_TAIL
                self._tail = bytearray(data[-OUTPUT_TAIL:])
                return
            self._tail += data
            over = len(self._tail) - OUTPUT_TAIL
            if over > 0:
                self._dropped += over
                del self._tail[:over]

    def close(self) -> None:
        """Ignore whatever is written from now on."""
        with self._lock:
            self._closed = True

    def take(self) -> str:
        """The unread output as text; the buffer starts over."""
        with self._lock:
            raw = bytes(self._head)
            if self._dropped:
                raw += f"\n[... {self._dropped} bytes of output skipped ...]\n".encode("ascii")
            raw += bytes(self._tail)
            self._head, self._tail, self._dropped = bytearray(), bytearray(), 0
        text = _ANSI_ESCAPE.sub("", raw.decode("utf-8", "replace")).replace("\r\n", "\n")
        return text[1:] if text.startswith("\ufeff") else text


# ── jobs ─────────────────────────────────────────────────────────────────────
def should_stop(signal) -> bool:
    """The tool call that waits should give up: cancelled, or the server is stopping."""
    return signal is not None and bool(signal.should_stop())


def one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def fmt_duration(seconds: float) -> str:
    """For people and AIs alike: "0.4 s", "12 s", "3 min 5 s", "2 h 7 min"."""
    if seconds < 10:
        return f"{seconds:.1f} s"
    if seconds < 60:
        return f"{int(seconds)} s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min {int(seconds) % 60} s"
    return f"{int(seconds // 3600)} h {int(seconds // 60) % 60} min"


class Job:
    """One command started by run_command."""

    def __init__(self, job_id: int, command: str, proc: subprocess.Popen, timeout_s: float,
                 script: str | None):
        self.id = job_id
        self.command = command
        self.proc = proc
        self.out = CappedBuffer()
        self.started = time.monotonic()
        self.done = threading.Event()
        # Written before done is set.
        self.exit_code = -1
        self.run_error: str | None = None
        self.ended = self.started
        self.lingered = False       # processes it started kept the output open after it ended
        self.timed_out = False
        self.stopped = False
        self._lock = threading.Lock()
        self._reaped = False
        self._eof = threading.Event()
        self._script = script
        self._timer = threading.Timer(timeout_s, self.kill)
        self._timer.daemon = True

    def _begin(self) -> None:
        threading.Thread(target=self._read_output, name=f"mast-remote-job{self.id}-out",
                         daemon=True).start()
        threading.Thread(target=self._supervise, name=f"mast-remote-job{self.id}-wait",
                         daemon=True).start()
        self._timer.start()

    def _read_output(self) -> None:
        pipe = self.proc.stdout
        try:
            while True:
                chunk = pipe.read(READ_CHUNK)
                if not chunk:
                    break
                self.out.write(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self._eof.set()
            try:
                pipe.close()
            except OSError:
                pass

    def _supervise(self) -> None:
        try:
            _wait_for_exit(self.proc)
        except Exception as exc:  # noqa: BLE001 - reported with the job, never lost
            log.exception("waiting for command %s failed", self.id)
            self.run_error = repr(exc)
        with self._lock:
            self._reaped = True     # from here on kill() leaves the process id alone
        self._timer.cancel()
        try:
            code = self.proc.wait()
        except Exception as exc:  # noqa: BLE001
            self.run_error = self.run_error or repr(exc)
            code = -1
        # A process the command left behind may still hold the output open.
        # Stop waiting for it; the reader keeps draining the pipe (so that
        # process is not blocked on a full pipe) but its output is not kept.
        if not self._eof.wait(EOF_GRACE_S):
            self.lingered = True
            self.out.close()
        _remove(self._script)
        self.exit_code = code if code >= 0 else -1      # POSIX: killed by a signal
        self.ended = time.monotonic()
        self.done.set()

    def finished(self) -> bool:
        return self.done.is_set()

    def kill(self, stopped: bool = False) -> None:
        """Kill the command and what it started (a timeout unless ``stopped``).

        A no-op once the command has ended: its process id may belong to another program.
        """
        with self._lock:
            if self._reaped:
                return
            if stopped:
                self.stopped = True
            elif not self.stopped:
                self.timed_out = True
            _kill_tree(self.proc)

    def wait(self, signal, seconds: float) -> bool:
        """Wait up to ``seconds`` for the command to end; whether it has."""
        end = time.monotonic() + max(0.0, seconds)
        while not self.done.is_set():
            left = end - time.monotonic()
            if left <= 0 or should_stop(signal):
                break
            self.done.wait(min(left, POLL_S))
        return self.done.is_set()

    def report(self, finished: bool) -> str:
        """The job's state and the output produced since the last report."""
        parts = []
        if finished:
            with self._lock:
                timed_out, stopped = self.timed_out, self.stopped
            took = fmt_duration(self.ended - self.started)
            if self.run_error:
                parts.append(f"The command failed to run: {self.run_error}\n")
            elif stopped:
                parts.append(f"Stopped on request after {took} (job_id {self.id}).\n")
            elif timed_out:
                parts.append(f"Killed because it reached its timeout after {took} "
                             f"(job_id {self.id}).\n")
            else:
                parts.append(f"Exit code: {self.exit_code} (took {took})\n")
            if self.lingered:
                parts.append("Note: processes started by the command were still holding its "
                             "output open; output they write later is not captured.\n")
        else:
            parts.append(f"Still running after {fmt_duration(time.monotonic() - self.started)} "
                         f"(job_id {self.id}). It keeps running in the background: call "
                         f"check_command with job_id {self.id} to wait for more output and the "
                         "exit code, or with stop=true to kill it.\n")
        out = self.out.take()
        if out:
            parts.append("Output:\n" + out + ("" if out.endswith("\n") else "\n"))
        elif finished:
            parts.append("(no output)\n")
        else:
            parts.append("(no new output yet)\n")
        return "".join(parts)


def _launch(job_id: int, command: str, cwd: str, timeout_s: float) -> Job:
    argv, script = shell_command(command)
    kwargs = {"cwd": cwd, "env": child_env(cwd), "stdin": subprocess.DEVNULL,
              "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "bufsize": 0}
    if _WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except BaseException:
        _remove(script)
        raise
    job = Job(job_id, command, proc, timeout_s, script)
    job._begin()
    return job


class JobManager:
    """The commands of one ``ComputerTools``: start, look up, stop, list, kill all."""

    def __init__(self):
        self._lock = threading.Lock()
        self._next = 0
        self._starting = 0          # slots reserved by start() calls in progress
        self._closed = False        # shutting down: no new commands
        self._jobs: dict[int, Job] = {}

    def start(self, command: str, cwd: str, timeout_s: float) -> Job:
        # Reserve a slot first, so that parallel calls cannot exceed MAX_RUNNING.
        with self._lock:
            if self._closed:
                raise JobError("MAST is shutting down")
            running = self._starting + sum(1 for j in self._jobs.values() if not j.finished())
            if running >= MAX_RUNNING:
                raise JobError(f"{running} commands are still running; wait for them or stop "
                               "some with check_command")
            self._starting += 1
            self._next += 1
            job_id = self._next
        job = None
        try:
            job = _launch(job_id, command, cwd, timeout_s)
        finally:
            with self._lock:
                self._starting -= 1
                if job is not None:
                    self._jobs[job_id] = job
                    self._prune_locked()
                closed = self._closed
        if closed:                  # shutdown began while the command was starting
            job.kill(stopped=True)
        return job

    def _prune_locked(self) -> None:
        """Forget the oldest finished jobs when there are too many."""
        for job_id in sorted(i for i, j in self._jobs.items() if j.finished()):
            if len(self._jobs) <= MAX_JOBS:
                break
            del self._jobs[job_id]

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def stop(self, job: Job, signal) -> bool:
        if job.finished():
            return True
        job.kill(stopped=True)
        return job.wait(signal, STOP_WAIT_S)

    def kill_all(self, *, close: bool) -> int:
        """Kill every running command (in parallel) and wait for them, at most ``CLOSE_WAIT_S``.

        ``close`` also refuses new commands from now on. Returns how many were running.
        """
        with self._lock:
            if close:
                self._closed = True
            running = [j for j in self._jobs.values() if not j.finished()]
        for job in running:
            threading.Thread(target=job.kill, kwargs={"stopped": True},
                             name=f"mast-remote-job{job.id}-kill", daemon=True).start()
        deadline = time.monotonic() + CLOSE_WAIT_S
        for job in running:
            left = deadline - time.monotonic()
            if left <= 0 or not job.done.wait(left):
                break
        return len(running)

    def list(self) -> str:
        """Every remembered command, newest first."""
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.id, reverse=True)
        if not jobs:
            return "No commands have been started yet.\n"
        lines = []
        for job in jobs:
            if job.finished():
                state = f"finished, exit code {job.exit_code}"
            else:
                state = "running for " + fmt_duration(time.monotonic() - job.started)
            lines.append(f"job_id {job.id}: {state}: {one_line(job.command, 100)}\n")
        return "".join(lines)
