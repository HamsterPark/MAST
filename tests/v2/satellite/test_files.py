"""文件接收：断点续传、sha 校验、原子改名、落盘布局、.meta.json、路径清洗。"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pytest

from mast.satellite import protocol as P
from mast.satellite.files import FileArchive, FileReceiver, safe_rel_path
from mast.satellite.spe import build_footer_xml, write_spe
from mast.satellite.stm_ring import StmStateRing


def _rx(root: Path, ring=None, ingest=None, stored=None):
    arch = FileArchive(lambda: root)
    rx = FileReceiver(arch, context_fn=lambda sid: {"boot_id": "B", "hub_boot_id": "H",
                                                    "clock": {"offset_ms": 100.0, "delay_ms": 2.0,
                                                              "rtt_ms": 3.0, "samples": 5}},
                      stm_ring=ring, ingest_fn=ingest, on_stored=stored)
    return arch, rx


def _offer(data: bytes, name="a.csv", rel="2026/1019/a.csv", kind="csv", meta=None):
    sha = hashlib.sha256(data).hexdigest()
    return P.parse_to_hub({"t": "file_offer", "file_id": sha, "name": name, "rel_path": rel,
                           "size": len(data), "sha256": sha, "mtime_ms": 1_700_000_000_000,
                           "kind": kind, "meta": meta or {}})


def _chunk(sha, off, b):
    return P.parse_to_hub({"t": "file_chunk", "file_id": sha, "offset": off,
                           "data_b64": base64.b64encode(b).decode()})


def _end(sha):
    return P.parse_to_hub({"t": "file_end", "file_id": sha})


def test_full_transfer_lands_atomically_with_meta(tmp_path):
    root = tmp_path
    ingested, stored = [], []
    arch, rx = _rx(root, ingest=lambda paths, sid: ingested.append((paths, sid)) or "submitted",
                   stored=stored.append)
    data = b"x,y\n" * 1000
    off = _offer(data)
    assert rx.offer("rig1", off) == [{"t": "file_want", "file_id": off.file_id, "offset": 0}]
    for i in range(0, len(data), 1000):
        assert rx.chunk("rig1", _chunk(off.file_id, i, data[i:i + 1000])) == []
    part = root / "satellite/rig1/incoming" / f"{off.file_id}.part"
    assert part.exists()
    out = rx.end("rig1", _end(off.file_id))
    assert out[0]["ok"] is True and out[0]["stored"] == "satellite/rig1/files/2026/1019/a.csv"
    dest = root / out[0]["stored"]
    assert dest.read_bytes() == data and not part.exists()
    assert not (root / "satellite/rig1/incoming" / f"{off.file_id}.offer.json").exists()
    meta = json.loads((dest.parent / "a.csv.meta.json").read_text(encoding="utf-8"))
    assert meta["sat_id"] == "rig1" and meta["boot_id"] == "B"
    assert meta["clock"]["offset_ms"] == 100.0
    assert meta["times"]["source"] == "mtime"
    assert meta["times"]["acq_end_hub_ms"] == pytest.approx(1_700_000_000_000 - 100.0)
    row = arch.get(off.file_id)
    assert row["experiment_ingest"] == "submitted" and stored[0]["file_id"] == off.file_id
    assert [p.name for p in ingested[0][0]] == ["a.csv", "a.csv.meta.json"]
    # 已入库：再 offer 直接回 offset==size + file_done
    again = rx.offer("rig1", _offer(data))
    assert again[0]["offset"] == len(data) and again[1]["ok"] is True


def test_resume_from_part_and_after_hub_restart(tmp_path):
    data = bytes(range(256)) * 400
    arch, rx = _rx(tmp_path)
    off = _offer(data)
    rx.offer("rig1", off)
    rx.chunk("rig1", _chunk(off.file_id, 0, data[:30000]))
    # 枢纽重启：新的接收器，只有磁盘上的 .part 与 .offer.json
    arch2, rx2 = _rx(tmp_path)
    want = rx2.offer("rig1", off)
    assert want == [{"t": "file_want", "file_id": off.file_id, "offset": 30000}]
    assert rx2.stats["resumed"] == 1
    # offset 不对的块被丢弃并回 file_want 重新对齐（同一位置 1 s 内只回一次）
    assert rx2.chunk("rig1", _chunk(off.file_id, 40000, data[40000:41000]))[0]["offset"] == 30000
    assert rx2.chunk("rig1", _chunk(off.file_id, 41000, data[41000:42000])) == []
    rx2.chunk("rig1", _chunk(off.file_id, 30000, data[30000:]))
    assert rx2.end("rig1", _end(off.file_id))[0]["ok"] is True
    # 没有 offer 也能续（枢纽在 chunk 中途重启）：从 .offer.json 恢复
    data2 = b"q" * 5000
    off2 = _offer(data2, name="b.csv", rel="b.csv")
    rx2.offer("rig1", off2)
    rx2.chunk("rig1", _chunk(off2.file_id, 0, data2[:2000]))
    _, rx3 = _rx(tmp_path)
    rx3.chunk("rig1", _chunk(off2.file_id, 2000, data2[2000:]))
    assert rx3.end("rig1", _end(off2.file_id))[0]["ok"] is True


def test_end_with_missing_bytes_asks_for_the_rest(tmp_path):
    data = b"z" * 10000
    _, rx = _rx(tmp_path)
    off = _offer(data)
    rx.offer("rig1", off)
    rx.chunk("rig1", _chunk(off.file_id, 0, data[:4000]))
    assert rx.end("rig1", _end(off.file_id)) == [
        {"t": "file_want", "file_id": off.file_id, "offset": 4000}]


def test_sha_mismatch_drops_part_and_reports(tmp_path):
    data = b"good data" * 100
    arch, rx = _rx(tmp_path)
    off = _offer(data)
    rx.offer("rig1", off)
    rx.chunk("rig1", _chunk(off.file_id, 0, b"X" + data[1:]))
    out = rx.end("rig1", _end(off.file_id))
    assert out == [{"t": "file_done", "file_id": off.file_id, "ok": False, "stored": None,
                    "error": "sha256_mismatch"}]
    assert not (tmp_path / "satellite/rig1/incoming" / f"{off.file_id}.part").exists()
    assert arch.list() == []
    # 从头重来就能成
    assert rx.offer("rig1", off)[0]["offset"] == 0


def test_overflow_and_bad_offer_rejected(tmp_path):
    _, rx = _rx(tmp_path)
    data = b"abc"
    off = _offer(data)
    rx.offer("rig1", off)
    out = rx.chunk("rig1", _chunk(off.file_id, 0, b"abcdef"))
    assert out[0]["ok"] is False and out[0]["error"] == "size_overflow"
    bad = P.parse_to_hub({"t": "file_offer", "file_id": "a" * 64, "name": "x", "size": 1,
                          "sha256": "b" * 64})
    assert rx.offer("rig1", bad)[0]["error"] == "file_id_must_equal_sha256"
    rx.max_file_bytes = 2
    assert "too_large" in rx.offer("rig1", _offer(b"12345"))[0]["error"]
    unknown = rx.chunk("rig1", _chunk("c" * 64, 0, b"x"))
    assert unknown[0]["ok"] is False and "unknown_file" in unknown[0]["error"]


def test_same_name_different_content_never_overwrites(tmp_path):
    _, rx = _rx(tmp_path)
    for content in (b"first", b"second"):
        off = _offer(content, name="Raman_ 1.spe", rel="Raman_ 1.spe", kind="other")
        rx.offer("rig1", off)
        rx.chunk("rig1", _chunk(off.file_id, 0, content))
        rx.end("rig1", _end(off.file_id))
    files = sorted(p.name for p in (tmp_path / "satellite/rig1/files").iterdir()
                   if not p.name.endswith(".meta.json"))
    assert files == ["Raman_ 1 (2).spe", "Raman_ 1.spe"]
    assert (tmp_path / "satellite/rig1/files/Raman_ 1.spe").read_bytes() == b"first"


@pytest.mark.parametrize("rel, expect", [
    ("2026\\20261016\\Raman_ 13.spe", "2026/20261016/Raman_ 13.spe"),
    ("../../etc/passwd", "etc/passwd"),
    ("C:\\Windows\\x.spe", "C_/Windows/x.spe"),
    ("/abs/x.spe", "abs/x.spe"),
    ("a/CON.txt", "a/_CON.txt"),
    ("a/b. ", "a/b"),
    ("x.spe.meta.json", "x.spe.meta.json_"),
    ("", "fallback.spe"),
])
def test_rel_path_sanitised(rel, expect):
    p, _changed = safe_rel_path(rel, "fallback.spe")
    assert str(p) == expect
    assert ".." not in p.parts


def test_spe_meta_uses_footer_times_and_stm_window(tmp_path):
    now = time.time()
    ring = StmStateRing(now_fn=lambda: now)
    for i in range(20):
        ring.add(now - 10 + i * 0.5, {"bias_v": 0.2, "current_a": 5e-11, "z_m": 1e-9,
                                      "z_controller_on": True})
    wl = np.linspace(635, 692, 1340)
    from datetime import datetime, timezone

    t_start = datetime.fromtimestamp(now - 6, tz=timezone.utc).astimezone()
    t_end = datetime.fromtimestamp(now - 5, tz=timezone.utc).astimezone()
    footer = build_footer_xml(width=1340, height=1, frames=1, wavelengths_nm=wl,
                              laser_line_nm=633.0, exposure_ms=1000.0, center_nm=664.5,
                              grating="[500nm,1200][0][0]", created_iso=t_end.isoformat(),
                              origin_created_iso=t_start.isoformat())
    spe = tmp_path / "src.spe"
    write_spe(spe, np.zeros((1, 1340), dtype=np.uint16), footer)
    data = spe.read_bytes()
    arch, rx = _rx(tmp_path / "root", ring=ring)
    off = _offer(data, name="Raman 1.spe", rel="Raman 1.spe", kind="spe")
    rx.offer("rig1", off)
    rx.chunk("rig1", _chunk(off.file_id, 0, data))
    out = rx.end("rig1", _end(off.file_id))
    assert out[0]["ok"]
    meta = arch.meta(off.file_id)
    assert meta["spe"]["ok"] and meta["spe"]["key"]["laser_line_nm"] == 633.0
    assert meta["times"]["source"] == "spe_footer"
    # θ = 100 ms：枢纽时间 = 卫星时间 − 100
    assert meta["times"]["acq_start_hub_ms"] == pytest.approx((now - 6) * 1000 - 100, abs=2)
    stm = meta["stm_state"]
    assert stm["matched"] and stm["before"] is not None and stm["after"] is not None
    assert stm["during"]["current_a"]["mean"] == pytest.approx(5e-11)
    row = arch.list("rig1")[0]
    assert row["spe_ok"] is True and row["stm_matched"] is True
