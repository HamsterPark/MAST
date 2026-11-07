"""命令 journal（契约 §5.3「幂等」）：``request_id → 状态/结果``，JSONL，保留最近 1 万条。

每次状态变化追加一行；重启时按行回放，最后一行为准。重启前还在 ``running`` 的条目改判
``lost_on_restart``（新进程 = 新 boot_id，旧作业的结局谁也不知道）—— 枢纽再查它时如实回答，
绝不重放。行数超过上限两倍时整体重写一次（原子替换）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

JOURNAL_KEEP = 10000


class Journal:
    def __init__(self, path: str | os.PathLike | None, keep: int = JOURNAL_KEEP) -> None:
        self.path = Path(path) if path else None
        self.keep = int(keep)
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self._lines = 0
        self._lock = threading.Lock()
        self.lost_on_load: list[str] = []
        if self.path is not None:
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    self._lines += 1
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    cid = str(row.get("id") or "")
                    if not cid:
                        continue
                    self._entries[cid] = row
                    self._entries.move_to_end(cid)
        except FileNotFoundError:
            return
        except OSError:
            return
        while len(self._entries) > self.keep:
            self._entries.popitem(last=False)
        for cid, row in list(self._entries.items()):
            if row.get("state") == "running":
                row = dict(row, state="lost_on_restart", t=time.time())
                self._entries[cid] = row
                self.lost_on_load.append(cid)
                self._append(row)

    def _append(self, row: dict) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._lines += 1
        if self._lines > 2 * self.keep:
            self._compact()

    def _compact(self) -> None:
        assert self.path is not None
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for row in self._entries.values():
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._lines = len(self._entries)

    def get(self, cid: str) -> dict | None:
        with self._lock:
            row = self._entries.get(cid)
            return dict(row) if row else None

    def put(self, cid: str, **fields) -> dict:
        with self._lock:
            row = dict(self._entries.get(cid) or {"id": cid})
            row.update(fields)
            row["t"] = time.time()
            self._entries[cid] = row
            self._entries.move_to_end(cid)
            while len(self._entries) > self.keep:
                self._entries.popitem(last=False)
            self._append(row)
            return dict(row)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


__all__ = ["Journal", "JOURNAL_KEEP"]
