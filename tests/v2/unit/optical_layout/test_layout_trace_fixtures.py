"""The shared trace fixtures (``tests/fixtures/optical_layout/*.json``) against the Python tracer.

The front end's TypeScript tracer reads the same files. Each file holds a layout and the trace the
two implementations must both produce; the Python result was checked by hand against the physics
(``test_layout_trace_physics.py`` re-derives the geometry independently, ``test_layout_beam_physics.py``
the beam: power, polarization and Gaussian beams). This file keeps the fixtures honest: they are
complete, canonical, free of rounding ties' cousins (negative zero), and a comparator that can
actually fail. The numbers inside a ``beam`` are compared with the mixed tolerance of contract v2
§4.8, ``|a - b| <= 1e-6 max(1, |a|, |b|)`` (a Rayleigh range can be ten thousand mm); the geometry
keeps the absolute 1e-6 of v1.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

from mast.optical_layout.model import Layout
from mast.optical_layout.trace import MAX_TRACE_EVENTS, near_misses, trace_layout

from ._layouts import trace_fixture_files

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "optical_layout"
FILES = trace_fixture_files()           # power_text_cases.json is in the folder too, and is not a trace
TOL = 1e-6

#: the cases the contract (§4) asks for, and the file that holds each
REQUIRED = {
    "single_fold_mirror": "单镜折返",
    "two_mirror_dogleg": "两镜潜望（俯视）",
    "beam_splitter_two_branches": "分束器两支",
    "dichroic_by_wavelength": "二向色按波长分路",
    "filter_blocks_and_passes": "滤光片挡掉",
    "back_side_hits": "镜子背面",
    "off_center_hits": "偏离中心命中",
    "miss_aperture": "错过口径",
    "fiber_coupling_continues": "光纤续接",
    "leaves_table": "离开平台",
    "iris_open_diameter": "光阑",
    "shutter_closed_and_open": "快门关",
    "max_events_ping_pong": "64 次上限（两面镜对打）",
    # contract v2 §7: the beam
    "pol_malus_two_polarizers": "A 马吕斯：两片偏振片",
    "pol_hwp_rotates": "B λ/2 转偏振（配 PBS 看分光）",
    "pol_qwp_isolator": "C2 λ/4 隔离器：回程全走反射口",
    "pol_mirror_flips_diagonal": "D 镜子把 +45° 变成 −45°",
    "pol_pbs_unpolarized_split": "E/E3 PBS 分 +45° 与非偏振光；非偏振过偏振片",
    "pol_pbs_p_only": "p 偏振打进 PBS：反射支被剪掉",
    "pol_waveplate_oblique": "轴角投影：斜入射与反面",
    "power_nd_throughput_fibre": "100 mW → OD1 → throughput 0.9 → coupling 0.6 → 续接光纤自带偏振和直径",
    "gauss_free_space": "F 自由传播",
    "gauss_kepler_3x": "G 开普勒扩束 3×",
    "gauss_objective_focus": "H 物镜聚焦，样品在焦点",
    "gauss_multi_wavelength": "同一光源两个波长、二向色分路，各自的 zR",
    # contract 3D §9: heights
    "height_periscope_up": "潜望镜升",
    "height_periscope_down": "潜望镜降（反向穿过，偏离保留）",
    "height_passes_over_and_under": "器件高度不同被越过",
    "height_source_heights": "光源高度不同",
    "height_iris_in_height": "光阑在高度方向：越过、挡住、从开口穿过",
    "height_mixed": "混合情形",
    "beam_crossed_polarizers_no_light": "正交偏振片：几何照走、p = 0",
}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def diff(a, b, path: str = "$", mixed: bool = False) -> list[str]:
    """Where *a* and *b* differ: numbers within ``TOL`` (inside a ``beam``: within ``TOL`` times
    ``max(1, |a|, |b|)``), everything else exactly - ``null``, strings, booleans, list lengths, keys."""
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return [] if a is b or a == b and type(a) is type(b) else [f"{path}: {a!r} != {b!r}"]
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        tol = TOL * max(1.0, abs(a), abs(b)) if mixed else TOL
        return [] if abs(a - b) <= tol else [f"{path}: {a!r} != {b!r}"]
    if isinstance(a, dict) and isinstance(b, dict):
        out = [f"{path}: keys {sorted(a)} != {sorted(b)}"] if set(a) != set(b) else []
        for k in a.keys() & b.keys():
            out += diff(a[k], b[k], f"{path}.{k}", mixed or k == "beam")
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: {len(a)} items != {len(b)}"]
        return [m for i, (x, y) in enumerate(zip(a, b)) for m in diff(x, y, f"{path}[{i}]", mixed)]
    return [] if a == b and type(a) is type(b) else [f"{path}: {a!r} != {b!r}"]


def test_the_fixture_set_covers_the_contract_list():
    names = {p.stem for p in FILES}
    assert len(FILES) >= 20, f"only {len(FILES)} fixtures"
    missing = {n: why for n, why in REQUIRED.items() if n not in names}
    assert not missing, f"fixtures the contract asks for are missing: {missing}"


def budget_of(doc: dict):
    """The smaller trace budget a fixture is made with (``trace_options.max_trace_events``), else None."""
    return doc.get("trace_options", {}).get("max_trace_events")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_the_python_trace_reproduces_the_fixture(path):
    doc = load(path)
    assert {"name", "description", "layout", "expected_trace"} <= set(doc)
    assert doc["name"] == path.stem and doc["description"].strip()
    got = trace_layout(doc["layout"], max_trace_events=budget_of(doc))
    assert diff(got, doc["expected_trace"]) == []


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_the_fixture_layout_is_in_canonical_form(path):
    """The front end traces ``layout`` as it stands; the server would have stored exactly this."""
    raw = load(path)["layout"]
    assert Layout.model_validate(raw).to_dict() == raw


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_the_trace_of_the_model_equals_the_trace_of_the_dict(path):
    doc = load(path)
    raw, n = doc["layout"], budget_of(doc)
    assert trace_layout(Layout.model_validate(raw), max_trace_events=n) == trace_layout(raw, max_trace_events=n)


def test_the_fixtures_that_name_the_default_budget_name_the_one_of_the_tracer():
    """The two implementations each check their own constant against this number."""
    named = {p.stem: load(p)["default_max_trace_events"] for p in FILES if "default_max_trace_events" in load(p)}
    assert set(named) == {"trace_budget_exact", "trace_budget_cut"}
    assert set(named.values()) == {MAX_TRACE_EVENTS}


def test_the_budget_fixtures_hold_what_they_claim():
    exact, cut = load(FIXTURES / "trace_budget_exact.json"), load(FIXTURES / "trace_budget_cut.json")
    assert exact["layout"] == cut["layout"]                       # the same layout, two budgets
    assert (budget_of(exact), budget_of(cut)) == (5, 3)

    def events(doc):
        return [e for t in doc["expected_trace"] for b in t["branches"] for e in b["events"]]

    assert len(events(exact)) == 5
    assert all(b["end"] != "budget" for t in exact["expected_trace"] for b in t["branches"])
    assert len(events(cut)) == 3
    assert [e["component"] for e in events(cut)] == [e["component"] for e in events(exact)][:3]
    marks = [(t["source"], b["branch"]) for t in cut["expected_trace"] for b in t["branches"] if b["end"] == "budget"]
    assert marks == [("las1", "0.t")]                             # the transmitted branch, after the reflected one
    assert cut["expected_trace"][-1]["branches"][-1]["end"] == "budget"          # the last branch of the last tree
    assert [t["source"] for t in cut["expected_trace"]] == ["las1"]              # las2 is not traced
    assert [t["source"] for t in exact["expected_trace"]] == ["las1", "las2"]


@pytest.mark.parametrize("path", [p for p in FILES if "expected_misses" in load(p)],
                         ids=lambda p: p.stem)
def test_the_near_misses_are_those_of_the_fixture(path):
    doc = load(path)
    assert diff(near_misses(doc["layout"], doc["expected_trace"]), doc["expected_misses"]) == []


def test_the_miss_fixture_really_holds_a_near_miss():
    doc = load(FIXTURES / "miss_aperture.json")
    (miss,) = doc["expected_misses"]
    assert miss["component"] == "m1" and miss["outside_mm"] > 0
    assert "m1" not in {e["component"] for t in doc["expected_trace"]
                        for b in t["branches"] for e in b["events"]}


def test_no_fixture_has_a_negative_zero_or_a_non_finite_number():
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        assert "-0.0," not in text and "-0.0\n" not in text, path.name
        for token in ("NaN", "Infinity"):
            assert token not in text, (path.name, token)


def _stokes_ok(s) -> bool:
    """``s`` is ``null`` or three numbers on or inside the unit sphere."""
    return s is None or (len(s) == 3 and all(-1.0 <= v <= 1.0 for v in s) and math.hypot(*s) <= 1.0 + 1e-8)


def test_every_branch_obeys_the_shape_the_front_end_relies_on():
    for path in FILES:
        for tree in load(path)["expected_trace"]:
            assert set(tree) == {"source", "wavelength_nm", "beam", "branches"}
            tb = tree["beam"]
            assert set(tb) == {"source_mw", "m2"}
            assert tb["source_mw"] is None or tb["source_mw"] >= 0.0
            assert tb["m2"] is None or tb["m2"] >= 1.0
            ids = [b["branch"] for b in tree["branches"]]
            assert ids[0] == "0" and len(set(ids)) == len(ids), (path.name, ids)
            for b in tree["branches"]:
                assert set(b) == {"branch", "fraction", "segments", "events", "end"}
                assert b["end"] in {"stop", "blocked", "back_hit", "split", "left_table", "max_events", "budget"}
                assert 0 < b["fraction"] <= 1
                n_seg, n_ev = len(b["segments"]), len(b["events"])
                # one segment per event; one more when the branch ran out of the table or of the budget
                assert n_seg == n_ev + (1 if b["end"] in ("left_table", "budget") else 0), (path.name, b["branch"])
                for seg in b["segments"]:
                    # contract 3D §4: the height and the passes are added keys
                    assert set(seg) == {"x0", "y0", "x1", "y1", "beam", "z_mm", "passes"}
                    for ps in seg["passes"]:
                        assert set(ps) == {"component", "x", "y", "dz_mm"}
                    sb = seg["beam"]
                    assert set(sb) == {"p", "s", "q"}
                    assert 0.0 <= sb["p"] <= 1.0 and _stokes_ok(sb["s"]) and (sb["s"] is not None or sb["p"] == 0.0)
                    # a Gaussian beam in every segment of a tree that has one, never in one that has not
                    assert (sb["q"] is None) == (tb["m2"] is None), (path.name, b["branch"])
                    assert sb["q"] is None or (len(sb["q"]) == 2 and sb["q"][1] > 0.0)
                for i, ev in enumerate(b["events"]):
                    assert set(ev) == {"component", "action", "x", "y", "offset_mm",
                                       "incidence_deg", "path_mm", "beam",
                                       "z_mm", "dz_mm"}          # contract 3D §4: added keys
                    # the beam arrives at the height of the segment that leads to the event
                    assert ev["z_mm"] == b["segments"][i]["z_mm"], (path.name, b["branch"], i)
                    assert ev["action"] in {"reflect", "transmit", "split", "stop", "blocked",
                                            "back_hit"}
                    assert 0.0 <= ev["incidence_deg"] <= 90.0
                    eb = ev["beam"]
                    assert set(eb) == {"p", "mw", "d_mm", "s"}
                    assert (eb["mw"] is None) == (tb["source_mw"] is None)
                    assert (eb["d_mm"] is None) == (tb["m2"] is None) and (eb["d_mm"] is None or eb["d_mm"] > 0)
                    # nothing changes the power or the polarization along a segment: the arrival is
                    # the state at the start of the segment that leads to the event
                    assert (eb["p"], eb["s"]) == (b["segments"][i]["beam"]["p"], b["segments"][i]["beam"]["s"])
                paths = [e["path_mm"] for e in b["events"]]
                assert paths == sorted(paths), (path.name, b["branch"])
                for a, nxt in zip(b["segments"], b["segments"][1:]):
                    assert (a["x1"], a["y1"]) == (nxt["x0"], nxt["y0"]), "the polyline is broken"


def test_the_comparator_can_fail():
    """A comparator that always agrees would make every fixture test vacuous."""
    doc = load(FIXTURES / "single_fold_mirror.json")
    good = doc["expected_trace"]
    assert diff(good, copy.deepcopy(good)) == []
    for mutate in (
        lambda t: t[0]["branches"][0]["events"][0].update(x=t[0]["branches"][0]["events"][0]["x"] + 0.001),
        lambda t: t[0]["branches"][0].update(end="blocked"),
        lambda t: t[0]["branches"][0]["events"][0].update(action="transmit"),
        lambda t: t[0]["branches"][0]["events"].pop(),
        lambda t: t[0].update(wavelength_nm=None),
        lambda t: t.append({}),
    ):
        bad = copy.deepcopy(good)
        mutate(bad)
        assert diff(good, bad), "the comparator did not see a changed fixture"
    assert not diff({"x": 1.0}, {"x": 1.0 + 5e-7}) and diff({"x": 1.0}, {"x": 1.0 + 2e-6})
    assert diff({"x": None}, {"x": 0.0}) and diff({"x": True}, {"x": 1.0})


def test_the_comparator_is_relative_inside_a_beam_and_absolute_outside():
    beam = lambda zr: {"beam": {"q": [0.0, zr]}}                   # noqa: E731
    assert not diff(beam(12000.0), beam(12000.0 + 0.01)), "1e-6 relative of 12000 is 0.012"
    assert diff(beam(12000.0), beam(12000.0 + 0.02))
    assert diff({"x0": 12000.0}, {"x0": 12000.0 + 0.01}), "the geometry stays absolute"
    assert not diff(beam(0.5), beam(0.5 + 9e-7)) and diff(beam(0.5), beam(0.5 + 2e-6)), "below 1: absolute 1e-6"
    # null, list lengths and keys stay strict inside a beam
    assert diff({"beam": {"s": None}}, {"beam": {"s": [0.0, 0.0, 0.0]}})
    assert diff({"beam": {"s": [1.0, 0.0, 0.0]}}, {"beam": {"s": [1.0, 0.0]}})
    assert diff({"beam": {"p": 1.0}}, {"beam": {"p": 1.0, "mw": None}})
    doc = load(FIXTURES / "gauss_kepler_3x.json")
    good = doc["expected_trace"]
    for mutate in (
        lambda t: t[0]["branches"][0]["events"][1]["beam"].update(d_mm=t[0]["branches"][0]["events"][1]["beam"]["d_mm"] * 1.00001),
        lambda t: t[0]["branches"][0]["segments"][1]["beam"].update(q=None),
        lambda t: t[0]["beam"].update(m2=1.001),
        lambda t: t[0]["branches"][0]["events"][0]["beam"].update(s=None),
    ):
        bad = copy.deepcopy(good)
        mutate(bad)
        assert diff(good, bad), "the comparator did not see a changed beam"


def test_the_boundary_values_of_the_fixtures_sit_clear_of_rounding_ties():
    """Python's ``round`` and JavaScript's ``Math.round`` part ways only on ties: every number in
    an expected trace that is a coordinate or length must be a multiple of 0.001 (it is stored
    rounded), so re-rounding it cannot move it - and a value that a different implementation
    would round the other way shows up as a difference in the *third* decimal of the unrounded
    value, which the generator (``tests/fixtures/optical_layout/regenerate.py``) checked. Here:
    stored values are fixed points of the rounding. The beam's numbers are rounded to 1e-9, a
    thousandth of the tolerance, so their ties do not matter."""
    for path in FILES:
        for tree in load(path)["expected_trace"]:
            for b in tree["branches"]:
                for seg in b["segments"]:
                    for k in ("x0", "y0", "x1", "y1", "z_mm"):
                        assert abs(seg[k] - round(seg[k], 3)) < 1e-12, (path.name, seg[k])
                    for ps in seg["passes"]:
                        for k in ("x", "y", "dz_mm"):
                            assert abs(ps[k] - round(ps[k], 3)) < 1e-12, (path.name, k, ps[k])
                for ev in b["events"]:
                    for k in ("x", "y", "offset_mm", "incidence_deg", "path_mm", "z_mm", "dz_mm"):
                        assert abs(ev[k] - round(ev[k], 3)) < 1e-12, (path.name, k, ev[k])
                assert abs(b["fraction"] - round(b["fraction"], 6)) < 1e-12
                assert math.isfinite(b["fraction"])


def test_the_generator_would_write_exactly_these_files(tmp_path):
    """``regenerate.py --check``: the fixtures are what the tracer gives now, and none sits on a
    rounding tie, at the 1e-3 floor of a polarizing beam splitter or at the dark threshold."""
    import subprocess
    import sys

    script = FIXTURES / "regenerate.py"
    proc = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True, timeout=120,
                          cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
