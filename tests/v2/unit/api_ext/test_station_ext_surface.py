"""The external surface (``/api/ext/v1``) under the optical station profile.

* the door: STM skills are refused, optical and generic ones run (and an STM station is as
  it was);
* skill search and cards: withheld skills are not offered, the card says why;
* the briefing: tip / instrument / live / prefs disappear, a station section, the signed
  optical state and the waiting operator prompts appear; nothing touches Nanonis and no
  device is opened;
* VISTA: ``mast_act`` named actions are refused with a pointer to ``mast_run``; a spectrum
  acquired through ``/vista/act`` comes back as an observation with a picture.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

from _ext_world import H, wait_terminal

_STATION_TESTS = Path(__file__).resolve().parents[2] / "station"
if str(_STATION_TESTS) not in sys.path:
    sys.path.insert(0, str(_STATION_TESTS))

from _spectra import write_spectrum  # noqa: E402

from mast import station  # noqa: E402
from mast.operator_hitl import optical_state, prompts  # noqa: E402
from mast.skills.composite.interpreter import make_spec_skill  # noqa: E402
from mast.skills.composite.spec import CompositeSpec  # noqa: E402


def _spec(name, *skills):
    nodes = [{"type": "step", "id": f"s{i}", "skill": s, "params": {}} for i, s in enumerate(skills)]
    return make_spec_skill(CompositeSpec.from_dict(
        {"name": name, "description": name, "version": 1, "safety_level": "confirm",
         "params": [], "nodes": nodes}))


@pytest.fixture()
def ow(world, monkeypatch, tmp_path):
    """The ext world, on an optical station with a simulated bench."""
    from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry
    from mast.skills import station_face
    from mast.skills.builtins.bias import GetBias
    from mast.skills.builtins.optics_spectro import AcquireSpectrum
    from mast.skills.builtins.optics_stage import ListOpticalDevices

    monkeypatch.setenv(station.ENV_VAR, "optical")
    station.reset()
    cfg = tmp_path / "optical_instruments.json"
    cfg.write_text(json.dumps({"devices": [
        {"id": "ccd", "type": "sim_spectrometer", "role": "raman_ccd", "laser_line_nm": 532.0,
         "time_scale": 0.0, "limits": {"max_exposure_ms": 5000, "max_frames": 10}},
        {"id": "witec", "type": "sim_microscope"},
        {"id": "superk", "type": "sim_tunable_laser"}], "delay_line": None}), encoding="utf-8")
    reset_instrument_registry(InstrumentRegistry(cfg))
    old_b = prompts.set_backend(prompts.InMemoryPromptBackend())
    old_s = optical_state.set_store(optical_state.OpticalStateStore())
    for cls in (ListOpticalDevices, AcquireSpectrum, GetBias):
        world.reg.register(cls)
    world.reg.register(_spec("OptOnly", "ListOpticalDevices"))
    world.reg.register(_spec("ScansToo", "ListOpticalDevices", "GetBias"))
    station_face.bind_registry(world.reg)
    world.svc = world.app.state.vista
    world.svc.stop_watcher()
    yield world
    station_face.bind_registry(None)
    reset_instrument_registry(None)
    prompts.set_backend(old_b)
    optical_state.set_store(old_s)
    station.reset()


def _submit(c, skill, **params):
    return c.post("/jobs", json={"skill": skill, "params": params}, headers=H())


# ── the door ────────────────────────────────────────────────────────────────────
def test_an_stm_skill_is_refused_at_the_door_on_an_optical_station(ow):
    r = _submit(ow.client, "GetBias")
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"] == "skill_disabled" and body["disabled"] == ["GetBias"]
    assert ow.jm.list(actor="ext:tester") == [], "a refused skill must not become a job"
    assert ow.pool.calls == []


def test_a_composite_that_scans_is_refused_through_its_steps(ow):
    r = _submit(ow.client, "ScansToo")
    assert r.status_code == 422 and r.json()["error"] == "skill_disabled"
    assert "GetBias" in r.json()["disabled"]


def test_optical_skills_and_composites_of_them_run(ow):
    for skill in ("ListOpticalDevices", "OptOnly"):
        r = _submit(ow.client, skill)
        assert r.status_code == 202, (skill, r.text)
        job = wait_terminal(ow.client, r.json()["job_id"])
        assert job["state"] == "succeeded", (skill, job)
    assert ow.pool.calls == []


def test_a_probe_skill_from_nowhere_is_not_on_the_whitelist(ow):
    r = _submit(ow.client, "ExtRead")
    assert r.status_code == 422 and r.json()["error"] == "skill_disabled"


def test_the_same_door_on_an_stm_station_lets_the_stm_skill_in(ow, monkeypatch):
    monkeypatch.setenv(station.ENV_VAR, "stm")
    r = _submit(ow.client, "GetBias")
    assert r.status_code == 202, r.text


def test_instrument_agent_tools_that_read_stm_state_are_not_offered(ow, monkeypatch):
    assert ow.client.get("/skills/get_current_tip", headers=H()).status_code == 404
    r = _submit(ow.client, "get_current_tip")
    assert r.status_code == 404 and r.json()["error"] == "unknown_skill"
    # the generic ones are still there
    assert ow.client.get("/skills/list_experiments", headers=H()).status_code == 200
    monkeypatch.setenv(station.ENV_VAR, "stm")
    assert ow.client.get("/skills/get_current_tip", headers=H()).status_code == 200


# ── search and cards ────────────────────────────────────────────────────────────
def _names(c, q):
    return {x["name"] for x in c.get("/skills/search", params={"q": q}, headers=H()).json()["results"]}


def test_search_does_not_offer_what_the_station_withholds(ow, monkeypatch):
    assert "GetBias" not in _names(ow.client, "bias")
    assert "AcquireSpectrum" in _names(ow.client, "spectrum")
    monkeypatch.setenv(station.ENV_VAR, "stm")
    assert "GetBias" in _names(ow.client, "bias")
    assert "AcquireSpectrum" in _names(ow.client, "spectrum")


def test_the_card_of_a_withheld_skill_says_why(ow):
    card = ow.client.get("/skills/GetBias", headers=H()).json()
    assert card["tool_face"]["disabled"] is True and "光学站" in card["tool_face"]["reason"]
    ok = ow.client.get("/skills/AcquireSpectrum", headers=H()).json()
    assert ok["tool_face"]["disabled"] is False, ok["tool_face"]


# ── an STM station keeps the optical station's own skills off its surface ───────────
_FAMILY = {
    "GetWITecStatus": "mast.skills.builtins.optics_witec",
    "GetLaserStatus": "mast.skills.builtins.optics_sources",
    "SHGExcitationScan": "mast.skills.builtins.optics_protocols",
    "AskOperator": "mast.skills.builtins.operator_skills",
}


@pytest.fixture()
def family(ow):
    """Stand-ins for what the witec / sources / protocols / operator modules register (their
    modules may register nothing yet)."""
    from mast.core.types import SafetyLevel, SkillCategory, SkillMetadata, SkillResult
    from mast.skills.base import BaseSkill

    def make(name, module):
        class _S(BaseSkill):
            def metadata(self):
                return SkillMetadata(name=name, version="1.0.0", category=SkillCategory.READ,
                                     safety_level=SafetyLevel.AUTO, description=f"{name} (stand-in)")

            def execute(self, context, params):
                return SkillResult(skill_name=name, success=True, data={})
        _S.__module__ = module
        return _S

    for name, module in _FAMILY.items():
        ow.reg.register(make(name, module))
    return tuple(_FAMILY)


def test_an_stm_station_refuses_the_optical_stations_own_skills_at_the_door(ow, family, monkeypatch):
    monkeypatch.setenv(station.ENV_VAR, "stm")
    for name in family:
        r = _submit(ow.client, name)
        assert r.status_code == 422, (name, r.text)
        assert r.json()["error"] == "skill_disabled" and r.json()["disabled"] == [name]
    assert ow.jm.list(actor="ext:tester") == [], "a refused skill must not become a job"
    assert _submit(ow.client, "GetBias").status_code == 202, "the STM's own skills still get in"


def test_the_optical_station_runs_them(ow, family):
    for name in family:
        r = _submit(ow.client, name)
        assert r.status_code == 202, (name, r.text)
        assert wait_terminal(ow.client, r.json()["job_id"])["state"] == "succeeded", name


def test_an_stm_station_neither_searches_nor_offers_them(ow, family, monkeypatch):
    assert "AskOperator" in _names(ow.client, "AskOperator")
    monkeypatch.setenv(station.ENV_VAR, "stm")
    assert "AskOperator" not in _names(ow.client, "AskOperator")
    card = ow.client.get("/skills/AskOperator", headers=H()).json()
    assert card["tool_face"]["disabled"] is True
    assert "光学站" in card["tool_face"]["reason"] and "stm" in card["tool_face"]["reason"]
    monkeypatch.setenv(station.ENV_VAR, "optical")
    shown = ow.client.get("/skills/AskOperator", headers=H()).json()
    assert shown["tool_face"]["disabled"] is False


# ── the registry the request belongs to, not a global binding ───────────────────────
# ``CoreRuntime.setup`` binds the live registry for the station gate; a process that never ran it
# (the one-shot CLI, an embedding, a script) binds nothing. The door and the skill card hold the
# registry of the request, so they must classify that one instead of reading a global.

def test_the_door_classifies_the_registry_it_was_given_even_when_nothing_is_bound(ow, family, monkeypatch):
    from mast.skills import station_face

    station_face.bind_registry(None)
    monkeypatch.setenv(station.ENV_VAR, "stm")
    for name in family:
        r = _submit(ow.client, name)
        assert r.status_code == 422 and r.json()["error"] == "skill_disabled", (name, r.text)
    assert _submit(ow.client, "GetBias").status_code == 202, "the STM's own skills still get in"
    monkeypatch.setenv(station.ENV_VAR, "optical")
    r = _submit(ow.client, "GetBias")
    assert r.status_code == 422 and r.json()["error"] == "skill_disabled", r.text


def test_the_skill_card_classifies_the_registry_it_was_given_even_when_nothing_is_bound(
        ow, family, monkeypatch):
    from mast.skills import station_face

    station_face.bind_registry(None)
    monkeypatch.setenv(station.ENV_VAR, "stm")
    card = ow.client.get("/skills/AskOperator", headers=H()).json()
    assert card["tool_face"]["disabled"] is True and "只属于光学站" in card["tool_face"]["reason"], card["tool_face"]
    monkeypatch.setenv(station.ENV_VAR, "optical")
    card = ow.client.get("/skills/GetBias", headers=H()).json()
    assert card["tool_face"]["disabled"] is True and "不属于光学站" in card["tool_face"]["reason"], card["tool_face"]


# ── the same, on the real skills of the four modules ────────────────────────────────
def _real_station_skills():
    """name -> class for every skill the four optical-station modules define (the real ones)."""
    import importlib

    from mast.skills.base import BaseSkill

    out = {}
    for module in dict.fromkeys(_FAMILY.values()):      # the four modules, written out above
        mod = importlib.import_module(module)
        for cls in vars(mod).values():
            if (isinstance(cls, type) and issubclass(cls, BaseSkill) and cls is not BaseSkill
                    and cls.__module__ == module):
                try:
                    out[cls().metadata().name] = cls
                except Exception:  # noqa: BLE001 - an abstract helper, not a skill
                    continue
    return out


@pytest.fixture()
def real_family(ow):
    classes = _real_station_skills()
    assert len(classes) >= 30, f"the optical station's modules register only {sorted(classes)}"
    for cls in classes.values():
        ow.reg.register(cls)
    return tuple(sorted(classes))


def test_an_stm_station_refuses_every_real_optical_station_skill_at_the_door(ow, real_family, monkeypatch):
    monkeypatch.setenv(station.ENV_VAR, "stm")
    wrong = {}
    for name in real_family:
        r = _submit(ow.client, name)
        got = (r.status_code, r.json().get("error"), r.json().get("disabled"))
        if got != (422, "skill_disabled", [name]):
            wrong[name] = got
    assert wrong == {}
    assert ow.jm.list(actor="ext:tester") == [], "a refused skill must not become a job"


def test_an_stm_station_says_every_real_optical_station_skill_belongs_to_the_optical_station(
        ow, real_family, monkeypatch):
    monkeypatch.setenv(station.ENV_VAR, "stm")
    wrong = {}
    for name in real_family:
        r = ow.client.get(f"/skills/{name}", headers=H())
        face = r.json().get("tool_face", {}) if r.status_code == 200 else {"status": r.status_code}
        if not (face.get("disabled") is True and "只属于光学站" in face.get("reason", "")):
            wrong[name] = face
    assert wrong == {}
    assert [n for n in real_family if n in _names(ow.client, n)] == [], "search must not offer them"


def test_an_optical_station_leaves_every_real_optical_station_skill_alone(ow, real_family):
    """Judged by the fourth gate itself: a name may still be refused by another gate (a hardware
    module that lists the same name), which is that gate's business."""
    from mast.skills import station_face
    from mast.skills.tool_face import compute

    assert not (set(real_family) & compute().station)
    assert not (set(real_family) & station_face.hidden_names(ow.reg))
    for name in real_family:
        reason = ow.client.get(f"/skills/{name}", headers=H()).json()["tool_face"]["reason"]
        assert "光学站" not in reason, (name, reason)


# ── the briefing ────────────────────────────────────────────────────────────────
def _briefing(c, **params):
    return c.get("/briefing", params=params, headers=H()).json()


def test_the_optical_briefing_has_optical_sections_and_no_stm_ones(ow):
    from mast.api.ext.overview import OPTICAL_ORDER

    b = _briefing(ow.client)
    assert list(b["sections"]) == list(OPTICAL_ORDER)
    assert {"station", "optical_state", "operator_prompts", "recent_spectra"} <= set(b["sections"])
    assert {"tip", "instrument", "live", "prefs", "recent_files"}.isdisjoint(b["sections"])
    for name in ("station", "status", "scope", "optical_state", "operator_prompts", "safety",
                 "recent_actions", "recent_spectra", "recording", "jobs", "operator_requests"):
        assert b["sections"][name]["ok"] is True, (name, b["sections"][name])
    status = b["sections"]["status"]["text"]
    assert "Nanonis 连接" not in status and "live:" not in status, status
    assert "站点画像" in status


def test_the_station_section_names_the_profile_the_rules_and_the_devices(ow):
    t = _briefing(ow.client, sections="station")["sections"]["station"]
    assert t["ok"] and t["data"]["profile"] == "optical"
    text = t["text"]
    for needle in ("optical", "mast_run", "COM Automation", "mast_ask_operator", "laser_area_safe",
                   "ccd", "witec", "superk"):
        assert needle in text, needle
    assert {d["id"] for d in t["data"]["devices"]} == {"ccd", "witec", "superk"}


def test_the_briefing_does_no_hardware_io_and_opens_no_device(ow):
    from mast.instruments.registry import get_instrument_registry

    _briefing(ow.client)
    ow.client.get("/status", headers=H())
    assert ow.pool.calls == [] and ow.state.refreshed == 0
    assert ow.rt.session_dir_asked == 0
    assert get_instrument_registry().built_station_devices() == {}, "the briefing opened a device"


def test_an_open_device_shows_its_status_in_the_briefing(ow):
    from mast.instruments.registry import get_instrument_registry

    get_instrument_registry().laser("superk")
    sec = _briefing(ow.client, sections="station")["sections"]["station"]
    dev = {d["id"]: d for d in sec["data"]["devices"]}
    assert dev["superk"]["connected"] is True and "status" in dev["superk"]
    assert dev["witec"]["connected"] is False and "status" not in dev["witec"]
    # the text a model reads: scalar fields as key=value, nested ones as sizes - never a
    # status cut in the middle of an object
    line = next(l for l in sec["text"].splitlines() if l.lstrip().startswith("- superk"))
    assert "emission=False" in line and "channels=[8 items]" in line, line
    assert "{" not in line.split("→", 1)[1], line


def test_the_signed_optical_state_and_the_waiting_prompts_are_in_the_briefing(ow):
    optical_state.get_store().set_by_operator("objective", "50x", by="alice", source="web")
    prompts.post(prompts.OperatorPrompt(kind="confirm", title="Close NKT CONTROL on COM15",
                                        skill="LaserEmissionOn"))
    b = _briefing(ow.client, sections="optical_state,operator_prompts")["sections"]
    assert "objective: 50x" in b["optical_state"]["text"] and "alice" in b["optical_state"]["text"]
    assert "laser_area_safe" in b["optical_state"]["text"]
    assert b["operator_prompts"]["data"]["count"] == 1
    assert "Close NKT CONTROL on COM15" in b["operator_prompts"]["text"]


def test_a_station_with_no_prompts_says_so(ow):
    t = _briefing(ow.client, sections="operator_prompts")["sections"]["operator_prompts"]
    assert t["ok"] and t["data"]["count"] == 0 and "没有未答" in t["text"]


def test_asking_for_a_stm_section_on_an_optical_station_lists_what_exists(ow):
    r = ow.client.get("/briefing", params={"sections": "tip"}, headers=H())
    assert r.status_code == 422 and r.json()["error"] == "unknown_section"
    assert "station" in r.json()["available"] and "tip" not in r.json()["available"]


def test_status_json_drops_the_nanonis_rows_on_an_optical_station(ow):
    s = ow.client.get("/status", headers=H()).json()
    assert s["station"] == "optical"
    assert "connection" not in s and "live" not in s
    assert s["degraded"] == [] or "scope" not in s["degraded"]


def test_an_stm_station_briefing_is_what_it_was(ow, monkeypatch):
    from mast.api.ext.overview import SECTIONS

    monkeypatch.setenv(station.ENV_VAR, "stm")
    b = _briefing(ow.client)
    assert set(b["sections"]) == set(SECTIONS) and "station" not in b["sections"]
    s = ow.client.get("/status", headers=H()).json()
    assert "station" not in s and "connection" in s and "live" in s


# ── VISTA ───────────────────────────────────────────────────────────────────────
def test_a_named_action_is_refused_with_a_pointer_to_mast_run(ow):
    tok = ow.client.post("/vista/observe", json={}, headers=H()).json()["observation"]["next_token"]
    r = ow.client.post("/vista/act", json={"action": "scan", "params": {}, "after": tok}, headers=H())
    assert r.status_code == 422, r.text
    b = r.json()
    assert b["error"] == "not_available_on_this_station" and "mast_run" in b["detail"]
    # the token is still good for the right door
    ok = ow.client.post("/vista/act", json={"skill": "ListOpticalDevices", "after": tok, "wait_s": 5},
                        headers=H())
    assert ok.status_code == 200, ok.text


def test_a_raw_stm_skill_through_vista_act_is_refused_by_the_same_door(ow):
    tok = ow.client.post("/vista/observe", json={}, headers=H()).json()["observation"]["next_token"]
    r = ow.client.post("/vista/act", json={"skill": "GetBias", "after": tok}, headers=H())
    assert r.status_code == 422 and r.json()["error"] == "skill_disabled"


def test_a_spectrum_acquired_through_vista_act_comes_back_as_an_observation(ow):
    o = ow.client.post("/vista/observe", json={}, headers=H()).json()["observation"]
    body = json.loads(o["text"])
    assert body["station"] == "optical" and body["latest_frame"] is None and o["images"] == []
    r = ow.client.post("/vista/act", json={
        "skill": "AcquireSpectrum", "skill_params": {"exposure_ms": 100.0, "frames": 2},
        "after": o["next_token"], "wait_s": 20}, headers=H())
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["state"] == "done" and a["ok"] is True, a
    obs = a["observation"]
    text = json.loads(obs["text"])
    assert text["new_spectra"] and text["new_spectra"][0]["kind"] == "spectrum"
    assert text["new_spectra"][0]["frames"] == 2 and text["new_spectra"][0]["exposure_ms"] == 100.0
    assert "new_frames" not in text and "frames_unavailable" not in text
    assert len(obs["images"]) == 1
    assert base64.b64decode(obs["images"][0]["data_b64"])[:8] == b"\x89PNG\r\n\x1a\n"
    assert obs["next_token"] and obs["next_token"] != o["next_token"]
    assert ow.pool.calls == [] and ow.state.refreshed == 0, "AcquireSpectrum touched Nanonis"
    # and the next plain observation shows that spectrum
    again = json.loads(ow.client.post("/vista/observe", json={}, headers=H()).json()["observation"]["text"])
    assert again["latest_frame"]["file"] == text["new_spectra"][0]["file"]
