"""``observe`` on an optical station: the newest spectrum, rendered and summarised, and the
state of the optical bench instead of seven Nanonis readbacks.

Layers: the spectrum reader (``mast.vista.optical``) on synthetic files with peaks at known
positions; the same reader against the REAL ``AcquireSpectrum`` output (so a change in the
sidecar breaks a test here, not an observation in the lab); the state echo
(``vista.state.optical_echo`` over ``mast.station_view``); and ``VistaService.observe`` itself.
"""
from __future__ import annotations

import base64
import io
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from _spectra import PEAKS, write_spectrum

from mast import station, station_view
from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry
from mast.operator_hitl import optical_state, prompts
from mast.vista import optical
from mast.vista.service import VistaService, _Caller
from mast.vista.state import optical_echo


def _png_size(png: bytes) -> tuple[int, int]:
    from PIL import Image

    im = Image.open(io.BytesIO(png))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    return im.size


@pytest.fixture(autouse=True)
def fresh_operator_backends():
    old_b = prompts.set_backend(prompts.InMemoryPromptBackend())
    old_s = optical_state.set_store(optical_state.OpticalStateStore())
    yield
    prompts.set_backend(old_b)
    optical_state.set_store(old_s)


@pytest.fixture()
def staging(tmp_path, monkeypatch):
    """No experiment in play: spectra land in ``<root>/artifacts/spectra/<date>``."""
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("MAST_EXPERIMENT_ROOT", str(tmp_path / "experiments"))
    from mast.logging import experiment_log as el

    prev = el.get_active_log()
    el.set_active_log(None)
    d = tmp_path / "root" / "artifacts" / "spectra" / datetime.now().strftime("%Y-%m-%d")
    d.mkdir(parents=True)
    yield d
    el.set_active_log(prev)


# ── reading one spectrum ────────────────────────────────────────────────────────
def test_the_summary_has_the_numbers_a_model_needs(tmp_path):
    f = write_spectrum(tmp_path, exposure_ms=2500.0)
    sf = optical.newest_spectrum(roots=[tmp_path])
    s = optical.summarize(sf)
    assert (s["kind"], s["file"], s["frames"], s["pixels"]) == ("spectrum", f.name, 2, 900)
    assert s["exposure_ms"] == 2500.0 and s["laser_nm"] == 532.0 and s["device_id"] == "witec"
    assert s["axis_nm"] == [530.0, 650.0]
    assert s["max_at"]["nm"] == pytest.approx(PEAKS[0][0], abs=0.5)
    assert s["max_counts"] == pytest.approx(PEAKS[0][1] + 100.0, rel=0.03)
    first, second = s["peaks"][0], s["peaks"][1]
    assert first["nm"] == pytest.approx(560.0, abs=0.5) and second["nm"] == pytest.approx(590.0, abs=0.5)
    assert first["counts"] > second["counts"], "strongest first"
    assert first["raman_shift_cm1"] == pytest.approx(1e7 / 532.0 - 1e7 / 560.0, abs=3.0)
    assert s["saturated"] is False and s["saturation_threshold"] == 60000.0
    assert "age_s" in s and s["age_s"] < 30


def test_a_saturated_frame_is_reported_even_when_the_mean_looks_fine(tmp_path):
    write_spectrum(tmp_path, saturate=True)
    s = optical.summarize(optical.newest_spectrum(roots=[tmp_path]))
    assert s["max_counts"] < 60000, "the mean over two frames hides it"
    assert s["frame_max_counts"] == 65535.0
    assert s["saturated"] is True and s["saturated_pixels"] == 4


def test_the_saturation_threshold_is_configurable(tmp_path, monkeypatch):
    write_spectrum(tmp_path)
    sf = optical.newest_spectrum(roots=[tmp_path])
    assert optical.summarize(sf)["saturated"] is False
    monkeypatch.setenv(optical.SATURATION_ENV, "500")
    s = optical.summarize(sf)
    assert s["saturation_threshold"] == 500.0 and s["saturated"] is True
    assert optical.summarize(sf, saturation_counts=1e9)["saturated"] is False
    monkeypatch.setenv(optical.SATURATION_ENV, "not a number")
    assert optical.summarize(sf)["saturation_threshold"] == optical.DEFAULT_SATURATION_COUNTS


def test_the_sidecar_may_declare_its_own_saturation_level(tmp_path):
    write_spectrum(tmp_path, extra_sidecar={"saturation_counts": 700})
    s = optical.summarize(optical.newest_spectrum(roots=[tmp_path]))
    assert s["saturation_threshold"] == 700.0 and s["saturated"] is True


def test_a_spectrum_without_a_sidecar_or_an_axis_still_reads(tmp_path):
    write_spectrum(tmp_path, sidecar=False, nan_axis=True, laser_nm=None)
    s = optical.summarize(optical.newest_spectrum(roots=[tmp_path]))
    assert s["sidecar"] == "missing or unreadable"
    assert "axis_nm" not in s and "laser_nm" not in s and "exposure_ms" not in s
    assert s["peaks"] and "nm" not in s["peaks"][0] and "pixel" in s["peaks"][0]


def test_the_picture_is_a_png_and_marks_the_peaks(tmp_path):
    write_spectrum(tmp_path)
    sf = optical.newest_spectrum(roots=[tmp_path])
    png = optical.render_png(sf)
    w, h = _png_size(png)
    assert w >= 700 and h >= 280
    assert optical.label_for(sf) == f'<visual kind="spectrum" file="{sf.path.name}"/>'
    # a saturated one is drawn differently from a clean one
    write_spectrum(tmp_path, "spec_20261103-130000-000001_witec", saturate=True)
    bad = optical.newest_spectrum(roots=[tmp_path])
    assert optical.render_png(bad) != png


def test_the_newest_spectrum_wins_and_foreign_files_are_ignored(tmp_path):
    write_spectrum(tmp_path, "spec_old", age_s=900)
    write_spectrum(tmp_path, "spec_new", age_s=5)
    np.save(tmp_path / "notes.npy", np.zeros((3, 3)))
    (tmp_path / "spec_junk.npy").write_bytes(b"not an array")
    names = [s.path.name for s in optical.list_spectra(5, roots=[tmp_path])]
    assert "notes.npy" not in names and names.index("spec_new.npy") < names.index("spec_old.npy")
    summary, png = optical.latest_view(roots=[tmp_path])        # newest by mtime: the junk file
    assert summary["file"] == "spec_junk.npy" and "error" in summary and png is None, (
        "an unreadable newest spectrum is reported, not silently replaced by an older one")


def test_no_spectrum_is_none_not_an_error(tmp_path):
    assert optical.latest_view(roots=[tmp_path]) == (None, None)


def test_spectra_made_since_a_listing_are_found(tmp_path):
    write_spectrum(tmp_path, "spec_a")
    before = optical.listing_keys(roots=[tmp_path])
    assert optical.new_since(before, roots=[tmp_path]) == []
    write_spectrum(tmp_path, "spec_b")
    assert [s.path.name for s in optical.new_since(before, roots=[tmp_path])] == ["spec_b.npy"]


def test_rendering_the_same_spectrum_twice_costs_one_render(tmp_path, monkeypatch):
    write_spectrum(tmp_path)
    calls = []
    real = optical.render_png
    monkeypatch.setattr(optical, "render_png", lambda *a, **k: calls.append(1) or real(*a, **k))
    a = optical.latest_view(roots=[tmp_path])
    b = optical.latest_view(roots=[tmp_path])
    assert len(calls) == 1 and a[1] == b[1] and b[0]["age_s"] >= a[0]["age_s"]


def test_a_spectrum_in_the_experiment_folder_is_found(tmp_path, monkeypatch):
    """Where ``AcquireSpectrum`` puts it: <experiment>/samples/<sample>/raw/other."""
    from mast.core import experiment_paths as ep
    from mast.logging import experiment_log as el

    monkeypatch.setenv("MAST_EXPERIMENT_ROOT", str(tmp_path / "experiments"))
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))

    class Storage:
        def get_experiment(self, eid):
            return {"name": "Raman", "start_time": "2026-11-03T10:00:00", "dir_name": ""}

        def get_sample(self, sid):
            return {"name": "MoS2", "sample_index": 1, "dir_name": "", "description": ""}

        def sample_ordinal(self, sid):
            return 1

    log = SimpleNamespace(_storage=Storage(), current_experiment_id="e1", current_sample_id="s1")
    prev = el.get_active_log()
    el.set_active_log(log)
    try:
        from mast.skills.builtins.optics_common import resolve_scope_dirs

        exp_dir, sdir = resolve_scope_dirs(create=True)
        raw = Path(ep.raw_dir(ep.sample_dir(exp_dir, sdir), ".npy", create=True))
        assert raw.name == "other"
        f = write_spectrum(raw)
        found = optical.newest_spectrum()
        assert found is not None and found.path == f
    finally:
        el.set_active_log(prev)


# ── against the real AcquireSpectrum ────────────────────────────────────────────
class NoStmCtx:
    """What a context looks like on an optical station: an idle pool. Counts every touch."""

    def __init__(self):
        self.calls, self.refreshes = [], 0
        ctx = self

        class State:
            def refresh(self):
                ctx.refreshes += 1
                raise ConnectionError("no Nanonis")

            def snapshot(self):
                raise ConnectionError("no Nanonis")

        self.state = State()

    def safe_call(self, method, *a, **k):
        self.calls.append(method)
        raise ConnectionError("no Nanonis")

    def check_abort(self):
        return False


@pytest.fixture()
def sim_bench(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("MAST_EXPERIMENT_ROOT", str(tmp_path / "experiments"))
    from mast.logging import experiment_log as el

    prev = el.get_active_log()
    el.set_active_log(None)               # no sample: the spectrum stays in staging
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": [
        {"id": "ccd", "type": "sim_spectrometer", "role": "raman_ccd", "laser_line_nm": 532.0,
         "time_scale": 0.0, "limits": {"max_exposure_ms": 5000, "max_frames": 10}}],
        "delay_line": None}), encoding="utf-8")
    reset_instrument_registry(InstrumentRegistry(p))
    yield tmp_path
    reset_instrument_registry(None)
    el.set_active_log(prev)


def test_the_reader_understands_what_acquirespectrum_really_writes(sim_bench, set_profile):
    from mast.skills.builtins.optics_spectro import AcquireSpectrum

    set_profile("optical")
    ctx = NoStmCtx()
    res = AcquireSpectrum().execute(ctx, {"exposure_ms": 100.0, "frames": 3})
    assert res.success, res.error
    # an optical station has no STM: nothing was read, nothing was sampled
    assert ctx.calls == [] and ctx.refreshes == 0
    side = json.loads(Path(res.data["sidecar_path"]).read_text(encoding="utf-8"))
    assert side["stm"]["before"]["available"] is False
    cur = side["stm"]["current_during_exposure"]
    assert cur["n"] == 0 and cur["errors"] == 0, "no sample, and no failed read counted either"

    summary, png = optical.latest_view()
    assert summary["file"] == Path(res.data["path"]).name and "error" not in summary
    assert summary["frames"] == 3 and summary["exposure_ms"] == 100.0
    assert summary["laser_nm"] == 532.0 and summary["device_id"] == "ccd"
    assert summary["max_counts"] == pytest.approx(res.data["counts"]["max"], abs=0.01)
    assert png and _png_size(png)[0] > 600


def test_on_an_stm_station_acquirespectrum_still_reads_the_stm(sim_bench):
    from mast.skills.builtins.optics_spectro import AcquireSpectrum

    class StmCtx(NoStmCtx):
        def __init__(self):
            super().__init__()
            self.state = SimpleNamespace(
                refresh=lambda: self._bump(), snapshot=lambda: self._bump())

        def _bump(self):
            self.refreshes += 1
            return SimpleNamespace(bias_v=0.5, current_a=1e-10)

    ctx = StmCtx()
    res = AcquireSpectrum().execute(ctx, {"exposure_ms": 100.0, "frames": 1,
                                          "sample_current": False})
    assert res.success, res.error
    assert ctx.refreshes == 2, "a fresh STM read before and after the exposure, as always"


# ── the state echo ──────────────────────────────────────────────────────────────
@pytest.fixture()
def bench(tmp_path):
    cfg = {"devices": [
        {"id": "witec", "type": "sim_microscope"},
        {"id": "superk", "type": "sim_tunable_laser"},
        {"id": "npi", "type": "sim_laser", "enabled": False},
        {"id": "pm", "type": "sim_power_meter"},
        {"id": "tc", "type": "sim_temperature"},
    ], "delay_line": None}
    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    reg = InstrumentRegistry(config_path=p)
    reset_instrument_registry(reg)
    yield reg
    reset_instrument_registry(None)


def test_the_echo_never_opens_a_device(bench):
    echo = optical_echo(registry=bench)
    assert bench.built_station_devices() == {}, "an observation opened a device"
    assert sorted(echo["devices_closed"]) == ["pm", "superk", "tc", "witec"]
    assert "microscope" not in echo and "lasers" not in echo
    assert echo["operator_prompts_open"] == 0
    assert set(echo["optical_state"]) == {"unset"}


def test_the_echo_reports_the_status_of_the_devices_the_process_has_open(bench):
    bench.microscope("witec")
    bench.laser("superk")
    bench.temperature_controller("tc")
    echo = optical_echo(registry=bench)
    assert set(echo["microscope"]) == {"witec"} and echo["microscope"]["witec"]["device_id"] == "witec"
    assert "write_access" in echo["microscope"]["witec"]
    assert set(echo["lasers"]) == {"superk"} and set(echo["temperature"]) == {"tc"}
    assert echo["devices_closed"] == ["pm"], "the disabled device is not listed as merely closed"
    json.dumps(echo)                                            # the model reads it as JSON


def test_the_signed_optical_state_and_waiting_prompts_are_in_the_echo(bench):
    optical_state.get_store().set_by_operator("objective", "100x", by="alice", source="test")
    prompts.post(prompts.OperatorPrompt(kind="confirm", title="Close NKT CONTROL"))
    echo = optical_echo(registry=bench)
    assert echo["optical_state"]["signed"] == {"objective": "100x"}
    assert "objective" not in echo["optical_state"]["unset"]
    assert echo["operator_prompts_open"] == 1


def test_a_device_that_hangs_costs_the_observation_seconds_not_forever(bench):
    release = threading.Event()

    class Hung:
        device_id = "superk"

        def status(self):
            release.wait(30)

    bench.laser("superk")
    bench._station["superk"] = Hung()
    try:
        t0 = time.monotonic()
        echo = optical_echo(registry=bench, timeout_s=0.3)
        assert time.monotonic() - t0 < 5.0
        assert "timed out" in echo["lasers"]["superk"]["error"]
    finally:
        release.set()


def test_a_device_that_never_answers_does_not_leak_a_thread_per_observation(bench):
    """Observations are polled for hours; a stuck read must not be re-started each time."""
    release = threading.Event()
    started = []

    class Hung:
        device_id = "superk"

        def status(self):
            started.append(1)
            release.wait(30)

    bench.laser("superk")
    bench._station["superk"] = Hung()
    try:
        first = optical_echo(registry=bench, timeout_s=0.2)
        before = threading.active_count()
        for _ in range(5):
            echo = optical_echo(registry=bench, timeout_s=0.2)
        assert len(started) == 1, "a second status read was started on a device that has not answered"
        assert threading.active_count() <= before
        assert "still waiting" in echo["lasers"]["superk"]["error"]
        assert "timed out" in first["lasers"]["superk"]["error"]
    finally:
        release.set()
    time.sleep(0.2)
    ok = optical_echo(registry=bench, timeout_s=0.2)      # the abandoned read has finished: reads resume
    assert len(started) == 2


def test_a_device_that_raises_is_reported_next_to_it(bench):
    class Broken:
        def status(self):
            raise RuntimeError("port COM14 is held by NKT CONTROL")

    bench.laser("superk")
    bench._station["superk"] = Broken()
    echo = optical_echo(registry=bench)
    assert "COM14" in echo["lasers"]["superk"]["error"]


def test_bounded_reports_values_errors_and_timeouts():
    assert station_view.bounded(lambda: 5, 1) == (True, 5)
    ok, err = station_view.bounded(lambda: 1 / 0, 1)
    assert not ok and "ZeroDivisionError" in err
    ok, err = station_view.bounded(lambda: time.sleep(5), 0.1)
    assert not ok and "timed out" in err


# ── VistaService.observe ────────────────────────────────────────────────────────
@pytest.fixture()
def svc(tmp_path):
    s = VistaService(root=tmp_path / "vista", start_watcher=False)
    yield s
    s.shutdown()


def _observe(svc, session="s1"):
    return svc.observe(_Caller(actor="tester", session=session))


def test_an_optical_observation_carries_the_spectrum_and_the_optical_state(
        svc, staging, bench, set_profile):
    set_profile("optical")
    bench.microscope("witec")
    write_spectrum(staging)
    o = _observe(svc)
    body = json.loads(o["text"])
    assert body["station"] == "optical" and body["observe"] is True
    lf = body["latest_frame"]
    assert lf["kind"] == "spectrum" and lf["peaks"][0]["nm"] == pytest.approx(560.0, abs=0.5)
    assert set(body["instrument"]["microscope"]) == {"witec"}
    # a picture, labelled, and the token the next act must quote
    assert len(o["images"]) == 1 and o["images"][0]["mime"] == "image/png"
    assert o["images"][0]["label"].startswith('<visual kind="spectrum"')
    assert base64.b64decode(o["images"][0]["data_b64"])[:8] == b"\x89PNG\r\n\x1a\n"
    assert o["next_token"] and o["next_token"] in o["text"]
    # none of the STM's readbacks
    assert not {"bias_v", "current_pa", "z_nm", "setpoint_pa", "withdrawn"} & set(body["instrument"])


def test_an_optical_observation_without_a_spectrum_is_empty_but_still_gets_a_token(
        svc, staging, bench, set_profile):
    set_profile("optical")
    o = _observe(svc)
    body = json.loads(o["text"])
    assert body["latest_frame"] is None and o["images"] == [] and o["next_token"]


def test_strict_mode_puts_the_token_on_the_spectrum_picture(svc, staging, bench, set_profile):
    set_profile("optical")
    write_spectrum(staging)
    plain = _observe(svc)
    svc._strict_override = True
    strict = _observe(svc)
    assert strict["next_token"] is None and strict["token_on_image"] is True
    w0, h0 = _png_size(base64.b64decode(plain["images"][0]["data_b64"]))
    w1, h1 = _png_size(base64.b64decode(strict["images"][0]["data_b64"]))
    assert w1 == w0 and h1 > h0, "the token band is appended under the picture"


def test_a_new_spectrum_changes_the_next_observation(svc, staging, bench, set_profile):
    set_profile("optical")
    write_spectrum(staging, "spec_20261103-120000-000001_witec", age_s=60)
    first = json.loads(_observe(svc)["text"])["latest_frame"]["file"]
    write_spectrum(staging, "spec_20261103-120100-000001_witec", peaks=((600.0, 700.0),))
    second = json.loads(_observe(svc)["text"])["latest_frame"]
    assert first.endswith("120000-000001_witec.npy") and second["file"].endswith("120100-000001_witec.npy")
    assert second["peaks"][0]["nm"] == pytest.approx(600.0, abs=0.5)


def test_the_stm_observation_is_untouched(svc, staging):
    o = _observe(svc)
    body = json.loads(o["text"])
    assert "station" not in body, "STM observations keep their exact shape"
    assert body["latest_frame"] is None and o["images"] == []
    assert set(body) >= {"observe", "workspace", "turn", "latest_frame", "instrument", "last_act"}


def test_named_actions_are_refused_on_an_optical_station_before_the_token_is_touched(
        svc, set_profile):
    from mast.vista.service import VistaError

    set_profile("optical")
    svc.bind(SimpleNamespace(), jobs=SimpleNamespace())
    caller = _Caller(actor="tester", session="s1")
    tok = _observe(svc)["next_token"]
    with pytest.raises(VistaError) as ei:
        svc.submit_act(caller, {"action": "scan", "params": {}, "after": tok})
    assert ei.value.status == 422 and ei.value.code == "not_available_on_this_station"
    assert "mast_run" in ei.value.detail and ei.value.extra == {"station": "optical"}
    assert svc.gate.is_live(tok), "a refusal that ran nothing must not burn the token"


def test_the_frame_watcher_does_nothing_on_an_optical_station(svc, set_profile, tmp_path, monkeypatch):
    set_profile("optical")
    monkeypatch.setenv("MAST_VISTA_SESSION_DIR", str(tmp_path))
    svc.bind(SimpleNamespace(), jobs=SimpleNamespace())
    _observe(svc)
    assert svc.watch_once() == []
    assert svc._watch_dir is None, "it must not even have looked at a controller save folder"
