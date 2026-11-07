"""Update-check guards: one request at a time, a timestamp in the future is stale, and only
version numbers are compared.

* A double click on "Check for updates" (or the automatic check still running) must not start a
  second request.
* ``checked_at`` in the future (clock set back, corrupt cache) used to block every automatic and
  manual check until the clock caught up.
* A release tag that is not a version number (``nightly``) was shown as "New version available";
  it is now shown as it is, without a claim.

``LauncherApp`` needs Tk, so the methods are called on a small stand-in object that has the few
attributes they touch; threads are replaced by a recorder.
"""
from __future__ import annotations

import importlib.util
import sys
import time as _time
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_LAUNCHER = _REPO / "mast2_launcher.py"


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("mast2_launcher_github_guards", _LAUNCHER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Var:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value


class _Label:
    def configure(self, **kwargs):
        self.kwargs = kwargs


class _Win:
    def __init__(self):
        self.after_calls = []

    def after(self, ms, fn, *args):
        self.after_calls.append((ms, fn, args))


class _BrokenWin:
    def after(self, *args, **kwargs):
        raise RuntimeError("main thread is not in main loop")


class _App:
    """The attributes the update-check methods use; the real methods are bound by make_app()."""

    def __init__(self, root: Path, win=None):
        self.root_dir = root
        self.update_status_var = _Var()
        self.update_status_lbl = _Label()
        self.win = win or _Win()
        self.logs: list[str] = []

    def _log(self, message):
        self.logs.append(message)

    def _local_mast_version(self):
        return "7.1.4"


def make_app(launcher, root: Path, win=None) -> _App:
    app = _App(root, win)
    for name in ("_on_github_release_result", "_apply_github_release_cache_to_ui"):
        setattr(app, name, types.MethodType(getattr(launcher.LauncherApp, name), app))
    return app


@pytest.fixture
def threads(launcher, monkeypatch):
    """Replace threading.Thread inside the launcher with a recorder that never runs anything."""

    class FakeThread:
        started: list = []

        def __init__(self, target=None, daemon=None, **kwargs):
            self.target = target

        def start(self):
            type(self).started.append(self)

    monkeypatch.setattr(launcher, "threading", types.SimpleNamespace(Thread=FakeThread))
    return FakeThread


# ── one request at a time ───────────────────────────────────────────────────────────────
def test_a_second_click_while_a_check_runs_starts_no_second_request(launcher, tmp_path, threads):
    app = make_app(launcher, tmp_path)
    check = launcher.LauncherApp._check_github_release
    check(app, force=True)
    assert len(threads.started) == 1 and app._github_check_inflight is True

    check(app, force=True)   # double click
    check(app, force=False)  # the automatic check firing meanwhile
    assert len(threads.started) == 1
    assert app.logs == ["An update check is already running."]  # only the manual click is told


def test_the_result_releases_the_guard(launcher, tmp_path, threads, monkeypatch):
    app = make_app(launcher, tmp_path)
    check = launcher.LauncherApp._check_github_release
    check(app, force=True)
    assert app._github_check_inflight is True

    app._on_github_release_result({"status": "error", "error": "HTTP 403"}, True)
    assert app._github_check_inflight is False

    # a forced error also starts the 60 s manual cooldown: move the clock past it
    monkeypatch.setattr(launcher, "time", types.SimpleNamespace(time=lambda: _time.time() + 3600))
    check(app, force=True)
    assert len(threads.started) == 2


def test_the_guard_is_released_when_the_thread_cannot_start(launcher, tmp_path, threads):
    def boom(self):
        raise RuntimeError("can't start new thread")

    threads.start = boom
    app = make_app(launcher, tmp_path)
    launcher.LauncherApp._check_github_release(app, force=True)
    assert app._github_check_inflight is False
    assert any("Update check failed" in line for line in app.logs)


def test_the_guard_is_released_when_the_window_cannot_take_the_result(
    launcher, tmp_path, threads, monkeypatch
):
    monkeypatch.setattr(
        launcher, "_fetch_latest_github_release", lambda **kw: {"status": "error", "error": "x"})
    app = make_app(launcher, tmp_path, win=_BrokenWin())
    launcher.LauncherApp._check_github_release(app, force=True)
    assert app._github_check_inflight is True
    threads.started[0].target()  # the worker finishes, but Tk refuses the call
    assert app._github_check_inflight is False


def test_the_worker_hands_its_result_to_the_window_and_keeps_the_guard_until_then(
    launcher, tmp_path, threads, monkeypatch
):
    monkeypatch.setattr(
        launcher, "_fetch_latest_github_release", lambda **kw: {"status": "error", "error": "x"})
    app = make_app(launcher, tmp_path)
    launcher.LauncherApp._check_github_release(app, force=True)
    threads.started[0].target()
    assert len(app.win.after_calls) == 1 and app._github_check_inflight is True
    _ms, fn, args = app.win.after_calls[0]
    fn(*args)  # Tk delivers it on the main thread
    assert app._github_check_inflight is False


# ── a timestamp in the future is not "recent" ───────────────────────────────────────────
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("checked_at", [
    1_700_000_000.0 + 10,   # the clock was set back a little
    1e18,                   # far future / corrupt
    float("inf"), float("-inf"), float("nan"), "nan",
])
def test_a_future_or_non_finite_checked_at_is_stale(launcher, checked_at, force):
    now = 1_700_000_000.0
    assert launcher._should_check_github_release(
        {"checked_at": checked_at}, now=now, force=force) is True


def test_a_real_recent_check_still_blocks(launcher):
    now = 1_700_000_000.0
    for force in (False, True):
        assert launcher._should_check_github_release(
            {"checked_at": now - 10}, now=now, force=force) is False


# ── only version numbers are compared ───────────────────────────────────────────────────
@pytest.mark.parametrize("remote, local, newer", [
    ("v7.1.5", "7.1.4", True),
    ("7.2", "7.1.9", True),
    ("7.1.4", "7.1.4", False),
    ("7.1.3", "7.1.4", False),
    ("nightly", "7.1.4", False),   # not a version number
    ("latest", "latest", False),
    ("", "7.1.4", False),
    ("7.1.5", "?", False),         # this build's version is unknown
])
def test_only_version_numbers_are_compared(launcher, remote, local, newer):
    assert launcher._is_remote_version_newer(remote, local) is newer


def test_the_updates_row_shows_a_tag_it_cannot_compare_without_a_claim(launcher, tmp_path):
    app = make_app(launcher, tmp_path)
    paint = launcher.LauncherApp._apply_github_release_cache_to_ui

    launcher._save_github_release_cache(tmp_path, {"checked_at": 1.0, "version": "nightly"})
    paint(app)
    text = app.update_status_var.value
    assert "nightly" in text and "cannot be compared" in text
    assert "Up to date" not in text and "New version" not in text

    launcher._save_github_release_cache(tmp_path, {"checked_at": 1.0, "version": "7.1.5"})
    paint(app)
    assert "New version v7.1.5 available (you have v7.1.4)" in app.update_status_var.value

    launcher._save_github_release_cache(tmp_path, {"checked_at": 1.0, "version": "7.1.4"})
    paint(app)
    assert "Up to date (v7.1.4)" in app.update_status_var.value
