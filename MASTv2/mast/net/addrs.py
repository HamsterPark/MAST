"""This machine's addresses, and which path a client reached us on.

Two consumers: the LAN certificate (:mod:`mast.net.tls`, which must list every
address a peer dials) and the 设置 → 远程访问 panel (which shows the LAN
address(es) and tells a remote viewer how they got here).

Why not the UDP-socket trick (``connect(("8.8.8.8", 80))`` → ``getsockname()``)
for "the LAN IP": it returns the interface of the route to 8.8.8.8, and a
full-tunnel VPN owns that route. On a machine whose VPN routes 0.0.0.0/1 it
returned the tunnel's own address instead of the Wi-Fi one, so the panel advertised an address no LAN peer can reach. Nothing
on this side can tell a VPN adapter from a NIC by its address alone, so
:func:`lan_ipv4_candidates` lists them all and lets the operator pick; the old
guess is kept only as an ordering hint.

Everything here is pure stdlib and never raises: resolution failures give
empty results.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Optional

#: Tailscale's address space: IPv4 CGNAT 100.64.0.0/10 and the IPv6 ULA.
TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

#: What counts as "came in over a local network". Explicit rather than
#: ``is_private``, which also covers documentation / benchmarking ranges.
_LAN_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "fc00::/7", "fe80::/10",
))


def norm_ip(value: object) -> Optional[str]:
    """Canonical text form of an IP address (brackets and ``%zone`` stripped),
    or ``None`` when *value* is not an IP literal."""
    v = str(value or "").strip().strip("[]").split("%", 1)[0]
    try:
        return ipaddress.ip_address(v).compressed
    except ValueError:
        return None


def is_tailscale_ip(ip: str) -> bool:
    n = norm_ip(ip)
    if n is None:
        return False
    a = ipaddress.ip_address(n)
    return a in (TAILSCALE_V4 if a.version == 4 else TAILSCALE_V6)


def local_addresses() -> list[str]:
    """Every address the hostname resolves to locally, canonical, de-duplicated,
    in resolver order. On Windows that is the address of each adapter that is up
    (Tailscale included once it is connected)."""
    try:
        host = socket.gethostname()
    except OSError:
        return []
    out: list[str] = []
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    for info in infos:
        ip = norm_ip(info[4][0])
        if ip and ip not in out:
            out.append(ip)
    return out


def route_guess_ipv4() -> str:
    """Source address of the route to 8.8.8.8 (no packet is sent), '' if none.
    A VPN that owns that route makes this the tunnel address — see module docs."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""
    return ip if ip and not ip.startswith("127.") else ""


def _lan_candidate(ip: Optional[str]) -> bool:
    if ip is None:
        return False
    a = ipaddress.ip_address(ip)
    return not (a.version != 4 or a.is_loopback or a.is_link_local or a.is_unspecified
                or a in TAILSCALE_V4)


def lan_ipv4_candidates(addresses: Optional[list[str]] = None,
                        route_guess: Optional[str] = None) -> list[str]:
    """IPv4 addresses a peer on a local network might dial: no loopback,
    link-local or Tailscale addresses. The route guess goes first so the
    single-address case reads as before — it is a local address by construction
    (``getsockname``), so it is listed even where the hostname does not resolve
    to it (Linux hosts files often map the hostname to 127.0.1.1 only)."""
    addrs = local_addresses() if addresses is None else addresses
    guess = norm_ip(route_guess_ipv4() if route_guess is None else route_guess)
    out: list[str] = []
    for raw in addrs:
        ip = norm_ip(raw)
        if _lan_candidate(ip) and ip not in out:
            out.append(ip)
    if _lan_candidate(guess):
        if guess in out:
            out.remove(guess)
        out.insert(0, guess)
    return out


def classify_client(ip: str) -> str:
    """Which path a client reached us on: ``local`` / ``tailscale`` / ``lan`` /
    ``other`` (``other`` also covers an unparseable address)."""
    n = norm_ip(ip)
    if n is None:
        return "other"
    a = ipaddress.ip_address(n)
    if a.is_loopback:
        return "local"
    if is_tailscale_ip(n):
        return "tailscale"
    if any(a in net for net in _LAN_NETS if net.version == a.version):
        return "lan"
    return "other"


__all__ = [
    "TAILSCALE_V4",
    "TAILSCALE_V6",
    "norm_ip",
    "is_tailscale_ip",
    "local_addresses",
    "route_guess_ipv4",
    "lan_ipv4_candidates",
    "classify_client",
]
