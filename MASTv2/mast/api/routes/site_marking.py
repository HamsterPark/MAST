"""位点标注页的本地端点：服务填好的页面，并保管页面与 agent 共写的「标点文档」。

设计稿 ``docs/v2/design/id23_site_marking_20261026.md``。页面（``skills/builtins/site_marking_template.html``
填好后写成 ``page.html``）由 MAST 自己的端口服务；数据通道不走 Artifact 运行时，而是 ``fetch`` 下面的
``/doc`` 端点 —— 页面里的 shim 把原来的 ``claude.use("db")``（collection / doc / onSnapshot）接到它们上：
GET 轮询当快照，POST 发补丁。

* ``GET  /api/site-marking/{page_id}/page`` —— ``page.html``（``text/html``，不缓存，只许同源嵌入）。
* ``GET  /api/site-marking/{page_id}/doc`` —— 整份文档::

      {"sites": {"<id>": {...}}, "plan/marking": null, "plan/card": null,
       "plan/confirm": null, "status/live": null}

  页面目录在、marks.json 还没有 → 上面这份空文档；页面目录不在 → 404（把写错的 page_id 读成「还没人标」，
  agent 会一直等下去）；marks.json 读不了或内容坏了 → 500，**绝不折成空文档**：下一次写会拿空文档把
  操作员的全部标记覆盖掉。
* ``POST /api/site-marking/{page_id}/doc`` —— 应用**一条**补丁，读-改-写是原子的，成功回 ``{"ok": true}``::

      {"target": "sites", "id": ID, "op": "set",    "data": {...}}   整个替换（新建也用它）
      {"target": "sites", "id": ID, "op": "update", "data": {...}}   浅合并；null 写成 null；站位不在 → 404
      {"target": "sites", "id": ID, "op": "delete"}                  删除；本来就不在也算成功（重试不报错）
      {"target": "doc", "path": P, "op": "set", "data": {...} | null}
          P ∈ plan/marking · plan/card · plan/confirm · status/live；``data: null`` 清空

  ``update`` 不会替不存在的站位建一行：用户刚删掉的站位，agent 随后回写的 status 不能把它复活成一行
  没有坐标的残骸。未知的 target / op / path、多出来的字段、缺 ``data``、``data`` 类型不对、
  NaN / Infinity、id 为空或超长 → 400。站位里的字段（x / y / kind ……）这里**不**校验，那是读它的 skill 的事。

落盘：``<project_root>/artifacts/site_marking/<page_id>/{page.html, marks.json}``，``project_root`` 即
``mast._runtime_paths.project_root()``（和 diagnostics / mosaics / maps 等产物同一约定，**没有** ``MASTv2/`` 这一段；
页面、skill 与端点共用这一条路径）。页面目录由 PrepareSiteMarkingPage 在写 ``page.html``（和
一份空的 ``marks.json``）时建，它的路径必须和 :func:`page_dir` 一致（写页面的一方可以直接用
``page_dir(page_id, create=True)``）；本模块的端点从不凭 URL 凭空建目录，所以写错的 page_id 是 404，而不是悄悄
多出一个没人读的目录。

写请求的闸门（:func:`_write_refusal`）照 ``mast.api.ext.app.browser_refusal`` 的判据，但**不 import 它**
（有的构建整块不带 ``mast.api.ext``）：必须 ``Content-Type: application/json``（415）；带 ``Origin`` /
``Sec-Fetch-*`` 的就是浏览器发的，Host 要是回环名或 IP 字面量（陌生主机名＝DNS 重绑定的形状）、
``Sec-Fetch-Site`` 只认 same-origin / none、``Origin`` 必须就是本服务（403）。程序化客户端（MCP、脚本、
curl）不带这些头，不受影响；体必填（空体 → 400）。这一页上有「确认任务卡，开始测量」，所以也不许被别的站点
用 iframe 盖住诱导点击（``frame-ancestors 'self'``）。写端点前面还有主 app 的访问闸门（同源 + LAN 认证），
这里是第二道。

并发：每个页面一把进程内锁，读-改-写与 GET 读都在锁里，写走先写临时文件再 ``os.replace``（Windows 上
「目标正被读」的短暂占用有重试，见 ``mast.gallery.paths.atomic_write_bytes``）。**进程内的写入方（skill）
必须走 :func:`apply_patch` / :func:`read_doc`，不要自己打开 marks.json 写** —— 那把锁只在这里，绕过它，
页面的一次拖动和 agent 的一次回写就会互相覆盖。只读的一方直接读文件不会读到半截（写是整体替换），但 Windows 上
撞上替换的那一瞬可能短暂打不开，重试一次即可；要省心就用 :func:`read_doc`。锁是进程内的：两个 MAST 进程同时写
同一页不受保护（不是预期用法）。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from mast._runtime_paths import project_root
from mast.gallery.paths import atomic_write_bytes, tmp_path_for
from mast.i18n import tr

logger = logging.getLogger(__name__)

# 不进主 OpenAPI：页面是 MAST 自己服务的独立 HTML，前端 SPA 不调这几个端点；留在 schema 里就要
# 重新生成 frontend/openapi.json 与 schema.d.ts（AGENTS.md），却没有任何类型使用者。
router = APIRouter(tags=["site-marking"], include_in_schema=False)

__all__ = [
    "DOC_PATHS", "MARKS_NAME", "PAGE_NAME", "SiteMarkingError",
    "apply_patch", "empty_doc", "page_dir", "read_doc", "router",
]

#: 页面目录下的两个文件：PrepareSiteMarkingPage 写 page.html，本模块写 marks.json。
PAGE_NAME = "page.html"
MARKS_NAME = "marks.json"

#: 四个「计划 / 状态」文档的路径（页面与 agent 约定的固定名字）。
DOC_PATHS = ("plan/marking", "plan/card", "plan/confirm", "status/live")

_SITE_OPS = ("set", "update", "delete")
_PAGE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: Windows 设备名：``con`` / ``nul`` / ``com1`` 这类纯字母数字名当目录名会落到设备上，而不是目录。
_WIN_RESERVED = frozenset({"con", "prn", "aux", "nul",
                           *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})
_MAX_ID_CHARS = 128
_MAX_BODY_BYTES = 1 << 20      # 一条补丁
_MAX_DOC_BYTES = 8 << 20       # 整份 marks.json

_NO_STORE = {"Cache-Control": "no-store"}
_PAGE_HEADERS = {
    **_NO_STORE,
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "frame-ancestors 'self'",
    "X-Frame-Options": "SAMEORIGIN",
}


class SiteMarkingError(Exception):
    """带 HTTP 状态码的领域错误：路由原样映射成 ``HTTPException``，进程内调用方（skill）直接捕获。

    400 page_id / 补丁不合法　404 页面或站位不存在　413 太大　500 marks.json 坏了或读不了
    503 marks.json 被别的进程占着（稍后重试）。
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _bad(zh: str, en: str) -> SiteMarkingError:
    return SiteMarkingError(400, tr(zh, en))


def _http(exc: SiteMarkingError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


def _show(value: Any) -> str:
    """把一个来自请求的值放进错误信息：截短，别把一整个 1 MiB 的体回显回去。"""
    s = repr(value)
    return s if len(s) <= 60 else s[:57] + "..."


# ── 位置与锁 ───────────────────────────────────────────────────────────


def _check_page_id(page_id: str) -> None:
    if (not isinstance(page_id, str) or _PAGE_ID_RE.fullmatch(page_id) is None
            or page_id.lower() in _WIN_RESERVED):
        raise _bad("page_id 只能由字母、数字、短横线和下划线组成（最长 64 个字符）",
                   "page_id may only contain letters, digits, hyphens and underscores (at most 64 characters)")


def page_dir(page_id: str, *, create: bool = False) -> Path:
    """``<project_root>/artifacts/site_marking/<page_id>``（页面、skill 与端点共用的这一条路径）。

    ``create=False``（端点用）目录不在 → 404；``create=True`` 只给写页面的那一方（PrepareSiteMarkingPage）。
    每次调用都重新解析根目录：测试靠 ``MAST2_PROJECT_ROOT`` 把它指到 tmp。
    """
    _check_page_id(page_id)
    d = project_root() / "artifacts" / "site_marking" / page_id
    if create:
        d.mkdir(parents=True, exist_ok=True)
    elif not d.is_dir():
        raise SiteMarkingError(404, tr(f"没有这个标注页：{page_id}", f"No such marking page: {page_id}"))
    return d


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(page_id: str) -> threading.Lock:
    # NTFS 不分大小写：Abc 与 abc 是同一个目录，必须是同一把锁。
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(page_id.lower(), threading.Lock())


def _io_error(exc: OSError, path: Path) -> SiteMarkingError:
    logger.warning("位点标注：访问 %s 失败：%s", path, exc)
    if isinstance(exc, PermissionError):    # Windows 上被编辑器 / 杀软占着：稍后就好
        return SiteMarkingError(503, tr(f"{path.name} 暂时被别的程序占用，请稍后再试",
                                        f"{path.name} is locked by another program; try again shortly"))
    return SiteMarkingError(500, tr(f"无法访问 {path.name}：{exc.strerror or exc}",
                                    f"Cannot access {path.name}: {exc.strerror or exc}"))


# ── 文档的读与写 ───────────────────────────────────────────────────────


def empty_doc() -> dict:
    return {"sites": {}, **{p: None for p in DOC_PATHS}}


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def _finite_float(text: str) -> float:
    # 1e999 解析得出 inf，却写不出合法 JSON（浏览器的 JSON.parse 读不了）；读和写都在入口处挡掉。
    value = float(text)
    if value in (float("inf"), float("-inf")):
        raise ValueError(f"non-finite number {text}")
    return value


def _loads(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)


def _corrupt(path: Path, why: str) -> SiteMarkingError:
    logger.warning("位点标注：%s 内容不对（%s），拒绝读写，原文件原样保留", path, why)
    rel = f"artifacts/site_marking/{path.parent.name}/{path.name}"
    return SiteMarkingError(500, tr(
        f"{rel} 不是一份合法的标点文档（{why}）。为免覆盖里面的标记，已拒绝读写；请人工检查，或把它移走后重试。",
        f"{rel} is not a valid marks document ({why}). Refusing to read or write it so the marks inside "
        "are not overwritten; check it by hand, or move it away and retry."))


def _load(path: Path) -> dict:
    """marks.json → 补齐了标准键的文档（多出来的键原样保留）。

    文件不在、或只有空白 → 空文档（里面没有任何可丢的东西）。读不了、不是 JSON、不是对象、``sites`` 不是对象
    → 抛错，**不**回空文档：写路径上那等于拿一份空文档覆盖掉操作员标好的全部位点。
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return empty_doc()
    except OSError as exc:
        raise _io_error(exc, path) from exc
    if not raw.strip():
        return empty_doc()
    try:
        loaded = _loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _corrupt(path, tr("不是合法的 JSON", "not valid JSON")) from exc
    if not isinstance(loaded, dict):
        raise _corrupt(path, tr("顶层不是对象", "the top level is not an object"))
    if loaded.get("sites") is not None and not isinstance(loaded["sites"], dict):
        raise _corrupt(path, tr("sites 不是对象", "'sites' is not an object"))
    doc = empty_doc()
    doc.update(loaded)
    if doc["sites"] is None:
        doc["sites"] = {}
    return doc


def _store(path: Path, doc: dict) -> None:
    try:
        data = json.dumps(doc, ensure_ascii=False, indent=1, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        # TypeError 是进程内调用方塞了存不成 JSON 的对象；HTTP 来的体已经是 JSON 原生的，
        # 能走到这里的是 1e999 这种解析得出 inf、却写不出合法 JSON 的数。
        raise _bad("补丁里有存不成 JSON 的值（NaN、Infinity、超大数或不可序列化的对象）",
                   "The patch contains a value that cannot be stored as JSON (NaN, Infinity, "
                   "an overflowing number, or a non-serialisable object)") from exc
    if len(data) > _MAX_DOC_BYTES:
        raise SiteMarkingError(413, tr(f"标点文档会超过 {_MAX_DOC_BYTES >> 20} MiB 的上限，这条补丁没有应用",
                                       f"The marks document would exceed the {_MAX_DOC_BYTES >> 20} MiB limit; "
                                       "the patch was not applied"))
    try:
        atomic_write_bytes(path, data)
    except OSError as exc:
        try:
            tmp_path_for(path).unlink()
        except OSError:
            pass
        raise _io_error(exc, path) from exc


def read_doc(page_id: str) -> dict:
    """整份文档（带锁读）。页面目录不在 → 404；marks.json 还没有 → 空文档。"""
    d = page_dir(page_id)               # 先验目录：没见过的 page_id 不登记锁，GET 随便什么 id 都不该让注册表变大
    with _lock_for(page_id):
        return _load(d / MARKS_NAME)


# ── 补丁 ───────────────────────────────────────────────────────────────


class _Patch(NamedTuple):
    target: str     # "sites" | "doc"
    op: str
    key: str        # 站位 id，或文档路径
    data: Any


def _parse_patch(patch: Any) -> _Patch:
    if not isinstance(patch, dict):
        raise _bad("补丁必须是一个 JSON 对象", "The patch must be a JSON object")
    target, op = patch.get("target"), patch.get("op")
    if target == "sites":
        allowed = {"target", "id", "op", "data"}
    elif target == "doc":
        allowed = {"target", "path", "op", "data"}
    else:
        raise _bad(f"未知的 target：{_show(target)}（只有 sites 和 doc）",
                   f"Unknown target: {_show(target)} (only 'sites' and 'doc')")
    # 多出来的字段一律拒绝，而不是悄悄忽略：agent 习惯带的 if_version 之类若被吞掉，
    # 它会以为自己拿到了并发保护。
    extra = sorted(str(k) for k in patch if k not in allowed)
    if extra:
        raise _bad(f"{target} 补丁里有不认识的字段：{_show(extra)}",
                   f"Unknown field(s) in a '{target}' patch: {_show(extra)}")

    if target == "doc":
        if op != "set":
            raise _bad(f"文档只支持 op=set，收到 {_show(op)}", f"Documents only support op='set', got {_show(op)}")
        path = patch.get("path")
        if path not in DOC_PATHS:
            raise _bad(f"未知的文档路径：{_show(path)}（只有 {', '.join(DOC_PATHS)}）",
                       f"Unknown document path: {_show(path)} (only {', '.join(DOC_PATHS)})")
        if "data" not in patch:
            raise _bad("缺少 data（清空文档请传 data: null）", "'data' is required (send data: null to clear)")
        data = patch["data"]
        if data is not None and not isinstance(data, dict):
            raise _bad("data 必须是 JSON 对象或 null", "'data' must be a JSON object or null")
        return _Patch("doc", "set", path, data)

    if op not in _SITE_OPS:
        raise _bad(f"站位只支持 op={'/'.join(_SITE_OPS)}，收到 {_show(op)}",
                   f"Sites support op={'/'.join(_SITE_OPS)}, got {_show(op)}")
    sid = patch.get("id")
    if (not isinstance(sid, str) or not sid.strip() or len(sid) > _MAX_ID_CHARS
            or any(ord(c) < 32 for c in sid)):
        raise _bad(f"站位 id 必须是 1–{_MAX_ID_CHARS} 个字符的非空字符串，不含控制字符，收到 {_show(sid)}",
                   f"A site id must be a non-empty string of 1-{_MAX_ID_CHARS} characters without "
                   f"control characters, got {_show(sid)}")
    if op == "delete":
        return _Patch("sites", "delete", sid, None)
    data = patch.get("data")
    if not isinstance(data, dict):
        raise _bad(f"op={op} 需要 data（一个 JSON 对象）", f"op={op} needs 'data' (a JSON object)")
    return _Patch("sites", op, sid, data)


def _apply(doc: dict, p: _Patch) -> None:
    if p.target == "doc":
        doc[p.key] = p.data
        return
    sites = doc["sites"]
    if p.op == "set":
        sites[p.key] = p.data
    elif p.op == "update":
        cur = sites.get(p.key)
        if cur is None:
            raise SiteMarkingError(404, tr(f"没有这个站位：{p.key}（新建请用 op=set）",
                                           f"No such site: {p.key} (use op='set' to create one)"))
        if not isinstance(cur, dict):
            raise SiteMarkingError(500, tr(f"站位 {p.key} 在 marks.json 里不是对象，无法合并",
                                           f"Site {p.key} is not an object in marks.json; cannot merge into it"))
        cur.update(p.data)
    else:                                   # delete —— 幂等：不在也算成功
        sites.pop(p.key, None)


def apply_patch(page_id: str, patch: Any) -> None:
    """对 ``page_id`` 的文档应用一条补丁（格式见模块说明）。整个读-改-写在页面锁里，原子落盘；
    marks.json 不在就在写的那一刻建，页面目录不在则 404（不凭 page_id 建目录）。

    不合法的补丁、不存在的页面 / 站位、存不下的值都抛 :class:`SiteMarkingError`（带 HTTP 状态码），
    且文件**一个字节都不动**。
    """
    p = _parse_patch(patch)
    path = page_dir(page_id) / MARKS_NAME
    with _lock_for(page_id):
        doc = _load(path)
        _apply(doc, p)
        _store(path, doc)


# ── 写请求的闸门（照 mast.api.ext.app.browser_refusal；有意不 import 它） ──────

_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _host_part(netloc: str) -> str:
    """``[::1]:7862`` → ``::1``；``127.0.0.1:7862`` → ``127.0.0.1``；``rig`` → ``rig``。"""
    n = (netloc or "").strip().lower()
    if n.startswith("["):
        return n[1:n.find("]")] if "]" in n else n
    return n.rsplit(":", 1)[0] if n.count(":") == 1 else n


def _is_ip_or_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _write_refusal(request: Request) -> tuple[int, str] | None:
    """返回 ``(状态码, 说明)`` 或 ``None``（放行）。"""
    h = request.headers
    ctype = (h.get("content-type") or "").split(";", 1)[0].strip().lower()
    if not (ctype == "application/json" or (ctype.startswith("application/") and ctype.endswith("+json"))):
        # 不依赖某个版本 FastAPI 的 strict_content_type：没有 Content-Type 的体也被当 JSON 解析，
        # 正是绕开 CORS 预检的办法（no-cors 的空 / text/plain POST 是「简单请求」）。
        return 415, tr("写请求必须带 Content-Type: application/json 与一个 JSON 请求体",
                       "Write requests must carry Content-Type: application/json and a JSON body")
    if not ("origin" in h or any(k.lower().startswith("sec-fetch-") for k in h.keys())):
        return None                         # 程序化客户端（MCP、脚本、curl）不带这些头
    host = h.get("host") or ""
    if not _is_ip_or_loopback(_host_part(host)):
        return 403, tr("浏览器经主机名写入被拒（DNS 重绑定的形状）；请用 IP 地址或 127.0.0.1 打开这个页面",
                       "A browser write by host name was refused (the shape of DNS rebinding); "
                       "open this page by IP address or 127.0.0.1")
    site = (h.get("sec-fetch-site") or "").strip().lower()
    if site and site not in ("same-origin", "none"):
        return 403, tr(f"跨站写请求被拒（Sec-Fetch-Site: {site[:40]}）",
                       f"Cross-site write refused (Sec-Fetch-Site: {site[:40]})")
    origin = h.get("origin")
    if origin is not None:
        try:
            o = urlsplit(origin.strip())
        except ValueError:
            o = None
        if o is None or o.scheme != request.url.scheme or o.netloc.lower() != host.strip().lower():
            return 403, tr(f"跨站写请求被拒（Origin: {origin[:120]}）", f"Cross-site write refused (Origin: {origin[:120]})")
    return None


async def _read_body(request: Request) -> bytes:
    """读请求体，超过上限就停（不把一个巨大的体整个读进内存）。"""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _MAX_BODY_BYTES:
        raise SiteMarkingError(413, tr(f"请求体超过 {_MAX_BODY_BYTES >> 10} KiB 的上限",
                                       f"The request body exceeds the {_MAX_BODY_BYTES >> 10} KiB limit"))
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            raise SiteMarkingError(413, tr(f"请求体超过 {_MAX_BODY_BYTES >> 10} KiB 的上限",
                                           f"The request body exceeds the {_MAX_BODY_BYTES >> 10} KiB limit"))
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_body(raw: bytes) -> Any:
    if not raw.strip():
        raise _bad("请求体必填：发一条 JSON 补丁", "A request body is required: send one JSON patch")
    try:
        return _loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _bad(f"请求体不是合法的 JSON：{exc}", f"The request body is not valid JSON: {exc}") from None


# ── 端点 ───────────────────────────────────────────────────────────────


@router.get("/site-marking/{page_id}/page", response_class=HTMLResponse)
def get_page(page_id: str) -> HTMLResponse:
    """页面 HTML。页面目录或 page.html 不在 → 404。"""
    try:
        d = page_dir(page_id)
    except SiteMarkingError as exc:
        raise _http(exc) from None
    path = d / PAGE_NAME
    try:
        body = path.read_bytes()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=tr("这个标注页还没有生成页面",
                                                       "This marking page has no page.html yet")) from None
    except OSError as exc:
        raise _http(_io_error(exc, path)) from None
    return HTMLResponse(content=body, headers=_PAGE_HEADERS)


@router.get("/site-marking/{page_id}/doc")
def get_doc(page_id: str) -> JSONResponse:
    """整份标点文档；页面每 1.2 s 轮询一次。"""
    try:
        doc = read_doc(page_id)
    except SiteMarkingError as exc:
        raise _http(exc) from None
    return JSONResponse(content=doc, headers=_NO_STORE)


@router.post("/site-marking/{page_id}/doc")
async def post_doc(page_id: str, request: Request) -> dict:
    """应用一条补丁。体必填、要 JSON、浏览器请求验同源（见模块说明）。"""
    refusal = _write_refusal(request)
    if refusal is not None:
        status, detail = refusal
        logger.warning("位点标注：拒绝写入 %s（HTTP %s）origin=%r host=%r sec-fetch-site=%r", request.url.path, status,
                       request.headers.get("origin"), request.headers.get("host"),
                       request.headers.get("sec-fetch-site"))
        raise HTTPException(status_code=status, detail=detail)
    try:
        # 先验 page_id（和页面目录在不在）再读体：错的 id 不值得为它读一个 1 MiB 的请求。
        page_dir(page_id)
        patch = _parse_body(await _read_body(request))
        await run_in_threadpool(apply_patch, page_id, patch)
    except SiteMarkingError as exc:
        raise _http(exc) from None
    return {"ok": True}
