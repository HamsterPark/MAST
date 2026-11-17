"""Heights in the trace (contract ``docs/v2/design/optical_layout_3d.md`` §3-§5) against geometry worked out here.

Every height fixture (§9) has a test whose expected numbers are computed in the test by another route than the
tracer's - plain sums of lengths, ``w(z) = w0 sqrt(1 + (z / zR)^2)`` for the Gaussian beam, and, for a periscope,
the two 45 degree mirrors as 3-D vectors reflecting a ray - and checked against the tracer's output and against
the shared fixture that pins it. Then the rules one by one: the hit rule and its edge, an iris in height (the
three cases, in their order), the window and the order of the passes, the cap on them, the near misses at another
height, and the properties that keep the plane untouched (a beam height alone changes no geometry, all heights
moved together change nothing).
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import numpy as np
import pytest

from mast.optical_layout import trace as T
from mast.optical_layout.model import Layout
from mast.optical_layout.trace import MAX_TRACE_PASSES, near_misses, trace_layout

from ._layouts import comp, det, laser, layout, mirror

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "optical_layout"
EPS = 1e-9


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def both(name: str) -> list[list]:
    """The trace of the fixture's layout now and the fixture's expected trace: every check is made on both."""
    doc = fixture(name)
    return [trace_layout(doc["layout"]), doc["expected_trace"]]


def branch(trace: list, source: str, bid: str = "0") -> dict:
    return next(b for t in trace if t["source"] == source for b in t["branches"] if b["branch"] == bid)


def diameter(z: float, d0: float, nm: float, m2: float = 1.0) -> float:
    """The 1/e^2 diameter z mm past a waist of diameter d0: w(z) = w0 sqrt(1 + (z / zR)^2), zR = pi w0^2 / (M2 lambda)."""
    w0 = d0 / 2.0
    zr = math.pi * w0 * w0 / (m2 * nm * 1e-6)
    return 2.0 * w0 * math.sqrt(1.0 + (z / zr) ** 2)


def periscope_exit(z_ray: float, z_in: float, z_out: float) -> float:
    """The height at which a level ray leaves a periscope of two parallel 45 degree mirrors, by reflecting a
    3-D ray (x along the beam, z up) off the lower mirror (centre (0, z_in), normal (-1, +1)/sqrt 2 for a rise)
    and the upper one (centre (0, z_out), normal (+1, -1)/sqrt 2) - the derivation of contract 3D §3.4."""
    sign = 1.0 if z_out > z_in else -1.0
    p = np.array([-50.0, z_ray])
    d = np.array([1.0, 0.0])

    def reflect(p, d, centre, normal):
        normal = normal / np.linalg.norm(normal)
        t = np.dot(centre - p, normal) / np.dot(d, normal)
        hit = p + t * d
        return hit, d - 2.0 * np.dot(d, normal) * normal

    p, d = reflect(p, d, np.array([0.0, z_in]), np.array([-1.0, sign]))
    assert abs(d[0]) < 1e-12 and abs(abs(d[1]) - 1.0) < 1e-12           # going straight up (or down)
    p, d = reflect(p, d, np.array([0.0, z_out]), np.array([1.0, -sign]))
    assert abs(d[0] - 1.0) < 1e-12                                       # level again, the same way
    return float(p[1])


# ── the fixtures (contract 3D §9) ────────────────────────────────────────────────────────────


def test_the_periscope_formula_is_the_geometry_of_two_mirrors():
    """z' = z + (z_out - z_in): the beam keeps its offset; the vertical run does not depend on it."""
    for z_ray, z_in, z_out in ((75.0, 75.0, 250.0), (78.0, 75.0, 300.0), (203.0, 200.0, 60.0), (70.0, 75.0, 75.5)):
        assert periscope_exit(z_ray, z_in, z_out) == pytest.approx(z_ray + (z_out - z_in), abs=1e-9)


@pytest.mark.parametrize("trace", both("height_periscope_up"), ids=["now", "fixture"])
def test_a_periscope_lifts_the_beam_and_its_vertical_run_is_path(trace):
    br = branch(trace, "las1")
    assert [s["z_mm"] for s in br["segments"]] == [75.0, 250.0, 250.0]
    assert [(e["component"], e["action"], e["z_mm"], e["dz_mm"]) for e in br["events"]] == [
        ("per1", "transmit", 75.0, 0.0), ("m_high", "reflect", 250.0, 0.0), ("det1", "stop", 250.0, 0.0)]
    rise = 250.0 - 75.0
    assert [e["path_mm"] for e in br["events"]] == [150.0, 150.0 + rise + 400.0, 150.0 + rise + 400.0 + 300.0]
    assert br["segments"][1]["passes"] == [{"component": "m_low", "x": 400.0, "y": 100.0, "dz_mm": 175.0}]
    assert br["segments"][0]["passes"] == br["segments"][2]["passes"] == []
    # the Gaussian beam spreads over the vertical run as well
    d = br["events"][1]["beam"]["d_mm"]
    assert d == pytest.approx(diameter(725.0, 1.0, 532.0), abs=EPS)
    assert d == pytest.approx(1.114077, abs=5e-7) and abs(d - diameter(550.0, 1.0, 532.0)) > 0.04
    assert br["segments"][1]["beam"]["q"][0] == pytest.approx(150.0 + rise)


@pytest.mark.parametrize("trace", both("height_periscope_down"), ids=["now", "fixture"])
def test_a_periscope_met_both_ways_keeps_the_offset(trace):
    br = branch(trace, "las1")
    z0 = 203.0
    z1 = periscope_exit(z0, 200.0, 60.0)                         # forward: in at 200, out at 60
    z2 = periscope_exit(z1, 60.0, 200.0)                         # back: in at 60, out at 200
    assert (z1, z2) == pytest.approx((63.0, 203.0))
    assert [s["z_mm"] for s in br["segments"]] == [z0, z1, z1, z2]
    assert [(e["component"], e["dz_mm"]) for e in br["events"]] == [("per1", 3.0), ("m1", 3.0), ("per1", 3.0), ("det1", 3.0)]
    run = 140.0
    assert [e["path_mm"] for e in br["events"]] == [150.0, 150.0 + run + 200.0, 490.0 + 200.0, 690.0 + run + 180.0]
    assert br["events"][-1]["beam"]["d_mm"] == pytest.approx(diameter(1010.0, 2.0, 633.0), abs=EPS)
    assert br["events"][-1]["beam"]["d_mm"] == pytest.approx(2.040994, abs=5e-7)


@pytest.mark.parametrize("trace", both("height_passes_over_and_under"), ids=["now", "fixture"])
def test_passes_over_and_under_and_the_edge_of_the_aperture(trace):
    br = branch(trace, "las1")
    half = 25.4 / 2
    # under 100 (75 - 100 = -25), over 50 (+25), in the order along the beam
    assert br["segments"][0]["passes"] == [{"component": "m_hi", "x": 200.0, "y": 100.0, "dz_mm": -25.0},
                                           {"component": "m_lo", "x": 300.0, "y": 100.0, "dz_mm": 25.0}]
    # 87.7: exactly half the aperture off, which belongs to the aperture (as |offset| = A/2 does in the plane)
    edge = br["events"][0]
    assert (edge["component"], edge["action"], edge["dz_mm"]) == ("m_edge", "reflect", -half)
    # 87.71: 0.01 mm beyond the edge, passed
    assert br["segments"][1]["passes"] == [{"component": "m_out", "x": 400.0, "y": 300.0, "dz_mm": -12.71}]
    assert [(e["component"], e["action"], e["dz_mm"]) for e in br["events"][1:]] == [("iris1", "transmit", -2.0),
                                                                                      ("det1", "stop", 0.0)]


@pytest.mark.parametrize("trace", both("height_source_heights"), ids=["now", "fixture"])
def test_sources_emit_at_their_own_heights_and_the_beam_height_is_the_default(trace):
    start = {t["source"]: t["branches"][0]["segments"][0]["z_mm"] for t in trace}
    assert start == {"las_a": 120.0, "las_b": 140.0, "las_c": 120.0, "lamp1": 60.0, "fo1": 90.0}
    assert branch(trace, "las_b")["segments"][0]["passes"] == [{"component": "m1", "x": 200.0, "y": 200.0, "dz_mm": 20.0}]
    assert branch(trace, "fo1")["segments"][0]["passes"] == [{"component": "m2", "x": 450.0, "y": 400.0, "dz_mm": -30.0}]
    for t in trace:
        assert all(e["dz_mm"] == 0.0 and e["action"] == "stop" for b in t["branches"] for e in b["events"])


@pytest.mark.parametrize("trace", both("height_iris_in_height"), ids=["now", "fixture"])
def test_an_iris_in_height_passed_blocked_or_through_in_that_order(trace):
    """Contract 3D §3.3: |dz| > A/2 passes over it (no effect); else |dz| > open/2 is blocked; else through."""
    over, block, through = branch(trace, "las1"), branch(trace, "las2"), branch(trace, "las3")
    assert over["segments"][0]["passes"] == [{"component": "iris_over", "x": 200.0, "y": 100.0, "dz_mm": 35.0}]
    assert [(e["component"], e["action"]) for e in over["events"]] == [("det1", "stop")]
    assert [(e["component"], e["action"], e["dz_mm"]) for e in block["events"]] == [("iris_block", "blocked", -3.0)]
    assert block["end"] == "blocked"
    assert [(e["component"], e["action"], e["dz_mm"]) for e in through["events"]] == [("iris_open", "transmit", -2.5),
                                                                                      ("det3", "stop", 0.0)]


@pytest.mark.parametrize("trace", both("height_mixed"), ids=["now", "fixture"])
def test_the_mixed_case(trace):
    t, r = branch(trace, "las1", "0.t"), branch(trace, "las1", "0.r")
    assert branch(trace, "las1")["events"][0]["dz_mm"] == 3.0                    # BS1 at 75, the beam at 78
    # transmitted: M_B (300 mm) and M_A (75) at one place: M_A is hit, M_B is not a pass (it is at the hit point)
    assert t["segments"][0]["passes"] == [] and t["events"][0]["component"] == "m_a"
    assert (t["events"][1]["component"], t["events"][1]["action"], t["end"]) == ("iris1", "blocked", "blocked")
    # reflected: under M_P, up P1 (75 -> 300 with the offset of +3), on to D_S
    assert r["segments"][0]["passes"] == [{"component": "m_p", "x": 150.0, "y": 180.0, "dz_mm": -122.0}]
    assert [s["z_mm"] for s in r["segments"]] == [78.0, periscope_exit(78.0, 75.0, 300.0), 303.0]
    assert [e["dz_mm"] for e in r["events"]] == [3.0, 3.0, 3.0]
    assert r["events"][-1]["path_mm"] == 100.0 + 150.0 + 225.0 + 250.0 + 650.0
    d = r["events"][-1]["beam"]["d_mm"]
    assert d == pytest.approx(diameter(1375.0, 1.5, 785.0), abs=EPS)
    assert 3.0 + d / 2 - 3.0 == pytest.approx(0.878838, abs=5e-7)               # the overflow in height of the summary
    # the near miss at the component's height only
    doc = fixture("height_mixed")
    assert [m["component"] for m in near_misses(doc["layout"], trace)] == ["m_nm2"]
    assert doc["expected_misses"] == near_misses(doc["layout"], trace)


# ── the rules ─────────────────────────────────────────────────────────────────────────────────


def test_the_hit_rule_is_half_the_aperture_in_height_with_the_plane_tolerance():
    """|dz| <= A/2 + 1e-9 is a hit, 1e-8 further is a pass (heights below 0.01 mm reach the tracer only unrounded)."""
    def first(z):
        lay = Layout.model_validate(layout([laser("las1", 0, 100), mirror("m1", 100, 100, 135, props={"z_mm": 75}),
                                            det("d", 100, 300, 270)]))
        lay.components[1].props["z_mm"] = z
        return T.trace_layout(lay)[0]["branches"][0]

    assert first(75 + 12.7)["events"][0]["component"] == "m1"
    assert first(75 + 12.7 + 5e-10)["events"][0]["component"] == "m1"
    br = first(75 + 12.7 + 1e-8)
    assert br["events"] == [] and br["segments"][0]["passes"][0]["component"] == "m1"


def test_a_pass_is_no_event_and_lights_nothing():
    lay = layout([laser("las1", 0, 100), mirror("m1", 100, 100, 135, props={"z_mm": 200}), det("d", 300, 100, 180)])
    br = trace_layout(lay)[0]["branches"][0]
    assert [e["component"] for e in br["events"]] == ["d"] and br["events"][0]["path_mm"] == 300.0
    assert T.lit_components(trace_layout(lay)) == {"d"}


def test_only_the_passes_before_the_hit_belong_to_the_segment():
    """A pass between the source and the hit is recorded; one beyond the hit is not (the beam ends at D)."""
    lay = layout([laser("las1", 0, 100), mirror("before", 50, 100, 135, props={"z_mm": 200}),
                  det("d", 100, 100, 180), mirror("after", 150, 100, 135, props={"z_mm": 200})])
    br = trace_layout(lay)[0]["branches"][0]
    assert [p["component"] for p in br["segments"][0]["passes"]] == ["before"]


def test_dz_keeps_its_hundredths():
    """Heights are on a 0.01 mm grid, so a dz has hundredths: an event's and a pass's dz_mm keep them, rounded to
    0.001 mm like every length of the trace (75 - 75.07 is -0.06999999999999318 in floating point)."""
    lay = layout([laser("las1", 0, 100), mirror("m1", 100, 100, 135, props={"z_mm": 75.25}),
                  det("over", 100, 200, 270, aperture=25.4, props={"z_mm": 87.75}),
                  det("d", 100, 300, 270, props={"z_mm": 75.07})])
    br = trace_layout(lay)[0]["branches"][0]
    assert [(e["component"], e["z_mm"], e["dz_mm"]) for e in br["events"]] == [("m1", 75.0, -0.25), ("d", 75.0, -0.07)]
    assert br["segments"][1]["passes"] == [{"component": "over", "x": 100.0, "y": 200.0, "dz_mm": -12.75}]


def test_passes_at_one_point_go_by_id_whatever_the_layout_order():
    """Two components at the very same place and height: one point along the ray, ordered by id (code points)."""
    for order in (("b_mirror", "a_mirror"), ("a_mirror", "b_mirror")):
        comps = [laser("las1", 0, 100)] + [mirror(cid, 100, 100, 135, props={"z_mm": 200}) for cid in order]
        br = trace_layout(layout(comps + [det("d", 300, 100, 180)]))[0]["branches"][0]
        assert [p["component"] for p in br["segments"][0]["passes"]] == ["a_mirror", "b_mirror"], order
    # upper case before lower case, digits before letters: the code points, not a locale
    comps = [laser("las1", 0, 100)] + [mirror(cid, 100, 100, 135, props={"z_mm": 200}) for cid in ("m", "M", "1")]
    br = trace_layout(layout(comps + [det("d", 300, 100, 180)]))[0]["branches"][0]
    assert [p["component"] for p in br["segments"][0]["passes"]] == ["1", "M", "m"]


def test_collinear_surfaces_passed_at_one_point_go_by_id_too():
    """Two 40 mm detectors whose surfaces are collinear and overlap, crossed at one point (their t differ in the last
    bit): one run, by id - in both layout orders (the case of the fixture nearest_hit_ties, at another height)."""
    for order in (("b", "a"), ("a", "b")):
        dets = {"a": det("a", 412.5, 87.5, 225, aperture=40, props={"z_mm": 300}),
                "b": det("b", 405.43, 94.57, 225, aperture=40, props={"z_mm": 300})}
        lay = layout([laser("las1", 300, 100, 0)] + [dets[k] for k in order])
        br = trace_layout(lay)[0]["branches"][0]
        assert [p["component"] for p in br["segments"][0]["passes"]] == ["a", "b"], order


def test_the_passes_of_a_trace_stop_at_their_cap_and_the_geometry_does_not_change():
    """Two mirrors bounce a beam 64 times across 397 windows at another height: 64 x 397 passes, of which the trace
    keeps MAX_TRACE_PASSES; the events are those of the same layout without the windows."""
    base = [laser("las1", 15, 100), mirror("ma", 2000, 100, 180), mirror("mb", 10, 100, 0)]
    windows = [comp(f"w{i}", "window", 20 + 4.9 * i, 100, 0, props={"z_mm": 500}) for i in range(397)]
    full = trace_layout(layout(base + windows, width=2100.0))
    bare = trace_layout(layout(base, width=2100.0))
    passes = sum(len(s["passes"]) for t in full for b in t["branches"] for s in b["segments"])
    assert passes == MAX_TRACE_PASSES == 5000
    strip = lambda tr: [[(e["component"], e["path_mm"]) for b in t["branches"] for e in b["events"]] for t in tr]  # noqa: E731
    assert strip(full) == strip(bare)


def test_a_near_miss_is_only_at_the_components_height():
    beside = layout([laser("las1", 0, 100), mirror("m1", 200, 115, 135), det("d", 400, 100, 180)])
    assert [m["component"] for m in near_misses(beside, trace_layout(beside))] == ["m1"]
    high = copy.deepcopy(beside)
    high["components"][1]["props"] = {"z_mm": 200}
    assert near_misses(high, trace_layout(high)) == []
    # at the edge of the height (|dz| = A/2) it still counts
    edge = copy.deepcopy(beside)
    edge["components"][1]["props"] = {"z_mm": 87.7}
    assert [m["component"] for m in near_misses(edge, trace_layout(edge))] == ["m1"]


def test_a_periscope_is_entered_at_its_exit_from_the_other_side():
    """Light against the periscope's +x enters at z_out_mm; at the height of z_mm it is not at the entrance and
    passes over it (contract 3D §3.4: only the entrance side is judged)."""
    back = layout([comp("las1", "laser", 400, 100, 180, props={"wavelength_nm": 532, "z_mm": 250}),
                   comp("p", "periscope", 200, 100, 0, props={"z_out_mm": 250}), det("d", 50, 100, 0)])
    br = trace_layout(back)[0]["branches"][0]
    assert [(e["component"], e["z_mm"], e["dz_mm"]) for e in br["events"]] == [("p", 250.0, 0.0), ("d", 75.0, 0.0)]
    wrong = copy.deepcopy(back)
    wrong["components"][0]["props"]["z_mm"] = 75
    br = trace_layout(wrong)[0]["branches"][0]
    assert br["segments"][0]["passes"] == [{"component": "p", "x": 200.0, "y": 100.0, "dz_mm": -175.0}]


# ── what the heights must not change ──────────────────────────────────────────────────────────


def _geometry(trace: list) -> list:
    out = []
    for t in trace:
        for b in t["branches"]:
            out.append((t["source"], b["branch"], b["end"], b["fraction"],
                        [(s["x0"], s["y0"], s["x1"], s["y1"], s["beam"]) for s in b["segments"]],
                        [{k: v for k, v in e.items() if k not in ("z_mm", "dz_mm")} for e in b["events"]]))
    return out


@pytest.mark.parametrize("name", ["pol_hwp_rotates", "gauss_kepler_3x", "beam_splitter_cascade", "fiber_coupling_continues",
                                  "iris_open_diameter", "nearest_hit_ties"])
def test_a_beam_height_alone_changes_no_geometry(name):
    doc = fixture(name)
    raised = copy.deepcopy(doc["layout"])
    raised["table"]["beam_height_mm"] = 180.0
    a, b = trace_layout(doc["layout"]), trace_layout(raised)
    assert _geometry(a) == _geometry(b)
    assert {s["z_mm"] for t in b for br in t["branches"] for s in br["segments"]} == {180.0}


@pytest.mark.parametrize("name", ["height_mixed", "height_periscope_down", "height_source_heights", "height_passes_over_and_under"])
def test_moving_every_height_together_changes_nothing_but_the_heights(name):
    doc = fixture(name)
    moved = copy.deepcopy(doc["layout"])
    moved["table"]["beam_height_mm"] = moved["table"].get("beam_height_mm", 75.0) + 40.0
    for c in moved["components"]:
        for k in ("z_mm", "z_out_mm"):
            if k in c["props"]:
                c["props"][k] += 40.0
    a, b = trace_layout(doc["layout"]), trace_layout(moved)
    assert _geometry(a) == _geometry(b)
    za = [s["z_mm"] for t in a for br in t["branches"] for s in br["segments"]]
    zb = [s["z_mm"] for t in b for br in t["branches"] for s in br["segments"]]
    assert [round(x + 40.0, 3) for x in za] == zb
    pa = [p for t in a for br in t["branches"] for s in br["segments"] for p in s["passes"]]
    pb = [p for t in b for br in t["branches"] for s in br["segments"] for p in s["passes"]]
    assert pa == pb


def test_nearest_hit_without_a_height_is_at_the_tables_beam_height():
    world = T._World(Layout.model_validate({**layout([mirror("m1", 100, 100, 180, props={"z_mm": 200})]),
                                            "table": {"beam_height_mm": 200}}))
    assert world.nearest_hit(0, 100, 1.0, 0.0, None) is not None
    assert world.nearest_hit(0, 100, 1.0, 0.0, None, 75.0) is None


# ── the picture for an agent (render.py, contract 3D §8) ──────────────────────────────────────


def test_the_png_labels_give_heights_only_where_there_is_something_to_say():
    from mast.optical_layout.render import _height_suffix, render_png

    lay = Layout.model_validate(layout([mirror("m", 0, 0, 135), mirror("hi", 0, 0, 135, props={"z_mm": 120.5}),
                                        comp("up", "periscope", 0, 0, 0, props={"z_out_mm": 250}),
                                        comp("down", "periscope", 0, 0, 0, props={"z_mm": 200, "z_out_mm": 60}),
                                        comp("flat", "periscope", 0, 0, 0, props={"z_mm": 90})]))
    m, hi, up, down, flat = lay.components
    assert [_height_suffix(c, 75.0) for c in (m, hi, up, down, flat)] == ["", " z120.5", " ▲250", " ▼60", " z90"]
    assert _height_suffix(hi, 120.5) == ""
    png = render_png(fixture("height_mixed")["layout"], width_px=400)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_passes_are_in_the_order_along_the_ray_not_of_their_ids():
    """By t first: an id that sorts later but sits nearer comes first (only a run within 1e-9 goes by id)."""
    lay = layout([laser("las1", 0, 100), mirror("z_near", 100, 100, 135, props={"z_mm": 200}),
                  mirror("a_far", 200, 100, 135, props={"z_mm": 200}), det("d", 300, 100, 180)])
    br = trace_layout(lay)[0]["branches"][0]
    assert [p["component"] for p in br["segments"][0]["passes"]] == ["z_near", "a_far"]
