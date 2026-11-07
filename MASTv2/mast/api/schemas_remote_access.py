"""Typed response for GET /api/remote-access (设置 → 远程访问 panel).

Mirrors :mod:`mast.net.tailscale` for the wire. Kept out of the big shared
``schemas.py`` (like schemas_optics / schemas_settings_admin_write) so the
remote-access feature stays self-contained.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TailscaleInfo(BaseModel):
    """Live Tailscale node status (from ``tailscale status --json``)."""
    installed: bool = Field(description="tailscale CLI/daemon found on this machine")
    running: bool = Field(description="daemon BackendState == Running")
    ready: bool = Field(description="installed + running + holding a Tailscale IP")
    backend_state: str = Field(default="", description="Running / Stopped / NeedsLogin / …")
    device_name: str = Field(default="", description="this node's short hostname")
    magic_dns: str = Field(default="", description="MagicDNS FQDN (empty if MagicDNS off)")
    tailnet: str = Field(default="", description="tailnet name, e.g. name.ts.net")
    ips: list[str] = Field(default_factory=list, description="Tailscale IPs (v4 first)")
    hint: str = Field(default="", description="Chinese next-step when not ready")


class RemoteUrl(BaseModel):
    """One address to open on the OTHER computer."""
    label: str
    host: str
    url: str


class TlsInfo(BaseModel):
    """The certificate the https listener is serving (LAN mode)."""
    spki_sha256: str = Field(description="hex SHA-256 of the DER SubjectPublicKeyInfo — "
                                         "the satellite pin (hub.spki_sha256)")
    dns_names: list[str] = Field(default_factory=list, description="SAN DNS names")
    ip_addresses: list[str] = Field(default_factory=list, description="SAN IP addresses")
    not_after: str = Field(default="", description="expiry, ISO-8601 UTC")
    issued_by_mast: bool = Field(
        default=True, description="self-signed CN=MAST LAN (re-signed automatically, same key)")
    uncovered: list[str] = Field(
        default_factory=list,
        description="current Tailscale/LAN addresses the certificate does not list yet "
                    "(added at the next service start; the key and SPKI stay the same)")


class ViewerInfo(BaseModel):
    """How THIS request reached the service — for checking a remote path."""
    client_ip: str = Field(default="", description="peer address of this request")
    host: str = Field(default="", description="Host header the browser used")
    via: str = Field(default="other", description="local | tailscale | lan | other")


class RemoteAccessResponse(BaseModel):
    lan_enabled: bool = Field(description="service bound to 0.0.0.0 (remote reachable)")
    scheme: str = Field(description="http | https actually serving")
    port: int = Field(description="port the service is serving on")
    lan_ip: str = Field(default="", description="this machine's local-network IPv4 "
                                                "(first of lan_ips)")
    lan_ips: list[str] = Field(
        default_factory=list,
        description="every IPv4 a local-network peer might dial (a VPN adapter's address "
                    "cannot be told apart from a NIC's, so all are listed)")
    tailscale: TailscaleInfo
    urls: list[RemoteUrl] = Field(
        default_factory=list, description="addresses to open on another computer")
    note: str = Field(default="", description="one-line human guidance for the panel")
    viewer: ViewerInfo | None = Field(default=None, description="how this request arrived")
    tls: TlsInfo | None = Field(default=None, description="serving certificate (https only)")


__all__ = ["TailscaleInfo", "RemoteUrl", "TlsInfo", "ViewerInfo", "RemoteAccessResponse"]
