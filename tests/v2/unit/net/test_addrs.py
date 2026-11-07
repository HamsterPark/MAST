"""Local addresses + client-path classification — mast.net.addrs.

Motivating case: a full-tunnel VPN
routes 0.0.0.0/1, so the UDP route trick toward 8.8.8.8 returned the tunnel
address 192.168.50.7, and 设置 → 远程访问 advertised it as「局域网地址」while
the Wi-Fi address 10.0.1.79 was not shown at all.
"""
from __future__ import annotations

import sys
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

from mast.net import addrs  # noqa: E402

# getaddrinfo(hostname) example (Wi-Fi + a full-tunnel VPN + Tailscale)
LAPTOP = ["10.0.1.79", "100.64.3.57", "192.168.50.7", "2001:db8:a20::7",
          "fd7a:115c:a1e0::c3", "fe80::3:4"]


@pytest.mark.parametrize("raw,want", [
    ("10.0.0.1", "10.0.0.1"),
    ("[fd7a:115c:a1e0::1]", "fd7a:115c:a1e0::1"),
    ("fe80::1%12", "fe80::1"),
    ("0:0:0:0:0:0:0:1", "::1"),
    (" 100.64.0.1 ", "100.64.0.1"),
    ("rig-pc", None),
    ("", None),
    (None, None),
])
def test_norm_ip(raw, want):
    assert addrs.norm_ip(raw) == want


def test_vpn_laptop_lists_wifi_and_tunnel_and_nothing_else():
    got = addrs.lan_ipv4_candidates(LAPTOP, route_guess="192.168.50.7")
    assert got == ["192.168.50.7", "10.0.1.79"]   # guess first, Wi-Fi still offered
    assert "100.64.3.57" not in got                  # Tailscale is not a LAN address


def test_single_nic_reads_as_before():
    assert addrs.lan_ipv4_candidates(["10.0.0.11", "192.168.60.1", "100.64.2.38"],
                                     route_guess="10.0.0.11") == ["10.0.0.11", "192.168.60.1"]


def test_guess_is_listed_even_when_the_hostname_does_not_resolve_to_it():
    # Linux: hostname → 127.0.1.1 only; the old single-guess behaviour must survive.
    assert addrs.lan_ipv4_candidates(["127.0.1.1"], route_guess="10.0.0.5") == ["10.0.0.5"]
    assert addrs.lan_ipv4_candidates(["10.0.0.5", "169.254.3.3", "127.0.0.1"],
                                     route_guess="192.168.60.9") == ["192.168.60.9", "10.0.0.5"]


def test_unusable_guess_is_dropped():
    for bad in ("127.0.0.1", "169.254.1.1", "100.64.2.38", "not-an-ip", ""):
        assert addrs.lan_ipv4_candidates(["10.0.0.5"], route_guess=bad) == ["10.0.0.5"]


def test_offline_gives_empty():
    assert addrs.lan_ipv4_candidates([], route_guess="") == []


@pytest.mark.parametrize("ip,via", [
    ("127.0.0.1", "local"),
    ("::1", "local"),
    ("100.64.9.81", "tailscale"),
    ("fd7a:115c:a1e0::c3", "tailscale"),
    ("10.0.0.10", "lan"),
    ("192.168.1.9", "lan"),
    ("fe80::1", "lan"),
    ("203.0.113.42", "other"),
    ("testclient", "other"),
    ("", "other"),
])
def test_classify_client(ip, via):
    assert addrs.classify_client(ip) == via


def test_tailscale_range_edges():
    assert addrs.is_tailscale_ip("100.64.0.0") and addrs.is_tailscale_ip("100.127.255.255")
    assert not addrs.is_tailscale_ip("100.63.255.255") and not addrs.is_tailscale_ip("100.128.0.0")


def test_local_addresses_never_raises(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("resolver down")

    monkeypatch.setattr(addrs.socket, "getaddrinfo", boom)
    assert addrs.local_addresses() == []
