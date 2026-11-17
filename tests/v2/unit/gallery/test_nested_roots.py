"""数据根嵌套 / 重复：自动合并，不再报错（2026-11-12 起）。

嵌套的两个启用的根会让同一个文件得到两个 id（``<根名>/<相对路径>``），所以只留外层那个；
同一目录填两遍只留第一个。被并掉的根若原先存过、挂着标记，标记搬到它的文件在外层根下的新 id ——
最后一条用真构建核对：搬过去的键正是构建给这个文件的 id。
"""
from __future__ import annotations

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mast.api.context import AppContext
from mast.api.routes.gallery import router
from mast.gallery import config as gcfg
from mast.gallery import marks as gmarks


def _client() -> TestClient:
    app = FastAPI()
    app.state.ctx = AppContext()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_a_nested_root_is_folded_into_the_outer_one(tmp_path):
    outer = tmp_path / "Data"
    inner = outer / "Nanonis" / "2026"
    inner.mkdir(parents=True)
    merges: list = []
    roots, errors = gcfg.normalise_roots([{"path": str(inner)}, {"path": str(outer)}], merges=merges)
    assert errors == []
    assert [(r.name, r.path) for r in roots] == [("Data", str(outer))]
    assert len(merges) == 1
    m = merges[0]
    assert (m.into, m.rel, m.path) == ("Data", "Nanonis/2026", str(inner))
    assert "Data" in m.note


def test_three_levels_keep_only_the_outermost(tmp_path):
    a = tmp_path / "A"
    b = a / "B"
    c = b / "C"
    c.mkdir(parents=True)
    merges: list = []
    roots, errors = gcfg.normalise_roots([{"path": str(b)}, {"path": str(c)}, {"path": str(a)}],
                                         merges=merges)
    assert errors == [] and [r.name for r in roots] == ["A"]
    assert sorted((m.rel, m.into) for m in merges) == [("B", "A"), ("B/C", "A")]


def test_the_same_directory_twice_is_merged(tmp_path):
    d = tmp_path / "SPM"
    d.mkdir()
    merges: list = []
    roots, errors = gcfg.normalise_roots(
        [{"path": str(d), "enabled": False}, {"name": "Main", "path": str(d), "enabled": True}],
        merges=merges)
    assert errors == [] and len(roots) == 1
    assert (roots[0].name, roots[0].enabled) == ("Main", True), "名字取第一个显式给的；任一启用即启用"
    assert len(merges) == 1 and merges[0].rel == ""


def test_disabled_roots_take_no_part_in_nesting(tmp_path):
    """停用的根不被遍历，不会让文件有两个 id：两个都留着，不合并。"""
    a = tmp_path / "A"
    b = a / "B"
    b.mkdir(parents=True)
    for outer_on, inner_on in ((False, True), (True, False)):
        merges: list = []
        roots, errors = gcfg.normalise_roots(
            [{"path": str(a), "enabled": outer_on}, {"path": str(b), "enabled": inner_on}],
            merges=merges)
        assert errors == [] and len(roots) == 2 and merges == [], (outer_on, inner_on)


def test_siblings_and_prefix_lookalikes_are_not_nested(tmp_path):
    """``D:\\Data`` 与 ``D:\\Data2`` 名字前缀相同，但不是嵌套。"""
    x = tmp_path / "Data"
    y = tmp_path / "Data2"
    x.mkdir()
    y.mkdir()
    merges: list = []
    roots, errors = gcfg.normalise_roots([{"path": str(x)}, {"path": str(y)}], merges=merges)
    assert errors == [] and len(roots) == 2 and merges == []


def test_the_api_saves_the_merged_list_and_says_why(gallery_dir, tmp_path):
    outer = tmp_path / "Data"
    inner = outer / "sub"
    inner.mkdir(parents=True)
    body = _client().post("/api/gallery/config", json={
        "roots": [{"path": str(outer)}, {"path": str(inner)}]}).json()
    assert body["ok"] is True, body
    assert [r["path"] for r in body["roots"]] == [str(outer)]
    assert body["detail"] and str(inner) in body["detail"]


def test_rekey_prefix_without_matching_marks_writes_nothing(gallery_dir):
    assert gmarks.rekey_prefix("Nope/", "Other/x/") == 0
    assert not (gallery_dir / "marks.json").exists()


def test_marks_of_a_merged_root_follow_its_files_to_their_new_ids(gallery_dir, tmp_path, synth, t0_ns):
    data = tmp_path / "Data"
    run = data / "Run1"
    synth.sxm(run / "d" / "x.sxm", np.random.default_rng(0).normal(0, 1e-11, (32, 32)), mtime_ns=t0_ns)
    synth.sxm(data / "other" / "y.sxm", np.random.default_rng(1).normal(0, 1e-11, (32, 32)),
              mtime_ns=t0_ns + 10**9)
    c = _client()

    # 1. Run1 存成一个独立的根，操作员打了分、建了系列、标了目录，另一条标记挂在它的帧上
    assert c.post("/api/gallery/config", json={"roots": [{"name": "Run1", "path": str(run)}]}).json()["ok"]
    gmarks.patch_marks({
        "items": {"Run1/d/x.sxm": {"r": 2, "note": "best", "ts": 1},
                  "Elsewhere/s.dat": {"note": "spectrum", "ts": 1,
                                      "anchor": {"id": "Run1/d/x.sxm", "rel": "prev"}}},
        "series": {"s1": {"name": "S", "ids": ["Run1/d/x.sxm"], "ts": 1}},
        "days": {"Run1/d": {"done": True, "ts": 1}},
    })

    # 2. 再加上外层的 Data：Run1 并进 Data，标记跟着走
    body = c.post("/api/gallery/config", json={"roots": [
        {"name": "Run1", "path": str(run)}, {"name": "Data", "path": str(data)}]}).json()
    assert body["ok"] is True, body
    assert [r["name"] for r in body["roots"]] == ["Data"]
    assert "Run1" in body["detail"] and "Data" in body["detail"]

    doc = gmarks.load_marks()
    new_id = "Data/Run1/d/x.sxm"
    assert "Run1/d/x.sxm" not in doc["items"]
    assert doc["items"][new_id]["note"] == "best" and doc["items"][new_id]["r"] == 2
    assert "Run1/d/x.sxm" in doc["tomb"], "旧键要有墓碑，迟到的旧改动写不回去"
    assert doc["items"]["Elsewhere/s.dat"]["anchor"]["id"] == new_id
    assert doc["series"]["s1"]["ids"] == [new_id]
    assert "Data/Run1/d" in doc["days"] and "Run1/d" not in doc["days"]

    # 一条迟到的旧改动（搬之前排进客户端队列的）不能把旧键写回来
    gmarks.patch_marks({"items": {"Run1/d/x.sxm": {"r": -1, "ts": 2}}})
    assert "Run1/d/x.sxm" not in gmarks.load_marks()["items"]

    # 3. 真构建：这个文件拿到的 id 就是标记搬去的那个
    from mast.gallery import service

    assert c.post("/api/gallery/build", json={"force": False}).json()["degraded"] is False
    assert service.wait(120)
    by, _dirs = gmarks.items_meta()
    assert new_id in by, sorted(by)
    assert "Data/other/y.sxm" in by
