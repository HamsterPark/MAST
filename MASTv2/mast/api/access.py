"""主服务的访问闸门：纯 ASGI 中间件，http 与 websocket 都过它。

为什么不再是 ``BaseHTTPMiddleware``：它只处理 http scope，websocket 原样放行 —— LAN 模式下 ``/ws/*``
先 ``accept()`` 后不认证，局域网里任何人不带密码就能连 ``/ws/events`` 看实时读数、连 ``/ws/voice``
往仪器 agent 里送话（修之前的实测见 ``tests/v2/unit/api/test_ws_auth.py``）。

规则
----
1. ``/ws/satellite/`` 下的 WS 整段放行：卫星不知道操作员密码，它的令牌由卫星端点自己在 accept 之前验。
2. **同源**：浏览器发起的 WS（带 ``Origin``），以及浏览器发起的写请求（POST / PUT / PATCH / DELETE 带
   ``Origin``，或 ``Sec-Fetch-Site`` 是 cross-site / same-site），Origin 的主机与端口必须等于请求的 ``Host``
   （或反代给的 ``X-Forwarded-Host``）。``Host`` 没写端口时按**这次请求实际的 scheme** 取缺省端口
   （``X-Forwarded-Proto`` 优先，其次连接本身是不是 TLS），Origin 没写端口时按它自己的 scheme 取 ——
   这样同一台机器 80 端口上的别的页面冒充不了 443 上的 MAST。**不设主机白名单**：LAN IP、Tailscale IP、
   主机名、MagicDNS 名、localhost，只要页面是从同一个地址打开的就放行。开发态另外放行 Vite 的源。
   回环模式（不设密码）也校验：否则操作员浏览器里随便哪个网页都能对 127.0.0.1 发 WS 握手、或者
   用一个表单 POST 按下没有请求体的 ``/api/admin/shutdown``。``/api/ext/`` 不在这里查写请求 —— 外部面
   子应用有自己更严的 ``browser_refusal``。
3. 设了凭据（LAN 模式）：Basic 头，或 Basic 成功后签发的会话 cookie。cookie 是 HttpOnly、SameSite=Strict、
   TLS 下 Secure（明文 http 上不加，否则浏览器丢掉它），不带 Max-Age / Domain（会话级、只认这个主机），
   HMAC 签名带过期（12 h，过半续签）；签名密钥每个进程一份（重启即作废，浏览器下一次带 Basic 的请求
   会重签）。只签给像浏览器的请求（带 ``Sec-Fetch-*`` / ``Origin`` / 要 HTML）—— MCP 插件、curl 这类
   程序化客户端拿到的响应和以前一模一样。缺凭据的 http 一律 401 + ``WWW-Authenticate``（浏览器会拿
   缓存的 Basic 重试），不是 403。
4. 不带 ``Origin`` / ``Sec-Fetch-*`` 的程序化客户端不受规则 2 影响；GET 不受规则 2 影响。

不防 DNS 重绑定：重绑定的页面 Origin 与 Host 是同一个陌生主机名，规则 2 认它同源；LAN 模式下它拿不到
凭据（Basic 缓存和 cookie 都按主机名隔离），回环模式下它和 HTTP 接口本来就一样敞着（设计稿已记）。
"""

from __future__ import annotations

from mast.i18n import tr
import base64
import binascii
import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: 这个前缀下的 WS 不过操作员认证（卫星令牌由端点自己验）。带尾斜杠：别把 /ws/satellitex 也放过去。
SATELLITE_WS_PREFIX = "/ws/satellite/"
#: 外部面子应用自己做浏览器请求的拒绝（``mast/api/ext/app.py::browser_refusal``），这里不重复查写请求。
EXT_PREFIX = "/api/ext/"
#: 会话 cookie 的寿命。过了一半，下一次带 Basic 的浏览器请求续签。
SESSION_TTL_S = 12 * 3600

_REALM = 'Basic realm="MAST"'
_DEFAULT_PORT = {"http": 80, "https": 443}
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _split_host(value: str) -> tuple[str, int | None] | None:
    """``192.0.2.5:7862`` → ("192.0.2.5", 7862)；``[::1]:7862`` → ("::1", 7862)；``name`` → ("name", None)。"""
    v = value.strip().lower()
    if v.startswith("["):
        end = v.find("]")
        if end < 0:
            return None
        host, rest = v[1:end], v[end + 1:]
        if not rest:
            return (host, None) if host else None
        if not rest.startswith(":"):
            return None
        port = rest[1:]
    elif v.count(":") == 1:
        host, port = v.split(":")
    else:
        return (v, None) if v and ":" not in v else None
    if not host or not port.isdigit():
        return None
    return host, int(port)


def _origin_parts(origin: str) -> tuple[str, str, int] | None:
    """``https://Host:7862`` → ("https", "host", 7862)。不像浏览器的 Origin（null、扩展、带路径的）→ None。"""
    try:
        u = urlsplit(origin.strip())
        scheme, host, port = u.scheme.lower(), (u.hostname or "").lower(), u.port
    except ValueError:
        return None
    if scheme not in _DEFAULT_PORT or not host:
        return None
    if u.path not in ("", "/") or u.query or u.fragment or u.username or u.password:
        return None
    return scheme, host, port if port is not None else _DEFAULT_PORT[scheme]


def _request_scheme(scope: Scope, headers: Headers) -> str:
    """这次请求在浏览器那一侧的 scheme（http / https）：反代给的 X-Forwarded-Proto 优先，其次连接本身。"""
    proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto in ("https", "wss"):
        return "https"
    if proto in ("http", "ws"):
        return "http"
    return "https" if scope.get("scheme") in ("https", "wss") else "http"


def _same_origin(origin: tuple[str, str, int], host_value: str, default_port: int) -> bool:
    hp = _split_host(host_value)
    if hp is None:
        return False
    host, port = hp
    return host == origin[1] and (port if port is not None else default_port) == origin[2]


def _browserish(headers: Headers) -> bool:
    if "origin" in headers:
        return True
    if any(k.startswith("sec-fetch-") for k in headers.keys()):
        return True
    return "text/html" in (headers.get("accept") or "")


def _cookie_values(headers: Headers, name: str) -> list[str]:
    out: list[str] = []
    for raw in headers.getlist("cookie"):
        for part in raw.split(";"):
            k, sep, v = part.strip().partition("=")
            if sep and k == name:
                out.append(v.strip().strip('"'))
    return out


def _app_path(scope: Scope) -> str:
    path, root = scope.get("path") or "", scope.get("root_path") or ""
    return path[len(root):] if root and path.startswith(root) else path


async def _deny_ws(scope: Scope, send: Send, status: int, text: str) -> None:
    """拒绝 WS 握手。服务器支持 Denial Response 扩展就回真正的 HTTP 状态码，否则 close-before-accept
    （服务器回 403）。浏览器的 WebSocket API 看不到状态码，它只对 curl / 日志有用。"""
    if "websocket.http.response" in (scope.get("extensions") or {}):
        headers = [(b"content-type", b"text/plain; charset=utf-8")]
        if status == 401:
            headers.append((b"www-authenticate", _REALM.encode()))
        await send({"type": "websocket.http.response.start", "status": status, "headers": headers})
        await send({"type": "websocket.http.response.body", "body": text.encode()})
    else:
        await send({"type": "websocket.close", "code": 1008})


class AccessGate:
    """``app.add_middleware(AccessGate, credentials=(user, pwd) | None, dev_origins=[...])``。"""

    def __init__(self, app: ASGIApp, *, credentials: tuple[str, str] | None = None,
                 dev_origins: Iterable[str] = (), session_ttl_s: float = SESSION_TTL_S,
                 clock: Callable[[], float] = time.time) -> None:
        self.app = app
        # 整串比较 "user:password"（和旧中间件一样）：用户名里有冒号也不会拆错。
        self._pair = (f"{credentials[0]}:{credentials[1]}".encode("utf-8")
                      if credentials is not None else None)
        self._key = secrets.token_bytes(32)
        self._ttl = max(1, int(session_ttl_s))
        self._clock = clock
        # 开发态额外认的源：精确的 "http://host:port"，或 "http://host:*"（该主机任意端口）。
        self._dev: set[str] = set()
        self._dev_any_port: set[tuple[str, str]] = set()
        for raw in dev_origins:
            o = raw.strip().lower().rstrip("/")
            if o.endswith(":*"):
                parts = _origin_parts(o[:-2])
                if parts is not None:
                    self._dev_any_port.add(parts[:2])
            elif o:
                self._dev.add(o)
        self._noted: set[tuple[str, str, str]] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "websocket":
            await self._websocket(scope, receive, send)
        elif kind == "http":
            await self._http(scope, receive, send)
        else:                                  # lifespan
            await self.app(scope, receive, send)

    # —— 同源 ——
    def origin_allowed(self, origin: str, headers: Headers, scope: Scope) -> bool:
        if origin.strip().lower().rstrip("/") in self._dev:
            return True
        o = _origin_parts(origin)
        if o is None:
            return False
        if o[:2] in self._dev_any_port:
            return True
        # 信任 X-Forwarded-Host / -Proto 的前提：网页给 WebSocket 握手加不了自定义头；给 fetch 加则会触发
        # CORS 预检，而生产不批任何跨源预检（dev_cors 也只批 Vite 的源）。哪天放宽 CORS，这里要重新审。
        default_port = _DEFAULT_PORT[_request_scheme(scope, headers)]
        candidates = [headers.get("host") or ""]
        for fwd in headers.getlist("x-forwarded-host"):
            candidates.extend(p.strip() for p in fwd.split(","))
        return any(_same_origin(o, c, default_port) for c in candidates if c)

    def _write_refusal(self, scope: Scope, headers: Headers) -> str | None:
        """浏览器发起的跨站写请求（CSRF 的形状）→ 拒绝理由；否则 None。"""
        if scope.get("method") not in _WRITE_METHODS or _app_path(scope).startswith(EXT_PREFIX):
            return None
        origin = headers.get("origin")
        if origin is not None:
            return None if self.origin_allowed(origin, headers, scope) else f"Origin: {origin[:120]}"
        site = (headers.get("sec-fetch-site") or "").strip().lower()
        if site in ("cross-site", "same-site"):
            return f"Sec-Fetch-Site: {site}"
        return None

    def _note(self, what: str, origin: str, host: str) -> None:
        key = (what, origin[:200], host[:200])
        if key in self._noted:
            return
        if len(self._noted) < 256:
            self._noted.add(key)
        logger.warning("拒绝跨站%s：Origin=%r Host=%r（浏览器页面与服务不同源）",
                       what, origin[:200], host[:200])

    # —— websocket ——
    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        if _app_path(scope).startswith(SATELLITE_WS_PREFIX):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is not None and not self.origin_allowed(origin, headers, scope):
            self._note(" WebSocket 握手", origin, headers.get("host") or "")
            await _deny_ws(scope, send, 403, "cross-origin WebSocket refused")
            return
        if self._pair is not None and not (self._basic_ok(headers)
                                           or self._session_left(scope, headers) is not None):
            await _deny_ws(scope, send, 401, "Unauthorized")
            return
        await self.app(scope, receive, send)

    # —— http ——
    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = Headers(scope=scope)
        why = self._write_refusal(scope, headers)
        if why is not None:
            self._note("写请求", headers.get("origin") or headers.get("sec-fetch-site") or "",
                       headers.get("host") or "")
            await JSONResponse({"detail": tr(f"跨站写请求被拒（{why}）", f"Cross-site write request refused ({why})")},
                               status_code=403)(scope, receive, send)
            return
        if self._pair is None:                 # 回环模式：不设密码
            await self.app(scope, receive, send)
            return
        basic = self._basic_ok(headers)
        if not basic and self._session_left(scope, headers) is None:
            await PlainTextResponse("Unauthorized", status_code=401,
                                    headers={"WWW-Authenticate": _REALM})(scope, receive, send)
            return
        if not basic or not _browserish(headers):
            await self.app(scope, receive, send)
            return
        left = self._session_left(scope, headers)
        if left is not None and left > self._ttl / 2:
            await self.app(scope, receive, send)
            return
        cookie = self._set_cookie(scope, headers)

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message,
                           "headers": [*message.get("headers", []), (b"set-cookie", cookie)]}
            await send(message)

        await self.app(scope, receive, send_with_cookie)

    # —— 凭据 ——
    def _basic_ok(self, headers: Headers) -> bool:
        raw = headers.get("authorization")
        if not raw or self._pair is None:
            return False
        scheme, _, param = raw.strip().partition(" ")
        if scheme.lower() != "basic" or not param.strip():
            return False
        try:
            decoded = base64.b64decode(param.strip(), validate=True)
        except (binascii.Error, ValueError):   # 坏 base64、非 ASCII：401，不是 500
            return False
        return hmac.compare_digest(decoded, self._pair)

    @staticmethod
    def _cookie_name(scope: Scope) -> str:
        # 带上端口：cookie 不按端口隔离，同一台机器上两个 MAST（7862 / 7870）不能互相顶掉。
        server = scope.get("server") or ()
        port = server[1] if len(server) > 1 else None
        return f"mast_session_{port}" if port else "mast_session"

    def _sign(self, body: str) -> str:
        mac = hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")

    def _session_left(self, scope: Scope, headers: Headers) -> float | None:
        """有效会话 cookie 还剩多少秒；没有、签名不对、过期 → None。"""
        best: float | None = None
        now = self._clock()
        for token in _cookie_values(headers, self._cookie_name(scope)):
            parts = token.split(".")
            if len(parts) != 4 or parts[0] != "v1" or not parts[1].isdigit():
                continue
            body = ".".join(parts[:3])
            try:
                good = hmac.compare_digest(parts[3].encode("ascii"), self._sign(body).encode("ascii"))
            except (UnicodeEncodeError, ValueError):
                continue
            left = int(parts[1]) - now
            if good and left > 0 and (best is None or left > best):
                best = left
        return best

    def _set_cookie(self, scope: Scope, headers: Headers) -> bytes:
        exp = int(self._clock()) + self._ttl
        body = f"v1.{exp}.{secrets.token_urlsafe(9)}"
        # 不写 Max-Age / Expires：会话 cookie，关掉浏览器就没了 —— 和以前只靠 Basic 缓存时一样，
        # 换了 cookie 不该让登录在浏览器重启后还活着。寿命由签名里的过期时刻在服务端限住。
        attrs = [f"{self._cookie_name(scope)}={body}.{self._sign(body)}", "Path=/",
                 "HttpOnly", "SameSite=Strict"]
        if _request_scheme(scope, headers) == "https":
            attrs.append("Secure")
        return "; ".join(attrs).encode("latin-1")


__all__ = ["AccessGate", "EXT_PREFIX", "SATELLITE_WS_PREFIX", "SESSION_TTL_S"]
