"""The tracer against physics worked out independently of its own formulas.

The fixtures' expected traces were produced by the implementation; this file is how they were
(and stay) checked: reflections are re-derived with the angle form of the law of reflection and a
2x2 linear solve (the tracer uses dot products), the standard folds are asserted as directions,
and the geometric edge cases of the contract are pinned one by one: aperture end points, parallel
incidence, zero-length rays, the self-hit guard, ties, the event cap, lineage of the cap.
"""
from __future__ import annotations

import math
import random
import time

import numpy as np
import pytest

from mast.optical_layout import trace as T
from mast.optical_layout.model import Layout
from mast.optical_layout.trace import near_misses, trace_layout

from ._layouts import comp, det, laser, layout, mirror, validated


def events(tr, tree=0, branch=0):
    return tr[tree]["branches"][branch]["events"]


def direction(seg):
    dx, dy = seg["x1"] - seg["x0"], seg["y1"] - seg["y0"]
    n = math.hypot(dx, dy)
    return dx / n, dy / n


# ── the standard folds ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("src_angle, mirror_angle, out", [
    (0, 135, (0, 1)),       # the contract's own example: +x folded to +y
    (0, 45, None),          # back side: the beam is blocked, nothing leaves
    (0, 225, (0, -1)),      # +x folded to -y
    (90, 315, (1, 0)),      # +y folded to +x
    (90, 225, (-1, 0)),     # +y folded to -x
    (180, 45, (0, 1)),      # -x folded to +y
    (270, 135, (-1, 0)),    # -y folded to -x
    (0, 180, (-1, 0)),      # normal incidence: straight back
])
def test_a_mirror_turns_the_beam_by_the_law_of_reflection(src_angle, mirror_angle, out):
    """d' = d - 2 (d.n) n, with the working sides the contract defines (n = the way the
    reflecting side faces)."""
    lay = layout([laser("las1", 200, 200, src_angle), mirror("m1", 200 + 100 * math.cos(math.radians(src_angle)),
                                                           200 + 100 * math.sin(math.radians(src_angle)), mirror_angle)])
    br = trace_layout(lay)[0]["branches"][0]
    if out is None:
        assert br["end"] == "back_hit" and br["events"][0]["action"] == "back_hit"
        return
    assert br["events"][0]["action"] == "reflect"
    dx, dy = direction(br["segments"][1])
    assert (round(dx, 6), round(dy, 6)) == (out[0], out[1]), (dx, dy)


def test_the_contracts_example_fold_sends_the_beam_up_the_table():
    tr = trace_layout(layout([laser("las1", 50, 62.5), mirror("m1", 112.5, 62.5, 135)]))
    ev = events(tr)[0]
    assert (ev["x"], ev["y"], ev["offset_mm"], ev["incidence_deg"], ev["path_mm"]) == (
        112.5, 62.5, 0.0, 45.0, 62.5)
    last = tr[0]["branches"][0]["segments"][-1]
    assert (last["x0"], last["y0"]) == (112.5, 62.5) and last["x1"] == 112.5 and last["y1"] > 62.5


def test_a_mirror_facing_away_blocks_with_a_back_hit_not_a_reflection():
    br = trace_layout(layout([laser("las1", 50, 62.5), mirror("m1", 112.5, 62.5, 315)]))[0]["branches"][0]
    assert br["end"] == "back_hit" and [e["action"] for e in br["events"]] == ["back_hit"]


# ── an independent oracle on random single reflections ───────────────────────────────────────


def _oracle_hit(p, d, c, surf):
    """P + t d = C + s u  ->  (t, s)."""
    t, s = np.linalg.solve(np.column_stack([d, -surf]), c - p)
    return t, s


def test_random_reflections_agree_with_the_angle_form_of_the_law():
    """200 random lasers / mirrors / detectors: where the tracer says the beam hit, the offset on
    the mirror, the outgoing direction and the second hit - against numpy and angle arithmetic."""
    rng = random.Random(20261103)
    checked = 0
    while checked < 200:
        theta = rng.uniform(0, 360)
        px, py = rng.uniform(300, 900), rng.uniform(300, 600)
        dist = rng.uniform(60, 300)
        lateral = rng.uniform(-8, 8)
        # the mirror's normal faces back along the beam, turned by delta (incidence |delta|)
        delta = rng.choice((-1, 1)) * rng.uniform(5, 75)
        nu = (theta + 180 + delta) % 360
        u = np.array([-math.sin(math.radians(nu)), math.cos(math.radians(nu))])
        hit = np.array([px, py]) + dist * np.array([math.cos(math.radians(theta)), math.sin(math.radians(theta))])
        centre = hit - lateral * u
        lay = Layout.model_validate(layout([laser("las1", px, py, theta), mirror("m1", float(centre[0]), float(centre[1]), nu)]))
        m = lay.component("m1")
        l = lay.component("las1")
        # the oracle works from the *normalised* numbers, as the tracer does
        p = np.array([l.x_mm, l.y_mm])
        d = np.array([math.cos(math.radians(l.angle_deg)), math.sin(math.radians(l.angle_deg))])
        c = np.array([m.x_mm, m.y_mm])
        surf = np.array([-math.sin(math.radians(m.angle_deg)), math.cos(math.radians(m.angle_deg))])
        t, s = _oracle_hit(p, d, c, surf)
        refl = (2 * m.angle_deg + 180 - l.angle_deg) % 360
        d2 = np.array([math.cos(math.radians(refl)), math.sin(math.radians(refl))])
        h = p + t * d
        # a big wall of a detector across the reflected beam
        wall_c = h + 400 * d2
        lay = Layout.model_validate({**lay.to_dict(), "components": [
            *[c_.model_dump(mode="json") for c_ in lay.components],
            det("wall", float(wall_c[0]), float(wall_c[1]), (refl + 180) % 360, aperture=400).copy()]})
        wall = lay.component("wall")
        wn = math.radians(wall.angle_deg)
        t2, s2 = _oracle_hit(h, d2, np.array([wall.x_mm, wall.y_mm]), np.array([-math.sin(wn), math.cos(wn)]))
        tr = trace_layout(lay)
        br = tr[0]["branches"][0]
        ev = br["events"]
        assert [e["component"] for e in ev] == ["m1", "wall"], (theta, nu, ev)
        assert abs(ev[0]["x"] - h[0]) < 6e-4 and abs(ev[0]["y"] - h[1]) < 6e-4
        assert abs(ev[0]["offset_mm"] - s) < 6e-4
        assert abs(ev[0]["incidence_deg"] - abs(delta)) < 0.01
        h2 = h + t2 * d2
        assert abs(ev[1]["x"] - h2[0]) < 6e-4 and abs(ev[1]["y"] - h2[1]) < 6e-4
        assert abs(ev[1]["offset_mm"] - s2) < 6e-4
        assert abs(ev[1]["path_mm"] - (t + t2)) < 6e-4
        checked += 1


# ── aperture end points, parallel incidence, zero length ─────────────────────────────────────


def _vertical_mirror_hit(y_beam, aperture=25.4):
    """A mirror at (100, 100) facing -x (angle 180), a beam along +x at height y_beam (given
    unrounded: the model would round it to 0.01 mm)."""
    lay = validated(layout([laser("las1", 0, 100), mirror("m1", 100, 100, 180, aperture=aperture)]),
                    las1__y_mm=y_beam)
    return [e["component"] for e in events(trace_layout(lay))]


def test_the_aperture_end_points_belong_to_the_aperture():
    half = 25.4 / 2
    assert _vertical_mirror_hit(100 + half) == ["m1"]
    assert _vertical_mirror_hit(100 - half) == ["m1"]
    assert _vertical_mirror_hit(100 + half + 5e-10) == ["m1"], "the 1e-9 tolerance"
    assert _vertical_mirror_hit(100 + half + 2e-9) == []
    assert _vertical_mirror_hit(100 - half - 2e-9) == []
    assert _vertical_mirror_hit(100 + 13) == [], "just outside"


def test_the_offset_is_signed_along_the_local_y_axis():
    for y, expect in ((104, -4.0), (96, 4.0)):       # angle 180: local +y points down (-y)
        tr = trace_layout(layout([laser("las1", 0, y), mirror("m1", 100, 100, 180)]))
        assert events(tr)[0]["offset_mm"] == expect
    for y, expect in ((104, 4.0), (96, -4.0)):       # angle 0: local +y points up
        tr = trace_layout(layout([laser("las1", 300, y, 180), mirror("m1", 100, 100, 0)]))
        assert events(tr)[0]["offset_mm"] == expect


def test_a_surface_parallel_to_the_beam_is_never_hit():
    lay = layout([laser("las1", 0, 100), mirror("m1", 100, 100, 90, aperture=500)])
    assert events(trace_layout(lay)) == []
    # the other way: a beam along the surface of a large detector
    assert events(trace_layout(layout([laser("las1", 0, 100), det("d", 100, 100, 90, aperture=500)]))) == []


def test_parallel_means_a_dot_product_below_one_billionth():
    base = layout([laser("las1", 0, 100), mirror("m1", 100, 100, 90, aperture=500)])
    tiny = validated(base, las1__angle_deg=math.degrees(5e-10))           # |d.n| = 5e-10: parallel
    assert events(trace_layout(tiny)) == []
    bigger = validated(base, las1__angle_deg=math.degrees(5e-9))          # 5e-9: a (very grazing) hit
    ev = events(trace_layout(bigger))
    assert [e["component"] for e in ev] == ["m1"] and ev[0]["incidence_deg"] == 90.0


def test_a_ray_that_starts_on_a_surface_does_not_hit_it():
    """Zero distance: ``t`` must exceed 1e-6."""
    base = layout([laser("las1", 100, 100), mirror("m1", 100, 100, 180), det("d", 200, 100, 180)])
    assert [e["component"] for e in events(trace_layout(base))] == ["d"]
    close = validated(base, las1__x_mm=100 - 5e-7)
    assert [e["component"] for e in events(trace_layout(close))] == ["d"], "closer than 1e-6 is no hit"
    far = validated(base, las1__x_mm=100 - 5e-6)
    assert [e["component"] for e in events(trace_layout(far))][:1] == ["m1"], "but 5e-6 is"


def test_a_ray_starting_on_the_boundary_moving_out_has_zero_length():
    lay = layout([laser("las1", 2200, 100, 0)])        # 1200 + 1000
    seg = trace_layout(lay)[0]["branches"][0]["segments"]
    assert seg == [{"x0": 2200.0, "y0": 100.0, "x1": 2200.0, "y1": 100.0}]
    # moving in, from exactly the boundary, it travels the whole way across
    seg = trace_layout(layout([laser("las1", 2200, 100, 180)]))[0]["branches"][0]["segments"]
    assert seg[0]["x1"] == -1000.0


def test_the_ray_leaves_through_the_nearest_edge_and_stops_at_ten_metres():
    br = trace_layout(layout([laser("las1", 600, 450, 45)]))[0]["branches"][0]
    seg = br["segments"][0]
    assert br["end"] == "left_table" and seg["y1"] == 1900.0 and abs(seg["x1"] - 2050.0) < 1e-3
    br = trace_layout(layout([laser("las1", 0, 0, 0)], width=20000.0))[0]["branches"][0]
    assert br["segments"][0]["x1"] == 10000.0 and br["end"] == "left_table"


def test_a_hit_beyond_the_ten_metre_limit_is_not_reached():
    lay = layout([laser("las1", 0, 100), det("d", 12000, 100, 180)], width=20000.0)
    assert trace_layout(lay)[0]["branches"][0]["end"] == "left_table"
    lay = layout([laser("las1", 0, 100), det("d", 9999, 100, 180)], width=20000.0)
    assert trace_layout(lay)[0]["branches"][0]["end"] == "stop"


# ── ties, the self-hit guard, the cap ────────────────────────────────────────────────────────


def test_the_nearest_wins_and_among_equals_the_first_in_the_list():
    window_first = layout([laser("las1", 0, 100), comp("win", "window", 100, 100, 180), mirror("m1", 100, 100, 180)])
    ev = events(trace_layout(window_first))
    assert [(e["component"], e["action"]) for e in ev] == [("win", "transmit")], "the mirror at distance 0 is ignored"
    mirror_first = layout([laser("las1", 0, 100), mirror("m1", 100, 100, 180), comp("win", "window", 100, 100, 180)])
    first = events(trace_layout(mirror_first))[0]
    assert first["component"] == "m1" and first["action"] == "reflect", "the earlier component wins a tie"
    nearer = layout([laser("las1", 0, 100), mirror("m1", 100.5, 100, 180), comp("win", "window", 100, 100, 180)])
    assert events(trace_layout(nearer))[0]["component"] == "win", "but a nearer one always wins"


def _tie_pair(order: str, beam_y: float, angle: float = 225.0) -> str:
    """The component a beam along +x hits first, with two 40 mm detectors whose surfaces are collinear and
    overlap (centres 10 mm apart along the surface, both on x + y = 200) listed in *order*."""
    pair = {"A": det("a", 100.0, 100.0, angle, aperture=40.0), "B": det("b", 110.0, 90.0, angle, aperture=40.0)}
    lay = layout([laser("las1", 10.0, beam_y, 0.0)] + [pair[k] for k in order])
    return events(trace_layout(lay))[0]["component"]


def test_a_tie_between_overlapping_collinear_surfaces_goes_to_the_earlier_component_whatever_the_rounding_does():
    """The beam crosses both surfaces at one point; ((C-P).n)/(d.n) differs in its last bit and the nearer-by-
    noise component used to win (in about 6% of these cases the later one of B, A; and differently in the front
    end's JavaScript). The list order is the rule: t within EPS_TIE is a tie."""
    wrong = []
    for i in range(1, 400):
        y = round(100.0 - i * 0.02, 2)                       # the crossing stays inside both apertures
        for order in ("AB", "BA"):
            if _tie_pair(order, y) != order[0].lower():
                wrong.append((order, y))
    assert wrong == [], wrong[:5]


def test_the_tie_tolerance_is_a_nanometre_wide_and_a_real_difference_is_never_a_tie():
    assert T.EPS_TIE == 1e-9
    base = layout([laser("las1", 0, 100), det("far", 300, 100, 180), det("near", 300, 100, 180)])
    # 0.5e-9 mm nearer: a tie, the earlier in the list (far) wins; 2e-9 mm nearer: the nearer one wins
    tie = validated(base, far__x_mm=300.0, near__x_mm=300.0 - 5e-10)
    assert events(trace_layout(tie))[0]["component"] == "far"
    nearer = validated(base, far__x_mm=300.0, near__x_mm=300.0 - 2e-9)
    assert events(trace_layout(nearer))[0]["component"] == "near"
    other_way = validated(base, far__x_mm=300.0 - 5e-10, near__x_mm=300.0)       # the earlier one is nearer by 0.5e-9
    assert events(trace_layout(other_way))[0]["component"] == "far"
    # and the hundredth of a millimetre the editor can place is far above it
    real = layout([laser("las1", 0, 100), det("far", 300.01, 100, 180), det("near", 300.0, 100, 180)])
    assert events(trace_layout(real))[0]["component"] == "near"


def test_the_self_hit_guard_applies_below_a_thousandth_of_a_millimetre_only():
    """White box: ``nearest_hit`` ignores the component the ray has just acted on for
    ``t < 1e-3``, and no other."""
    world = T._World(Layout.model_validate(layout([mirror("m1", 100, 100, 180)])))
    m = world.comps[0]
    assert world.nearest_hit(100 - 5e-4, 100, 1.0, 0.0, None) is not None
    assert world.nearest_hit(100 - 5e-4, 100, 1.0, 0.0, m) is None
    assert world.nearest_hit(100 - 5e-3, 100, 1.0, 0.0, m) is not None


def test_a_grazing_reflection_does_not_hit_its_own_mirror_again():
    """Nearly parallel to the surface, in and out (incidence 89.99999999 degrees): one event."""
    lay = validated(layout([laser("las1", 0, 100.0, 0.0), mirror("m1", 400, 100, 270, aperture=500)]),
                    m1__angle_deg=270 - 1e-7)
    ev = events(trace_layout(lay))
    assert [e["component"] for e in ev] == ["m1"], ev
    assert ev[0]["incidence_deg"] > 89.999


def test_the_cap_on_events_ends_the_branch_after_the_64th_and_a_terminal_wins_over_it():
    """62 windows, then a detector as the 63rd event: ordinary stop. A 64th event that is
    not terminal ends the branch with max_events; a 64th that is terminal is a stop."""
    chain = [comp(f"w{i}", "window", 100 + 10 * i, 100, 0) for i in range(63)]
    br = trace_layout(layout([laser("las1", 0, 100), *chain]))[0]["branches"][0]
    assert len(br["events"]) == 63 and br["end"] == "left_table"
    chain.append(comp("w63", "window", 100 + 10 * 63, 100, 0))
    br = trace_layout(layout([laser("las1", 0, 100), *chain]))[0]["branches"][0]
    assert len(br["events"]) == 64 and br["end"] == "max_events" and len(br["segments"]) == 64
    chain[-1] = det("d", 100 + 10 * 63, 100, 180)
    br = trace_layout(layout([laser("las1", 0, 100), *chain]))[0]["branches"][0]
    assert len(br["events"]) == 64 and br["end"] == "stop"


def test_children_of_a_split_inherit_the_event_count_of_their_parent():
    chain = [comp(f"w{i}", "window", 100 + 10 * i, 100, 0) for i in range(62)]
    bs = comp("bs", "beam_splitter", 100 + 10 * 62, 100, 135)               # the 63rd event
    tail_t = comp("wt", "window", 800, 100, 0)                               # the 64th of the .t child
    tail_r = det("dr", 100 + 10 * 62, 300, 270)                              # the 64th of the .r child
    tr = trace_layout(layout([laser("las1", 0, 100), *chain, bs, tail_t, tail_r]))
    by = {b["branch"]: b for b in tr[0]["branches"]}
    assert by["0"]["end"] == "split" and len(by["0"]["events"]) == 63
    assert by["0.t"]["end"] == "max_events" and [e["component"] for e in by["0.t"]["events"]] == ["wt"]
    assert by["0.r"]["end"] == "stop" and [e["component"] for e in by["0.r"]["events"]] == ["dr"]


def test_a_split_that_is_the_64th_event_ends_the_branch_instead_of_splitting():
    chain = [comp(f"w{i}", "window", 100 + 10 * i, 100, 0) for i in range(63)]
    bs = comp("bs", "beam_splitter", 100 + 10 * 63, 100, 135)
    br = trace_layout(layout([laser("las1", 0, 100), *chain, bs]))[0]
    assert [b["branch"] for b in br["branches"]] == ["0"] and br["branches"][0]["end"] == "max_events"
    assert br["branches"][0]["events"][-1]["action"] == "split"


# ── splitting arithmetic ─────────────────────────────────────────────────────────────────────


def test_fractions_multiply_down_the_tree_and_add_up():
    lay = layout([laser("las1", 0, 100), comp("bs1", "beam_splitter", 100, 100, 135, props={"reflect_fraction": 0.3}),
                  comp("bs2", "beam_splitter", 200, 100, 135, props={"reflect_fraction": 0.6}),
                  det("a", 100, 300, 270), det("b", 200, 300, 270), det("c", 400, 100, 180)])
    br = {b["branch"]: b for b in trace_layout(lay)[0]["branches"]}
    assert [round(br[k]["fraction"], 6) for k in ("0.r", "0.t.r", "0.t.t")] == [0.3, 0.42, 0.28]
    assert abs(sum(br[k]["fraction"] for k in ("0.r", "0.t.r", "0.t.t")) - 1.0) < 1e-9
    assert [b["branch"] for b in trace_layout(lay)[0]["branches"]] == ["0", "0.r", "0.t", "0.t.r", "0.t.t"]


def test_a_branch_at_exactly_the_cutoff_is_kept_and_just_below_is_dropped():
    def ids(r):
        lay = layout([laser("las1", 0, 100), comp("bs", "beam_splitter", 100, 100, 135, props={"reflect_fraction": r}),
                      det("a", 100, 300, 270), det("b", 300, 100, 180)])
        return [b["branch"] for b in trace_layout(lay)[0]["branches"]]

    assert ids(0.001) == ["0", "0.r", "0.t"]                 # 0.001 is not below 1e-3
    assert ids(0.0009) == ["0", "0.t"]
    assert ids(0.999) == ["0", "0.r", "0.t"]                 # 1 - 0.999 = 0.001000...
    assert ids(0.9991) == ["0", "0.r"]
    assert ids(1.0) == ["0", "0.r"] and ids(0.0) == ["0", "0.t"]


def test_the_dichroic_reflects_short_by_default():
    lay = layout([laser("las1", 0, 100, wl=[500, 900]), comp("dm", "dichroic", 100, 100, 135, props={"edge_nm": 700}),
                  det("up", 100, 300, 270), det("through", 300, 100, 180)])
    tr = trace_layout(lay)
    assert [e["action"] for e in events(tr, 0)] == ["reflect", "stop"] and tr[0]["wavelength_nm"] == 500
    assert [e["action"] for e in events(tr, 1)] == ["transmit", "stop"]


# ── fibres ───────────────────────────────────────────────────────────────────────────────────


def test_a_fibre_output_that_is_continued_never_emits_by_itself():
    lay = layout([laser("las1", 0, 100), comp("fi", "fiber_in", 100, 100, 180, props={"continues_at": "fo"}),
                  comp("fo", "fiber_out", 100, 300, 0, props={"wavelength_nm": 450}), det("d", 300, 300, 180)])
    tr = trace_layout(lay)
    assert [(t["source"], t["wavelength_nm"]) for t in tr] == [("las1", 532.0), ("fo", 532.0)]
    dark = layout([comp("fi", "fiber_in", 100, 100, 180, props={"continues_at": "fo"}),
                   comp("fo", "fiber_out", 100, 300, 0, props={"wavelength_nm": 450}), det("d", 300, 300, 180)])
    assert trace_layout(dark) == [], "nothing goes in, nothing comes out"


def test_a_dangling_or_wrong_continuation_is_ignored():
    for target in ("nobody", "las1"):
        lay = layout([laser("las1", 0, 100), comp("fi", "fiber_in", 100, 100, 180, props={"continues_at": target})])
        tr = trace_layout(lay)
        assert [t["source"] for t in tr] == ["las1"] and tr[0]["branches"][0]["end"] == "stop"


def test_a_beam_on_the_back_of_a_fibre_coupler_is_not_coupled():
    lay = layout([laser("las1", 0, 100), comp("fi", "fiber_in", 100, 100, 0, props={"continues_at": "fo"}),
                  comp("fo", "fiber_out", 100, 300, 0, props={"wavelength_nm": 450})])
    tr = trace_layout(lay)
    assert len(tr) == 1 and tr[0]["branches"][0]["end"] == "back_hit"


# ── odds and ends ────────────────────────────────────────────────────────────────────────────


def test_tracing_changes_nothing_and_is_repeatable():
    raw = layout([laser("las1", 50, 62.5), mirror("m1", 112.5, 62.5, 135), det("d", 112.5, 300, 270)])
    import copy

    before = copy.deepcopy(raw)
    first = trace_layout(raw)
    assert raw == before and trace_layout(raw) == first


def test_sources_are_never_hit_and_do_not_block():
    lay = layout([laser("las1", 0, 100), laser("las2", 100, 100, 90), det("d", 300, 100, 180)])
    assert [e["component"] for e in events(trace_layout(lay), 0)] == ["d"]


def test_the_wavelength_list_gives_one_tree_each_in_the_listed_order():
    tr = trace_layout(layout([laser("las1", 0, 100, wl=[1064, 532, 633])]))
    assert [t["wavelength_nm"] for t in tr] == [1064.0, 532.0, 633.0]


def test_a_full_table_traces_quickly():
    """400 components, 32 sources: the interactive editor re-traces on every drag."""
    rng = random.Random(1)
    comps = [laser(f"s{i}", rng.uniform(0, 1200), rng.uniform(0, 900), rng.uniform(0, 360)) for i in range(32)]
    types = ("mirror", "beam_splitter", "lens", "window", "filter", "iris", "detector")
    for i in range(368):
        t = rng.choice(types)
        props = {"pass_min_nm": 400} if t == "filter" else {}
        comps.append(comp(f"c{i}", t, rng.uniform(0, 1200), rng.uniform(0, 900), rng.uniform(0, 360), props=props))
    lay = Layout.model_validate(layout(comps))
    started = time.perf_counter()
    tr = trace_layout(lay)
    elapsed = time.perf_counter() - started
    assert len(tr) == 32 and elapsed < 3.0, f"{elapsed:.2f} s"


def test_a_splitter_ping_pong_stays_bounded():
    """The worst shape for a tree: a splitter between two mirrors that keep sending the light
    back. The 1e-3 floor on fractions and the 64 events of a lineage bound the work."""
    lay = layout([laser("las1", 100, 100, 0), comp("bs", "beam_splitter", 300, 100, 180, props={"reflect_fraction": 0.5}),
                  mirror("ma", 600, 100, 180), mirror("mb", 20, 100, 0)])
    started = time.perf_counter()
    tr = trace_layout(lay)
    elapsed = time.perf_counter() - started
    assert elapsed < 10.0, f"{elapsed:.2f} s"
    assert all(len(b["events"]) <= 64 for b in tr[0]["branches"])
    assert len(tr[0]["branches"]) <= 1100


# ── the near-miss report ─────────────────────────────────────────────────────────────────────


def _near(y):
    lay = layout([laser("las1", 0, y), mirror("m1", 100, 100, 180), det("far", 400, y, 180)])
    return near_misses(lay, trace_layout(lay))


def test_a_near_miss_is_outside_the_aperture_but_within_one_aperture_of_the_centre():
    assert _near(100 + 12.7) == [], "on the edge: a hit, not a miss"
    miss = _near(100 + 12.7 + 1)
    assert [m["component"] for m in miss] == ["m1"]
    assert miss[0]["outside_mm"] == pytest.approx(1.0, abs=2e-3) and miss[0]["offset_mm"] == pytest.approx(-13.7, abs=2e-3)
    assert _near(100 + 25.4) != [] and _near(100 + 25.4 + 0.01) == []
    assert _near(100 - 20)[0]["offset_mm"] == pytest.approx(20.0, abs=2e-3), "signed like the hit offsets"


def test_no_miss_is_reported_for_a_surface_behind_the_source_or_beyond_the_end():
    behind = layout([laser("las1", 500, 113.7), mirror("m1", 100, 100, 180), det("d", 900, 113.7, 180)])
    assert near_misses(behind, trace_layout(behind)) == []
    beyond = layout([laser("las1", 0, 113.7), det("d", 50, 113.7, 180), mirror("m1", 100, 100, 180)])
    assert near_misses(beyond, trace_layout(beyond)) == []


def test_lit_components_are_those_with_an_event():
    lay = layout([laser("las1", 0, 100), comp("w", "window", 50, 100, 0), mirror("m2", 100, 500, 180), det("d", 300, 100, 180)])
    assert T.lit_components(trace_layout(lay)) == {"w", "d"}
