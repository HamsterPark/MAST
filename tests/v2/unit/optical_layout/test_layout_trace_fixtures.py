"""The shared trace fixtures (``tests/fixtures/optical_layout/*.json``) against the Python tracer.

The front end's TypeScript tracer reads the same files. Each file holds a layout and the trace the
two implementations must both produce; the Python result was checked by hand against the physics
(``test_layout_trace_physics.py`` re-derives the important ones independently). This file keeps
the fixtures honest: they are complete, canonical, free of rounding ties' cousins (negative zero),
and a comparator that can actually fail.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

from mast.optical_layout.model import Layout
from mast.optical_layout.trace import MAX_TRACE_EVENTS, near_misses, trace_layout

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "optical_layout"
FILES = sorted(FIXTURES.glob("*.json"))
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
}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def diff(a, b, path: str = "$") -> list[str]:
    """Where *a* and *b* differ: numbers within ``TOL``, everything else exactly."""
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return [] if a is b or a == b and type(a) is type(b) else [f"{path}: {a!r} != {b!r}"]
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return [] if abs(a - b) <= TOL else [f"{path}: {a!r} != {b!r}"]
    if isinstance(a, dict) and isinstance(b, dict):
        out = [f"{path}: keys {sorted(a)} != {sorted(b)}"] if set(a) != set(b) else []
        for k in a.keys() & b.keys():
            out += diff(a[k], b[k], f"{path}.{k}")
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: {len(a)} items != {len(b)}"]
        return [m for i, (x, y) in enumerate(zip(a, b)) for m in diff(x, y, f"{path}[{i}]")]
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


def test_every_branch_obeys_the_shape_the_front_end_relies_on():
    for path in FILES:
        for tree in load(path)["expected_trace"]:
            assert set(tree) == {"source", "wavelength_nm", "branches"}
            ids = [b["branch"] for b in tree["branches"]]
            assert ids[0] == "0" and len(set(ids)) == len(ids), (path.name, ids)
            for b in tree["branches"]:
                assert set(b) == {"branch", "fraction", "segments", "events", "end"}
                assert b["end"] in {"stop", "blocked", "back_hit", "split", "left_table", "max_events", "budget"}
                assert 0 < b["fraction"] <= 1
                n_seg, n_ev = len(b["segments"]), len(b["events"])
                # one segment per event; one more when the branch ran out of the table or of the budget
                assert n_seg == n_ev + (1 if b["end"] in ("left_table", "budget") else 0), (path.name, b["branch"])
                for ev in b["events"]:
                    assert set(ev) == {"component", "action", "x", "y", "offset_mm",
                                       "incidence_deg", "path_mm"}
                    assert ev["action"] in {"reflect", "transmit", "split", "stop", "blocked",
                                            "back_hit"}
                    assert 0.0 <= ev["incidence_deg"] <= 90.0
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


def test_the_boundary_values_of_the_fixtures_sit_clear_of_rounding_ties():
    """Python's ``round`` and JavaScript's ``Math.round`` part ways only on ties: every number in
    an expected trace that is a coordinate or length must be a multiple of 0.001 (it is stored
    rounded), so re-rounding it cannot move it - and a value that a different implementation
    would round the other way shows up as a difference in the *third* decimal of the unrounded
    value, which the generator checked. Here: stored values are fixed points of the rounding."""
    for path in FILES:
        for tree in load(path)["expected_trace"]:
            for b in tree["branches"]:
                for seg in b["segments"]:
                    for v in seg.values():
                        assert abs(v - round(v, 3)) < 1e-12, (path.name, v)
                for ev in b["events"]:
                    for k in ("x", "y", "offset_mm", "incidence_deg", "path_mm"):
                        assert abs(ev[k] - round(ev[k], 3)) < 1e-12, (path.name, k, ev[k])
                assert abs(b["fraction"] - round(b["fraction"], 6)) < 1e-12
                assert math.isfinite(b["fraction"])
