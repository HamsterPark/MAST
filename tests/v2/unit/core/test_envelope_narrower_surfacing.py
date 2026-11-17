"""A configured envelope NARROWER than the measured travel must not block silently.

Motivating case: an override file narrower than the real Z travel. AutoApproach
engages near the bottom of the range, so every later ``SetZPosition`` below the
configured minimum was refused with a bare "violates global safety minimum". The
boot reconciliation had flagged the NARROWER finding -- once, in the log. Nothing
read it afterwards.

These tests pin the *surfacing*, not any widening: the cache holds findings only,
the guard's limits never change, and a stale finding expires on its own when the
override is edited.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mast.api.context import AppContext
from mast.config import SafetyLimits
from mast.core import envelope_reconcile as er
from mast.core.safety import SafetyGuard
from mast.core.types import ParameterSpec, SafetyLevel, SkillMetadata


@dataclass
class _Rec:
    return_value: Any = None
    error: str = ""


# Example numbers: Piezo_RangeGet Z full range = 2 x 208.7324 nm; XY +/-1.2195 um.
_RIG = {
    "Piezo_RangeGet": _Rec(("", b"", [2.439e-6, 2.439e-6, 4.174647e-7])),
    "ZCtrl_LimitsGet": _Rec(("", b"", [2.087323593968904e-07, -2.087323593968904e-07])),
    "ZCtrl_LimitsEnabledGet": _Rec(("", b"", [0])),
}
_STALE = dict(z_min_m=-1.695e-07, z_max_m=1.695e-07, xy_min_m=-1.22e-06, xy_max_m=1.22e-06,
              scan_size_max_m=2.439e-06)
_FIXED = {**_STALE, "z_min_m": -2.05e-07, "z_max_m": 2.05e-07,
          "xy_min_m": -1.2195e-06, "xy_max_m": 1.2195e-06}


def _safe_call(verb, *_a, **_k):
    return _RIG.get(verb, _Rec(error="unsupported"))


@pytest.fixture(autouse=True)
def _clean_cache():
    er._forget_reconciliation()
    yield
    er._forget_reconciliation()


def _set_z_meta():
    return SkillMetadata(
        name="SetZPosition", description="", safety_level=SafetyLevel.AUTO,
        parameters=[ParameterSpec(name="z_pos_m", type="float", unit="m", required=True)],
    )


def _reconcile(limits):
    res = er.reconcile_envelope(_safe_call, limits)
    er.remember_reconciliation(res)
    return res


# -- rejection message ---------------------------------------------------------


def test_rejection_names_both_numbers_and_the_fix_once_reconciled():
    limits = SafetyLimits(**_STALE)
    _reconcile(limits)
    msgs = SafetyGuard(limits).check_parameter_bounds(_set_z_meta(), {"z_pos_m": -1.9e-07})
    assert len(msgs) == 1
    m = msgs[0]
    assert "violates global safety minimum" in m          # original verdict kept
    assert "NARROWER than the measured instrument travel" in m
    assert "z_min_m" in m and "-1.695e-07" in m           # configured number
    assert "2.08732e-07" in m                              # measured number
    assert "Piezo_RangeGet/2" in m
    assert "/api/admin/overrides/safety_limits" in m and "instrument-init" in m
    assert "never beyond the measured travel" in m


def test_upper_bound_gets_the_same_hint():
    limits = SafetyLimits(**_STALE)
    _reconcile(limits)
    m = SafetyGuard(limits).check_parameter_bounds(_set_z_meta(), {"z_pos_m": 1.9e-07})[0]
    assert "violates global safety maximum" in m and "z_max_m" in m and "NARROWER" in m


def test_no_hint_before_any_reconciliation():
    msgs = SafetyGuard(SafetyLimits(**_STALE)).check_parameter_bounds(
        _set_z_meta(), {"z_pos_m": -1.9e-07})
    assert len(msgs) == 1 and "NARROWER" not in msgs[0]


def test_hint_expires_when_the_override_is_corrected():
    _reconcile(SafetyLimits(**_STALE))
    fixed = SafetyLimits(**_FIXED)
    # still outside the (now wider) bound -> still rejected, but no stale claim
    m = SafetyGuard(fixed).check_parameter_bounds(_set_z_meta(), {"z_pos_m": -2.1e-07})[0]
    assert "violates global safety minimum" in m and "NARROWER" not in m
    assert all(f.field_name not in ("z_min_m", "z_max_m")
               for f in er.active_narrower_findings(fixed))


def test_wider_than_travel_gets_no_narrower_hint():
    limits = SafetyLimits(**{**_STALE, "z_min_m": -5e-07, "z_max_m": 5e-07})
    _reconcile(limits)
    m = SafetyGuard(limits).check_parameter_bounds(_set_z_meta(), {"z_pos_m": -6e-07})[0]
    assert "NARROWER" not in m


def test_hint_never_changes_the_guard_limits_or_the_verdict():
    limits = SafetyLimits(**_STALE)
    _reconcile(limits)
    g = SafetyGuard(limits)
    before = g._limits.model_dump()
    assert g.check_parameter_bounds(_set_z_meta(), {"z_pos_m": -1.9e-07})
    assert g._limits.model_dump() == before
    assert g.check_parameter_bounds(_set_z_meta(), {"z_pos_m": -1.0e-07}) == []


def test_agent_path_gate_carries_the_same_hint():
    from mast.agents._shared.safety_mw import SafetyGate

    limits = SafetyLimits(**_STALE)
    _reconcile(limits)
    v = SafetyGate(limits).check_global_bounds(_set_z_meta(), {"z_pos_m": -1.9e-07})
    assert len(v) == 1 and "NARROWER than the measured instrument travel" in v[0]


# -- cache ---------------------------------------------------------------------


def test_boot_reconcile_remembers_its_result():
    class _Pool:
        safe_call = staticmethod(_safe_call)

    class _Guard:
        _limits = SafetyLimits(**_STALE)

    class _Rt:
        _pool = _Pool()
        _safety = _Guard()

    assert er.last_reconciliation() is None
    res = er.reconcile_at_boot(_Rt(), background=False)
    cached = er.last_reconciliation()
    assert cached is not None and cached[0] is res
    assert {f.field_name for f in er.active_narrower_findings(_Guard._limits)} >= {
        "z_min_m", "z_max_m"}


# -- /api/safety/limits header and instrument-init list -------------------------


def _client(limits: SafetyLimits, router) -> TestClient:
    class _G:
        _limits = limits

    class _Rt:
        _safety = _G()

    ctx = AppContext()
    ctx.runtime = _Rt()
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_limits_header_unknown_then_flagged_then_none():
    from mast.api.routes.safety import router

    limits = SafetyLimits(**_STALE)
    c = _client(limits, router)
    assert c.get("/api/safety/limits").headers["x-safety-envelope-narrower"] == "unknown"
    _reconcile(limits)
    hdr = c.get("/api/safety/limits").headers["x-safety-envelope-narrower"]
    assert "z_min_m" in hdr and "z_max_m" in hdr
    fixed = SafetyLimits(**_FIXED)
    _reconcile(fixed)
    assert _client(fixed, router).get("/api/safety/limits").headers[
        "x-safety-envelope-narrower"] == "none"


def test_init_list_surfaces_narrower_as_outstanding_required():
    from mast.api.routes.instrument_init import router

    _reconcile(SafetyLimits(**_STALE))
    body = _client(SafetyLimits(**_STALE), router).get("/api/instrument-init").json()
    assert any("z_min_m" in s for s in body["envelope_narrower"])
    assert "safety_limits.z_min_m" in body["outstanding_required"]
    assert body["needs_setup"] is True and body["should_prompt"] is True
