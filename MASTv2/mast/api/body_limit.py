"""Refuse a request body whose length is not declared, or too large, *before it is read*.

FastAPI reads the whole body of a request (``await request.body()``) before it resolves a route's
dependencies, so a size check in a ``Depends`` runs after the body is in memory: it keeps a huge
body from being validated, not from being buffered, and a ``Transfer-Encoding: chunked`` body has no
``Content-Length`` for it to look at at all. The check belongs in front of the route handler: a route
class built by :func:`sized_route_class` runs :func:`length_refusal` on every request that may carry a
body and answers before FastAPI has asked the server for a single byte of it.

The rule is that of the remote-AI MCP server (``mast_mcp/http_rpc.py``): a write must say how long it
is (411), the number must be a number (400), and it must be at most the limit (413).
``Transfer-Encoding`` is refused with the 411 too: with both headers the chunked framing wins, so a
small ``Content-Length`` would not bound the body.
"""
from __future__ import annotations

from typing import Any, Callable, Coroutine, Mapping, Optional

from fastapi import Request, Response
from fastapi.routing import APIRoute

from mast.i18n import tr

#: methods whose request may carry a body
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def length_refusal(headers: Mapping[str, str], limit: int) -> Optional[tuple[int, str, str]]:
    """``(status, error code, detail)`` when the body of a request with these headers must not be
    read, else ``None``. 411 ``length_required`` (no ``Content-Length``, or a ``Transfer-Encoding``),
    400 ``bad_content_length`` (not a plain number), 413 ``request_too_large`` (above *limit*)."""
    if headers.get("transfer-encoding") is not None:
        return (411, "length_required",
                tr("请求体必须带 Content-Length，不接受分块传输（Transfer-Encoding）",
                   "The request body must carry a Content-Length; Transfer-Encoding (chunked) is not accepted"))
    raw = headers.get("content-length")
    if raw is None:
        return (411, "length_required",
                tr("请求体必须带 Content-Length（不接受分块上传）", "The request body must carry a Content-Length (chunked uploads are not accepted)"))
    raw = raw.strip()
    if not (raw.isascii() and raw.isdigit()):
        return (400, "bad_content_length", tr("Content-Length 不是一个数字", "Content-Length is not a number"))
    if len(raw) > 18 or int(raw) > limit:           # (a longer string is too large; int() would also refuse it)
        return (413, "request_too_large",
                tr(f"请求体太大（上限 {limit} 字节）", f"The request body is too large (limit {limit} bytes)"))
    return None


def sized_route_class(limit: int, refuse: Callable[[int, str, str], Exception]) -> type[APIRoute]:
    """A route class whose handler first applies :func:`length_refusal` (limit *limit*) to a request
    that may carry a body and raises ``refuse(status, code, detail)`` instead of reading it."""

    class SizedBodyRoute(APIRoute):
        def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            inner = super().get_route_handler()

            async def handler(request: Request) -> Response:
                if request.method in BODY_METHODS:
                    refusal = length_refusal(request.headers, limit)
                    if refusal is not None:
                        raise refuse(*refusal)
                return await inner(request)

            return handler

    return SizedBodyRoute


__all__ = ["BODY_METHODS", "length_refusal", "sized_route_class"]
