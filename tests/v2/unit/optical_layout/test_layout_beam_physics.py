"""The beam of the trace against physics worked out independently (contract optical_layout_v2.md §7).

Every anchor of §7 has a test here whose expected numbers are computed in the test by a different route
than the tracer's, and compared with both the tracer's output and the shared fixture that pins it:

* polarization by **Jones calculus** (complex field vectors in the beam's frame ``e_h = z x d``, ``e_v = z``)
  instead of the tracer's Mueller arithmetic, turned into Stokes numbers with the convention the
  contract's matrices use (``S3 = -2 Im(Ex conj(Ey))``; a retarder ``R(-t) diag(1, e^{i delta}) R(t)``,
  checked against the contract's ``W`` for random angles below); unpolarized light as the incoherent sum
  of two orthogonal halves; a mirror ``diag(1, -1)``;
* the axis of a part as the ray sees it from **3-D vectors of the physical part** (contract v2 §4.5): an
  absorbing polarizer passes the field perpendicular to its absorbing axis ``a`` and to the ray, ``d x a``;
  a positive uniaxial waveplate's fast (ordinary) wave is perpendicular to its optic axis ``c`` and to the
  ray, ``d x c`` - not the tracer's ``atan2`` shortcut;
* Gaussian beams by ``w(z) = w0 sqrt(1 + (z / zR)^2)`` and **Self's thin-lens imaging formula** for the
  waist behind a lens, not the ``1/q' = 1/q - 1/f`` algebra;
* power as the product of the numbers on the parts.
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

import numpy as np
import pytest

from mast.optical_layout import trace as T
from mast.optical_layout.trace import trace_layout

from ._layouts import comp, det, layout

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "optical_layout"
#: the output is rounded to 1e-9; a little more for the arithmetic
EPS = 3e-9


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def traced(name: str) -> tuple[list, list]:
    """``(the trace of the fixture's layout now, the fixture's expected trace)``: every check below is
    made on both."""
    doc = fixture(name)
    return trace_layout(doc["layout"]), doc["expected_trace"]


def event(trace: list, source: str, branch: str, component: str, wavelength=None) -> dict:
    for tree in trace:
        if tree["source"] != source or (wavelength is not None and tree["wavelength_nm"] != wavelength):
            continue
        for br in tree["branches"]:
            if br["branch"] == branch:
                for ev in br["events"]:
                    if ev["component"] == component:
                        return ev
    raise AssertionError(f"no event {source} {branch} {component}")


def branch(trace: list, source: str, bid: str) -> dict:
    tree = next(t for t in trace if t["source"] == source)
    return next(b for b in tree["branches"] if b["branch"] == bid)


# ── Jones calculus (the independent route for polarization) ─────────────────────────────────


def rot(t: float) -> np.ndarray:
    return np.array([[math.cos(t), math.sin(t)], [-math.sin(t), math.cos(t)]])


def j_linear(alpha_deg: float, power: float = 1.0) -> np.ndarray:
    a = math.radians(alpha_deg)
    return math.sqrt(power) * np.array([math.cos(a), math.sin(a)], dtype=complex)


def j_polarizer(theta_deg: float) -> np.ndarray:
    t = math.radians(theta_deg)
    v = np.array([math.cos(t), math.sin(t)])
    return np.outer(v, v).astype(complex)


def j_retarder(theta_deg: float, delta: float) -> np.ndarray:
    t = math.radians(theta_deg)
    return rot(-t) @ np.diag([1.0, np.exp(1j * delta)]) @ rot(t)


J_MIRROR = np.diag([1.0, -1.0]).astype(complex)
HALF, QUARTER = math.pi, math.pi / 2


def stokes(*fields: np.ndarray) -> np.ndarray:
    """The Stokes vector of the incoherent sum of *fields*."""
    total = np.zeros(4)
    for e in fields:
        ex, ey = e
        total += [abs(ex) ** 2 + abs(ey) ** 2, abs(ex) ** 2 - abs(ey) ** 2,
                  2 * (ex * np.conj(ey)).real, -2 * (ex * np.conj(ey)).imag]
    return total


def unpolarized(power: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    return j_linear(0, power / 2), j_linear(90, power / 2)


def expect_beam(ev_beam: dict, s: np.ndarray, *, mw_of=None) -> None:
    """The arrival (or segment) *ev_beam* holds the Stokes vector *s* (unnormalised)."""
    assert ev_beam["p"] == pytest.approx(s[0], abs=EPS)
    if s[0] < 1e-12:
        assert ev_beam["s"] is None
    else:
        assert ev_beam["s"] == pytest.approx(list(s[1:] / s[0]), abs=EPS)
    if mw_of is not None:
        assert ev_beam["mw"] == pytest.approx(mw_of * s[0], abs=1e-9)


Z3 = np.array([0.0, 0.0, 1.0])


def part_vectors(theta_deg: float, part_angle_deg: float, ray_angle_deg: float) -> dict:
    """The 3-D vectors of a polarizer or waveplate (axis ``theta`` in its own frame, from its local +y towards
    up) met by a ray along *ray_angle_deg*: ``d``; the ray's frame ``e_h = z x d``, ``e_v = z``; the part's
    face (``u``, ``z``) and normal ``n``; the axis given ``t = cos(theta) u + sin(theta) z`` (a polarizer's
    transmission axis, a waveplate's fast axis at normal incidence) and the one perpendicular to it in the
    face, ``a = -sin(theta) u + cos(theta) z`` (a polarizer's absorbing axis; a positive crystal's optic axis
    ``c``, its slow axis at normal incidence)."""
    a_, r, th = (math.radians(x) for x in (part_angle_deg, ray_angle_deg, theta_deg))
    u = np.array([-math.sin(a_), math.cos(a_), 0.0])
    n = np.array([math.cos(a_), math.sin(a_), 0.0])
    d = np.array([math.cos(r), math.sin(r), 0.0])
    return {"d": d, "e_h": np.cross(Z3, d), "e_v": Z3, "n": n, "u": u,
            "t": math.cos(th) * u + math.sin(th) * Z3, "a": -math.sin(th) * u + math.cos(th) * Z3}


def angle_in_ray_frame(v: np.ndarray, frame: dict) -> float:
    """The direction of a transverse vector *v*, from ``e_h`` towards ``e_v`` (degrees, [0, 180))."""
    return math.degrees(math.atan2(v @ frame["e_v"], v @ frame["e_h"])) % 180.0


def seen_axis(theta_deg: float, part_angle_deg: float, ray_angle_deg: float) -> float:
    """The axis the ray sees (degrees, [0, 180)): the transmitted field of an absorbing polarizer, or the
    ordinary (fast) wave of a positive uniaxial waveplate - both perpendicular to ``a`` (resp. ``c``) and to
    the ray, i.e. along ``d x a`` (contract v2 §4.5)."""
    v = part_vectors(theta_deg, part_angle_deg, ray_angle_deg)
    return angle_in_ray_frame(np.cross(v["d"], v["a"]), v)


def psi_deg(s: list) -> float:
    """The orientation of a linear polarization, in [0, 180)."""
    return (math.degrees(0.5 * math.atan2(s[1], s[0]))) % 180.0


# ── the convention itself ────────────────────────────────────────────────────────────────────


def test_the_jones_route_reproduces_the_contracts_retarder_matrix():
    """Not circular: the Jones retarder and the S3 sign above are one consistent convention, the same
    as the contract's W (§4.5) - checked on random states, axes and retardances."""
    rng = random.Random(7)
    for _ in range(200):
        th, delta = rng.uniform(-180, 180), rng.uniform(0, 2 * math.pi)
        e = np.array([complex(rng.gauss(0, 1), rng.gauss(0, 1)), complex(rng.gauss(0, 1), rng.gauss(0, 1))])
        s_in, s_out = stokes(e), stokes(j_retarder(th, delta) @ e)
        mueller = T._retarded(tuple(s_in), math.radians(th), math.cos(delta), math.sin(delta))
        assert np.allclose(mueller, s_out, atol=1e-12)
        assert np.allclose(T._mirrored(tuple(s_in)), stokes(J_MIRROR @ e), atol=1e-12)


# ── anchors A-E: polarization and power ──────────────────────────────────────────────────────


def test_anchor_a_malus_two_polarizers():
    for tr in traced("pol_malus_two_polarizers"):
        h = j_linear(0)
        after1 = j_polarizer(30) @ h
        after2 = j_polarizer(90) @ after1
        expect_beam(event(tr, "las1", "0", "pol2")["beam"], stokes(after1), mw_of=10.0)
        expect_beam(event(tr, "las1", "0", "det1")["beam"], stokes(after2), mw_of=10.0)
        assert stokes(after1)[0] == pytest.approx(math.cos(math.radians(30)) ** 2) == pytest.approx(0.75)
        assert stokes(after2)[0] == pytest.approx(0.75 * math.cos(math.radians(60)) ** 2) == pytest.approx(0.1875)
        assert psi_deg(event(tr, "las1", "0", "pol2")["beam"]["s"]) == pytest.approx(30.0, abs=1e-6)
        assert event(tr, "las1", "0", "det1")["beam"]["s"] == pytest.approx([-1.0, 0.0, 0.0], abs=EPS)


def test_anchor_b_a_half_wave_plate_turns_the_polarization_and_the_pbs_splits_it():
    for tr in traced("pol_hwp_rotates"):
        for src, hwp, pbs, axis in (("las1", "hwp1", "pbs1", 22.5), ("las2", "hwp2", "pbs2", 15.0)):
            e = j_retarder(axis, HALF) @ j_linear(0)
            expect_beam(event(tr, src, "0", pbs)["beam"], stokes(e), mw_of=20.0)
            assert psi_deg(event(tr, src, "0", pbs)["beam"]["s"]) == pytest.approx(2 * axis, abs=1e-6)
            t_field, r_field = j_polarizer(0) @ e, J_MIRROR @ j_polarizer(90) @ e
            st, sr = stokes(t_field), stokes(r_field)
            assert branch(tr, src, "0.t")["fraction"] == pytest.approx(st[0], abs=1e-6)
            assert branch(tr, src, "0.r")["fraction"] == pytest.approx(sr[0], abs=1e-6)
            expect_beam(branch(tr, src, "0.t")["segments"][0]["beam"], st)
            expect_beam(branch(tr, src, "0.r")["segments"][0]["beam"], sr)
        assert event(tr, "las1", "0", "pbs1")["beam"]["s"] == pytest.approx([0.0, 1.0, 0.0], abs=EPS)       # +45
        assert branch(tr, "las2", "0.t")["fraction"] == pytest.approx(0.75, abs=1e-6)                     # cos^2 30


def test_anchors_c_and_c2_the_quarter_wave_isolator():
    for tr in traced("pol_qwp_isolator"):
        h = j_linear(0)
        out = j_retarder(45, QUARTER) @ h
        expect_beam(event(tr, "las1", "0.t", "m1")["beam"], stokes(out), mw_of=5.0)
        assert stokes(out)[1:] == pytest.approx([0.0, 0.0, -1.0], abs=1e-12)              # C: chi = -45
        back = J_MIRROR @ out
        expect_beam(_second(tr, "qwp1")["beam"], stokes(back))                             # back at the plate
        seen = seen_axis(45, 0, 180)                                                       # the same plate from behind
        assert seen == pytest.approx(135.0, abs=1e-9)
        returned = j_retarder(seen, QUARTER) @ back
        assert stokes(returned)[1:] == pytest.approx([-1.0, 0.0, 0.0], abs=1e-12)          # C2: vertical
        expect_beam(_second(tr, "pbs1")["beam"], stokes(returned))
        # the PBS sends all of it to the reflecting port; nothing went that way on the way in
        assert [b["branch"] for b in tr[0]["branches"]] == ["0", "0.t", "0.t.r"]
        assert branch(tr, "las1", "0.t.r")["fraction"] == pytest.approx(stokes(J_MIRROR @ j_polarizer(90) @ returned)[0], abs=1e-6)
        assert branch(tr, "las1", "0.t.r")["events"][0]["component"] == "iso"


def _second(tr: list, component: str) -> dict:
    """The second event at *component* in branch 0.t of the isolator (the way back)."""
    hits = [e for e in branch(tr, "las1", "0.t")["events"] if e["component"] == component]
    return hits[-1]


def test_anchor_d_a_mirror_turns_plus_45_into_minus_45_and_a_splitter_reflects_the_same_way():
    for tr in traced("pol_mirror_flips_diagonal"):
        d45 = j_linear(45)
        after_m1 = J_MIRROR @ d45
        expect_beam(event(tr, "las1", "0", "bs1")["beam"], stokes(after_m1), mw_of=1.0)
        assert stokes(after_m1)[1:] == pytest.approx([0.0, -1.0, 0.0], abs=1e-12)            # 135 deg
        r, t = 0.3, 0.7
        expect_beam(branch(tr, "las1", "0.r")["segments"][0]["beam"], stokes(math.sqrt(r) * (J_MIRROR @ after_m1)))
        expect_beam(branch(tr, "las1", "0.t")["segments"][0]["beam"], stokes(math.sqrt(t) * after_m1))


def test_anchors_e_and_e3_pbs_halves_and_a_polarizer_on_unpolarized_light():
    for tr in traced("pol_pbs_unpolarized_split"):
        for src, fields in (("las1", (j_linear(45),)), ("las2", unpolarized())):
            st = stokes(*(j_polarizer(0) @ e for e in fields))
            sr = stokes(*(J_MIRROR @ j_polarizer(90) @ e for e in fields))
            assert (st[0], sr[0]) == pytest.approx((0.5, 0.5))
            expect_beam(event(tr, src, "0.t", "det_t" + src[-1])["beam"], st, mw_of=10.0)
            expect_beam(event(tr, src, "0.r", "det_r" + src[-1])["beam"], sr, mw_of=10.0)
            assert (branch(tr, src, "0.r")["fraction"], branch(tr, src, "0.t")["fraction"]) == (0.5, 0.5)
        out = stokes(*(j_polarizer(60) @ e for e in unpolarized()))
        expect_beam(event(tr, "lamp1", "0", "det3")["beam"], out)
        assert out[0] == pytest.approx(0.5) and psi_deg(event(tr, "lamp1", "0", "det3")["beam"]["s"]) == pytest.approx(60.0)
        assert event(tr, "lamp1", "0", "det3")["beam"]["mw"] is None                       # a lamp has no power_mw


def test_p_into_a_pbs_is_only_transmitted_and_s_only_reflected():
    for tr in traced("pol_pbs_p_only"):
        assert stokes(J_MIRROR @ j_polarizer(90) @ j_linear(0))[0] == pytest.approx(0.0, abs=1e-30)
        assert [b["branch"] for b in tr[0]["branches"]] == ["0", "0.t"]
        assert [b["branch"] for b in tr[1]["branches"]] == ["0", "0.r"]
        expect_beam(event(tr, "las2", "0.r", "det_r2")["beam"], stokes(J_MIRROR @ j_polarizer(90) @ j_linear(90)))


#: the anchor 'projection' of contract v2 §7: theta = 30 deg seen at 40 deg incidence (dn = cos 40 deg)
SEEN_30_AT_40 = 23.858655


def test_the_axis_an_oblique_ray_sees_is_that_of_the_physical_parts():
    """Contract v2 §4.5, derived from the parts themselves with 3-D vectors (no trace.py): an absorbing
    polarizer (wire grid, dichroic sheet) absorbs the field along ``a`` (in its face, perpendicular to the
    transmission axis ``t``), so what passes is perpendicular to ``a`` and to the ray, ``d x a``; a waveplate of
    a positive uniaxial crystal (quartz, MgF2) has its optic axis ``c`` along the same ``a`` (its slow axis at
    normal incidence), and its fast, ordinary wave is perpendicular to ``c`` and to the ray, ``d x c``."""
    v = part_vectors(30, 40, 0)                                       # theta 30, the part turned 40 deg: dn = cos 40
    assert v["d"] @ v["n"] == pytest.approx(math.cos(math.radians(40)), abs=1e-15)
    passed = np.cross(v["d"], v["a"])                                 # the absorbing polarizer
    assert abs(passed @ v["a"]) < 1e-15 and abs(passed @ v["d"]) < 1e-15
    assert angle_in_ray_frame(passed, v) == pytest.approx(SEEN_30_AT_40, abs=5e-7)
    c = v["a"]                                                        # the positive crystal's optic axis
    ordinary = np.cross(v["d"], c)                                    # its fast wave
    assert abs(ordinary @ c) < 1e-15 and angle_in_ray_frame(ordinary, v) == pytest.approx(SEEN_30_AT_40, abs=5e-7)
    # the projection of t itself onto the plane across the ray (the old reading, and a negative crystal's
    # fast, extraordinary wave) turns the other way: 37.004502 deg
    t_perp = v["t"] - (v["t"] @ v["d"]) * v["d"]
    assert angle_in_ray_frame(t_perp, v) == pytest.approx(37.004502, abs=5e-7)
    # front, from behind, and the axes 0 / 90 are the same in every reading
    assert seen_axis(30, 0, 0) == pytest.approx(30.0, abs=1e-9)
    assert seen_axis(30, 0, 180) == pytest.approx(150.0, abs=1e-9)                       # -30
    for theta in (0, 90):
        w = part_vectors(theta, 40, 0)
        t_perp = w["t"] - (w["t"] @ w["d"]) * w["d"]
        assert angle_in_ray_frame(t_perp, w) == pytest.approx(seen_axis(theta, 40, 0), abs=1e-9)
    # and the trace: the fixture of the anchor
    for tr in traced("pol_waveplate_oblique"):
        cases = (("las1", "det1", 40.0, 0.0), ("las2", "det2", 0.0, 180.0), ("las3", "det3", 0.0, 0.0))
        for src, detector, part_angle, ray_angle in cases:
            seen = seen_axis(30, part_angle, ray_angle)
            expect_beam(event(tr, src, "0", detector)["beam"], stokes(j_retarder(seen, HALF) @ j_linear(0)))
        out = event(tr, "las4", "0", "det4")["beam"]                  # unpolarized light, the oblique polarizer
        assert out["p"] == pytest.approx(0.5, abs=EPS)
        assert psi_deg(out["s"]) == pytest.approx(SEEN_30_AT_40, abs=5e-7)
        assert psi_deg(event(tr, "las1", "0", "det1")["beam"]["s"]) == pytest.approx(2 * SEEN_30_AT_40, abs=1e-6)


def test_waveplates_and_polarizers_at_random_axes_and_angles_agree_with_jones():
    """Random chains of plates and polarizers, hit at random incidence from a random direction: the
    Stokes vector at the end against Jones calculus with the axes the 3-D parts give (``d x a``)."""
    rng = random.Random(20261112)
    for case in range(60):
        ray = rng.choice((0.0, 90.0, 180.0, 270.0, 30.0, 145.0))
        d = (math.cos(math.radians(ray)), math.sin(math.radians(ray)))
        alpha = round(rng.uniform(-180, 180), 2)
        start = (600.0, 450.0)
        comps = [comp("las1", "laser", *start, ray, props={"wavelength_nm": 633, "polarization": "linear",
                                                          "pol_angle_deg": alpha})]
        e = j_linear(alpha)
        for k in range(1, 5):
            kind = rng.choice(("half", "quarter", "polarizer"))
            part_angle = round(ray + rng.choice((1, -1)) * rng.uniform(0, 60) + rng.choice((0, 180)), 2) % 360
            theta = round(rng.uniform(-180, 180), 2)
            x, y = start[0] + 60 * k * d[0], start[1] + 60 * k * d[1]
            if kind == "polarizer":
                comps.append(comp(f"c{k}", "polarizer", round(x, 2), round(y, 2), part_angle, props={"axis_deg": theta}))
                e = j_polarizer(seen_axis(theta, part_angle, ray)) @ e
            else:
                comps.append(comp(f"c{k}", "waveplate", round(x, 2), round(y, 2), part_angle,
                                  props={"retardance": kind, "fast_axis_deg": theta}))
                e = j_retarder(seen_axis(theta, part_angle, ray), HALF if kind == "half" else QUARTER) @ e
        comps.append(det("end", round(start[0] + 300 * d[0], 2), round(start[1] + 300 * d[1], 2), (ray + 180) % 360,
                         aperture=40))
        tr = trace_layout(layout(comps))
        evs = tr[0]["branches"][0]["events"]
        assert [x["component"] for x in evs] == ["c1", "c2", "c3", "c4", "end"], case
        s = stokes(e)
        got = evs[-1]["beam"]
        assert got["p"] == pytest.approx(s[0], abs=1e-8), case
        if s[0] > 1e-6:
            assert got["s"] == pytest.approx(list(s[1:] / s[0]), abs=1e-6), case


# ── power ────────────────────────────────────────────────────────────────────────────────────


def test_power_through_nd_a_mirror_and_a_fibre_coupling():
    for tr in traced("power_nd_throughput_fibre"):
        mw_at_fibre = 100.0 * 10 ** -1 * 0.9
        assert event(tr, "las1", "0", "m1")["beam"]["mw"] == pytest.approx(10.0, abs=1e-9)
        assert event(tr, "las1", "0", "fi1")["beam"]["mw"] == pytest.approx(mw_at_fibre, abs=1e-9)
        fo = next(t for t in tr if t["source"] == "fo1")
        assert fo["beam"]["source_mw"] == pytest.approx(mw_at_fibre * 0.6, abs=1e-9) == pytest.approx(5.4)
        first = fo["branches"][0]
        # its own polarization (vertical) and beam (2 mm, M^2 1.1), its own relative power
        assert first["segments"][0]["beam"]["p"] == 1.0
        assert first["segments"][0]["beam"]["s"] == pytest.approx([-1.0, 0.0, 0.0], abs=EPS)
        zr = math.pi * 1.0 ** 2 / (1.1 * 532e-6)
        assert first["segments"][0]["beam"]["q"] == pytest.approx([0.0, zr], rel=1e-8)          # q: 9 significant digits
        w = 1.0 * math.sqrt(1 + (300 / zr) ** 2)
        assert first["events"][0]["beam"]["d_mm"] == pytest.approx(2 * w, abs=1e-8)
        assert first["events"][0]["beam"]["mw"] == pytest.approx(5.4, abs=1e-9)


def test_a_fibre_fed_twice_takes_the_first_branch_only():
    for tr in traced("power_fibre_first_arrival"):
        fo = next(t for t in tr if t["source"] == "fo1")
        assert fo["beam"]["source_mw"] == pytest.approx(50.0 * 0.3 * 0.5, abs=1e-9)          # not (15 + 35) x 0.5


def test_every_throughput_multiplies_the_power():
    for tr in traced("power_throughput_chain"):
        ev = {e["component"]: e for e in tr[0]["branches"][0]["events"]}
        factors = [("win", 0.95), ("lens", 0.98), ("filt", 0.9), ("aotf", 0.5), ("prism", 0.99), ("peri", 0.97),
                   ("cust_t", 0.8), ("wp", 0.9), ("pol", 0.5 * 0.8), ("obj", 0.7), ("iris", 1.0), ("shut", 1.0),
                   ("grat", 0.7), ("smp", 0.6), ("cust_r", 0.5)]
        order = [c for c, _ in factors] + ["det"]
        p = 1.0
        for (name, factor), nxt in zip(factors, order[1:]):
            p *= factor
            assert ev[nxt]["beam"]["p"] == pytest.approx(p, abs=EPS), name
            assert ev[nxt]["beam"]["mw"] == pytest.approx(10.0 * p, abs=1e-8), name
        assert ev["det"]["beam"]["s"] == pytest.approx([1.0, 0.0, 0.0], abs=EPS)          # the polarizer's axis


def test_crossed_polarizers_leave_exactly_no_light_and_the_geometry_goes_on():
    for tr in traced("beam_crossed_polarizers_no_light"):
        h = j_linear(0)
        assert stokes(j_polarizer(120) @ j_polarizer(30) @ h)[0] == pytest.approx(0.0, abs=1e-15)
        dark = event(tr, "las1", "0", "m1")["beam"]
        assert (dark["p"], dark["s"], dark["mw"]) == (0.0, None, 0.0)
        assert [b["branch"] for b in tr[0]["branches"]] == ["0", "0.r", "0.t"]
        # a PBS that receives no light (S0 exactly 0) splits the geometry 0.5 / 0.5 (contract v2 §4.6, §12.2.1)
        assert [branch(tr, "las1", b)["fraction"] for b in ("0.r", "0.t")] == [0.5, 0.5]
        for b in ("0.r", "0.t"):
            child = branch(tr, "las1", b)
            assert child["segments"][0]["beam"] == {"p": 0.0, "s": None, "q": None}
            assert (child["events"][0]["beam"]["p"], child["events"][0]["beam"]["s"]) == (0.0, None)
            assert child["end"] == "stop"


def test_the_floating_point_residue_of_crossed_polarizers_is_what_the_dark_rule_removes():
    """White box: without the rule, 30 / 120 deg leave a residue (not 0) - the reason for EPS_DARK."""
    theta1, theta2 = math.radians(30), math.radians(120)
    c1, s1 = math.cos(2 * theta1), math.sin(2 * theta1)
    k1 = 0.5 * (1 + c1)
    after1 = (k1, k1 * c1, k1 * s1, 0.0)
    c2, s2 = math.cos(2 * theta2), math.sin(2 * theta2)
    residue = 0.5 * (after1[0] + c2 * after1[1] + s2 * after1[2])
    assert residue != 0.0 and abs(residue) < 1e-15
    assert T._polarized(after1, theta2) == (0.0, 0.0, 0.0, 0.0)
    # and light that is merely weak is not touched by it
    weak = T._polarized(after1, theta2 - math.radians(0.01))                    # sin^2(0.01 deg) = 3e-8
    assert weak[0] == pytest.approx(0.75 * math.sin(math.radians(0.01)) ** 2, rel=1e-6)


# ── anchors F-H: Gaussian beams ──────────────────────────────────────────────────────────────


def rayleigh(d_mm: float, nm: float, m2: float = 1.0) -> float:
    return math.pi * (d_mm / 2) ** 2 / (m2 * nm * 1e-6)


def diameter(w0: float, zr: float, z: float) -> float:
    return 2 * w0 * math.sqrt(1 + (z / zr) ** 2)


def self_lens(w0: float, zr: float, s: float, f: float) -> tuple[float, float, float]:
    """Self's thin-lens formula: a waist *w0* (Rayleigh range *zr*) at distance *s* before a lens of
    focal length *f* -> ``(w0', zr', s')``, the new waist and its distance behind the lens."""
    m = 1 / math.sqrt((1 - s / f) ** 2 + (zr / f) ** 2)
    s_out = f + (s - f) * f * f / ((s - f) ** 2 + zr ** 2)
    return m * w0, m * m * zr, s_out


def test_anchor_f_free_space():
    for tr in traced("gauss_free_space"):
        zr = rayleigh(1.0, 633)
        assert zr == pytest.approx(1240.755392, abs=1e-6)
        assert tr[0]["branches"][0]["segments"][0]["beam"]["q"] == pytest.approx([0.0, zr], rel=1e-8)
        assert event(tr, "las1", "0", "win1")["beam"]["d_mm"] == pytest.approx(diameter(0.5, zr, 500), abs=1e-8)
        assert event(tr, "las1", "0", "det1")["beam"]["d_mm"] == pytest.approx(diameter(0.5, zr, 1000), abs=1e-8)
        assert diameter(0.5, zr, 1000) == pytest.approx(1.284357, abs=1e-6)
        # M^2 = 2: the Rayleigh range of the real beam is halved (same waist)
        zr2 = rayleigh(1.0, 633, 2.0)
        assert tr[1]["beam"]["m2"] == 2.0 and zr2 == pytest.approx(zr / 2)
        assert event(tr, "las2", "0", "det2")["beam"]["d_mm"] == pytest.approx(diameter(0.5, zr2, 1000), abs=1e-8)


def test_anchor_g_kepler_expander():
    for tr in traced("gauss_kepler_3x"):
        w0, zr = 0.5, rayleigh(1.0, 532)
        d_lens1 = diameter(w0, zr, 100)
        w1, zr1, s1 = self_lens(w0, zr, 100, 50)               # the focus between the lenses
        d_lens2 = diameter(w1, zr1, 200 - s1)
        w2, zr2, s2 = self_lens(w1, zr1, 200 - s1, 150)
        d_far = diameter(w2, zr2, 1000 - s2)
        assert (d_lens1, d_lens2, d_far) == pytest.approx((1.002291, 3.000765, 3.014325), abs=1e-6)
        assert w2 / w0 == pytest.approx(150 / 50, rel=1e-3)        # the expansion f2 / f1
        assert event(tr, "las1", "0", "lens1")["beam"]["d_mm"] == pytest.approx(d_lens1, abs=1e-8)
        assert event(tr, "las1", "0", "lens2")["beam"]["d_mm"] == pytest.approx(d_lens2, abs=1e-8)
        assert event(tr, "las1", "0", "det1")["beam"]["d_mm"] == pytest.approx(d_far, abs=1e-8)
        between = tr[0]["branches"][0]["segments"][1]["beam"]["q"]
        assert -between[0] == pytest.approx(s1, rel=1e-8) and between[1] == pytest.approx(zr1, rel=1e-8)


def test_anchor_h_objective_focus():
    for tr in traced("gauss_objective_focus"):
        lam, f, w0 = 633e-6, 4.0, 2.0
        zr = rayleigh(4.0, 633)
        w1, zr1, s1 = self_lens(w0, zr, 50, f)
        assert s1 == pytest.approx(4.000002, abs=1e-6)
        assert 2 * w1 * 1e3 == pytest.approx(0.805958, abs=1e-6)                       # um
        assert 2 * lam * f / (math.pi * w0) * 1e3 == pytest.approx(0.805961, abs=1e-6)   # the estimate
        q = tr[0]["branches"][0]["segments"][1]["beam"]["q"]
        assert -q[0] == pytest.approx(s1, rel=1e-8)                                     # the waist behind the objective
        assert 2 * math.sqrt(lam * q[1] / math.pi) == pytest.approx(2 * w1, rel=2e-6)  # its size, from zR'
        at_sample = event(tr, "las1", "0", "smp1")["beam"]["d_mm"]
        assert at_sample == pytest.approx(diameter(w1, zr1, 4.0 - s1), abs=1.5e-9)
        # the back focal plane: lambda f / (pi w0) exactly, whatever the input waist
        assert at_sample == pytest.approx(2 * lam * f / (math.pi * w0), abs=1.5e-9)


def test_an_extreme_but_legal_rayleigh_range_is_not_rounded_to_zero():
    """q is written to 9 significant digits (contract v2 §12.2): 10 mm light (1e7 nm), a 1e-6 mm waist and
    M^2 = 1000 - all legal - give zR = 7.85e-17 mm, which 9 decimals made 0 (and d(q) a division by zero)."""
    lay = layout([comp("las1", "laser", 0, 100, props={"wavelength_nm": 1e7, "beam_diameter_mm": 1e-6, "m2": 1000}),
                  det("d", 100, 100, 180)])
    tr = trace_layout(lay)
    q = tr[0]["branches"][0]["segments"][0]["beam"]["q"]
    zr = math.pi * (0.5e-6) ** 2 / (1000 * 10.0)
    assert q[0] == 0.0 and q[1] > 0 and q[1] == pytest.approx(zr, rel=1e-8)
    assert float(f"{q[1]:.9g}") == q[1]                                                  # stored at 9 digits
    d = tr[0]["branches"][0]["events"][0]["beam"]["d_mm"]
    assert d == pytest.approx(diameter(0.5e-6, zr, 100), rel=1e-6)


def test_two_wavelengths_of_one_source_each_have_their_own_rayleigh_range_and_all_the_power():
    for tr in traced("gauss_multi_wavelength"):
        assert [t["wavelength_nm"] for t in tr] == [488.0, 785.0]
        for tree in tr:
            zr = rayleigh(1.5, tree["wavelength_nm"], 1.2)
            assert tree["beam"] == {"source_mw": 30.0, "m2": 1.2}
            assert tree["branches"][0]["segments"][0]["beam"]["q"] == pytest.approx([0.0, zr], rel=1e-8)
            last = tree["branches"][0]["events"][-1]["beam"]
            assert last["d_mm"] == pytest.approx(diameter(0.75, zr, 200 + (500 if tree["wavelength_nm"] == 488 else 800)), abs=1e-8)
        d45 = j_linear(45)
        expect_beam(event(tr, "las1", "0", "det_b", 488.0)["beam"], stokes(J_MIRROR @ d45), mw_of=30.0)
        expect_beam(event(tr, "las1", "0", "det_r", 785.0)["beam"], stokes(d45), mw_of=30.0)


def test_a_lens_without_a_focal_length_and_a_window_leave_the_beam_alone():
    lay = layout([comp("las1", "laser", 0, 100, props={"wavelength_nm": 633, "beam_diameter_mm": 1}),
                  comp("w", "window", 300, 100), comp("l0", "lens", 600, 100), det("d", 1000, 100, 180)])
    evs = trace_layout(lay)[0]["branches"][0]["events"]
    zr = rayleigh(1.0, 633)
    assert [e["beam"]["d_mm"] for e in evs] == pytest.approx([diameter(0.5, zr, z) for z in (300, 600, 1000)], abs=1e-8)


def test_the_dark_threshold_and_the_floor_are_what_the_contract_and_its_notes_say():
    assert T.EPS_DARK == 1e-12 and T.MIN_FRACTION == 1e-3 and T.MW_DECIMALS == 9
