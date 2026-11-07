"""Contract tests for GET /api/remote-access (设置 → 远程访问).

Tailscale detection is monkeypatched, so the route is exercised across bind
states + Tailscale states with no daemon. See mast/net/tailscale.py.
"""
from __future__ import annotations

import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mast.api.context import AppContext
from mast.api.routes.remote_access import router
from mast.net.tailscale import TailscaleStatus


def _client(*, bound_host: str | None = None, ssl: bool = False, port: int = 7862,
            certfile: str | None = None) -> TestClient:
    app = FastAPI()
    app.state.ctx = AppContext()
    if bound_host is not None:
        cfg = types.SimpleNamespace(
            host=bound_host, port=port,
            ssl_certfile=(certfile or ("cert.pem" if ssl else None)),
        )
        app.state.uvicorn_server = types.SimpleNamespace(config=cfg)
    app.include_router(router, prefix="/api")
    return TestClient(app)


def _ready() -> TailscaleStatus:
    return TailscaleStatus(
        installed=True, running=True, backend_state="Running",
        self_ips=["100.101.102.103", "fd7a:115c:a1e0::9"],
        magic_dns="mast-box.tail1a2b.ts.net", tailnet="tail1a2b.ts.net",
        device_name="mast-box",
    )


def test_not_bound_reports_local_only(monkeypatch):
    monkeypatch.setattr("mast.net.tailscale.get_status",
                        lambda *a, **k: TailscaleStatus(installed=False, hint="装 Tailscale"))
    body = _client().get("/api/remote-access").json()          # no uvicorn_server → not bound
    assert body["lan_enabled"] is False
    assert "远程访问未开启" in body["note"]
    assert body["urls"] == []
    assert body["tailscale"]["installed"] is False


def test_bound_and_ready_gives_url(monkeypatch):
    monkeypatch.setattr("mast.net.tailscale.get_status", lambda *a, **k: _ready())
    body = _client(bound_host="0.0.0.0", ssl=True, port=7862).get("/api/remote-access").json()
    assert body["lan_enabled"] is True
    assert body["scheme"] == "https"
    assert body["port"] == 7862
    assert body["tailscale"]["ready"] is True
    assert body["tailscale"]["device_name"] == "mast-box"
    # IP recommended first (self-signed + *.ts.net HSTS → MagicDNS blocked in browser)
    assert body["urls"][0]["url"] == "https://100.101.102.103:7862"
    assert body["urls"][-1]["url"] == "https://mast-box.tail1a2b.ts.net:7862"
    assert "就绪" in body["note"]


def test_bound_but_tailscale_stopped_surfaces_hint(monkeypatch):
    st = TailscaleStatus(installed=True, running=False, backend_state="Stopped",
                         hint="打开 Tailscale 并保持登录")
    monkeypatch.setattr("mast.net.tailscale.get_status", lambda *a, **k: st)
    body = _client(bound_host="0.0.0.0", ssl=False, port=7862).get("/api/remote-access").json()
    assert body["lan_enabled"] is True
    assert body["scheme"] == "http"          # no ssl_certfile → http
    assert body["urls"] == []
    assert body["note"] == "打开 Tailscale 并保持登录"


def test_ipv6_bind_counts_as_bound(monkeypatch):
    monkeypatch.setattr("mast.net.tailscale.get_status", lambda *a, **k: _ready())
    body = _client(bound_host="::", ssl=True).get("/api/remote-access").json()
    assert body["lan_enabled"] is True
    assert any(u["url"] == "https://[fd7a:115c:a1e0::9]:7862" for u in body["urls"])


def test_ipv4_bind_does_not_offer_the_ipv6_url(monkeypatch):
    # 0.0.0.0 takes no IPv6: the [fd7a:…] URL refused connections.
    monkeypatch.setattr("mast.net.tailscale.get_status", lambda *a, **k: _ready())
    body = _client(bound_host="0.0.0.0", ssl=True).get("/api/remote-access").json()
    assert not any("fd7a" in u["url"] for u in body["urls"])


def test_note_names_the_tailnet(monkeypatch):
    monkeypatch.setattr("mast.net.tailscale.get_status", lambda *a, **k: _ready())
    body = _client(bound_host="0.0.0.0", ssl=True).get("/api/remote-access").json()
    assert "tail1a2b.ts.net" in body["note"]


def test_lan_ips_lists_every_candidate(monkeypatch):
    monkeypatch.setattr("mast.net.tailscale.get_status", lambda *a, **k: _ready())
    monkeypatch.setattr("mast.net.addrs.lan_ipv4_candidates",
                        lambda *a, **k: ["192.168.50.7", "10.0.1.79"])
    body = _client(bound_host="0.0.0.0", ssl=True).get("/api/remote-access").json()
    assert body["lan_ips"] == ["192.168.50.7", "10.0.1.79"]
    assert body["lan_ip"] == "192.168.50.7"


def test_viewer_says_how_the_request_arrived(monkeypatch):
    monkeypatch.setattr("mast.net.tailscale.get_status", lambda *a, **k: _ready())
    body = _client(bound_host="0.0.0.0", ssl=True).get(
        "/api/remote-access", headers={"host": "100.101.102.103:7862"}).json()
    assert body["viewer"]["host"] == "100.101.102.103:7862"
    assert body["viewer"]["client_ip"] == "testclient"
    assert body["viewer"]["via"] == "other"


def test_tls_reports_the_pin_and_what_the_cert_does_not_list(monkeypatch, tmp_path):
    from mast.net.tls import ensure_lan_cert, spki_sha256

    cert, key = tmp_path / "lan_cert.pem", tmp_path / "lan_key.pem"
    ensure_lan_cert(cert, key, addresses=["10.0.0.11"], hostname="box")
    monkeypatch.setattr("mast.net.tailscale.get_status", lambda *a, **k: _ready())
    monkeypatch.setattr("mast.net.addrs.lan_ipv4_candidates", lambda *a, **k: ["10.0.0.11"])
    body = _client(bound_host="0.0.0.0", certfile=str(cert)).get("/api/remote-access").json()
    assert body["scheme"] == "https"
    t = body["tls"]
    assert t["spki_sha256"] == spki_sha256(cert)
    assert "10.0.0.11" in t["ip_addresses"] and t["issued_by_mast"] is True
    assert t["uncovered"] == ["100.101.102.103"]          # Tailscale IP not in the SAN yet


def test_no_tls_block_without_a_readable_cert(monkeypatch):
    monkeypatch.setattr("mast.net.tailscale.get_status", lambda *a, **k: _ready())
    assert _client(bound_host="0.0.0.0", ssl=True).get("/api/remote-access").json()["tls"] is None
    assert _client(bound_host="0.0.0.0").get("/api/remote-access").json()["tls"] is None


def test_response_shape_is_typed(monkeypatch):
    monkeypatch.setattr("mast.net.tailscale.get_status", lambda *a, **k: _ready())
    body = _client(bound_host="0.0.0.0", ssl=True).get("/api/remote-access").json()
    for key in ("lan_enabled", "scheme", "port", "lan_ip", "lan_ips", "tailscale", "urls",
                "note", "viewer", "tls"):
        assert key in body
    for key in ("installed", "running", "ready", "backend_state", "device_name",
                "magic_dns", "tailnet", "ips", "hint"):
        assert key in body["tailscale"]
