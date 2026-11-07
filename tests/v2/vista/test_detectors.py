"""DetectorManager (``mast.vista.detectors``): validation, persistence, limits, wake cap,
auto-disable, the run queue, and a few real runs in a pyexec interpreter."""
from __future__ import annotations

import asyncio
import json
import struct
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from mast.vista.archive import Archive
from mast.vista.detectors import DetectorError, DetectorManager, validate_code
from mast.vista.wait import WaitHub

HIT = "def detect(frame):\n    return {'hit': True, 'message': 'seen', 'value': 1}\n"


# ── tiny synthetic frames ────────────────────────────────────────────────────
def write_sxm(path: Path, fwd: np.ndarray, bwd: np.ndarray) -> Path:
    """A minimal real ``.sxm``: one Z channel, both directions (same shape as the ext tests')."""
    ny, nx = fwd.shape
    header = (
        ":NANONIS_VERSION:\n2\n"
        ":SCANIT_TYPE:\n\t FLOAT            MSBFIRST\n"
        ":REC_DATE:\n 14.10.2026\n"
        ":REC_TIME:\n12:00:00\n"
        ":BIAS:\n\t-1.200000E+0\n"
        f":SCAN_PIXELS:\n{nx:>10d}{ny:>10d}\n"
        f":SCAN_OFFSET:\n{0.0:>19.6E}{0.0:>19.6E}\n"
        f":SCAN_RANGE:\n{5e-9:>19.6E}{5e-9:>19.6E}\n"
        ":SCAN_ANGLE:\n\t0.000E+0\n"
        ":SCAN_DIR:\ndown\n"
        ":Z-CONTROLLER>SETPOINT:\n50.0000E-12\n"
        ":DATA_INFO:\n"
        "\tChannel\tName\tUnit\tDirection\tCalibration\tOffset\n"
        "\t14\tZ\tm\tboth\t9.000E-9\t0.000E+0\n"
        ":SCANIT_END:\n\n"
    )
    arr = np.concatenate([fwd.astype(np.float32).ravel(), bwd.astype(np.float32).ravel()])
    path.write_bytes(header.encode("utf-8") + b"\x1a\x04" + struct.pack(">%df" % arr.size, *arr.tolist()))
    return path


def write_dat(path: Path, n: int = 21) -> Path:
    v = np.linspace(-1.0, 1.0, n)
    lines = ["Experiment\tbias spectroscopy", "Bias>Bias (V)\t0.1", "", "[DATA]",
             "Bias calc (V)\tCurrent (A)"]
    lines += [f"{a:.6E}\t{1e-10 * a:.6E}" for a in v]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def frames(tmp_path):
    arch = Archive(tmp_path / "archive")
    z = np.linspace(0, 2e-10, 64, dtype=np.float32).reshape(8, 8)
    scan = arch.add_scan(1, write_sxm(tmp_path / "a.sxm", z, z[:, ::-1]))
    spec = arch.add_spectrum(1, write_dat(tmp_path / "b.dat"))
    return arch, scan, spec


class FakeRun:
    """Stands in for the pyexec run: returns queued results (or ``default``), records calls,
    and can be held at a gate."""

    def __init__(self, default=None):
        self.default = default or {"ok": True, "hit": True, "message": "seen", "value": 1}
        self.results: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.started = threading.Event()
        self.gate: threading.Event | None = None

    def __call__(self, det, archive, entry, *, timeout_s):
        self.calls.append((det["id"], entry.fid))
        self.started.set()
        if self.gate is not None:
            self.gate.wait(10)
        return dict(self.results.pop(0) if self.results else self.default)


def settle(mgr: DetectorManager, total_s: float = 10.0) -> None:
    deadline = time.monotonic() + total_s
    while time.monotonic() < deadline:
        st = mgr.status()
        if st["queued"] == 0 and st["running"] is None:
            return
        time.sleep(0.02)
    raise AssertionError(f"detector queue did not drain: {mgr.status()}")


def manager(tmp_path, **kw) -> DetectorManager:
    kw.setdefault("run_frame", FakeRun())
    return DetectorManager(tmp_path / "ws" / "detectors.json", **kw)


# ── validation and the store ─────────────────────────────────────────────────
@pytest.mark.parametrize("code, why", [
    ("", "empty"), ("def detect(:\n", "compile"), ("x = 1\n", "detect"),
    ("async def detect(frame):\n    return True\n", "async"),
    ("def detect(frame):\n    return True\n" + "#" * (64 * 1024), "bytes"),
    ("return 1\n", "compile"),
], ids=["empty", "syntax", "no-detect", "async", "too-big", "return-outside"])
def test_code_is_checked_without_running_it(code, why):
    with pytest.raises(ValueError, match=why):
        validate_code(code)


@pytest.mark.parametrize("kw", [
    {"name": ""}, {"name": "x" * 81}, {"applies_to": []}, {"applies_to": ["partial"]},
    {"max_runtime_s": 0}, {"max_runtime_s": 121}, {"max_runtime_s": True}, {"description": "d" * 501},
])
def test_create_refuses_invalid_fields(tmp_path, kw):
    args = {"name": "edge", "code": HIT, "applies_to": ["scan"], **kw}
    with pytest.raises(ValueError):
        manager(tmp_path).create(**args)


def test_detectors_persist_with_code_stats_and_hits(tmp_path, frames):
    arch, scan, _ = frames
    mgr = manager(tmp_path)
    det = mgr.create(name="step edge", code=HIT, applies_to="scan", description="d",
                     created_by="ext:tester")
    assert det["enabled"] is True and det["applies_to"] == ["scan"] and "code" not in det
    mgr.on_frame(arch, scan)
    settle(mgr)
    mgr.close()
    store = json.loads((tmp_path / "ws" / "detectors.json").read_text(encoding="utf-8"))
    assert store["detectors"][0]["code"] == HIT

    again = DetectorManager(tmp_path / "ws" / "detectors.json", run_frame=FakeRun())
    got = again.get(det["id"])
    assert got["code"] == HIT and got["created_by"] == "ext:tester"
    assert got["stats"]["runs"] == 1 and got["stats"]["hits"] == 1
    assert [h["fid"] for h in got["recent_hits"]] == [scan.fid]
    assert [d["id"] for d in again.list()] == [det["id"]] and "code" not in again.list()[0]


def test_a_corrupt_store_is_moved_aside_not_overwritten(tmp_path):
    p = tmp_path / "ws" / "detectors.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    mgr = DetectorManager(p, run_frame=FakeRun())
    assert mgr.list() == [] and mgr.load_warnings
    assert list(p.parent.glob("detectors.json.corrupt-*"))


def test_same_name_same_code_is_a_replay_different_code_conflicts(tmp_path):
    mgr = manager(tmp_path)
    a = mgr.create(name="edge", code=HIT, applies_to=["scan"])
    b = mgr.create(name="edge", code=HIT, applies_to=["scan"])
    assert b["id"] == a["id"] and b["existing"] is True and len(mgr.list()) == 1
    with pytest.raises(DetectorError) as ei:
        mgr.create(name="edge", code=HIT + "\n# v2\n", applies_to=["scan"])
    assert ei.value.status == 409 and ei.value.code == "detector_name_taken"


def test_enabled_limit_and_delete(tmp_path):
    mgr = manager(tmp_path, max_enabled=2)
    a = mgr.create(name="a", code=HIT, applies_to=["scan"])
    mgr.create(name="b", code=HIT, applies_to=["scan"])
    with pytest.raises(DetectorError) as ei:
        mgr.create(name="c", code=HIT, applies_to=["scan"])
    assert ei.value.code == "too_many_enabled"
    assert mgr.set_enabled(a["id"], False)["disabled_reason"] == "disabled on request"
    c = mgr.create(name="c", code=HIT, applies_to=["scan"])
    with pytest.raises(DetectorError):
        mgr.set_enabled(a["id"], True)
    assert mgr.delete(c["id"]) is True and mgr.delete(c["id"]) is False
    assert mgr.set_enabled(a["id"], True)["enabled"] is True
    with pytest.raises(DetectorError) as ei:
        mgr.set_enabled("det-nope", True)
    assert ei.value.status == 404


# ── runs, hits, the wake cap, auto-disable ───────────────────────────────────
def test_a_hit_wakes_a_detector_wait(tmp_path, frames):
    arch, scan, spec = frames
    hub, seen = WaitHub(), []
    run = FakeRun()
    mgr = manager(tmp_path, hub=hub, run_frame=run, on_event=seen.append)
    det = mgr.create(name="edge", code=HIT, applies_to=["scan"])
    c = hub.cursor()
    mgr.on_frame(arch, spec)                 # not a scan: never run
    mgr.on_frame(arch, scan)
    ans = asyncio.run(hub.wait([{"type": "detector", "detector_id": det["id"]}], timeout_s=10, since=c))
    ev = ans["fired"][0]["event"]
    assert ev["kind"] == "hit" and ev["fid"] == scan.fid and ev["frame_kind"] == "scan"
    assert ev["message"] == "seen" and ev["value"] == 1
    settle(mgr)
    assert run.calls == [(det["id"], scan.fid)]
    assert seen and seen[0]["fid"] == scan.fid
    mgr.close()


def test_hits_beyond_the_wake_cap_are_recorded_but_suppressed(tmp_path, frames):
    arch, scan, _ = frames
    hub = WaitHub()
    mgr = manager(tmp_path, hub=hub, wakes_per_hour=2)
    det = mgr.create(name="edge", code=HIT, applies_to=["scan"])
    c = hub.cursor()
    for _ in range(3):
        mgr.on_frame(arch, scan)
    settle(mgr)
    got = mgr.get(det["id"])
    assert got["stats"]["hits"] == 3 and got["stats"]["suppressed"] == 1
    assert [h["suppressed"] for h in got["recent_hits"]] == [False, False, True]
    ans = asyncio.run(hub.wait([{"type": "detector"}], timeout_s=0, since=c))
    assert len(ans["fired"]) == 2
    assert mgr.status()["wakes_last_hour"] == 2
    mgr.close()


def test_consecutive_errors_auto_disable_and_say_why(tmp_path, frames):
    arch, scan, _ = frames
    hub = WaitHub()
    run = FakeRun(default={"ok": False, "error": "ZeroDivisionError: division by zero"})
    mgr = manager(tmp_path, hub=hub, run_frame=run)
    det = mgr.create(name="broken", code=HIT, applies_to=["scan"])
    c = hub.cursor()
    for _ in range(6):
        mgr.on_frame(arch, scan)
    settle(mgr)
    got = mgr.get(det["id"])
    assert got["enabled"] is False and "5 consecutive errors" in got["disabled_reason"]
    assert got["stats"]["errors"] == 5 and len(run.calls) == 5      # the 6th run was skipped
    ans = asyncio.run(hub.wait([{"type": "detector", "detector_id": det["id"]}], timeout_s=0, since=c))
    assert [f["event"]["kind"] for f in ans["fired"]] == ["disabled"]
    mgr.on_frame(arch, scan)
    settle(mgr)
    assert len(run.calls) == 5
    # re-enabling clears the streak and the reason
    again = mgr.set_enabled(det["id"], True)
    assert again["disabled_reason"] is None and again["stats"]["consecutive_errors"] == 0
    mgr.close()


def test_a_noisy_detector_is_auto_disabled(tmp_path, frames):
    arch, scan, _ = frames
    mgr = manager(tmp_path, hub=WaitHub(), hits_per_hour=2)
    det = mgr.create(name="noisy", code=HIT, applies_to=["scan"])
    for _ in range(3):
        mgr.on_frame(arch, scan)
    settle(mgr)
    got = mgr.get(det["id"])
    assert got["enabled"] is False and "3 hits within an hour" in got["disabled_reason"]
    mgr.close()


def test_the_queue_drops_the_oldest_run_when_full(tmp_path, frames):
    arch, scan, _ = frames
    run = FakeRun()
    run.gate = threading.Event()
    mgr = manager(tmp_path, run_frame=run, queue_max=2)
    det = mgr.create(name="slow", code=HIT, applies_to=["scan"])
    mgr.on_frame(arch, scan)
    assert run.started.wait(5)              # the first run is in progress, held at the gate
    for _ in range(3):
        mgr.on_frame(arch, scan)            # queue: 2 slots, so one queued run is dropped
    st = mgr.status()
    assert st["queued"] == 2 and st["dropped"] == 1 and st["running"] == det["id"]
    run.gate.set()
    settle(mgr)
    got = mgr.get(det["id"])
    assert got["stats"]["runs"] == 3 and got["stats"]["dropped"] == 1
    mgr.close()


def test_close_is_bounded_while_a_run_is_stuck(tmp_path, frames):
    arch, scan, _ = frames
    run = FakeRun()
    run.gate = threading.Event()
    mgr = manager(tmp_path, run_frame=run)
    mgr.create(name="slow", code=HIT, applies_to=["scan"])
    mgr.on_frame(arch, scan)
    assert run.started.wait(5)
    t0 = time.monotonic()
    mgr.close(timeout_s=0.2)
    assert time.monotonic() - t0 < 1.5
    mgr.on_frame(arch, scan)                # ignored after close
    run.gate.set()
    assert mgr.status()["queued"] == 0


def test_test_runs_one_frame_and_records_nothing(tmp_path, frames):
    arch, scan, spec = frames
    run = FakeRun()
    mgr = manager(tmp_path, run_frame=run)
    det = mgr.create(name="edge", code=HIT, applies_to=["scan"])
    mgr.set_enabled(det["id"], False)       # a disabled detector can still be tested
    res = mgr.test(det["id"], arch, scan.fid)
    assert res["ok"] and res["hit"] and res["frame"] == scan.fid and res["recorded"] is False
    assert mgr.get(det["id"])["stats"]["runs"] == 0
    for fid, code, status in ((spec.fid, "frame_not_applicable", 422),
                              ("t0099.s0", "unknown_frame", 404)):
        with pytest.raises(DetectorError) as ei:
            mgr.test(det["id"], arch, fid)
        assert (ei.value.code, ei.value.status) == (code, status)
    with pytest.raises(DetectorError) as ei:
        mgr.test("det-nope", arch, scan.fid)
    assert ei.value.code == "unknown_detector"


def test_an_executor_crash_is_the_runs_error(tmp_path, frames):
    arch, scan, _ = frames

    def boom(det, archive, entry, *, timeout_s):
        raise OSError("disk full")

    mgr = manager(tmp_path, run_frame=boom)
    det = mgr.create(name="edge", code=HIT, applies_to=["scan"])
    res = mgr.test(det["id"], arch, scan.fid)
    assert res["ok"] is False and "disk full" in res["error"] and res["hit"] is False


# ── real runs in pyexec ──────────────────────────────────────────────────────
@pytest.fixture()
def pyexec_rt(tmp_path, monkeypatch):
    from mast.pyexec.runtime import find_runtime

    monkeypatch.setenv("MAST_DP_SESSIONS_DIR", str(tmp_path / "dp-sessions"))
    rt = find_runtime()
    if rt is None:
        pytest.skip("no analysis Python runtime on this machine")
    return rt


SCAN_CODE = """
import numpy as np

def detect(frame):
    z = frame.channels["Z"]
    back = frame.backward["Z"]
    span = float(np.nanmax(z) - np.nanmin(z))
    return {"hit": span > 1e-10, "message": f"{frame.kind} {frame.fid} {z.shape} {back.shape}",
            "value": {"span_m": span, "nx": frame.geometry["nx"], "units": frame.units["Z"],
                      "mean": np.float32(z.mean())}}
"""


def test_pyexec_scan_run_reads_the_archived_values(tmp_path, frames, pyexec_rt):
    arch, scan, _ = frames
    mgr = DetectorManager(tmp_path / "ws" / "detectors.json")
    det = mgr.create(name="span", code=SCAN_CODE, applies_to=["scan"])
    res = mgr.test(det["id"], arch, scan.fid)
    assert res["ok"], res
    assert res["hit"] is True and res["message"] == f"scan {scan.fid} (8, 8) (8, 8)"
    expected = arch.array(scan.fid, "Z")
    assert res["value"]["span_m"] == pytest.approx(float(expected.max() - expected.min()), rel=1e-6)
    assert res["value"]["nx"] == 8 and res["value"]["units"] == "m"
    assert isinstance(res["value"]["mean"], float)
    assert "runtime_isolated" in res
    sessions = tmp_path / "dp-sessions"
    assert list(sessions.glob("*/vista_detectors"))       # the run happened in this session root
    leftovers = list(sessions.rglob("vista_det_*")) + list(sessions.rglob("vista_detectors/*"))
    assert leftovers == []                   # run dirs, scripts and logs are cleaned up


def test_pyexec_worker_run_on_a_spectrum_wakes_the_hub(tmp_path, frames, pyexec_rt):
    arch, _, spec = frames
    hub = WaitHub()
    mgr = DetectorManager(tmp_path / "ws" / "detectors.json", hub=hub)
    code = ("def detect(frame):\n"
            "    cols = list(frame.columns)\n"
            "    return {'hit': True, 'message': ','.join(cols), 'value': len(frame.columns[cols[0]])}\n")
    det = mgr.create(name="points", code=code, applies_to=["spectrum"])
    c = hub.cursor()
    mgr.on_frame(arch, spec)
    ans = asyncio.run(hub.wait([{"type": "detector"}], timeout_s=60, since=c))
    ev = ans["fired"][0]["event"]
    assert ev["detector_id"] == det["id"] and ev["fid"] == spec.fid
    assert ev["message"] == "Bias calc (V),Current (A)" and ev["value"] == 21
    mgr.close()


def test_pyexec_errors_carry_the_traceback_and_runaways_are_killed(tmp_path, frames, pyexec_rt):
    from mast.pyexec.session import get_session

    arch, scan, _ = frames
    made: list[int] = []

    def session():
        made.append(1)
        return get_session(experiment_id="ws", thread_id="vista-detectors/ws")

    mgr = DetectorManager(tmp_path / "ws" / "detectors.json", session_factory=session)
    bad = mgr.create(name="bad", code="def detect(frame):\n    return 1 / 0\n", applies_to=["scan"])
    res = mgr.test(bad["id"], arch, scan.fid)
    assert res["ok"] is False and res["error"].startswith("ZeroDivisionError")
    assert "detector.py" in res["traceback"]
    wrong = mgr.create(name="wrong", code="def detect(frame):\n    return 3.5\n", applies_to=["scan"])
    res = mgr.test(wrong["id"], arch, scan.fid)
    assert res["ok"] is False and "returned a float" in res["error"]
    loop = mgr.create(name="loop", code="def detect(frame):\n    while True:\n        pass\n",
                      applies_to=["scan"], max_runtime_s=1)
    t0 = time.monotonic()
    res = mgr.test(loop["id"], arch, scan.fid)
    assert time.monotonic() - t0 < 30
    assert res["ok"] is False and res["timed_out"] is True and "killed" in res["error"]
    assert made == [1]                       # one provisioned session for all three runs
