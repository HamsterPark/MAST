"""``/detectors``（``mast.api.ext.detectors``）：建、查、启停、删、单帧试跑，以及错误体。

VISTA 服务用替身顶上（``detectors()`` / ``workspace().archive``）；管理器与帧存档是真的，
只把 pyexec 那一次运行换成假的（真子进程的那几条在 ``tests/v2/vista/test_detectors.py``）。
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from _ext_world import H, write_sxm

from mast.vista.archive import Archive
from mast.vista.detectors import DetectorManager

CODE = "def detect(frame):\n    return {'hit': True, 'message': 'edge', 'value': 2}\n"


def fake_run(det, archive, entry, *, timeout_s):
    return {"ok": True, "hit": True, "message": f"{det['name']} on {entry.fid}", "value": 2}


class FakeService:
    def __init__(self, mgr, archive):
        self.mgr, self.archive = mgr, archive

    def bind(self, ctx, **kw):                  # what create_ext_app does with the service
        return self

    def shutdown(self):
        pass

    def detectors(self):
        return self.mgr

    def workspace(self):
        return SimpleNamespace(archive=self.archive)


@pytest.fixture()
def det_app(tmp_path, monkeypatch):
    import mast.vista.service as vista_service
    from mast.api.ext import create_ext_app
    from mast.api.ext.detectors import router
    from mast.api.ext.jobs import JobManager

    arch = Archive(tmp_path / "ws" / "archive")
    z = np.linspace(0, 1e-10, 64, dtype=np.float32).reshape(8, 8)
    scan = arch.add_scan(1, write_sxm(tmp_path / "a.sxm", z, z))
    mgr = DetectorManager(tmp_path / "ws" / "detectors.json", run_frame=fake_run)
    svc = FakeService(mgr, arch)
    # the ext app binds this double instead of creating the process singleton
    monkeypatch.setattr(vista_service, "get_service", lambda ctx=None: svc)
    jm = JobManager(tmp_path / "journal")
    app = create_ext_app(SimpleNamespace(), job_manager=jm)
    app.state.vista = svc
    # create_ext_app mounts it already (FastAPI ≥ 0.140 keeps it as one _IncludedRouter)
    if not any(getattr(r, "original_router", None) is router or getattr(r, "path", None) == "/detectors"
               for r in app.routes):
        app.include_router(router)
    # detector code runs as the MAST user: creation is loopback-only by default
    yield SimpleNamespace(client=TestClient(app, client=("127.0.0.1", 50123)), mgr=mgr, scan=scan,
                          app=app)
    mgr.close()
    jm.shutdown(timeout_s=1)


def create(w, **kw):
    body = {"name": "edge", "code": CODE, "applies_to": ["scan"], **kw}
    return w.client.post("/detectors", json=body, headers=H())


def test_create_replay_and_conflict(det_app):
    r = create(det_app, description="step edges")
    assert r.status_code == 201, r.text
    det = r.json()
    assert det["id"].startswith("det-") and det["enabled"] is True
    assert det["created_by"] == "ext:tester" and det["description"] == "step edges"
    again = create(det_app)
    assert again.status_code == 200 and again.json()["existing"] is True
    assert again.json()["id"] == det["id"]
    clash = create(det_app, code=CODE + "# v2\n")
    assert clash.status_code == 409 and clash.json()["error"] == "detector_name_taken"
    assert clash.json()["detector_id"] == det["id"]


@pytest.mark.parametrize("kw, code", [
    ({"code": "def detect(:\n"}, "invalid_detector"),
    ({"code": "x = 1\n"}, "invalid_detector"),
    ({"applies_to": ["partial"]}, "invalid_request"),
    ({"applies_to": []}, "invalid_request"),
    ({"max_runtime_s": 500}, "invalid_request"),
    ({"name": ""}, "invalid_request"),
])
def test_invalid_detectors_are_422(det_app, kw, code):
    r = create(det_app, **kw)
    assert r.status_code == 422 and r.json()["error"] == code, r.text


def test_list_get_enable_delete(det_app):
    det = create(det_app).json()
    c = det_app.client
    listed = c.get("/detectors", headers=H()).json()
    assert listed["count"] == 1 and "code" not in listed["detectors"][0]
    assert listed["limits"]["max_enabled"] == 8
    got = c.get(f"/detectors/{det['id']}", headers=H()).json()
    assert got["code"] == CODE and got["recent_hits"] == []
    off = c.post(f"/detectors/{det['id']}/enable", json={"enabled": False}, headers=H()).json()
    assert off["enabled"] is False and off["disabled_reason"] == "disabled on request"
    on = c.post(f"/detectors/{det['id']}/enable", json={"enabled": True}, headers=H()).json()
    assert on["enabled"] is True and on["disabled_reason"] is None
    r = c.request("DELETE", f"/detectors/{det['id']}", json={}, headers=H())
    assert r.status_code == 200 and r.json() == {"deleted": det["id"]}
    for r in (c.get(f"/detectors/{det['id']}", headers=H()),
              c.request("DELETE", f"/detectors/{det['id']}", json={}, headers=H()),
              c.post(f"/detectors/{det['id']}/enable", json={"enabled": True}, headers=H())):
        assert r.status_code == 404 and r.json()["error"] == "unknown_detector"


def test_the_enabled_limit_is_a_409(det_app):
    for i in range(8):
        assert create(det_app, name=f"d{i}").status_code == 201
    r = create(det_app, name="ninth")
    assert r.status_code == 409 and r.json()["error"] == "too_many_enabled"
    assert len(r.json()["enabled"]) == 8


def test_test_runs_on_an_archived_frame(det_app):
    det = create(det_app).json()
    c = det_app.client
    r = c.post(f"/detectors/{det['id']}/test", json={"frame": det_app.scan.fid}, headers=H())
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] and res["hit"] and res["message"] == f"edge on {det_app.scan.fid}"
    assert res["recorded"] is False
    r = c.post(f"/detectors/{det['id']}/test", json={"frame": "t0042.s0"}, headers=H())
    assert r.status_code == 404 and r.json()["error"] == "unknown_frame"
    r = c.post("/detectors/det-nope/test", json={"frame": det_app.scan.fid}, headers=H())
    assert r.status_code == 404 and r.json()["error"] == "unknown_detector"


def test_not_wired_is_a_declared_503(det_app, monkeypatch):
    import mast.vista.service as vista_service

    det_app.app.state.vista = None
    monkeypatch.setattr(vista_service, "get_service", lambda ctx=None: None)
    r = det_app.client.get("/detectors", headers=H())
    assert r.status_code == 503 and r.json()["error"] == "not_wired" and r.json()["missing"]
    det_app.app.state.vista = SimpleNamespace(detectors=lambda: None)
    r = det_app.client.get("/detectors/det-x", headers=H())
    assert r.status_code == 503 and r.json()["missing"] == ["vista.detectors"]


def test_creating_or_test_running_a_detector_from_another_host_is_refused(det_app, monkeypatch):
    """LAN mode: the shared password must not turn into remote code execution."""
    from fastapi.testclient import TestClient

    remote = TestClient(det_app.app)                   # client host "testclient": not loopback
    body = {"name": "remote", "code": "def detect(frame):\n    return False\n",
            "applies_to": ["scan"]}
    r = remote.post("/detectors", json=body)
    assert r.status_code == 403 and r.json()["error"] == "loopback_only"
    r = remote.post("/detectors/d_nope/test", json={"frame": "t0001.s0"})
    assert r.status_code == 403 and r.json()["error"] == "loopback_only"
    assert remote.get("/detectors").status_code == 200   # looking is fine
    monkeypatch.setenv("MAST_DETECTORS_ALLOW_REMOTE", "1")
    assert remote.post("/detectors", json=body).status_code in (200, 201)
