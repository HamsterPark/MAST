"""The LAN TLS certificate: keep its key, pin its SPKI.

Why this exists
---------------
LAN / remote-access mode serves https with a self-signed certificate
(``<data>/api key/lan_cert.pem`` + ``lan_key.pem``). It has to list every address
a client dials — LAN IP, Tailscale IP, later the fibre NIC — so it is re-signed
whenever this machine gains an address. Earlier versions minted
a NEW private key on every re-sign. Satellites pin the SHA-256 of the certificate's
SubjectPublicKeyInfo (``docs/v2/design/ters_satellite_integration.md`` §5.1,
trap 10), so a new key cuts every satellite off the hub the first time an IP
changes. This is not hypothetical: a machine that moves to another tailnet still has
the previous Tailscale IP in its certificate, so its next start re-signs.

Rules
-----
* **The key survives re-signing.** :func:`ensure_lan_cert` loads the existing key
  and mints a new one only when there is none, or it cannot be read (the
  unreadable file is moved aside, not overwritten) — and logs the new pin.
* **The SAN only grows.** A re-sign keeps every name and IP the old certificate
  listed, so a laptop moving between networks does not flip between two
  certificates. (Capped at :data:`MAX_SAN_ENTRIES`.)
* **Only dialable addresses are required.** The listener binds IPv4 ``0.0.0.0``:
  IPv6 privacy addresses (they rotate daily) and link-local addresses are not
  required; loopback and the stable Tailscale ULA are. Explicit ``extra_hosts``
  always are.
* **Someone else's certificate is left alone.** Only certificates this module
  issues (self-signed, ``CN=MAST LAN``) are re-signed; an operator-supplied one
  is kept and its gaps are logged.

Not fixed here: clients that trust the certificate *file* — the OTA
``update_server_ca.pem`` pin, a certificate imported into the Windows store —
still reject a re-signed certificate even with the same key. OpenSSL only
accepts a self-signed leaf byte-identical to the trusted one (verified on
OpenSSL 3.0, with and without CA extensions). Re-signing less
often is all this module can do for them.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import logging
import os
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Union

from mast.net.addrs import TAILSCALE_V6, local_addresses, norm_ip

logger = logging.getLogger(__name__)

PathLike = Union[str, "os.PathLike[str]"]

#: Subject (and issuer) of every certificate this module issues.
COMMON_NAME = "MAST LAN"
VALIDITY = _dt.timedelta(days=3650)
#: Re-sign (same key) once fewer than this many days of validity remain.
RENEW_BEFORE = _dt.timedelta(days=30)
#: Upper bound on SAN entries kept across re-signs; beyond it the history is
#: dropped and only what is required now is listed.
MAX_SAN_ENTRIES = 128


# ── SPKI pins ───────────────────────────────────────────────────────────────

def _load_cert(data: bytes):
    from cryptography import x509

    if b"-----BEGIN" in data:
        return x509.load_pem_x509_certificate(data)
    return x509.load_der_x509_certificate(data)


def _spki_der(public_key) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return public_key.public_bytes(serialization.Encoding.DER,
                                   serialization.PublicFormat.SubjectPublicKeyInfo)


def spki_sha256_of(cert_bytes: bytes) -> str:
    """Lower-case hex SHA-256 of the DER SubjectPublicKeyInfo of a certificate
    given as PEM or DER bytes — the value a satellite stores as
    ``hub.spki_sha256``. Same digest as
    ``openssl x509 -pubkey -noout | openssl pkey -pubin -outform der | sha256sum``."""
    return hashlib.sha256(_spki_der(_load_cert(cert_bytes).public_key())).hexdigest()


def spki_sha256(cert: Union[PathLike, bytes]) -> str:
    """SPKI pin (lower-case hex, 64 chars, no colons) of a certificate given as a
    file path (PEM or DER) or as PEM/DER bytes."""
    if isinstance(cert, (bytes, bytearray, memoryview)):
        return spki_sha256_of(bytes(cert))
    return spki_sha256_of(Path(cert).read_bytes())


def lan_cert_paths(user_root: PathLike) -> tuple[Path, Path]:
    """``(cert, key)`` of the LAN certificate under a MAST data root —
    ``<root>/api key/lan_cert.pem`` and ``lan_key.pem``, what the launcher serves."""
    d = Path(user_root) / "api key"
    return d / "lan_cert.pem", d / "lan_key.pem"


def peer_spki_sha256(host: str, port: int, *, timeout: float = 5.0) -> str:
    """SPKI pin of the certificate a live TLS server presents.

    Reads the pin without trusting anything (no chain or name check) — for
    comparing what a server actually serves against a configured pin. Raises
    ``OSError`` / ``ssl.SSLError`` when the server cannot be reached."""
    h = str(host).strip().strip("[]")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((h, int(port)), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=None if norm_ip(h) else h) as tls:
            der = tls.getpeercert(binary_form=True)
    return spki_sha256_of(der)


# ── reading certificates ────────────────────────────────────────────────────

def _san(cert) -> tuple[set[str], set[str]]:
    from cryptography import x509

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set(), set()
    names = {str(n).lower() for n in san.get_values_for_type(x509.DNSName)}
    ips = {ipaddress.ip_address(i).compressed for i in san.get_values_for_type(x509.IPAddress)}
    return names, ips


def _not_after(cert) -> _dt.datetime:
    na = getattr(cert, "not_valid_after_utc", None)
    if na is None:  # cryptography < 42
        na = cert.not_valid_after.replace(tzinfo=_dt.timezone.utc)
    return na


def _is_ours(cert) -> bool:
    from cryptography.x509.oid import NameOID

    if cert.issuer != cert.subject:
        return False
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return bool(cn) and cn[0].value == COMMON_NAME


@dataclass(frozen=True)
class CertInfo:
    """What a certificate file says, for display and diagnostics."""

    spki_sha256: str
    dns_names: tuple[str, ...]
    ip_addresses: tuple[str, ...]
    not_after: str          # ISO-8601, UTC
    issued_by_mast: bool    # self-signed CN=MAST LAN (re-signed automatically)

    def covers(self, host: str) -> bool:
        ip = norm_ip(host)
        if ip is not None:
            return ip in self.ip_addresses
        return str(host or "").strip().rstrip(".").lower() in self.dns_names


def cert_info(cert_path: PathLike) -> Optional[CertInfo]:
    """Parse *cert_path*; ``None`` when it is missing or unreadable."""
    try:
        cert = _load_cert(Path(cert_path).read_bytes())
    except Exception:  # noqa: BLE001 — missing / not a certificate / no cryptography
        return None
    names, ips = _san(cert)
    return CertInfo(
        spki_sha256=hashlib.sha256(_spki_der(cert.public_key())).hexdigest(),
        dns_names=tuple(sorted(names)),
        ip_addresses=tuple(sorted(ips)),
        not_after=_not_after(cert).isoformat(),
        issued_by_mast=_is_ours(cert),
    )


# ── what the certificate must cover ─────────────────────────────────────────

def _required_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if a.is_loopback:
        return True
    if a.version == 4:
        return not (a.is_link_local or a.is_unspecified or a.is_multicast)
    return a in TAILSCALE_V6


def required_hosts(extra_hosts: Iterable[str] = (), *,
                   addresses: Optional[list[str]] = None,
                   hostname: Optional[str] = None) -> tuple[set[str], set[str]]:
    """``(dns_names, ips)`` the LAN certificate must list: loopback, this
    machine's hostname and dialable local addresses (see module docs), plus
    every explicit *extra_hosts* entry (wildcard binds skipped). Names are
    lower-cased, IPs canonical."""
    names = {"localhost"}
    ips = {"127.0.0.1", "::1"}
    if hostname is None:
        try:
            hostname = socket.gethostname()
        except OSError:
            hostname = ""
    if hostname:
        names.add(hostname.lower())
    for raw in (local_addresses() if addresses is None else addresses):
        ip = norm_ip(raw)
        if ip and _required_ip(ip):
            ips.add(ip)
    for h in extra_hosts or ():
        raw = str(h or "").strip().strip("[]").rstrip(".")
        if not raw or raw in ("0.0.0.0", "::"):
            continue
        ip = norm_ip(raw)
        if ip is not None:
            ips.add(ip)
        else:
            names.add(raw.lower())
    return names, ips


def known_host_names(cert_path: Optional[PathLike] = None) -> set[str]:
    """Host names (lower-case) this machine answers to: ``localhost``, its
    hostname, and every DNS name in *cert_path*'s SAN (MagicDNS included once
    it has been folded in). For a Host-header allowlist — IP literals are not
    included; allow those by form."""
    names, _ = required_hosts(addresses=[])
    if cert_path is not None:
        info = cert_info(cert_path)
        if info is not None:
            names.update(info.dns_names)
    return names


# ── issuing ─────────────────────────────────────────────────────────────────

@dataclass
class EnsureResult:
    ok: bool                       # a usable cert/key pair exists afterwards
    action: str                    # kept | resigned | created | failed
    spki_sha256: str = ""
    key_reused: bool = False
    added: list[str] = field(default_factory=list)   # names/IPs newly listed
    reason: str = ""


def _read_key(path: Path):
    from cryptography.hazmat.primitives import serialization

    try:
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    except Exception:  # noqa: BLE001 — missing / encrypted / not a key
        return None


def _read_cert(path: Path):
    try:
        return _load_cert(path.read_bytes())
    except Exception:  # noqa: BLE001
        return None


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _build(key, names: set[str], ips: set[str], now: _dt.datetime):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)])
    san = [x509.DNSName(n) for n in sorted(names)]
    san += [x509.IPAddress(ipaddress.ip_address(i)) for i in sorted(ips)]
    return (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + VALIDITY)
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )


def ensure_lan_cert(cert_path: PathLike, key_path: PathLike, *,
                    extra_hosts: Iterable[str] = (),
                    log: Optional[logging.Logger] = None,
                    now: Optional[_dt.datetime] = None,
                    addresses: Optional[list[str]] = None,
                    hostname: Optional[str] = None) -> EnsureResult:
    """Make sure *cert_path*/*key_path* hold a usable pair covering
    :func:`required_hosts`. Re-signs with the SAME key when something is
    missing, the pair does not match, or validity runs short; see module docs.
    Never raises. *addresses*/*hostname*/*now* exist for tests."""
    log = log or logger
    cert_p, key_p = Path(cert_path), Path(key_path)
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError as exc:
        log.warning("LAN TLS: `cryptography` unavailable (%s) — cannot issue a certificate.", exc)
        return EnsureResult(False, "failed", reason=f"cryptography unavailable: {exc}")

    now = now or _dt.datetime.now(_dt.timezone.utc)
    names, ips = required_hosts(extra_hosts, addresses=addresses, hostname=hostname)
    key = _read_key(key_p)
    old = _read_cert(cert_p)
    have_names, have_ips = _san(old) if old is not None else (set(), set())
    missing = sorted(names - have_names) + sorted(ips - have_ips)
    paired = (key is not None and old is not None
              and _spki_der(old.public_key()) == _spki_der(key.public_key()))

    if old is not None and not _is_ours(old):
        # Operator-supplied certificate: never overwrite it.
        spki = hashlib.sha256(_spki_der(old.public_key())).hexdigest()
        if missing or not paired:
            log.warning("LAN TLS: %s is not a MAST-issued certificate — left as is%s%s.",
                        cert_p, f"; it does not list {missing}" if missing else "",
                        "" if paired else "; it does NOT match the key file")
        return EnsureResult(True, "kept", spki_sha256=spki, key_reused=True,
                            reason="foreign certificate")

    fresh = old is not None and _not_after(old) - now > RENEW_BEFORE
    if paired and fresh and not missing:
        return EnsureResult(True, "kept", key_reused=True,
                            spki_sha256=hashlib.sha256(_spki_der(key.public_key())).hexdigest())

    reasons: list[str] = []
    if old is None:
        reasons.append("certificate unreadable" if cert_p.exists() else "no certificate")
    elif not fresh:
        reasons.append(f"expires {_not_after(old).date()}")
    if old is not None and key is not None and not paired:
        reasons.append("certificate does not match the key")
    if missing:
        reasons.append("not listed: " + ", ".join(missing))

    key_reused = key is not None
    try:
        cert_p.parent.mkdir(parents=True, exist_ok=True)
        if key is None:
            if key_p.exists():
                aside = key_p.with_name(f"{key_p.name}.unreadable-{now:%Y%m%d%H%M%S}")
                os.replace(key_p, aside)
                log.warning("LAN TLS: key file %s unreadable — moved to %s.", key_p, aside.name)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            _atomic_write(key_p, key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        want_names, want_ips = names | have_names, ips | have_ips
        if len(want_names) + len(want_ips) > MAX_SAN_ENTRIES:
            want_names, want_ips = names, ips
        cert = _build(key, want_names, want_ips, now)
        _atomic_write(cert_p, cert.public_bytes(serialization.Encoding.PEM))
    except Exception as exc:  # noqa: BLE001 — never block boot on certificate I/O
        log.warning("LAN TLS: could not write %s / %s (%s).", cert_p, key_p, exc)
        return EnsureResult(False, "failed", reason=str(exc))

    spki = hashlib.sha256(_spki_der(key.public_key())).hexdigest()
    reason = "; ".join(reasons)
    if key_reused:
        log.info("LAN TLS: re-signed %s (%s) — same key, SPKI unchanged %s.", cert_p, reason, spki)
        action = "resigned"
    elif old is not None or cert_p.exists():
        log.warning("LAN TLS: NEW key for %s (%s) — SPKI is now %s; re-pin every satellite "
                    "(hub.spki_sha256).", cert_p, reason, spki)
        action = "created"
    else:
        log.info("LAN TLS: generated %s, SPKI %s.", cert_p, spki)
        action = "created"
    return EnsureResult(True, action, spki_sha256=spki, key_reused=key_reused,
                        added=missing, reason=reason)


__all__ = [
    "COMMON_NAME",
    "MAX_SAN_ENTRIES",
    "CertInfo",
    "EnsureResult",
    "cert_info",
    "ensure_lan_cert",
    "known_host_names",
    "lan_cert_paths",
    "peer_spki_sha256",
    "required_hosts",
    "spki_sha256",
    "spki_sha256_of",
]
