# -*- coding: utf-8 -*-
"""环境标签记录器：钩子只 ``submit``（放进队列），一个按需起的工作线程做全部 I/O。

钩子一侧（技能记录接缝、文件监视、实验文件夹副本落地）调 :meth:`FileEnvTagger.submit`：
**只做字符串处理与一次加锁的字典操作，不 stat、不读文件、不读配置，永不阻塞、永不抛**。队列
有界（:data:`QUEUE_MAX`），满了丢弃并计数；同一路径只留一个待办。

工作线程（daemon，第一次提交时起，空闲 :data:`IDLE_EXIT_S` 后自己退出 —— 不常驻）对每个待办：

1. 文件还在吗；**静置门**：mtime 太新（还在写）就延后（``.3ds`` 采集中每个点都改写，60 s）；
2. sidecar 已在、描述的就是这一份字节 ⇒ 什么都不做；
3. 这个身份（文件名、size、mtime_ns）已经有记录（内存 LRU 或中央记录）⇒ 原样补写 sidecar ——
   实验文件夹里的副本就是这样拿到与原件一字不差的记录；
4. 否则现算一份（:func:`mast.envhistory.filetags.snapshot.build_record`）：**只读内存缓存
   （读数环、``get_latest()``）、传感器的类型与字段、环境历史库，绝不碰设备**；
5. 不覆盖地写 sidecar；写中央记录；
6. 一个目录第一次写成 sidecar 时记一行 INFO（本进程内一个目录只记一次）；目录写失败就冷却
   :data:`DIR_COOLDOWN_S`，期间只写中央记录，日志首次大声、之后每分钟至多一条。
"""
from __future__ import annotations

import json
import logging
import os
import platform
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from mast.envhistory.filetags import sidecar as SC

logger = logging.getLogger(__name__)

SUFFIXES = (".sxm", ".dat", ".3ds")
#: 静置门（秒）：mtime 距今不足这么久就延后。``.3ds`` 在采集中每完成一个点就写一次。
SETTLE_S = {".sxm": 3.0, ".dat": 3.0, ".3ds": 60.0}
QUEUE_MAX = 1024
#: 最近打过标的记录（每条约 3 KB）。副本通常几秒到几分钟后就到，更旧的靠中央记录。
LRU_MAX = 256
DIR_COOLDOWN_S = 600.0
IDLE_EXIT_S = 60.0
_FAIL_QUIET_S = 60.0


@dataclass
class _Job:
    path: str
    origin: str
    observed_at: float
    not_before: float = 0.0          # monotonic


def _identity_key(path: str, size: int, mtime_ns: int) -> tuple:
    return (os.path.basename(path).lower(), int(size), int(mtime_ns))


class FileEnvTagger:
    """见模块说明。所有公开方法永不抛。"""

    def __init__(self, *, ring: Any = None,
                 latest_getter: Callable[[], dict] | None = None,
                 meta_getter: Callable[[], dict] | None = None,
                 store_getter: Callable[[], Any] | None = None,
                 settings_getter: Callable[[], tuple[bool, float]] | None = None,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 queue_max: int = QUEUE_MAX, idle_exit_s: float = IDLE_EXIT_S,
                 start_worker: bool = True, host: str | None = None,
                 mast_version: str | None = None) -> None:
        self._ring = ring
        self._latest_getter = latest_getter
        self._meta_getter = meta_getter
        self._store_getter = store_getter
        self._settings_getter = settings_getter
        self._clock = clock
        self._mono = monotonic
        self._queue_max = max(1, int(queue_max))
        self._idle_exit = float(idle_exit_s)
        self._start_worker = bool(start_worker)
        self._host = platform.node() if host is None else str(host)
        self._mast_version = self._version() if mast_version is None else str(mast_version)

        self._cv = threading.Condition()
        self._jobs: "OrderedDict[str, _Job]" = OrderedDict()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._busy = False
        self._lru: "OrderedDict[tuple, dict]" = OrderedDict()
        self._cooldown: dict[str, float] = {}
        self._announced: set[str] = set()
        self._fail_log: dict[str, list] = {}
        #: 最近一次读到的设置（状态页与文件监视用）。读配置是 I/O：只在工作线程、文件监视线程、
        #: 改设置的请求里刷新（:meth:`refresh_settings`），保存路径上从不读。
        self.enabled_cached = True
        self.max_age_cached = 30.0
        self.last_error: str | None = None
        self.counts: dict[str, int] = {k: 0 for k in (
            "submitted", "accepted", "merged", "dropped_full", "dropped_disabled", "ignored",
            "vanished", "deferred", "same", "reused", "computed", "empty", "written",
            "superseded", "foreign", "failed_write", "cooldown_skipped", "central_written",
            "central_failed", "errors")}

    # ── 钩子一侧 ──────────────────────────────────────────────────────

    def submit(self, paths: Iterable, *, origin: str, observed_at: float | None = None) -> int:
        """把路径排进队列。**永不阻塞、永不抛**；返回新收下的条数。

        开关关着时也照收：要不要打标由工作线程按**最新**的设置决定（关着就丢、计数）。若在这里按
        缓存的开关丢，关过一次之后就再没有待办去刷新那个缓存 —— 重新打开永远不生效。"""
        try:
            return self._submit(paths, origin, observed_at)
        except Exception:  # noqa: BLE001 — 钩子路径绝不因打标出事
            self.counts["errors"] += 1
            return 0

    def refresh_settings(self) -> tuple[bool, float]:
        """重读开关与阈值（读图库配置，带 TTL）。**不在保存路径上调**：工作线程、文件监视线程、
        改设置的请求会调它。永不抛。"""
        return self._settings()

    def _submit(self, paths, origin: str, observed_at: float | None) -> int:
        when = float(observed_at) if observed_at is not None else float(self._clock())
        accepted = 0
        with self._cv:
            for p in paths or ():
                s = str(p or "")
                self.counts["submitted"] += 1
                if not s or os.path.splitext(s)[1].lower() not in SUFFIXES:
                    self.counts["ignored"] += 1
                    continue
                key = os.path.normcase(os.path.normpath(s))
                job = self._jobs.get(key)
                if job is not None:
                    job.not_before = 0.0       # 又变了：到点就看，不另排一个
                    self.counts["merged"] += 1
                    continue
                if len(self._jobs) >= self._queue_max:
                    self.counts["dropped_full"] += 1
                    continue
                self._jobs[key] = _Job(path=s, origin=str(origin or ""), observed_at=when)
                accepted += 1
            self.counts["accepted"] += accepted
            self._cv.notify_all()
        if self._start_worker:
            self._ensure_worker()
        return accepted

    # ── 工作线程 ──────────────────────────────────────────────────────

    def _ensure_worker(self) -> None:
        with self._cv:
            if self._stopping:
                return
            t = self._thread
            if t is not None and t.is_alive():
                return
            t = threading.Thread(target=self._run, name="mast-env-tags", daemon=True)
            self._thread = t
        t.start()

    def _take_due_locked(self) -> tuple[_Job | None, float | None]:
        now = self._mono()
        wait = None
        for key, job in self._jobs.items():
            if job.not_before <= now:
                del self._jobs[key]
                return job, None
            w = job.not_before - now
            wait = w if wait is None else min(wait, w)
        return None, wait

    def _run(self) -> None:
        idle_since = self._mono()
        while True:
            with self._cv:
                if self._stopping:
                    return
                job, wait = self._take_due_locked()
                if job is None:
                    if not self._jobs and self._mono() - idle_since >= self._idle_exit:
                        if self._thread is threading.current_thread():
                            self._thread = None
                        return
                    self._cv.wait(timeout=min(1.0, wait) if wait is not None else 1.0)
                    continue
                self._busy = True
            try:
                self._process(job)
            except Exception as exc:  # noqa: BLE001 — 工作线程绝不能死
                self.counts["errors"] += 1
                self._note_failure("process", f"{job.path}: {type(exc).__name__}: {exc}")
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()
            idle_since = self._mono()

    def process_pending(self) -> int:
        """同步处理所有**已到点**的待办（测试与关停用；不需要工作线程）。返回处理的条数。"""
        n = 0
        while True:
            with self._cv:
                job, _wait = self._take_due_locked()
            if job is None:
                return n
            try:
                self._process(job)
            except Exception as exc:  # noqa: BLE001
                self.counts["errors"] += 1
                self._note_failure("process", f"{job.path}: {type(exc).__name__}: {exc}")
            n += 1

    def drain(self, timeout: float = 10.0) -> bool:
        """等队列空、工作线程闲下来（测试用）。返回是否等到了。"""
        deadline = self._mono() + float(timeout)
        with self._cv:
            while self._jobs or self._busy:
                left = deadline - self._mono()
                if left <= 0:
                    return False
                self._cv.wait(timeout=min(0.2, left))
        return True

    def stop(self, timeout: float = 2.0) -> None:
        """请工作线程停下，有界等待；剩下的待办数如实记一句。不强杀（它不碰 TCP，daemon）。"""
        with self._cv:
            self._stopping = True
            left = len(self._jobs)
            self._cv.notify_all()
            t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=max(0.0, float(timeout)))
        if left:
            logger.info("环境标签：关停时还有 %d 个文件没打标（下次启动不会回头补）", left)

    # ── 处理一个文件（工作线程上）──────────────────────────────────────

    def _settings(self) -> tuple[bool, float]:
        try:
            if self._settings_getter is not None:
                enabled, max_age = self._settings_getter()
            else:
                from mast.envhistory.filetags.settings import current
                enabled, max_age = current()
        except Exception:  # noqa: BLE001
            enabled, max_age = True, 30.0
        self.enabled_cached = bool(enabled)
        self.max_age_cached = float(max_age)
        return self.enabled_cached, self.max_age_cached

    def _store(self):
        try:
            if self._store_getter is not None:
                return self._store_getter()
            from mast.envhistory.store import get_store
            return get_store()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _version() -> str:
        try:
            from mast.api.version import get_version
            return str(get_version())
        except Exception:  # noqa: BLE001
            return ""

    def _requeue(self, job: _Job, not_before: float) -> None:
        with self._cv:
            key = os.path.normcase(os.path.normpath(job.path))
            if key not in self._jobs:          # 期间又提交过同一路径 ⇒ 那一个已经在排了
                job.not_before = not_before
                self._jobs[key] = job
            self._cv.notify_all()

    def _process(self, job: _Job) -> None:
        enabled, max_age = self._settings()
        if not enabled:
            self.counts["dropped_disabled"] += 1
            return
        path = job.path
        ext = os.path.splitext(path)[1].lower()
        try:
            st = os.stat(path)
        except FileNotFoundError:
            self.counts["vanished"] += 1
            return
        except OSError as exc:                  # 网络盘掉线之类：这次不打，记一笔
            self._note_failure(f"stat:{os.path.dirname(path)}", f"{type(exc).__name__}: {exc}")
            return
        age = float(self._clock()) - st.st_mtime
        settle = SETTLE_S.get(ext, 3.0)
        if age < settle:
            self.counts["deferred"] += 1
            self._requeue(job, self._mono() + (settle - age) + 0.1)
            return
        size, mtime_ns = int(st.st_size), int(st.st_mtime_ns)
        head_cache: dict = {}

        def head() -> str:
            if "v" not in head_cache:
                head_cache["v"] = SC.head_sha256(path)
            return head_cache["v"]

        # 2. 已有的 sidecar 描述的就是这一份字节 ⇒ 不动
        existing = SC.read_sidecar(SC.sidecar_path(path))
        if SC.is_ours(existing) and SC.identity_matches(existing, size=size, mtime_ns=mtime_ns,
                                                        head=head):
            self.counts["same"] += 1
            self._remember(_identity_key(path, size, mtime_ns), existing)
            return

        # 3. 这个身份已经有记录 ⇒ 原样补写（副本）
        ident = _identity_key(path, size, mtime_ns)
        store = self._store()
        payload = self._lru.get(ident) or self._find_central(store, path, size, mtime_ns, head)
        reused = payload is not None
        if reused:
            self.counts["reused"] += 1
        else:
            payload = self._compute(path, size, mtime_ns, head(), st.st_mtime, job, max_age, store)
            if not payload.get("values") and not payload.get("window"):
                self.counts["empty"] += 1      # 什么都不知道：不写空记录
                return
            self.counts["computed"] += 1

        # 5. sidecar（目录冷却中就只写中央记录）
        d = os.path.normcase(os.path.dirname(os.path.abspath(path)))
        status, spath, err = "cooldown", "", ""
        if self._cooldown.get(d, 0.0) > self._mono():
            self.counts["cooldown_skipped"] += 1
        else:
            status, spath, err = SC.write_sidecar(path, payload, size=size, mtime_ns=mtime_ns,
                                                  head=head)
            if status in ("written", "superseded"):
                self.counts[status] += 1
                self._announce(spath)
            elif status == "same":
                self.counts["same"] += 1
            elif status == "foreign":
                self.counts["foreign"] += 1
                self._note_failure(f"foreign:{spath}", err)
            else:
                self.counts["failed_write"] += 1
                self._cooldown[d] = self._mono() + DIR_COOLDOWN_S
                self._note_failure(f"dir:{d}", f"{spath}: {err}")
        if not reused and store is not None:
            ok = store.put_file_tag(
                name=os.path.basename(path), size=size, mtime_ns=mtime_ns, tagged_path=path,
                tagged_at=float(self._clock()), payload=json.dumps(payload, ensure_ascii=False),
                sidecar_path=spath if status in ("written", "superseded", "same") else None,
                sidecar_error=None if status in ("written", "superseded", "same") else (err or status))
            self.counts["central_written" if ok else "central_failed"] += 1
            if not ok:
                self._note_failure("central", "could not write the central env-tag record")
        self._remember(ident, payload)

    def _compute(self, path, size, mtime_ns, head_sha, mtime, job: _Job, max_age, store) -> dict:
        from mast.envhistory.filetags.acq import acquisition_window
        from mast.envhistory.filetags.snapshot import build_record

        sensors = None
        if self._meta_getter is not None:
            try:
                sensors = self._meta_getter()
            except Exception:  # noqa: BLE001
                sensors = None
        latest: dict = {}
        if self._latest_getter is not None:
            try:
                latest = dict(self._latest_getter() or {})
            except Exception:  # noqa: BLE001
                latest = {}
        return build_record(
            data_path=path, size=size, mtime_ns=mtime_ns, head_sha256=head_sha,
            acq=acquisition_window(path, mtime=mtime), sensors=sensors, latest=latest,
            ring=self._ring, store=store, max_age_s=max_age, origin=job.origin,
            observed_at=job.observed_at, now=float(self._clock()), host=self._host,
            mast_version=self._mast_version)

    def _find_central(self, store, path, size, mtime_ns, head) -> dict | None:
        if store is None:
            return None
        try:
            rows = store.find_file_tags(os.path.basename(path), size)
        except Exception:  # noqa: BLE001
            return None
        exact = [r for r in rows if int(r.get("mtime_ns") or 0) == int(mtime_ns)]
        for r in exact + [r for r in rows if r not in exact]:
            try:
                doc = json.loads(r.get("payload") or "")
            except (TypeError, ValueError):
                continue
            if SC.is_ours(doc) and SC.identity_matches(doc, size=size, mtime_ns=mtime_ns, head=head):
                return doc
        return None

    def _remember(self, ident: tuple, payload: dict) -> None:
        self._lru[ident] = payload
        self._lru.move_to_end(ident)
        while len(self._lru) > LRU_MAX:
            self._lru.popitem(last=False)

    def _announce(self, spath: str) -> None:
        d = os.path.dirname(spath)
        key = os.path.normcase(d)
        if key in self._announced:
            return
        self._announced.add(key)
        logger.info("环境标签：开始在 %s 写 sidecar（MAST 的环境记录：温度、磁场、真空……；数据文件不动，"
                    "删掉这个文件夹只丢这些记录）", d)

    def _note_failure(self, key: str, msg: str) -> None:
        """首次大声，之后同类至多每分钟一条（照抄 ingest_sink._note_failure 的纪律）。"""
        self.last_error = msg
        st = self._fail_log.setdefault(key, [0, 0.0])
        st[0] += 1
        now = self._mono()
        if st[0] == 1:
            st[1] = now
            logger.warning("环境标签：%s", msg)
        elif now - st[1] >= _FAIL_QUIET_S:
            st[1] = now
            logger.warning("环境标签：仍在失败（累计 %d 次）：%s", st[0], msg)

    # ── 状态 ──────────────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._cv:
            pending = len(self._jobs)
            alive = bool(self._thread is not None and self._thread.is_alive())
        out = {"enabled": self.enabled_cached, "max_age_s": self.max_age_cached,
               "pending": pending, "worker_alive": alive, "last_error": self.last_error,
               "cooling_dirs": sum(1 for v in self._cooldown.values() if v > self._mono()),
               **self.counts}
        try:
            out["ring"] = self._ring.stats() if self._ring is not None else None
        except Exception:  # noqa: BLE001
            out["ring"] = None
        return out


# ── 文件监视用：一次遍历收三种扩展名 ────────────────────────────────────


def _skip_dir_names() -> set[str]:
    try:
        from mast.webui.scan_preview import _NON_SCAN_DIR_NAMES
        base = {n.lower() for n in _NON_SCAN_DIR_NAMES}
    except Exception:  # noqa: BLE001
        base = {"env", "signals", "monitors"}
    return base | {".mast", "_gallery", "__pycache__", ".git"}


def walk_data_files(root) -> list[tuple[str, tuple[int, int]]]:
    """``[(路径, (mtime_ns, size))]``：``root`` 下全部 ``.sxm/.dat/.3ds``。只 stat，不读文件。"""
    out: list[tuple[str, tuple[int, int]]] = []
    skip = _skip_dir_names()
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name.lower() not in skip:
                            stack.append(e.path)
                        continue
                    if os.path.splitext(e.name)[1].lower() not in SUFFIXES:
                        continue
                    st = e.stat()
                except OSError:
                    continue
                out.append((e.path, (int(st.st_mtime_ns), int(st.st_size))))
    return out


# ── 进程单例 ────────────────────────────────────────────────────────────

_TAGGER: FileEnvTagger | None = None
_TAGGER_LOCK = threading.Lock()


def get_tagger() -> FileEnvTagger | None:
    return _TAGGER


def set_tagger(tagger: FileEnvTagger | None) -> None:
    global _TAGGER
    with _TAGGER_LOCK:
        _TAGGER = tagger


__all__ = ["FileEnvTagger", "SUFFIXES", "SETTLE_S", "QUEUE_MAX", "DIR_COOLDOWN_S",
           "walk_data_files", "get_tagger", "set_tagger"]
