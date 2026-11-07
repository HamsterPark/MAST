"""Rewrite ai-remote.handoff.json host/prompt when Tailscale IPv4 changes."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_LAUNCHER = _REPO / "mast2_launcher.py"


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location(
        "mast2_launcher_handoff_rewrite", _LAUNCHER
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_handoff(path: Path, host: str) -> dict:
    payload = {
        "computer": "STM-PC",
        "user": "lab",
        "port": 22,
        "host": host,
        "key_path": r"C:\Users\lab\.ssh\ai-remote",
        "handover_note": "Carry this PRIVATE key file to the other computer: ai-remote",
        "prompt_line": (
            f"Control my computer STM-PC over SSH:  "
            f"ssh -i C:\\Users\\lab\\.ssh\\ai-remote -p 22 lab@{host}"
        ),
        "updated_at": "2026-01-01T00:00:00",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def test_rewrite_updates_host_and_prompt(launcher, tmp_path: Path, monkeypatch):
    handoff = tmp_path / "ai-remote.handoff.json"
    _write_handoff(handoff, "100.64.1.43")
    monkeypatch.setattr(launcher, "_remote_ai_handoff_path", lambda: handoff)

    assert launcher._rewrite_remote_ai_handoff_host("100.64.2.38") is True
    data = json.loads(handoff.read_text(encoding="utf-8"))
    assert data["host"] == "100.64.2.38"
    assert "lab@100.64.2.38" in data["prompt_line"]
    assert "100.64.1.43" not in data["prompt_line"]
    assert data["user"] == "lab"
    assert data["key_path"].endswith("ai-remote")
    assert data["handover_note"].startswith("Carry this PRIVATE key")
    assert data["updated_at"] != "2026-01-01T00:00:00"

    # Same address again → no write.
    before = handoff.read_text(encoding="utf-8")
    assert launcher._rewrite_remote_ai_handoff_host("100.64.2.38") is False
    assert handoff.read_text(encoding="utf-8") == before


def test_rewrite_skips_missing_or_empty(launcher, tmp_path: Path, monkeypatch):
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(launcher, "_remote_ai_handoff_path", lambda: missing)
    assert launcher._rewrite_remote_ai_handoff_host("100.64.2.38") is False
    assert launcher._rewrite_remote_ai_handoff_host("") is False


# ── the file setup-ai-ssh.ps1 really leaves behind ─────────────────────────────────
# The launcher starts powershell.exe (Windows PowerShell 5.1). There
#   ($handoff | ConvertTo-Json) | Set-Content -LiteralPath $path -Encoding UTF8
# writes a UTF-8 BOM (EF BB BF), CRLF newlines, four-space indents and two spaces after
# every colon. json.loads() rejects text that starts with the BOM, so a reader that opens
# the file as plain "utf-8" saw every real handoff file as corrupt: the paste lines never
# showed and the Tailscale-IP rewrite silently did nothing. The fixtures above are written
# without a BOM, which is why they never noticed.
_BOM = b"\xef\xbb\xbf"


def _write_powershell51_handoff(path: Path, host: str) -> None:
    body = (
        "{\r\n"
        '    "computer":  "STM-PC",\r\n'
        '    "user":  "lab",\r\n'
        '    "port":  22,\r\n'
        f'    "host":  "{host}",\r\n'
        '    "key_path":  "C:\\\\Users\\\\lab\\\\.ssh\\\\ai-remote",\r\n'
        '    "pub_line":  "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKey ai-remote@STM-PC",\r\n'
        f'    "prompt_line":  "Control my computer STM-PC over SSH:  '
        f'ssh -i C:\\\\Users\\\\lab\\\\.ssh\\\\ai-remote -p 22 lab@{host}",\r\n'
        '    "handover_note":  "Carry this PRIVATE key file to the other computer: '
        'C:\\\\Users\\\\lab\\\\.ssh\\\\ai-remote",\r\n'
        '    "updated_at":  "2026-11-03T00:36:20.2342664+08:00"\r\n'
        "}\r\n"
    )
    path.write_bytes(_BOM + body.encode("utf-8"))


def test_load_reads_a_powershell_51_file_with_a_bom(launcher, tmp_path: Path, monkeypatch):
    handoff = tmp_path / "ai-remote.handoff.json"
    _write_powershell51_handoff(handoff, "100.64.1.43")
    assert handoff.read_bytes().startswith(_BOM)  # the fixture really has the BOM
    monkeypatch.setattr(launcher, "_remote_ai_handoff_path", lambda: handoff)

    data = launcher._load_remote_ai_handoff()
    assert data["host"] == "100.64.1.43"
    assert data["prompt_line"].startswith("Control my computer STM-PC over SSH:")


def test_rewrite_reads_a_powershell_51_file_and_writes_plain_utf8(
    launcher, tmp_path: Path, monkeypatch
):
    handoff = tmp_path / "ai-remote.handoff.json"
    _write_powershell51_handoff(handoff, "100.64.1.43")
    monkeypatch.setattr(launcher, "_remote_ai_handoff_path", lambda: handoff)

    assert launcher._rewrite_remote_ai_handoff_host("100.64.2.38") is True

    raw = handoff.read_bytes()
    assert not raw.startswith(_BOM)  # the rewrite writes plain UTF-8, no BOM
    data = json.loads(raw.decode("utf-8"))  # so strict utf-8 reads it back
    assert data["host"] == "100.64.2.38"
    assert data["prompt_line"].endswith("lab@100.64.2.38")
    assert "100.64.1.43" not in data["prompt_line"]
    # fields the PowerShell -Uninstall path and the launcher still need are kept
    assert data["pub_line"].startswith("ssh-ed25519 ")
    assert data["key_path"] == r"C:\Users\lab\.ssh\ai-remote"
    assert data["handover_note"].startswith("Carry this PRIVATE key")
    # and the launcher can read what it just wrote
    assert launcher._load_remote_ai_handoff()["host"] == "100.64.2.38"


def test_rewrite_leaves_a_powershell_51_file_alone_when_the_host_matches(
    launcher, tmp_path: Path, monkeypatch
):
    handoff = tmp_path / "ai-remote.handoff.json"
    _write_powershell51_handoff(handoff, "100.64.2.38")
    before = handoff.read_bytes()
    monkeypatch.setattr(launcher, "_remote_ai_handoff_path", lambda: handoff)

    assert launcher._rewrite_remote_ai_handoff_host("100.64.2.38") is False
    assert handoff.read_bytes() == before  # no write at all: BOM and CRLF untouched
