"""Optical-station protocols: SHGExcitationScan, PolarizationMatrix, TemperatureSeries, PowerSeries.

Only simulated devices (``sim_microscope`` / ``sim_tunable_laser`` / ``sim_laser`` /
``sim_power_meter`` / ``sim_temperature``), the in-memory operator backends and
``prompts.FakeOperator`` — no hardware, no lab computer. Files land in a temporary experiment folder.

What is pinned (docs/v2/design/optical_station_witec.md §6.3):

* every protocol is a CONFIRM write skill behind the sample gate, with a ``dry_run`` that touches no
  hardware and posts no prompt;
* out of range is refused before anything is touched, never clamped;
* every human step goes through the operator prompts; the protocols never write an optical-state slot;
* whatever ends a run, the safe direction is taken (source emission off, WITec shutter closed,
  temperature controller stopped) and everything measured so far is kept, the summary saying where and why;
* the sidecar carries all conditions, the optical-state snapshot and the device status.
"""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[5] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import csv
import dataclasses
import json
import re
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from mast.core.instrument_lock import needs_token
from mast.core.sample_gate import requires_sample
from mast.core.types import SafetyLevel
from mast.i18n import use_lang
from mast.instruments.base import InstrumentError, OperationNotSupported
from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry
from mast.live.testing import LiveRecorder, isolated_live_hub
from mast.logging import experiment_log as el
from mast.operator_hitl import optical_state, prompts
from mast.skills.builtins import optics_protocols as op
from mast.skills.builtins.optics_protocols import (
    PolarizationMatrix,
    PowerSeries,
    SHGExcitationScan,
    TemperatureSeries,
)

ALL = (SHGExcitationScan, PolarizationMatrix, TemperatureSeries, PowerSeries)


# ── fixtures ────────────────────────────────────────────────────────────────────────────────────


class FakeStorage:
    def __init__(self):
        self.exp = {"e1": {"name": "Optical protocols", "start_time": "2026-11-03T10:00:00", "dir_name": ""}}
        self.smp = {"s1": {"name": "WS2 flake", "sample_index": 1, "dir_name": "", "description": "",
                           "sample_type": "", "sample_subtype": "", "start_time": "2026-11-03T10:01:00"}}

    def get_experiment(self, eid):
        return dict(self.exp[eid]) if eid in self.exp else None

    def get_sample(self, sid):
        return dict(self.smp[sid]) if sid in self.smp else None

    def sample_ordinal(self, sid):
        return 1

    def set_experiment_dir_name(self, eid, name):
        self.exp[eid]["dir_name"] = name

    def set_sample_dir_name(self, sid, name, idx):
        self.smp[sid]["dir_name"] = name

    def get_samples(self, eid):
        return [dict(v, id=k) for k, v in self.smp.items()]


@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setenv("MAST_LANG", "en")


@pytest.fixture(autouse=True)
def fresh_operator_backends():
    old_b = prompts.set_backend(prompts.InMemoryPromptBackend())
    store = optical_state.OpticalStateStore()
    broad = []
    for s in store.slots():
        if s.id == op.SLOT_WAVEPLATE:        # a broadband plate so a wide scan needs one swap only
            s = dataclasses.replace(s, options=tuple(list(s.options) + [
                {"id": "hwp_broad", "label": "broadband λ/2", "meta": {"valid_nm": [600, 1400]}}]))
        broad.append(s)
    store.set_slots(broad)
    old_s = optical_state.set_store(store)
    yield
    prompts.set_backend(old_b)
    optical_state.set_store(old_s)


@pytest.fixture
def scope(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST_EXPERIMENT_ROOT", str(tmp_path / "experiments"))
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))
    storage = FakeStorage()
    log = SimpleNamespace(_storage=storage, current_experiment_id="e1", current_sample_id="s1")
    prev = el.get_active_log()
    el.set_active_log(log)
    yield SimpleNamespace(storage=storage, log=log, root=tmp_path)
    el.set_active_log(prev)


def _build_bench(tmp_path, *, write_access=True, tc=None, mic_limits=None, superk_limits=None,
                 npi_emission=True, tc_write=True, superk_write=True, superk_emission=True):
    mic_entry = {"id": "witec", "type": "sim_microscope", "time_scale": 0.0, "sim_write_access": write_access}
    if mic_limits:
        mic_entry["microscope_limits"] = mic_limits
    superk = {"id": "superk", "type": "sim_tunable_laser", "write_enabled": superk_write,
              "allow_emission_control": superk_emission}
    if superk_limits:
        superk["limits"] = superk_limits
    cfg = {"devices": [
        mic_entry, superk,
        {"id": "npi", "type": "sim_laser", "wavelength_nm": 1064.0, "allow_emission_control": npi_emission},
        {"id": "pm", "type": "sim_power_meter"},
        {"id": "tc", "type": "sim_temperature", "write_enabled": tc_write,
         **(tc or {"time_scale": 600.0, "tau_s": 1.0})},
    ]}
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    reg = InstrumentRegistry(p)
    reset_instrument_registry(reg)
    b = SimpleNamespace(reg=reg, mic=reg.microscope("witec"), tun=reg.laser("superk"), npi=reg.laser("npi"),
                        pm=reg.power_meter("pm"), tc=reg.temperature_controller("tc"))
    b.mic.shg_source = lambda: b.tun.output()
    b.pm.source = lambda: (b.tun.output() or (0.0, 0.0))[1]
    return b


@pytest.fixture
def bench(tmp_path, scope):
    b = _build_bench(tmp_path)
    yield b
    reset_instrument_registry(None)


@pytest.fixture
def make_bench(tmp_path, scope):
    def make(**kw):
        return _build_bench(tmp_path, **kw)

    yield make
    reset_instrument_registry(None)


class Ctx:
    """What the protocols need from an ExecutionContext: ``check_abort``."""

    def __init__(self, abort=lambda: False):
        self._abort = abort

    def check_abort(self):
        return bool(self._abort())


class Operator:
    """A scripted operator: answers every prompt by its ``context['reason']``.

    ``overrides[reason](prompt) -> reply | None`` replaces the default answer (None = never answer).
    Every prompt it saw is in ``seen``; an exception in a policy is kept in ``errors`` (a crashed
    policy would otherwise look like an operator timeout)."""

    def __init__(self, bench=None, **overrides):
        self.bench = bench
        self.overrides = overrides
        self.seen: list = []
        self.errors: list = []
        self.emission_at_prompt: list = []
        #: what the bench looked like when each prompt was posted (nothing configured yet? no exposure yet?)
        self.at_prompt: list = []

    def reasons(self):
        return [p.context.get("reason") for p in self.seen]

    def __call__(self, p):
        reason = p.context.get("reason")
        self.seen.append(p)
        try:
            if self.bench is not None:
                self.emission_at_prompt.append((reason, self.bench.tun.emitting))
                self.at_prompt.append({"reason": reason, "emitting": self.bench.tun.emitting,
                                       "beam_path": self.bench.mic.beam_path,
                                       "acquisitions": self.bench.mic.acquisitions,
                                       "series": len(self.bench.mic.series_runs)})
            if reason in self.overrides:
                return self.overrides[reason](p)
            return self.default(p, reason)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(repr(exc))
            return None

    @staticmethod
    def default(p, reason):
        if reason == "laser_attest":
            return {"value": True}                           # a plain attest: the operator signs "true"
        if reason == "waveplate":
            return {"choice": (p.context.get("covering") or [p.options[0].id])[0]}
        if reason == "incident_polarizer":
            if p.options:
                return {"choice": p.options[0].id}
            return {"value": p.context["requested_deg"]}
        if reason == "power":
            return {"value": p.context["requested"]}
        if reason == "center":
            return {"value": p.context["requested_nm"]}
        if reason == "power_reading":
            return {"value": 1.0}
        return {"choice": "yes"}                              # write_access, manual_emission


def run(skill_cls, params, *, bench=None, operator=None, ctx=None, limit_s=45.0, **overrides):
    """Execute *skill_cls* with a scripted operator. Returns ``(result, operator)``.

    A watchdog aborts a run that does not finish within ``limit_s``: a regression that makes a bounded
    wait unbounded then fails this test instead of hanging the whole suite."""
    operator = operator or Operator(bench, **overrides)
    ctx = ctx or Ctx()
    watchdog = threading.Event()
    inner = ctx._abort
    ctx._abort = lambda: bool(inner()) or watchdog.is_set()
    box: dict = {}

    def target():
        try:
            box["res"] = skill_cls().execute(ctx, params)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            box["exc"] = exc

    with prompts.FakeOperator(operator, interval_s=0.005):
        worker = threading.Thread(target=target, daemon=True, name="protocol-under-test")
        worker.start()
        worker.join(limit_s)
        if worker.is_alive():
            watchdog.set()
            worker.join(10)
            pytest.fail(f"{skill_cls.__name__} did not finish within {limit_s:.0f} s (the watchdog aborted it)")
    if "exc" in box:
        raise box["exc"]
    assert not operator.errors, operator.errors
    return box["res"], operator


def unattended(skill_cls, params):
    """Execute with nobody to answer prompts. A plan that is NOT refused blocks on its first prompt, so that wait is
    bounded: the run then stops by itself and the assertions about the refusal fail, instead of the suite hanging."""
    return skill_cls().execute(Ctx(), {**params, "prompt_timeout_s": 0.05})


SHG = {"laser_id": "superk", "amplitude_pct": 5, "integration_s": 0.2, "settle_s": 0}


def shg_params(**kw):
    return {**SHG, "wavelengths_nm": "700,800,900", **kw}


def sidecar_of(npy_path) -> dict:
    return json.loads(Path(npy_path).with_suffix(".json").read_text(encoding="utf-8"))


def summary_of(res) -> dict:
    return json.loads(Path(res.data["summary_path"]).read_text(encoding="utf-8"))


def assert_safe(b):
    """The safe direction: nothing emits, the WITec shutter is shut."""
    assert b.tun.emitting is False
    assert b.npi.emitting is False
    assert b.mic.shutter_open is False


def preset_by_hand(src, lit):
    """The operator set amplitudes in the vendor software while the source is still off ({channel: %}); setting
    one on a source that is not emitting starts no light, so it passes the gates."""
    for ch, pct in lit.items():
        src.set_amplitude_pct(pct, channel=ch)


def switched_on_by_hand(src, lit=None):
    """The operator switched *src* on in the vendor software before the protocol started. ``lit`` = {channel: %}
    of the channels that carry light; every other channel stays at 0."""
    preset_by_hand(src, lit or {})
    src.emitting = True


# ── registration, metadata, helpers ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cls", ALL)
def test_metadata_contract(cls):
    meta = cls().metadata()
    assert meta.name == cls.__name__
    assert meta.safety_level is SafetyLevel.CONFIRM
    assert {"spectrum", "acquisition"} <= set(meta.tags)
    assert requires_sample(meta, meta.name), "behind the sample gate"
    assert needs_token(meta, meta.name), "takes the instrument token"
    names = [p.name for p in meta.parameters]
    assert len(names) == len(set(names))
    assert "dry_run" in names
    assert all(p.type in ("float", "int", "str", "bool") for p in meta.parameters)
    with use_lang("zh"):
        zh = cls().metadata()
    with use_lang("en"):
        en = cls().metadata()
    assert zh.description != en.description
    for pz, pe in zip(zh.parameters, en.parameters):
        assert pz.name == pe.name and pz.description and pe.description


def test_registered_for_the_frozen_build():
    from mast.core.registry import SkillRegistry

    reg = SkillRegistry()
    reg.discover("mast.skills.builtins")
    have = {m.name for m in reg.list_skills()}
    assert {c.__name__ for c in ALL} <= have
    assert "_Protocol" not in have


def test_number_lists_and_ranges():
    assert op._number_list("810, 820;830 840", "x") == [810, 820, 830, 840]
    assert op._number_list("[1, 2.5]", "x") == [1, 2.5]
    for bad in ("", "a,b", "[1, true]", "1, nan", "{}", "[1,"):
        with pytest.raises(op.ProtocolRefused):
            op._number_list(bad, "x")
    with pytest.raises(op.ProtocolRefused, match="more than"):
        op._number_list(",".join(["1"] * (op.MAX_STEPS + 1)), "x")
    assert op._frange(810, 830, 10, "x") == [810, 820, 830]
    assert op._frange(830, 810, 10, "x") == [830, 820, 810]                   # direction follows start -> stop
    assert op._frange(0.0, 1.0, 0.1, "x")[-1] == 1.0                          # float noise does not drop the end
    assert op._frange(5, 5, 1, "x") == [5]
    with pytest.raises(op.ProtocolRefused):
        op._frange(0, 1, 0, "x")
    with pytest.raises(op.ProtocolRefused, match="exceed"):
        op._frange(0, 1e6, 1, "x")
    kw = dict(list_key="l", start_key="a", stop_key="b", step_key="s", label="x")
    assert op._series({"l": "1,2"}, **kw) == [1, 2]
    assert op._series({"a": 1, "b": 3, "s": 1}, **kw) == [1, 2, 3]
    assert op._series({}, default=(0, 20, 10), **kw) == [0, 10, 20]
    for bad in ({"l": "1", "a": 1, "b": 2, "s": 1}, {"a": 1, "b": 2}, {}):
        with pytest.raises(op.ProtocolRefused):
            op._series(bad, **kw)


def test_names_are_short_safe_and_carry_the_conditions():
    assert op._sig(0.4217) == "0.422" and op._sig(1234.6, 3) == "1235" and op._sig(0) == "0"
    assert op._sig(-150.0, 4) == "-150" and op._sig(None) == "NA"
    assert op._stem("shg", "810nm", "p0.42mW", "a0deg") == "shg_810nm_p0.42mW_a0deg"
    long = op._stem(*["x" * 20] * 5, tag="tag")
    assert len(long) <= op.STEM_LIMIT and not long.endswith("_")
    assert op._safe("a/b:c*d?") == "abcd"


def test_analyzer_convention_reproduces_the_lab_history():
    """caption "0,0" "0,135" "10,5" "10,140" ... "360,135" of the manual par/crs sets."""
    kw = dict(scale=0.5, zero=0.0, crs_offset=135.0)
    assert [op.analyzer_reading(0, m, **kw) for m in ("par", "crs")] == [0.0, 135.0]
    assert [op.analyzer_reading(10, m, **kw) for m in ("par", "crs")] == [5.0, 140.0]
    assert op.analyzer_reading(360, "par", **kw) == 180.0
    assert op.analyzer_reading(360, "crs", **kw) == 315.0 and 315 % 180 == 135       # the history's "360,135"
    # 135 and 45 are the same physical setting for a half-wave-plate analyzer (period 90 in the reading)
    assert (135.0 - 45.0) % 90.0 == 0.0
    # a reading that reaches 360 is the same position as 0 — wrapped, not clamped
    assert op.analyzer_reading(360, "par", scale=1.0, zero=0.0, crs_offset=0.0) == 0.0
    assert op.analyzer_reading(10, "crs", scale=1.0, zero=0.0, crs_offset=-45.0) == 325.0


def _gauss_spectrum(center=500.0, sigma=0.8, amp=1000.0, bg=100.0, n=1340, dx=0.0485, seed=1):
    ax = center + (np.arange(n) - (n - 1) / 2) * dx
    rng = np.random.default_rng(seed)
    y = bg + amp * np.exp(-0.5 * ((ax - center) / sigma) ** 2) + rng.normal(0, 3, n)
    return ax, y


def test_feature_integral_finds_the_peak_and_ignores_a_hot_pixel():
    ax, y = _gauss_spectrum(center=500.0)
    y[10] += 1e6                                           # a cosmic ray far from the feature
    y[int(np.argmin(np.abs(ax - 503.0)))] += 5e5           # and one inside the search window
    out = op.feature_integral(ax, y, center_nm=500.4, window_nm=8, search_nm=10)
    assert out["found"] and abs(out["peak_nm"] - 500.0) < 0.2
    assert abs(out["integral"] - 1000 * 0.8 * np.sqrt(2 * np.pi)) < 0.2 * 1000 * 0.8 * np.sqrt(2 * np.pi) + 5e5 * 0.0485
    # no feature: noise only
    ax, y = _gauss_spectrum(amp=0.0)
    assert not op.feature_integral(ax, y, center_nm=500, window_nm=8, search_nm=10)["found"]
    # expected position outside the window / no axis
    assert "outside" in op.feature_integral(ax, y, center_nm=900, window_nm=8, search_nm=10)["reason"]
    assert op.feature_integral(None, y, center_nm=500, window_nm=8, search_nm=10)["found"] is False


def test_loglog_fit_recovers_the_exponent():
    xs = [1, 2, 4, 8, 16]
    fit = op.loglog_fit(xs, [3.0 * x ** 2 for x in xs])
    assert abs(fit["slope"] - 2.0) < 1e-9 and fit["r2"] > 0.999999
    assert op.loglog_fit([1, 2], [1, 2]) is None
    assert op.loglog_fit([0, -1, 2, 3, 4], [1, 1, 4, 9, 16])["n"] == 3        # non-positive pairs are dropped


# ── SHGExcitationScan ──────────────────────────────────────────────────────────────────────────────


def test_shg_dry_run_plans_without_touching_anything(bench, scope):
    res = SHGExcitationScan().execute(Ctx(), {**SHG, "wavelengths_nm": "540,560,1040,1060", "dry_run": True,
                                                "power_meter_id": "pm"})
    assert res.success, res.error
    d = res.data
    assert d["dry_run"] and d["valid"] and d["touched_hardware"] is False and d["n_steps"] == 4
    assert [s["manual"] for s in d["steps"]] == [["waveplate"], [], ["waveplate"], []]       # 532 plate, then 1064 plate
    assert d["steps"][2]["conditions"]["spectrometer_center_nm"] == 520.0
    assert d["before_start"] == ["laser_attest"], "the attestation comes before the first step"
    assert d["manual_prompts_expected"] == 3 and d["estimated_machine_s"] > 0
    assert any("attestation" in n for n in d["notes"])
    # nothing moved, nothing asked, nothing written
    assert bench.mic.calls == [] and bench.mic.acquisitions == 0
    assert bench.tun.emitting is False and bench.pm.reads == 0
    assert prompts.get_backend().list_open() == [] and prompts.get_backend()._prompts == {}
    assert not (scope.root / "root" / "artifacts" / "optics_protocols").exists()


def test_shg_dry_run_reports_every_problem_it_can_see(bench, scope):
    res = SHGExcitationScan().execute(Ctx(), {**SHG, "wavelengths_nm": "450,1500", "dry_run": True,
                                                "center_offset_nm": -200})
    assert not res.success and res.data["valid"] is False
    text = " | ".join(res.data["problems"])
    assert "no plate in slot excitation_waveplate covers" in text          # 900 nm: neither 532 nor 1064 nor broadband? (broadband is 600-1400)
    assert "outside the source range" in text and "spectrometer centre range" in text


def test_shg_dry_run_900nm_is_fine_with_the_broadband_plate(bench, scope):
    res = SHGExcitationScan().execute(Ctx(), {**SHG, "wavelengths_nm": "900", "dry_run": True})
    assert res.success and res.data["steps"][0]["manual"] == ["waveplate"]


def test_shg_full_scan_normalises_to_p_squared_and_records_everything(bench, scope):
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="700,800,900,1000,1100,1200,1300",
                                                 power_meter_id="pm", temperature_id="tc", tag="ws2"), bench=bench)
    assert res.success, res.error
    d = res.data
    assert d["status"] == "completed" and d["steps_done"] == 7 and d["stopped_at_step"] is None
    assert opr.reasons() == ["laser_attest", "waveplate"], "sign first, then start: one laser attestation, one plate prompt"
    assert opr.at_prompt[0]["beam_path"] == "video", "nothing had been configured when the signature was asked for"
    # the SHG peak sits at lambda/2, found everywhere
    for r in d["rows"]:
        assert r["found"] and abs(r["peak_offset_nm"]) < 1.0
        assert abs(r["peak_nm"] - r["excitation_nm"] / 2) < 1.0
    # P^2 normalisation flattens what the source's spectral roll-off bends
    a = d["analysis"]
    assert a["spread_raw"] > 0.2 and a["spread_i_over_p2"] < 0.25 * a["spread_raw"]
    assert a["spread_i_over_p2"] < a["spread_i_over_p"] < a["spread_raw"]
    # file names carry the conditions (nobody renames by hand)
    for r in d["rows"]:
        assert re.fullmatch(r"shg_\d+nm_p[\d.]+mW_a0deg_ws2", r["stem"]), r["stem"]
        npy = Path(r["file"])
        assert npy.is_file() and npy.with_suffix(".json").is_file() and "other" in npy.parts
        arr = np.load(npy)
        assert arr.shape[0] == 2 and np.all(np.isfinite(arr[0]))                 # axis row + 1 frame
    # incremental summary: json + csv + png, archived with the run
    doc = summary_of(res)
    assert doc["status"] == "completed" and doc["steps_done"] == 7 and doc["stop"] is None
    rows = list(csv.DictReader(Path(d["summary_csv_path"]).open(encoding="utf-8")))
    assert len(rows) == 7 and rows[0]["excitation_nm"] == "700.0" and float(rows[0]["i_over_p2"]) > 0
    assert Path(d["plot_path"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert d["stored_in_experiment"] is True
    # the safe direction and the source state
    assert_safe(bench)
    assert d["cleanup"]["emission"]["state_after"] is False and d["cleanup"]["shutter"] == "closed"
    assert d["cleanup"]["write_access"] == "released" and bench.mic.has_write_access() is False
    assert "microscope" not in d["cleanup"], "no panic stop after a normal finish"
    assert bench.tun.channels()[0]["amplitude_pct"] == 5.0                        # amplitude set once at the start


def test_shg_sidecar_has_every_condition_the_lab_used_to_type(bench, scope):
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="800,900", power_meter_id="pm", temperature_id="tc",
                                                 analyzer_deg=20), bench=bench)
    assert res.success, res.error
    side = sidecar_of(res.data["spectrum_files"][0])
    assert side["schema"] == "mast.spectrum/1" and side["protocol"]["name"] == "SHGExcitationScan"
    assert side["protocol"]["step"] == 1 and side["protocol"]["n_steps"] == 2
    c = side["conditions"]
    for key in ("excitation_nm", "power_mw", "analyzer_deg", "temperature_c", "spectrometer_center_nm",
                "integration_s", "accumulations", "amplitude_pct", "channel", "waveplate", "power_source"):
        assert key in c, key
    assert c["excitation_nm"] == 800.0 and c["spectrometer_center_nm"] == 400.0 and c["analyzer_deg"] == 20.0
    assert c["integration_s"] == 0.2 and c["power_source"] == "meter" and c["power_mw"] > 0
    assert c["temperature_c"] == pytest.approx(22.0, abs=0.5)
    # optical state: what the operator signed, who and where
    st = side["optical_state"]
    assert st["excitation_waveplate"]["value"] == "hwp_broad" and st["excitation_waveplate"]["source"].startswith("prompt:")
    assert st["laser_area_safe"]["value"] is True and st["laser_area_safe"]["valid"] is True
    assert side["optical_state_changed_during_exposure"] is False
    # device status of everything involved
    assert {"microscope", "spectrometer", "laser", "power_meter", "temperature"} <= set(side["instruments"])
    assert side["instruments"]["laser"]["status"]["emission"] is True               # it was emitting while measuring
    assert side["files"]["array"]["shape"] == [2, 1340] and len(side["files"]["array"]["sha256"]) == 64
    assert side["result"]["analysis"]["found"] is True
    # the first step carries the prompts that happened during it
    assert [p["kind"] for p in side["operator_prompts"]] == [] or side["operator_prompts"][0]["status"] == "answered"
    assert side["scope"]["experiment_id"] == "e1" and side["scope"]["sample_id"] == "s1"


def test_no_protocol_code_writes_an_optical_state_slot(bench, scope):
    """Only the operator's answer may write a slot: every history entry comes from a prompt answer."""
    src = Path(op.__file__).read_text(encoding="utf-8")
    assert "set_by_operator(" not in src and "clear_by_operator(" not in src
    store = optical_state.get_store()
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="800,900"), bench=bench)
    assert res.success, res.error
    assert store.history and all(h["source"].startswith("prompt:") for h in store.history)
    assert {h["slot"] for h in store.history} == {"excitation_waveplate", "laser_area_safe"}


def test_shg_swapping_the_plate_switches_the_beam_off_first(bench, scope):
    swap_seen: list = []

    def plate(p):
        swap_seen.append(bench.tun.emitting)
        return {"choice": "hwp_532" if p.context["excitation_nm"] < 700 else "hwp_1064"}

    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="540,560,1040"), bench=bench, waveplate=plate)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "waveplate", "waveplate"]
    assert swap_seen == [False, False], "the source is off while a person swaps the optic (also the first time)"
    assert [r["waveplate"] for r in res.data["rows"]] == ["hwp_532", "hwp_532", "hwp_1064"]
    assert bench.tun.channels()[0]["wavelength_nm"] == 1040.0
    side = sidecar_of(res.data["spectrum_files"][-1])
    assert side["conditions"]["waveplate"] == "hwp_1064"
    assert side["operator_prompts"] and side["operator_prompts"][0]["attest_slots"] == ["excitation_waveplate"]
    assert_safe(bench)


def test_shg_wrong_plate_stops_cleanly_and_keeps_the_data(bench, scope):
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="540,1040"), bench=bench,
                   waveplate=lambda p: {"choice": "hwp_532"})           # still the 532 plate for 1040 nm
    assert not res.success
    d = res.data
    assert d["status"] == "stopped" and d["stop_kind"] == "waveplate_not_covering"
    assert d["stopped_at_step"] == 2 and d["steps_done"] == 1
    assert len(d["spectrum_files"]) == 2 and all(Path(f).is_file() for f in d["spectrum_files"])
    doc = summary_of(res)
    assert doc["status"] == "stopped" and doc["stop"]["at_step"] == 2 and doc["stop"]["kind"] == "waveplate_not_covering"
    assert [s["status"] for s in doc["steps"]] == ["done", "stopped"]
    assert_safe(bench)


def test_shg_without_a_signature_the_emission_is_never_switched_on(bench, scope):
    # asking is not allowed -> refused before anything is touched
    res, opr = run(SHGExcitationScan, shg_params(ask_attest_laser=False, waveplate_slot="none"), bench=bench)
    assert not res.success and res.data["refused"] and res.data["touched_hardware"] is False
    assert "attestation" in res.error
    assert opr.seen == [] and bench.tun.emitting is False and bench.mic.acquisitions == 0 and bench.mic.calls == []
    # the operator says no -> clean stop, still never on
    res, opr = run(SHGExcitationScan, shg_params(waveplate_slot="none"), bench=bench,
                   laser_attest=lambda p: {"value": False})
    assert not res.success and res.data["stop_kind"] == "operator_declined" and res.data["steps_done"] == 0
    assert bench.mic.acquisitions == 0
    assert optical_state.get_store().is_valid("laser_area_safe", expect=True) is False
    assert_safe(bench)
    assert res.data["stopped_at_step"] is None and res.data["stop_phase"] == "prepare"


def test_the_device_config_gate_is_not_bypassed_by_an_attestation(make_bench, scope):
    from mast.instruments.laser import EmissionControlDisabled

    b = make_bench(npi_emission=False)
    wire_npi(b)
    # allow_emission_control=false: refused up front, with the way out in the message
    res = PowerSeries().execute(Ctx(), power_params())
    assert not res.success and res.data["touched_hardware"] is False
    assert "allow_emission_control" in res.error and "manage_emission=false" in res.error
    assert b.npi.emitting is False
    # …and a valid attestation does not open the second gate: the driver still refuses
    optical_state.get_store().set_by_operator("laser_area_safe", True, by="test-operator", source="test")
    with pytest.raises(EmissionControlDisabled):
        b.npi.emission_on()
    res = PowerSeries().execute(Ctx(), power_params())
    assert not res.success and "allow_emission_control" in res.error and b.npi.emitting is False


def test_shg_operator_never_answers_times_out_and_stops_cleanly(bench, scope):
    res, opr = run(SHGExcitationScan, shg_params(waveplate_slot="none", prompt_timeout_s=0.2), bench=bench,
                   laser_attest=lambda p: None)
    assert not res.success and res.data["stop_kind"] == "operator_timeout"
    assert res.data["status"] == "stopped" and bench.mic.acquisitions == 0
    assert prompts.list_open() == [], "a timed-out prompt is closed, not left open"
    assert_safe(bench)


def test_shg_dismissed_prompt_stops_cleanly(bench, scope):
    # an answer the prompt rejects makes FakeOperator dismiss it (what a UI 'cancel' does)
    res, opr = run(SHGExcitationScan, shg_params(), bench=bench, waveplate=lambda p: {"choice": "no-such-plate"})
    assert not res.success and res.data["stop_kind"] == "operator_cancelled"
    assert_safe(bench)


def test_shg_abort_mid_run_keeps_the_data_and_says_where_it_stopped(bench, scope):
    ctx = Ctx(abort=lambda: bench.mic.acquisitions >= 2)
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="700,800,900,1000,1100", power_meter_id="pm"),
                   bench=bench, ctx=ctx)
    assert not res.success
    d = res.data
    assert d["status"] == "aborted" and d["stop_kind"] == "aborted"
    assert d["steps_done"] == 2 and d["stopped_at_step"] == 3 and d["stop_phase"] == "step"
    assert len(d["spectrum_files"]) == 4 and all(Path(f).is_file() for f in d["spectrum_files"])
    doc = summary_of(res)
    assert doc["status"] == "aborted" and doc["stop"]["at_step"] == 3 and doc["steps_done"] == 2
    assert [s["status"] for s in doc["steps"]] == ["done", "done", "pending", "pending", "pending"]
    assert len(list(csv.DictReader(Path(d["summary_csv_path"]).open(encoding="utf-8")))) == 2
    assert d["cleanup"]["spectrometer"] == "stop sent" and d["cleanup"]["microscope"] == "stop_all sent"
    assert ("stop_all", None) in bench.mic.calls
    assert_safe(bench)


def test_an_abort_that_lands_right_after_an_exposure_does_not_throw_the_spectrum_away(bench, scope):
    """The abort flips the moment the exposure is in hand: the power read, the analysis and the files of
    that step must still happen (only the *next* step is not started)."""
    ctx = Ctx(abort=lambda: bench.mic.acquisitions >= 1)
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="700,800", power_meter_id="pm", waveplate_slot="none"),
                 bench=bench, ctx=ctx)
    d = res.data
    assert d["status"] == "aborted" and d["steps_done"] == 1 and d["stopped_at_step"] == 2
    side = sidecar_of(d["spectrum_files"][0])
    assert side["conditions"]["power_after_mw"] is not None and side["result"]["analysis"]["found"] is True
    assert_safe(bench)


def test_switching_the_active_sample_mid_run_stops_it_and_files_nothing_under_the_new_sample(bench, scope):
    orig = bench.mic.acquire

    def acquire_then_switch(**kw):
        out = orig(**kw)
        if bench.mic.acquisitions == 2:                     # somebody picks another sample while step 2 is exposing
            scope.storage.smp["s2"] = dict(scope.storage.smp["s1"], name="Other flake", sample_index=2)
            scope.log.current_sample_id = "s2"
        return out

    bench.mic.acquire = acquire_then_switch
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="700,800,900,1000", waveplate_slot="none"), bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "scope_changed" and d["status"] == "stopped"
    assert d["steps_done"] == 2 and d["stopped_at_step"] == 3
    exp_dir = scope.root / "experiments"
    files = sorted(p.name for p in exp_dir.rglob("*") if p.is_file() and "samples" in p.parts)
    assert any("S01__WS2_flake" in str(p) for p in exp_dir.rglob("*.npy")), files
    assert not any("S02" in str(p) for p in exp_dir.rglob("*") if p.is_file()), "nothing was filed under the new sample"
    # step 2 was exposed after the switch happened: it is kept, in staging (and the summary says so)
    assert len(d["spectrum_files"]) == 4
    staged = [Path(f) for f in d["spectrum_files"] if "artifacts" in Path(f).parts]
    assert len(staged) == 2 and all(f.is_file() for f in staged)
    assert d["stored_in_experiment"] is False and any("changed" in w for w in d["warnings"])
    assert_safe(bench)


def test_shg_abort_while_waiting_for_the_operator(bench, scope):
    flag = threading.Event()
    threading.Timer(0.3, flag.set).start()
    res, opr = run(SHGExcitationScan, shg_params(waveplate_slot="none"), bench=bench, ctx=Ctx(abort=flag.is_set),
                   laser_attest=lambda p: None)
    assert not res.success and res.data["stop_kind"] == "aborted"
    assert prompts.list_open() == []
    assert_safe(bench)


def test_shg_limits_are_refused_before_anything_is_touched(make_bench, scope):
    b = make_bench(superk_limits={"min_nm": 430, "max_nm": 1400, "max_amplitude_pct": 20})
    b.mic.shg_source = lambda: b.tun.output()
    cases = {
        "wavelength": shg_params(wavelengths_nm="800,1500"),
        "amplitude": shg_params(amplitude_pct=50),
        "centre": shg_params(center_offset_nm=1000, wavelengths_nm="800"),
        "exposure": shg_params(integration_s=3000),
        "total time": shg_params(integration_s=500, accumulations=10),
        "analyzer": shg_params(analyzer_deg=400),
        "bad list": shg_params(wavelengths_nm="800,abc"),
        "both lists": shg_params(start_nm=800, stop_nm=900, step_nm=10),
        "no amplitude": {"laser_id": "superk", "wavelengths_nm": "800"},
        "no plate for 450": shg_params(wavelengths_nm="450"),
        "unknown plate slot": shg_params(waveplate_slot="no_such_slot"),
    }
    for what, params in cases.items():
        res, opr = run(SHGExcitationScan, params, bench=b)
        assert not res.success, what
        assert res.data["refused"] and res.data["touched_hardware"] is False, what
        assert opr.seen == [], what
    assert b.mic.calls == [] and b.mic.acquisitions == 0 and b.tun.emitting is False
    assert b.tun.channels()[0]["amplitude_pct"] == 0.0, "an out-of-range request is not clamped into a smaller one"
    assert not (scope.root / "root" / "artifacts" / "optics_protocols").exists()


def test_shg_refuses_a_source_whose_writes_are_not_enabled_yet(make_bench, scope):
    b = make_bench(superk_write=False)                  # a real driver before the bench self-test
    res = SHGExcitationScan().execute(Ctx(), shg_params())
    assert not res.success and "write_enabled" in res.error and res.data["touched_hardware"] is False
    assert b.tun.emitting is False and b.mic.calls == []


# ── allow_emission_control=false: software does not add light (docs 6.4) ─────────────────────────────────
#
# While a source emits, every increase - from 0, or on a channel that is already lit - passes the full emission gate
# in the drivers. _prepare sets the scan's channel to amplitude_pct, so with the gate closed a plan that must RAISE
# the channel of a source that is emitting (or whose state cannot be read) is refused up front (nothing touched,
# nothing asked, nothing written) instead of halting halfway through the preparation. The same amplitude, a lower
# one (the protocol dims), a colour change and a source that is still off are fine: the amplitude is set first, the
# operator switches the source on afterwards.


def test_shg_a_dark_channel_is_refused_up_front(make_bench, scope):
    b = make_bench(superk_emission=False)
    switched_on_by_hand(b.tun)                                    # emitting, every channel still at 0
    res, opr = run(SHGExcitationScan, shg_params(manage_emission=False, waveplate_slot="none"), bench=b)
    assert not res.success and res.data["refused"] is True and res.data["touched_hardware"] is False
    assert len(res.data["problems"]) == 1
    msg = res.error
    assert "allow_emission_control is false and the source is emitting (or its emission state cannot be read), so software adds no light" in msg
    assert "raises channel 0 from 0% to 5%" in msg
    assert "set channel 0 to at least 5% by hand" in msg and "only dims it to 5% and tunes the colour" in msg      # way out 1
    assert "enable allow_emission_control in the device config after the bench self-test" in msg                      # way out 2
    assert opr.seen == [] and b.mic.calls == [] and b.mic.acquisitions == 0 and b.pm.reads == 0
    assert b.tun.channels()[0]["amplitude_pct"] == 0.0 and b.tun.emitting is True, "the operator's state is left alone"
    assert not (scope.root / "root" / "artifacts" / "optics_protocols").exists()
    assert not (scope.root / "experiments").exists()
    assert optical_state.get_store().history == []


@pytest.mark.parametrize("preset", [{}, {0: 3.0}])
def test_shg_a_source_that_is_still_off_gets_its_amplitude_set_first_and_the_operator_switches_it_on(preset, make_bench, scope):
    """A raise on a source that is off adds no light (the drivers do not gate it either): the plan lets it through,
    the protocol sets the amplitude and then asks the operator to switch the source on."""
    b = make_bench(superk_emission=False)
    preset_by_hand(b.tun, preset)                                 # the channel is at 0 or at 3 %: either way 5 % raises it
    at_confirm = []

    def switch_on(p):
        at_confirm.append(b.tun.channels()[0]["amplitude_pct"])  # set BEFORE the operator is asked
        b.tun.emitting = True
        return {"choice": "yes"}

    res, opr = run(SHGExcitationScan, shg_params(manage_emission=False, waveplate_slot="none"), bench=b,
                   manual_emission=switch_on)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "manual_emission"] and at_confirm == [5.0]
    assert b.tun.channels()[0]["amplitude_pct"] == 5.0
    msg = opr.seen[1].message
    assert "once the source emits, software adds no light" in msg and "set channel 0 to 5% while the source was still off" in msg
    assert sidecar_of(res.data["spectrum_files"][0])["conditions"]["source_mode"] == "manual"
    assert_safe(b)


def test_shg_a_lit_channel_below_the_plan_is_refused_too(make_bench, scope):
    b = make_bench(superk_emission=False)
    switched_on_by_hand(b.tun, {0: 3.0})
    res = unattended(SHGExcitationScan, shg_params(manage_emission=False, waveplate_slot="none"))
    assert not res.success and res.data["touched_hardware"] is False
    assert "raises channel 0 from 3% to 5%" in res.error and "set channel 0 to at least 5% by hand" in res.error
    assert b.tun.channels()[0]["amplitude_pct"] == 3.0 and b.mic.calls == []


def test_shg_the_refusal_names_both_ways_out_in_chinese_too(make_bench, scope):
    b = make_bench(superk_emission=False)
    switched_on_by_hand(b.tun)
    with use_lang("zh"):
        res = unattended(SHGExcitationScan, shg_params(manage_emission=False, waveplate_slot="none"))
    assert not res.success and res.data["touched_hardware"] is False
    assert "光源正在出光（或读不到出光状态）" in res.error and "软件不加光" in res.error and "从 0% 抬到 5%" in res.error
    assert "① 开始前在厂商软件里手动把通道 0 设到至少 5%" in res.error and "调颜色" in res.error
    assert "② 台上自检通过后，在设备配置里打开 allow_emission_control" in res.error


@pytest.mark.parametrize("lit", [7.0, 5.0])
def test_shg_a_channel_at_or_above_the_plan_runs_to_completion(lit, make_bench, scope):
    b = make_bench(superk_emission=False)
    switched_on_by_hand(b.tun, {0: lit, 3: 7.0})                  # the scan's channel and another one carry light
    res, opr = run(SHGExcitationScan, shg_params(manage_emission=False, waveplate_slot="none"), bench=b)
    assert res.success, res.error
    assert res.data["steps_done"] == 3 and opr.reasons() == ["laser_attest"], "already emitting: nothing to switch on"
    chans = {c["channel"]: c["amplitude_pct"] for c in b.tun.channels()}
    assert chans[0] == 5.0, "dimmed (7 -> 5) or left alone (5 -> 5): never raised"
    assert chans[3] == 0.0, "isolate_channel dims the other lit channel: dimming is never gated"
    assert sidecar_of(res.data["spectrum_files"][0])["conditions"]["source_mode"] == "already_on"
    assert_safe(b)


def test_shg_with_emission_control_allowed_a_raise_is_not_refused(make_bench, scope):
    b = make_bench()                                              # allow_emission_control=true (what the gate asks for)
    switched_on_by_hand(b.tun, {0: 3.0})
    res, opr = run(SHGExcitationScan, shg_params(manage_emission=False, waveplate_slot="none"), bench=b)
    assert res.success, res.error
    assert b.tun.channels()[0]["amplitude_pct"] == 5.0            # 3 -> 5
    assert_safe(b)


def test_shg_an_amplitude_of_zero_is_not_a_raise(make_bench, scope):
    b = make_bench(superk_emission=False)
    switched_on_by_hand(b.tun)
    res, _ = run(SHGExcitationScan, shg_params(manage_emission=False, waveplate_slot="none", amplitude_pct=0,
                                               wavelengths_nm="800"), bench=b)
    assert res.success, res.error                                 # it asks for no light: nothing to refuse
    assert_safe(b)


def test_shg_an_unreadable_channel_amplitude_counts_as_possibly_lower(make_bench, scope, monkeypatch):
    b = make_bench(superk_emission=False)
    switched_on_by_hand(b.tun, {0: 7.0})
    monkeypatch.setattr(b.tun, "channels", lambda: [{"channel": i, "wavelength_nm": 600.0, "amplitude_pct": None,
                                                     "enabled": False} for i in range(8)])
    res = unattended(SHGExcitationScan, shg_params(manage_emission=False, waveplate_slot="none"))
    assert not res.success and res.data["touched_hardware"] is False
    assert "from an amplitude that cannot be read to 5%" in res.error


@pytest.mark.parametrize("how", ["none", "raises"])
def test_shg_an_unreadable_emission_state_counts_as_emitting(how, make_bench, scope, monkeypatch):
    b = make_bench(superk_emission=False)

    def unreadable():
        if how == "raises":
            raise OSError("serial timeout")
        return None

    monkeypatch.setattr(b.tun, "emission_state", unreadable)      # the drivers treat this as "may be emitting" too
    res = unattended(SHGExcitationScan, shg_params(manage_emission=False, waveplate_slot="none"))
    assert not res.success and res.data["touched_hardware"] is False
    assert "emission state cannot be read" in res.error and "raises channel 0 from 0% to 5%" in res.error
    assert b.mic.calls == [] and b.tun.channels()[0]["amplitude_pct"] == 0.0


def test_shg_the_scan_channel_is_the_one_that_counts(make_bench, scope):
    """Channel 0 carries 7 %, channel 2 nothing: a scan on channel 2 raises it, a scan on channel 0 only dims."""
    b = make_bench(superk_emission=False)
    switched_on_by_hand(b.tun, {0: 7.0})
    res = unattended(SHGExcitationScan, shg_params(manage_emission=False, waveplate_slot="none", channel=2))
    assert not res.success and res.data["touched_hardware"] is False
    assert "raises channel 2 from 0% to 5%" in res.error and "set channel 2 to at least 5% by hand" in res.error
    res, _ = run(SHGExcitationScan, shg_params(manage_emission=False, waveplate_slot="none", channel=0,
                                               wavelengths_nm="800"), bench=b)
    assert res.success, res.error
    assert_safe(b)


def test_shg_a_source_that_is_off_runs_when_the_channel_was_set_by_hand_and_the_prompt_says_what_is_needed(make_bench, scope):
    b = make_bench(superk_emission=False)
    preset_by_hand(b.tun, {0: 7.0})                               # set in the vendor software; the source is still off

    def switch_on(p):
        b.tun.emitting = True                                     # the operator switches it on in the vendor software
        return {"choice": "yes"}

    res, opr = run(SHGExcitationScan, shg_params(manage_emission=False, waveplate_slot="none"), bench=b,
                   manual_emission=switch_on)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "manual_emission"]
    msg = opr.seen[1].message
    assert "allow_emission_control=false: once the source emits, software adds no light" in msg
    assert "set channel 0 to 5% while the source was still off" in msg and "set it by hand" in msg
    assert b.tun.channels()[0]["amplitude_pct"] == 5.0            # dimmed from 7
    assert sidecar_of(res.data["spectrum_files"][0])["conditions"]["source_mode"] == "manual"
    assert_safe(b)


@pytest.mark.parametrize("allowed", [False, True])
def test_shg_the_manual_prompt_names_what_the_gate_needs_only_when_the_gate_is_closed(make_bench, scope, allowed):
    b = make_bench(superk_emission=allowed)
    preset_by_hand(b.tun, {0: 7.0})

    def switch_on(p):
        b.tun.emitting = True
        return {"choice": "yes"}

    res, opr = run(SHGExcitationScan, shg_params(manage_emission=False, waveplate_slot="none", wavelengths_nm="800"),
                   bench=b, manual_emission=switch_on)
    assert res.success, res.error
    msg = opr.seen[opr.reasons().index("manual_emission")].message
    assert "Switch it on in the vendor software" in msg
    if allowed:
        assert "allow_emission_control" not in msg
    else:
        assert "allow_emission_control=false" in msg and "once the source emits, software adds no light" in msg


def test_shg_a_raise_the_plan_did_not_see_is_still_stopped_by_the_driver(make_bench, scope, monkeypatch):
    """The plan check is an early warning, not the gate: if the channel is lower than the plan read (it changed in
    between), the driver's own gate still refuses the raise."""
    b = make_bench(superk_emission=False)
    switched_on_by_hand(b.tun)                                    # emitting, channel 0 really at 0
    real = b.tun.channels
    monkeypatch.setattr(b.tun, "channels", lambda: [dict(c, amplitude_pct=7.0) for c in real()])    # …but the plan reads 7 %
    res, opr = run(SHGExcitationScan, shg_params(manage_emission=False, waveplate_slot="none"), bench=b)
    assert not res.success and res.data["stop_kind"] == "emission_refused" and res.data["stop_phase"] == "prepare"
    assert res.data["steps_done"] == 0 and b.mic.acquisitions == 0
    assert real()[0]["amplitude_pct"] == 0.0
    assert_safe(b)


def test_shg_dry_run_cannot_read_the_source_so_it_only_leaves_a_note(make_bench, scope):
    b = make_bench(superk_emission=False)
    switched_on_by_hand(b.tun)
    reads = []
    b.tun.channels = lambda: reads.append("channels") or []
    b.tun.emission_state = lambda: reads.append("emission_state")
    res = SHGExcitationScan().execute(Ctx(), shg_params(manage_emission=False, dry_run=True))
    assert res.success and res.data["valid"] is True and res.data["touched_hardware"] is False and reads == []
    note = next(n for n in res.data["notes"] if "dry run cannot read the source" in n)
    assert "software does not add light while the source is emitting" in note and "channel 0 must already be at 5% or more" in note
    assert "enable allow_emission_control after the bench self-test" in note and "a source that is still off gets its amplitude set first" in note
    # the gate open (or nothing to raise): nothing to warn about
    for gate_open, extra in ((True, {}), (False, {"amplitude_pct": 0})):
        b2 = make_bench(superk_emission=gate_open)
        res = SHGExcitationScan().execute(Ctx(), shg_params(manage_emission=False, dry_run=True, **extra))
        assert res.success and not any("dry run cannot read the source" in n for n in res.data["notes"])
    assert b2.mic.calls == []


def test_a_protocol_that_raises_nothing_is_not_refused_for_a_dark_channel(make_bench, scope):
    """Only the protocols that set a channel / the power themselves have anything to refuse."""
    b = make_bench(superk_emission=False)
    switched_on_by_hand(b.tun, {0: 5.0})
    b.mic.shg_source = lambda: b.tun.output()
    res, opr = run(TemperatureSeries, {"temperatures_c": "30", "ramp_c_per_min": 20, "hold_s": 0.05, "poll_s": 0.01,
                                       "integration_s": 0.1, "settle_s": 0, "laser_id": "superk",
                                       "manage_emission": False}, bench=b)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest"]
    assert_safe(b)


def test_warmup_after_switching_the_emission_on_is_waited_for_and_can_be_cancelled(bench, scope, monkeypatch):
    waits = []
    real = op._Run.sleep
    monkeypatch.setattr(op._Run, "sleep", lambda self, s, chunk=0.05: (waits.append(round(s, 3)), real(self, 0.0))[1])
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="800", waveplate_slot="none", warmup_s=90), bench=bench)
    assert res.success, res.error
    assert 90.0 in waits and waits.index(90.0) < len(waits), waits
    assert [r["found"] for r in res.data["rows"]] == [True]


def test_shg_two_lasers_without_an_id_is_ambiguous(bench, scope):
    res = SHGExcitationScan().execute(Ctx(), {"amplitude_pct": 5, "wavelengths_nm": "800", "dry_run": True})
    assert not res.success and "pass device_id" in res.error and res.data["touched_hardware"] is False


def test_shg_centre_the_driver_cannot_set_asks_the_operator(bench, scope):
    def unsupported(nm):
        raise OperationNotSupported("grating/centre are not settable over COM on this system")

    bench.mic.set_center_wavelength = unsupported
    centres = []

    def centre(p):                                   # the operator sets it in WITec Control, then reports it
        bench.mic.settings["center_wavelength_nm"] = p.context["requested_nm"]
        centres.append(p.context["requested_nm"])
        return {"value": p.context["requested_nm"]}

    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="800,900"), bench=bench, center=centre)
    assert res.success, res.error
    assert centres == [400.0, 450.0]
    assert [r["found"] for r in res.data["rows"]] == [True, True]
    side = sidecar_of(res.data["spectrum_files"][0])
    assert side["conditions"]["center_method"] == "operator" and side["conditions"]["spectrometer_center_nm"] == 400.0
    assert [c["method"] for c in summary_of(res)["centre_log"]] == ["operator", "operator"]


def test_shg_centre_fallback_state_and_fail(bench, scope):
    def unsupported(nm):
        raise OperationNotSupported("no")

    bench.mic.set_center_wavelength = unsupported
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="800", center_fallback="state",
                                                 center_state="SHG"), bench=bench)
    assert res.success and bench.mic.state == "SHG"
    assert not any(r == "center" for r in opr.reasons())
    assert sidecar_of(res.data["spectrum_files"][0])["conditions"]["center_method"] == "state"
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="800", center_fallback="fail"), bench=bench)
    assert not res.success and res.data["stop_kind"] == "center_not_settable"
    assert_safe(bench)
    named = SHGExcitationScan().execute(Ctx(), shg_params(center_fallback="state"))        # the state needs a name
    assert not named.success and "center_state" in named.error


def test_shg_centre_tolerance_moves_the_centre_only_when_needed(bench, scope):
    seen = []
    orig = bench.mic.set_center_wavelength

    def spy(nm):
        seen.append(nm)
        return orig(nm)

    bench.mic.set_center_wavelength = spy
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="800,802,804,806,808,810", center_tolerance_nm=2.5),
                 bench=bench)
    assert res.success, res.error
    # 2 nm of lambda = 1 nm of centre: 400 is set, 401/402 are within 2.5 nm and left alone, 403 is not
    assert seen == [400.0, 403.0], seen
    assert [r["center_nm"] for r in res.data["rows"]] == [400.0, 400.0, 400.0, 403.0, 403.0, 403.0]


def test_shg_power_from_the_operator_when_there_is_no_meter(bench, scope):
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="800,900", power_prompt=True), bench=bench,
                   power_reading=lambda p: {"value": 2.0})
    assert res.success, res.error
    assert opr.reasons().count("power_reading") == 2
    rows = res.data["rows"]
    assert [r["power_mw"] for r in rows] == [2.0, 2.0]
    assert rows[0]["i_over_p2"] == pytest.approx(rows[0]["intensity"] / 4.0)
    assert sidecar_of(res.data["spectrum_files"][0])["conditions"]["power_source"] == "operator"
    # neither a meter nor a prompt: power is null and the P normalisation is flagged, the data are still kept
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="800"), bench=bench)
    assert res.success and res.data["rows"][0]["power_mw"] is None and res.data["rows"][0]["i_over_p2"] is None
    assert any("no power" in w for w in res.data["warnings"])


def test_shg_normalisation_modes(bench, scope):
    base = shg_params(wavelengths_nm="800,1000", power_meter_id="pm", waveplate_slot="none")
    for mode, key in (("P2", "i_over_p2"), ("P", "i_over_p"), ("none", "intensity")):
        res, _ = run(SHGExcitationScan, {**base, "normalize": mode}, bench=bench)
        assert res.success, res.error
        assert all(r["i_norm"] == pytest.approx(r[key]) for r in res.data["rows"]), mode


def test_shg_write_access_is_asked_for_and_can_be_declined(make_bench, scope):
    b = make_bench(write_access=False)
    b.mic.shg_source = lambda: b.tun.output()

    def grant(p):
        b.mic.grant_write_access(True)
        return {"choice": "yes"}

    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="800", analyzer_deg=10, waveplate_slot="none"),
                   bench=b, write_access=grant)
    assert res.success, res.error
    assert opr.reasons().count("write_access") == 1 and b.mic.analyzer_deg == 10.0
    # declined -> clean stop
    b2 = make_bench(write_access=False)
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="800", analyzer_deg=10, waveplate_slot="none"),
                   bench=b2, write_access=lambda p: {"choice": "no"})
    assert not res.success and res.data["stop_kind"] == "operator_declined"
    # asking switched off -> clean stop, no prompt
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="800", analyzer_deg=10, waveplate_slot="none",
                                                 ask_write_access=False), bench=b2)
    assert not res.success and res.data["stop_kind"] == "write_access" and opr.seen == []
    assert b2.tun.emitting is False


def test_shg_unexpected_device_error_still_takes_the_safe_direction(bench, scope):
    orig = bench.mic.acquire
    n = {"i": 0}

    def flaky(**kw):
        n["i"] += 1
        if n["i"] == 2:
            raise RuntimeError("camera exploded")
        return orig(**kw)

    bench.mic.acquire = flaky
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="700,800,900", waveplate_slot="none"), bench=bench)
    assert not res.success and res.data["status"] == "failed" and res.data["stop_kind"] == "error"
    assert "camera exploded" in res.error and res.data["steps_done"] == 1 and res.data["stopped_at_step"] == 2
    assert_safe(bench)
    assert summary_of(res)["status"] == "failed"


def test_shg_time_budget(bench, scope, monkeypatch):
    # a plan whose own estimate already exceeds the budget is refused up front
    res = SHGExcitationScan().execute(Ctx(), shg_params(wavelengths_nm="700,800,900", waveplate_slot="none",
                                                        max_total_s=10, integration_s=2))
    assert not res.success and "max_total_s" in res.error and res.data["touched_hardware"] is False
    # …and a run that overruns it (slow prompts, slow devices) stops cleanly with what it has
    monkeypatch.setattr(op._Run, "over_budget", lambda self: len(self.rows) >= 2)
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="700,800,900,1000", waveplate_slot="none"), bench=bench)
    assert not res.success and res.data["stop_kind"] == "time_budget" and res.data["status"] == "stopped"
    assert res.data["steps_done"] == 2 and res.data["stopped_at_step"] == 3
    assert_safe(bench)


def test_write_access_goes_back_unless_told_otherwise(bench, scope):
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="800", waveplate_slot="none",
                                               release_write_access=False), bench=bench)
    assert res.success and bench.mic.has_write_access() is True and "write_access" not in res.data["cleanup"]
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="800", waveplate_slot="none"), bench=bench)
    assert res.success and bench.mic.has_write_access() is False


def test_shg_isolates_the_channel_it_uses(bench, scope):
    bench.tun.set_amplitude_pct(30, channel=3)             # left on by somebody
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="800", waveplate_slot="none"), bench=bench)
    assert res.success, res.error
    assert bench.tun.channels()[3]["amplitude_pct"] == 0.0


def test_shg_live_stream_gets_the_mean_spectrum(bench, scope):
    with isolated_live_hub() as hub, LiveRecorder(hub) as rec:
        res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="800,900", waveplate_slot="none"), bench=bench)
    assert res.success, res.error
    frames = rec.frames("local/spectra/witec")
    assert len(frames) == 2 and frames[-1]["meta"]["protocol"] == "SHGExcitationScan"
    assert not rec.overflowed


def _expire_attestation_after_first_exposure(bench):
    orig = bench.mic.acquire

    def acquire_then_expire(**kw):
        res = orig(**kw)
        if bench.mic.acquisitions == 1:                 # eight hours later, as far as the registry can tell
            optical_state.get_store()._values["laser_area_safe"].valid_until = "2000-01-01T00:00:00+00:00"
        return res

    bench.mic.acquire = acquire_then_expire


def test_an_attestation_that_expires_mid_run_is_signed_again_with_the_beam_off(bench, scope):
    _expire_attestation_after_first_exposure(bench)
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="700,800", waveplate_slot="none"), bench=bench)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "laser_attest"]
    assert opr.emission_at_prompt == [("laser_attest", False), ("laser_attest", False)], \
        "the source is off while the operator signs again"
    assert [r["found"] for r in res.data["rows"]] == [True, True], "…and back on for the second measurement"
    assert optical_state.get_store().is_valid("laser_area_safe", expect=True)
    assert_safe(bench)


def test_an_expired_attestation_without_permission_to_ask_stops_with_the_beam_off(bench, scope):
    optical_state.get_store().set_by_operator("laser_area_safe", True, by="test-operator", source="test")
    _expire_attestation_after_first_exposure(bench)
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="700,800,900", waveplate_slot="none",
                                                 ask_attest_laser=False), bench=bench)
    assert not res.success and res.data["stop_kind"] == "laser_not_attested"
    assert res.data["steps_done"] == 1 and res.data["stopped_at_step"] == 2 and opr.seen == []
    assert_safe(bench)


def test_shg_on_a_plain_spectrometer_without_a_microscope(tmp_path, scope):
    cfg = {"devices": [{"id": "ccd", "type": "sim_spectrometer", "time_scale": 0.0},
                       {"id": "superk", "type": "sim_tunable_laser"}]}
    (tmp_path / "optical_instruments.json").write_text(json.dumps(cfg), encoding="utf-8")
    reset_instrument_registry(InstrumentRegistry(tmp_path / "optical_instruments.json"))
    try:
        res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="700,800", waveplate_slot="none"))
        assert res.success, res.error
        assert "microscope" not in res.data["devices"] and res.data["devices"]["spectrometer"]["id"] == "ccd"
        assert "shutter" not in res.data["cleanup"] and res.data["cleanup"]["emission"]["state_after"] is False
        assert [r["found"] for r in res.data["rows"]] == [False, False], "the sim spectrometer has no SHG source"
        assert any("no SHG peak" in w for w in res.data["warnings"])
        # protocols that need the analyzer refuse politely
        pol = PolarizationMatrix().execute(Ctx(), {"incident_angles_deg": "0", "dry_run": True})
        assert not pol.success and "needs a microscope" in pol.error
    finally:
        reset_instrument_registry(None)


def test_a_bug_while_planning_is_a_refusal_not_a_raw_exception(bench, scope, monkeypatch):
    def broken(self, cfg, rig):
        raise RuntimeError("plan exploded")

    monkeypatch.setattr(SHGExcitationScan, "_plan", broken)
    res = SHGExcitationScan().execute(Ctx(), shg_params())
    assert not res.success and "plan exploded" in res.error and res.data["touched_hardware"] is False
    assert bench.mic.calls == [] and bench.tun.emitting is False


# ── PolarizationMatrix ───────────────────────────────────────────────────────────────────────────────


def pol_params(**kw):
    return {"incident_angles_deg": "0,10", "integration_s": 0.1, "settle_s": 0, "witec_laser": "532",
            "witec_power_mw": 2, **kw}


def test_polmat_dry_run_gives_the_lab_analyzer_sequence(bench, scope):
    res = PolarizationMatrix().execute(Ctx(), {"incident_angles_deg": "0,10,20", "dry_run": True,
                                               "witec_laser": "532", "integration_s": 0.1})
    assert res.success, res.error
    d = res.data
    assert d["n_steps"] == 6 and d["manual_prompts_expected"] == 4 and d["before_start"] == ["laser_attest"]
    assert [s["conditions"]["analyzer_deg"] for s in d["steps"]] == [0.0, 135.0, 5.0, 140.0, 10.0, 145.0]
    assert [s["conditions"]["mode"] for s in d["steps"]] == ["par", "crs"] * 3
    assert [bool(s["manual"]) for s in d["steps"]] == [True, False] * 3          # one prompt per theta
    assert bench.mic.calls == [] and prompts.get_backend()._prompts == {}


def test_polmat_default_grid_is_the_lab_grid(bench, scope):
    res = PolarizationMatrix().execute(Ctx(), {"dry_run": True, "witec_laser": "532"})
    assert res.success and res.data["n_steps"] == 74                           # 37 angles x par/crs
    assert res.data["steps"][-1]["conditions"]["incident_deg"] == 360.0
    one = PolarizationMatrix().execute(Ctx(), {"dry_run": True, "mode": "par", "start_deg": 0, "stop_deg": 90,
                                               "step_deg": 30})
    assert one.success and one.data["n_steps"] == 4


def test_polmat_run_asks_for_each_angle_and_follows_the_signed_angle(bench, scope):
    signed = {0.0: 0.0, 10.0: 10.4}                                           # the dial was set 0.4 deg off

    res, opr = run(PolarizationMatrix, pol_params(), bench=bench,
                   incident_polarizer=lambda p: {"value": signed[p.context["requested_deg"]]})
    assert res.success, res.error
    d = res.data
    assert d["steps_done"] == 4
    assert opr.reasons() == ["laser_attest", "incident_polarizer", "incident_polarizer"], "one signature, one prompt per theta"
    assert [r["mode"] for r in d["rows"]] == ["par", "crs", "par", "crs"]
    assert [r["incident_deg_actual"] for r in d["rows"]] == [0.0, 0.0, 10.4, 10.4]
    assert [r["analyzer_deg"] for r in d["rows"]] == [0.0, 135.0, pytest.approx(5.2), pytest.approx(140.2)]
    analyzer_calls = [v for k, v in bench.mic.calls if k == "analyzer"]
    assert analyzer_calls == [0.0, 135.0, pytest.approx(5.2), pytest.approx(140.2)]
    side = sidecar_of(d["rows"][2]["file"])                    # third step: theta = 10 deg, par
    c = side["conditions"]
    assert c["mode"] == "par" and c["incident_polarizer_deg"] == 10.4 and c["incident_polarizer_requested_deg"] == 10.0
    assert c["analyzer_deg"] == pytest.approx(5.2) and c["excitation_nm"] == 532.0 and c["witec_laser"] == "532"
    assert side["optical_state"]["incident_polarizer_deg"]["value"] == 10.4
    assert side["optical_state"]["incident_polarizer_deg"]["source"].startswith("prompt:")
    assert re.fullmatch(r"polmat_par_i10\.4deg_a5\.2deg", d["rows"][2]["stem"])
    assert all(r["found"] for r in d["rows"]), "the shutter was open during the exposures (Si line seen)"
    assert bench.mic.shutter_open is False
    # par/crs contrast follows cos^2 of the analyzer (sim model): par at 0 deg is the bright one
    assert d["rows"][0]["intensity"] > d["rows"][1]["intensity"]
    assert set(d["analysis"]["modes"]) == {"par", "crs"}
    assert Path(d["plot_path"]).read_bytes()[:4] == b"\x89PNG"


@pytest.fixture
def slow_registry_writes(monkeypatch):
    """The operator's answer wakes the protocol before the optical-state write has landed (a persisted
    store, a loaded machine): the registry lags the answer by a moment."""
    store = optical_state.get_store()
    real = store.set_by_operator

    def slow(*a, **kw):
        time.sleep(0.25)
        return real(*a, **kw)

    monkeypatch.setattr(store, "set_by_operator", slow)


def test_the_protocols_do_not_judge_on_a_registry_that_has_not_caught_up_with_the_answer(bench, scope,
                                                                                         slow_registry_writes):
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="700,800"), bench=bench)       # plate + laser
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "waveplate"]
    res, _ = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="20"), bench=bench)
    assert res.success, res.error                                                                 # incident angle
    wire_npi(bench)
    bench.npi.SUPPORTS = frozenset({"emission"})

    def set_dial(p):
        bench.npi.power_pct = p.context["requested"]
        return {"value": p.context["requested"]}

    res, _ = run(PowerSeries, power_params(powers="10,20,40"), bench=bench, power=set_dial)       # manual power
    assert res.success, res.error
    assert [r["power_set"] for r in res.data["rows"]] == [10, 20, 40]


# ── a WITec shutter that cannot be confirmed closed ──────────────────────────────────


def _flaky_close(bench, monkeypatch, *, times=1, answer_open=False):
    """The WITec shutter fails to close ``times`` times after it was open: with an exception, or (answer_open) by
    answering "still open"."""
    real = bench.mic._set_shutter_raw
    left = {"n": times}

    def flaky(open_):
        if not open_ and left["n"] and bench.mic.shutter_open:
            left["n"] -= 1
            if answer_open:
                return True
            raise InstrumentError("WITec Control did not answer within 5 s (simulated)")
        return real(open_)

    monkeypatch.setattr(bench.mic, "_set_shutter_raw", flaky)


def test_a_failed_shutter_close_after_an_exposure_ends_the_run_and_nothing_more_is_asked(bench, scope, monkeypatch):
    _flaky_close(bench, monkeypatch)
    open_at_prompt = []

    def turn(p):
        open_at_prompt.append(bench.mic.shutter_open)
        return {"value": p.context["requested_deg"]}

    res, opr = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10,20"), bench=bench,
                   incident_polarizer=turn)
    d = res.data
    assert not res.success and d["stop_kind"] == "shutter_not_closed" and d["status"] == "failed"
    assert open_at_prompt == [False], "the second hand-rotation prompt is never posted"
    assert opr.reasons().count("incident_polarizer") == 1
    assert d["steps_done"] == 1 and len(d["spectrum_files"]) == 2, "the spectrum just measured is kept"
    assert d["stopped_at_step"] == 2
    assert bench.mic.calls.count(("stop_all", None)) == 2, "one at the fault itself, one in the cleanup"
    assert "could not be confirmed closed" in d["stop_reason"] and "stop_all was sent" in d["stop_reason"]
    assert any("could not be confirmed closed" in w for w in d["warnings"])
    assert d["cleanup"]["shutter"] == "closed" and bench.mic.shutter_open is False       # the cleanup shut it
    assert d["cleanup"]["write_access"] == "released", "the close is confirmed now: the write grant goes back"
    assert summary_of(res)["stop"]["kind"] == "shutter_not_closed"
    assert prompts.list_open() == []


def test_a_shutter_that_answers_still_open_is_not_closed(bench, scope, monkeypatch):
    _flaky_close(bench, monkeypatch, answer_open=True)
    res, opr = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10"), bench=bench)
    d = res.data
    assert d["stop_kind"] == "shutter_not_closed" and d["steps_done"] == 1
    assert "the driver answers that the shutter is still open" in d["stop_reason"]
    assert "closed again after stop_all: read back closed" in d["stop_reason"]
    assert opr.reasons().count("incident_polarizer") == 1 and bench.mic.shutter_open is False


def test_a_fault_on_the_last_step_is_reported_after_the_last_step(bench, scope, monkeypatch):
    _flaky_close(bench, monkeypatch)
    res, _ = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0"), bench=bench)
    d = res.data
    assert d["stop_kind"] == "shutter_not_closed" and d["steps_done"] == 1 and d["steps_planned"] == 1
    assert d["stopped_at_step"] is None and d["stop_phase"] == "step"
    assert "stopped after the last step" in res.summary
    assert summary_of(res)["stop"]["at_step"] is None


def test_after_a_shutter_fault_nothing_is_asked_and_nothing_new_starts(bench, scope):
    proto = PolarizationMatrix()
    params = pol_params(mode="par", incident_angles_deg="0", prompt_timeout_s=0.05)    # (a guard that fails must not wait)
    cfg = proto._parse(params)
    rig = op._resolve_rig(cfg, **proto._rig_needs(cfg))
    r = op._Run(proto, Ctx(), params, cfg, rig, proto._plan(cfg, rig))
    r.shutter_fault = "the WITec shutter could not be confirmed closed"
    with pytest.raises(op.ProtocolHalt) as ei:
        r.ask(prompts.OperatorPrompt(kind="confirm", title="x", message="y"))
    assert ei.value.kind == "shutter_not_closed"
    assert prompts.get_backend()._prompts == {}, "no prompt reaches the operator"
    for what in (r.check, lambda: r.sleep(0.01), lambda: r.hw("anything", lambda: 1)):
        with pytest.raises(op.ProtocolHalt) as ei:
            what()
        assert ei.value.kind == "shutter_not_closed"
    assert r.hw("a read that finishes a measured step", lambda: 7, check=False) == 7


def test_a_shutter_that_answers_open_in_the_cleanup_too_is_not_taken_for_closed(bench, scope, monkeypatch):
    _flaky_close(bench, monkeypatch, times=99, answer_open=True)
    released = []
    monkeypatch.setattr(bench.mic, "release_write_access", lambda: released.append(1))
    res, _ = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10"), bench=bench)
    d = res.data
    assert d["stop_kind"] == "shutter_not_closed"
    assert d["cleanup"]["shutter"].startswith("close failed") and "still reads open" in d["cleanup"]["shutter"]
    assert d["cleanup"]["write_access"].startswith("kept") and released == []


def test_a_shutter_that_stays_unclosable_is_reported_and_keeps_the_write_access(bench, scope, monkeypatch):
    _flaky_close(bench, monkeypatch, times=99)
    released = []
    monkeypatch.setattr(bench.mic, "release_write_access", lambda: released.append(1))
    res, opr = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10"), bench=bench)
    d = res.data
    assert d["stop_kind"] == "shutter_not_closed" and d["steps_done"] == 1
    assert "closing again after stop_all failed too" in d["stop_reason"]
    assert d["cleanup"]["shutter"].startswith("close failed")
    assert d["cleanup"]["write_access"].startswith("kept") and released == [], "StopWITec / CloseWITecShutter still need it"
    assert any("could not be confirmed closed" in w for w in d["warnings"])
    assert opr.reasons().count("incident_polarizer") == 1
    assert bench.mic.shutter_open is True, "the double stays as the human found it"


def test_a_shutter_fault_outranks_the_reason_the_run_was_already_stopping(bench, scope, monkeypatch):
    """The operator aborts the moment the shutter is open and the close that follows fails: the run ends as
    shutter_not_closed and the abort stays in the text."""
    _flaky_close(bench, monkeypatch)
    opened = {"x": False}
    real_set = bench.mic.set_shutter

    def spy(open_):
        out = real_set(open_)
        opened["x"] = opened["x"] or bool(open_)
        return out

    monkeypatch.setattr(bench.mic, "set_shutter", spy)
    res, _ = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10"), bench=bench,
                 ctx=Ctx(abort=lambda: opened["x"]))
    d = res.data
    assert d["stop_kind"] == "shutter_not_closed" and "[aborted" in d["stop_reason"]
    assert d["steps_done"] == 0 and bench.mic.shutter_open is False


def test_selecting_the_witec_laser_closes_the_shutter_of_the_laser_just_selected(bench, scope):
    """WITec's shutter belongs to the selected laser (Laser|Selected|Shutter): the close before select_laser was for
    the previous one. Modelled with a per-laser shutter; the 633's was left open by a person."""
    mic = bench.mic
    per_laser = {0: False, 1: True}
    real_select = mic._select_laser_raw

    def select(index):
        real_select(index)
        mic.shutter_open = per_laser.get(int(index), False)

    def set_shutter(open_):
        per_laser[mic.selected_laser] = bool(open_)
        mic.shutter_open = bool(open_)
        return bool(open_)

    mic._select_laser_raw = select
    mic._set_shutter_raw = set_shutter
    open_at_prompt = []

    def turn(p):
        open_at_prompt.append(per_laser[mic.selected_laser])
        return {"value": p.context["requested_deg"]}

    res, _ = run(PolarizationMatrix, {"incident_angles_deg": "0", "mode": "par", "integration_s": 0.05, "settle_s": 0,
                                      "witec_laser": "633", "witec_power_mw": 2}, bench=bench, incident_polarizer=turn)
    assert res.success, res.error
    assert open_at_prompt == [False]
    assert per_laser == {0: False, 1: False}


def test_selecting_the_witec_laser_closes_its_shutter_before_its_power_is_touched(bench, scope):
    """PowerSeries has no prompt (so no swap close) to hide behind: a power set behind an open shutter is light on
    the sample, so the shutter of the laser just selected must already be shut."""
    mic = bench.mic
    per_laser = {0: False, 1: True}                               # the 633's shutter was left open by a person
    real_select, real_power = mic._select_laser_raw, mic._set_laser_power_raw
    at_power = []

    def select(index):
        real_select(index)
        mic.shutter_open = per_laser.get(int(index), False)

    def set_shutter(open_):
        per_laser[mic.selected_laser] = bool(open_)
        mic.shutter_open = bool(open_)
        return bool(open_)

    def power(mw):
        at_power.append(per_laser[mic.selected_laser])
        return real_power(mw)

    mic._select_laser_raw, mic._set_shutter_raw, mic._set_laser_power_raw = select, set_shutter, power
    res, _ = run(PowerSeries, {"witec_laser": "633", "powers": "1,2", "integration_s": 0.05, "settle_s": 0,
                               "center_nm": 547, "feature_nm": 547.2}, bench=bench)
    assert res.success, res.error
    assert len(at_power) == 3 and not any(at_power), "the first power in _prepare and one per step: all behind a shut shutter"


def test_a_swap_is_never_started_with_a_shutter_that_cannot_be_closed(bench, scope):
    """The first close (in _prepare) works, the one right before the plate swap does not: no swap prompt is posted."""
    real = bench.mic.set_shutter
    closes = {"n": 0}

    def flaky(open_):
        if not open_:
            closes["n"] += 1
            if closes["n"] == 2:
                raise InstrumentError("WITec Control is busy (simulated)")
        return real(open_)

    bench.mic.set_shutter = flaky
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="540,1040"), bench=bench)
    assert not res.success and res.data["stop_kind"] == "device_error" and res.data["stop_phase"] == "prepare"
    assert "waveplate" not in opr.reasons()
    assert bench.mic.acquisitions == 0
    assert_safe(bench)


def test_polmat_confirm_entry_is_a_one_click_attestation(bench, scope):
    res, opr = run(PolarizationMatrix, pol_params(angle_entry="confirm", mode="par"), bench=bench)
    assert res.success, res.error
    angle_prompts = [p for p in opr.seen if p.context["reason"] == "incident_polarizer"]
    assert len(angle_prompts) == 2 and all(p.kind == "attest" and len(p.options) == 1 for p in angle_prompts)
    assert [r["incident_deg_actual"] for r in res.data["rows"]] == [0.0, 10.0]


def test_polmat_asks_again_when_the_signed_angle_is_off_then_stops(bench, scope):
    calls = {"n": 0}

    def off_then_right(p):
        calls["n"] += 1
        return {"value": p.context["requested_deg"] + (7.0 if calls["n"] == 1 else 0.0)}

    res, opr = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="20"), bench=bench,
                   incident_polarizer=off_then_right)
    assert res.success and calls["n"] == 2 and res.data["rows"][0]["incident_deg_actual"] == 20.0
    assert [p.context["attempt"] for p in opr.seen if p.context["reason"] == "incident_polarizer"] == [1, 2]
    # three wrong answers in a row: stop
    res, opr = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="20"), bench=bench,
                   incident_polarizer=lambda p: {"value": 50.0})
    assert not res.success and res.data["stop_kind"] == "polarizer_mismatch" and opr.reasons().count("incident_polarizer") == 3
    assert res.data["stopped_at_step"] == 1 and bench.mic.shutter_open is False
    # 360 and 0 are the same direction
    res, _ = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="360"), bench=bench,
                 incident_polarizer=lambda p: {"value": 0.0})
    assert res.success


def test_polmat_operator_dismisses_the_angle_prompt(bench, scope):
    res, _ = run(PolarizationMatrix, pol_params(), bench=bench, incident_polarizer=lambda p: {"value": 999})
    assert not res.success and res.data["stop_kind"] == "operator_cancelled"
    assert bench.mic.shutter_open is False and res.data["steps_done"] == 0


def test_polmat_refuses_an_analyzer_reading_outside_the_microscope_range(make_bench, scope):
    b = make_bench(mic_limits={"analyzer_deg": [0, 180], "x_um": [-100, 100], "y_um": [-100, 100], "z_um": [-10, 10],
                               "max_laser_power_mw": {"532": 20}})
    res = PolarizationMatrix().execute(Ctx(), pol_params(incident_angles_deg="360", mode="crs"))
    assert not res.success and "analyzer reading" in res.error and res.data["touched_hardware"] is False
    assert b.mic.calls == []
    bad = PolarizationMatrix().execute(Ctx(), pol_params(incident_angles_deg="0,400"))
    assert not bad.success and "[0, 360]" in bad.error


def test_polmat_external_source_goes_through_both_gates(bench, scope):
    bench.mic.shg_source = lambda: (1064.0, 0.001) if bench.npi.emitting else None
    res, opr = run(PolarizationMatrix, {"incident_angles_deg": "0", "mode": "par", "integration_s": 0.1,
                                        "settle_s": 0, "laser_id": "npi", "center_nm": 532, "feature_nm": 532},
                   bench=bench)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "incident_polarizer"], "the beam is cleared before the first angle is asked"
    assert res.data["rows"][0]["found"] is True
    assert_safe(bench)
    # neither witec_laser nor laser_id: the operator is assumed to have switched the light on
    plan = PolarizationMatrix().execute(Ctx(), {"incident_angles_deg": "0", "dry_run": True})
    assert plan.success and any("assumed" in n for n in plan.data["notes"])
    # both sources: refused
    both = PolarizationMatrix().execute(Ctx(), {"witec_laser": "532", "laser_id": "npi", "dry_run": True})
    assert not both.success and "not both" in both.error


def test_polmat_witec_power_over_the_cap_is_refused(bench, scope):
    res = PolarizationMatrix().execute(Ctx(), pol_params(witec_power_mw=50))
    assert not res.success and "outside [0, 20" in res.error and res.data["touched_hardware"] is False
    no_cap = PolarizationMatrix().execute(Ctx(), pol_params(witec_laser="355", witec_power_mw=1))
    assert not no_cap.success
    assert bench.mic.calls == []


# ── hands in the beam path: a person swaps an optic / turns the incident polariser ───────
#
# Before a person reaches in, the WITec shutter is shut and the external source is dark: switched off by software
# when software can (whether this run switched it on or it was already on), otherwise the operator is asked to
# switch it off or block the beam first. The prompt only says what was read back. Afterwards the source comes back
# by software (allow_emission_control, manage_emission, a fresh signature) or the operator is asked to.


def _polmat_source_params(**kw):
    return {"incident_angles_deg": "0,10", "mode": "par", "integration_s": 0.05, "settle_s": 0, "laser_id": "npi",
            "center_nm": 532, "feature_nm": 532, **kw}


def test_polarization_matrix_takes_the_external_source_dark_while_a_person_turns_the_polariser(bench, scope):
    bench.mic.shg_source = lambda: (1064.0, 0.001) if bench.npi.emitting else None       # SHG peak only while it emits
    emitting_at_prompt, shutter_at_prompt, emitting_in_exposure, messages = [], [], [], []
    orig_acquire = bench.mic.acquire

    def acquire(**kw):
        emitting_in_exposure.append(bench.npi.emitting)
        return orig_acquire(**kw)

    bench.mic.acquire = acquire

    def turn(p):
        emitting_at_prompt.append(bench.npi.emitting)
        shutter_at_prompt.append(bench.mic.shutter_open)
        messages.append(p.message)
        return {"value": p.context["requested_deg"]}

    res, opr = run(PolarizationMatrix, _polmat_source_params(), bench=bench, incident_polarizer=turn)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "incident_polarizer", "incident_polarizer"]
    assert emitting_at_prompt == [False, False] and shutter_at_prompt == [False, False]
    assert emitting_in_exposure == [True, True], "…and it is back for the exposures"
    assert all(r["found"] for r in res.data["rows"])
    assert all("The WITec shutter is closed (confirmed by read-back)." in m for m in messages)
    assert all("The source npi has been switched off by software (read back: off)." in m for m in messages)
    assert_safe(bench)


def test_polarization_matrix_a_hand_lit_source_is_taken_dark_and_the_operator_switches_it_back_on(make_bench, scope):
    b = make_bench(npi_emission=False)                            # software may switch it off (always), not on
    b.mic.shg_source = lambda: (1064.0, 0.001) if b.npi.emitting else None
    b.npi.emitting = True                                         # lit by hand before the protocol started
    at_prompt = []

    def turn(p):
        at_prompt.append(b.npi.emitting)
        return {"value": p.context["requested_deg"]}

    def back_on(p):
        b.npi.emitting = True                                     # the operator switches it on in the vendor software
        return {"choice": "yes"}

    res, opr = run(PolarizationMatrix, _polmat_source_params(manage_emission=False), bench=b,
                   incident_polarizer=turn, source_back_on=back_on)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "incident_polarizer", "source_back_on", "incident_polarizer", "source_back_on"]
    assert at_prompt == [False, False]
    assert all(r["found"] for r in res.data["rows"]), "the source was on again for every exposure"
    assert_safe(b)


def test_polarization_matrix_manage_emission_is_only_a_wish_when_the_gate_is_closed(make_bench, scope):
    """manage_emission=true but allow_emission_control=false: the source cannot come back by software (the driver
    would refuse emission_on), so the operator is asked - the run does not stop on a refused emission_on."""
    b = make_bench(npi_emission=False)
    b.mic.shg_source = lambda: (1064.0, 0.001) if b.npi.emitting else None
    b.npi.emitting = True

    def back_on(p):
        b.npi.emitting = True
        return {"choice": "yes"}

    res, opr = run(PolarizationMatrix, _polmat_source_params(), bench=b, source_back_on=back_on)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "incident_polarizer", "source_back_on", "incident_polarizer", "source_back_on"]
    assert_safe(b)


def test_a_confirmed_switch_on_that_the_source_does_not_show_stops_the_run(make_bench, scope):
    b = make_bench(npi_emission=False)
    b.npi.emitting = True
    res, opr = run(PolarizationMatrix, _polmat_source_params(manage_emission=False), bench=b,
                   source_back_on=lambda p: {"choice": "yes"})            # "done" - and the source still reads dark
    assert not res.success and res.data["stop_kind"] == "emission_refused" and res.data["steps_done"] == 0
    assert "still reports no emission after the confirmation" in res.data["stop_reason"]
    assert opr.reasons() == ["laser_attest", "incident_polarizer", "source_back_on"]
    assert_safe(b)


def test_polarization_matrix_without_any_source_tells_the_operator_to_block_the_beam(bench, scope):
    seen = []
    res, opr = run(PolarizationMatrix, {"incident_angles_deg": "0", "mode": "par", "integration_s": 0.05, "settle_s": 0},
                   bench=bench, incident_polarizer=lambda p: (seen.append(p.message), {"value": 0.0})[1])
    assert res.success, res.error
    assert "does not control your light source: switch it off or block the beam" in seen[0]
    assert "The WITec shutter is closed" in seen[0]


def test_the_waveplate_prompt_names_the_plates_that_cover_the_wavelength_in_its_message(bench, scope, monkeypatch):
    """The prompt shows the slot's own option labels (an asker's labels are dropped), so a mark on the option never
    reaches the operator: which plates can be used has to be in the message, before the operator chooses."""
    seen = []

    def plate(p):
        seen.append(p)
        return Operator.default(p, "waveplate")

    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="1040,540"), bench=bench, waveplate=plate)
    assert res.success, res.error
    first, second = seen
    assert first.context["covering"] == ["hwp_1064", "hwp_broad"] and second.context["covering"] == ["hwp_532"]
    msg = first.message
    assert "Plates that cover 1040 nm: λ/2 @1064 nm (hwp_1064), broadband λ/2 (hwp_broad)." in msg
    assert "hwp_532" not in msg.split("Plates that cover 1040 nm:")[1].split(".")[0]
    assert "the plate registered now is (none registered) and does not cover it." in msg
    assert "Plates that cover 540 nm: λ/2 @532 nm (hwp_532)." in second.message
    assert "the plate registered now is λ/2 @1064 nm (hwp_1064)" in second.message, "the plate in the beam shows its label too"
    assert [o.label for o in first.options] == ["λ/2 @532 nm", "λ/2 @1064 nm", "无 / none", "broadband λ/2"], "the slot's labels, no marks"
    monkeypatch.setenv("MAST_LANG", "zh")                           # the protocol runs on its own thread: the process default
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="1040"), bench=bench, waveplate=plate)
    assert "覆盖 1040 nm 的片：λ/2 @1064 nm (hwp_1064)、broadband λ/2 (hwp_broad)。" in seen[-1].message


def test_shg_a_source_that_was_already_on_is_taken_dark_for_the_plate_swap_too(bench, scope):
    bench.tun.emission_on()                                       # somebody switched it on before the protocol started
    seen = []

    def plate(p):
        seen.append((bench.tun.emitting, p.message))
        return Operator.default(p, "waveplate")

    exposures = []
    orig_acquire = bench.mic.acquire

    def acquire(**kw):
        exposures.append(bench.tun.emitting)
        return orig_acquire(**kw)

    bench.mic.acquire = acquire
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="540,1040"), bench=bench, waveplate=plate)
    assert res.success, res.error
    assert [e for e, _ in seen] == [False, False], "a source this run did not switch on is taken dark for the swap as well"
    assert all("The source superk has been switched off by software (read back: off)." in m for _, m in seen)
    assert all("switched off for the swap" not in m for _, m in seen)
    assert exposures == [True, True], "and brought back by software afterwards (allow_emission_control, a fresh signature)"
    assert sidecar_of(res.data["spectrum_files"][0])["conditions"]["source_mode"] == "software", "…so it is this run's own now"
    assert_safe(bench)


def test_shg_a_hand_lit_source_is_switched_off_for_the_swap_and_the_operator_switches_it_back_on(make_bench, scope):
    b = make_bench(superk_emission=False)
    switched_on_by_hand(b.tun, {0: 7.0})
    at_plate, exposures = [], []
    orig_acquire = b.mic.acquire

    def acquire(**kw):
        exposures.append(b.tun.emitting)
        return orig_acquire(**kw)

    b.mic.acquire = acquire

    def plate(p):
        at_plate.append(b.tun.emitting)
        return Operator.default(p, "waveplate")

    def back_on(p):
        b.tun.emitting = True
        return {"choice": "yes"}

    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="540,1040", manage_emission=False), bench=b,
                   waveplate=plate, source_back_on=back_on)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "waveplate", "source_back_on", "waveplate", "source_back_on"]
    assert at_plate == [False, False] and exposures == [True, True]
    assert b.tun.channels()[0]["amplitude_pct"] == 5.0
    assert_safe(b)


def test_shg_manage_emission_false_leaves_the_switch_on_to_the_operator_even_when_software_could(make_bench, scope):
    b = make_bench()                                              # allow_emission_control=true: software COULD switch it on
    switched_on_by_hand(b.tun, {0: 7.0})
    calls = []
    real_on = b.tun.emission_on
    b.tun.emission_on = lambda: (calls.append("on"), real_on())[1]

    def back_on(p):
        b.tun.emitting = True
        return {"choice": "yes"}

    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="540,1040", manage_emission=False), bench=b,
                   source_back_on=back_on)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "waveplate", "source_back_on", "waveplate", "source_back_on"]
    assert calls == [], "manage_emission=false: the protocol never switches the emission on"
    assert_safe(b)


def test_shg_when_software_cannot_take_the_source_dark_the_operator_is_asked_first(make_bench, scope, monkeypatch):
    b = make_bench(superk_emission=False)
    switched_on_by_hand(b.tun, {0: 7.0})
    monkeypatch.setattr(b.tun, "emission_off", lambda: True)      # the driver cannot take it dark: the read-back says "on"
    log = []

    def switch_off_by_hand(p):
        log.append(("dark?", p.message))
        b.tun.emitting = False
        return {"choice": "yes"}

    def plate(p):
        log.append(("plate", p.message, b.tun.emitting))
        return Operator.default(p, "waveplate")

    def back_on(p):
        b.tun.emitting = True
        return {"choice": "yes"}

    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="540", manage_emission=False), bench=b,
                   source_off_for_swap=switch_off_by_hand, waveplate=plate, source_back_on=back_on)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "source_off_for_swap", "waveplate", "source_back_on"], "asked BEFORE hands go in"
    dark = next(e for e in log if e[0] == "dark?")[1]
    assert "Software could not confirm that the source superk is off (read back: still emitting)" in dark
    kind, msg, emitting = next(e for e in log if e[0] == "plate")
    assert emitting is False
    assert "you confirmed that you switched it off or blocked the beam" in msg
    assert "switched off by software" not in msg, "the prompt only states what is true"


def test_an_expired_signature_is_not_asked_for_while_the_source_may_still_be_on(bench, scope, expiring_store, monkeypatch):
    """The signature runs out after step 1 and the source cannot be taken dark (its read-back stays 'on'): the
    operator is not asked to sign a laser-area-safe statement while the beam may still be there."""
    _expire_after(bench, expiring_store, 1)
    real_off = bench.tun.emission_off
    state = {"stuck": False}

    def off():
        return True if state["stuck"] else real_off()

    monkeypatch.setattr(bench.tun, "emission_off", off)
    orig_acquire = bench.mic.acquire

    def acquire(**kw):
        out = orig_acquire(**kw)
        state["stuck"] = True                                     # from now on the off command does not take
        return out

    bench.mic.acquire = acquire
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="700,800,900", waveplate_slot="none"), bench=bench)
    assert not res.success and res.data["stop_kind"] == "emission_refused"
    assert "could not be confirmed off" in res.data["stop_reason"]
    assert opr.reasons() == ["laser_attest"], "only the first signing: never asked again while the beam may be on"
    assert res.data["steps_done"] == 1 and res.data["stopped_at_step"] == 2


# ── TemperatureSeries ────────────────────────────────────────────────────────────────────────────────


def temp_params(**kw):
    return {"temperatures_c": "30,40", "ramp_c_per_min": 20, "tolerance_c": 0.5, "hold_s": 0.05, "poll_s": 0.01,
            "timeout_s": 20, "integration_s": 0.1, "settle_s": 0, "witec_laser": "532", **kw}


def test_temperature_dry_run_plan_and_limits(make_bench, scope):
    b = make_bench()
    res = TemperatureSeries().execute(Ctx(), {**temp_params(), "dry_run": True})
    assert res.success, res.error
    d = res.data
    assert d["n_steps"] == 2 and d["estimated_machine_s"] > 0 and d["steps"][0]["conditions"]["temperature_set_c"] == 30.0
    assert b.tc.status()["state"] == "stopped" and b.mic.calls == []
    for what, params in {
            "temperature above the controller range": temp_params(temperatures_c="30,900"),
            "ramp above the controller limit": temp_params(ramp_c_per_min=500),
            "final setpoint below absolute zero": temp_params(final_setpoint_c=-300),
            "final setpoint above the controller range": temp_params(final_setpoint_c=900),
            "no temperatures": temp_params(temperatures_c=""),
            "series longer than the microscope allows": temp_params(measure="analyzer_series", series_points=1442)}.items():
        out = TemperatureSeries().execute(Ctx(), {**params, "dry_run": True})
        assert not out.success, what
    ok = TemperatureSeries().execute(Ctx(), {**temp_params(measure="analyzer_series", series_points=1441), "dry_run": True})
    assert ok.success, ok.error                                  # exactly at the limit is fine
    assert b.tc.status()["state"] == "stopped" and b.mic.calls == []


def test_temperature_refuses_when_writes_are_disabled(make_bench, scope):
    b = make_bench(tc_write=False)
    res = TemperatureSeries().execute(Ctx(), temp_params())
    assert not res.success and "write_enabled" in res.error and res.data["touched_hardware"] is False
    assert b.tc.status()["state"] == "stopped" and b.mic.calls == []


def test_temperature_series_single_measurements(make_bench, scope):
    b = make_bench()
    res, opr = run(TemperatureSeries, temp_params(autofocus=True, power_meter_id="pm", final_setpoint_c=25,
                                                  tag="ctrl"), bench=b)
    assert res.success, res.error
    d = res.data
    assert d["status"] == "completed" and d["steps_done"] == 2
    assert [round(r["temperature_c"]) for r in d["rows"]] == [30, 40]
    assert all(abs(r["temperature_c"] - r["temperature_set_c"]) <= 0.5 for r in d["rows"])
    assert all(r["found"] for r in d["rows"]), "Si line seen: the WITec shutter was open during the exposure"
    for r in d["rows"]:                                        # the temperature is in the sidecar, not typed into the name
        assert re.fullmatch(r"tser_s00[12]_single_ctrl", r["stem"]), r["stem"]
    side = sidecar_of(d["spectrum_files"][0])
    c = side["conditions"]
    assert c["temperature_set_c"] == 30.0 and abs(c["temperature_c"] - 30.0) <= 0.5
    assert c["autofocus"]["ok"] is True and c["measure"] == "single" and "tolerance_c" in c and "settle_s" in c
    assert side["instruments"]["temperature"]["status"]["state"] == "running"
    assert side["optical_state"]["laser_area_safe"]["value"] is True             # the WITec laser needs the signature too
    # normal finish with final_setpoint_c: the controller is sent there; light is off
    assert d["cleanup"]["temperature"] == {"action": "final_setpoint", "setpoint_c": 25.0}
    assert b.tc.target == 25.0 and b.tc.status()["state"] == "running"
    assert b.mic.shutter_open is False and opr.reasons() == ["laser_attest"]


def test_temperature_series_stops_the_controller_without_a_final_setpoint(make_bench, scope):
    b = make_bench()
    res, _ = run(TemperatureSeries, temp_params(), bench=b)
    assert res.success and res.data["cleanup"]["temperature"] == {"action": "stop"}
    assert b.tc.status()["state"] == "stopped"


def test_temperature_timeout_stops_cleanly_with_the_data_so_far(make_bench, scope):
    b = make_bench(tc={"time_scale": 1.0, "tau_s": 100000.0})                   # will never get there
    res, _ = run(TemperatureSeries, temp_params(temperatures_c="23,60", timeout_s=0.3, tolerance_c=0.5), bench=b)
    # 23 deg is within 0.5 of the start 22? no (1 deg) -> times out at step 1
    assert not res.success and res.data["stop_kind"] == "temperature_timeout"
    assert res.data["stopped_at_step"] == 1 and res.data["steps_done"] == 0
    assert b.tc.status()["state"] == "stopped" and b.mic.shutter_open is False
    assert summary_of(res)["stop"]["kind"] == "temperature_timeout"
    # a point that is already there needs no ramp
    res2, _ = run(TemperatureSeries, temp_params(temperatures_c="22", timeout_s=2), bench=b)
    assert res2.success


def test_temperature_final_setpoint_the_controller_refuses_falls_back_to_stop(make_bench, scope):
    from mast.instruments.temperature import TemperatureLimitError

    b = make_bench()
    orig = b.tc.set_setpoint

    def picky(c, **kw):
        if c == 25.0:
            raise TemperatureLimitError("not today")
        return orig(c, **kw)

    b.tc.set_setpoint = picky
    res, _ = run(TemperatureSeries, temp_params(final_setpoint_c=25), bench=b)
    assert res.success, res.error
    assert res.data["cleanup"]["temperature"]["action"] == "stop"
    assert any("final setpoint" in w for w in res.data["warnings"])
    assert b.tc.status()["state"] == "stopped"


def test_temperature_abort_while_settling_never_sends_a_new_setpoint(make_bench, scope):
    b = make_bench(tc={"time_scale": 1.0, "tau_s": 100000.0})
    flag = threading.Event()
    threading.Timer(0.3, flag.set).start()
    res, _ = run(TemperatureSeries, temp_params(temperatures_c="60", timeout_s=30, final_setpoint_c=25),
                 bench=b, ctx=Ctx(abort=flag.is_set))
    assert not res.success and res.data["status"] == "aborted"
    assert res.data["cleanup"]["temperature"] == {"action": "stop"}, "after an abort: stop, never a new setpoint"
    assert b.tc.status()["state"] == "stopped" and b.tc.target == 60.0


def test_temperature_analyzer_series_measure(make_bench, scope):
    b = make_bench()
    res, _ = run(TemperatureSeries, temp_params(measure="analyzer_series", series_points=5, series_start_deg=0,
                                                series_stop_deg=180), bench=b)
    assert res.success, res.error
    assert len(b.mic.series_runs) == 2 and b.mic.series_runs[0]["points"] == 5
    side = sidecar_of_json(res.data["spectrum_files"][0])
    assert side["conditions"]["measure"] == "analyzer_series" and side["conditions"]["series_points"] == 5
    assert side["extra"]["series"]["points"] == 5 and side["files"]["array"] is None
    assert [r["measure"] for r in res.data["rows"]] == ["analyzer_series"] * 2
    assert b.mic.shutter_open is False


def sidecar_of_json(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_temperature_series_external_source_and_attestation(make_bench, scope):
    b = make_bench()
    b.mic.shg_source = lambda: b.tun.output()
    res, opr = run(TemperatureSeries, {"temperatures_c": "30", "ramp_c_per_min": 20, "hold_s": 0.05, "poll_s": 0.01,
                                       "integration_s": 0.1, "settle_s": 0, "laser_id": "superk"}, bench=b,
                   ctx=Ctx())
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest"]
    assert b.tun.emitting is False and b.tc.status()["state"] == "stopped"


# ── somebody else stopped the temperature controller ────────────────────────────────────────


def _spy_setpoints(tc):
    sent = []
    real = tc.set_setpoint

    def spy(c, **kw):
        sent.append(c)
        return real(c, **kw)

    tc.set_setpoint = spy
    return sent


def test_temperature_series_stops_when_somebody_else_stopped_the_controller_and_sends_no_other_setpoint(make_bench, scope):
    """StopTemperatureRamp (or the panel's stop) pressed while step 1 measures: the controller reads 'stopped' and
    this run did not stop it. Step 2 must not start the stage again - and neither may the final setpoint."""
    b = make_bench()
    sent = _spy_setpoints(b.tc)
    orig_acquire = b.mic.acquire

    def acquire(**kw):
        out = orig_acquire(**kw)
        b.tc.stop()
        return out

    b.mic.acquire = acquire
    res, opr = run(TemperatureSeries, temp_params(temperatures_c="30,40,50", final_setpoint_c=25), bench=b)
    d = res.data
    assert not res.success and d["stop_kind"] == "temperature_stopped" and d["status"] == "stopped"
    assert d["steps_done"] == 1 and d["stopped_at_step"] == 2
    assert sent == [30.0], "no setpoint after somebody stopped it - the final one included"
    assert d["cleanup"]["temperature"] == {"action": "stop"}
    assert b.tc.status()["state"] == "stopped" and b.tc.target == 30.0
    assert "this run did not stop it" in d["stop_reason"]
    assert b.mic.shutter_open is False


def test_temperature_series_stops_when_the_controller_is_stopped_while_it_settles(make_bench, scope):
    b = make_bench(tc={"time_scale": 1.0, "tau_s": 100000.0})        # never settles on its own
    sent = []
    real = b.tc.set_setpoint

    def spy(c, **kw):
        sent.append(c)
        out = real(c, **kw)
        threading.Timer(0.15, b.tc.stop).start()                      # somebody presses StopTemperatureRamp
        return out

    b.tc.set_setpoint = spy
    res, _ = run(TemperatureSeries, temp_params(temperatures_c="60,70", timeout_s=30, final_setpoint_c=25), bench=b)
    d = res.data
    assert not res.success and d["stop_kind"] == "temperature_stopped" and d["status"] == "stopped"
    assert d["steps_done"] == 0 and d["stopped_at_step"] == 1 and d["stop_phase"] == "step"
    assert sent == [60.0] and d["cleanup"]["temperature"] == {"action": "stop"}
    assert b.tc.status()["state"] == "stopped"


def test_temperature_series_does_not_send_the_final_setpoint_to_a_controller_somebody_stopped(make_bench, scope):
    b = make_bench()
    sent = _spy_setpoints(b.tc)
    orig_acquire = b.mic.acquire

    def acquire(**kw):
        out = orig_acquire(**kw)
        b.tc.stop()                                                   # pressed during the last measurement
        return out

    b.mic.acquire = acquire
    res, _ = run(TemperatureSeries, temp_params(temperatures_c="30", final_setpoint_c=25), bench=b)
    assert res.success, res.error                                    # the point itself was measured
    assert sent == [30.0], "…but a new setpoint would start the stage again"
    assert res.data["cleanup"]["temperature"]["action"] == "stop"
    assert "stopped by somebody else" in res.data["cleanup"]["temperature"]["note"]
    assert any("stopped by somebody else" in w for w in res.data["warnings"])
    assert b.tc.status()["state"] == "stopped"


def test_temperature_series_a_controller_that_starts_out_stopped_is_not_somebody_elses_decision(make_bench, scope):
    b = make_bench()
    assert b.tc.status()["state"] == "stopped"
    sent = _spy_setpoints(b.tc)
    res, _ = run(TemperatureSeries, temp_params(temperatures_c="30,40"), bench=b)
    assert res.success, res.error
    assert sent == [30.0, 40.0]


# ── PowerSeries ────────────────────────────────────────────────────────────────────────────────────────


def wire_npi(b):
    b.mic.shg_source = lambda: (1064.0, b.npi.output_w()) if b.npi.emitting else None
    b.pm.source = b.npi.output_w


def power_params(**kw):
    return {"laser_id": "npi", "powers": "10,20,40,80", "integration_s": 0.05, "settle_s": 0, "center_nm": 532,
            "feature_nm": 532, **kw}


def test_power_series_shg_slope_is_two(bench, scope):
    wire_npi(bench)
    res, opr = run(PowerSeries, power_params(power_meter_id="pm"), bench=bench)
    assert res.success, res.error
    d = res.data
    assert opr.reasons() == ["laser_attest"] and d["status"] == "completed" and d["steps_done"] == 4
    fit = d["analysis"]["fit"]
    assert d["analysis"]["x_source"] == "measured_power_mw"
    assert abs(fit["slope"] - 2.0) < 0.1 and fit["r2"] > 0.99 and fit["n"] == 4
    assert [r["power_set"] for r in d["rows"]] == [10, 20, 40, 80] and d["rows"][0]["power_unit"] == "pct"
    assert d["rows"][0]["power_mw"] == pytest.approx(0.2 * 0.10 * 1e3, rel=1e-6)
    assert all(r["found"] for r in d["rows"])
    side = sidecar_of(d["spectrum_files"][0])
    assert side["conditions"]["power_mode"] == "software" and side["conditions"]["power_set"] == 10.0
    assert side["conditions"]["excitation_nm"] == 1064.0
    assert re.fullmatch(r"pser_1064nm_p10pct", d["rows"][0]["stem"]), d["rows"][0]["stem"]
    assert Path(d["plot_path"]).read_bytes()[:4] == b"\x89PNG"
    assert_safe(bench)
    assert bench.pm.reads == 4 * 2 * 5                          # before and after every step, 5 readings averaged


def test_power_series_without_a_meter_fits_the_set_values(bench, scope):
    wire_npi(bench)
    res, _ = run(PowerSeries, power_params(), bench=bench)
    assert res.success, res.error
    assert res.data["analysis"]["x_source"] == "set_or_signed_power"
    assert abs(res.data["analysis"]["fit"]["slope"] - 2.0) < 0.1
    assert res.data["rows"][0]["power_mw"] is None


def test_power_series_manual_source_asks_the_operator_for_every_step(bench, scope):
    wire_npi(bench)
    bench.npi.SUPPORTS = frozenset({"emission"})               # the NPI Rainbow: no documented power command

    def set_dial(p):                                           # the operator turns the vendor-software dial
        bench.npi.power_pct = p.context["requested"]
        return {"value": p.context["requested"]}

    res, opr = run(PowerSeries, power_params(), bench=bench, power=set_dial)
    assert res.success, res.error
    assert opr.reasons().count("power") == 4 and opr.reasons().count("laser_attest") == 1
    assert opr.reasons()[:2] == ["laser_attest", "power"], "signed for the laser first, then the first level, then the beam"
    assert [r["power_set"] for r in res.data["rows"]] == [10, 20, 40, 80]
    side = sidecar_of(res.data["rows"][2]["file"])             # third step: 40 %
    assert side["conditions"]["power_mode"] == "manual" and side["conditions"]["power_attested"] == 40.0
    assert side["optical_state"]["npi_power_pct"]["value"] == 40.0
    assert abs(res.data["analysis"]["fit"]["slope"] - 2.0) < 0.1
    assert_safe(bench)
    # outside the slot's range: refused up front
    bad = PowerSeries().execute(Ctx(), power_params(powers="10,150"))
    assert not bad.success and "range" in bad.error and bad.data["touched_hardware"] is False


def test_power_series_witec_laser_respects_the_cap(bench, scope):
    res, _ = run(PowerSeries, {"witec_laser": "532", "powers": "0.5,1,2", "integration_s": 0.05, "settle_s": 0,
                               "center_nm": 547, "feature_nm": 547.2}, bench=bench)
    assert res.success, res.error
    assert [r["power_unit"] for r in res.data["rows"]] == ["mW"] * 3
    assert [r["power_mw"] for r in res.data["rows"]] == [0.5, 1.0, 2.0], "no meter: the WITec read-back is the power"
    assert bench.mic.laser_power_mw[0] == 2.0 and bench.mic.shutter_open is False
    assert all(r["found"] for r in res.data["rows"])
    over = PowerSeries().execute(Ctx(), {"witec_laser": "532", "powers": "1,50", "dry_run": True})
    assert not over.success and "outside the WITec laser cap [0, 20" in over.error
    uncontrollable = PowerSeries().execute(Ctx(), {"witec_laser": "355", "powers": "1", "dry_run": True})
    assert not uncontrollable.success
    neither = PowerSeries().execute(Ctx(), {"powers": "1", "dry_run": True})
    assert not neither.success and "exactly one source" in neither.error
    both = PowerSeries().execute(Ctx(), {"powers": "1", "witec_laser": "532", "laser_id": "npi", "dry_run": True})
    assert not both.success
    assert bench.mic.calls == []


def test_power_series_software_power_limit(bench, scope):
    over = PowerSeries().execute(Ctx(), power_params(powers="10,150"))
    assert not over.success and "outside [0, 100" in over.error and over.data["touched_hardware"] is False
    assert bench.npi.emitting is False and bench.npi.power_pct == 50.0


def test_power_series_manual_emission_when_software_may_not_switch_it(make_bench, scope):
    b = make_bench(npi_emission=False)
    wire_npi(b)
    down = {"manage_emission": False, "powers": "40,20,10"}            # software does not add light: the series only dims
    res, opr = run(PowerSeries, power_params(**down), bench=b)
    assert not res.success                                     # the sim source never reports emitting: the confirm is honest
    assert opr.reasons() == ["laser_attest", "manual_emission"] and res.data["stop_kind"] == "emission_refused"
    res, opr = run(PowerSeries, power_params(**down), bench=b,
                   manual_emission=lambda p: {"choice": "no"})            # the operator declines to switch it on
    assert not res.success and res.data["stop_kind"] == "operator_declined"
    b.npi.emitting = True                                      # now somebody really switched it on
    res, opr = run(PowerSeries, power_params(**down), bench=b)
    assert res.success and opr.reasons() == []                 # already emitting: nothing to ask
    assert b.npi.emitting is False, "…and it is still switched off at the end (safe direction)"


# ── PowerSeries under allow_emission_control=false: the series may only go down ───────────────────────────
#
# Software does not add light while the source emits, so a series that raises the power - from one step to the next
# (the source emits from the first step on), or from the power the source has now to the first value when it is
# emitting - is refused up front; repeating a value, dimming (to 0 as well) and a source that is still off are fine.


def test_power_series_a_source_at_zero_is_refused_up_front(make_bench, scope):
    b = make_bench(npi_emission=False)
    wire_npi(b)
    b.npi.power_pct, b.npi.emitting = 0.0, True                   # switched on by hand, the dial still at 0
    res, opr = run(PowerSeries, power_params(manage_emission=False, powers="40,20,10"), bench=b)
    assert not res.success and res.data["refused"] is True and res.data["touched_hardware"] is False
    assert len(res.data["problems"]) == 1
    msg = res.error
    assert "allow_emission_control is false and the source is emitting (or its emission state cannot be read), so software adds no light" in msg
    assert "raising the power from 0 pct to 40 pct" in msg
    assert "set the power to at least 40 pct by hand" in msg and "afterwards the protocol only dims it" in msg      # way out 1
    assert "enable allow_emission_control in the device config after the bench self-test" in msg                    # way out 2
    assert opr.seen == [] and b.mic.calls == [] and b.mic.acquisitions == 0 and b.pm.reads == 0
    assert b.npi.power_pct == 0.0 and b.npi.emitting is True, "the operator's state is left alone"
    assert not (scope.root / "root" / "artifacts" / "optics_protocols").exists()
    assert not (scope.root / "experiments").exists()
    assert optical_state.get_store().history == []


@pytest.mark.parametrize("dial", [0.0, 5.0])
def test_power_series_a_source_that_is_still_off_gets_its_power_set_first_and_the_operator_switches_it_on(dial, make_bench, scope):
    b = make_bench(npi_emission=False)
    wire_npi(b)
    b.npi.power_pct = dial                                        # off, the dial at 0 or 5 %: 40 % raises it
    at_confirm = []

    def switch_on(p):
        at_confirm.append(b.npi.power_pct)                        # set BEFORE the operator is asked
        b.npi.emitting = True
        return {"choice": "yes"}

    res, opr = run(PowerSeries, power_params(manage_emission=False, powers="40,20"), bench=b, manual_emission=switch_on)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "manual_emission"] and at_confirm == [40.0]
    assert [r["power_set"] for r in res.data["rows"]] == [40, 20]
    msg = opr.seen[1].message
    assert "once the source emits, software adds no light" in msg and "set the power to 40 pct while the source was still off" in msg
    assert_safe(b)


def test_power_series_a_lit_source_below_the_first_value_is_refused_too(make_bench, scope):
    b = make_bench(npi_emission=False)
    b.npi.power_pct, b.npi.emitting = 5.0, True
    res = unattended(PowerSeries, power_params(manage_emission=False, powers="40,20"))
    assert not res.success and res.data["touched_hardware"] is False
    assert "raising the power from 5 pct to 40 pct" in res.error and b.npi.power_pct == 5.0 and b.mic.calls == []


def test_power_series_a_raise_the_plan_did_not_see_is_still_stopped_by_the_driver(make_bench, scope, monkeypatch):
    """The plan check is an early warning, not the gate: if the power is lower than the plan read (it changed in
    between), the driver's own gate refuses the raise (any raise on a source that emits adds light)."""
    b = make_bench(npi_emission=False)
    b.npi.power_pct, b.npi.emitting = 5.0, True
    monkeypatch.setattr(b.npi, "get_power", lambda: 80.0)                 # the plan reads 80 %: 40 and 20 only dim …
    res, opr = run(PowerSeries, power_params(manage_emission=False, powers="40,20"), bench=b)
    assert not res.success and res.data["stop_kind"] == "emission_refused" and res.data["stop_phase"] == "prepare"
    assert res.data["steps_done"] == 0 and b.npi.power_pct == 5.0         # … but the source really was at 5 %
    assert b.npi.emitting is False


def test_power_series_a_lit_source_runs_to_completion_and_may_end_at_zero(make_bench, scope):
    b = make_bench(npi_emission=False)
    wire_npi(b)
    b.npi.emitting = True                                         # lit by hand, the dial at its default 50 %
    res, opr = run(PowerSeries, power_params(manage_emission=False, powers="40,40,20,10,0"), bench=b)
    assert res.success, res.error
    assert [r["power_set"] for r in res.data["rows"]] == [40, 40, 20, 10, 0] and opr.reasons() == ["laser_attest"]
    assert b.npi.power_pct == 0.0, "down to 0 is dimming: never gated"
    assert_safe(b)


def test_power_series_a_series_that_goes_up_is_refused_up_front(make_bench, scope):
    b = make_bench(npi_emission=False)
    b.npi.power_pct, b.npi.emitting = 80.0, True                  # high enough for every value: only the series raises
    for dry in (True, False):                                      # decided from the list alone: a dry run knows it too
        res = unattended(PowerSeries, power_params(manage_emission=False, powers="10,20,40,80", dry_run=dry))
        assert not res.success and res.data["touched_hardware"] is False
        (msg,) = res.data["problems"]
        assert "raises the power at 3 place(s) (e.g. step 2: 10 -> 20 pct)" in msg
        assert "order the series from high to low" in msg
        assert "enable allow_emission_control in the device config after the bench self-test" in msg
    # a 0 followed by a value is a raise like any other
    res = unattended(PowerSeries, power_params(manage_emission=False, powers="10,0,20", dry_run=True))
    assert not res.success and "raises the power at 1 place(s) (e.g. step 3: 0 -> 20 pct)" in res.error
    assert b.mic.calls == [] and b.npi.power_pct == 80.0 and b.npi.emitting is True
    # the source emits from the first step on: a table that goes up is refused whatever the source does now
    b.npi.emitting = False
    res = unattended(PowerSeries, power_params(manage_emission=False, powers="10,20"))
    assert not res.success and res.data["touched_hardware"] is False
    assert "raises the power at 1 place(s) (e.g. step 2: 10 -> 20 pct)" in res.error and b.mic.calls == []
    # repeating a value and going down (to 0 as well) never raises
    res = PowerSeries().execute(Ctx(), power_params(manage_emission=False, powers="40,40,20,0,0", dry_run=True))
    assert res.success and res.data["valid"] is True
    # with emission control allowed the driver is the one to gate it, and it lets it through
    make_bench()
    res = PowerSeries().execute(Ctx(), power_params(manage_emission=False, powers="10,20,40,80", dry_run=True))
    assert res.success and res.data["valid"] is True


def test_power_series_an_unreadable_power_counts_as_possibly_lower(make_bench, scope, monkeypatch):
    b = make_bench(npi_emission=False)
    b.npi.emitting = True

    def unreadable():
        raise OSError("serial timeout")

    monkeypatch.setattr(b.npi, "get_power", unreadable)
    res = unattended(PowerSeries, power_params(manage_emission=False, powers="40,20"))
    assert not res.success and res.data["touched_hardware"] is False
    assert "raising the power from a power that cannot be read to 40 pct" in res.error and b.mic.calls == []


@pytest.mark.parametrize("how", ["none", "raises"])
def test_power_series_an_unreadable_emission_state_counts_as_emitting(how, make_bench, scope, monkeypatch):
    b = make_bench(npi_emission=False)
    b.npi.power_pct = 0.0

    def unreadable():
        if how == "raises":
            raise OSError("serial timeout")
        return None

    monkeypatch.setattr(b.npi, "emission_state", unreadable)
    res = unattended(PowerSeries, power_params(manage_emission=False, powers="40,20"))
    assert not res.success and res.data["touched_hardware"] is False
    assert "emission state cannot be read" in res.error and "raising the power from 0 pct to 40 pct" in res.error
    assert b.mic.calls == [] and b.npi.power_pct == 0.0


def test_power_series_a_source_that_is_off_runs_when_the_dial_was_set_by_hand_and_the_prompt_says_what_is_needed(make_bench, scope):
    b = make_bench(npi_emission=False)
    wire_npi(b)                                                   # off, the dial at its default 50 %: 40 and 20 only dim

    def switch_on(p):
        b.npi.emitting = True
        return {"choice": "yes"}

    res, opr = run(PowerSeries, power_params(manage_emission=False, powers="40,20"), bench=b, manual_emission=switch_on)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "manual_emission"]
    msg = opr.seen[1].message
    assert "allow_emission_control=false: once the source emits, software adds no light" in msg
    assert "set the power to 40 pct while the source was still off" in msg and "set it by hand" in msg
    assert_safe(b)


@pytest.mark.parametrize("allowed", [False, True])
def test_power_series_the_manual_prompt_names_what_the_gate_needs_only_when_the_gate_is_closed(make_bench, scope, allowed):
    b = make_bench(npi_emission=allowed)
    wire_npi(b)

    def switch_on(p):
        b.npi.emitting = True
        return {"choice": "yes"}

    res, opr = run(PowerSeries, power_params(manage_emission=False, powers="20,10"), bench=b, manual_emission=switch_on)
    assert res.success, res.error
    msg = opr.seen[opr.reasons().index("manual_emission")].message
    assert "Switch it on in the vendor software" in msg
    if allowed:
        assert "allow_emission_control" not in msg
    else:
        assert "allow_emission_control=false" in msg and "once the source emits, software adds no light" in msg
    assert_safe(b)


def test_power_series_dry_run_cannot_read_the_source_so_it_only_leaves_a_note(make_bench, scope):
    b = make_bench(npi_emission=False)
    b.npi.power_pct, b.npi.emitting = 0.0, True
    reads = []
    b.npi.get_power = lambda: reads.append("get_power")
    b.npi.emission_state = lambda: reads.append("emission_state")
    res = PowerSeries().execute(Ctx(), power_params(manage_emission=False, powers="40,20,10", dry_run=True))
    assert res.success and res.data["valid"] is True and res.data["touched_hardware"] is False and reads == []
    note = next(n for n in res.data["notes"] if "dry run cannot read the source" in n)
    assert "software does not add light while the source is emitting" in note and "must already be 40 pct or more" in note
    assert "enable allow_emission_control after the bench self-test" in note and "a source that is still off gets its power set first" in note
    # the gate open: nothing to warn about
    make_bench()
    res = PowerSeries().execute(Ctx(), power_params(manage_emission=False, powers="40,20,10", dry_run=True))
    assert res.success and not any("dry run cannot read the source" in n for n in res.data["notes"])


# ── cross-cutting ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cls,params", [
    (SHGExcitationScan, {"laser_id": "superk", "amplitude_pct": 5, "wavelengths_nm": "800"}),
    (PolarizationMatrix, {"incident_angles_deg": "0", "witec_laser": "532"}),
    (TemperatureSeries, {"temperatures_c": "30"}),
    (PowerSeries, {"laser_id": "npi", "powers": "10"}),
])
def test_dry_run_leaves_no_trace_anywhere(cls, params, bench, scope):
    before = bench.tc.status()["state"]
    res = cls().execute(Ctx(), {**params, "dry_run": True})
    assert res.success, res.error
    assert res.data["touched_hardware"] is False and res.data["steps"]
    assert bench.mic.calls == [] and bench.mic.acquisitions == 0 and bench.pm.reads == 0
    assert bench.tun.emitting is False and bench.npi.emitting is False and bench.tc.status()["state"] == before
    assert prompts.get_backend()._prompts == {}
    assert not (scope.root / "root" / "artifacts").exists()
    assert not (scope.root / "experiments").exists()
    assert optical_state.get_store().history == []


@pytest.mark.parametrize("cls,params", [
    (SHGExcitationScan, shg_params(wavelengths_nm="800")),
    (PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0")),
    (TemperatureSeries, temp_params(temperatures_c="30")),
    (PowerSeries, power_params(powers="10,20,40")),
])
def test_an_already_latched_abort_starts_nothing(cls, params, bench, scope):
    res = cls().execute(Ctx(abort=lambda: True), params)
    assert not res.success and res.data.get("refused") and "aborted" in res.error
    assert bench.mic.calls == [] and bench.tun.emitting is False and bench.npi.emitting is False


def test_summary_is_rewritten_after_every_step(bench, scope, monkeypatch):
    """Incremental: after each step the staging summary already lists it (a crash loses at most one step)."""
    seen = []
    real = op._Run.complete_step

    def spy(self, step, row):
        real(self, step, row)
        doc = json.loads((self.dir / f"{self.run_id}_summary.json").read_text(encoding="utf-8"))
        seen.append((doc["steps_done"], doc["status"], len(list(csv.DictReader((self.dir / f"{self.run_id}_summary.csv").open(encoding="utf-8"))))))

    monkeypatch.setattr(op._Run, "complete_step", spy)
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="800,900,1000", waveplate_slot="none"), bench=bench)
    assert res.success
    assert seen == [(1, "running", 1), (2, "running", 2), (3, "running", 3)]
    assert summary_of(res)["status"] == "completed"


def test_without_an_active_sample_the_files_stay_in_staging(bench, scope):
    el.set_active_log(None)
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="800", waveplate_slot="none"), bench=bench)
    assert res.success and res.data["stored_in_experiment"] is False
    assert all(Path(p).is_file() for p in res.data["paths"]) and "artifacts" in Path(res.data["spectrum_files"][0]).parts


def test_registry_misuse_is_a_refusal_not_a_crash(bench, scope):
    res = SHGExcitationScan().execute(Ctx(), {**SHG, "wavelengths_nm": "800", "microscope_id": "nope"})
    assert not res.success and "nope" in res.error
    res = SHGExcitationScan().execute(Ctx(), {**SHG, "wavelengths_nm": "800", "laser_id": "witec"})
    assert not res.success and "microscope" in res.error
    res = TemperatureSeries().execute(Ctx(), {"temperatures_c": "30", "temperature_id": "superk"})
    assert not res.success
    reset_instrument_registry(InstrumentRegistry(scope.root / "empty.json"))
    res = SHGExcitationScan().execute(Ctx(), shg_params())
    assert not res.success and res.data["touched_hardware"] is False


# ── the laser_area_safe gate: every protocol, before anything, at every step, right before the light ────────


class ExpiringStore(optical_state.OpticalStateStore):
    """A registry whose laser attestation can be made to run out on cue (what eight hours do in real life)."""

    def expire(self, slot_id=op.SLOT_LASER_SAFE):
        with self._lock:
            self._values[slot_id].valid_until = "2000-01-01T00:00:00+00:00"


@pytest.fixture
def expiring_store():
    store = ExpiringStore(optical_state.get_store().slots())
    optical_state.set_store(store)                     # the autouse fixture puts the original back
    return store


def _expire_after(bench, store, n):
    """The attestation runs out the moment the n-th exposure is in hand."""
    orig = bench.mic.acquire

    def wrapped(**kw):
        out = orig(**kw)
        if bench.mic.acquisitions == n:
            store.expire()
        return out

    bench.mic.acquire = wrapped


def enforce_light_attestation(b):
    """Make the sim behave like the real witec_com driver: no shutter-open, exposure or Analyzer series
    without a valid laser_area_safe attestation. Returns the list of refusals it handed out."""
    refusals: list = []

    def guard(name, fn, *, only_open=False):
        def wrapped(*a, **kw):
            opening = a[0] if a else kw.get("open_")
            if (not only_open or opening) and not optical_state.get_store().is_valid(op.SLOT_LASER_SAFE, expect=True):
                refusals.append(name)
                raise InstrumentError(f"{name}: the laser_area_safe attestation is missing")
            return fn(*a, **kw)

        return wrapped

    b.mic.acquire = guard("acquire", b.mic.acquire)
    b.mic.run_analyzer_series = guard("series", b.mic.run_analyzer_series)
    b.mic.set_shutter = guard("shutter", b.mic.set_shutter, only_open=True)
    return refusals


GATED = [
    (SHGExcitationScan, shg_params(wavelengths_nm="800", waveplate_slot="none")),
    (PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0")),
    (TemperatureSeries, temp_params(temperatures_c="30")),
    (PowerSeries, power_params(powers="10,20,40")),
]


@pytest.mark.parametrize("cls,params", GATED)
def test_every_protocol_needs_the_signature_before_it_touches_anything(cls, params, bench, scope):
    optical_state.get_store()                          # nothing signed
    # may not ask -> refused up front, nothing touched, nothing written
    res = cls().execute(Ctx(), {**params, "ask_attest_laser": False})
    assert not res.success and res.data["refused"] and res.data["touched_hardware"] is False
    assert "laser_area_safe" in res.error and "ask_attest_laser=false" in res.error
    assert bench.mic.calls == [] and bench.mic.acquisitions == 0 and bench.tun.emitting is False
    assert prompts.get_backend()._prompts == {} and not (scope.root / "root" / "artifacts").exists()
    plan0 = cls().execute(Ctx(), {**params, "ask_attest_laser": False, "dry_run": True})
    assert not plan0.success and plan0.data["valid"] is False
    # may ask -> the plan says a signature is needed first
    plan = cls().execute(Ctx(), {**params, "dry_run": True})
    assert plan.success, plan.error
    assert plan.data["before_start"] == ["laser_attest"]
    assert any("laser_area_safe" in n for n in plan.data["notes"])
    assert plan.data["manual_prompts_expected"] >= 1
    # signed -> nothing to ask
    optical_state.get_store().set_by_operator("laser_area_safe", True, by="test-operator", source="test")
    signed = cls().execute(Ctx(), {**params, "dry_run": True})
    assert signed.success and signed.data["before_start"] == []
    assert not any("laser_area_safe" in n for n in signed.data["notes"])


GATED_RUNS = {
    "SHG, external source": (SHGExcitationScan, shg_params(wavelengths_nm="700,800", waveplate_slot="none"), False),
    "PolarizationMatrix, WITec laser": (PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10"), False),
    "TemperatureSeries, single": (TemperatureSeries, temp_params(temperatures_c="30,40"), False),
    "TemperatureSeries, Analyzer series": (TemperatureSeries, temp_params(temperatures_c="30", measure="analyzer_series",
                                                                          series_points=3), False),
    "PowerSeries, WITec laser": (PowerSeries, {"witec_laser": "532", "powers": "1,2", "integration_s": 0.05,
                                               "settle_s": 0, "center_nm": 547, "feature_nm": 547.2}, False),
    "PowerSeries, external source": (PowerSeries, power_params(powers="10,20"), True),
}


@pytest.mark.parametrize("name", list(GATED_RUNS))
def test_a_driver_that_refuses_light_without_a_signature_is_never_asked_to_emit(name, bench, scope):
    cls, params, npi = GATED_RUNS[name]
    if npi:
        wire_npi(bench)
    refusals = enforce_light_attestation(bench)
    with pytest.raises(InstrumentError):                # the control: with nothing signed the guard really refuses
        bench.mic.set_shutter(True)
    assert refusals == ["shutter"]
    refusals.clear()
    res, opr = run(cls, params, bench=bench)
    assert res.success, res.error
    assert refusals == [], "the protocol asked for the light only after the signature was in"
    assert opr.reasons()[0] == "laser_attest" and opr.at_prompt[0]["acquisitions"] == 0
    assert opr.at_prompt[0]["beam_path"] == "video", "…and before anything was configured"
    assert_safe(bench)


def test_an_expired_signature_is_asked_for_before_the_next_step_starts_its_own_prompts(bench, scope, expiring_store):
    _expire_after(bench, expiring_store, 2)
    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="540,560,1040"), bench=bench)
    assert res.success, res.error
    # step 3 needs another plate, but the signature comes first — and the beam is off for both prompts
    assert opr.reasons() == ["laser_attest", "waveplate", "laser_attest", "waveplate"]
    assert [e for e in opr.emission_at_prompt[2:]] == [("laser_attest", False), ("waveplate", False)]
    assert [r["found"] for r in res.data["rows"]] == [True, True, True]
    assert sidecar_of(res.data["rows"][2]["file"])["optical_state"]["laser_area_safe"]["valid"] is True
    assert_safe(bench)


def test_an_expired_signature_with_the_witec_laser_is_asked_for_before_the_next_angle(bench, scope, expiring_store):
    _expire_after(bench, expiring_store, 2)                              # after theta = 0 par/crs
    res, opr = run(PolarizationMatrix, pol_params(mode="both", incident_angles_deg="0,10"), bench=bench)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "incident_polarizer", "laser_attest", "incident_polarizer"]
    assert res.data["steps_done"] == 4 and all(r["found"] for r in res.data["rows"])
    assert bench.mic.shutter_open is False


@pytest.mark.parametrize("answer,kind", [
    ("declines", "operator_declined"), ("is silent", "operator_timeout"), ("may not be asked", "laser_not_attested")])
def test_an_expired_signature_that_does_not_come_stops_the_run_in_the_safe_direction(answer, kind, bench, scope,
                                                                                    expiring_store):
    calls = {"n": 0}

    def attest(p):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"value": True}
        return {"value": False} if answer == "declines" else None

    params = pol_params(mode="par", incident_angles_deg="0,10,20")
    if answer == "may not be asked":
        optical_state.get_store().set_by_operator("laser_area_safe", True, by="test-operator", source="test")
        params["ask_attest_laser"] = False
    if answer == "is silent":
        params["prompt_timeout_s"] = 0.3
    _expire_after(bench, expiring_store, 1)
    res, opr = run(PolarizationMatrix, params, bench=bench, laser_attest=attest)
    assert not res.success and res.data["stop_kind"] == kind
    assert res.data["steps_done"] == 1 and res.data["stopped_at_step"] == 2
    assert len(res.data["spectrum_files"]) == 2, "step 1 is kept"
    assert summary_of(res)["stop"]["kind"] == kind and summary_of(res)["stop"]["at_step"] == 2
    assert prompts.list_open() == []
    assert_safe(bench)


def test_the_emission_is_never_switched_on_without_a_valid_signature_however_it_ran_out(bench, scope, expiring_store):
    """The signature runs out while the operator is busy with the first plate, and again during a mid-run
    swap: every emission_on that follows must find a fresh one (the source is off while people work)."""
    valid_at_emission_on: list = []
    real_on = bench.tun.emission_on

    def watched_on():
        valid_at_emission_on.append(optical_state.get_store().is_valid(op.SLOT_LASER_SAFE, expect=True))
        return real_on()

    bench.tun.emission_on = watched_on

    def plate_then_expire(p):                               # the operator takes long enough for the signature to run out
        expiring_store.expire()
        return {"choice": "hwp_532" if p.context["excitation_nm"] < 700 else "hwp_1064"}

    res, opr = run(SHGExcitationScan, shg_params(wavelengths_nm="540,1040"), bench=bench, waveplate=plate_then_expire)
    assert res.success, res.error
    assert opr.reasons() == ["laser_attest", "waveplate", "laser_attest", "waveplate", "laser_attest"]
    assert len(valid_at_emission_on) >= 2 and all(valid_at_emission_on), valid_at_emission_on
    assert all(e[1] is False for e in opr.emission_at_prompt), "the source was off at every prompt"
    assert [r["found"] for r in res.data["rows"]] == [True, True]
    assert_safe(bench)


@pytest.mark.parametrize("measure", ["single", "analyzer_series"])
def test_a_signature_that_runs_out_during_a_long_wait_is_renewed_right_before_the_light(measure, make_bench, scope,
                                                                                         expiring_store):
    b = make_bench()
    refusals = enforce_light_attestation(b)
    reads = {"n": 0}
    orig_status = b.tc.status

    def status_then_expire():
        reads["n"] += 1
        if reads["n"] == 2:                                 # the temperature is still settling
            expiring_store.expire()
        return orig_status()

    b.tc.status = status_then_expire
    extra = {"measure": "analyzer_series", "series_points": 3} if measure == "analyzer_series" else {}
    res, opr = run(TemperatureSeries, temp_params(temperatures_c="40", **extra), bench=b)
    assert res.success, res.error
    assert refusals == []
    assert opr.reasons() == ["laser_attest", "laser_attest"]
    second = opr.at_prompt[1]
    assert second["acquisitions"] == 0 and second["series"] == 0, "re-signed before the first exposure / series"
    assert reads["n"] >= 2


# ── through the real ExecutionContext (sample gate, token, validation, abort wiring) ───────────────────


def _real_context(abort_event=None):
    from mast.core.execution_context import ExecutionContext
    from mast.core.registry import SkillRegistry

    reg = SkillRegistry()
    for cls in ALL:
        reg.register(cls)
    return ExecutionContext(pool=None, state=None, registry=reg, abort_event=abort_event, owner="test")


def test_through_the_execution_context_the_sample_gate_and_validation_apply(bench, scope):
    ctx = _real_context()
    ok = ctx.run("SHGExcitationScan", shg_params(dry_run=True))
    assert ok.success, ok.error
    unknown = ctx.run("SHGExcitationScan", {**shg_params(dry_run=True), "bogus": 1})
    assert not unknown.success and "Unknown parameter" in unknown.error
    missing = ctx.run("SHGExcitationScan", {"laser_id": "superk", "wavelengths_nm": "800", "dry_run": True})
    assert not missing.success and "amplitude_pct" in missing.error
    bad_type = ctx.run("SHGExcitationScan", shg_params(dry_run=True, accumulations="many"))
    assert not bad_type.success
    # an experiment without a sample: the gate refuses (data-producing tags) and nothing is touched.
    # (prompt_timeout_s: if the gate ever let this through, nobody answers and the test must fail fast)
    scope.log.current_sample_id = None
    refused = ctx.run("SHGExcitationScan", shg_params(waveplate_slot="none", prompt_timeout_s=0.2))
    assert not refused.success and "sample_gate" in refused.error and "no_active_sample" in refused.error
    assert bench.mic.calls == [] and bench.tun.emitting is False and bench.mic.acquisitions == 0


def test_through_the_execution_context_a_run_holds_the_instrument_token_while_it_waits_for_a_person(bench, scope):
    from mast.core.instrument_lock import instrument_lock

    holders = []

    def attest(p):
        holders.append((instrument_lock().snapshot() or {}).get("skill"))
        return {"value": True}

    with prompts.FakeOperator(Operator(bench, laser_attest=attest), interval_s=0.005):
        res = _real_context().run("SHGExcitationScan", shg_params(wavelengths_nm="700,800", waveplate_slot="none",
                                                                   power_meter_id="pm"))
    assert res.success, res.error
    assert holders == ["SHGExcitationScan"]
    assert not instrument_lock().snapshot(), "the token is released when the protocol ends"


def test_through_the_execution_context_the_abort_event_stops_the_protocol_between_steps(bench, scope):
    stop = threading.Event()
    orig = bench.mic.acquire

    def acquire_then_stop(**kw):
        out = orig(**kw)
        if bench.mic.acquisitions == 1:
            stop.set()                                            # the operator presses stop during step 1
        return out

    bench.mic.acquire = acquire_then_stop
    with prompts.FakeOperator(Operator(bench), interval_s=0.005):
        res = _real_context(stop).run("SHGExcitationScan", shg_params(wavelengths_nm="700,800,900", waveplate_slot="none"))
    assert not res.success and res.data["status"] == "aborted"
    assert res.data["steps_done"] == 1 and res.data["stopped_at_step"] == 2
    assert_safe(bench)


# ── the real witec_com driver (against FakeBucsClient) under the protocols ──────────────────────────────────
#
# SimMicroscope has none of WITec's rules - no write access that is handed back, shutters that belong to the
# selected laser, a gate in front of the beam-path switch - so problems of that kind passed every test above
# (the plain test double is too thin to show them). These run the protocols on witec_com + FakeBucsClient.


@pytest.fixture
def witec_bench(tmp_path, scope):
    from mast.instruments.witec import microscope as wm
    from mast.instruments.witec.fake_bucs import FakeBucsClient

    fake = FakeBucsClient(operator_allows_write=True)              # the operator HAS pressed COM Automation
    old = wm.set_client_factory(lambda entry: fake)

    def make(*, allow=True, hold_write=True):
        cfg = {"devices": [
            {"id": "witec", "type": "witec_com", "enabled": True, "allow_emission_control": allow, "poll_s": 0.01,
             "beam_path_settle_s": 0.0,
             "limits": {"min_exposure_ms": 10, "max_exposure_ms": 600000, "max_total_s": 7200,
                        "min_center_nm": 300, "max_center_nm": 1100},
             "microscope_limits": {"max_laser_power_mw": {"532": 20, "633": 10}}},
            {"id": "superk", "type": "sim_tunable_laser", "write_enabled": True,
             "limits": {"min_nm": 430, "max_nm": 1100, "max_amplitude_pct": 50}},
            {"id": "tc", "type": "sim_temperature", "write_enabled": True, "time_scale": 600.0, "tau_s": 1.0},
        ]}
        p = tmp_path / "optical_instruments.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        reg = InstrumentRegistry(p)
        reset_instrument_registry(reg)
        mic = reg.microscope("witec")
        if hold_write:
            assert mic.request_write_access()
        return SimpleNamespace(fake=fake, reg=reg, mic=mic, tun=reg.laser("superk"), tc=reg.temperature_controller("tc"))

    yield SimpleNamespace(fake=fake, make=make)
    reset_instrument_registry(None)
    wm.set_client_factory(old)


WITEC_POL = {"incident_angles_deg": "0,10", "mode": "par", "integration_s": 0.05, "settle_s": 0,
             "witec_laser": "532", "witec_power_mw": 2}
WITEC_SHG = {"laser_id": "superk", "amplitude_pct": 5, "integration_s": 0.05, "settle_s": 0, "wavelengths_nm": "800",
             "waveplate_slot": "none", "center_fallback": "fail"}


def _shutter_sets(fake):
    from mast.instruments.witec import params as P

    return [v for (kind, path, v) in fake.log if kind == "set" and path == P.LASER_SHUTTER]


def test_witec_com_polarization_matrix_keeps_the_shutter_shut_while_a_person_turns_the_polariser(witec_bench):
    from mast.instruments.witec import params as P

    w = witec_bench.make()
    shut_at_prompt = []

    def turn(p):
        shut_at_prompt.append(witec_bench.fake.peek(P.LASER_SHUTTER))
        return {"value": p.context["requested_deg"]}

    res, opr = run(PolarizationMatrix, WITEC_POL, incident_polarizer=turn)
    assert res.success, res.error
    assert shut_at_prompt == [False, False]
    assert _shutter_sets(witec_bench.fake).count(True) == 2, "open once per exposure, nowhere else"
    assert witec_bench.fake.peek(P.LASER_SHUTTER) is False
    assert not w.mic.has_write_access(), "the grant went back when the job ended"


def test_witec_com_select_laser_closes_the_shutter_of_the_laser_just_selected(witec_bench):
    """WITec's shutter belongs to the selected laser: the 633's was left open by a person while the 532 is selected."""
    from mast.instruments.witec import params as P

    witec_bench.make()
    fake = witec_bench.fake
    fake._select_laser(2)                                          # the 633 …
    fake.human_sets(P.LASER_POWER, 5.0)
    fake.human_sets(P.LASER_SHUTTER, True)                         # … with its shutter open
    fake._select_laser(1)                                          # and back to the 532
    seen = []

    def turn(p):
        seen.append(fake._shutter[2])
        return {"value": p.context["requested_deg"]}

    res, _ = run(PolarizationMatrix, {**WITEC_POL, "witec_laser": "633", "incident_angles_deg": "0"}, incident_polarizer=turn)
    assert res.success, res.error
    assert seen == [False]


def test_witec_com_the_shutter_of_the_laser_just_selected_is_shut_before_its_power_is_set(witec_bench):
    from mast.instruments.witec import params as P

    witec_bench.make()
    fake = witec_bench.fake
    fake._select_laser(2)                                          # the 633, with its shutter left open by a person
    fake.human_sets(P.LASER_POWER, 5.0)
    fake.human_sets(P.LASER_SHUTTER, True)
    fake._select_laser(1)
    at_power = []
    point = fake._tree[P.LASER_POWER]
    orig = point.on_set

    def spy(value):
        at_power.append(fake._shutter[fake._selected])
        orig(value)

    point.on_set = spy
    res, _ = run(PowerSeries, {"witec_laser": "633", "powers": "1,2", "integration_s": 0.05, "settle_s": 0,
                               "center_nm": 664, "feature_nm": 664.5})
    assert res.success, res.error
    assert at_power and not any(at_power), "every power the protocol set was set behind a shut shutter"


def test_witec_com_a_shutter_that_will_not_close_after_the_exposure_ends_the_run(witec_bench):
    from mast.instruments.witec import params as P
    from mast.instruments.witec.bucs import BucsError

    w = witec_bench.make()
    fake = witec_bench.fake
    orig = w.mic.acquire
    shut_at_prompt = []

    def acquire(**kw):
        out = orig(**kw)
        fake.inject(P.LASER_SHUTTER, BucsError("WITec Control did not answer (simulated)"), ops=("set",), times=1)
        return out

    w.mic.acquire = acquire

    def turn(p):
        shut_at_prompt.append(fake.peek(P.LASER_SHUTTER))
        return {"value": p.context["requested_deg"]}

    res, opr = run(PolarizationMatrix, {**WITEC_POL, "incident_angles_deg": "0,10,20"}, incident_polarizer=turn)
    d = res.data
    assert not res.success and d["stop_kind"] == "shutter_not_closed" and d["steps_done"] == 1
    assert shut_at_prompt == [False], "no second prompt"
    assert P.STOP_SEQUENCER in fake.triggers(), "stop_all was sent"
    assert fake.peek(P.LASER_SHUTTER) is False
    assert len(d["spectrum_files"]) == 2


def test_witec_com_every_protocol_runs_with_the_shutter_open_only_during_the_exposures(witec_bench):
    from mast.instruments.witec import params as P

    w = witec_bench.make()
    fake = witec_bench.fake
    res, _ = run(PowerSeries, {"witec_laser": "532", "powers": "1,2,4", "integration_s": 0.05, "settle_s": 0,
                               "center_nm": 547, "feature_nm": 547.2})
    assert res.success, res.error
    assert res.data["steps_done"] == 3 and _shutter_sets(fake).count(True) == 3
    assert fake.peek(P.LASER_SHUTTER) is False
    res, _ = run(TemperatureSeries, {"temperatures_c": "30", "ramp_c_per_min": 20, "hold_s": 0.05, "poll_s": 0.01,
                                     "timeout_s": 20, "integration_s": 0.05, "settle_s": 0, "witec_laser": "532"})
    assert res.success, res.error
    assert res.data["steps_done"] == 1 and fake.peek(P.LASER_SHUTTER) is False
    res, _ = run(SHGExcitationScan, WITEC_SHG)
    assert res.success, res.error
    assert res.data["steps_done"] == 1 and fake.peek(P.LASER_SHUTTER) is False
    assert witec_bench.fake.peek(P.LASER_SHUTTER) is False and w.tun.emitting is False


# The close direction takes the write
# access back by itself, and the beam-path switch does not demand allow_emission_control while the shutter is shut.


def test_witec_com_a_protocol_started_after_the_previous_one_gave_the_write_access_back_runs(witec_bench):
    witec_bench.make()
    res, _ = run(SHGExcitationScan, WITEC_SHG)
    assert res.success, (res.data.get("stop_kind"), res.error)
    res, opr = run(SHGExcitationScan, WITEC_SHG)                   # it handed the grant back; the second one's first close must not need it
    assert res.success, (res.data.get("stop_kind"), res.error)
    assert "write_access" not in opr.reasons()


def test_witec_com_a_scan_that_only_uses_the_external_source_does_not_need_the_witec_laser_gate(witec_bench):
    witec_bench.make(allow=False)                                  # the WITec's own laser is never used here
    res, _ = run(SHGExcitationScan, WITEC_SHG)
    assert res.success, (res.data.get("stop_kind"), res.error)
    assert res.data["steps_done"] == 1


# ── a stop somebody else asks for while the protocol runs ──────────────────────────────────
#
# StopWITec, CloseWITecShutter, LaserEmissionOff, StopTemperatureRamp, the withdrawal of laser_area_safe and the
# E-STOP act once; the next step used to undo them (open the shutter again, switch the source back on, send the next
# setpoint). Every station device counts them (note_external_stop / external_stop_epoch); the run records the
# epochs when it begins and ends cleanly as soon as one changes. The stop skills call note_external_stop before
# they act - here a stop is simulated the same way: the device is told, then it is stopped.


def _count_calls(obj, name):
    """Wrap ``obj.<name>`` and return the list of argument tuples it is called with."""
    calls = []
    real = getattr(obj, name)

    def spy(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    setattr(obj, name, spy)
    return calls


def _after_exposure(bench, fn, *, n=1):
    """Run ``fn()`` right after the n-th exposure is in hand (still inside the acquire call)."""
    orig = bench.mic.acquire

    def acquire(**kw):
        out = orig(**kw)
        if bench.mic.acquisitions == n:
            fn()
        return out

    bench.mic.acquire = acquire


def test_a_shutter_closed_from_outside_ends_the_run_and_the_next_exposure_does_not_reopen_it(bench, scope):
    shutter_calls = _count_calls(bench.mic, "set_shutter")

    def close_from_outside():                                      # CloseWITecShutter by another agent
        bench.mic.note_external_stop("CloseWITecShutter by ext:alice")
        bench.mic.set_shutter(False)

    _after_exposure(bench, close_from_outside)
    res, opr = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10,20"), bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["status"] == "stopped"
    assert d["steps_done"] == 1 and d["stopped_at_step"] == 2 and d["stop_phase"] == "step"
    assert "CloseWITecShutter by ext:alice" in d["stop_reason"] and "microscope" in d["stop_reason"]
    assert shutter_calls.count((True,)) == 1, "the shutter was opened once: for step 1, never again"
    assert opr.reasons().count("incident_polarizer") == 1, "…and nobody is asked to turn the polariser for step 2"
    assert bench.mic.shutter_open is False and bench.mic.external_stop_epoch == 1
    assert d["cleanup"]["shutter"] == "closed"
    side = sidecar_of(d["spectrum_files"][0])
    assert "CloseWITecShutter by ext:alice" in side["external_stop_during_exposure"], "the spectrum a stop arrived in is flagged"
    assert summary_of(res)["stop"]["kind"] == "stopped_externally"


def test_a_stop_noted_while_a_person_turns_the_polariser_stops_the_run_before_the_next_exposure(bench, scope):
    shutter_calls = _count_calls(bench.mic, "set_shutter")

    def turn(p):
        if p.context["requested_deg"] == 10.0:                     # StopWITec while the second angle is being set
            bench.mic.note_external_stop("StopWITec by ext:bob")
            bench.mic.stop_all()
        return {"value": p.context["requested_deg"]}

    res, opr = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10,20"), bench=bench,
                   incident_polarizer=turn)
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and "StopWITec by ext:bob" in d["stop_reason"]
    assert d["steps_done"] == 1 and shutter_calls.count((True,)) == 1
    assert opr.reasons().count("incident_polarizer") == 2
    assert_safe(bench)


def test_a_stop_asked_for_during_the_last_step_ends_the_run_after_it(bench, scope):
    """No next step to stop before: the measured step is stored and the run is reported as stopped, not completed."""
    _after_exposure(bench, lambda: bench.mic.note_external_stop("StopWITec by ext:bob"))
    res, _ = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0"), bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["status"] == "stopped"
    assert d["steps_done"] == 1 and d["steps_planned"] == 1 and d["stopped_at_step"] is None
    assert "stopped after the last step" in res.summary and len(d["spectrum_files"]) == 2
    assert_safe(bench)


def test_a_stop_asked_for_while_the_operator_is_being_waited_for_cancels_the_wait(bench, scope):
    def never_answers(p):
        threading.Timer(0.3, lambda: bench.mic.note_external_stop("StopWITec by ext:bob")).start()
        return None

    res, opr = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10"), bench=bench,
                   incident_polarizer=never_answers)
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["steps_done"] == 0
    assert d["elapsed_s"] < 20, "the 30-minute wait for the operator was cut short"
    assert prompts.list_open() == [], "…and the prompt is not left open"
    assert_safe(bench)


@pytest.mark.parametrize("cls,params,opens", [
    (SHGExcitationScan, shg_params(wavelengths_nm="700,800,900", waveplate_slot="none"), 0),      # an external source
    (PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10,20"), 1),               # the WITec laser's shutter
])
def test_safe_off_all_while_the_protocol_runs_stops_it_and_nothing_is_switched_back_on(cls, params, opens, bench, scope):
    """The operator withdraws laser_area_safe (or the E-STOP fires): emission_safety.safe_off_all runs."""
    from mast.instruments import emission_safety

    on_calls = _count_calls(bench.tun, "emission_on")
    shutter_calls = _count_calls(bench.mic, "set_shutter")

    def withdraw():
        emission_safety.safe_off_all("laser_area_safe recorded as False by ana", registry=bench.reg)

    _after_exposure(bench, withdraw)
    res, opr = run(cls, params, bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["status"] == "stopped"
    assert "laser_area_safe recorded as False by ana" in d["stop_reason"]
    assert d["steps_done"] == 1 and d["stopped_at_step"] == 2
    assert len(on_calls) == (1 if cls is SHGExcitationScan else 0), "the source is never switched on again"
    assert shutter_calls.count((True,)) == opens, "…nor the shutter reopened"
    assert_safe(bench)
    assert bench.tun.external_stop_epoch == 1 and bench.mic.external_stop_epoch == 1


def test_a_stop_asked_for_before_the_run_does_not_stop_it(bench, scope):
    bench.mic.note_external_stop("an old StopWITec")
    bench.tc.note_external_stop("an old StopTemperatureRamp")
    res, _ = run(PolarizationMatrix, pol_params(mode="par"), bench=bench)
    assert res.success, res.error
    assert bench.mic.external_stop_epoch == 1 and bench.tc.external_stop_epoch == 1, "the run added none"


def test_the_runs_own_closing_and_cleanup_never_count_as_a_stop_from_outside(make_bench, scope, monkeypatch):
    b = make_bench()
    watched = (b.mic, b.tun, b.npi, b.tc)
    # a normal finish with plate swaps, a normal finish with a final setpoint, an operator abort and a shutter fault:
    # all the run's own emission_off / set_shutter(False) / stop_all / tc.stop() / emission_on calls
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="540,1040"), bench=b)
    assert res.success, res.error
    res, _ = run(TemperatureSeries, temp_params(temperatures_c="30,40", final_setpoint_c=25), bench=b)
    assert res.success, res.error
    n0 = b.mic.acquisitions
    res, _ = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10"), bench=b,
                 ctx=Ctx(abort=lambda: b.mic.acquisitions >= n0 + 1))
    assert res.data["stop_kind"] == "aborted"
    _flaky_close(b, monkeypatch)
    res, _ = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10"), bench=b)
    assert res.data["stop_kind"] == "shutter_not_closed"
    assert [d.external_stop_epoch for d in watched] == [0, 0, 0, 0]


def test_a_second_run_after_a_stop_from_outside_starts_from_the_new_count(bench, scope):
    plain = bench.mic.acquire
    _after_exposure(bench, lambda: bench.mic.note_external_stop("StopWITec by ext:bob"))
    res, _ = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10"), bench=bench)
    assert res.data["stop_kind"] == "stopped_externally" and bench.mic.external_stop_epoch == 1
    bench.mic.acquire = plain                                      # the hook is taken off again
    res, _ = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10"), bench=bench)
    assert res.success, res.error                                  # the epoch it began with is 1, and stays 1
    assert bench.mic.external_stop_epoch == 1


def test_a_temperature_stop_from_outside_is_enough_when_the_controller_state_cannot_be_read(make_bench, scope):
    """A TC100 without a status register: the read-back never says 'stopped'. The stop is still counted."""
    b = make_bench()
    real_status = b.tc.status
    b.tc.status = lambda: {k: v for k, v in real_status().items() if k != "state"}
    sent = _spy_setpoints(b.tc)

    def stop_ramp():                                               # StopTemperatureRamp by another agent
        b.tc.note_external_stop("StopTemperatureRamp by ext:alice")
        b.tc.stop()

    _after_exposure(b, stop_ramp)
    res, _ = run(TemperatureSeries, temp_params(temperatures_c="30,40,50", final_setpoint_c=25), bench=b)
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["status"] == "stopped"
    assert d["steps_done"] == 1 and d["stopped_at_step"] == 2
    assert sent == [30.0], "no setpoint after the stop - the final one included"
    assert "StopTemperatureRamp by ext:alice" in d["stop_reason"] and "temperature controller" in d["stop_reason"]
    assert d["cleanup"]["temperature"] == {"action": "stop"}
    assert b.tc.external_stop_epoch == 1


def test_a_temperature_stop_from_outside_while_the_point_settles_sends_nothing_more(make_bench, scope):
    b = make_bench(tc={"time_scale": 1.0, "tau_s": 100000.0})        # never settles on its own
    real_status = b.tc.status
    b.tc.status = lambda: {k: v for k, v in real_status().items() if k != "state"}
    sent = []
    real = b.tc.set_setpoint

    def spy(c, **kw):
        sent.append(c)
        out = real(c, **kw)
        threading.Timer(0.15, lambda: b.tc.note_external_stop("StopTemperatureRamp by ext:alice")).start()
        return out

    b.tc.set_setpoint = spy
    res, _ = run(TemperatureSeries, temp_params(temperatures_c="60,70", timeout_s=30, final_setpoint_c=25), bench=b)
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["steps_done"] == 0
    assert sent == [60.0] and d["cleanup"]["temperature"] == {"action": "stop"}


def test_the_final_setpoint_is_not_sent_after_a_stop_from_outside_even_when_the_run_finished(bench, scope):
    """The protocol's last check is behind it; cleanup looks once more before it would start the stage again."""
    proto = TemperatureSeries()
    params = temp_params(temperatures_c="30", final_setpoint_c=25, prompt_timeout_s=0.05)
    cfg = proto._parse(params)
    rig = op._resolve_rig(cfg, **proto._rig_needs(cfg))
    r = op._Run(proto, Ctx(), params, cfg, rig, proto._plan(cfg, rig))
    sent = _spy_setpoints(rig.tc)
    r.watch_stops()
    r.commands_temperature = True
    r.status = "completed"                                         # a normal finish …
    rig.tc.note_external_stop("StopTemperatureRamp by ext:alice")  # … and a stop right after the last check
    rep = r.cleanup()
    assert sent == [] and rep["temperature"]["action"] == "stop"
    assert "from outside the run" in rep["temperature"]["note"]
    assert any("from outside the run" in w for w in r.warnings)


def test_the_run_watches_every_device_it_uses_and_no_other(bench, scope):
    proto = SHGExcitationScan()
    params = shg_params(wavelengths_nm="800", waveplate_slot="none", power_meter_id="pm", temperature_id="tc",
                        prompt_timeout_s=0.05)
    cfg = proto._parse(params)
    rig = op._resolve_rig(cfg, **proto._rig_needs(cfg))
    r = op._Run(proto, Ctx(), params, cfg, rig, proto._plan(cfg, rig))
    r.watch_stops()
    assert sorted(r.stop_watch) == ["light source", "microscope", "temperature controller"]
    assert r.external_stop() == ""
    bench.npi.note_external_stop("a source this run does not use")
    assert r.external_stop() == ""
    bench.tun.note_external_stop("LaserEmissionOff by ext:alice")
    assert "light source" in r.external_stop() and "LaserEmissionOff by ext:alice" in r.external_stop()
    with pytest.raises(op.ProtocolHalt) as ei:
        r.check()
    assert ei.value.kind == "stopped_externally"
    assert r.hw("a post-exposure read", lambda: 7, check=False) == 7, "a measured step is still finished"


def test_a_device_whose_epoch_cannot_be_read_counts_as_zero(bench, scope):
    class Odd:
        @property
        def external_stop_epoch(self):
            raise RuntimeError("no such thing")

    proto = PolarizationMatrix()
    params = pol_params(mode="par", incident_angles_deg="0", prompt_timeout_s=0.05)
    cfg = proto._parse(params)
    rig = op._resolve_rig(cfg, **proto._rig_needs(cfg))
    plan = proto._plan(cfg, rig)
    rig.mic = Odd()
    r = op._Run(proto, Ctx(), params, cfg, rig, plan)
    r.watch_stops()
    assert r.stop_watch["microscope"][1] == 0 and r.external_stop() == ""


# ── every shutter, not only the selected laser's, before hands go into the beam path ──────────────


def _closing_report(**kw):
    return {"closed": ["532 nm"], "already_closed": ["633 nm"], "no_software_shutter": [], "not_present": [], "failed": {},
            "skipped": "", "selection_restored": True, "selected_without_shutter": "", **kw}


def test_a_swap_closes_every_shutter_the_driver_can_reach_and_the_prompt_says_what_was_read_back(bench, scope):
    calls = []
    bench.mic.close_all_shutters = lambda: calls.append("all") or _closing_report(no_software_shutter=["355 nm"])
    seen = []

    def turn(p):
        seen.append(p.message)
        return {"value": p.context["requested_deg"]}

    res, _ = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10"), bench=bench, incident_polarizer=turn)
    assert res.success, res.error
    assert calls == ["all", "all"], "once per hand rotation"
    msg = seen[0]
    assert "Every WITec laser shutter is closed (confirmed by read-back: 532 nm, 633 nm)." in msg
    assert "Lasers with no software shutter (block the beam physically if one of them is in use): 355 nm." in msg
    assert "not checked" not in msg and "Only the selected laser" not in msg
    assert "The WITec shutter is closed (confirmed by read-back)." not in msg


# A report that comes back (nothing raised) but says it could not look at every laser is not a success: the witec
# driver reports shutter_confirmed and skipped / scope, and the registry stopped counting such a report
# as confirmed. A person is not sent into a beam path that may still be live.


@pytest.mark.parametrize("report", [
    {"skipped": "other lasers not visited: a sequencer is running (swapping lasers would disturb it)", "shutter_confirmed": False},
    {"skipped": "other lasers not visited: the sequencer state could not be read (link hiccup)", "shutter_confirmed": False},
    {"shutter_confirmed": False},
    {"skipped": "other lasers not visited: a sequencer is running"},
], ids=["sequencer-running", "sequencer-unreadable", "driver-says-unconfirmed", "skipped-without-the-flag"])
def test_a_swap_is_not_started_on_a_report_that_does_not_confirm_every_shutter(bench, scope, report):
    bench.mic.close_all_shutters = lambda: _closing_report(**report)
    res, opr = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10"), bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "shutter_not_closed" and d["steps_done"] == 0
    assert "incident_polarizer" not in opr.reasons(), "no person is asked to reach in"
    assert ("stop_all", None) in bench.mic.calls
    assert (report.get("skipped") or "shutter_confirmed=false") in d["stop_reason"]
    assert_safe(bench)


def test_the_plate_swap_is_not_started_on_such_a_report_either(bench, scope):
    bench.mic.close_all_shutters = lambda: _closing_report(skipped="other lasers not visited: a sequencer is running",
                                                          shutter_confirmed=False)
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="540,1040"), bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "shutter_not_closed" and d["steps_done"] == 0
    assert d["stop_phase"] == "prepare", "the first plate is put in before the first step"
    assert "a sequencer is running" in d["stop_reason"]
    assert_safe(bench)


def test_a_swap_on_a_selected_laser_only_report_says_so_and_does_not_claim_every_shutter(bench, scope):
    bench.mic.close_all_shutters = lambda: _closing_report(scope="selected_laser_only", closed=["532 nm"], already_closed=[],
                                                          shutter_confirmed=True)
    seen = []

    def turn(p):
        seen.append(p.message)
        return {"value": p.context["requested_deg"]}

    res, _ = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0"), bench=bench, incident_polarizer=turn)
    assert res.success, res.error
    assert "Only the selected laser's shutter is closed (confirmed by read-back: 532 nm)" in seen[0]
    assert "close_every_laser is off" in seen[0] and "Every WITec laser shutter" not in seen[0]


def test_the_swap_sentence_never_says_every_shutter_when_the_report_says_otherwise():
    for rep_ in ({"closed": ["532 nm"], "skipped": "a sequencer is running"},
                 {"closed": ["532 nm"], "shutter_confirmed": False},
                 {"closed": ["532 nm"], "scope": "selected_laser_only"}):
        sw = op._Swap()
        sw.shutter, sw.shutters = "all", rep_
        zh, en = sw.describe()
        assert "Every WITec laser shutter" not in en and "每台激光的快门都已关" not in zh, rep_
    sw = op._Swap()
    sw.shutter, sw.shutters = "all", {"closed": ["532 nm"], "scope": "all_lasers", "shutter_confirmed": True}
    assert "Every WITec laser shutter is closed (confirmed by read-back: 532 nm)." in sw.describe()[1]


def test_a_driver_that_cannot_close_every_shutter_gets_the_one_it_has_closed_and_says_so(bench, scope):
    assert not hasattr(bench.mic, "close_all_shutters")
    seen = []
    res, _ = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0"), bench=bench,
                 incident_polarizer=lambda p: (seen.append(p.message), {"value": 0.0})[1])
    assert res.success, res.error
    assert "The WITec shutter is closed (confirmed by read-back)." in seen[0] and "Every WITec laser shutter" not in seen[0]


def test_a_swap_is_not_started_when_not_every_shutter_could_be_closed(bench, scope):
    def refuse():
        raise InstrumentError("the shutter was NOT closed (633 nm: WITec Control refused the write)")

    bench.mic.close_all_shutters = refuse
    res, opr = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10"), bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "shutter_not_closed" and d["steps_done"] == 0
    assert "incident_polarizer" not in opr.reasons(), "no person is asked to reach in"
    assert ("stop_all", None) in bench.mic.calls
    assert "633 nm: WITec Control refused the write" in d["stop_reason"]
    assert_safe(bench)


def test_a_laser_without_a_software_shutter_stops_the_swap_with_the_drivers_words(bench, scope):
    def no_shutter():
        raise OperationNotSupported("the selected laser (1064 nm) has no software shutter - block the beam physically "
                                    "(the shutters of the other lasers were closed)")

    bench.mic.close_all_shutters = no_shutter
    res, opr = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0"), bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "not_supported" and "block the beam physically" in d["stop_reason"]
    assert "incident_polarizer" not in opr.reasons()
    assert_safe(bench)


def test_the_plate_swap_closes_every_shutter_too(bench, scope):
    calls = []
    bench.mic.close_all_shutters = lambda: calls.append("all") or _closing_report()
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="540,1040"), bench=bench)
    assert res.success, res.error
    assert calls == ["all", "all"]


# ── the same on witec_com + FakeBucsClient ──────────────────────────────────────────────────────────────────────


def test_witec_com_a_stop_from_outside_mid_run_stops_the_next_step_and_nothing_is_reopened(witec_bench):
    from mast.instruments.witec import params as P

    w = witec_bench.make()
    fake = witec_bench.fake
    orig = w.mic.acquire

    def acquire(**kw):
        out = orig(**kw)
        w.mic.note_external_stop("StopWITec by ext:bob")            # what the StopWITec skill does first …
        w.mic.stop_all()                                            # … and then it stops (shutter, sequencer)
        return out

    w.mic.acquire = acquire
    res, opr = run(PolarizationMatrix, {**WITEC_POL, "incident_angles_deg": "0,10,20"})
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["steps_done"] == 1
    assert "StopWITec by ext:bob" in d["stop_reason"]
    assert _shutter_sets(fake).count(True) == 1, "the shutter was opened for step 1 and never again"
    assert fake.peek(P.LASER_SHUTTER) is False
    assert opr.reasons().count("incident_polarizer") == 1
    assert w.mic.external_stop_epoch == 1, "the run's own closing and cleanup counted nothing"


def test_witec_com_a_swap_closes_the_shutters_of_every_laser(witec_bench):
    from mast.instruments.witec import params as P

    witec_bench.make()
    fake = witec_bench.fake
    fake._select_laser(2)                                          # the 633, with its shutter left open by a person
    fake.human_sets(P.LASER_POWER, 5.0)
    fake.human_sets(P.LASER_SHUTTER, True)
    fake._select_laser(1)                                          # and the 532 selected: the protocol's laser
    seen = []

    def turn(p):
        seen.append((list(fake._shutter), fake._selected, p.message))
        return {"value": p.context["requested_deg"]}

    res, _ = run(PolarizationMatrix, {**WITEC_POL, "incident_angles_deg": "0"}, incident_polarizer=turn)
    assert res.success, res.error
    shutters, selected, msg = seen[0]
    assert not any(shutters), "every laser's shutter is shut when the person reaches in, not only the 532's"
    assert selected == 1, "…and the selection is back on the 532"
    assert "Every WITec laser shutter is closed (confirmed by read-back: " in msg and "633 nm" in msg
    assert "Lasers with no software shutter" in msg and "355 nm" in msg, "what the driver could not shut is said too"


def test_witec_com_a_swap_with_the_sequencer_state_unreadable_is_not_started(witec_bench):
    from mast.instruments.witec import params as P
    from mast.instruments.witec.bucs import BucsConnectionLost

    w = witec_bench.make()
    fake = witec_bench.fake
    real = w.mic.close_all_shutters

    def close_all():
        fake.inject(P.IS_SEQUENCER_ACTIVE, BucsConnectionLost("link hiccup (test)"), ops=("get",), times=1)
        return real()                                              # the driver closes the selected laser's shutter, visits no other

    w.mic.close_all_shutters = close_all
    res, opr = run(PolarizationMatrix, {**WITEC_POL, "incident_angles_deg": "0,10"})
    d = res.data
    assert not res.success and d["stop_kind"] == "shutter_not_closed" and d["steps_done"] == 0
    assert "could not be read" in d["stop_reason"]
    assert "incident_polarizer" not in opr.reasons(), "no person is asked to reach in"
    assert fake.peek(P.LASER_SHUTTER) is False


def test_witec_com_a_swap_with_close_every_laser_off_says_only_the_selected_shutter_was_closed(witec_bench):
    w = witec_bench.make()
    w.mic.cfg = dataclasses.replace(w.mic.cfg, close_every_laser=False)
    seen = []

    def turn(p):
        seen.append(p.message)
        return {"value": p.context["requested_deg"]}

    res, _ = run(PolarizationMatrix, {**WITEC_POL, "incident_angles_deg": "0"}, incident_polarizer=turn)
    assert res.success, res.error
    assert "Only the selected laser's shutter is closed" in seen[0] and "Every WITec laser shutter" not in seen[0]


# ── the real stop skills, called mid-run: skill -> note_external_stop -> the protocol ends cleanly ─────────────
#
# The external-stop tests above simulate what a stop skill does first (note the stop, then act). These call the skills
# themselves, so a skill that stops counting - or a device that stops counting for it - fails here.


def test_the_real_laser_emission_off_skill_ends_a_running_scan_and_nothing_switches_the_source_back_on(bench, scope):
    from mast.skills.builtins.optics_sources import LaserEmissionOff

    on_calls = _count_calls(bench.tun, "emission_on")
    _after_exposure(bench, lambda: LaserEmissionOff().execute(SimpleNamespace(owner="ext:carol"),
                                                             {"device_id": bench.tun.device_id}))
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="800,810,820", waveplate_slot="none"), bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["status"] == "stopped"
    assert "LaserEmissionOff (ext:carol)" in d["stop_reason"]
    assert d["steps_done"] == 1 and len(on_calls) == 1, "the source is never switched on again"
    assert_safe(bench)


def test_the_real_stop_temperature_ramp_skill_ends_a_running_series_and_no_setpoint_follows(bench, scope):
    from mast.skills.builtins.optics_sources import StopTemperatureRamp

    sent = _spy_setpoints(bench.tc)
    _after_exposure(bench, lambda: StopTemperatureRamp().execute(SimpleNamespace(owner="ext:carol"),
                                                                {"device_id": bench.tc.device_id}))
    res, _ = run(TemperatureSeries, temp_params(temperatures_c="30,40,50", final_setpoint_c=25), bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["status"] == "stopped"
    assert "StopTemperatureRamp (ext:carol)" in d["stop_reason"]
    assert d["steps_done"] == 1 and sent == [30.0], "no second point and no final setpoint"
    assert bench.tc.status()["state"] == "stopped"


@pytest.mark.parametrize("skill_name", ["StopWITec", "CloseWITecShutter", "ReleaseWITecWriteAccess"])
def test_witec_com_the_real_stop_skills_end_a_running_protocol_and_the_shutter_is_not_reopened(witec_bench, skill_name):
    from mast.instruments.witec import params as P
    from mast.skills.builtins import optics_witec

    w = witec_bench.make()
    fake = witec_bench.fake
    skill = getattr(optics_witec, skill_name)
    orig = w.mic.acquire
    n = {"k": 0}

    def acquire(**kw):
        out = orig(**kw)
        n["k"] += 1
        if n["k"] == 1:
            skill().execute(SimpleNamespace(owner="ext:bob"), {})
        return out

    w.mic.acquire = acquire
    res, opr = run(PolarizationMatrix, {**WITEC_POL, "incident_angles_deg": "0,10,20"})
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["steps_done"] == 1
    assert f"{skill_name} (ext:bob)" in d["stop_reason"]
    assert _shutter_sets(fake).count(True) == 1, "opened for step 1 and never again"
    assert fake.peek(P.LASER_SHUTTER) is False


# ── StopInProgress (a stop is visiting the lasers) is somebody else's stop; ShutterNotClosed is not a missing grant ──
#
# The WITec driver refuses an open / a laser selection / an exposure while a stop is under way (StopInProgress, a
# MotionAborted) and raises ShutterNotClosed (a WriteAccessDenied!) when a shutter could not be confirmed closed. The
# first is a clean stop of the same kind as the epochs show - not "aborted", not a device error. The second must never
# be answered with "please press COM Automation": the light may be on.


def test_a_stop_under_way_is_somebody_elses_stop_a_plain_abort_stays_an_abort():
    from mast.instruments.base import MotionAborted
    from mast.instruments.witec.microscope import ShutterNotClosed, StopInProgress

    halt = op._classify(StopInProgress("witec: a stop / close-all is under way - 'open the WITec shutter' was not sent"),
                        "open the WITec shutter")
    assert halt.kind == "stopped_externally" and halt.kind in op._CLEAN_STOPS
    assert "under way" in halt.message and "open the WITec shutter" in halt.message
    with use_lang("zh"):
        assert op._classify(StopInProgress("witec: x"), "做某事").kind == "stopped_externally"
    assert op._classify(MotionAborted("aborted by the operator"), "x").kind == "aborted"
    assert op._classify(ShutterNotClosed("witec: the shutter was NOT closed (633 nm)"), "x").kind == "shutter_not_closed"


def test_a_shutter_open_refused_because_a_stop_is_under_way_ends_the_run_as_somebody_elses_stop(bench, scope):
    from mast.instruments.witec.microscope import StopInProgress

    real = bench.mic.set_shutter
    opens = []

    def set_shutter(open_):
        if open_:
            opens.append(1)
            if len(opens) == 2:                                    # the stop starts just before the second step's open
                raise StopInProgress("witec: a stop / close-all is under way ('open the WITec shutter' was not sent)")
        return real(open_)

    bench.mic.set_shutter = set_shutter
    res, opr = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10,20"), bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["status"] == "stopped"
    assert d["steps_done"] == 1 and len(opens) == 2, "the shutter is not tried a third time"
    assert "a stop / close-all is under way" in d["stop_reason"]
    assert_safe(bench)


def test_witec_com_a_stop_that_arrives_while_a_laser_settles_ends_the_run_as_somebody_elses_stop(witec_bench):
    from mast.instruments.witec import params as P

    w = witec_bench.make()
    real_sleep, real_select = w.mic._sleep, w.mic.select_laser
    fired, inside = [], []

    def select_laser(*a, **k):
        inside.append(True)
        try:
            return real_select(*a, **k)
        finally:
            inside.pop()

    def sleep(seconds):
        if inside and not fired:
            fired.append(True)
            w.mic.stop_all()                                       # a stop from outside, while the laser is being selected
        real_sleep(seconds)

    w.mic.select_laser, w.mic._sleep = select_laser, sleep
    res, opr = run(PolarizationMatrix, {**WITEC_POL, "incident_angles_deg": "0,10"})
    d = res.data
    assert fired, "the hook ran"
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["status"] == "stopped", (d["stop_kind"], d["stop_reason"])
    assert d["steps_done"] == 0 and "under way" in d["stop_reason"]
    assert "write_access" not in opr.reasons()
    assert witec_bench.fake.peek(P.LASER_SHUTTER) is False


def test_a_shutter_that_could_not_be_confirmed_closed_stops_the_run_and_nobody_is_asked_to_press_com_automation(bench, scope):
    from mast.instruments.witec.microscope import ShutterNotClosed

    def select(*a, **k):
        raise ShutterNotClosed("witec: the shutter was NOT closed (633 nm: WITec Control refused the write)",
                               report={"failed": {"633 nm": "refused"}, "shutter_confirmed": False})

    bench.mic.select_laser = select
    res, opr = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10"), bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "shutter_not_closed" and d["steps_done"] == 0
    assert "write_access" not in opr.reasons(), "not a missing grant: nobody is asked to press COM Automation"
    assert ("stop_all", None) in bench.mic.calls
    assert "633 nm: WITec Control refused the write" in d["stop_reason"]
    assert_safe(bench)


def test_a_shutter_that_could_not_be_confirmed_closed_during_an_exposure_stops_the_run_too(bench, scope):
    from mast.instruments.witec.microscope import ShutterNotClosed

    real = bench.mic.acquire
    n = {"k": 0}

    def acquire(**kw):
        n["k"] += 1
        if n["k"] == 2:                                            # the emission check said no, and the shutter would not shut
            raise ShutterNotClosed("witec: the attestation was withdrawn - AND the shutter could not be closed again")
        return real(**kw)

    bench.mic.acquire = acquire
    res, opr = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10,20"), bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "shutter_not_closed" and d["steps_done"] == 1
    assert n["k"] == 2, "the exposure is not tried again"
    assert "write_access" not in opr.reasons()
    assert ("stop_all", None) in bench.mic.calls
    assert_safe(bench)


def test_witec_com_a_close_that_cannot_be_confirmed_at_the_start_stops_the_run_and_does_not_ask_for_com_automation(witec_bench):
    from mast.instruments.witec import params as P
    from mast.instruments.witec.bucs import BucsError

    witec_bench.make()
    fake = witec_bench.fake
    fake.inject(P.LASER_SHUTTER, BucsError("WITec Control did not answer (simulated)"), ops=("set",), times=1)
    res, opr = run(PolarizationMatrix, {**WITEC_POL, "incident_angles_deg": "0,10"})
    d = res.data
    assert not res.success and d["stop_kind"] == "shutter_not_closed" and d["steps_done"] == 0, (d["stop_kind"], d["stop_reason"])
    assert "write_access" not in opr.reasons(), "the old answer was a COM Automation prompt and a retry"
    assert P.STOP_SEQUENCER in fake.triggers(), "stop_all was sent"
    assert fake.peek(P.LASER_SHUTTER) is False


# ── a device call cut by a stop: what the run knows (the counter, the abort) decides the label ──────────────────
#
# The stop skills count the stop BEFORE they stop (note, then act), so by the time a device raises because it is being
# stopped - MotionAborted, StopInProgress, the WITec acquisition that finds its sequencer idle - the counter has moved.
# A plain MotionAborted with no counted stop is still an abort, and an abort that was asked for outranks (as in check()).


def _bare_run(abort=lambda: False):
    proto = SHGExcitationScan()
    params = shg_params(wavelengths_nm="800", waveplate_slot="none", prompt_timeout_s=0.05)
    cfg = proto._parse(params)
    rig = op._resolve_rig(cfg, **proto._rig_needs(cfg))
    r = op._Run(proto, Ctx(abort=abort), params, cfg, rig, proto._plan(cfg, rig))
    r.watch_stops()
    return r


def test_a_device_call_cut_by_a_stop_is_labelled_by_the_counter_and_the_abort(bench, scope):
    from mast.instruments.base import MotionAborted
    from mast.instruments.microscope import MicroscopeLimitError
    from mast.instruments.witec.microscope import StopInProgress

    flag = {"on": False}
    quiet, counted, both = _bare_run(), _bare_run(), _bare_run(abort=lambda: flag["on"])
    cut = MotionAborted("x: acquisition stopped")
    idle = InstrumentError("witec: the sequencer is idle but delivered no spectrum (it was stopped)")
    # nobody stopped anything: a plain MotionAborted is an abort, a device error is one, StopInProgress is a stop by its type
    assert quiet.halt_for(cut, "acquire").kind == "aborted"
    assert quiet.halt_for(idle, "acquire").kind == "device_error"
    assert quiet.halt_for(StopInProgress("witec: a stop is under way"), "open").kind == "stopped_externally"

    bench.mic.note_external_stop("StopWITec (ext:bob)")
    for exc in (cut, idle, StopInProgress("witec: a stop is under way")):
        h = counted.halt_for(exc, "acquire a spectrum")
        assert h.kind == "stopped_externally" and h.kind in op._CLEAN_STOPS, exc
        assert "StopWITec (ext:bob)" in h.message, "who asked"
        assert "acquire a spectrum" in h.message and str(exc) in h.message, "the device's own words are kept"
    # refusals and unexpected errors are not relabelled
    assert counted.halt_for(MicroscopeLimitError("too high"), "x").kind == "limit"
    assert counted.halt_for(RuntimeError("a bug"), "x").kind == "error"
    # an abort that was asked for outranks the counted stop (as in check()); a device error stays one
    flag["on"] = True
    assert both.halt_for(cut, "x").kind == "aborted"
    assert both.halt_for(StopInProgress("witec: a stop is under way"), "x").kind == "aborted"
    assert both.halt_for(idle, "x").kind == "device_error"


@pytest.mark.parametrize("counted", [True, False], ids=["stop-counted", "nothing-counted"])
def test_a_motion_aborted_during_an_exposure_is_that_stop_only_when_a_stop_was_counted(bench, scope, counted):
    from mast.instruments.base import MotionAborted

    real = bench.mic.acquire
    n = {"k": 0}

    def acquire(**kw):
        n["k"] += 1
        if n["k"] == 2:
            if counted:
                bench.mic.note_external_stop("StopWITec (ext:bob)")      # the skill counts first …
            raise MotionAborted("witec: acquisition stopped")           # … then the device is stopped under the exposure
        return real(**kw)

    bench.mic.acquire = acquire
    res, _ = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10,20"), bench=bench)
    d = res.data
    assert not res.success and d["steps_done"] == 1 and n["k"] == 2
    if counted:
        assert d["stop_kind"] == "stopped_externally" and d["status"] == "stopped"
        assert "StopWITec (ext:bob)" in d["stop_reason"] and "acquisition stopped" in d["stop_reason"]
    else:
        assert d["stop_kind"] == "aborted" and d["status"] == "aborted"
    assert_safe(bench)


@pytest.mark.parametrize("skill_name, where", [("StopWITec", "settle"), ("CloseWITecShutter", "settle"),
                                               ("StopWITec", "exposure"), ("CloseWITecShutter", "exposure")])
def test_witec_com_a_real_stop_skill_landing_in_the_middle_of_a_call_ends_the_run_as_that_stop(witec_bench, skill_name, where):
    from mast.instruments.witec import params as P
    from mast.skills.builtins import optics_witec

    w = witec_bench.make()
    fake = witec_bench.fake
    skill = getattr(optics_witec, skill_name)
    target = "select_laser" if where == "settle" else "acquire"
    real_call, real_sleep = getattr(w.mic, target), w.mic._sleep
    inside, fired = [], []

    def wrapped(*a, **k):
        inside.append(True)
        try:
            return real_call(*a, **k)
        finally:
            inside.pop()

    def sleep(seconds):
        if inside and not fired:
            fired.append(True)
            skill().execute(SimpleNamespace(owner="ext:bob"), {})       # lands in the middle of the call
        real_sleep(seconds)

    setattr(w.mic, target, wrapped)
    w.mic._sleep = sleep
    res, opr = run(PolarizationMatrix, {**WITEC_POL, "incident_angles_deg": "0,10,20"})
    d = res.data
    assert fired, "the stop landed inside the call"
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["status"] == "stopped", (d["stop_kind"], d["stop_reason"])
    assert f"{skill_name} (ext:bob)" in d["stop_reason"], "who asked: the counter had moved before the device raised"
    assert w.mic.external_stop_epoch == 1
    assert _shutter_sets(fake).count(True) == (0 if where == "settle" else 1), "no shutter opened after the stop"
    assert fake.peek(P.LASER_SHUTTER) is False


# ── a stop that nobody counts: registry.stop_all() ────────────────────────────────────────────────────────────────
#
# The four stop skills and safe_off_all count the stop before they act (note_external_stop); registry.stop_all() - what
# StopOpticalStage without a device_id, the shutdown hooks and a composite's failure clean-up call - stops the whole
# station (microscope, lasers, temperature controllers) and counts nothing. A running protocol is not told: it opens the
# shutter again at its next step. (The E-STOP is covered by the abort latch, which outranks.) Repro on the Sim bench:
# PolarizationMatrix, 3 steps, StopOpticalStage after step 1 -> completed 3/3, the shutter opened 3 times. strict: when
# the registry (or the skill) counts the stop, this test turns green and the marker has to go.


def test_stop_optical_stage_without_a_device_id_ends_a_running_protocol(bench, scope):
    from mast.skills.builtins.optics_stage import StopOpticalStage

    shutter_calls = _count_calls(bench.mic, "set_shutter")
    _after_exposure(bench, lambda: StopOpticalStage().execute(SimpleNamespace(owner="ext:dan"), {}))
    res, _ = run(PolarizationMatrix, pol_params(mode="par", incident_angles_deg="0,10,20"), bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["steps_done"] == 1, (d["status"], d["steps_done"])
    assert shutter_calls.count((True,)) == 1, "the shutter is not opened again after the panic stop"


# ── the driver cancelled a switch-on / a setpoint because a stop was asked for after it started ─────────────
#
# EmissionPreempted / TemperaturePreempted (InstrumentError subclasses, "pre-empted" in the message): the stop wins,
# and what had been switched on is switched back off. For the protocol that is somebody else's stop - the same clean
# stop as one the epochs show - and not a device error. (Laser.off_epoch / TemperatureController.stop_epoch count
# EVERY off / stop, the run's own cleanup included: they cancel queued operations, they are not the "somebody pressed
# stop" signal; that is external_stop_epoch.)


def test_a_cancelled_switch_on_or_setpoint_is_a_clean_stop_and_every_other_device_error_stays_one():
    from mast.instruments.laser import EmissionPreempted
    from mast.instruments.temperature import TemperaturePreempted

    for exc in (EmissionPreempted("superk: emission on was pre-empted by an emission-off"),
                TemperaturePreempted("tc: setpoint 40 was pre-empted by a stop")):
        halt = op._classify(exc, "do the thing")
        assert halt.kind == "stopped_externally" and halt.kind in op._CLEAN_STOPS
        assert "pre-empted" in halt.message and "do the thing" in halt.message
        assert str(exc) in halt.message, "which device and which action: the operator needs it"
        with use_lang("zh"):
            zh = op._classify(exc, "做某事")
        assert zh.kind == "stopped_externally" and str(exc) in zh.message and "做某事" in zh.message
        assert "被别处要求的停止打断" in zh.message
    assert op._classify(InstrumentError("boom"), "x").kind == "device_error"
    assert op._classify(OperationNotSupported("no"), "x").kind == "not_supported"


def test_a_switch_on_the_driver_cancelled_ends_the_run_cleanly_before_anything_is_measured(bench, scope):
    from mast.instruments.laser import EmissionPreempted

    def preempted():
        raise EmissionPreempted("superk: emission on was pre-empted by an emission-off")

    bench.tun.emission_on = preempted
    res, opr = run(SHGExcitationScan, shg_params(waveplate_slot="none"), bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["status"] == "stopped"
    assert d["stop_phase"] == "prepare" and d["steps_done"] == 0 and "pre-empted" in d["stop_reason"]
    assert bench.mic.acquisitions == 0
    assert_safe(bench)


def test_a_switch_back_on_after_a_swap_that_the_driver_cancelled_ends_the_run_cleanly(bench, scope):
    from mast.instruments.laser import EmissionPreempted

    real_on = bench.tun.emission_on
    calls = {"n": 0}

    def on():
        calls["n"] += 1
        if calls["n"] == 2:                                        # the restore after the second plate swap
            raise EmissionPreempted("superk: emission on was pre-empted by an emission-off")
        return real_on()

    bench.tun.emission_on = on
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="540,1040"), bench=bench)
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["status"] == "stopped"
    assert d["steps_done"] == 1 and d["stop_phase"] == "step" and calls["n"] == 2, "the source is not tried a third time"
    assert_safe(bench)


def test_a_setpoint_the_controller_cancelled_ends_the_run_and_no_other_is_sent(make_bench, scope):
    from mast.instruments.temperature import TemperaturePreempted

    b = make_bench()
    real = b.tc.set_setpoint
    sent = []

    def setpoint(c, **kw):
        sent.append(c)
        if len(sent) == 2:
            raise TemperaturePreempted("tc: setpoint 40 was pre-empted by a stop")
        return real(c, **kw)

    b.tc.set_setpoint = setpoint
    res, _ = run(TemperatureSeries, temp_params(temperatures_c="30,40,50", final_setpoint_c=25), bench=b)
    d = res.data
    assert not res.success and d["stop_kind"] == "stopped_externally" and d["status"] == "stopped"
    assert d["steps_done"] == 1 and d["stopped_at_step"] == 2
    assert sent == [30.0, 40.0], "no third point and no final setpoint"
    assert d["cleanup"]["temperature"] == {"action": "stop"} and "pre-empted" in d["stop_reason"]


def test_a_final_setpoint_the_controller_cancelled_falls_back_to_stopping_it(make_bench, scope):
    from mast.instruments.temperature import TemperaturePreempted

    b = make_bench()
    real = b.tc.set_setpoint

    def setpoint(c, **kw):
        if c == 25.0:
            raise TemperaturePreempted("tc: setpoint 25 was pre-empted by a stop")
        return real(c, **kw)

    b.tc.set_setpoint = setpoint
    res, _ = run(TemperatureSeries, temp_params(final_setpoint_c=25), bench=b)
    assert res.success, res.error                                  # the measurement itself was done
    assert res.data["cleanup"]["temperature"]["action"] == "stop"
    assert any("final setpoint" in w for w in res.data["warnings"])
    assert b.tc.status()["state"] == "stopped"


def test_the_runs_own_off_and_stop_do_not_look_like_somebody_elses_stop(bench, scope):
    """off_epoch / stop_epoch count EVERY emission_off() / stop() - the run's own swaps and cleanup included; only
    external_stop_epoch says that somebody else pressed stop. A watch on the first two would end every swap."""
    proto = SHGExcitationScan()
    params = shg_params(wavelengths_nm="800", waveplate_slot="none", temperature_id="tc", prompt_timeout_s=0.05)
    cfg = proto._parse(params)
    rig = op._resolve_rig(cfg, **proto._rig_needs(cfg))
    r = op._Run(proto, Ctx(), params, cfg, rig, proto._plan(cfg, rig))
    r.watch_stops()
    off0, stop0 = bench.tun.off_epoch, bench.tc.stop_epoch
    bench.tun.emission_off()                    # what the run itself does to take the source dark for a swap
    bench.tc.stop()
    assert bench.tun.off_epoch == off0 + 1 and bench.tc.stop_epoch == stop0 + 1
    assert r.external_stop() == ""
    r.check()                                   # does not raise
    bench.tun.note_external_stop("LaserEmissionOff by ext:alice")
    assert "light source" in r.external_stop()


def test_the_run_watches_the_external_epoch_not_the_shutdown_generations(make_bench, scope):
    """Its own cleanup moves off_epoch / stop_epoch (every off / stop counts there) but not external_stop_epoch."""
    b = make_bench()
    res, _ = run(SHGExcitationScan, shg_params(wavelengths_nm="800", waveplate_slot="none"), bench=b)
    assert res.success, res.error
    res, _ = run(TemperatureSeries, temp_params(temperatures_c="30"), bench=b)
    assert res.success, res.error
    assert b.tun.off_epoch > 0 and b.tc.stop_epoch > 0, "the run's own emission_off / stop() were counted there …"
    assert b.tun.external_stop_epoch == 0 and b.tc.external_stop_epoch == 0 and b.mic.external_stop_epoch == 0
