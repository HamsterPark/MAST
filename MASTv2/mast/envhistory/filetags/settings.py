# -*- coding: utf-8 -*-
"""环境标签的两个设置：开关（默认开）与新鲜度阈值（默认 30 s，5–600）。

存在图库状态目录的 ``env_tags.json``（``{"version": 1, "enabled": …, "max_age_s": …}``）。界面在
图库「数据根与构建」页的「环境标签」一节，经 ``GET/POST /api/gallery/env-tags`` 读写 —— 操作员眼里
这是图库的功能。**不放进图库的 ``config.json``**：那份由「保存设置」整条重写，重写时没带这两个键
就会把它们悄悄重置成默认（设计 §11-10）。文件不存在、读不了或字段不对 ⇒ 按默认；读的路径不创建
任何东西。

带 TTL 缓存（5 s），**按文件路径分开缓存**：测试每条都把 ``MAST_GALLERY_DIR`` 指到自己的 tmp，
按路径分开才不会把上一条测试的设置漏给下一条。``current()`` 只在工作线程和图库构建里调用 ——
钩子一侧只看记录器上缓存的那个布尔值（读这份 JSON 也是 I/O，状态目录可能在网络盘上）。
"""
from __future__ import annotations

import math
import threading
import time
from pathlib import Path

DEFAULT_ENABLED = True
DEFAULT_MAX_AGE_S = 30.0
MIN_MAX_AGE_S = 5.0
MAX_MAX_AGE_S = 600.0
TTL_S = 5.0
FILE_NAME = "env_tags.json"
FILE_VERSION = 1

_lock = threading.Lock()
_cache: dict[str, tuple[float, tuple[bool, float]]] = {}


def clamp_max_age(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_S
    if not math.isfinite(f):
        return DEFAULT_MAX_AGE_S
    return min(MAX_MAX_AGE_S, max(MIN_MAX_AGE_S, f))


def settings_path(lay=None) -> Path:
    """图库状态目录里的 ``env_tags.json``。只算路径，不碰磁盘。"""
    from mast.gallery import paths as gpaths

    lay = lay or gpaths.layout()
    return Path(lay.state) / FILE_NAME


def _load_file(path: Path) -> tuple[bool, float]:
    from mast.gallery import paths as gpaths

    raw = gpaths.read_json(path, None)
    if not isinstance(raw, dict):
        return DEFAULT_ENABLED, DEFAULT_MAX_AGE_S
    enabled = raw.get("enabled", DEFAULT_ENABLED)
    return (enabled if isinstance(enabled, bool) else DEFAULT_ENABLED,
            clamp_max_age(raw.get("max_age_s", DEFAULT_MAX_AGE_S)))


def load(lay=None) -> tuple[bool, float]:
    """``(开关, 阈值秒)``，现读、不缓存。不存在、坏了、字段不对 ⇒ 默认值。不创建任何东西。"""
    return _load_file(settings_path(lay))


def save(enabled: bool, max_age_s, lay=None) -> tuple[bool, float]:
    """原子写 ``env_tags.json`` 并清缓存；返回实际存下的值（阈值夹到 5–600）。写失败就抛。"""
    from mast.gallery import paths as gpaths

    lay = lay or gpaths.layout()
    val = (bool(enabled), clamp_max_age(max_age_s))
    gpaths.ensure_state_dir(lay)
    gpaths.atomic_write_json(settings_path(lay),
                             {"version": FILE_VERSION, "enabled": val[0], "max_age_s": val[1]},
                             indent=1)
    reset_cache()
    return val


def current() -> tuple[bool, float]:
    """``(开关, 新鲜度阈值秒)``，带 5 s 缓存。永不抛；读不了按默认。"""
    try:
        path = settings_path()
    except Exception:  # noqa: BLE001
        return DEFAULT_ENABLED, DEFAULT_MAX_AGE_S
    key = str(path)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    try:
        val = _load_file(path)
    except Exception:  # noqa: BLE001
        val = (DEFAULT_ENABLED, DEFAULT_MAX_AGE_S)
    with _lock:
        _cache[key] = (now + TTL_S, val)
    return val


def reset_cache() -> None:
    with _lock:
        _cache.clear()


__all__ = ["DEFAULT_ENABLED", "DEFAULT_MAX_AGE_S", "MIN_MAX_AGE_S", "MAX_MAX_AGE_S", "FILE_NAME",
           "clamp_max_age", "current", "load", "reset_cache", "save", "settings_path"]
