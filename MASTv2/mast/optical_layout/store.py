"""Files of the optical layouts: atomic writes, history, comments, archive.

Where (``<root>`` is ``mast._runtime_paths.project_root()``, looked up at every call so a test or
a relocated data folder is honoured)::

    <root>/config/optical_layouts/<id>.json                    the current version
    <root>/config/optical_layouts/<id>.comments.json           {"version", "next", "comments": [...]}
    <root>/config/optical_layouts/history/<id>.jsonl           one whole version per line, newest last
    <root>/config/optical_layouts/archive/<id>-<stamp>.*       a deleted layout (never really deleted)

Rules:

* **Optimistic concurrency.** :meth:`LayoutStore.save` takes the revision the caller started
  from; if it is not the stored one the save is refused (:class:`Conflict`) and nothing changes.
  A new layout is saved with ``base_revision`` 0. ``revision``, ``updated_at`` and ``updated_by``
  are written here, never taken from the caller.
* **Atomic.** The current file is replaced in one step (temp file in the same folder, fsync,
  ``os.replace`` with the short retries Windows needs when a reader has the file open); the
  history line is appended first and taken back if the replace fails, so a refused save leaves
  both files as they were. Temp files are named ``*.tmp-<pid>-<tid>``, never ``.part-`` (the
  runtime sweeps those from its data roots at start-up).
* **History.** Every save appends a whole version; the newest 200 are kept.
* **A file that cannot be read is never overwritten.** Unreadable JSON, a document that fails
  validation, or a file whose ``id`` is not its name is logged (WARNING) and raised as
  :class:`Corrupt`; the routes answer 500 with the reason. Nothing is "repaired".
* **Archive instead of delete.** The layout's three files are moved into ``archive/`` with a time
  stamp and a small ``.meta.json`` (who, when, which revision).
* **Comments** are plain records (see :meth:`LayoutStore.add_comment`); at most
  :data:`MAX_COMMENTS` per layout - when full, the oldest *resolved* ones are moved to
  ``archive/<id>.comments-pruned.jsonl`` to make room; a layout whose comments are all open
  refuses more (:class:`LimitReached`).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from pydantic import ValidationError

from mast.i18n import tr
from mast.optical_layout.model import Layout, is_valid_id

logger = logging.getLogger(__name__)

MAX_HISTORY = 200
MAX_COMMENTS = 1000
MAX_COMMENT_CHARS = 2000
#: ``os.replace`` onto a file somebody has open fails on Windows for a few ms (antivirus, a reader)
_REPLACE_RETRY_S = (0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.45)


class StoreError(Exception):
    """Base of what the store raises on purpose."""


class NotFound(StoreError):
    """No such layout / revision / comment."""


class Invalid(StoreError, ValueError):
    """The request itself is wrong (an empty comment, a component that is not in the layout)."""


class Conflict(StoreError):
    """The caller's ``base_revision`` is not the stored one (or the layout already exists)."""

    def __init__(self, message: str, current_revision: int, updated_by: str = "", updated_at: str = "") -> None:
        super().__init__(message)
        self.current_revision = current_revision
        self.updated_by = updated_by
        self.updated_at = updated_at


class Corrupt(StoreError):
    """A stored file that cannot be used (and is left exactly as it is)."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(tr(f"{path.name} 读不了：{reason}。文件原样保留，没有被覆盖；请检查或移走它。",
                            f"{path.name} cannot be used: {reason}. The file was left as it is, "
                            "not overwritten; check it or move it away."))
        self.path = path
        self.reason = reason


class LimitReached(StoreError):
    def __init__(self, message: str, code: str, limit: int) -> None:
        super().__init__(message)
        self.code = code
        self.limit = limit


class SaveFailed(StoreError):
    """The disk refused; nothing was changed."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__(tr(f"没能存盘（{type(exc).__name__}: {exc}）。什么都没有改变，请检查数据目录后重试。",
                            f"Could not save ({type(exc).__name__}: {exc}). Nothing was changed; "
                            "check the data folder and try again."))
        self.cause = exc


def layout_root() -> Path:
    from mast._runtime_paths import project_root

    return project_root() / "config" / "optical_layouts"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
#: (path, mtime_ns, size) -> what a listing shows of a layout (see ``LayoutStore._head``)
_HEADS: dict[tuple, tuple] = {}
_HEADS_GUARD = threading.Lock()


def _lock(root: Path, layout_id: str) -> threading.RLock:
    key = f"{os.path.normcase(str(root))}|{layout_id.lower()}"
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.RLock()
        return lock


def _forget_head(path: Path) -> None:
    """A write of ours changes the file: whatever the listing remembered of it is out of date (two
    writes within one clock tick, of the same size, would otherwise look like no change)."""
    with _HEADS_GUARD:
        for key in [k for k in _HEADS if k[0] == str(path)]:
            del _HEADS[key]


def _tmp(path: Path) -> Path:
    return path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident():x}")


def atomic_write(path: Path, data: bytes | Iterable[bytes]) -> None:
    """Replace *path* by *data* (bytes, or an iterable of byte chunks, written one after the other
    so that a big file need not be in memory) in one step. Raises ``OSError``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp(path)
    try:
        with open(tmp, "wb") as fh:
            if isinstance(data, (bytes, bytearray)):
                fh.write(data)
            else:
                for chunk in data:
                    fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        for delay in (*_REPLACE_RETRY_S, None):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if delay is None:
                    raise
                time.sleep(delay)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


#: the block the history file is read backwards in
_BLOCK = 1 << 18


def _lines_backwards(path: Path, block: int = _BLOCK) -> Iterator[bytes]:
    """The lines of *path* from the last to the first (without the newline), holding one line and one
    block in memory. A history line is a whole layout - up to a megabyte - and the file has up to 200
    of them: it is never read as a whole. The file is closed when the generator is."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        tail = b""                                  # the start of a line whose beginning is in an earlier block
        while pos > 0:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            parts = (fh.read(step) + tail).split(b"\n")
            tail = parts[0]
            yield from reversed(parts[1:])
        yield tail


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _line_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


@dataclass(frozen=True)
class Row:
    """One layout in a listing."""

    id: str
    title: str
    revision: int
    updated_at: str
    updated_by: str
    components: int
    open_comments: int
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "title": self.title, "revision": self.revision,
                             "updated_at": self.updated_at, "updated_by": self.updated_by,
                             "components": self.components, "open_comments": self.open_comments}
        if self.error:
            d["error"] = self.error
        return d


class LayoutStore:
    """The layouts under one data root (default: the project's)."""

    def __init__(self, root: Optional[os.PathLike | str] = None) -> None:
        self._root = Path(root) if root is not None else None

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else layout_root()

    # -- paths -------------------------------------------------------------------------------

    def _path(self, layout_id: str) -> Path:
        if not is_valid_id(layout_id):
            raise NotFound(tr(f"没有这张光路图：{layout_id!r}", f"No such optical layout: {layout_id!r}"))
        return self.root / f"{layout_id}.json"

    def _comments_path(self, layout_id: str) -> Path:
        return self.root / f"{layout_id}.comments.json"

    def _history_path(self, layout_id: str) -> Path:
        return self.root / "history" / f"{layout_id}.jsonl"

    # -- reading -----------------------------------------------------------------------------

    def exists(self, layout_id: str) -> bool:
        try:
            return self._path(layout_id).is_file()
        except NotFound:
            return False

    def load(self, layout_id: str) -> Layout:
        """The stored layout; :class:`NotFound` or :class:`Corrupt`."""
        path = self._path(layout_id)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            raise NotFound(tr(f"没有这张光路图：{layout_id!r}", f"No such optical layout: {layout_id!r}")) from None
        except (OSError, UnicodeError) as exc:
            raise self._corrupt(path, f"{type(exc).__name__}: {exc}") from None
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise self._corrupt(path, f"not valid JSON ({exc})") from None
        try:
            lay = Layout.model_validate(raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"]) or "document"
            raise self._corrupt(path, f"{where}: {first['msg']}") from None
        if lay.id != layout_id:
            if lay.id.lower() == layout_id.lower():       # a case-insensitive file system
                raise NotFound(tr(f"没有这张光路图：{layout_id!r}", f"No such optical layout: {layout_id!r}"))
            raise self._corrupt(path, f"the file holds the layout {lay.id!r}")
        return lay

    @staticmethod
    def _corrupt(path: Path, reason: str) -> Corrupt:
        logger.warning("optical layout: %s cannot be used (%s); it is left untouched", path, reason)
        return Corrupt(path, reason)

    def list_layouts(self) -> list[dict[str, Any]]:
        """Every layout on disk, most recently saved first. A file that cannot be read is listed
        with an ``error`` (so it does not seem to have vanished)."""
        root = self.root
        if not root.is_dir():
            return []
        rows: list[Row] = []
        for path in root.glob("*.json"):
            stem = path.stem
            if not is_valid_id(stem):                    # <id>.comments.json and strays
                continue
            head = self._head(path, stem)
            if head is None:                             # not (or no longer) there
                continue
            if isinstance(head, str):                    # cannot be read: listed with the reason
                rows.append(Row(stem, "", 0, "", "", 0, 0, error=head))
                continue
            rows.append(Row(stem, head[0], head[1], head[2], head[3], head[4], self._open_count_quiet(stem)))
        rows.sort(key=lambda r: (r.updated_at, r.id), reverse=True)
        return [r.to_dict() for r in rows]

    def _head(self, path: Path, layout_id: str) -> "tuple | str | None":
        """``(title, revision, updated_at, updated_by, components)`` of a layout file, ``None`` if it
        vanished, the reason as a string if it cannot be read. Remembered per file by modification time and
        size: a listing is asked for by the briefing, constantly, and must not re-validate every
        layout every time."""
        try:
            st = path.stat()
        except OSError:
            return None
        key = (str(path), st.st_mtime_ns, st.st_size)
        with _HEADS_GUARD:
            hit = _HEADS.get(key)
        if hit is not None:
            return hit
        try:
            lay = self.load(layout_id)
        except NotFound:
            return None
        except Corrupt as exc:
            return str(exc)                              # not remembered: the file may be fixed any moment
        head = (lay.title, lay.revision, lay.updated_at, lay.updated_by, len(lay.components))
        with _HEADS_GUARD:
            for old in [k for k in _HEADS if k[0] == key[0]]:
                del _HEADS[old]
            if len(_HEADS) > 500:
                _HEADS.clear()
            _HEADS[key] = head
        return head

    # -- writing -----------------------------------------------------------------------------

    def save(self, layout: Layout, *, base_revision: int, by: str,
             client: Optional[dict[str, Any]] = None) -> Layout:
        """Store *layout* as the next revision. :class:`Conflict` if ``base_revision`` is not the
        stored revision (0 = "this layout does not exist yet"); :class:`Corrupt` if the stored file
        cannot be read; :class:`SaveFailed` if the disk refuses (nothing changed then)."""
        lid = layout.id
        with _lock(self.root, lid):
            try:
                current: Optional[Layout] = self.load(lid)
            except NotFound:
                current = None
            have = current.revision if current is not None else 0
            if base_revision != have:
                if current is not None:
                    zh = (f"版本冲突：这张图现在是第 {have} 版（你基于第 {base_revision} 版修改）。别处已经改过，"
                          "请载入最新版再改，或另存为新图。")
                    en = (f"Revision conflict: the layout is at revision {have} now (you started from "
                          f"{base_revision}). It was changed elsewhere; load the latest or save a copy.")
                else:
                    zh = f"版本冲突：这张图还不存在（你基于第 {base_revision} 版），可能已被归档；请另存为新图。"
                    en = (f"Revision conflict: the layout does not exist (you started from revision "
                          f"{base_revision}); it may have been archived. Save a copy.")
                raise Conflict(tr(zh, en), have, current.updated_by if current else "",
                               current.updated_at if current else "")
            new = layout.model_copy(update={"revision": have + 1, "updated_at": _now(),
                                            "updated_by": by})
            doc = new.to_dict()
            hist = self._history_path(lid)
            prior = hist.stat().st_size if hist.exists() else 0
            try:
                hist.parent.mkdir(parents=True, exist_ok=True)
                with open(hist, "ab") as fh:
                    fh.write(_line_bytes({"revision": new.revision, "saved_at": new.updated_at,
                                          "by": by, "client": client, "layout": doc}))
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                self._truncate(hist, prior)
                raise SaveFailed(exc) from None
            try:
                atomic_write(self._path(lid), _json_bytes(doc))
            except OSError as exc:
                self._truncate(hist, prior)
                raise SaveFailed(exc) from None
            _forget_head(self._path(lid))
            self._trim_history(hist)
            return new

    @staticmethod
    def _truncate(path: Path, size: int) -> None:
        try:
            with open(path, "r+b") as fh:
                fh.truncate(size)
        except OSError as exc:
            logger.warning("optical layout: could not take back the history line in %s (%s)", path, exc)

    @staticmethod
    def _trim_history(path: Path) -> None:
        """Keep the newest :data:`MAX_HISTORY` lines. Best effort: a failure only means the file is
        a little long until the next save (readers cut to the newest 200 anyway). One line in memory
        at a time: the file can be 200 megabytes."""
        try:
            with open(path, "rb") as fh:
                total = sum(1 for _ in fh)
            if total > MAX_HISTORY:
                skip = total - MAX_HISTORY

                def newest() -> Iterator[bytes]:
                    with open(path, "rb") as fh:
                        for n, line in enumerate(fh):
                            if n >= skip:
                                yield line

                atomic_write(path, newest())
        except OSError as exc:
            logger.warning("optical layout: could not trim the history %s (%s)", path, exc)

    def archive(self, layout_id: str, *, base_revision: int, by: str,
                client: Optional[dict[str, Any]] = None) -> Path:
        """Move the layout, its history and its comments into ``archive/``; returns the archived
        layout file. The same revision rule as :meth:`save`."""
        with _lock(self.root, layout_id):
            current = self.load(layout_id)
            if base_revision != current.revision:
                raise Conflict(
                    tr(f"版本冲突：这张图现在是第 {current.revision} 版（你基于第 {base_revision} 版）。",
                       f"Revision conflict: the layout is at revision {current.revision} now (you "
                       f"started from {base_revision})."),
                    current.revision, current.updated_by, current.updated_at)
            stamp = _stamp()
            dest = self.root / "archive"
            moves = [(self._path(layout_id), dest / f"{layout_id}-{stamp}.json"),
                     (self._history_path(layout_id), dest / f"{layout_id}-{stamp}.history.jsonl"),
                     (self._comments_path(layout_id), dest / f"{layout_id}-{stamp}.comments.json")]
            done: list[tuple[Path, Path]] = []
            try:
                dest.mkdir(parents=True, exist_ok=True)
                atomic_write(dest / f"{layout_id}-{stamp}.meta.json",
                             _json_bytes({"id": layout_id, "revision": current.revision, "title": current.title,
                                          "archived_by": by, "archived_at": _now(), "client": client}))
                for src, dst in moves:
                    if src.exists():
                        os.replace(src, dst)
                        done.append((src, dst))
                _forget_head(moves[0][0])
            except OSError as exc:
                for src, dst in reversed(done):
                    try:
                        os.replace(dst, src)
                    except OSError:
                        logger.error("optical layout: could not put %s back after a failed archive", src)
                try:
                    (dest / f"{layout_id}-{stamp}.meta.json").unlink()
                except OSError:
                    pass
                raise SaveFailed(exc) from None
            return moves[0][1]

    # -- history -----------------------------------------------------------------------------

    def _history_records(self, layout_id: str) -> Iterator[dict[str, Any]]:
        """The history records, newest first, each revision once (the last line written for it: a
        retried save repeats a revision) and at most :data:`MAX_HISTORY` of them. The file is read
        from its end, one line in memory at a time, and only as far as the caller goes: a history is
        up to 200 megabytes. A line that is not a record is skipped. Close the generator when you stop
        early (the file is open until then)."""
        hist = self._history_path(layout_id)
        seen: set[int] = set()
        try:
            for line in _lines_backwards(hist):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    rev = int(rec["revision"])
                except (ValueError, KeyError, TypeError) as exc:
                    logger.warning("optical layout: a line of %s is not a history record (%s); skipped", hist, exc)
                    continue
                if rev in seen:                                # an earlier line of a revision written twice
                    continue
                seen.add(rev)
                yield rec
                if len(seen) >= MAX_HISTORY:
                    return
        except FileNotFoundError:
            return
        except OSError as exc:
            raise self._corrupt(hist, f"{type(exc).__name__}: {exc}") from None

    def history(self, layout_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Revisions, newest first: ``{revision, updated_at, updated_by, title, components}``."""
        self._path(layout_id)
        if not self.exists(layout_id) and not self._history_path(layout_id).exists():
            raise NotFound(tr(f"没有这张光路图：{layout_id!r}", f"No such optical layout: {layout_id!r}"))
        want = max(0, int(limit))
        rows: list[dict[str, Any]] = []
        if want == 0:
            return rows
        records = self._history_records(layout_id)
        try:
            for rec in records:
                doc = rec.get("layout") if isinstance(rec.get("layout"), dict) else {}
                rows.append({"revision": int(rec["revision"]), "updated_at": str(rec.get("saved_at") or ""),
                             "updated_by": str(rec.get("by") or ""), "title": str(doc.get("title") or ""),
                             "components": len(doc.get("components") or [])})
                if len(rows) >= want:
                    break
        finally:
            records.close()
        return rows

    def revision(self, layout_id: str, revision: int) -> Layout:
        """That version of the layout (:class:`NotFound` if it is not among the newest 200). Reads the
        history from its end and stops at the version."""
        return self.revision_record(layout_id, revision)[0]

    def revision_record(self, layout_id: str, revision: int) -> tuple[Layout, str, str]:
        """:meth:`revision` with when it was saved and by whom, as its history line says."""
        self._path(layout_id)
        records = self._history_records(layout_id)
        try:
            for rec in records:
                if int(rec["revision"]) == int(revision):
                    try:
                        return (Layout.model_validate(rec.get("layout")), str(rec.get("saved_at") or ""),
                                str(rec.get("by") or ""))
                    except ValidationError as exc:
                        first = exc.errors()[0]
                        raise self._corrupt(self._history_path(layout_id), f"revision {revision}: {first['msg']}") from None
        finally:
            records.close()
        raise NotFound(tr(f"{layout_id!r} 没有第 {revision} 版（只保留最近 {MAX_HISTORY} 版）",
                          f"{layout_id!r} has no revision {revision} (the newest {MAX_HISTORY} are kept)"))

    # -- comments ----------------------------------------------------------------------------

    def _read_comments(self, layout_id: str) -> dict[str, Any]:
        path = self._comments_path(layout_id)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {"version": 1, "next": 1, "comments": []}
        except (OSError, UnicodeError) as exc:
            raise self._corrupt(path, f"{type(exc).__name__}: {exc}") from None
        try:
            doc = json.loads(text)
            comments = doc["comments"]
            if not isinstance(comments, list) or not all(isinstance(c, dict) and "id" in c for c in comments):
                raise ValueError("comments must be a list of records")
            doc["next"] = int(doc.get("next") or 1)
        except (ValueError, KeyError, TypeError) as exc:
            raise self._corrupt(path, f"not a comments file ({exc})") from None
        return doc

    def comments(self, layout_id: str, *, open_only: bool = False) -> list[dict[str, Any]]:
        """The comments of an existing layout, oldest first (each with the stored ``client``
        record: callers that hand comments to someone else drop it, see :func:`public_comment`)."""
        if not self.exists(layout_id):
            raise NotFound(tr(f"没有这张光路图：{layout_id!r}", f"No such optical layout: {layout_id!r}"))
        rows = self._read_comments(layout_id)["comments"]
        return [c for c in rows if not (open_only and c.get("resolved"))]

    def _open_count_quiet(self, layout_id: str) -> int:
        try:
            return sum(1 for c in self._read_comments(layout_id)["comments"] if not c.get("resolved"))
        except Corrupt:
            return 0

    def open_comment_count(self, layout_id: str) -> int:
        return len(self.comments(layout_id, open_only=True))

    def add_comment(self, layout_id: str, *, text: str, by: str, author: str,
                    component_id: Optional[str] = None, client: Optional[dict[str, Any]] = None,
                    max_open_for_author: Optional[int] = None) -> dict[str, Any]:
        """Append a comment. ``author`` is ``"operator"`` or ``"agent"``; ``by`` the name (an
        agent's is its ``ext:<name>`` id). ``component_id`` must be a component of the layout.
        :class:`LimitReached` when ``by`` already has ``max_open_for_author`` unresolved comments
        (``code`` ``too_many_open_comments``) or the layout is full of unresolved ones
        (``code`` ``layout_comments_full``)."""
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            raise Invalid(tr("评论不能是空的", "a comment must not be empty"))
        if len(text) > MAX_COMMENT_CHARS:
            raise Invalid(tr(f"评论最多 {MAX_COMMENT_CHARS} 字符（现在 {len(text)}）",
                                f"a comment takes at most {MAX_COMMENT_CHARS} characters (this one has {len(text)})"))
        with _lock(self.root, layout_id):
            lay = self.load(layout_id)
            if component_id is not None and lay.component(component_id) is None:
                raise Invalid(tr(f"这张图里没有器件 {component_id!r}", f"this layout has no component {component_id!r}"))
            doc = self._read_comments(layout_id)
            comments: list[dict[str, Any]] = doc["comments"]
            if max_open_for_author is not None:
                mine = sum(1 for c in comments if not c.get("resolved") and c.get("by") == by)
                if mine >= max_open_for_author:
                    raise LimitReached(
                        tr(f"你已经有 {mine} 条没解决的评论（上限 {max_open_for_author}）：等操作员解决后再发新的。",
                           f"You already have {mine} unresolved comments (the limit is {max_open_for_author}): "
                           "wait for the operator to resolve some."), "too_many_open_comments", max_open_for_author)
            pruned: list[dict[str, Any]] = []
            while len(comments) - len(pruned) >= MAX_COMMENTS:
                oldest = next((c for c in comments if c.get("resolved") and c not in pruned), None)
                if oldest is None:
                    raise LimitReached(
                        tr(f"这张图已有 {MAX_COMMENTS} 条评论且都没解决：先解决一些。",
                           f"This layout holds {MAX_COMMENTS} comments, none of them resolved: resolve some first."),
                        "layout_comments_full", MAX_COMMENTS)
                pruned.append(oldest)
            comment = {"id": f"c{doc['next']}", "author": author, "by": by, "text": text,
                       "component_id": component_id, "created_at": _now(), "resolved": False,
                       "resolved_by": None, "resolved_at": None, "client": client}
            kept = [c for c in comments if c not in pruned] + [comment]
            self._write_comments(layout_id, {"version": 1, "next": doc["next"] + 1, "comments": kept}, pruned)
            return comment

    def resolve_comment(self, layout_id: str, comment_id: str, *, by: str,
                        client: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Mark a comment resolved (idempotent: a resolved one is returned as it is)."""
        with _lock(self.root, layout_id):
            self.load(layout_id)
            doc = self._read_comments(layout_id)
            for c in doc["comments"]:
                if c.get("id") == comment_id:
                    if not c.get("resolved"):
                        c.update(resolved=True, resolved_by=by, resolved_at=_now(),
                                 resolved_client=client)
                        self._write_comments(layout_id, doc, [])
                    return c
        raise NotFound(tr(f"没有评论 {comment_id!r}", f"No such comment {comment_id!r}"))

    def _write_comments(self, layout_id: str, doc: dict[str, Any], pruned: list[dict[str, Any]]) -> None:
        try:
            if pruned:
                arch = self.root / "archive" / f"{layout_id}.comments-pruned.jsonl"
                arch.parent.mkdir(parents=True, exist_ok=True)
                with open(arch, "ab") as fh:
                    for c in pruned:
                        fh.write(_line_bytes(c))
            atomic_write(self._comments_path(layout_id), _json_bytes(doc))
        except OSError as exc:
            raise SaveFailed(exc) from None


_PUBLIC_COMMENT_KEYS = ("id", "author", "by", "text", "component_id", "created_at", "resolved",
                        "resolved_by", "resolved_at")


def public_comment(comment: dict[str, Any]) -> dict[str, Any]:
    """The comment without what the server saw of its writer (peer address, user agent ...)."""
    return {k: comment.get(k) for k in _PUBLIC_COMMENT_KEYS}


def get_store() -> LayoutStore:
    """The store of the project's data root (the root is looked up at every use)."""
    return LayoutStore()


__all__ = [
    "Conflict", "Corrupt", "Invalid", "LayoutStore", "LimitReached", "MAX_COMMENTS", "MAX_COMMENT_CHARS",
    "MAX_HISTORY", "NotFound", "SaveFailed", "StoreError", "atomic_write", "get_store",
    "layout_root", "public_comment",
]
