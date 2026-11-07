"""卫星文件的接收与归档（契约 §5.2 file_*、§11）。

落盘布局（``<数据根>`` 是安装时选的数据目录，与服务同一个）::

    <数据根>/satellite/<sat_id>/
        incoming/<file_id>.part         正在收的字节（断点续传的依据）
        incoming/<file_id>.offer.json   对应的 file_offer（枢纽重启后也能续传）
        files/<rel_path>                收完、校验过、原子改名后的文件
        files/<rel_path>.meta.json      元数据旁注
        index.jsonl                     每入库一个文件追加一行（FileArchive 的数据源）

流程：``file_offer`` → 回 ``file_want{offset}``（已有 N 字节就从 N 续传；已入库就回
``offset == size`` 外加 ``file_done``）→ ``file_chunk`` 只接受 ``offset`` 恰好等于已收字节数
的块，否则回 ``file_want`` 重新对齐 → ``file_end`` → 校验 sha256（= file_id = offer.sha256）
→ ``os.replace`` 到 ``files/`` → 写 ``.meta.json`` → 追加 index → 回 ``file_done{ok}``。
sha 不符：删掉 ``.part``，回 ``file_done{ok:false, error:"sha256_mismatch"}``，卫星从头重传。

这里的方法会阻塞（磁盘 I/O、算哈希），端点放在线程池里调用。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from mast.satellite import protocol as P

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 2 << 30            # 与 ExperimentFileStore 的单文件上限一致
MIN_FREE_BYTES = 256 << 20
MAX_REL_PATH_CHARS = 200
MAX_REL_DEPTH = 8
META_SUFFIX = ".meta.json"
META_SCHEMA = "mast.satellite.file_meta/1"

_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).isoformat(
        timespec="milliseconds")


def safe_rel_path(rel_path: str, name: str) -> tuple[PurePosixPath, bool]:
    """把卫星给的相对路径变成安全的 POSIX 相对路径。返回 ``(路径, 是否改动过)``。

    不拒收（数据比路径重要）：``..``、盘符、绝对路径、Windows 保留名、非法字符、末尾空格
    与点都会被替换或丢掉，原始值记在 ``.meta.json`` 里。
    """
    raw = str(rel_path or "").replace("\\", "/").strip()
    if not raw:
        raw = str(name or "file")
    changed = False
    parts: list[str] = []
    for seg in raw.split("/"):
        s = seg.strip()
        if s in ("", "."):
            if seg:
                changed = True
            continue
        if s == "..":
            changed = True
            continue
        t = _BAD_CHARS.sub("_", s)
        t = t.rstrip(" .") or "_"
        if _RESERVED.match(t):
            t = "_" + t
        if t != seg:
            changed = True
        parts.append(t)
    if not parts:
        parts = [_BAD_CHARS.sub("_", str(name or "file")).rstrip(" .") or "file"]
        changed = True
    if len(parts) > MAX_REL_DEPTH:
        parts = parts[-MAX_REL_DEPTH:]
        changed = True
    rel = PurePosixPath(*parts)
    if len(str(rel)) > MAX_REL_PATH_CHARS:
        last = parts[-1]
        stem, dot, ext = last.rpartition(".")
        keep = max(8, MAX_REL_PATH_CHARS - len(ext) - 2)
        rel = PurePosixPath((stem or last)[:keep] + (dot + ext if dot else ""))
        changed = True
    if rel.name.lower().endswith(META_SUFFIX):
        rel = rel.with_name(rel.name + "_")
        changed = True
    return rel, changed


# ── 已入库文件的查询 ────────────────────────────────────────────────────────

class FileArchive:
    """``hub.files()`` 返回的对象：按卫星读 ``index.jsonl``，按修改时间缓存。"""

    def __init__(self, data_root_fn: Callable[[], Path]) -> None:
        self._root_fn = data_root_fn
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[tuple[int, int], list[dict]]] = {}

    def base(self) -> Path:
        return Path(self._root_fn()) / "satellite"

    def sat_dir(self, sat_id: str) -> Path:
        return self.base() / sat_id

    def files_dir(self, sat_id: str) -> Path:
        return self.sat_dir(sat_id) / "files"

    def incoming_dir(self, sat_id: str) -> Path:
        return self.sat_dir(sat_id) / "incoming"

    def index_path(self, sat_id: str) -> Path:
        return self.sat_dir(sat_id) / "index.jsonl"

    def sat_ids(self) -> list[str]:
        b = self.base()
        try:
            return sorted(p.name for p in b.iterdir() if p.is_dir() and (p / "index.jsonl").exists())
        except OSError:
            return []

    def _entries(self, sat_id: str) -> list[dict]:
        p = self.index_path(sat_id)
        try:
            st = p.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            return []
        with self._lock:
            hit = self._cache.get(sat_id)
            if hit is not None and hit[0] == key:
                return hit[1]
            rows: list[dict] = []
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue          # 半行（崩溃时写到一半）——跳过
                        if isinstance(row, dict) and row.get("file_id"):
                            rows.append(row)
            except OSError:
                return []
            self._cache[sat_id] = (key, rows)
            return rows

    def add(self, entry: dict) -> None:
        sat_id = str(entry["sat_id"])
        p = self.index_path(sat_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._cache.pop(sat_id, None)

    def find(self, sat_id: str, file_id: str) -> dict | None:
        for row in reversed(self._entries(sat_id)):
            if row.get("file_id") == file_id:
                return row
        return None

    def lookup(self, sat_id: str, *, sha256: str | None = None,
               rel_path: str | None = None) -> dict | None:
        """按内容哈希（= file_id）或卫星侧相对路径找已入库的文件；没到就是 None。

        ``rel_path`` 按卫星给的原值比（``\\`` 与 ``/`` 视为同一个分隔符、不区分大小写 ——
        卫星是 Windows）；同一路径入库过多次时返回最新那条。返回的行里 ``stored_rel`` 是相对
        数据根的路径，``self.path(file_id)`` 给绝对路径。
        """
        if sha256:
            row = self.find(sat_id, str(sha256).strip().lower())
            return dict(row) if row else None
        if rel_path:
            want = str(rel_path).replace("\\", "/").strip("/").lower()
            for row in reversed(self._entries(sat_id)):
                got = str(row.get("rel_path") or "").replace("\\", "/").strip("/").lower()
                if got == want:
                    return dict(row)
        return None

    def list(self, sat_id: str | None = None, *, limit: int = 100,
             since_ms: int | None = None) -> list[dict]:
        """新的在前。"""
        ids = [sat_id] if sat_id else self.sat_ids()
        rows: list[dict] = []
        for sid in ids:
            rows.extend(self._entries(sid))
        if since_ms is not None:
            rows = [r for r in rows if int(r.get("received_ms") or 0) > int(since_ms)]
        rows.sort(key=lambda r: int(r.get("received_ms") or 0), reverse=True)
        return [dict(r) for r in rows[: max(1, int(limit))]]

    def get(self, file_id: str, sat_id: str | None = None) -> dict | None:
        ids = [sat_id] if sat_id else self.sat_ids()
        for sid in ids:
            row = self.find(sid, file_id)
            if row is not None:
                return dict(row)
        return None

    def path(self, file_id: str, sat_id: str | None = None) -> Path | None:
        row = self.get(file_id, sat_id)
        if row is None:
            return None
        return Path(self._root_fn()) / row["stored_rel"]

    def meta(self, file_id: str, sat_id: str | None = None) -> dict | None:
        row = self.get(file_id, sat_id)
        if row is None or not row.get("meta_rel"):
            return None
        try:
            return json.loads((Path(self._root_fn()) / row["meta_rel"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


# ── 接收 ────────────────────────────────────────────────────────────────────

@dataclass
class _Transfer:
    sat_id: str
    offer: dict
    part: Path
    offer_path: Path
    received: int = 0
    started_ms: int = field(default_factory=_now_ms)
    last_resync_offset: int = -1
    last_resync_mono: float = 0.0


class FileReceiver:
    def __init__(self, archive: FileArchive, *, context_fn: Callable[[str], dict],
                 stm_ring=None, ingest_fn: Callable[[list[Path], str], str] | None = None,
                 on_stored: Callable[[dict], None] | None = None,
                 max_file_bytes: int = MAX_FILE_BYTES, min_free_bytes: int = MIN_FREE_BYTES) -> None:
        self.archive = archive
        self._context = context_fn
        self._ring = stm_ring
        self._ingest = ingest_fn
        self._on_stored = on_stored
        self.max_file_bytes = int(max_file_bytes)
        self.min_free_bytes = int(min_free_bytes)
        self._lock = threading.RLock()
        self._transfers: dict[tuple[str, str], _Transfer] = {}
        self.stats = {"offers": 0, "stored": 0, "duplicates": 0, "sha_mismatch": 0,
                      "resumed": 0, "bytes": 0}

    # ── 消息 ──
    def offer(self, sat_id: str, offer: P.FileOffer) -> list[dict]:
        fid = offer.file_id
        self.stats["offers"] += 1
        if offer.sha256 != fid:
            return [_done(fid, False, error="file_id_must_equal_sha256")]
        existing = self.archive.find(sat_id, fid)
        if existing is not None:
            self.stats["duplicates"] += 1
            return [{"t": "file_want", "file_id": fid, "offset": int(offer.size)},
                    _done(fid, True, stored=existing.get("stored_rel"))]
        if offer.size > self.max_file_bytes:
            return [_done(fid, False, error=f"too_large (> {self.max_file_bytes} bytes)")]
        inc = self.archive.incoming_dir(sat_id)
        inc.mkdir(parents=True, exist_ok=True)
        free = _free_bytes(inc)
        if free is not None and free < offer.size * 2 + self.min_free_bytes:
            return [_done(fid, False, error="disk_low")]
        with self._lock:
            tr = self._transfers.get((sat_id, fid))
            if tr is None:
                tr = _Transfer(sat_id=sat_id, offer=offer.model_dump(mode="json"),
                               part=inc / f"{fid}.part", offer_path=inc / f"{fid}.offer.json")
                self._transfers[(sat_id, fid)] = tr
            else:
                tr.offer = offer.model_dump(mode="json")
            _write_json_atomic(tr.offer_path, tr.offer)
            tr.received = _size(tr.part)
            if tr.received > offer.size:
                tr.part.unlink(missing_ok=True)
                tr.received = 0
            if tr.received:
                self.stats["resumed"] += 1
                logger.info("卫星 %s 文件 %s 断点续传：已有 %d / %d 字节", sat_id, offer.name,
                            tr.received, offer.size)
            if tr.received == offer.size:
                # 字节齐了但上次没走到 file_end（断线 / 枢纽重启）：自己收尾。
                return [{"t": "file_want", "file_id": fid, "offset": int(offer.size)},
                        *self._finalize(tr)]
            return [{"t": "file_want", "file_id": fid, "offset": int(tr.received)}]

    def chunk(self, sat_id: str, chunk: P.FileChunk) -> list[dict]:
        fid = chunk.file_id
        with self._lock:
            tr = self._transfers.get((sat_id, fid)) or self._restore(sat_id, fid)
            if tr is None:
                return [_done(fid, False, error="unknown_file (send file_offer first)")]
            data = chunk.decoded()
            size = int(tr.offer["size"])
            if chunk.offset != tr.received:
                now = time.monotonic()
                # 在途还有好几个错位的块时只回一次，免得 want 风暴。
                if tr.last_resync_offset != tr.received or now - tr.last_resync_mono > 1.0:
                    tr.last_resync_offset, tr.last_resync_mono = tr.received, now
                    return [{"t": "file_want", "file_id": fid, "offset": int(tr.received)}]
                return []
            if tr.received + len(data) > size:
                self._drop(tr)
                return [_done(fid, False, error="size_overflow")]
            with open(tr.part, "ab") as f:
                f.write(data)
            tr.received += len(data)
            self.stats["bytes"] += len(data)
            return []

    def end(self, sat_id: str, end: P.FileEnd) -> list[dict]:
        fid = end.file_id
        with self._lock:
            tr = self._transfers.get((sat_id, fid)) or self._restore(sat_id, fid)
            if tr is None:
                existing = self.archive.find(sat_id, fid)
                if existing is not None:
                    return [_done(fid, True, stored=existing.get("stored_rel"))]
                return [_done(fid, False, error="unknown_file (send file_offer first)")]
            tr.received = _size(tr.part)
            if tr.received < int(tr.offer["size"]):
                return [{"t": "file_want", "file_id": fid, "offset": int(tr.received)}]
            return self._finalize(tr)

    # ── 内部 ──
    def _restore(self, sat_id: str, fid: str) -> _Transfer | None:
        inc = self.archive.incoming_dir(sat_id)
        op = inc / f"{fid}.offer.json"
        try:
            offer = json.loads(op.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        tr = _Transfer(sat_id=sat_id, offer=offer, part=inc / f"{fid}.part", offer_path=op,
                       received=_size(inc / f"{fid}.part"))
        self._transfers[(sat_id, fid)] = tr
        return tr

    def _drop(self, tr: _Transfer) -> None:
        self._transfers.pop((tr.sat_id, tr.offer["file_id"]), None)
        for p in (tr.part, tr.offer_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    def _finalize(self, tr: _Transfer) -> list[dict]:
        offer = tr.offer
        fid = str(offer["file_id"])
        sha = _sha256(tr.part)
        if sha != fid:
            self.stats["sha_mismatch"] += 1
            logger.warning("卫星 %s 文件 %s 的 sha256 不符（收到 %s，应为 %s）—— 丢弃，等卫星重传",
                           tr.sat_id, offer.get("name"), sha[:12], fid[:12])
            self._drop(tr)
            return [_done(fid, False, error="sha256_mismatch")]
        root = Path(self.archive._root_fn())
        rel, changed = safe_rel_path(str(offer.get("rel_path") or ""), str(offer.get("name") or ""))
        files_dir = self.archive.files_dir(tr.sat_id)
        dest = files_dir / Path(*rel.parts)
        dest = _free_name(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tr.part, dest)
        received_ms = _now_ms()
        # mtime 刻意保持「收到的时刻」：卫星上的原始 mtime 只写进 .meta.json。按 mtime 找
        # 新文件的下游（若有）不会把刚到的文件当成旧文件漏掉。
        meta = self._build_meta(tr, dest, rel, changed, received_ms)
        meta_path = dest.with_name(dest.name + META_SUFFIX)
        _write_json_atomic(meta_path, meta)
        entry = {
            "file_id": fid, "sat_id": tr.sat_id, "name": offer.get("name"),
            "rel_path": offer.get("rel_path"), "stored_rel": _rel_posix(dest, root),
            "meta_rel": _rel_posix(meta_path, root), "size": int(offer.get("size") or 0),
            "sha256": fid, "kind": offer.get("kind"), "mtime_ms": offer.get("mtime_ms"),
            "received_ms": received_ms, "boot_id": meta.get("boot_id"),
            "acq_start_hub_ms": (meta.get("times") or {}).get("acq_start_hub_ms"),
            "acq_end_hub_ms": (meta.get("times") or {}).get("acq_end_hub_ms"),
            "spe_ok": (meta.get("spe") or {}).get("ok") if meta.get("spe") else None,
            "stm_matched": bool((meta.get("stm_state") or {}).get("matched")),
        }
        ingest = "no_runtime"
        if self._ingest is not None:
            try:
                ingest = self._ingest([dest, meta_path], tr.sat_id)
            except Exception as exc:  # noqa: BLE001
                ingest = f"error: {type(exc).__name__}"
        entry["experiment_ingest"] = ingest
        self.archive.add(entry)
        self._transfers.pop((tr.sat_id, fid), None)
        try:
            tr.offer_path.unlink(missing_ok=True)
        except OSError:
            pass
        self.stats["stored"] += 1
        logger.info("卫星 %s 文件已入库：%s（%d 字节，STM 快照%s）", tr.sat_id, entry["stored_rel"],
                    entry["size"], "有" if entry["stm_matched"] else "无")
        if self._on_stored is not None:
            try:
                self._on_stored(entry)
            except Exception:  # noqa: BLE001
                logger.debug("on_stored failed", exc_info=True)
        return [_done(fid, True, stored=entry["stored_rel"])]

    def _build_meta(self, tr: _Transfer, dest: Path, rel: PurePosixPath, changed: bool,
                    received_ms: int) -> dict:
        offer = tr.offer
        ctx = {}
        try:
            ctx = self._context(tr.sat_id) or {}
        except Exception:  # noqa: BLE001
            ctx = {}
        clock = dict(ctx.get("clock") or {})
        theta = clock.get("offset_ms")
        spe = None
        is_spe = str(offer.get("kind") or "").lower() == "spe" or dest.suffix.lower() == ".spe"
        if is_spe:
            from mast.satellite.spe import parse_spe

            parsed = parse_spe(dest)
            footer = parsed.get("footer") or {}
            spe = {"ok": parsed.get("ok"), "error": parsed.get("error"),
                   "header": parsed.get("header"), "key": footer.get("key"),
                   "footer": {k: v for k, v in footer.items() if k != "key"}}
        times = _acq_times(offer, spe, theta)
        stm = None
        if self._ring is not None:
            try:
                stm = self._ring.window(times.get("acq_start_hub_ms"), times.get("acq_end_hub_ms"))
            except Exception as exc:  # noqa: BLE001
                stm = {"matched": False, "reason": f"error: {exc}"}
        hlc = None
        try:
            from mast.logging.v2.hlc import HLCClock

            hlc = HLCClock("HUB").now().encode()
        except Exception:  # noqa: BLE001
            hlc = None
        return {
            "schema": META_SCHEMA,
            "sat_id": tr.sat_id,
            "boot_id": ctx.get("boot_id"),
            "hub_boot_id": ctx.get("hub_boot_id"),
            "file": {
                "file_id": offer.get("file_id"), "name": offer.get("name"),
                "rel_path": offer.get("rel_path"), "stored_name": dest.name,
                "rel_path_sanitized": str(rel) if changed else None,
                "size": offer.get("size"), "sha256": offer.get("sha256"),
                "kind": offer.get("kind"), "mtime_ms": offer.get("mtime_ms"),
                "mtime_utc": _iso(offer.get("mtime_ms")),
            },
            "satellite_meta": offer.get("meta") or {},
            "received_ms": received_ms,
            "received_utc": _iso(received_ms),
            "hlc": hlc,
            "clock": {"offset_ms": theta, "delay_ms": clock.get("delay_ms"),
                      "rtt_ms": clock.get("rtt_ms"), "samples": clock.get("samples"),
                      "convention": "ts_hub = ts_sat - offset_ms"},
            "times": times,
            "spe": spe,
            "stm_state": stm,
        }


def _acq_times(offer: dict, spe: dict | None, theta: float | None) -> dict:
    """采集时间窗（卫星时钟 → 枢纽时钟）。来源优先级：卫星元数据 > SPE 文件尾 > mtime。"""
    meta = offer.get("meta") or {}
    start = meta.get("acq_started_ms")
    end = meta.get("acq_finished_ms")
    source = "satellite_meta" if (start or end) else None
    key = (spe or {}).get("key") or {}
    if source is None and (key.get("acq_origin_created_ms") or key.get("file_created_ms")):
        start = key.get("acq_origin_created_ms")
        end = key.get("file_created_ms") or start
        source = "spe_footer"
    if source is None:
        end = offer.get("mtime_ms") or None
        start = end
        exp = key.get("exposure_ms")
        frames = key.get("frames") or 1
        if end and isinstance(exp, (int, float)):
            start = float(end) - float(exp) * float(frames)
        source = "mtime" if end else "unknown"
    off = float(theta) if isinstance(theta, (int, float)) else None

    def _hub(v):
        if v is None:
            return None
        return float(v) - (off if off is not None else 0.0)

    sh, eh = _hub(start), _hub(end)
    return {"source": source, "acq_start_sat_ms": start, "acq_end_sat_ms": end,
            "acq_start_hub_ms": sh, "acq_end_hub_ms": eh,
            "acq_start_utc": _iso(sh), "acq_end_utc": _iso(eh),
            "clock_offset_known": off is not None}


def _done(fid: str, ok: bool, *, stored: str | None = None, error: str | None = None) -> dict:
    return {"t": "file_done", "file_id": fid, "ok": bool(ok), "stored": stored, "error": error}


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _free_bytes(p: Path) -> int | None:
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def _free_name(dest: Path) -> Path:
    """目标已存在（同名不同内容）→ ``name (2).ext``…… 不覆盖任何已入库文件。"""
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for i in range(2, 10000):
        cand = dest.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    return dest.with_name(f"{stem} ({int(time.time() * 1000)}){suffix}")


def _rel_posix(p: Path, base: Path) -> str:
    try:
        return p.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def _write_json_atomic(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def b64_chunks(data: bytes, chunk_bytes: int):
    """卫星端用：把字节切成 ``(offset, data_b64)``。"""
    for off in range(0, len(data), chunk_bytes):
        yield off, base64.b64encode(data[off:off + chunk_bytes]).decode("ascii")


__all__ = ["FileArchive", "FileReceiver", "safe_rel_path", "META_SUFFIX", "META_SCHEMA",
           "b64_chunks"]
