"""``POST /wait``（``mast.api.ext.wait``）：条件交给 WaitHub、观察附进回答、错误体与没接线的降级。

VISTA 服务用替身顶上：路由只认 ``app.state.vista``（``create_ext_app`` 绑的那个，没有再问
``get_service(ctx)``）→ ``svc.hub`` / ``svc.observation_after_wait``。等待本身用真的 ``WaitHub``。
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from _ext_world import H

from mast.vista.wait import WaitHub


class FakeService:
    def __init__(self, hub: WaitHub):
        self.hub = hub
        self.obs = None
        self.calls: list[tuple[str, list[str]]] = []

    def bind(self, ctx, **kw):                  # what create_ext_app does with the service
        return self

    def shutdown(self):
        pass

    def observation_after_wait(self, session, fired):
        self.calls.append((session, [f["type"] for f in fired]))
        if isinstance(self.obs, Exception):
            raise self.obs
        return self.obs


def ext_app(ctx, jm, svc, router, monkeypatch):
    """The ext app with ``svc`` as its VISTA service (the process singleton is never created)
    and ``router`` mounted once (``create_ext_app`` may already include it)."""
    import mast.vista.service as vista_service
    from mast.api.ext import create_ext_app

    monkeypatch.setattr(vista_service, "get_service", lambda ctx=None: svc)
    app = create_ext_app(ctx, job_manager=jm)
    app.state.vista = svc
    if not mounted(app, router):
        app.include_router(router)
    return app


def mounted(app, router) -> bool:
    """FastAPI ≥ 0.140 keeps an included router as one ``_IncludedRouter(original_router=...)``;
    older versions copy its routes in."""
    have = {getattr(r, "path", None) for r in app.routes}
    return (any(getattr(r, "original_router", None) is router for r in app.routes)
            or {r.path for r in router.routes} <= have)


@pytest.fixture()
def wait_app(tmp_path, monkeypatch):
    from mast.api.ext.jobs import JobManager
    from mast.api.ext.wait import router

    jm = JobManager(tmp_path / "journal")
    svc = FakeService(WaitHub(job_lookup=jm.get))
    app = ext_app(SimpleNamespace(), jm, svc, router, monkeypatch)
    yield SimpleNamespace(client=TestClient(app), svc=svc, hub=svc.hub, app=app)
    jm.shutdown(timeout_s=1)


def post(w, body, **headers):
    return w.client.post("/wait", json=body, headers={**H(), **headers})


def test_a_fired_wait_carries_the_services_observation(wait_app):
    wait_app.svc.obs = {"obs_id": "o000001", "next_token": "K7QM-29XA"}
    r = post(wait_app, {"conditions": [{"type": "timer", "seconds": 0}], "timeout_s": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [f["type"] for f in body["fired"]] == ["timer"] and body["timed_out"] is False
    assert body["observation"]["obs_id"] == "o000001"
    assert body["cursor"].startswith("c1.")
    assert wait_app.svc.calls == [("s1", ["timer"])]          # the caller's session goes along


def test_without_an_observation_the_key_is_absent(wait_app):
    body = post(wait_app, {"conditions": [{"type": "frame"}], "timeout_s": 0}).json()
    assert body["timed_out"] is True and "observation" not in body


def test_the_cursor_round_trips_over_http(wait_app):
    first = post(wait_app, {"conditions": [{"type": "frame"}], "timeout_s": 0}).json()
    wait_app.hub.publish("frame", {"fid": "t0001.s0"})
    t0 = time.monotonic()
    second = post(wait_app, {"conditions": [{"type": "frame"}], "timeout_s": 30,
                             "since": first["cursor"]}).json()
    assert time.monotonic() - t0 < 2.0
    assert [f["event"]["fid"] for f in second["fired"]] == ["t0001.s0"]


def test_an_event_during_the_http_wait_returns_early(wait_app):
    threading.Timer(0.3, wait_app.hub.publish, args=("detector", {"detector_id": "det-1"})).start()
    t0 = time.monotonic()
    body = post(wait_app, {"conditions": [{"type": "detector", "detector_id": "det-1"}],
                           "timeout_s": 30}).json()
    assert time.monotonic() - t0 < 3.0
    assert body["fired"][0]["event"]["detector_id"] == "det-1"


def test_an_unknown_job_fires_at_once_with_an_error(wait_app):
    body = post(wait_app, {"conditions": [{"type": "job", "job_id": "j_000000000000"}],
                           "timeout_s": 30}).json()
    assert body["fired"][0]["event"]["error"] == "unknown_job"


@pytest.mark.parametrize("body, code", [
    ({"conditions": [{"type": "nope"}]}, "invalid_condition"),
    ({"conditions": [{"type": "job"}]}, "invalid_condition"),
    ({"conditions": [{"type": "frame"}], "since": "c1.zzzz"}, "invalid_cursor"),
    ({"conditions": [{"type": "frame"}], "timeout_s": 900}, "invalid_request"),
    ({"conditions": []}, "invalid_request"),
    ({"conditions": "frame"}, "invalid_request"),
])
def test_bad_requests_are_422_with_a_code(wait_app, body, code):
    r = post(wait_app, body)
    assert r.status_code == 422 and r.json()["error"] == code, r.text


def test_an_observation_failure_does_not_lose_what_fired(wait_app):
    wait_app.svc.obs = RuntimeError("archive unreadable")
    r = post(wait_app, {"conditions": [{"type": "timer", "seconds": 0}], "timeout_s": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["fired"][0]["type"] == "timer" and "archive unreadable" in body["observation_error"]


def test_not_wired_is_a_declared_503(wait_app, monkeypatch):
    import mast.vista.service as vista_service

    wait_app.app.state.vista = None
    monkeypatch.setattr(vista_service, "get_service", lambda ctx=None: None)
    r = post(wait_app, {"conditions": [{"type": "frame"}], "timeout_s": 0})
    assert r.status_code == 503 and r.json()["error"] == "not_wired" and r.json()["missing"] == ["vista"]

    def boom(ctx=None):
        raise RuntimeError("no experiment log")

    monkeypatch.setattr(vista_service, "get_service", boom)
    r = post(wait_app, {"conditions": [{"type": "frame"}], "timeout_s": 0})
    assert r.status_code == 503 and "no experiment log" in r.json()["detail"]

    # a service object without a hub is not wired either
    wait_app.app.state.vista = SimpleNamespace(hub=None)
    r = post(wait_app, {"conditions": [{"type": "frame"}], "timeout_s": 0})
    assert r.status_code == 503 and r.json()["missing"] == ["vista.hub"]
