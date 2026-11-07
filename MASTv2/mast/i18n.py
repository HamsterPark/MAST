"""Language of the text MAST returns to people: English (default) or Simplified Chinese.

Every user-visible backend string is written inline as a pair, ``tr("中文", "English")``,
next to where it is used — the same convention as the frontend's ``src/lib/i18n.ts``.
There is no shared dictionary: a pair cannot drift away from its call site.

Which language ``tr()`` picks, in order:

1. the language of the HTTP request being served — the ``mast_lang`` cookie the web
   UI sets on every page load, or an ``X-MAST-Lang`` header from other clients
   (:class:`LanguageMiddleware` puts it in a context variable, so it follows the
   request into thread-pool handlers and streaming generators);
2. :func:`use_lang` around a block of code;
3. the process default: ``MAST_LANG`` (``en`` / ``zh``), else English.

Text built once at import time (module-level constants) is evaluated in the process
default. Where such text reaches a person, build it at the point of use instead —
wrap it in a function, or keep the pair and call ``tr`` when rendering.

LLM prompts are not user-facing text and are not translated through this module.
"""
from __future__ import annotations

import contextlib
import os
from contextvars import ContextVar
from typing import Iterator, Literal, Optional

Lang = Literal["en", "zh"]

DEFAULT_LANG: Lang = "en"
ENV_VAR = "MAST_LANG"
COOKIE_NAME = "mast_lang"
HEADER_NAME = "x-mast-lang"

_request_lang: ContextVar[Optional[str]] = ContextVar("mast_request_lang", default=None)


def normalize(value: object) -> Optional[Lang]:
    """``"zh-CN"`` → ``"zh"``, ``"EN"`` → ``"en"``; anything else → ``None``."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v.startswith("zh"):
        return "zh"
    if v.startswith("en"):
        return "en"
    return None


def process_lang() -> Lang:
    """The language used outside a request (background threads, CLI, startup)."""
    return normalize(os.environ.get(ENV_VAR)) or DEFAULT_LANG


def get_lang() -> Lang:
    return normalize(_request_lang.get()) or process_lang()


def tr(zh: str, en: str) -> str:
    """Pick the string for the active language."""
    return zh if get_lang() == "zh" else en


def is_zh() -> bool:
    return get_lang() == "zh"


@contextlib.contextmanager
def use_lang(lang: Optional[str]) -> Iterator[None]:
    """Run a block in a given language (``None`` / unknown = leave it unchanged)."""
    token = _request_lang.set(normalize(lang) or _request_lang.get())
    try:
        yield
    finally:
        _request_lang.reset(token)


def _lang_from_scope(scope) -> Optional[Lang]:
    cookie_lang: Optional[Lang] = None
    header_lang: Optional[Lang] = None
    for name, value in scope.get("headers") or ():
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                key, _, val = part.strip().partition("=")
                if key == COOKIE_NAME:
                    cookie_lang = normalize(val) or cookie_lang
        elif name == HEADER_NAME.encode():
            header_lang = normalize(value.decode("latin-1"))
    # An explicit header from a client beats the browser cookie.
    return header_lang or cookie_lang


class LanguageMiddleware:
    """Pure ASGI middleware: sets the request language for ``tr()``."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = _request_lang.set(_lang_from_scope(scope))
        try:
            await self.app(scope, receive, send)
        finally:
            _request_lang.reset(token)
