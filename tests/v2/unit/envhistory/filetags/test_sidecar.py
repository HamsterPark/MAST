# -*- coding: utf-8 -*-
"""sidecar：命名不撞、不覆盖、旧的改名留存、别人的文件不碰、数据文件不动。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from mast.envhistory.filetags import sidecar as SC

NS = 1_800_000_000_000_000_000


def _data(path: Path, body: bytes = b"HEADER\x1a\x04" + b"\x00" * 2000, ns: int = NS) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    os.utime(path, ns=(ns, ns))
    return path


def _payload(path: Path, *, ns: int = NS, value: float = 4.2) -> dict:
    st = path.stat()
    return {"schema": SC.SCHEMA,
            "file": {"name": path.name, "kind": path.suffix[1:], "size": st.st_size,
                     "mtime_ns": ns, "head_sha256": SC.head_sha256(path), "tagged_path": str(path)},
            "values": [{"sensor": "t", "value": value}]}


@pytest.fixture()
def d(tmp_path) -> Path:
    return tmp_path / "data"                    # tmp_path 里另有 _sidecars/，场景放子目录


def test_names_keep_the_whole_file_name_and_live_under_mast_envtags(d):
    a, b = SC.sidecar_path(d / "foo.sxm"), SC.sidecar_path(d / "foo.dat")
    assert a != b and a.name == "foo.sxm.json" and b.name == "foo.dat.json"
    assert a.parent == d / ".mast" / "envtags"
    assert SC.sidecar_path(d / "foo.sxm") != (d / "foo.sxm").with_suffix(".json")   # 光学站的命名


def test_a_new_sidecar_is_written_atomically_and_the_data_file_is_untouched(d):
    p = _data(d / "Topo_0001.sxm")
    before = hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns
    status, spath, err = SC.write_sidecar(p, _payload(p), size=p.stat().st_size, mtime_ns=NS)
    assert (status, err) == ("written", "")
    doc = json.loads(Path(spath).read_text(encoding="utf-8"))
    assert doc["schema"] == SC.SCHEMA and doc["values"][0]["value"] == 4.2
    assert (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) == before
    leftovers = [x.name for x in Path(spath).parent.iterdir() if x.name != Path(spath).name]
    assert leftovers == []                      # 临时文件收拾干净了


def test_the_temp_name_never_contains_part(d):
    tmp = SC._tmp_for(d / ".mast" / "envtags" / "x.sxm.json")
    assert ".part-" not in tmp.name and SC.TMP_MARK in tmp.name


def test_an_existing_sidecar_for_the_same_file_is_left_alone(d):
    p = _data(d / "a.dat")
    SC.write_sidecar(p, _payload(p, value=1.0), size=p.stat().st_size, mtime_ns=NS)
    sp = SC.sidecar_path(p)
    raw, mt = sp.read_bytes(), sp.stat().st_mtime_ns
    status, _, _ = SC.write_sidecar(p, _payload(p, value=2.0), size=p.stat().st_size, mtime_ns=NS)
    assert status == "same"
    assert sp.read_bytes() == raw and sp.stat().st_mtime_ns == mt


def test_a_stale_sidecar_from_a_reused_file_name_is_moved_aside_not_deleted(d):
    p = _data(d / "a.dat", ns=NS)
    SC.write_sidecar(p, _payload(p, ns=NS, value=1.0), size=p.stat().st_size, mtime_ns=NS)
    old_raw = SC.sidecar_path(p).read_bytes()
    new_ns = NS + 3600 * 10**9                  # Nanonis 重用编号：同名、一小时后的新测量
    p = _data(d / "a.dat", body=b"NEW" * 900, ns=new_ns)
    status, spath, _ = SC.write_sidecar(p, _payload(p, ns=new_ns, value=2.0),
                                        size=p.stat().st_size, mtime_ns=new_ns)
    assert status == "superseded"
    assert json.loads(Path(spath).read_text(encoding="utf-8"))["values"][0]["value"] == 2.0
    kept = Path(spath).parent / f"a.dat.superseded-{NS}.json"
    assert kept.read_bytes() == old_raw


def test_someone_elses_file_in_the_way_is_not_touched(d):
    p = _data(d / "a.sxm")
    sp = SC.sidecar_path(p)
    sp.parent.mkdir(parents=True)
    sp.write_text("not ours", encoding="utf-8")
    status, _, err = SC.write_sidecar(p, _payload(p), size=p.stat().st_size, mtime_ns=NS)
    assert status == "foreign" and err
    assert sp.read_text(encoding="utf-8") == "not ours"


def test_a_directory_we_cannot_create_is_a_failure_not_an_exception(d):
    p = _data(d / "a.sxm")
    (d / ".mast").write_text("a file where the folder should be", encoding="utf-8")
    status, _, err = SC.write_sidecar(p, _payload(p), size=p.stat().st_size, mtime_ns=NS)
    assert status == "failed" and err


def test_identity_tolerates_two_seconds_then_falls_back_to_the_head_hash(d):
    p = _data(d / "a.sxm")
    doc = _payload(p)
    size = p.stat().st_size
    assert SC.identity_matches(doc, size=size, mtime_ns=NS + 1_900_000_000)       # exFAT 取整
    assert not SC.identity_matches(doc, size=size + 1, mtime_ns=NS)
    far = NS + 3600 * 10**9                                                        # 拷贝没保留 mtime
    assert not SC.identity_matches(doc, size=size, mtime_ns=far)
    assert SC.identity_matches(doc, size=size, mtime_ns=far, head=lambda: SC.head_sha256(p))
    assert not SC.identity_matches(doc, size=size, mtime_ns=far, head="0" * 64)


# ── .mast 是链接或联接点：拒写──────────────────────────────


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_junctioned_mast_folder_is_refused_and_nothing_lands_where_it_points(d):
    import _winapi

    p = _data(d / "session" / "Topo_0001.sxm")
    outside = d / "elsewhere"
    outside.mkdir(parents=True)
    link = p.parent / ".mast"
    _winapi.CreateJunction(str(outside), str(link))
    try:
        status, _sp, err = SC.write_sidecar(p, _payload(p), size=p.stat().st_size, mtime_ns=NS)
        assert status == "failed" and "junction" in err
        assert list(outside.rglob("*")) == []
    finally:
        os.rmdir(link)                              # 只拆联接点，不碰它指向的目录


def test_a_symlinked_envtags_folder_is_refused(d):
    p = _data(d / "session" / "Topo_0001.sxm")
    outside = d / "elsewhere"
    outside.mkdir(parents=True)
    (p.parent / ".mast").mkdir()
    link = p.parent / ".mast" / "envtags"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    try:
        status, _sp, err = SC.write_sidecar(p, _payload(p), size=p.stat().st_size, mtime_ns=NS)
        assert status == "failed" and "link" in err
        assert list(outside.iterdir()) == []
    finally:
        try:
            os.unlink(link)
        except OSError:
            os.rmdir(link)


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_data_directory_reached_through_a_junction_still_gets_its_sidecar(d):
    """数据目录本身经由联接点到达不要紧：sidecar 落在它真实的目录下面。"""
    import _winapi

    real = d / "real"
    _data(real / "Topo_0001.sxm")
    via = d / "via"
    _winapi.CreateJunction(str(real), str(via))
    try:
        q = via / "Topo_0001.sxm"
        status, _sp, err = SC.write_sidecar(q, _payload(q), size=q.stat().st_size, mtime_ns=NS)
        assert (status, err) == ("written", "")
        assert (real / ".mast" / "envtags" / "Topo_0001.sxm.json").is_file()
    finally:
        os.rmdir(via)
