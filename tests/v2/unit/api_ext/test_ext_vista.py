"""VISTA 外部面：一动一观（令牌、同一代码单元、在跑的动作）、观测带图、外来帧、归档工具、
笔记与检查点、幂等重发、/jobs 对已接入会话的拒绝、严格模式、重启后回合号。

假仪器：一个名叫 ``ScanAt`` 的写技能把一张小 ``.sxm`` 写进「控制器的保存目录」
（``MAST_VISTA_SESSION_DIR`` 指到 tmp），几个读技能回读偏压 / 电流。
"""
from __future__ import annotations

import base64
import io
import threading
import time

import numpy as np
import pytest

from _ext_world import GATES, H, write_sxm

from mast.core.types import ParameterSpec, SafetyLevel, SkillCategory, SkillMetadata, SkillResult
from mast.skills.base import BaseSkill

SCANS: list[dict] = []
SAVES: list[dict] = []
#: what the fake ScanAt's composite record says ran before it stopped
STARTED = ["configure", "set_buffer", "start_scan"]


def _p(name, unit="", lo=None, hi=None):
    return ParameterSpec(name=name, type="float", unit=unit, required=False, description=name,
                         min_value=lo, max_value=hi)


class FakeScanAt(BaseSkill):
    """写技能：把一帧写进保存目录（真仪器上 Nanonis 自动保存）。``block``：扫到一半等
    ``GATES['vscan']`` 或被中止 —— 中止时像真的 ScanAt 那样失败、不存文件，记录里写着
    ``start_scan`` 跑过。``abort_global``：失败前顺手按下进程级急停（测抢救不越过急停）。"""

    session_dir = None
    block = False
    abort_global = None

    def metadata(self):
        return SkillMetadata(
            name="ScanAt", version="1.0.0", category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO, description="扫一帧", tags=["scan"],
            parameters=[_p("center_x_m", "m", -1e-6, 1e-6), _p("center_y_m", "m", -1e-6, 1e-6),
                        _p("size_m", "m", 1e-10, 1e-5),
                        ParameterSpec(name="pixels", type="int", unit="", required=False,
                                      description="pixels", min_value=16, max_value=1024)])

    def execute(self, context, params):
        SCANS.append(dict(params))
        stopped = {"line_time_s": 0.1, "requested_pixels": 32,
                   "_progress": {"completed_steps": list(STARTED), "failed_steps": ["wait_scan"]}}
        if FakeScanAt.abort_global is not None:
            from mast.core.execution_context import mark_abort

            mark_abort(FakeScanAt.abort_global, "测试：急停")
            return SkillResult(skill_name="ScanAt", success=False,
                               error="WaitScanComplete failed: aborted by user", data=stopped)
        if FakeScanAt.block:
            gate = GATES.setdefault("vscan", threading.Event())
            for _ in range(400):
                if gate.wait(0.05):
                    break
                if context.check_abort():
                    return SkillResult(skill_name="ScanAt", success=False,
                                       error="WaitScanComplete failed: aborted by user", data=stopped)
        n = len(SCANS)
        yy, xx = np.mgrid[0:32, 0:32]
        z = (np.exp(-((xx - 10 - n) ** 2 + (yy - 16) ** 2) / 8.0) * 1e-10).astype(np.float32)
        write_sxm(FakeScanAt.session_dir / f"vista_{n:03d}.sxm", z, z,
                  range_m=float(params.get("size_m", 5e-9)))
        return SkillResult(skill_name="ScanAt", success=True,
                           data={"line_time_s": 0.1, "requested_pixels": 32})


class FakeSaveScan(BaseSkill):
    """``Scan_Save`` of a stopped scan: the rows it reached, the rest NaN."""

    def metadata(self):
        return SkillMetadata(name="SaveScan", version="1.0.0", category=SkillCategory.WRITE,
                             safety_level=SafetyLevel.AUTO, description="存当前扫描", tags=["scan", "save"],
                             parameters=[ParameterSpec(name="timeout_ms", type="int", unit="ms",
                                                       required=False, description="t", min_value=-1)])

    def execute(self, context, params):
        SAVES.append(dict(params))
        yy, xx = np.mgrid[0:32, 0:32]
        z = (np.exp(-((xx - 16) ** 2 + (yy - 8) ** 2) / 8.0) * 1e-10).astype(np.float32)
        z[20:, :] = np.nan
        path = FakeScanAt.session_dir / f"stopped_{len(SAVES):03d}.sxm"
        write_sxm(path, z, z.copy())
        return SkillResult(skill_name="SaveScan", success=True,
                           data={"timed_out": False, "saved_path": str(path)})


class SlowWrite(BaseSkill):
    def metadata(self):
        return SkillMetadata(name="VSlowWrite", version="1.0.0", category=SkillCategory.WRITE,
                             safety_level=SafetyLevel.AUTO, description="慢写", tags=["setting"])

    def execute(self, context, params):
        gate = GATES.setdefault("vslow", threading.Event())
        for _ in range(400):
            if gate.wait(0.05):
                return SkillResult(skill_name="VSlowWrite", success=True)
            if context.check_abort():
                return SkillResult(skill_name="VSlowWrite", success=False, error="aborted")
        return SkillResult(skill_name="VSlowWrite", success=False, error="timeout")


def _reader(name, data):
    class _R(BaseSkill):
        def metadata(self):
            return SkillMetadata(name=name, version="1.0.0", category=SkillCategory.READ,
                                 safety_level=SafetyLevel.AUTO, description="读", tags=["read"])

        def execute(self, context, params):
            return SkillResult(skill_name=name, success=True, data=dict(data))
    _R.__name__ = f"Read{name}"
    return _R


@pytest.fixture()
def vw(world, monkeypatch):
    sdir = world.tmp / "nanonis_session"
    sdir.mkdir()
    monkeypatch.setenv("MAST_VISTA_SESSION_DIR", str(sdir))
    FakeScanAt.session_dir = sdir
    FakeScanAt.block = False
    FakeScanAt.abort_global = None
    SCANS.clear()
    SAVES.clear()
    for cls in (FakeScanAt, FakeSaveScan, SlowWrite, _reader("GetBias", {"bias_v": -0.2}),
                _reader("GetCurrent", {"current_a": 4.9e-11})):
        world.reg.register(cls)
    world.sdir = sdir
    world.svc = world.app.state.vista
    world.svc.stop_watcher()            # the tests poll with watch_once() themselves
    yield world
    GATES.setdefault("vslow", threading.Event()).set()
    GATES.setdefault("vscan", threading.Event()).set()
    FakeScanAt.block = False
    FakeScanAt.abort_global = None


def _observe(c, session="s1"):
    r = c.post("/vista/observe", json={}, headers=H(session=session))
    assert r.status_code == 200, r.text
    return r.json()["observation"]


def _scan(c, token, *, session="s1", item=None, wait_s=10, **extra):
    meta = {"item_id": item} if item else {}
    body = {"action": "scan", "params": {"center": {"x_nm": 0, "y_nm": 0}, "size_nm": 5},
            "after": token, "client_meta": meta, "wait_s": wait_s, **extra}
    return c.post("/vista/act", json=body, headers=H(session=session))


def test_observe_then_act_returns_the_new_frame_with_an_image_and_the_next_token(vw):
    c = vw.client
    obs = _observe(c)
    assert obs["next_token"] and obs["images"] == [] and '"latest_frame":null' in obs["text"]
    r = _scan(c, obs["next_token"])
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["kind"] == "act" and a["state"] == "done" and a["ok"] is True and a["turn"] == 1
    o = a["observation"]
    assert o["frames"] == ["t0001.s0"]
    assert len(o["images"]) == 1 and o["images"][0]["mime"] == "image/png"
    assert base64.b64decode(o["images"][0]["data_b64"])[:8] == b"\x89PNG\r\n\x1a\n"
    assert o["next_token"] and o["next_token"] != obs["next_token"]
    assert o["next_token"] in o["text"] and '"origin":"act"' in o["text"]
    assert '"bias_v":-0.2' in o["text"]          # state echo
    assert SCANS[0]["size_m"] == pytest.approx(5e-9)


def test_an_act_without_the_newest_token_is_refused_with_the_newest_observation(vw):
    c = vw.client
    r = _scan(c, None)
    assert r.status_code == 409 and r.json()["error"] == "observe_first"
    rec = r.json()["observation"]
    assert rec["next_token"]
    assert not SCANS                                   # nothing executed
    ok = _scan(c, rec["next_token"])
    assert ok.status_code == 200 and ok.json()["ok"] is True
    again = _scan(c, rec["next_token"])                # consumed
    assert again.status_code == 409 and again.json()["error"] == "observe_first"
    assert len(SCANS) == 1


def test_the_token_is_accepted_in_any_case_and_without_the_dash(vw):
    c = vw.client
    tok = _observe(c)["next_token"]
    r = _scan(c, tok.replace("-", "").lower())
    assert r.status_code == 200, r.text


def test_one_act_per_code_cell(vw):
    c = vw.client
    tok = _observe(c)["next_token"]
    first = _scan(c, tok, item="ctc_cell_1")
    assert first.status_code == 200
    nxt = first.json()["observation"]["next_token"]
    second = _scan(c, nxt, item="ctc_cell_1")
    assert second.status_code == 409 and second.json()["error"] == "same_code_cell"
    assert len(SCANS) == 1
    third = _scan(c, nxt, item="ctc_cell_2")            # a new cell with the same token is fine
    assert third.status_code == 200


def test_a_second_act_while_one_is_running_is_refused(vw):
    c = vw.client
    tok = _observe(c)["next_token"]
    r = c.post("/vista/act", json={"skill": "VSlowWrite", "after": tok, "wait_s": 0},
               headers=H())
    assert r.status_code == 200 and r.json()["state"] == "running"
    job_id = r.json()["job_id"]
    busy = _scan(c, tok)
    assert busy.status_code == 409 and busy.json()["error"] == "act_in_flight"
    assert busy.json()["job_id"] == job_id
    GATES["vslow"].set()
    done = c.get(f"/vista/acts/{job_id}", params={"wait_s": 10}, headers=H()).json()
    assert done["state"] == "done" and done["observation"]["next_token"]


def test_reads_through_act_are_plain_jobs_and_keep_the_token(vw):
    c = vw.client
    tok = _observe(c)["next_token"]
    r = c.post("/vista/act", json={"skill": "GetBias", "after": None, "wait_s": 5}, headers=H())
    assert r.status_code == 200 and r.json()["kind"] == "job"
    assert r.json()["job"]["state"] == "succeeded"
    assert _scan(c, tok).status_code == 200            # token still live


def test_jobs_endpoint_refuses_driving_skills_for_a_gated_session_only(vw):
    c = vw.client
    _observe(c, session="gated")
    r = c.post("/jobs", json={"skill": "ExtWrite"}, headers=H(session="gated"))
    assert r.status_code == 409 and r.json()["error"] == "use_act"
    ok_read = c.post("/jobs", json={"skill": "ExtRead"}, headers=H(session="gated"))
    assert ok_read.status_code == 202
    legacy = c.post("/jobs", json={"skill": "ExtWrite"}, headers=H(session="legacy"))
    assert legacy.status_code == 202


def test_a_frame_from_outside_is_archived_and_invalidates_the_token(vw):
    c = vw.client
    svc = vw.svc
    tok = _observe(c)["next_token"]
    assert svc.watch_once() == []                      # first look: existing files are history
    z = np.zeros((16, 16), np.float32)
    write_sxm(vw.sdir / "operator_001.sxm", z, z)
    later = time.time() + 5
    assert svc.watch_once(now=later) == []             # seen once: wait until its size is stable
    entries = svc.watch_once(now=later)
    assert [e.fid for e in entries] == ["t0000.s0"]
    r = _scan(c, tok)
    assert r.status_code == 409 and r.json()["error"] == "observe_first"
    assert '"origin":"external"' in r.json()["observation"]["text"]
    assert not SCANS


def test_archive_tools_answer_in_the_tool_reply_shape(vw):
    c = vw.client
    tok = _observe(c)["next_token"]
    assert _scan(c, tok).status_code == 200
    r = c.post("/vista/measure/stats", json={"frame": "t0001.s0", "client_meta": {}}, headers=H())
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"text", "images", "is_error"} and body["is_error"] is False
    insp = c.post("/vista/inspect", json={"question": "the bump", "views": [
        {"label": "whole", "frame": "t0001.s0"}]}, headers=H()).json()
    assert insp["is_error"] is False and len(insp["images"]) == 1
    bad = c.post("/vista/measure/nope", json={}, headers=H())
    assert bad.status_code == 404


def test_notes_history_and_the_checkpoint_flow(vw):
    c = vw.client
    svc = vw.svc
    tok = _observe(c)["next_token"]
    assert _scan(c, tok).status_code == 200
    hist = c.get("/vista/history", params={"view": "events"}, headers=H()).json()
    assert '"action":"scan"' in hist["text"] and '"turn":1' in hist["text"]
    svc.request_checkpoint("s1", "image_limit")
    refused = c.post("/vista/observe", json={}, headers=H())
    assert refused.status_code == 409 and refused.json()["error"] == "checkpoint_requested"
    put = c.put("/vista/notes/working", json={"content": "scan 1 shows one bump"}, headers=H())
    assert put.status_code == 200 and "checkpoint ready" in put.json()["text"]
    assert svc.checkpoint_state("s1") == "ready"
    tok = svc.gate.token_for("s1") or _observe(c)["next_token"]
    blocked = _scan(c, tok)                       # the runner rotates the session now: no act
    assert blocked.status_code == 409 and blocked.json()["error"] == "checkpoint_requested"
    got = c.get("/vista/notes/working", headers=H()).json()
    assert "one bump" in got["text"]
    rec = svc.recovery_observation("s1")
    assert "one bump" in rec["text"] and rec["images"] and rec["next_token"]


def test_an_act_resent_with_its_request_id_is_not_run_twice(vw):
    c = vw.client
    tok = _observe(c)["next_token"]
    first = _scan(c, tok, request_id="r-1")
    again = _scan(c, tok, request_id="r-1")
    assert first.status_code == again.status_code == 200
    assert again.json()["idempotent_replay"] is True
    assert again.json()["job_id"] == first.json()["job_id"]
    assert len(SCANS) == 1


def test_strict_mode_prints_the_token_on_the_image_only(vw, monkeypatch):
    from PIL import Image

    monkeypatch.setenv("MAST_VISTA_STRICT", "1")
    c = vw.client
    obs = _observe(c)
    assert obs["next_token"] is None and obs["token_on_image"] is True
    assert len(obs["images"]) == 1                     # no frame yet: a rendered card
    tok = vw.svc.gate.token_for("s1")
    assert tok and tok not in obs["text"]
    a = _scan(c, tok).json()["observation"]
    png = base64.b64decode(a["images"][0]["data_b64"])
    h = Image.open(io.BytesIO(png)).height
    plain = vw.svc.workspace().archive.observation("t0001.s0")[0].png
    assert h > Image.open(io.BytesIO(plain)).height    # the band under the frame
    assert a["next_token"] is None and vw.svc.gate.token_for("s1") not in a["text"]


def test_a_closed_session_cannot_act(vw):
    c = vw.client
    tok = _observe(c)["next_token"]
    vw.svc.close_session("s1", "operator stop")
    r = _scan(c, tok)
    assert r.status_code == 409 and r.json()["error"] == "session_closed"


def test_the_workspace_survives_a_restart(vw):
    from mast.vista.workspace import Workspace

    c = vw.client
    tok = _observe(c)["next_token"]
    assert _scan(c, tok).status_code == 200
    ws = vw.svc.workspace()
    again = Workspace(ws.dir.parent, ws.id, guide_path=ws.dir.parent / "GUIDE.md")
    assert again.turn == 1 and [e.fid for e in again.archive.frames(kind="s")] == ["t0001.s0"]
    assert again.next_turn() == 2


def test_status_never_shows_the_token(vw):
    c = vw.client
    tok = _observe(c)["next_token"]
    st = c.get("/vista/status", headers=H()).json()
    assert st["gate"]["token_live"] is True and tok not in str(st)


def test_the_client_session_table_is_bounded(vw, monkeypatch):
    import mast.vista.service as S

    monkeypatch.setattr(S, "MAX_CLIENTS", 3)
    monkeypatch.setattr(S, "ACTIVE_CLIENT_S", 0)
    for i in range(6):
        vw.client.post("/vista/hello", json={}, headers=H(session=f"rot-{i}"))
    assert len(vw.svc._clients) <= 3


def test_the_turn_survives_an_events_file_rotation(vw, monkeypatch):
    import mast.vista.workspace as W

    monkeypatch.setattr(W, "EVENTS_ROTATE_BYTES", 300)
    c = vw.client
    tok = _observe(c)["next_token"]
    a = _scan(c, tok).json()
    _scan(c, a["observation"]["next_token"])
    ws = vw.svc.workspace()
    assert list(ws.dir.glob("events-*.jsonl"))                 # rotated at least once
    again = W.Workspace(ws.dir.parent, ws.id, guide_path=ws.dir.parent / "GUIDE.md")
    assert again.turn == 2 and again.next_turn() == 3


def test_a_declarative_composite_with_a_remedy_tag_still_counts_as_driving(vw):
    class SneakyComposite(BaseSkill):
        def metadata(self):
            return SkillMetadata(name="SneakyComposite", version="1.0.0",
                                 category=SkillCategory.COMPOSITE, safety_level=SafetyLevel.AUTO,
                                 description="x", tags=["withdraw", "composite", "spec"])

        def execute(self, context, params):  # pragma: no cover
            return SkillResult(skill_name="SneakyComposite", success=True)

    vw.reg.register(SneakyComposite)
    assert vw.svc.is_instrument_driving("SneakyComposite") is True
    assert vw.svc.is_instrument_driving("StopScan") is False      # the built-in remedy stays one


# ── 7.0.1 ───────────────────────────────────────────────────────────────────
def _act_text(answer):
    import json

    return json.loads(answer["observation"]["text"])


def test_an_act_job_reports_what_its_steps_recorded_and_the_parameters_it_ran_with(vw):
    """From 7.0.0 an act job said recorded={}, nanonis_calls=0 and params_used=null although
    every step was recorded: an agent could not confirm from the job that the act was."""
    c = vw.client
    a = _scan(c, _observe(c)["next_token"]).json()
    job = c.get(f"/jobs/{a['job_id']}", headers=H()).json()
    rec = job["recorded"]
    assert rec["v1"] is True and rec["v2_action_id"] and rec["v2_action_ids"] == [rec["v2_action_id"]]
    assert rec["experiment_id"] == vw.eid and rec["sample_id"] == vw.sid
    assert [s["skill"] for s in rec["steps"]] == ["ScanAt"]
    used = job["params_used"]
    assert used["action"] == "scan" and used["params"]["size_nm"] == 5.0
    assert used["steps"][0]["skill"] == "ScanAt"
    assert used["steps"][0]["params"]["size_m"] == pytest.approx(5e-9)
    assert used["plan"]["scan"]["estimated_scan_s"] > 0                  # the policy's estimate
    assert job["estimated_s"] == pytest.approx(used["plan"]["scan"]["estimated_scan_s"], abs=0.1)
    assert isinstance(job["result"]["nanonis_calls"], int)


def _until_scanning(total_s: float = 20.0) -> None:
    """The act's ScanAt is executing (its plan was noted before that)."""
    deadline = time.monotonic() + total_s
    while not SCANS and time.monotonic() < deadline:
        time.sleep(0.02)
    assert SCANS, "the fake ScanAt never started"


def test_a_running_scan_says_how_long_it_has_run_and_how_long_it_will_take(vw):
    c = vw.client
    FakeScanAt.block = True
    a = _scan(c, _observe(c)["next_token"], wait_s=0).json()
    assert a["state"] == "running"
    _until_scanning()
    time.sleep(0.2)
    job = c.get(f"/jobs/{a['job_id']}", headers=H()).json()
    assert job["state"] == "running" and job["elapsed_s"] is not None and job["elapsed_s"] >= 0
    assert job["estimated_s"] > 0 and 0 <= job["eta_s"] <= job["estimated_s"]
    assert job["params_used"]["plan"]["scan"]["pixels"]                   # visible while it runs
    GATES["vscan"].set()
    done = c.get(f"/vista/acts/{a['job_id']}", params={"wait_s": 10}, headers=H()).json()
    assert done["state"] == "done"
    final = c.get(f"/jobs/{a['job_id']}", headers=H()).json()
    assert "eta_s" not in final and final["elapsed_s"] is not None
    journal = (vw.tmp / "journal" / "jobs.jsonl").read_text(encoding="utf-8")
    assert '"eta_s"' not in journal                     # the journal keeps only settled values


def test_a_cancelled_scan_keeps_the_rows_it_acquired_as_a_partial_frame(vw):
    c = vw.client
    FakeScanAt.block = True
    a = _scan(c, _observe(c)["next_token"], wait_s=0).json()
    job_id = a["job_id"]
    _until_scanning()               # cancelled before ScanAt starts, nothing would be acquired
    r = c.post(f"/jobs/{job_id}/cancel", json={"reason": "test"}, headers=H())
    assert r.status_code == 200
    done = c.get(f"/vista/acts/{job_id}", params={"wait_s": 10}, headers=H()).json()
    assert done["state"] == "done" and done["job_state"] == "cancelled"
    obs = done["observation"]
    payload = _act_text(done)
    assert payload["ok"] is False and "aborted" in payload["error"]
    assert payload["partial_frame"] == "t0001.p0" and obs["frames"] == ["t0001.p0"]
    [frame] = payload["new_frames"]
    assert frame["kind"] == "partial" and frame["rows_acquired"] == 20 and frame["rows_total"] == 32
    assert len(obs["images"]) == 1                       # the agent sees what was acquired
    assert SAVES == [{"timeout_ms": 4000}]
    job = c.get(f"/jobs/{job_id}", headers=H()).json()
    assert [s["skill"] for s in job["recorded"]["steps"]] == ["ScanAt", "SaveScan"]
    assert job["state"] == "cancelled"                   # the save does not change how it ended
    hist = c.get("/vista/history", params={"view": "events"}, headers=H()).json()
    assert '"new_frames":["t0001.p0"]' in hist["text"]
    events = (vw.svc.workspace().dir / "events.jsonl").read_text(encoding="utf-8")
    assert '"purpose": "save the partial frame"' in events or '"purpose":"save the partial frame"' in events


def test_after_an_emergency_stop_a_stopped_scan_is_not_saved(vw):
    c = vw.client
    FakeScanAt.abort_global = vw.rt._orch_abort
    try:
        done = _scan(c, _observe(c)["next_token"]).json()
    finally:
        vw.rt._orch_abort.clear()
    payload = _act_text(done)
    assert payload["ok"] is False and payload["partial_frame"].startswith("not saved: SaveScan failed")
    assert SAVES == [] and payload["new_frames"] == []


def test_an_act_without_new_frames_names_the_newest_frame_as_observe_does(vw):
    c = vw.client
    a = _scan(c, _observe(c)["next_token"]).json()
    w = c.post("/vista/act", json={"action": "wait", "params": {"seconds": 1},
                                   "after": a["observation"]["next_token"], "wait_s": 10},
               headers=H()).json()
    latest = _act_text(w)["latest_frame"]
    assert isinstance(latest, dict) and latest["frame"] == "t0001.s0" and latest["kind"] == "scan"
    assert latest == _act_text({"observation": _observe(c)})["latest_frame"]


def test_the_state_echo_says_whether_the_tip_is_withdrawn_and_flags_a_lagging_current(vw, monkeypatch):
    import mast.vista.service as S

    monkeypatch.setattr(S, "CURRENT_SETTLE_BUDGET_S", 0.3)
    vw.reg.register(_reader("GetZControllerState", {
        "controller_on": False, "module_status": 1, "module_status_name": "Off",
        "z_m": 7.5e-07, "z_limits": [7.5e-07, -7.5e-07]}))
    vw.reg.register(_reader("GetSetpoint", {"setpoint_a": 5e-11}))
    vw.reg.register(_reader("GetCurrent", {"current_a": 5.0003e-11}))   # never decays here
    c = vw.client
    inst = _act_text({"observation": _observe(c)})["instrument"]
    assert inst["z_controller_on"] is False and inst["withdrawn"] is True
    assert inst["current_settling"] is True and inst["current_settle_s"] == 0.0   # observe: no wait
    tok = _observe(c)["next_token"]
    t0 = time.monotonic()
    w = c.post("/vista/act", json={"action": "wait", "params": {"seconds": 1}, "after": tok, "wait_s": 10},
               headers=H()).json()
    inst = _act_text(w)["instrument"]
    assert inst["current_settling"] is True and 0 < inst["current_settle_s"] <= 0.3
    assert inst["current_pa"] == pytest.approx(50.003)                  # reported, not replaced
    assert time.monotonic() - t0 < 5
