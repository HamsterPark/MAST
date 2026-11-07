"""GZip，但不压已经压过的媒体（图像响应不走 GZip）。

Starlette 的 GZipMiddleware 只跳过 ``text/event-stream`` 和已经带 Content-Encoding 的响应。JPEG / PNG
本来就是压缩格式，再 gzip 一遍体积几乎不变，却要多花 CPU、多一次缓冲 —— 对光相机的画面按几帧每秒
轮询，这份开销每一帧都要付。这里在响应头到达时按 Content-Type 判：``image/*``、``video/*``、
``audio/*`` 和几种压缩包原样放行，其余照旧交给 Starlette 的实现。

依赖 Starlette 1.0 的 ``IdentityResponder.send_with_compression`` 在 ``http.response.start`` 时记下
``content_type_is_excluded``、到 ``http.response.body`` 时才看它；升级 Starlette 时
``tests/v2/unit/api/test_app_gzip.py`` 会把行为钉住。
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware, GZipResponder
from starlette.types import Message, Receive, Scope, Send

#: 这些 Content-Type 前缀原样放行（大小写不敏感）。
PRECOMPRESSED_TYPES: tuple[str, ...] = (
    "image/", "video/", "audio/", "application/zip", "application/gzip", "application/x-7z",
    "font/woff2",
)


def is_precompressed(content_type: str) -> bool:
    return content_type.strip().lower().startswith(PRECOMPRESSED_TYPES)


class _MediaAwareGZipResponder(GZipResponder):
    async def send_with_compression(self, message: Message) -> None:
        await super().send_with_compression(message)
        if message["type"] == "http.response.start":
            ct = Headers(raw=message.get("headers") or []).get("content-type", "")
            if is_precompressed(ct):
                self.content_type_is_excluded = True


class MediaAwareGZipMiddleware(GZipMiddleware):
    """与 :class:`GZipMiddleware` 同样的参数；只是 :data:`PRECOMPRESSED_TYPES` 不压。"""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and "gzip" in Headers(scope=scope).get("Accept-Encoding", ""):
            responder = _MediaAwareGZipResponder(self.app, self.minimum_size,
                                                 compresslevel=self.compresslevel)
            await responder(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


__all__ = ["MediaAwareGZipMiddleware", "PRECOMPRESSED_TYPES", "is_precompressed"]
