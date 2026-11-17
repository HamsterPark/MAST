# -*- coding: utf-8 -*-
"""图库配置：要索引哪些数据根（设计文档 D2 / D4）。

**显式配置，不隐式扫描。** 打开页面不会遍历任何目录；:func:`suggest_roots` 只给建议，
点一下才加。几十 GB 的数据树不该因为有人点开一个标签页就被遍历，测试也不会因此误扫
真实数据（陷阱 T3）。

根名是每个条目 id 的第一段（``<根名>/<相对路径>``）。数据搬家时改 ``path`` 不改 ``name``，
标记跟着走。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from mast.i18n import tr
from mast.gallery import paths as _paths

logger = logging.getLogger(__name__)

CONFIG_VERSION = 1
MAX_WORKERS = 16

#: 自动增量更新的间隔（分钟）。下限 2：服务端另有 1 分钟的「两次构建最小间隔」，间隔不该比它还短；
#: 上限 12 h：再长就不如手动点。
AUTO_INTERVAL_DEFAULT_MIN = 10
AUTO_INTERVAL_MIN = 2
AUTO_INTERVAL_MAX = 720


def clamp_interval(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = AUTO_INTERVAL_DEFAULT_MIN
    return min(AUTO_INTERVAL_MAX, max(AUTO_INTERVAL_MIN, n))


def default_workers() -> int:
    return min(4, max(1, (os.cpu_count() or 2) // 2))


@dataclass(frozen=True)
class RootSpec:
    name: str
    path: str
    enabled: bool = True


@dataclass
class GalleryConfig:
    roots: list[RootSpec] = field(default_factory=list)
    workers: int = field(default_factory=default_workers)
    #: 自动增量更新：默认开。没有这个键的旧配置文件也按「开」读。
    auto_build: bool = True
    auto_interval_min: int = AUTO_INTERVAL_DEFAULT_MIN


# ── 读写 ───────────────────────────────────────────────────────────────


def load_config(lay: _paths.Layout | None = None) -> GalleryConfig:
    """读配置。文件不存在 ⇒ 空根列表 + 默认 workers。**不创建任何东西。**"""
    lay = lay or _paths.layout()
    raw = _paths.read_json(lay.config, None)
    if raw is None:
        if lay.config.exists():
            logger.warning("gallery config unreadable, treating as empty: %s", lay.config)
        return GalleryConfig()
    if not isinstance(raw, dict):
        return GalleryConfig()
    roots: list[RootSpec] = []
    for r in raw.get("roots") or []:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "").strip()
        path = str(r.get("path") or "").strip()
        if name and path:
            roots.append(RootSpec(name=name, path=path, enabled=bool(r.get("enabled", True))))
    try:
        workers = int(raw.get("workers"))
    except (TypeError, ValueError):
        workers = default_workers()
    return GalleryConfig(roots=roots, workers=min(MAX_WORKERS, max(1, workers)),
                         auto_build=bool(raw.get("auto_build", True)),
                         auto_interval_min=clamp_interval(raw.get("auto_interval_min")))


def save_config(cfg: GalleryConfig, lay: _paths.Layout | None = None) -> None:
    lay = lay or _paths.layout()
    _paths.ensure_state_dir(lay)
    _paths.atomic_write_json(lay.config, {
        "version": CONFIG_VERSION,
        "roots": [asdict(r) for r in cfg.roots],
        "workers": int(min(MAX_WORKERS, max(1, cfg.workers))),
        "auto_build": bool(cfg.auto_build),
        "auto_interval_min": clamp_interval(cfg.auto_interval_min),
    }, indent=1)


# ── 根名与路径校验 ─────────────────────────────────────────────────────

_NAME_OK = re.compile(r"^[A-Za-z0-9_.\-㐀-鿿豈-﫿]+$")
_WIN_RESERVED = {"con", "prn", "aux", "nul",
                 *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def validate_name(name: str) -> str | None:
    """合法返回 None，否则返回原因（中文，给操作员看）。

    根名会成为 ``thumbs/<根名>/`` 目录名与 URL 的一段，所以除了 D4 的字符集，还要避开
    Windows 保留设备名与结尾的点（Windows 会悄悄吃掉结尾的点）。"""
    if not name:
        return tr("根名为空", "Root name is empty")
    if name[0] in "_.":
        return tr(f"根名「{name}」不能以 _ 或 . 开头", f"Root name '{name}' cannot start with _ or .")
    if name.endswith("."):
        return tr(f"根名「{name}」不能以 . 结尾", f"Root name '{name}' cannot end with .")
    if not _NAME_OK.match(name):
        return tr(f"根名「{name}」只能含字母、数字、_ . - 与中文", f"Root name '{name}' may only contain letters, digits, _ . - and Chinese characters")
    if name.lower() in _WIN_RESERVED:
        return tr(f"根名「{name}」是 Windows 保留名", f"Root name '{name}' is a reserved Windows name")
    return None


def derive_name(path: str) -> str:
    """从目录名派生一个合法根名。"""
    base = os.path.basename(os.path.normpath(path)) or "root"
    base = re.sub(r"[^A-Za-z0-9_.\-㐀-鿿豈-﫿]+", "_", base)
    base = base.lstrip("_.").rstrip(".")
    if not base:
        base = "root"
    if base.lower() in _WIN_RESERVED:
        base = base + "_data"
    return base


def _norm(p: str | Path) -> str:
    try:
        return os.path.normcase(os.path.realpath(str(p)))
    except OSError:
        return os.path.normcase(os.path.abspath(str(p)))


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


@dataclass(frozen=True)
class RootMerge:
    """规整时被并进另一个根的数据根（嵌套，或同一目录填了两遍）。

    ``into``（路径 ``into_path``）已经覆盖 ``path`` 下的全部文件；两个都遍历的话，这些文件会各有
    两个 id（``<根名>/<相对路径>``）。``rel`` 是 ``path`` 相对 ``into_path`` 的路径（``/`` 分隔，
    同一目录为 ``""``）：原来挂在 ``<旧根名>/<x>`` 的标记，以后属于 ``<into>/<rel>/<x>``
    （见 :func:`mast.gallery.marks.rekey_prefix`）。``note`` 是给操作员看的一句说明。
    """

    path: str
    into: str
    into_path: str
    rel: str
    note: str


def normalise_roots(inputs: list[dict], *, merges: list[RootMerge] | None = None
                    ) -> tuple[list[RootSpec], list[str]]:
    """校验并规整一组根。``(roots, errors)``；有错误时调用方不应写盘。

    规则：路径必须是已存在的目录，统一成绝对路径；不许落在图库状态目录里；显式给的根名
    必须合法且不重复；没给名字的从目录名派生并去重（不抢显式名字）。

    **重复与嵌套自动合并，不报错**（原先两样都报错）：
    * 嵌套的两个**启用的**根会让同一个文件得到两个 id，所以只留外层那个 —— 它已经覆盖内层的
      全部文件。停用的根不被遍历，不参与嵌套判断。
    * 同一目录填了两遍只留第一个；任何一个启用就算启用，名字取第一个显式给的。
    每一次合并记进 ``merges``（调用方传一个空列表接收），带一句说明；被并掉的根若原先存过、
    挂着标记，调用方用 ``RootMerge.rel`` 把标记搬到新 id 下。
    """
    errors: list[str] = []
    explicit = {str(r.get("name") or "").strip().casefold()
                for r in inputs if isinstance(r, dict) and str(r.get("name") or "").strip()}
    try:
        state_key = _norm(_paths.state_dir())
    except Exception:  # noqa: BLE001 — 解析不了状态目录不该挡住配置
        state_key = None

    # 1. 逐个校验，留下候选（输入顺序）。
    cands: list[dict] = []
    for i, r in enumerate(inputs, 1):
        if not isinstance(r, dict):
            errors.append(tr(f"第 {i} 个数据根格式不对", f"Data root #{i} has an invalid format"))
            continue
        raw_path = str(r.get("path") or "").strip()
        if not raw_path:
            errors.append(tr(f"第 {i} 个数据根没有填路径", f"Data root #{i} has no path"))
            continue
        ap = os.path.abspath(os.path.expanduser(raw_path))
        if not os.path.isdir(ap):
            errors.append(tr(f"不是已存在的目录：{raw_path}", f"Not an existing directory: {raw_path}"))
            continue
        key = _norm(ap)
        if state_key and _within(key, state_key):
            errors.append(tr(f"数据根不能放在图库状态目录里：{raw_path}", f"A data root cannot be inside the gallery state directory: {raw_path}"))
            continue
        name = str(r.get("name") or "").strip()
        if name:
            why = validate_name(name)
            if why:
                errors.append(why)
                continue
        cands.append({"raw": raw_path, "ap": ap, "key": key, "name": name,
                      "enabled": bool(r.get("enabled", True))})

    # 2. 同一目录填了两遍：并进第一个。
    kept: list[dict] = []
    first_of: dict[str, dict] = {}
    drops: list[tuple[dict, dict, str]] = []          # (被并掉的, 并进的, rel)
    for c in cands:
        first = first_of.get(c["key"])
        if first is None:
            first_of[c["key"]] = c
            kept.append(c)
            continue
        first["enabled"] = first["enabled"] or c["enabled"]
        if not first["name"] and c["name"]:
            first["name"] = c["name"]
        drops.append((c, first, ""))

    # 3. 嵌套：启用的根从外到内看，落在已留下的启用根里的并进去。
    walked: list[dict] = []
    nested: set[int] = set()
    for c in sorted((c for c in kept if c["enabled"]), key=lambda c: len(c["key"])):
        into = next((w for w in walked if _within(c["key"], w["key"])), None)
        if into is None:
            walked.append(c)
            continue
        # 真实大小写的相对路径：遍历时 id 里的子目录名来自文件系统，不来自用户敲的大小写。
        try:
            rel = os.path.relpath(os.path.realpath(c["ap"]), os.path.realpath(into["ap"]))
        except ValueError:                             # 不同盘符（经 realpath 解开的联接点）
            rel = os.path.relpath(c["ap"], into["ap"])
        drops.append((c, into, rel.replace("\\", "/")))
        nested.add(id(c))
    kept = [c for c in kept if id(c) not in nested]

    # 4. 起名（输入顺序）。
    out: list[RootSpec] = []
    taken: set[str] = set()
    for c in kept:
        name = c["name"]
        if name:
            if name.casefold() in taken:
                errors.append(tr(f"根名重复：{name}", f"Duplicate root name: {name}"))
                continue
        else:
            base = derive_name(c["ap"])
            name, n = base, 2
            while name.casefold() in taken or name.casefold() in explicit:
                name = f"{base}_{n}"
                n += 1
        taken.add(name.casefold())
        c["final"] = name
        out.append(RootSpec(name=name, path=c["ap"], enabled=c["enabled"]))

    if merges is not None:
        for c, into, rel in drops:
            into_name = into.get("final") or into["name"] or derive_name(into["ap"])
            if rel:
                note = tr(f"「{c['raw']}」在数据根「{into_name}」里面，已经包含在内，自动去掉了",
                          f"'{c['raw']}' is inside data root '{into_name}', which already covers it; removed")
            else:
                note = tr(f"「{c['raw']}」填了两遍，已合并成一个",
                          f"'{c['raw']}' was entered twice; merged into one")
            merges.append(RootMerge(path=c["ap"], into=into_name, into_path=into["ap"],
                                    rel=rel, note=note))
    return out, errors


# ── 建议 ───────────────────────────────────────────────────────────────

_Y4 = re.compile(r"^\d{4}$")
_YM6 = re.compile(r"^\d{6}$")
_YMD8 = re.compile(r"^\d{8}$")


def suggest_roots(lay: _paths.Layout | None = None) -> list[tuple[str, str]]:
    """值得加进来的目录。只给**现在存在**的；已经配过的不再建议。**只读。**

    会话目录只从本进程已经记录过的 ``scan_registry.known_scan_dirs()`` 里拿 ——
    **绝不为了给建议去连仪器**。Nanonis 默认的会话目录形如 ``.../YYYY/YYYYMM/YYYYMMDD``，
    这时上溯三级就是整棵数据树的根，那才是图库该索引的东西。
    """
    lay = lay or _paths.layout()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        seen.update(_norm(r.path) for r in load_config(lay).roots)
    except Exception:  # noqa: BLE001
        pass

    def add(p: str | Path | None, why: str) -> None:
        if not p:
            return
        try:
            if not os.path.isdir(p):
                return
        except OSError:
            return
        k = _norm(p)
        if k in seen:
            return
        seen.add(k)
        out.append((str(p), why))

    try:
        from mast.core.scan_registry import known_scan_dirs

        for d in known_scan_dirs():
            p = Path(d)
            if (_YMD8.match(p.name) and _YM6.match(p.parent.name)
                    and _Y4.match(p.parent.parent.name)):
                add(p.parent.parent.parent,
                    tr(f"Nanonis 数据树的根（会话目录 {p} 上溯三级）",
                       f"Root of the Nanonis data tree (three levels above session directory {p})"))
            add(p, tr("Nanonis 会话目录（本次运行里记录过）", "Nanonis session directory (recorded during this run)"))
    except Exception:  # noqa: BLE001 — 建议是锦上添花
        pass
    try:
        from mast._runtime_paths import project_root

        add(project_root() / "working-sessions",
                tr("MAST 扫描技能的默认落盘目录", "Default save directory of the MAST scan skills"))
    except Exception:  # noqa: BLE001
        pass
    try:
        from mast.core.experiment_paths import experiment_root

        add(experiment_root(),
            tr("实验根（自动拷贝进实验文件夹的副本；与原文件同时加入时会折叠成一条）",
               "Experiment root (copies automatically placed in experiment folders; added together with the originals they fold into one entry)"))
    except Exception:  # noqa: BLE001
        pass
    return out
