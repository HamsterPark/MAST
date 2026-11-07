"""Smoke: the real MAST service on the STM-Bench simulator, driven over ``/api/ext/v1``.

What it pins, for the 7.0 end-to-end and MCP tests that build on the same bring-up:

* ``vista_sim.py serve`` boots a fully wired service against the simulated controller;
* the operator facts RuntimeHost syncs (tip, profile, tilt, coarse drive, scope, vacuum) are in;
* the scope can be set over HTTP;
* a ScanAt job runs to ``succeeded``, the simulator saves a .sxm that MAST's own reader opens,
  and ``/data/frame`` serves it;
* a read skill (GetBias) runs as a job and returns the controller's value.

Opt-in (``MAST_E2E_SIM=1``) and STM-Bench on ``PYTHONPATH``: see ``conftest.py``.
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports (canonical block for tests/v2/) ──
import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[3] / "MASTv2")
if sys.path and sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _n in list(sys.modules):
    if _n == "mast" or _n.startswith("mast."):
        _f = getattr(sys.modules[_n], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_n]

import io
import math
import time

import httpx
import numpy as np
import pytest

pytestmark = pytest.mark.e2e_sim

HEADERS = {"X-MAST-Actor": "e2e-sim", "X-MAST-Session": "smoke"}
#: a small frame: 64 lines x 2 directions x 0.05 s = 6.4 s of scanning on the simulator
PIXELS = 64
SCAN = {"center_x_m": 0.0, "center_y_m": 0.0, "size_m": 20e-9, "pixels": PIXELS,
        "line_time_s": 0.05}
JOB_BUDGET_S = 90.0


@pytest.fixture(scope="module")
def client():
    # trust_env=False: a system proxy must never sit between the test and a loopback service
    with httpx.Client(timeout=60.0, trust_env=False, headers=HEADERS) as c:
        yield c


def _run_job(client: httpx.Client, ext: str, skill: str, params: dict, *, request_id: str) -> dict:
    r = client.post(f"{ext}/jobs", json={"skill": skill, "params": params,
                                         "request_id": request_id, "note": "e2e_sim smoke"})
    assert r.status_code == 202, r.text
    job = r.json()
    deadline = time.monotonic() + JOB_BUDGET_S
    while not job["terminal"]:
        assert time.monotonic() < deadline, f"{skill} not terminal after {JOB_BUDGET_S:.0f} s: {job}"
        r = client.get(f"{ext}/jobs/{job['job_id']}", params={"wait_s": 20})
        assert r.status_code == 200, r.text
        job = r.json()
    return job


def test_health_reports_a_fully_wired_service(sim_service, client):
    r = client.get(f"{sim_service.ext_url}/health")
    assert r.status_code == 200, r.text
    assert r.headers.get("X-MAST-Ext-Version") == "1"
    body = r.json()
    assert body["ok"] is True and body["missing"] == [], body


def test_operator_facts_were_synced(sim_service):
    facts = sim_service.info["facts"]
    failed = {k: v for k, v in facts.items() if isinstance(v, dict) and v.get("ok") is False}
    assert not failed, failed
    assert facts["tip"]["tip_id"], facts["tip"]
    assert facts["experiment"]["experiment_id"] and facts["experiment"]["sample_id"], facts


def test_scope_can_be_set_over_http(sim_service, client):
    ext = sim_service.ext_url
    r = client.post(f"{ext}/scope", json={
        "experiment": {"name": "e2e-sim smoke", "goal": "bring-up smoke on stmsim"},
        "sample": {"name": "Au(111) seed0 smoke"},
        "reason": "e2e_sim smoke"})
    assert r.status_code == 200, r.text
    assert r.json()["changed"], r.json()
    scope = client.get(f"{ext}/scope").json()
    assert scope["experiment"]["name"] == "e2e-sim smoke", scope
    assert scope["sample"]["name"] == "Au(111) seed0 smoke", scope


def test_scanat_job_saves_a_frame_mast_reads(sim_service, client):
    session = sim_service.session_dir
    before = set(session.glob("*.sxm"))
    job = _run_job(client, sim_service.ext_url, "ScanAt", SCAN, request_id="e2e-scan-1")
    assert job["state"] == "succeeded", job
    data = job["result"]["data"]
    assert data["wait_timed_out"] is False and data["lines_done"] == PIXELS, data

    new = sorted(set(session.glob("*.sxm")) - before)
    assert len(new) == 1, f"expected one new .sxm in {session}, found {new}"
    from mast.io.nanonis_files import read_sxm

    z = read_sxm(str(new[0]))["channels"]["Z"]["forward"]
    assert z.shape == (PIXELS, PIXELS)
    assert np.isfinite(z).all() and float(np.ptp(z)) > 0.0       # a surface, not a constant

    r = client.get(f"{sim_service.ext_url}/data/frame", params={"path": str(new[0]), "channel": "Z"})
    assert r.status_code == 200, r.text
    served = np.load(io.BytesIO(r.content), allow_pickle=False)
    assert served["forward"].shape == (PIXELS, PIXELS)


def test_getbias_job_reads_the_simulated_controller(sim_service, client):
    job = _run_job(client, sim_service.ext_url, "GetBias", {}, request_id="e2e-bias-1")
    assert job["state"] == "succeeded", job
    bias = job["result"]["data"]["bias_v"]
    # the wire carries float32: 0.1 V comes back as 0.10000000149
    assert math.isclose(bias, sim_service.info["initial_bias_v"], abs_tol=1e-6), bias
