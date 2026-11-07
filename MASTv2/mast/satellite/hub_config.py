"""枢纽侧卫星配置 ``<数据根>/config/satellites.json``（契约 §5.5）。

**只用标准库**：管理 CLI（``admin.py``）要能在没有完整 MAST 依赖的 Python 里单独运行，
所以这个模块不 import pydantic、numpy 或 mast 的其他子包。

配置里只存令牌的 SHA-256；令牌本身只在 ``new-token`` 时打印一次。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any

if __package__:
    from mast.i18n import tr
else:  # admin.py run as a plain script: no ``mast`` package, so keep the same pair helper locally
    def tr(zh: str, en: str) -> str:
        return zh if os.environ.get("MAST_LANG", "").strip().lower().startswith("zh") else en

CONFIG_REL = Path("config") / "satellites.json"

#: 新建条目时的默认值（与契约 §5.5 的示例一致）。
ENTRY_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "token_sha256": "",
    "note": "",
    "heartbeat_s": 1.0,
    "deadman_s": 5.0,
    "max_upload_bytes_per_s": 1 << 20,
    "devices": {},
}

#: 卫星 id 会进 URL 路径（``/api/satellites/{id}``）和磁盘路径（``satellite/<id>/``），
#: 所以只允许保守字符；这几个字面量与 REST 的字面量路由同名，不许当 id。
SAT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
RESERVED_IDS = frozenset({"files", "port-guard", "health", "hub", "events", "devices"})

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class HubConfigError(ValueError):
    pass


def validate_sat_id(sat_id: str) -> str:
    s = str(sat_id or "").strip()
    if not SAT_ID_RE.match(s) or s.lower() in RESERVED_IDS:
        raise HubConfigError(tr(
            f"卫星 id {sat_id!r} 不合法：只允许字母数字开头的 1–32 个 [A-Za-z0-9_.-]，"
            f"且不能是保留字 {sorted(RESERVED_IDS)}",
            f"Invalid satellite id {sat_id!r}: only 1-32 characters from [A-Za-z0-9_.-], starting "
            f"with a letter or digit, are allowed, and it must not be a reserved word "
            f"{sorted(RESERVED_IDS)}"))
    return s


def data_root(explicit: str | os.PathLike | None = None) -> Path:
    """数据根：显式参数 > ``MAST2_PROJECT_ROOT`` / ``MAST_PROJECT_ROOT`` > ``project_root()``。"""
    if explicit:
        return Path(explicit)
    for var in ("MAST2_PROJECT_ROOT", "MAST_PROJECT_ROOT"):
        v = os.environ.get(var, "").strip()
        if v:
            return Path(v)
    try:
        from mast._runtime_paths import project_root

        return Path(project_root())
    except Exception:  # noqa: BLE001 — 脚本模式下没有 mast 包
        return Path.cwd()


def config_path(root: str | os.PathLike | None = None) -> Path:
    return data_root(root) / CONFIG_REL


def token_sha256(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def new_token() -> str:
    """256 位随机令牌（URL 安全 base64，43 个字符）。"""
    return secrets.token_urlsafe(32)


_DUMMY_HASH = token_sha256("mast-satellite-dummy-token")


def verify_token(entry: dict | None, token: str | None) -> bool:
    """常数时间比较 ``sha256(token)`` 与配置里的哈希。

    条目不存在或没配哈希时也照样做一次比较再返回 False —— 不让响应时间泄露
    「这个 id 存在与否」。
    """
    expected = str((entry or {}).get("token_sha256") or "").strip().lower()
    got = token_sha256(token or "")
    if not _HEX64.match(expected):
        hmac.compare_digest(got, _DUMMY_HASH)
        return False
    return hmac.compare_digest(got, expected) and bool(token)


def empty_config() -> dict:
    return {"satellites": []}


def load(path: str | os.PathLike) -> dict:
    """读配置。文件不存在 → 空配置；内容坏了 → :class:`HubConfigError`（调用方决定怎么降级）。"""
    p = Path(path)
    if not p.exists():
        return empty_config()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HubConfigError(tr(f"读不了 {p}：{exc}", f"Cannot read {p}: {exc}")) from exc
    if not isinstance(raw, dict):
        raise HubConfigError(tr(f"{p} 顶层不是对象", f"{p}: the top level is not an object"))
    sats = raw.get("satellites")
    if sats is None:
        raw["satellites"] = []
    elif not isinstance(sats, list):
        raise HubConfigError(tr(f"{p} 的 satellites 不是数组",
                                f"satellites in {p} is not an array"))
    return raw


def save(path: str | os.PathLike, data: dict) -> None:
    """原子写（同目录临时文件 + ``os.replace``）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def find(data: dict, sat_id: str) -> dict | None:
    for e in data.get("satellites") or []:
        if isinstance(e, dict) and str(e.get("id", "")) == sat_id:
            return e
    return None


def normalized_entry(entry: dict) -> dict:
    """补齐默认值、规整类型。不认识的字段原样保留。"""
    out = dict(ENTRY_DEFAULTS)
    out.update({k: v for k, v in (entry or {}).items()})
    out["id"] = str(out.get("id", ""))
    out["enabled"] = bool(out.get("enabled", True))
    out["token_sha256"] = str(out.get("token_sha256") or "").strip().lower()
    out["note"] = str(out.get("note") or "")
    for key, lo, hi in (("heartbeat_s", 0.05, 60.0), ("deadman_s", 0.5, 600.0)):
        try:
            v = float(out.get(key))
        except (TypeError, ValueError):
            v = float(ENTRY_DEFAULTS[key])
        out[key] = min(hi, max(lo, v))
    try:
        out["max_upload_bytes_per_s"] = max(1024, int(out.get("max_upload_bytes_per_s")))
    except (TypeError, ValueError):
        out["max_upload_bytes_per_s"] = int(ENTRY_DEFAULTS["max_upload_bytes_per_s"])
    if not isinstance(out.get("devices"), dict):
        out["devices"] = {}
    return out


def set_token(data: dict, sat_id: str, token: str, *, note: str | None = None) -> dict:
    """新建或更新一个条目的令牌哈希。返回该条目。"""
    sat_id = validate_sat_id(sat_id)
    entry = find(data, sat_id)
    if entry is None:
        entry = {"id": sat_id, **{k: (dict(v) if isinstance(v, dict) else v)
                                  for k, v in ENTRY_DEFAULTS.items()}}
        data.setdefault("satellites", []).append(entry)
    entry["token_sha256"] = token_sha256(token)
    if note is not None:
        entry["note"] = note
    return entry


__all__ = [
    "CONFIG_REL", "ENTRY_DEFAULTS", "SAT_ID_RE", "RESERVED_IDS", "HubConfigError",
    "validate_sat_id", "data_root", "config_path", "token_sha256", "new_token",
    "verify_token", "empty_config", "load", "save", "find", "normalized_entry", "set_token",
]
