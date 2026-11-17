# -*- coding: utf-8 -*-
"""Shell of DiagnoseMainsGapModulation (core: tests/v2/unit/monitoring/test_mains_gap.py)."""
from __future__ import annotations

import math

import numpy as np

from mast.core.types import NanonisCallRecord, SafetyLevel, SkillCategory
from mast.skills.builtins.diagnose_mains_gap import DiagnoseMainsGapModulation

DT = 5e-4


class Ctx:
    def __init__(self, canned):
        self.canned, self.calls = canned, []

    def safe_call(self, method, *args, role="main"):
        self.calls.append(method)
        e = self.canned.get(method)
        if e is None:
            return NanonisCallRecord(method=method, args=args, error="unmocked")
        if callable(e):
            e = e()
        return NanonisCallRecord(method=method, args=args, return_value=e.get("return_value"),
                                 error=e.get("error", ""))


def _trace(seed, pm=80.0, sign=-1.0):
    rng = np.random.default_rng(seed)
    t = np.arange(256) * DT
    ln = (math.log(50.0) + pm / 1000 * 19.0 * np.sin(2 * math.pi * 300.1 * t + rng.uniform(0, 6))
          + rng.normal(0, 0.02, 256))
    return ("", b"", [0.0, DT, 256, [float(v) for v in sign * np.exp(ln) * 1e-12]])


def _ctx(**kw):
    seeds = iter(range(100))
    return Ctx({"Osci1T_Run": {"return_value": ("", b"", [0])},
                "Osci1T_DataGet": lambda: {"return_value": _trace(next(seeds), **kw)}})


def test_metadata():
    m = DiagnoseMainsGapModulation().metadata()
    assert m.category == SkillCategory.READ and m.safety_level == SafetyLevel.AUTO
    assert {p.name for p in m.parameters} >= {"n_traces", "mains_hz", "max_harmonic", "two_kappa_nm", "floor_pa"}


def test_registered():
    from mast.skills.builtins import DiagnoseMainsGapModulation as D
    assert D is DiagnoseMainsGapModulation


def test_execute_negative_current_80pm():
    ctx = _ctx()
    r = DiagnoseMainsGapModulation().execute(ctx, {"n_traces": 4})
    assert r.success, r.error
    d = r.data
    assert d["dominant_hz"] == 300.0 and abs(d["dominant_pm"] - 80.0) < 4.0
    assert d["mains_locked"] and d["verdict"] == "fix_source"
    assert d["guidance"] and d["summary"]
    assert set(ctx.calls) <= {"Osci1T_Run", "Osci1T_DataGet"}     # read-only, no ChSet by default
    assert ctx.calls.count("Osci1T_DataGet") == 4


def test_guidance_kappa_validity_note_every_verdict():
    from mast.i18n import use_lang
    for pm, verdict in ((5.0, "ok"), (80.0, "fix_source")):
        for lang, keys in (("zh", ("2κ", "two_kappa_nm", "25–150 pm")),
                           ("en", ("2kappa", "two_kappa_nm", "25-150 pm"))):
            with use_lang(lang):
                r = DiagnoseMainsGapModulation().execute(_ctx(pm=pm), {"n_traces": 4})
            assert r.data["verdict"] == verdict
            assert any(all(k in x for k in keys) for x in r.data["guidance"]), (pm, lang)


def test_guidance_no_withdrawn_suspension_numbers():
    from mast.i18n import use_lang
    for lang in ("zh", "en"):
        with use_lang(lang):
            r = DiagnoseMainsGapModulation().execute(_ctx(), {"n_traces": 4})
        g = " ".join(r.data["guidance"])
        assert "149" not in g
        # never present a small reading as an effect of the suspension lock + release
        assert "→ 0.1" not in g and "after lock + release" not in g and "锁紧再松开后" not in g
        # the validity caveat stays: a small reading means nothing until 2kappa is verified
        assert ("毫无意义" if lang == "zh" else "means nothing") in g


def test_signal_index_only_first_trace():
    ctx = _ctx()
    ctx.canned["Osci1T_ChSet"] = {"return_value": ("", b"", [0])}
    DiagnoseMainsGapModulation().execute(ctx, {"n_traces": 3, "signal_index": 5})
    assert ctx.calls.count("Osci1T_ChSet") == 1


def test_osci_not_loaded():
    ctx = Ctx({"Osci1T_Run": {"error": "NeedModule Osci1T"}})
    r = DiagnoseMainsGapModulation().execute(ctx, {})
    assert not r.success and "Osci1T" in r.error


def test_no_samples():
    ctx = Ctx({"Osci1T_Run": {"return_value": ("", b"", [0])},
               "Osci1T_DataGet": {"return_value": ("", b"", [0.0, DT, 0, []])}})
    r = DiagnoseMainsGapModulation().execute(ctx, {"n_traces": 2})
    assert not r.success and r.error


def test_all_below_floor():
    low = ("", b"", [0.0, DT, 256, [1e-13] * 256])
    ctx = Ctx({"Osci1T_Run": {"return_value": ("", b"", [0])}, "Osci1T_DataGet": {"return_value": low}})
    r = DiagnoseMainsGapModulation().execute(ctx, {"n_traces": 2})
    assert not r.success and "floor" in r.error
