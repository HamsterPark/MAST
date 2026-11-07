"""WebSocket 也要过认证 —— TERS 契约 §12.3。

修之前：LAN 模式的 Basic 认证是 ``BaseHTTPMiddleware``，它对 websocket scope 直接放行；``/ws/*`` 先
``accept()`` 后不认证。于是局域网里任何人不带密码就能连 ``/ws/events`` 看实时读数、连 ``/ws/voice``
往仪器 agent 里送话。修之后是一个纯 ASGI 中间件（``mast.api.access.AccessGate``），http 与 websocket
都过它：

* LAN 模式：Basic 头，或 Basic 成功后签发的会话 cookie（HttpOnly、SameSite=Strict、TLS 下 Secure、
  签名带过期）；
* 浏览器发起的 WS（带 Origin）必须与 Host（或反代给的 X-Forwarded-Host）同源 —— 回环模式也一样，
  否则操作员浏览器里任何一个网页都能连上本机的 ``/ws/voice``；
* ``/ws/satellite/`` 整段放行，由卫星端点自己验令牌；
* 不带 Origin 的程序化客户端（MCP 插件、外部 API，都带 Basic 头）行为不变。

从仓库根运行::

    .venv-v2-py313/Scripts/python.exe -m pytest tests/v2/unit/api/test_ws_auth.py -q
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.modules.setdefault("nanonis_spm", MagicMock())

_MASTV2_ROOT = str(Path(__file__).resolve().parents[4] / "MASTv2")
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
from starlette.testclient import TestClient, WebSocketDenialResponse  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

USER, PWD = "op", "s3cret-pw"


def _basic(user: str = USER, pwd: str = PWD) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()}


_BROWSER = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none",
            "Accept": "text/html,application/xhtml+xml"}


def _refused(client: TestClient, path: str, headers: dict | None = None) -> int:
    """连一下；被拒返回 HTTP 状态码（close-before-accept 记作 403），连上了返回 0。"""
    try:
        with client.websocket_connect(path, headers=headers or {}):
            return 0
    except WebSocketDenialResponse as resp:
        return resp.status_code
    except WebSocketDisconnect:
        return 403


@pytest.fixture(scope="module")
def lan_app():
    from mast.api.app import create_app

    return create_app(auth=(USER, PWD), dev_cors=False)


@pytest.fixture()
def lan(lan_app) -> TestClient:
    return TestClient(lan_app)


@pytest.fixture(scope="module")
def loop_app():
    from mast.api.app import create_app

    return create_app(dev_cors=False)


# ════════════════════════════════════════════════════════════════════════════
# 1 · LAN 模式下不带凭据的 WS 必须被拒
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("path", ["/ws/events", "/ws/buffer", "/ws/live", "/ws/voice"])
def test_lan_mode_refuses_an_unauthenticated_websocket(lan, path):
    assert _refused(lan, path) == 401, f"{path} 不带凭据就连上了"


def test_lan_mode_refuses_a_wrong_password_on_websocket(lan):
    assert _refused(lan, "/ws/events", _basic(pwd="wrong")) == 401


@pytest.mark.parametrize("path", ["/ws/events", "/ws/buffer", "/ws/live"])
def test_lan_mode_basic_header_opens_the_websocket(lan, path):
    """MCP 插件 / 脚本：带 Basic、不带 Origin —— 行为不变。"""
    assert _refused(lan, path, _basic()) == 0


def test_a_browser_websocket_from_another_site_is_refused_even_with_credentials(lan):
    """浏览器会给跨站的 WS 握手自动带上缓存的 Basic 凭据 —— 这正是跨站 WS 劫持的形状。"""
    hdr = {**_basic(), "Origin": "https://evil.example"}
    assert _refused(lan, "/ws/events", hdr) == 403


def test_loopback_mode_still_refuses_cross_site_browser_websockets(loop_app):
    """回环模式不设密码，但操作员浏览器里的任何网页都能对 127.0.0.1 发 WS 握手；
    ``/ws/voice`` 能往仪器 agent 里送话。Origin 校验在回环模式下也必须生效。"""
    # TestClient.websocket_connect 不看 base_url（永远是 ws://testserver），Host 要显式给。
    c = TestClient(loop_app, base_url="http://127.0.0.1:7862")
    here = {"Host": "127.0.0.1:7862"}
    for path in ("/ws/voice", "/ws/events"):
        assert _refused(c, path, {**here, "Origin": "https://evil.example"}) == 403, path
    assert _refused(c, "/ws/events", {**here, "Origin": "http://127.0.0.1:7862"}) == 0
    assert _refused(c, "/ws/events", here) == 0, "回环模式下不带 Origin 的本机客户端不该受影响"
    assert c.get("/api/health").status_code == 200


# ════════════════════════════════════════════════════════════════════════════
# 2 · 会话 cookie：浏览器的 WS 握手靠它认证
# ════════════════════════════════════════════════════════════════════════════

def _session_cookie(resp) -> str:
    raw = [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"]
    assert raw, "Basic 登录成功的浏览器请求没有拿到会话 cookie"
    assert len(raw) == 1, raw
    return raw[0]


def test_browser_basic_login_issues_a_hardened_session_cookie(lan):
    r = lan.get("/api/health", headers={**_basic(), **_BROWSER})
    assert r.status_code == 200
    cookie = _session_cookie(r)
    attrs = [a.strip().lower() for a in cookie.split(";")]
    assert "httponly" in attrs and "samesite=strict" in attrs and "path=/" in attrs
    assert not any(a.startswith(("max-age=", "expires=")) for a in attrs), (
        "持久 cookie 会让登录在浏览器重启后还活着 —— 以前只靠 Basic 缓存时不会")
    assert "secure" not in attrs, "明文 http 上加 Secure 浏览器会直接丢掉这个 cookie"


def test_session_cookie_is_secure_over_tls(lan_app):
    c = TestClient(lan_app, base_url="https://testserver")
    cookie = _session_cookie(c.get("/api/health", headers={**_basic(), **_BROWSER}))
    assert "secure" in [a.strip().lower() for a in cookie.split(";")]


def test_the_session_cookie_alone_opens_websockets_and_http(lan_app):
    c = TestClient(lan_app)
    pair = _session_cookie(c.get("/", headers={**_basic(), **_BROWSER})).split(";", 1)[0]
    fresh = TestClient(lan_app)               # 空 cookie 罐：只带我们显式给的 Cookie 头
    hdr = {"Cookie": pair, "Origin": "http://testserver"}
    assert _refused(fresh, "/ws/live", hdr) == 0
    assert _refused(fresh, "/ws/events", hdr) == 0
    assert fresh.get("/api/health", headers={"Cookie": pair}).status_code == 200


def test_a_tampered_session_cookie_is_refused(lan_app):
    c = TestClient(lan_app)
    pair = _session_cookie(c.get("/", headers={**_basic(), **_BROWSER})).split(";", 1)[0]
    name, value = pair.split("=", 1)
    forged = f"{name}={value[:-2]}{'AA' if not value.endswith('AA') else 'BB'}"
    fresh = TestClient(lan_app)
    assert _refused(fresh, "/ws/events", {"Cookie": forged}) == 401
    assert fresh.get("/api/health", headers={"Cookie": forged}).status_code == 401


def test_a_cookie_from_another_service_process_is_refused(lan_app):
    """签名密钥每个进程一份：重启后旧 cookie 作废（浏览器下一次带 Basic 的请求会重签）。"""
    from mast.api.app import create_app

    other = create_app(auth=(USER, PWD), dev_cors=False)
    pair = _session_cookie(TestClient(other).get("/", headers={**_basic(), **_BROWSER})
                           ).split(";", 1)[0]
    assert _refused(TestClient(lan_app), "/ws/events", {"Cookie": pair}) == 401


def test_a_programmatic_basic_client_is_not_handed_a_cookie(lan):
    """MCP 插件、curl：只带 Basic、不带浏览器头 —— 响应和以前一样，不多一个 Set-Cookie。"""
    r = lan.get("/api/health", headers=_basic())
    assert r.status_code == 200
    assert "set-cookie" not in {k.lower() for k in r.headers.keys()}


# ════════════════════════════════════════════════════════════════════════════
# 3 · Origin 规则：服务被访问到的任何地址都要认
# ════════════════════════════════════════════════════════════════════════════

TS_NAME = "rig-pc.tail0000.ts.net"      # 形状像 MagicDNS 名，不是哪台真机器


# TestClient.websocket_connect 不看 base_url：要模拟「wss://…」的握手就写全 URL（scheme 决定了
# Host 头省略端口时的缺省端口）。浏览器里页面是 https 的，WS 就是 wss。
@pytest.mark.parametrize("ws, origin, host", [
    ("wss", "https://10.0.0.5:7862", "10.0.0.5:7862"),                   # LAN IP
    ("wss", "https://100.64.1.2:7862", "100.64.1.2:7862"),               # Tailscale IP
    ("wss", f"https://{TS_NAME}:7862", f"{TS_NAME}:7862"),               # MagicDNS 名
    ("wss", f"https://{TS_NAME}", TS_NAME),                              # 两边都省略 443
    ("wss", f"https://{TS_NAME}", f"{TS_NAME}:443"),
    ("ws", "http://rig-pc:7862", "rig-pc:7862"),                         # 主机名
    ("ws", "http://localhost:7862", "localhost:7862"),
    ("ws", "http://127.0.0.1:7862", "127.0.0.1:7862"),
    ("ws", "http://[::1]:7862", "[::1]:7862"),
    ("wss", "HTTPS://RIG-PC:7862", "rig-pc:7862"),                       # 大小写不敏感
    ("ws", "https://10.0.0.5:7862", "10.0.0.5:7862"),                    # 显式端口：scheme 不影响判定
])
def test_a_same_origin_browser_websocket_is_allowed_on_every_address(lan, ws, origin, host):
    hdr = {**_basic(), "Origin": origin, "Host": host}
    assert _refused(lan, f"{ws}://testserver/ws/events", hdr) == 0


@pytest.mark.parametrize("ws, origin, host", [
    ("wss", "null", "10.0.0.5:7862"),                                    # file:// / 沙箱 iframe
    ("wss", "http://10.0.0.5:8080", "10.0.0.5:7862"),                    # 同机别的端口
    ("wss", "http://10.0.0.5", "10.0.0.5:7862"),                         # 同机 80 端口
    ("wss", f"https://{TS_NAME}", f"{TS_NAME}:7862"),
    # 同一个主机名、80 端口上的别的页面，冒充 443 上（Host 省略端口）的 MAST：
    ("wss", f"http://{TS_NAME}", TS_NAME),
    ("wss", "https://10.0.0.5.evil.example:7862", "10.0.0.5:7862"),
    ("wss", "chrome-extension://abcdef", "10.0.0.5:7862"),
])
def test_a_cross_origin_browser_websocket_is_refused(lan, ws, origin, host):
    hdr = {**_basic(), "Origin": origin, "Host": host}
    assert _refused(lan, f"{ws}://testserver/ws/events", hdr) == 403


def test_origin_may_match_the_forwarded_host_of_a_reverse_proxy(lan):
    """tailscale serve / nginx 把 Host 改成上游地址时，浏览器看到的地址在 X-Forwarded-Host 里，
    浏览器那一侧的 scheme 在 X-Forwarded-Proto 里。浏览器的 WebSocket API 设不了这两个头，所以网页
    没法拿它们冒充同源。"""
    hdr = {**_basic(), "Origin": f"https://{TS_NAME}", "Host": "127.0.0.1:7862",
           "X-Forwarded-Host": TS_NAME, "X-Forwarded-Proto": "https"}
    assert _refused(lan, "/ws/events", hdr) == 0
    assert _refused(lan, "/ws/events", {**hdr, "X-Forwarded-Proto": "http"}) == 403, (
        "代理说浏览器那侧是 http（缺省 80），https 的 Origin（443）不该算同源")
    assert _refused(lan, "/ws/events", {**hdr, "X-Forwarded-Host": "somewhere-else.example"}) == 403
    # Host 原样保留的代理（tailscale serve 就是这样）只给 X-Forwarded-Proto 也行
    assert _refused(lan, "/ws/events", {**_basic(), "Origin": f"https://{TS_NAME}",
                                        "Host": TS_NAME, "X-Forwarded-Proto": "https"}) == 0


def test_dev_server_origins_are_allowed_only_in_dev_mode(lan):
    from mast.api.app import create_app

    via_vite = {"Origin": "http://localhost:5173", "Host": "127.0.0.1:7870"}
    dev = TestClient(create_app(dev_cors=True))
    assert _refused(dev, "/ws/events", via_vite) == 0
    assert _refused(dev, "/ws/events", {**via_vite, "Origin": "http://127.0.0.1:4173"}) == 0  # preview
    # 5173 被占时 Vite 自动换下一个端口（vite.config.ts 没开 strictPort）
    assert _refused(dev, "/ws/events", {**via_vite, "Origin": "http://localhost:5174"}) == 0
    assert _refused(dev, "/ws/events", {**via_vite, "Origin": "http://[::1]:5173"}) == 0
    assert _refused(dev, "/ws/events", {**via_vite, "Origin": "https://evil.example"}) == 403
    assert _refused(dev, "/ws/events", {**via_vite, "Origin": "http://192.0.2.7:5173"}) == 403, (
        "开发态也只认本机回环名")
    assert _refused(lan, "/ws/events", {**_basic(), **via_vite}) == 403


# ════════════════════════════════════════════════════════════════════════════
# 4 · HTTP 侧：行为不变
# ════════════════════════════════════════════════════════════════════════════

def test_http_without_credentials_gets_the_basic_challenge(lan):
    r = lan.get("/api/health")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == 'Basic realm="MAST"'


def test_the_mounted_external_gateway_is_still_behind_the_login(lan):
    assert lan.get("/api/ext/v1/health").status_code == 401
    r = lan.get("/api/ext/v1/health", headers=_basic())
    assert r.status_code != 401 and r.headers.get("x-mast-ext-version"), r.status_code


@pytest.mark.parametrize("value", [
    "Basic !!!not-base64!!!",
    "Basic " + base64.b64encode(b"\xff\xfe:\x00").decode(),
    "Basic " + base64.b64encode(f"{USER}{PWD}".encode()).decode(),   # 没有冒号
    "Bearer " + PWD,
    "Basic",
])
def test_malformed_authorization_is_401_not_500(lan, value):
    assert lan.get("/api/health", headers={"Authorization": value}).status_code == 401
    assert _refused(lan, "/ws/events", {"Authorization": value}) == 401


def test_a_non_ascii_authorization_header_is_401_not_500(lan_app):
    """旧中间件拿 str 做 compare_digest：头里有非 ASCII 字符就 TypeError → 500。"""
    c = TestClient(lan_app)
    raw = {"Authorization": "Basic été".encode("latin-1")}
    assert c.get("/api/health", headers=raw).status_code == 401
    assert _refused(c, "/ws/events", raw) == 401


def test_a_username_with_a_colon_still_logs_in():
    """旧中间件整串比较 ``Basic base64(user:password)``；新的不许在冒号上把它拆错。"""
    from mast.api.app import create_app

    c = TestClient(create_app(auth=("lab:ters", "pw"), dev_cors=False))
    assert c.get("/api/health", headers=_basic("lab:ters", "pw")).status_code == 200
    assert c.get("/api/health", headers=_basic("lab", "ters:pw")).status_code == 200  # 同一个串
    assert c.get("/api/health", headers=_basic("lab:ters", "nope")).status_code == 401


def test_non_ascii_credentials_round_trip():
    from mast.api.app import create_app

    c = TestClient(create_app(auth=("操作员", "密码-ä"), dev_cors=False))
    hdr = {"Authorization": "Basic " + base64.b64encode("操作员:密码-ä".encode()).decode()}
    assert c.get("/api/health", headers=hdr).status_code == 200


def test_lifespan_still_runs_through_the_gate(lan_app):
    with TestClient(lan_app) as c:            # 进出 with 会走 lifespan startup / shutdown
        assert c.get("/api/health", headers=_basic()).status_code == 200


# ════════════════════════════════════════════════════════════════════════════
# 4b · 跨站写请求（CSRF 的形状）
# ════════════════════════════════════════════════════════════════════════════

_FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture()
def shutdown_tripwire(monkeypatch):
    """万一闸门没拦住、请求真走进了 /api/admin/shutdown：令牌对不上它就原样拒绝；就算走到了
    ``_arm_exit_backstop``（20 s 后 ``os._exit(0)``，会把 pytest 静默地以 0 退出），也只记一笔。"""
    from mast.api.routes import admin

    calls: list[int] = []
    monkeypatch.setenv("MAST2_SHUTDOWN_TOKEN", "test-only-" + base64.b64encode(b"x" * 9).decode())
    monkeypatch.setattr(admin, "_arm_exit_backstop", lambda: calls.append(1))
    yield calls
    assert not calls, "关停处理函数真的跑了 —— 闸门没拦住跨站写请求"


def test_a_cross_site_form_post_cannot_press_shutdown_in_loopback_mode(loop_app, shutdown_tripwire):
    """回环模式不设密码：操作员浏览器里任何网页提交一个表单到 /api/admin/shutdown（没有请求体、
    简单请求、浏览器不做预检）就能让服务自关 —— 浏览器在这种 POST 上会带 Origin。"""
    c = TestClient(loop_app, base_url="http://127.0.0.1:7862")
    r = c.post("/api/admin/shutdown", headers={**_FORM, "Origin": "https://evil.example"})
    assert r.status_code == 403 and "跨站" in r.json()["detail"]


def test_a_cross_site_write_is_refused_even_with_cached_basic(lan, shutdown_tripwire):
    """顶层表单提交会带上浏览器缓存的 Basic 凭据，认证拦不住它，只能靠同源。"""
    r = lan.post("/api/admin/shutdown", headers={**_basic(), **_FORM,
                                                 "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_the_launchers_shutdown_request_still_passes_the_gate(lan, shutdown_tripwire):
    """启动器的优雅关停：Basic + application/json + ``{}``，不带 Origin /
    Sec-Fetch-*。它必须穿过闸门到达处理函数 —— 这里处理函数自己拒绝（TestClient 不是回环对端、令牌也不对），
    但 200 + ``ok: false`` 说明闸门没拦它；被闸门拦下会是 401 / 403。"""
    r = lan.post("/api/admin/shutdown", json={}, headers=_basic())
    assert r.status_code == 200 and r.json()["ok"] is False, (r.status_code, r.text[:200])


def test_the_mounted_external_gateway_keeps_its_own_browser_refusal(loop_app):
    """/api/ext/ 不归闸门管写请求：外部面的 browser_refusal 更严，而且回的是它自己的 JSON 错误体。"""
    r = TestClient(loop_app, base_url="http://127.0.0.1:7862").post(
        "/api/ext/v1/notes", json={"content": "x"},
        headers={"Origin": "https://evil.example", "Host": "127.0.0.1:7862"})
    assert r.status_code == 403 and r.json()["error"] == "cross_origin_refused"


# ════════════════════════════════════════════════════════════════════════════
# 5 · 中间件本体（最小 app）：卫星路径、过期、钟
# ════════════════════════════════════════════════════════════════════════════

def _mini(**kw):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Mount, Route, WebSocketRoute

    from mast.api.access import AccessGate

    async def ping(request):
        return PlainTextResponse("pong")

    async def ws_ok(websocket):
        await websocket.accept()
        await websocket.send_text(websocket.url.path)
        await websocket.close()

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]
    ext = Starlette(routes=[Route("/thing", ping, methods=methods)])
    app = Starlette(routes=[Route("/api/ping", ping, methods=methods),
                            WebSocketRoute("/ws/thing", ws_ok),
                            WebSocketRoute("/ws/satellite/v1", ws_ok),
                            Mount("/api/ext/v1", app=ext)])
    app.add_middleware(AccessGate, **kw)
    return app


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("headers, refused", [
    ({"Origin": "https://evil.example"}, True),
    ({"Origin": "http://testserver"}, False),                     # 同源（SPA 自己）
    ({"Origin": "null"}, True),
    ({"Sec-Fetch-Site": "cross-site"}, True),                     # 没带 Origin 的跨站
    ({"Sec-Fetch-Site": "same-site"}, True),                      # 同机别的端口 = 别的源
    ({"Sec-Fetch-Site": "same-origin"}, False),
    ({"Sec-Fetch-Site": "none"}, False),                          # 地址栏 / 书签
    ({}, False),                                                  # MCP / curl / 启动器
])
def test_write_methods_must_be_same_origin_when_a_browser_sends_them(method, headers, refused):
    c = TestClient(_mini(credentials=None))
    r = c.request(method, "/api/ping", headers=headers)
    assert (r.status_code == 403) is refused, (method, headers, r.status_code)


def test_reads_are_not_subject_to_the_write_rule():
    c = TestClient(_mini(credentials=None))
    assert c.get("/api/ping", headers={"Origin": "https://evil.example",
                                       "Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_dev_origins_may_write_in_dev_mode_only():
    vite = {"Origin": "http://localhost:5173", "Host": "127.0.0.1:7870",
            "Sec-Fetch-Site": "same-origin"}     # 浏览器眼里页面和请求都在 :5173，同源
    assert TestClient(_mini(dev_origins=["http://localhost:5173"])).post(
        "/api/ping", headers=vite).status_code == 200
    assert TestClient(_mini()).post("/api/ping", headers=vite).status_code == 403
    wild = TestClient(_mini(dev_origins=["http://localhost:*"]))
    assert wild.post("/api/ping", headers={**vite, "Origin": "http://localhost:5199"}).status_code == 200
    assert wild.post("/api/ping", headers={**vite, "Origin": "http://192.0.2.7:5173"}).status_code == 403
    assert wild.post("/api/ping", headers={**vite, "Origin": "https://localhost:5173"}).status_code == 403


def test_the_ext_prefix_is_left_to_the_sub_app():
    c = TestClient(_mini(credentials=None))
    r = c.post("/api/ext/v1/thing", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200 and r.text == "pong", "闸门不该替外部面判写请求"


def test_the_write_rule_runs_before_the_login_and_logs_once(caplog):
    import logging

    c = TestClient(_mini(credentials=(USER, PWD)))
    with caplog.at_level(logging.WARNING, logger="mast.api.access"):
        for _ in range(3):
            r = c.post("/api/ping", headers={**_basic(), "Origin": "https://evil.example"})
            assert r.status_code == 403
    assert len([rec for rec in caplog.records if "跨站" in rec.getMessage()]) == 1
    assert c.post("/api/ping", headers=_basic()).status_code == 200


def test_secure_follows_the_browser_side_scheme_behind_a_tls_proxy():
    """tailscale serve / Caddy 终止 TLS 后到 MAST 是明文 http；浏览器那一侧是 https，cookie 要 Secure。
    明文 LAN（没装 cryptography 时的退路）上则绝不能加 Secure，否则浏览器丢掉它、登录死循环。"""
    app = _mini(credentials=(USER, PWD))       # 每次一个新客户端：别让上一次的 cookie 罐影响续签判断
    behind_tls = TestClient(app).get("/api/ping", headers={**_basic(), **_BROWSER,
                                                           "X-Forwarded-Proto": "https"})
    assert "secure" in _session_cookie(behind_tls).lower()
    plain = TestClient(app).get("/api/ping", headers={**_basic(), **_BROWSER})
    assert "secure" not in _session_cookie(plain).lower()


def test_the_satellite_path_bypasses_the_operator_login():
    """卫星不知道操作员密码；它的令牌由 /ws/satellite/v1 端点自己在 accept 之前验。"""
    c = TestClient(_mini(credentials=(USER, PWD)))
    with c.websocket_connect("/ws/satellite/v1") as ws:
        assert ws.receive_text() == "/ws/satellite/v1"
    # Origin 也交给端点判断（端点要求 Bearer 令牌，浏览器的 WebSocket API 设不了它）
    with c.websocket_connect("/ws/satellite/v1", headers={"Origin": "https://evil.example"}) as ws:
        assert ws.receive_text() == "/ws/satellite/v1"
    assert _refused(c, "/ws/thing") == 401
    assert _refused(c, "/ws/satellitex") == 401, "前缀要带斜杠，别把别的路径也放过去"


def test_a_session_cookie_expires():
    now = [1_800_000_000.0]
    app = _mini(credentials=(USER, PWD), session_ttl_s=60, clock=lambda: now[0])
    c = TestClient(app)
    pair = _session_cookie(c.get("/api/ping", headers={**_basic(), **_BROWSER})).split(";", 1)[0]
    fresh = TestClient(app)
    assert _refused(fresh, "/ws/thing", {"Cookie": pair}) == 0
    now[0] += 61
    assert _refused(fresh, "/ws/thing", {"Cookie": pair}) == 401
    assert fresh.get("/api/ping", headers={"Cookie": pair}).status_code == 401


def test_a_fresh_cookie_is_not_reissued_on_every_request_but_an_aging_one_is():
    now = [1_800_000_000.0]
    app = _mini(credentials=(USER, PWD), session_ttl_s=100, clock=lambda: now[0])
    c = TestClient(app)
    pair = _session_cookie(c.get("/api/ping", headers={**_basic(), **_BROWSER})).split(";", 1)[0]
    hdr = {**_basic(), **_BROWSER, "Cookie": pair}
    fresh = TestClient(app)
    r = fresh.get("/api/ping", headers=hdr)
    assert "set-cookie" not in {k.lower() for k in r.headers.keys()}, "新鲜的 cookie 不该每次都重签"
    now[0] += 60                              # 过了一半寿命：带 Basic 的下一次请求续签
    r = fresh.get("/api/ping", headers=hdr)
    assert _session_cookie(r)


def test_the_cookie_name_is_per_port_so_two_services_on_one_host_do_not_clobber():
    app = _mini(credentials=(USER, PWD))
    a = TestClient(app, base_url="http://10.0.0.5:7862")
    b = TestClient(app, base_url="http://10.0.0.5:7870")
    ca = _session_cookie(a.get("/api/ping", headers={**_basic(), **_BROWSER}))
    cb = _session_cookie(b.get("/api/ping", headers={**_basic(), **_BROWSER}))
    assert ca.split("=", 1)[0] != cb.split("=", 1)[0]


def test_no_credentials_means_http_is_open_and_only_the_origin_rule_applies():
    c = TestClient(_mini(credentials=None))
    assert c.get("/api/ping").status_code == 200
    assert "set-cookie" not in {k.lower() for k in c.get("/api/ping", headers=_BROWSER).headers}
    assert _refused(c, "/ws/thing") == 0
    assert _refused(c, "/ws/thing", {"Origin": "https://evil.example"}) == 403


def test_a_close_before_accept_is_used_when_the_server_lacks_the_denial_extension():
    """老 ASGI 服务器没有 websocket.http.response 扩展：退回 close-before-accept（服务器回 403）。"""
    import asyncio

    app = _mini(credentials=(USER, PWD))
    sent: list[dict] = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "websocket", "path": "/ws/thing", "raw_path": b"/ws/thing",
             "query_string": b"", "headers": [(b"host", b"testserver")], "scheme": "ws",
             "server": ("testserver", 80), "client": ("1.2.3.4", 5), "root_path": "",
             "subprotocols": [], "asgi": {"version": "3.0"}, "extensions": {}}
    asyncio.run(app(scope, receive, send))
    assert sent and sent[0]["type"] == "websocket.close", sent
