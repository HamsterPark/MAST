"""The beam in the text an LLM reads (contract optical_layout_v2.md §5), in Chinese and in English.

Every component along a beam says what arrives there - power, spot, polarization; a segment with a waist
gets a focus line; a line of assumptions comes first when one applies; three more kinds of problem (a
spot larger than the aperture, an extinguished beam, waveplates without a retardance); and a text that is
too long leaves out spots and foci first and never cuts the things to look at. Numbers that the physics
gives are re-derived here (cos^2, Self's lens formula), not copied from the output.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from mast.i18n import use_lang
from mast.optical_layout.summary import (MAX_CHARS, polarization_text, power_text, share_text, spot_text,
                                         summarize)
from mast.optical_layout.trace import trace_layout

from ._layouts import comp, det, laser, layout, mirror

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "optical_layout"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["layout"]


def line_with(text: str, needle: str) -> str:
    return next(ln for ln in text.splitlines() if needle in ln)


def problems(lay, **kw) -> list[str]:
    out = summarize(lay, lang="en", **kw).splitlines()
    start = next((i for i, ln in enumerate(out) if ln.startswith("Things to look at")), None)
    if start is None:
        return []
    body = []
    for ln in out[start + 1:]:
        if not ln.startswith("- "):
            break
        body.append(ln[2:])
    return body


def stokes(psi_deg: float, chi_deg: float, dop: float = 1.0) -> list[float]:
    p, c = math.radians(2 * psi_deg), math.radians(2 * chi_deg)
    return [dop * math.cos(c) * math.cos(p), dop * math.cos(c) * math.sin(p), dop * math.sin(c)]


# ── the words for a polarization, a power, a spot ────────────────────────────────────────────


@pytest.mark.parametrize("s, en, zh", [
    (None, "no light", "无光"),
    ([0.0, 0.0, 0.0], "unpolarised", "非偏振"),
    ([0.005, 0.0, 0.0], "unpolarised", "非偏振"),                     # below 0.01 of polarization
    ([1.0, 0.0, 0.0], "linear 0° (p)", "线偏 0°（p）"),
    ([-1.0, 0.0, 0.0], "linear 90° (s)", "线偏 90°（s）"),
    ([0.0, 1.0, 0.0], "linear 45°", "线偏 45°"),
    ([0.0, -1.0, 0.0], "linear 135°", "线偏 135°"),
    (stokes(179.4, 0), "linear 179.4° (p)", "线偏 179.4°（p）"),      # within 1 deg of 180: p
    (stokes(179.96, 0), "linear 0° (p)", "线偏 0°（p）"),             # rounds to 180: back into [0, 180)
    (stokes(22.5, 0), "linear 22.5°", "线偏 22.5°"),
    (stokes(37.004502, 0), "linear 37°", "线偏 37°"),                 # no trailing .0
    (stokes(0.6, 0), "linear 0.6° (p)", "线偏 0.6°（p）"),
    (stokes(90.8, 0), "linear 90.8° (s)", "线偏 90.8°（s）"),
    (stokes(91.2, 0), "linear 91.2°", "线偏 91.2°"),                  # more than 1 deg off 90: no tag
    (stokes(30, 0.6), "linear 30°", "线偏 30°"),                      # |chi| < 1 deg is linear
    ([0.0, 0.0, -1.0], "circular (χ = -45°)", "圆偏（χ = -45°）"),
    ([0.0, 0.0, 1.0], "circular (χ = +45°)", "圆偏（χ = +45°）"),
    (stokes(10, 44.5), "circular (χ = +45°)", "圆偏（χ = +45°）"),    # |chi| > 44 deg is circular
    (stokes(30, 20), "elliptical 30°, χ = +20°", "椭圆偏 30°、χ = +20°"),     # the sign of χ always written
    (stokes(120, -12.5), "elliptical 120°, χ = -12.5°", "椭圆偏 120°、χ = -12.5°"),
    (stokes(0, 0, 0.5), "linear 0° (p) (degree of polarisation 0.50)", "线偏 0°（p）（偏振度 0.50）"),
])
def test_the_polarization_in_words(s, en, zh):
    with use_lang("en"):
        assert polarization_text(s) == en
    with use_lang("zh"):
        assert polarization_text(s) == zh


@pytest.mark.parametrize("mw, text", [
    (0.0, "0 mW"), (12.345, "12.3 mW"), (100.0, "100 mW"), (1500.0, "1.5 W"), (1.875, "1.88 mW"),
    (0.017, "17 µW"), (2.5e-5, "25 nW"), (1e-12, "0.001 pW"),
])
def test_a_power_is_written_in_the_unit_a_person_would_use(mw, text):
    assert power_text(mw) == text


def test_powers_are_written_as_the_shared_cases_say():
    """tests/fixtures/optical_layout/power_text_cases.json - the front end's tests read the same file. One
    algorithm, no float formatting (contract v2 §12.3): the contract's cases are in it."""
    doc = json.loads((FIXTURES / "power_text_cases.json").read_text(encoding="utf-8"))
    cases = doc["cases"]
    for case in cases:
        for lang in ("zh", "en"):
            with use_lang(lang):
                got = power_text(case["mw"]) if "mw" in case else share_text(case["p"])
            assert got == case[lang], (case, lang, got)
    by_mw = {c["mw"]: c["en"] for c in cases if "mw" in c}
    by_p = {c["p"]: c["en"] for c in cases if "p" in c}
    assert (by_mw[0.9999999999999998], by_mw[999.7], by_mw[0.0]) == ("1 mW", "1 W", "0 mW")
    # below 0.001 pW no digits at all (the contract's rule): sig3 only ever runs for e >= -12
    assert (by_mw[1e-13], by_mw[9.99e-13], by_mw[1e-12]) == ("< 0.001 pW", "< 0.001 pW", "0.001 pW")
    assert by_p[-1e-17] == "relative 0 %"                                         # a negative residue is no light
    assert {0.1725, 0.9665, 0.03125, 1e7} <= set(by_mw)
    assert (by_p[5e-06], by_p[1e-05], by_p[0.0]) == ("relative < 0.001 %", "relative 0.001 %", "relative 0 %")
    assert not [t for c in cases for t in (c["zh"], c["en"]) if "e+" in t or "e-" in t], "an exponent slipped in"


def test_a_spot_is_in_mm_down_to_a_tenth_and_in_um_below():
    assert (spot_text(1.2), spot_text(0.1), spot_text(0.0999), spot_text(0.000805961)) == (
        "Ø1.20 mm", "Ø0.10 mm", "Ø99.90 µm", "Ø0.81 µm")


# ── each component says what arrives ───────────────────────────────────────────────────────


def test_malus_in_words_power_and_polarization_at_each_component():
    en = summarize(fixture("pol_malus_two_polarizers"), lang="en")
    assert line_with(en, "P1 polarizer").endswith("· arriving 10 mW · linear 0° (p)")
    assert line_with(en, "P2 polarizer").endswith(f"· arriving {power_text(10 * math.cos(math.radians(30)) ** 2)} · linear 30°")
    assert line_with(en, "D1 detector").endswith("· arriving 1.88 mW · linear 90° (s)")      # 10 x 0.75 x 0.25
    zh = summarize(fixture("pol_malus_two_polarizers"), lang="zh")
    assert line_with(zh, "D1 探测器").endswith("· 入射 1.88 mW · 线偏 90°（s）")


def test_without_an_absolute_power_the_share_of_the_source_is_given():
    zh = summarize(fixture("pol_pbs_p_only"), lang="zh")
    assert line_with(zh, "DT1 探测器").endswith("· 相对 100 % · 线偏 0°（p）")
    en = summarize(fixture("power_throughput_chain"), lang="en")
    assert "arriving" in line_with(en, "D detector")
    assert line_with(summarize(fixture("two_mirror_dogleg"), lang="en"), "D1 detector").endswith("· relative 100 % · unpolarised")


def test_the_isolator_in_words():
    en = summarize(fixture("pol_qwp_isolator"), lang="en")
    assert line_with(en, "M1 mirror").endswith("· arriving 5 mW · circular (χ = -45°)")
    back = [ln for ln in en.splitlines() if "PBS1 polarizing beam splitter" in ln]
    assert len(back) == 2 and back[1].endswith("splits · arriving 5 mW · linear 90° (s)")
    assert "reflected branch 0.t.r (split share 100%)" in en and "transmits p, reflects s" in back[0]


def test_crossed_polarizers_say_no_light_and_the_extinction_is_a_problem():
    en = summarize(fixture("beam_crossed_polarizers_no_light"), lang="en")
    assert line_with(en, "M1 mirror").endswith("· arriving 0 mW · no light")
    assert problems(fixture("beam_crossed_polarizers_no_light")) == [
        "extinguished: the beam las1 532 nm has almost no light after P2 polarizer (polarization)"]
    zh = summarize(fixture("beam_crossed_polarizers_no_light"), lang="zh")
    assert "被消光：las1 532 nm 经过 P2 偏振片 之后几乎没有光（偏振）" in zh


def test_an_nd_filter_or_a_throughput_of_zero_extinguishes_too():
    lay = layout([laser("las1", 0, 100), comp("nd", "nd", 100, 100, 0, props={"od": 7}),
                  comp("w", "window", 200, 100, 0, props={"throughput": 0.0}), det("d", 300, 100, 180)])
    assert problems(lay) == ["extinguished: the beam las1 532 nm has almost no light after ND ND filter (ND)"]
    lay = layout([laser("las1", 0, 100), comp("w", "window", 200, 100, 0, props={"throughput": 0.0}), det("d", 300, 100, 180)])
    assert problems(lay) == ["extinguished: the beam las1 532 nm has almost no light after W window (throughput)"]


# ── Gaussian beams: spots and foci ───────────────────────────────────────────────────────────


def test_the_objective_focus_is_a_line_of_its_own():
    zh = summarize(fixture("gauss_objective_focus"), lang="zh")
    assert "     焦点：OBJ 之后 4.00 mm，Ø0.81 µm" in zh.splitlines()            # the contract's own example
    assert line_with(zh, "SMP 样品").endswith("· 光斑 Ø0.81 µm · 非偏振")
    assert line_with(zh, "OBJ 物镜").endswith("· 相对 100 % · 光斑 Ø4.00 mm · 非偏振")
    assert "focus: 4.00 mm after OBJ, Ø0.81 µm" in summarize(fixture("gauss_objective_focus"), lang="en")


def test_the_focus_of_a_kepler_expander_is_where_selfs_formula_puts_it():
    w0, lam, f = 0.5, 532e-6, 50.0
    zr = math.pi * w0 ** 2 / lam
    s = 100.0
    m = 1 / math.sqrt((1 - s / f) ** 2 + (zr / f) ** 2)
    s_out = f + (s - f) * f * f / ((s - f) ** 2 + zr ** 2)
    en = summarize(fixture("gauss_kepler_3x"), lang="en")
    assert f"focus: {s_out:.2f} mm after F50, Ø{2 * m * w0 * 1e3:.2f} µm" in en
    assert sum("focus:" in ln for ln in en.splitlines()) == 1                  # the waist at the laser is not a focus
    assert line_with(en, "F150 lens").endswith("· spot Ø3.00 mm · unpolarised")


def test_a_waist_just_past_the_end_of_a_segment_is_still_its_focus_but_one_further_is_not():
    for x, focus in ((104.0, True), (104.01, True), (103.99, False)):
        lay = layout([comp("las1", "laser", 50, 100, props={"wavelength_nm": 633, "beam_diameter_mm": 4}),
                      comp("obj", "objective", 100, 100, props={"focal_mm": 4}), det("d", x, 100, 180)])
        assert ("focus:" in summarize(lay, lang="en")) is focus, x


# ── assumptions ──────────────────────────────────────────────────────────────────────────────


def test_the_assumptions_come_first_and_only_when_they_apply():
    plain = summarize(fixture("two_mirror_dogleg"), lang="en")
    assert "Assumptions" not in plain                                           # no polarization optics
    lay = layout([laser("las1", 0, 100), comp("p", "polarizer", 100, 100, 0, props={"axis_deg": 0}), det("d", 300, 100, 180)])
    en = summarize(lay, lang="en").splitlines()
    assert en[3] == ("Assumptions: 1 light source without a polarization is taken as unpolarized "
                     "(a polarizer or a PBS halves its power)")
    zh = summarize(lay, lang="zh").splitlines()
    assert zh[3] == "假设：1 个光源没填偏振，按非偏振光算（过偏振片或 PBS 时功率减半）"
    assert "Assumptions" not in summarize(fixture("pol_malus_two_polarizers"), lang="en")    # its source says linear
    multi = summarize(fixture("gauss_multi_wavelength"), lang="en")
    assert "a source with several wavelengths emits all of its power_mw at each of them" in multi
    fibre = summarize(fixture("power_nd_throughput_fibre"), lang="zh")
    assert "假设：光纤续接的光按第一次到达的那一支计功率，相对功率从 1 重新算" in fibre
    assert "；光从 FC1 接过来，耦合进来 5.4 mW" in fibre


def test_parts_without_their_axis_or_retardance_say_so():
    lay = layout([laser("las1", 0, 100), comp("p", "polarizer", 100, 100, 0), comp("wp", "waveplate", 200, 100, 0),
                  comp("hwp", "waveplate", 250, 100, 0, props={"retardance": "half"}), det("d", 300, 100, 180)])
    en = summarize(lay, lang="en")
    assert "axis not given: taken as 0 deg" in line_with(en, "P polarizer")
    assert "retardance not given: taken as transparent" in line_with(en, "WP waveplate")
    assert "fast axis not given: taken as 0 deg" in line_with(en, "HWP waveplate")
    assert "waveplate without a retardance, taken as transparent (no effect on the polarization): WP waveplate" in problems(lay)
    assert "波片没填相位延迟，按透明处理（对偏振不起作用）：WP 波片" in summarize(lay, lang="zh")
    # one line per waveplate, and only for one a beam reaches
    two = layout([laser("las1", 0, 100), comp("wa", "waveplate", 100, 100, 0), comp("wb", "waveplate", 200, 100, 0),
                  comp("wc", "waveplate", 200, 600, 0), det("d", 300, 100, 180)])
    assert [p for p in problems(two) if p.startswith("waveplate")] == [
        "waveplate without a retardance, taken as transparent (no effect on the polarization): WA waveplate",
        "waveplate without a retardance, taken as transparent (no effect on the polarization): WB waveplate"]


def test_the_source_line_gives_its_polarization_and_beam():
    en = summarize(fixture("power_nd_throughput_fibre"), lang="en")
    assert "FO1 fiber output (532 nm, linear 90 deg, Ø2 mm beam at the exit, M² 1.1," in en
    assert "throughput 90%" in line_with(en, "M1 mirror") and "coupling 60%" in line_with(en, "FC1 fiber coupler")


# ── a spot larger than the aperture ──────────────────────────────────────────────────────────


def _wide(x_part: float, part: dict, beam_mm: float = 30.0) -> dict:
    return layout([comp("las1", "laser", 0, 100, props={"wavelength_nm": 1064, "beam_diameter_mm": beam_mm}),
                   part, det("far", 100, 1000, 270, aperture=200)])


def test_a_spot_larger_than_the_aperture_is_a_problem_with_how_much():
    lay = _wide(100, mirror("m1", 100, 100, 135))                      # 45 deg: the footprint is r / cos 45
    d = trace_layout(lay)[0]["branches"][0]["events"][0]["beam"]["d_mm"]
    over = d / 2 / math.cos(math.radians(45)) - 25.4 / 2
    got = [p for p in problems(lay) if p.startswith("spot larger")]
    assert got == [f"spot larger than the aperture: the beam las1 1064 nm is {spot_text(d)} across on M1 mirror, "
                   f"{d / 2 / math.cos(math.radians(45)):.2f} mm half-width along its surface at 0.0 mm off center, "
                   f"{over:.2f} mm beyond the edge (half aperture 12.7 mm)"]
    assert "光斑超出口径：las1 1064 nm 在 M1 反射镜 上光斑" in summarize(lay, lang="zh")


def test_a_spot_that_fits_and_the_parts_it_is_not_checked_for_are_not_problems():
    assert not [p for p in problems(_wide(100, mirror("m1", 100, 100, 135), beam_mm=10)) if p.startswith("spot larger")]
    for part in (comp("s", "sample", 100, 100, 135, props={"reflects": True}, aperture=5),
                 comp("bd", "beam_dump", 100, 100, 180, aperture=5), comp("bl", "blocker", 100, 100, 180, aperture=5)):
        assert not [p for p in problems(_wide(100, part)) if p.startswith("spot larger")], part["type"]


def test_an_iris_is_checked_against_its_opening():
    lay = layout([comp("las1", "laser", 0, 100, props={"wavelength_nm": 633, "beam_diameter_mm": 3}),
                  comp("ir", "iris", 100, 100, 0, props={"open_mm": 2}), det("d", 300, 100, 180)])
    got = [p for p in problems(lay) if p.startswith("spot larger")]
    assert len(got) == 1 and "(half aperture 1 mm)" in got[0]


# ── the length ───────────────────────────────────────────────────────────────────────────────


def _long_gaussian() -> dict:
    comps = [comp("las1", "laser", 0, 100, props={"wavelength_nm": 633, "beam_diameter_mm": 1, "power_mw": 5})]
    comps += [comp(f"l{i}", "lens", 20 + 10 * i, 100, 0, label=f"LENS-{i}", props={"focal_mm": 4.0 if i % 2 else -4.0},
                   function="relay") for i in range(50)]
    return layout(comps)


def test_too_long_leaves_out_spots_and_foci_first_and_keeps_power_and_polarization():
    lay = _long_gaussian()
    full = summarize(lay, lang="en", limit=10 ** 6)
    assert "spot Ø" in full and len(full) > MAX_CHARS
    lean = summarize(lay, lang="en", limit=len(full) - 1)
    assert "(spot sizes and foci are left out below to keep this short)" in lean
    assert "spot Ø" not in lean and "focus:" not in lean
    assert "arriving 5 mW" in lean and "unpolarised" in lean
    assert len(lean) < len(full) and "the beam paths are cut here" not in lean


def test_the_things_to_look_at_survive_any_length_that_holds_them():
    lay = _long_gaussian()
    full_problems = problems(lay, limit=10 ** 6)
    assert full_problems[3] == "(42 more of this kind)" and len(full_problems) == 5    # 3 spots + the rest + off the table
    for limit in (MAX_CHARS, 5000, 4000, 1200):
        got = problems(lay, limit=limit)
        assert [p for p in got if p.startswith("spot larger")] and [p for p in got if p.startswith("leaves the table")], limit
        assert len(summarize(lay, lang="en", limit=limit)) <= limit
    tight = summarize(lay, lang="en", limit=500)                                 # cannot hold them: cut from the end
    assert len(tight) == 500 and tight.endswith("(summary cut at 500 characters)")


# ── grouping, the hits a check looks at, and the order (contract v2 §12.3) ───────────────────


def test_spots_and_extinctions_are_grouped_by_component_with_the_worst_hit_and_how_many():
    lay = layout([comp("la", "laser", 0, 100, props={"wavelength_nm": 1064, "beam_diameter_mm": 30}),
                  comp("lb", "laser", 0, 140, 340, props={"wavelength_nm": 1064, "beam_diameter_mm": 40}),
                  mirror("m1", 100, 100, 135, aperture=25.4), det("far", 100, 800, 270, aperture=300)])
    evs = [e for t in trace_layout(lay) for b in t["branches"] for e in b["events"] if e["component"] == "m1"]
    assert len(evs) == 2
    overs = [abs(e["offset_mm"]) + e["beam"]["d_mm"] / 2 / math.cos(math.radians(e["incidence_deg"])) - 12.7 for e in evs]
    worst = max(range(2), key=lambda i: overs[i])
    got = [p for p in problems(lay) if p.startswith("spot larger")]
    assert len(got) == 1 and got[0].endswith(f"{overs[worst]:.2f} mm beyond the edge (half aperture 12.7 mm) (2 beams in all)")
    assert ("la 1064 nm", "lb 1064 nm")[worst] in got[0]
    assert "（共 2 束光）" in summarize(lay, lang="zh")


def test_an_extinction_names_the_brightest_beam_it_took():
    lay = layout([comp("la", "laser", 0, 100, props={"wavelength_nm": 633, "polarization": "linear"}),
                  comp("nd", "nd", 50, 100, 0, props={"od": 1}),
                  comp("lb", "laser", 100, 0, 90, props={"wavelength_nm": 633, "polarization": "linear"}),
                  comp("p1", "polarizer", 100, 100, 45, aperture=50, props={"axis_deg": 90}),
                  det("d1", 300, 100, 180), det("d2", 100, 300, 270)])
    got = [p for p in problems(lay) if p.startswith("extinguished")]
    assert got == ["extinguished: the beam lb 633 nm has almost no light after P1 polarizer (polarization) (2 beams in all)"]


def test_a_spot_check_skips_dark_beams_backs_and_blocks():
    dark = layout([comp("la", "laser", 0, 100, props={"wavelength_nm": 1064, "beam_diameter_mm": 30, "polarization": "linear"}),
                   comp("p1", "polarizer", 100, 100, 0, aperture=50, props={"axis_deg": 90}),
                   mirror("m1", 200, 100, 135), det("far", 200, 800, 270, aperture=300)])
    got = problems(dark)
    assert not [p for p in got if p.startswith("spot larger")], got                  # the light that reaches M1 is none
    assert [p for p in got if p.startswith("extinguished")]
    back = layout([comp("la", "laser", 0, 100, props={"wavelength_nm": 1064, "beam_diameter_mm": 30}), mirror("m1", 100, 100, 0)])
    got = problems(back)
    assert [p for p in got if p.startswith("back side")] and not [p for p in got if p.startswith("spot larger")]
    shut = layout([comp("la", "laser", 0, 100, props={"wavelength_nm": 1064, "beam_diameter_mm": 30}),
                   comp("sh", "shutter", 100, 100, 0, props={"closed": True})])
    assert not [p for p in problems(shut) if p.startswith("spot larger")]


def test_the_things_to_look_at_come_in_the_agreed_order():
    lay = layout([
        comp("l1", "laser", 20, 100, props={"wavelength_nm": 633, "beam_diameter_mm": 30, "polarization": "linear"}),
        comp("w1", "window", 100, 100, 0),                                          # spot larger than its 25.4 mm
        comp("p1", "polarizer", 200, 100, 0, aperture=50, props={"axis_deg": 90}),  # extinguishes; then off the table
        laser("l2", 20, 300), mirror("m2", 100, 300, 0),                            # the back of a mirror
        laser("l3", 20, 500), mirror("m4", 60, 520, 180),                           # passes just outside M4
        comp("wp", "waveplate", 150, 500, 0), det("d3", 300, 504, 180),             # no retardance; 4 mm off center
        laser("l4", 500, 700), mirror("ma", 600, 700, 180), mirror("mb", 400, 700, 0),     # ping-pong
        comp("fi", "fiber_in", 1000, 800, 0, props={"continues_at": "nobody"}),    # a broken fiber link
    ])
    kinds = ["back side", "off center", "missed aperture", "spot larger", "extinguished", "leaves the table",
             "components no beam reaches", "event limit", "broken fiber link", "waveplate without"]
    got = problems(lay)
    assert [next(k for k in kinds if p.startswith(k)) for p in got] == kinds, got


def test_two_beams_over_by_half_a_mm_and_two_mm_give_one_line_with_the_two():
    """Contract v2 §12.3.6, example: on one mirror a beam 0.5 mm over the edge and one 2 mm over make
    one line that reports the 2 mm and "2 beams"; a beam on the back of the same mirror and a beam that arrives
    without light (both far over the edge) are not looked at and not counted."""
    def las(cid, x, y, angle, d, **props):
        return comp(cid, "laser", x, y, angle, props={"wavelength_nm": 633, "beam_diameter_mm": d, **props})

    lay = layout([
        las("la", 100, 100, 0, 26.4),                                   # on center: 13.2 - 12.7 = 0.5 mm over
        las("lb", 100, 101.5, 0, 26.4),                                 # 1.5 mm off: 1.5 + 13.2 - 12.7 = 2.0 mm over
        las("lc", 500, 100, 180, 40.0),                                 # the back of M1 (7.3 mm over): not looked at
        las("ld", 250, 150, 315, 30.0, polarization="linear"),          # no light left (8.5 mm over): not looked at
        comp("pd", "polarizer", 275, 125, 315, aperture=40, props={"axis_deg": 90}),
        mirror("m1", 300, 100, 180),
    ])
    hits = {t["source"]: e for t in trace_layout(lay) for b in t["branches"] for e in b["events"] if e["component"] == "m1"}
    assert (hits["lc"]["action"], hits["ld"]["beam"]["p"]) == ("back_hit", 0.0)
    over = {k: abs(e["offset_mm"]) + e["beam"]["d_mm"] / 2 / math.cos(math.radians(e["incidence_deg"])) - 12.7
            for k, e in hits.items()}
    assert (round(over["la"], 6), round(over["lb"], 6)) == (0.5, 2.0) and min(over["lc"], over["ld"]) > 7
    got = [p for p in problems(lay) if p.startswith("spot larger")]
    assert got == ["spot larger than the aperture: the beam lb 633 nm is Ø26.40 mm across on M1 mirror, 13.20 mm "
                   "half-width along its surface at -1.5 mm off center, 2.00 mm beyond the edge (half aperture 12.7 mm) "
                   "(2 beams in all)"]
    zh = [ln for ln in summarize(lay, lang="zh").splitlines() if "光斑超出口径" in ln]
    assert len(zh) == 1 and "超出口径边缘 2.00 mm" in zh[0] and zh[0].endswith("（共 2 束光）")


# ── every kind of thing to look at stays in sight (contract v2 §12.3) ─────


KIND_PREFIXES = ("trace cut", "back side", "off center", "missed aperture", "spot larger", "extinguished",
                 "leaves the table", "components no beam reaches", "event limit", "broken fiber link",
                 "fiber output", "waveplate without", "the layout has no light source")


def _crowded() -> dict:
    """A crowded scene: thirteen spots larger than the detectors (a 12 mm beam on 10 mm), and after
    them three kinds of v1 problems - an unlit mirror (and an unlit, broken fibre coupler), a broken fibre link,
    a beam caught between two mirrors until the event cap."""
    comps = [comp("las", "laser", 50, 100, 0, label="", props={"wavelength_nm": 532, "power_mw": 20, "beam_diameter_mm": 12})]
    for i in range(13):
        x = 150 + 60 * i
        comps.append(comp(f"bs{i}", "beam_splitter", x, 100, 135, label="", aperture=50, props={"reflect_fraction": 0.1}))
        comps.append(comp(f"det{i}", "detector", x, 300, 270, label=""))
    comps.append(comp("dump", "beam_dump", 1100, 100, 180, label="", aperture=50))
    comps.append(comp("fc", "fiber_in", 600, 700, 0, label="", props={"continues_at": "nowhere"}))
    comps.append(comp("lonely", "mirror", 900, 700, 0, label=""))
    comps += [laser("l4", 500, 820), mirror("ma", 600, 820, 180), mirror("mb", 400, 820, 0)]
    return layout(comps)


def _kinds_shown(text: str) -> dict[str, int]:
    """kind -> lines shown, and the counted rest under the key ``kind + " more"``."""
    out: dict[str, int] = {}
    last = None
    body = text.split("Things to look at", 1)[1].splitlines()[1:]
    for ln in body:
        if not ln.startswith("- "):
            break
        item = ln[2:]
        if item.endswith("more of this kind)"):
            out[last + " more"] = int(item.split()[0].strip("("))
            continue
        last = next(k for k in KIND_PREFIXES if item.startswith(k))
        out[last] = out.get(last, 0) + 1
    return out


def test_many_problems_of_one_kind_no_longer_hide_the_other_kinds():
    shown = _kinds_shown(summarize(_crowded(), lang="en", limit=10 ** 6))
    assert shown["spot larger"] == 3 and shown["spot larger more"] == 10
    for kind in ("components no beam reaches", "event limit", "broken fiber link"):         # the v1 kinds after them
        assert shown[kind] == 1, (kind, shown)
    assert summarize(_crowded(), lang="en", limit=10 ** 6).count("Things to look at (16):") == 1


def test_a_text_that_must_shrink_drops_spots_then_lines_of_a_kind_then_the_paths_and_never_a_kind():
    lay = _crowded()
    trace = trace_layout(lay)
    full = summarize(lay, trace, lang="en", limit=10 ** 6)
    kinds = {k for k in _kinds_shown(full) if not k.endswith(" more")}
    assert len(full) > MAX_CHARS
    stages = set()
    for limit in range(len(full), 300, -40):
        text = summarize(lay, trace, lang="en", limit=limit)
        assert len(text) <= limit, limit
        if text.endswith(f"(summary cut at {limit} characters)"):
            stages.add("cut from the end")
            continue
        shown = _kinds_shown(text)
        assert {k for k in shown if not k.endswith(" more")} == kinds, limit                # no kind disappears
        narrow = all(n == 1 for k, n in shown.items() if not k.endswith(" more"))
        paths_cut = "the beam paths are cut here" in text
        if paths_cut:
            assert narrow and "spot Ø" not in text, limit
            stages.add("paths cut")
        elif narrow and shown.get("spot larger more") == 12:
            assert "spot Ø" not in text and "(spot sizes and foci are left out below to keep this short)" in text, limit
            stages.add("one line of a kind")
        elif "spot Ø" not in text:
            assert shown["spot larger"] == 3, limit
            stages.add("no spots")
        else:
            assert text == full, limit
            stages.add("whole")
    assert stages == {"whole", "no spots", "one line of a kind", "paths cut", "cut from the end"}, stages
    at_default = _kinds_shown(summarize(lay, lang="en"))
    assert {k for k in at_default if not k.endswith(" more")} == kinds


def test_a_continued_fibre_output_does_not_name_its_own_power():
    """The trace does not use a continued fibre output's own power_mw (contract v2 §4.2): its title gives what
    was coupled in instead."""
    lay = fixture("power_nd_throughput_fibre")
    fo = next(c for c in lay["components"] if c["id"] == "fo1")
    fo["props"]["power_mw"] = 999.0
    en = summarize(lay, lang="en")
    head = line_with(en, "FO1 fiber output (")
    assert "power 999 mW" not in head and "5.4 mW coupled in" in head
    assert "功率 999 mW" not in line_with(summarize(lay, lang="zh"), "FO1 光纤输出（")
    direct = fixture("power_nd_throughput_fibre")
    assert "power 100 mW" in line_with(summarize(direct, lang="en"), "L1 laser (")            # a laser still says it
