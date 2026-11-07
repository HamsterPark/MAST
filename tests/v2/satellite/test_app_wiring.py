"""接线：路由进了真正的 create_app、AccessGate 对卫星放行而对 REST 要 Basic、EventType、
冻结包声明、惰性导出。"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.v2.satellite.conftest import start_server, stop_server, wait_for, write_sat_config

_REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def app_default():
    from mast.api.app import create_app

    return create_app()


def test_routes_are_registered_in_the_real_app(app_default):
    paths = {getattr(r, "path", "") for r in app_default.routes}
    for p in ("/ws/satellite/v1", "/api/satellites", "/api/satellites/files",
              "/api/satellites/{sat_id}", "/api/satellites/{sat_id}/devices",
              "/api/satellites/{sat_id}/events", "/api/port-guard"):
        assert p in paths, p


def test_files_literal_is_not_shadowed_by_sat_id(app_default, sat_root):
    from fastapi.testclient import TestClient

    from mast.satellite.hub import SatelliteHub, reset_satellite_hub

    old = reset_satellite_hub(SatelliteHub(data_root=sat_root, enabled_fn=lambda: True))
    try:
        with TestClient(app_default) as cl:
            r = cl.get("/api/satellites/files")
            assert r.status_code == 200 and "files" in r.json()
            assert cl.get("/api/satellites/files-not").status_code == 404
            r = cl.get("/api/satellites")
            assert r.status_code == 200 and r.json()["satellites"] == []
    finally:
        reset_satellite_hub(old)


def test_access_gate_lets_satellites_through_but_guards_rest(hub, sat_root, sims):
    from mast.api.app import create_app

    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    app = create_app(dev_cors=False, auth=("op", "pw"))
    srv = start_server(app)
    try:
        sim = sims(srv.ws_url, token)
        assert sim.wait_connected(15), sim.sat.last_close
        wait_for(lambda: hub.link_state("sim1") == "connected")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(srv.http + "/api/satellites", timeout=5)
        assert ei.value.code == 401
        req = urllib.request.Request(srv.http + "/api/satellites", headers={
            "Authorization": "Basic " + base64.b64encode(b"op:pw").decode()})
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read())
        assert body["satellites"][0]["state"] == "connected"
        # 没令牌的 WS 仍被卫星端点自己拒（401），不会因为放行了 Basic 就裸奔
        from tests.v2.satellite.test_integration import _ws_status

        assert _ws_status(srv.ws_url, {"X-MAST-Satellite-Id": "sim1",
                                       "X-MAST-Satellite-Proto": "1"}) == 401
    finally:
        stop_server(srv)


def test_event_type_and_publisher():
    from mast.core.events import Event, EventBus, EventType

    assert EventType.SATELLITE.value == "satellite"
    bus = EventBus()
    seen = []
    bus.subscribe(lambda ev: seen.append(ev))
    bus.publish_satellite("rig1", "link", state="lost")
    assert seen[0].type is EventType.SATELLITE
    assert seen[0].data == {"sat_id": "rig1", "kind": "link", "state": "lost"}
    assert isinstance(seen[0], Event)


def test_frozen_build_declares_the_new_packages():
    src = (_REPO / "mast2.spec").read_text(encoding="utf-8")
    for name in ('"mast.satellite"', '"mast.live"', '"mast.core.port_guard"', '"psutil"'):
        assert name in src, name


def test_admin_import_does_not_pull_in_the_hub():
    code = ("import sys; import mast.satellite.admin as a; "
            "print(int('mast.satellite.hub' in sys.modules), int('mast.satellite.protocol' in sys.modules))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                       env={**__import__("os").environ, "PYTHONPATH": str(_REPO / "MASTv2")})
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["0", "0"]


def test_public_api_names_resolve():
    import mast.satellite as s

    for name in ("get_satellite_hub", "SatelliteError", "SatelliteUnavailable",
                 "SatelliteCommandError", "SatelliteTimeout", "PendingCommand", "PROTO_VERSION"):
        assert getattr(s, name) is not None
    assert issubclass(s.SatelliteTimeout, s.SatelliteError)
    with pytest.raises(AttributeError):
        s.nope  # noqa: B018
