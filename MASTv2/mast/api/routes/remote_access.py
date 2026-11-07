"""GET /api/remote-access — cross-network remote-control readiness (Tailscale).

Surfaces, for the in-app 设置 → 远程访问 panel: whether the service is bound for
remote access (0.0.0.0), the scheme/port actually serving, this machine's LAN
address(es), the live Tailscale status + the exact URL(s) to open on ANOTHER
computer, the serving certificate's SPKI pin, and how THIS request arrived (so an
operator checking from the far end sees「经 Tailscale」).

The heavy lifting (CLI detection) is :mod:`mast.net.tailscale`; this route is a
thin adapter that never raises. It's a *sync* def on purpose so FastAPI runs it
in the threadpool — ``tailscale status`` can block a couple of seconds and must
not stall the event loop.
"""
from __future__ import annotations

from typing import NamedTuple, Optional
from mast.i18n import tr

from fastapi import APIRouter, Request

from mast.api.schemas_remote_access import (
    RemoteAccessResponse,
    RemoteUrl,
    TailscaleInfo,
    TlsInfo,
    ViewerInfo,
)

router = APIRouter(tags=["remote-access"])


class _Binding(NamedTuple):
    scheme: str
    port: int
    bound_all: bool        # reachable from other machines at all
    bound_ipv6: bool       # the listener also takes IPv6 (host "::")
    certfile: Optional[str]


def _server_binding(request: Request) -> _Binding:
    """Prefers the live uvicorn server's config (the source of truth for the bind
    host + TLS); falls back to the request the client actually reached us on
    (correct for scheme/port even behind the SPA, just blind to the bind host).
    """
    scheme = request.url.scheme
    port = request.url.port or (443 if scheme == "https" else 80)
    bound_all = bound_ipv6 = False
    certfile: Optional[str] = None
    server = getattr(request.app.state, "uvicorn_server", None)
    cfg = getattr(server, "config", None)
    if cfg is not None:
        host = str(getattr(cfg, "host", "") or "")
        bound_all = host in ("0.0.0.0", "::", "")
        bound_ipv6 = host == "::"
        p = getattr(cfg, "port", None)
        if p:
            port = p
        certfile = getattr(cfg, "ssl_certfile", None) or None
        if certfile:
            scheme = "https"
    return _Binding(scheme, port, bound_all, bound_ipv6, certfile)


def _viewer(request: Request) -> ViewerInfo:
    from mast.net.addrs import classify_client

    ip = str(getattr(getattr(request, "client", None), "host", "") or "")
    return ViewerInfo(client_ip=ip, host=request.headers.get("host", ""),
                      via=classify_client(ip))


def _tls(certfile: Optional[str], wanted: list[str]) -> Optional[TlsInfo]:
    if not certfile:
        return None
    from mast.net.tls import cert_info

    info = cert_info(certfile)
    if info is None:
        return None
    return TlsInfo(
        spki_sha256=info.spki_sha256,
        dns_names=list(info.dns_names),
        ip_addresses=list(info.ip_addresses),
        not_after=info.not_after,
        issued_by_mast=info.issued_by_mast,
        uncovered=[h for h in wanted if not info.covers(h)],
    )


@router.get("/remote-access", response_model=RemoteAccessResponse)
def remote_access(request: Request) -> RemoteAccessResponse:
    from mast.net import tailscale as ts
    from mast.net.addrs import lan_ipv4_candidates

    b = _server_binding(request)
    status = ts.get_status()
    urls = ts.remote_urls(status, b.port, b.scheme, include_ipv6=b.bound_ipv6)
    lan_ips = lan_ipv4_candidates()

    if not b.bound_all:
        note = (tr("远程访问未开启 —— 目前仅本机可访问。在启动器勾选"
                "「启用局域网 / 远程访问」并重启服务即可开放跨网控制。", "Remote access is off: only this machine can reach MAST for now. Tick \"Enable LAN / remote access\" in the launcher and restart the service to allow cross-network control."))
    elif not status.installed:
        note = status.hint
    elif not status.ready:
        note = status.hint
    elif urls:
        where = f" {status.tailnet}" if status.tailnet else ""
        note = (tr(f"就绪 —— 在另一台电脑（登录同一个 Tailscale 网络{where}）"
                f"浏览器打开：{urls[0]['url']}", f"Ready. On another computer (signed in to the same Tailscale network{where}), open in a browser: {urls[0]['url']}"))
    else:
        note = tr("已绑定远程访问，但暂未取得 Tailscale 地址，请稍候刷新。", "Remote access is bound, but no Tailscale address yet; please refresh in a moment.")

    ts_v4 = [ip for ip in status.self_ips if ":" not in ip]
    return RemoteAccessResponse(
        lan_enabled=b.bound_all,
        scheme=b.scheme,
        port=b.port,
        lan_ip=lan_ips[0] if lan_ips else "",
        lan_ips=lan_ips,
        tailscale=TailscaleInfo(
            installed=status.installed,
            running=status.running,
            ready=status.ready,
            backend_state=status.backend_state,
            device_name=status.device_name,
            magic_dns=status.magic_dns,
            tailnet=status.tailnet,
            ips=list(status.self_ips),
            hint=status.hint,
        ),
        urls=[RemoteUrl(label=u["label"], host=u["host"], url=u["url"]) for u in urls],
        note=note,
        viewer=_viewer(request),
        tls=_tls(b.certfile, ts_v4 + lan_ips),
    )
