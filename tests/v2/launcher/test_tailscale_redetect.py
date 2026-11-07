"""The launcher re-detects Tailscale and says when it changed.

A station PC may carry several Tailscale accounts and move between tailnets
(<rig-host> → <rig-host>). A launcher that detected Tailscale once at
start would keep showing the old「远程 - Tailscale」URL. ``_tailscale_change_note``
is the pure half of the fix (the other half is a 60 s Tk ``after`` loop).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# ── v2 import bootstrap (canonical block; keeps mast.* resolving to MASTv2/) ──
_MASTV2_ROOT = str(Path(__file__).resolve().parents[3] / "MASTv2")
if sys.path and sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _n in list(sys.modules):
    if _n == "mast" or _n.startswith("mast."):
        _f = getattr(sys.modules[_n], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_n]

import pytest  # noqa: E402

from mast.net.tailscale import TailscaleStatus  # noqa: E402

_LAUNCHER_PATH = Path(__file__).resolve().parents[3] / "mast2_launcher.py"


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("mast2_launcher", _LAUNCHER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ready(ip: str, tailnet: str) -> TailscaleStatus:
    return TailscaleStatus(installed=True, running=True, backend_state="Running",
                           self_ips=[ip, "fd7a:115c:a1e0::1"], tailnet=tailnet)


def test_account_switch_is_reported_with_both_addresses(launcher):
    note = launcher._tailscale_change_note(_ready("<rig-host>", "tailaaaa.ts.net"),
                                           _ready("<rig-host>", "user@example.com"))
    assert "<rig-host>" in note and "<rig-host>" in note
    assert "user@example.com" in note
    assert "fd7a" not in note                      # only dialable IPv4 is named


def test_late_connect_and_disconnect(launcher):
    stopped = TailscaleStatus(installed=True, backend_state="Stopped", hint="打开 Tailscale")
    assert "Tailscale connected" in launcher._tailscale_change_note(stopped, _ready("<rig-host>", "n"))
    assert "Tailscale disconnected" in launcher._tailscale_change_note(_ready("<rig-host>", "n"), stopped)


def test_nothing_to_say(launcher):
    a = _ready("<rig-host>", "n")
    assert launcher._tailscale_change_note(a, _ready("<rig-host>", "n")) == ""
    assert launcher._tailscale_change_note(None, a) == ""        # first detection
    assert launcher._tailscale_change_note(a, None) == ""        # detection failed


def test_redetect_is_scheduled_at_startup():
    src = _LAUNCHER_PATH.read_text(encoding="utf-8")
    assert "self._schedule_tailscale_redetect()" in src
    assert "TAILSCALE_REDETECT_MS" in src
