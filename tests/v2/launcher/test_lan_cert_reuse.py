"""The launcher's LAN certificate keeps its key across re-signs.

``mast2_launcher._ensure_lan_cert`` (called by ``run_service`` and
``run_push_server``) delegates to :func:`mast.net.tls.ensure_lan_cert`. It used to
generate a NEW RSA key every time the host set changed, which
changes the SPKI that satellites pin. The structural test
below keeps a private key-minting path from coming back to the launcher.
"""
from __future__ import annotations

import importlib.util
import logging
import re
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

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LAUNCHER_PATH = _REPO_ROOT / "mast2_launcher.py"


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("mast2_launcher", _LAUNCHER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resign_for_a_new_tailscale_ip_keeps_the_pin(launcher, tmp_path, caplog):
    from mast.net.tls import cert_info, spki_sha256

    cert = tmp_path / "api key" / "lan_cert.pem"
    key = tmp_path / "api key" / "lan_key.pem"
    log = logging.getLogger("test.launcher.cert")
    assert launcher._ensure_lan_cert(cert, key, log, extra_hosts=["<rig-host>"]) is True
    pin, key_bytes = spki_sha256(cert), key.read_bytes()

    with caplog.at_level(logging.INFO, logger="test.launcher.cert"):
        assert launcher._ensure_lan_cert(
            cert, key, log, extra_hosts=["<rig-host>", "rig-pc.tailbbbb.ts.net"]) is True
    assert spki_sha256(cert) == pin
    assert key.read_bytes() == key_bytes
    info = cert_info(cert)
    assert info.covers("<rig-host>") and info.covers("<rig-host>")
    assert any(pin in r.getMessage() for r in caplog.records)      # pin is logged at start


def test_launcher_serves_the_files_hub_reads_the_pin_from(launcher, tmp_path):
    from mast.net.tls import lan_cert_paths

    assert launcher._lan_cert_files(tmp_path) == lan_cert_paths(tmp_path)


def test_wildcard_push_server_host_is_not_a_san_entry(launcher, tmp_path):
    from mast.net.tls import cert_info

    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    assert launcher._ensure_lan_cert(cert, key, logging.getLogger("t"), extra_hosts=["0.0.0.0"])
    assert "0.0.0.0" not in cert_info(cert).ip_addresses


def test_launcher_has_no_private_key_minting_path():
    src = _LAUNCHER_PATH.read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"generate_private_key\s*\(", code), (
        "mast2_launcher must not mint LAN TLS keys itself — use mast.net.tls.ensure_lan_cert, "
        "which re-signs with the existing key (satellites pin the SPKI).")
    assert "_cert_covers" not in code and "_lan_cert_hosts" not in code
