# -*- coding: utf-8 -*-
"""sidecar 文件：``<数据文件所在目录>/.mast/envtags/<完整文件名>.json``。

* **完整文件名**：``foo.sxm`` 与 ``foo.dat`` 各有各的 sidecar；与光学站的 ``foo.json``
  （``path.with_suffix(".json")``）也不撞。
* **不覆盖**：临时文件写好之后，Windows 用 ``os.rename``（目标存在即失败）、POSIX 用
  ``os.link`` 落地。已存在且描述的就是这个文件 ⇒ 一字不动；已存在、是本 schema、但描述的是
  另一个文件（Nanonis 重用了编号）⇒ 先改名为 ``<文件名>.superseded-<旧 mtime_ns>.json`` 留存，
  再写新的；已存在但**不是**本 schema 的文件 ⇒ 不碰。
* 临时文件名 ``.tmp-<pid>-<tid>``，**不含 ``.part-``**：启动时对实验根的清扫会删 ``*.part-*``。
* 数据文件只读打开（算 ``head_sha256``），永远不写。

身份核对：size 必须相等；mtime 相差 ≤ 2 s 即认（FAT/exFAT 只有 2 s 精度）；
超出时比数据文件前 64 KiB 的 sha256 —— 拷贝工具没保留 mtime 的副本也认得出。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

SCHEMA = "mast.envtags/1"
SUBDIR = (".mast", "envtags")
HEAD_BYTES = 64 * 1024
MTIME_TOLERANCE_NS = 2_000_000_000
TMP_MARK = ".tmp-"


def sidecar_dir(data_path) -> Path:
    p = Path(data_path)
    return p.parent.joinpath(*SUBDIR)


def sidecar_path(data_path) -> Path:
    p = Path(data_path)
    return sidecar_dir(p) / f"{p.name}.json"


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        isjunction = getattr(os.path, "isjunction", None)      # Python 3.12+
        return bool(isjunction(path)) if isjunction is not None else False
    except OSError:
        return True                                            # 看不清就当是：宁可不写


def contained_reason(data_path) -> str:
    """sidecar 目录是不是真的在数据目录下面。是 ⇒ 空串；不是 ⇒ 原因。

    ``.mast`` 或 ``.mast/envtags`` 是链接、联接点时，往里写的 sidecar 会落到它指向的别处去 ——
    拒写，调用方只写中央记录。数据目录本身经由联接点到达不要紧：两边按真实路径比。"""
    p = Path(data_path)
    cur = p.parent
    for part in SUBDIR:
        cur = cur / part
        if _is_link(cur):
            return f"{cur} is a link or junction; not writing a sidecar through it"
    try:
        real_dir = os.path.realpath(p.parent)
        real_side = os.path.realpath(sidecar_dir(p))
    except (OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    expect = os.path.join(real_dir, *SUBDIR)
    if os.path.normcase(real_side) != os.path.normcase(expect):
        return f"the sidecar directory resolves outside the data directory ({real_side})"
    return ""


def head_sha256(path, n: int = HEAD_BYTES) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:          # 只读
        h.update(fh.read(int(n)))
    return h.hexdigest()


def read_sidecar(path) -> dict | None:
    """读一个 sidecar；不存在、读不了、不是 JSON 对象 ⇒ None。不看 schema（调用方看）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def is_ours(doc: Any) -> bool:
    return isinstance(doc, dict) and doc.get("schema") == SCHEMA and isinstance(doc.get("file"), dict)


def identity_matches(doc: dict, *, size: int, mtime_ns: int,
                     head: Callable[[], str] | str | None = None) -> bool:
    """这份记录描述的是不是这一份字节。``head`` 可以是值，也可以是惰性算它的函数。"""
    f = doc.get("file") if isinstance(doc, dict) else None
    if not isinstance(f, dict):
        return False
    try:
        if int(f.get("size")) != int(size):
            return False
        d_ns = abs(int(f.get("mtime_ns")) - int(mtime_ns))
    except (TypeError, ValueError):
        return False
    if d_ns <= MTIME_TOLERANCE_NS:
        return True
    want = f.get("head_sha256")
    if not want or head is None:
        return False
    try:
        got = head() if callable(head) else head
    except OSError:
        return False
    return bool(got) and got == want


def dump(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False).encode("utf-8")


def _tmp_for(target: Path) -> Path:
    return target.with_name(f"{target.name}{TMP_MARK}{os.getpid()}-{threading.get_ident():x}")


def _place_no_clobber(src: Path, dst: Path) -> bool:
    """把 ``src`` 放到 ``dst``，``dst`` 已存在就不放。放成了返回 True。"""
    if os.name == "nt":
        try:
            os.rename(src, dst)            # Windows：目标存在 ⇒ FileExistsError
            return True
        except FileExistsError:
            return False
    try:
        os.link(src, dst)                  # POSIX：目标存在 ⇒ FileExistsError
    except FileExistsError:
        return False
    except OSError:
        # 不支持硬链接的文件系统（FAT、部分网络盘）：先看一眼再改名，留一个极小的竞态窗口
        if dst.exists():
            return False
        os.rename(src, dst)
        return True
    try:
        os.unlink(src)
    except OSError:
        pass
    return True


def write_no_clobber(target, data: bytes) -> bool:
    """原子、不覆盖地写一个文件。写成返回 True；目标已存在返回 False（目标一字不动）。
    文件系统错误照常抛 ``OSError``（调用方记账、冷却）。"""
    t = Path(target)
    t.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(t)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        return _place_no_clobber(tmp, t)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _supersede(existing: Path, old_doc: dict) -> Path | None:
    """把一份描述别的文件的旧 sidecar 改名留存（不删）。改不成返回 None。"""
    try:
        old_ns = int((old_doc.get("file") or {}).get("mtime_ns"))
    except (TypeError, ValueError):
        old_ns = 0
    base = existing.name[:-len(".json")] if existing.name.endswith(".json") else existing.name
    for i in range(1, 50):
        suffix = f".superseded-{old_ns}" + ("" if i == 1 else f"-{i}")
        dest = existing.with_name(f"{base}{suffix}.json")
        try:
            if _place_no_clobber(existing, dest):
                return dest
        except OSError:
            return None
    return None


def write_sidecar(data_path, payload: dict, *, size: int, mtime_ns: int,
                  head: Callable[[], str] | str | None = None) -> tuple[str, str, str]:
    """写这个数据文件的 sidecar。``(状态, sidecar 路径, 错误)``，永不抛。

    状态：``written`` 新写成 / ``same`` 已有、描述的就是它，没动 / ``superseded`` 旧的（描述别的
    文件）已改名留存、新的写成 / ``foreign`` 路径上是别人的文件，没动 / ``failed`` 文件系统错误，
    或 sidecar 目录不在数据目录下面（``.mast`` 是链接或联接点，见 contained_reason），没写。
    """
    target = sidecar_path(data_path)
    why = contained_reason(data_path)
    if why:
        return "failed", str(target), why
    try:
        data = dump(payload)
    except (TypeError, ValueError) as exc:
        return "failed", str(target), f"{type(exc).__name__}: {exc}"
    try:
        if write_no_clobber(target, data):
            return "written", str(target), ""
        existing = read_sidecar(target)
        if not is_ours(existing):
            return "foreign", str(target), "a file that is not an env-tag sidecar is in the way"
        if identity_matches(existing, size=size, mtime_ns=mtime_ns, head=head):
            return "same", str(target), ""
        if _supersede(target, existing) is None:
            return "failed", str(target), "could not move the stale sidecar aside"
        if write_no_clobber(target, data):
            return "superseded", str(target), ""
        return "failed", str(target), "the sidecar path was taken again while writing"
    except OSError as exc:
        return "failed", str(target), f"{type(exc).__name__}: {exc}"


__all__ = ["SCHEMA", "SUBDIR", "sidecar_dir", "sidecar_path", "head_sha256", "read_sidecar",
           "is_ours", "identity_matches", "dump", "write_no_clobber", "write_sidecar",
           "contained_reason", "MTIME_TOLERANCE_NS"]
