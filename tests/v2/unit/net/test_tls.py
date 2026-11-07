"""The LAN TLS certificate keeps its key — mast.net.tls.

Contract trap 10 (docs/v2/design/ters_satellite_integration.md): satellites pin
the SHA-256 of the certificate's SubjectPublicKeyInfo, so re-signing the LAN
certificate (a new IP, a new tailnet, the fibre NIC) must never change the key.
Older launchers minted a new key on every re-sign.

Everything runs in tmp_path with injected addresses; nothing touches the real
``<data>/api key``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import logging
import socket
import ssl
import sys
import threading
from pathlib import Path


def _find_mastv2_root() -> str:
    p = Path(__file__).resolve()
    while p.parent != p:
        if (p / "MASTv2").is_dir():
            return str(p / "MASTv2")
        p = p.parent
    raise RuntimeError("MASTv2 dir not found")


_MASTV2_ROOT = _find_mastv2_root()
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import pytest  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from mast.net import tls  # noqa: E402

LAN = "10.0.0.11"
OLD_TS = "100.64.1.43"      # the STM PC's Tailscale IP before the tailnet change (tailnet tailaaaa)
NEW_TS = "100.64.2.38"        # the STM PC's Tailscale IP after the tailnet change (tailnet tailbbbb)


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    d = tmp_path / "api key"
    return d / "lan_cert.pem", d / "lan_key.pem"


def _ensure(cert, key, addresses, extra=(), hostname="RIG-PC", now=None):
    return tls.ensure_lan_cert(cert, key, extra_hosts=extra, addresses=list(addresses),
                               hostname=hostname, now=now)


def _san(cert_path: Path) -> tuple[set[str], set[str]]:
    c = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return ({str(n) for n in san.get_values_for_type(x509.DNSName)},
            {ipaddress.ip_address(i).compressed for i in san.get_values_for_type(x509.IPAddress)})


def _spki_direct(cert_path: Path) -> str:
    c = x509.load_pem_x509_certificate(cert_path.read_bytes())
    der = c.public_key().public_bytes(serialization.Encoding.DER,
                                      serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def _legacy_cert(cert: Path, key: Path, names, ips, *, cn="MAST LAN", days=3650, k=None):
    """A pair exactly like the older launcher wrote it (SAN only)."""
    k = k or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    san = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    now = dt.datetime.now(dt.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
         .public_key(k.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=days))
         .add_extension(x509.SubjectAlternativeName(san), critical=False)
         .sign(k, hashes.SHA256()))
    cert.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.TraditionalOpenSSL,
                                    serialization.NoEncryption()))
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    return k


# ── SPKI ─────────────────────────────────────────────────────────────────────

def test_spki_is_sha256_of_der_subject_public_key_info(tmp_path):
    cert, key = _paths(tmp_path)
    res = _ensure(cert, key, [LAN])
    assert res.ok and res.action == "created"
    assert tls.spki_sha256(cert) == _spki_direct(cert) == res.spki_sha256
    assert len(res.spki_sha256) == 64 and res.spki_sha256 == res.spki_sha256.lower()
    der = x509.load_pem_x509_certificate(cert.read_bytes()).public_bytes(serialization.Encoding.DER)
    assert tls.spki_sha256_of(der) == res.spki_sha256          # DER input too
    # one entry point for every form (hub's show-pin passes whichever it has)
    assert tls.spki_sha256(der) == tls.spki_sha256(cert.read_bytes()) == tls.spki_sha256(str(cert))


def test_lan_cert_paths_is_the_served_location(tmp_path):
    cert, key = tls.lan_cert_paths(tmp_path)
    assert cert == tmp_path / "api key" / "lan_cert.pem"
    assert key == tmp_path / "api key" / "lan_key.pem"


def test_peer_spki_matches_what_the_server_serves(tmp_path):
    cert, key = _paths(tmp_path)
    _ensure(cert, key, [LAN])
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(cert, key)           # also proves the pair loads (uvicorn path)
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(1)
    port = ls.getsockname()[1]

    def serve():
        try:
            c, _ = ls.accept()
            with sctx.wrap_socket(c, server_side=True) as s:
                s.recv(1)
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    try:
        assert tls.peer_spki_sha256("127.0.0.1", port) == tls.spki_sha256(cert)
    finally:
        ls.close()


# ── the key survives re-signing ──────────────────────────────────────────────

def test_second_call_with_same_hosts_touches_nothing(tmp_path):
    cert, key = _paths(tmp_path)
    _ensure(cert, key, [LAN, NEW_TS])
    before = (cert.read_bytes(), key.read_bytes())
    res = _ensure(cert, key, [LAN, NEW_TS])
    assert res.action == "kept" and res.key_reused
    assert (cert.read_bytes(), key.read_bytes()) == before


def test_new_ip_resigns_with_the_same_key_and_keeps_the_old_entries(tmp_path):
    cert, key = _paths(tmp_path)
    first = _ensure(cert, key, [LAN])
    key_bytes = key.read_bytes()
    res = _ensure(cert, key, [LAN, NEW_TS])
    assert res.action == "resigned" and res.key_reused
    assert key.read_bytes() == key_bytes                     # key file untouched
    assert res.spki_sha256 == first.spki_sha256 == tls.spki_sha256(cert)
    assert NEW_TS in res.added
    _, ips = _san(cert)
    assert {LAN, NEW_TS} <= ips


def test_san_only_grows_so_moving_back_does_not_resign(tmp_path):
    cert, key = _paths(tmp_path)
    _ensure(cert, key, ["192.168.1.20"])                     # home Wi-Fi
    _ensure(cert, key, ["10.0.1.79"])                     # campus Wi-Fi → re-sign once
    snapshot = cert.read_bytes()
    res = _ensure(cert, key, ["192.168.1.20"])               # back home
    assert res.action == "kept"
    assert cert.read_bytes() == snapshot
    _, ips = _san(cert)
    assert {"192.168.1.20", "10.0.1.79"} <= ips


def test_tailnet_move_keeps_the_pin(tmp_path):
    """an STM PC whose cert lists 100.64.1.43 (tailnet tailaaaa) and which then
    joins another tailnet as 100.64.2.38. Its next start must
    re-sign WITHOUT a new key."""
    cert, key = _paths(tmp_path)
    _legacy_cert(cert, key,
                 ["RIG-PC", "rig-pc.tailaaaa.ts.net", "localhost"],
                 [LAN, OLD_TS, "127.0.0.1", "192.168.60.1", "::1",
                  "fd7a:115c:a1e0::a1", "fe80::1:2"])
    pin = _spki_direct(cert)
    res = _ensure(cert, key, [LAN, "192.168.60.1", NEW_TS, "fd7a:115c:a1e0::b2"],
                  extra=["rig-pc.tailbbbb.ts.net", NEW_TS, "fd7a:115c:a1e0::b2"])
    assert res.action == "resigned" and res.key_reused
    assert res.spki_sha256 == pin == tls.spki_sha256(cert)
    names, ips = _san(cert)
    assert {OLD_TS, NEW_TS, LAN} <= ips                      # old one kept, new one added
    assert "rig-pc.tailbbbb.ts.net" in names
    assert "rig-pc.tailaaaa.ts.net" in names


def test_legacy_uppercase_hostname_counts_as_covered(tmp_path):
    cert, key = _paths(tmp_path)
    _legacy_cert(cert, key, ["RIG-PC", "localhost"], [LAN, "127.0.0.1", "::1"])
    snapshot = cert.read_bytes()
    assert _ensure(cert, key, [LAN]).action == "kept"        # DNS names are case-insensitive
    assert cert.read_bytes() == snapshot


# ── which addresses are required ─────────────────────────────────────────────

def test_rotating_and_link_local_addresses_are_not_required(tmp_path):
    cert, key = _paths(tmp_path)
    _ensure(cert, key, [LAN, "2001:db8::1111", "fe80::1", "169.254.7.7"])
    _, ips = _san(cert)
    assert LAN in ips
    assert not ({"2001:db8::1111", "fe80::1", "169.254.7.7"} & ips)
    # tomorrow's IPv6 privacy address must not trigger a re-sign
    assert _ensure(cert, key, [LAN, "2001:db8::2222"]).action == "kept"


def test_tailscale_ula_and_loopback_are_required():
    names, ips = tls.required_hosts(addresses=["fd7a:115c:a1e0::c3", LAN],
                                    hostname="Lab_PC")
    assert {"127.0.0.1", "::1", "fd7a:115c:a1e0::c3", LAN} <= ips
    assert {"localhost", "lab_pc"} <= names


def test_extra_hosts_always_required_and_wildcards_skipped():
    names, ips = tls.required_hosts(["0.0.0.0", "::", "", "[fd7a:115c:a1e0::9]",
                                     "Box.tail1a2b.ts.net.", "10.0.0.7"],
                                    addresses=[], hostname="box")
    assert "fd7a:115c:a1e0::9" in ips and "10.0.0.7" in ips
    assert "box.tail1a2b.ts.net" in names
    assert "0.0.0.0" not in ips and "::" not in ips


# ── broken or foreign files ──────────────────────────────────────────────────

def test_cert_deleted_key_kept_restores_the_same_pin(tmp_path):
    cert, key = _paths(tmp_path)
    pin = _ensure(cert, key, [LAN]).spki_sha256
    cert.unlink()
    res = _ensure(cert, key, [LAN])
    assert res.action == "resigned" and res.key_reused and res.spki_sha256 == pin


def test_unreadable_key_is_moved_aside_and_the_new_pin_is_announced(tmp_path, caplog):
    cert, key = _paths(tmp_path)
    old_pin = _ensure(cert, key, [LAN]).spki_sha256
    key.write_bytes(b"not a key")
    with caplog.at_level(logging.WARNING):
        res = _ensure(cert, key, [LAN])
    assert res.ok and res.action == "created" and not res.key_reused
    assert res.spki_sha256 != old_pin
    aside = list(key.parent.glob("lan_key.pem.unreadable-*"))
    assert len(aside) == 1 and aside[0].read_bytes() == b"not a key"
    assert any("re-pin" in r.getMessage() for r in caplog.records)


def test_mismatched_pair_is_resigned_with_the_key_file(tmp_path):
    cert, key = _paths(tmp_path)
    k = _legacy_cert(cert, key, ["localhost"], [LAN, "127.0.0.1", "::1"])
    other_cert = tmp_path / "other.pem"
    _legacy_cert(other_cert, tmp_path / "other_key.pem", ["localhost"], [LAN, "127.0.0.1", "::1"])
    cert.write_bytes(other_cert.read_bytes())                 # cert no longer matches key
    res = _ensure(cert, key, [LAN], hostname="")
    assert res.action == "resigned" and res.key_reused
    want = hashlib.sha256(k.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
    assert tls.spki_sha256(cert) == want


def test_expiring_cert_is_renewed_with_the_same_key(tmp_path):
    cert, key = _paths(tmp_path)
    _legacy_cert(cert, key, ["box", "localhost"], [LAN, "127.0.0.1", "::1"], days=10)
    pin = _spki_direct(cert)
    res = _ensure(cert, key, [LAN], hostname="box")
    assert res.action == "resigned" and res.spki_sha256 == pin
    na = x509.load_pem_x509_certificate(cert.read_bytes()).not_valid_after_utc
    assert na - dt.datetime.now(dt.timezone.utc) > dt.timedelta(days=3000)


def test_operator_supplied_certificate_is_never_overwritten(tmp_path, caplog):
    cert, key = _paths(tmp_path)
    _legacy_cert(cert, key, ["box.tail1a2b.ts.net"], [], cn="box.tail1a2b.ts.net")
    snapshot = (cert.read_bytes(), key.read_bytes())
    with caplog.at_level(logging.WARNING):
        res = _ensure(cert, key, [LAN, NEW_TS])
    assert res.ok and res.action == "kept" and res.reason == "foreign certificate"
    assert (cert.read_bytes(), key.read_bytes()) == snapshot
    assert any("not a MAST-issued" in r.getMessage() for r in caplog.records)


def test_san_history_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(tls, "MAX_SAN_ENTRIES", 8)
    cert, key = _paths(tmp_path)
    for i in range(1, 6):
        _ensure(cert, key, [f"192.168.{i}.10"], hostname="box")
    names, ips = _san(cert)
    assert len(names) + len(ips) <= 8
    assert "192.168.5.10" in ips                              # what is required now survives


def test_no_cryptography_reports_failure_without_raising(tmp_path, monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("cryptography"):
            raise ImportError("no cryptography")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    cert, key = _paths(tmp_path)
    res = _ensure(cert, key, [LAN])
    assert res.ok is False and res.action == "failed"
    assert not cert.exists()


# ── reading ──────────────────────────────────────────────────────────────────

def test_cert_info_and_known_host_names(tmp_path):
    cert, key = _paths(tmp_path)
    _ensure(cert, key, [LAN], extra=["mast-box.tail1a2b.ts.net", NEW_TS], hostname="Mast-Box")
    info = tls.cert_info(cert)
    assert info is not None and info.issued_by_mast
    assert info.spki_sha256 == tls.spki_sha256(cert)
    assert info.covers(NEW_TS) and info.covers("[::1]") and info.covers("MAST-BOX.TAIL1A2B.TS.NET.")
    assert not info.covers("100.64.0.1")
    assert tls.cert_info(tmp_path / "missing.pem") is None
    names = tls.known_host_names(cert)
    assert {"localhost", "mast-box", "mast-box.tail1a2b.ts.net"} <= names


@pytest.mark.parametrize("addr", ["127.0.0.1", "::1", "10.1.2.3", "fd7a:115c:a1e0::1"])
def test_required_ip_kinds(addr):
    _, ips = tls.required_hosts(addresses=[addr], hostname="")
    assert ipaddress.ip_address(addr).compressed in ips
