"""扫描发现与图库遍历不进 current_monitor 和点目录。

长期运行后 experiments/current_monitor/segments/<day>/ 里可能有数十万个 .npy；两条遍历每次都
把它列一遍（scan_preview 对默认 8 个扩展名列 24 遍）。这里用合成小树钉住：
  * 跳过 current_monitor 与所有点目录（调用方点名的搜索根不跳）；
  * 其余输出的集合与顺序不变（与旧 glob 实现逐项对照）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from mast.gallery import config as gcfg
from mast.gallery import inventory as inv
from mast.webui import scan_preview as sp


def _touch(path: Path, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _tree(root: Path) -> None:
    now = time.time()
    _touch(root / "exp1" / "samples" / "s1" / "raw" / "sxm" / "a.sxm", now - 10)
    _touch(root / "exp1" / "scans" / "b.dat", now - 20)
    _touch(root / "top.sxm", now - 30)
    _touch(root / "exp1" / "env" / "x.csv", now)                       # 边车：本来就不算
    # 不该进的两类
    _touch(root / "current_monitor" / "segments" / "20261113" / "seg1.sxm", now)
    _touch(root / "current_monitor" / "stray.dat", now)
    _touch(root / "exp1" / ".hidden" / "h.sxm", now)
    _touch(root / ".mast" / "m.sxm", now)


def test_collect_scan_stats_skips_current_monitor_and_dot_dirs(tmp_path):
    _tree(tmp_path)
    names = sorted(p.name for p in sp._collect_scans(str(tmp_path)))
    assert names == ["a.sxm", "b.dat", "top.sxm"], names


def test_collect_scan_stats_does_not_list_the_pruned_directories(tmp_path, monkeypatch):
    """不止结果对：被剪的目录根本没被 scandir 过（这才是省下的 I/O）。"""
    _tree(tmp_path)
    listed: list[str] = []
    real = os.scandir

    def spy(path="."):
        listed.append(os.path.basename(str(path)))
        return real(path)

    monkeypatch.setattr(sp.os, "scandir", spy)
    sp.collect_scan_stats(str(tmp_path))
    assert "current_monitor" not in listed and "segments" not in listed
    assert ".hidden" not in listed and ".mast" not in listed
    assert "exp1" in listed


def test_a_search_dir_the_caller_names_is_still_listed(tmp_path):
    """被剪的是「走进去」的目录；调用方直接点名的根不受影响。"""
    _tree(tmp_path)
    got = [p.name for p in sp._collect_scans(str(tmp_path / "current_monitor"))]
    assert sorted(got) == ["seg1.sxm", "stray.dat"]


def _old_glob_collect(search_dir: str, max_depth: int = sp._MAX_SEARCH_DEPTH):
    """The pre-change implementation, verbatim, as the ordering oracle."""
    wanted = sp._SCAN_EXTS
    p = Path(search_dir)
    files: list[Path] = []
    for depth in range(max_depth + 1):
        for ext in wanted:
            pattern = "/".join(["*"] * depth) + f"/*{ext}" if depth > 0 else f"*{ext}"
            if depth == 0:
                files.extend(p.glob(pattern))
            else:
                files.extend(f for f in p.glob(pattern) if sp._is_scan_file(f))
    seen, out = set(), []
    for f in files:
        k = sp._norm_key(f)
        if k in seen:
            continue
        seen.add(k)
        out.append((f, int(f.stat().st_mtime_ns)))
    out.sort(key=lambda s: s[1], reverse=True)
    return out


def test_result_and_order_match_the_old_glob_walk_on_a_tree_without_pruned_dirs(tmp_path):
    """同一 mtime（并列）时的先后也要一样：旧实现按 (深度, 扩展名, 目录遍历序) 入列。"""
    t = time.time() - 100
    for i, rel in enumerate([
        "a.sxm", "a.dat", "a.3ds", "d1/b.sxm", "d1/b.dat", "d1/d2/c.sxm", "d1/d2/c.txt",
        "d3/d4/d5/e.sxm", "d3/d4/d5/d6/f.dat", "d3/d4/d5/d6/d7/g.sxm", "d3/env/skip.csv",
        "d8/h.SXM", "d8/h2.Dat",
    ]):
        _touch(tmp_path / rel, t)          # 全部同一 mtime：顺序完全由入列序决定
    new = [(s.path, s.mtime_ns) for s in sp.collect_scan_stats(str(tmp_path))]
    old = _old_glob_collect(str(tmp_path))
    assert new == old
    assert len(new) >= 10


def test_gallery_walk_skips_current_monitor_and_dot_dirs(tmp_path):
    root = tmp_path / "EXP"
    _touch(root / "e1" / "scans" / "a.sxm")
    _touch(root / "e1" / "scans" / "z.dat")
    _touch(root / "current_monitor" / "segments" / "20261113" / "s.dat")
    _touch(root / "Current_Monitor" / "t.sxm")
    _touch(root / "e1" / ".cache" / "u.sxm")
    cfg = gcfg.GalleryConfig(roots=[gcfg.RootSpec(name="EXP", path=str(root))])
    files, _scopes = inv.walk_roots(cfg)
    assert [f.id for f in files] == ["EXP/e1/scans/a.sxm", "EXP/e1/scans/z.dat"]


def test_gallery_walk_does_not_list_current_monitor(tmp_path, monkeypatch):
    root = tmp_path / "EXP"
    _touch(root / "e1" / "a.sxm")
    for i in range(5):
        _touch(root / "current_monitor" / "segments" / "d" / f"{i}.npy")
    seen: list[str] = []
    real = os.walk

    def spy(top, *a, **k):
        for dirpath, dirnames, filenames in real(top, *a, **k):
            seen.append(os.path.basename(dirpath))
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(inv.os, "walk", spy)
    cfg = gcfg.GalleryConfig(roots=[gcfg.RootSpec(name="EXP", path=str(root))])
    inv.walk_roots(cfg)
    assert "current_monitor" not in seen and "segments" not in seen
