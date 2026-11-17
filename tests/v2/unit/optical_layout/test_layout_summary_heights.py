"""Heights in the text an LLM reads (contract ``docs/v2/design/optical_layout_3d.md`` §6), in Chinese and English.

Only a layout that names a height gets any of it (the others are described word for word as before); then the
table line gives the beam height, a line says how heights are measured, a component its height (a periscope its
two), every hit the height of the beam and how far off the axis, every segment the components it passes over or
under (three lines at most, one more counting the rest); and three kinds of problem: off the axis in height by
more than 20 %, missed in height (a component nothing lights that a beam passes), a spot beyond the aperture in
height. Numbers are re-derived here, not copied from the output.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mast.optical_layout.summary import MAX_PASS_LINES, summarize
from mast.optical_layout.trace import MAX_TRACE_PASSES, trace_layout

from ._layouts import comp, det, laser, layout, mirror

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "optical_layout"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["layout"]


def line_with(text: str, needle: str) -> str:
    return next(ln for ln in text.splitlines() if needle in ln)


def problems(lay, lang: str = "en") -> list[str]:
    out = summarize(lay, lang=lang).splitlines()
    head = "Things to look at" if lang == "en" else "需要注意"
    start = next((i for i, ln in enumerate(out) if ln.startswith(head)), None)
    if start is None:
        return []
    body = []
    for ln in out[start + 1:]:
        if not ln.startswith("- "):
            break
        body.append(ln[2:])
    return body


# ── only where heights are used ──────────────────────────────────────────────────────────────


def test_a_layout_without_heights_says_nothing_about_them():
    """Even a spot too large all round (which the v2 check reports in the plane) is not a problem in height there."""
    lay = layout([comp("l1", "laser", 20, 100, props={"wavelength_nm": 633, "beam_diameter_mm": 30}),
                  comp("w1", "window", 100, 100, 0), det("d", 300, 100, 180, aperture=40)])
    for lang in ("zh", "en"):
        text = summarize(lay, lang=lang)
        for word in ("光高", "台面光束高度", "高度 z", "越过", ", beam at ", "beam height", "Heights z", "passes over",
                     "in height"):
            assert word not in text, (lang, word)
    assert [p for p in problems(lay) if p.startswith("spot larger")]


def test_the_header_gives_the_beam_height_and_how_heights_are_measured():
    up = fixture("height_periscope_up")                      # no beam height given: 75, said to be the default
    zh, en = summarize(up, lang="zh"), summarize(up, lang="en")
    assert "（公制），台面光束高度 75 mm（没填，按缺省）；器件 5 个" in zh
    assert "(metric), beam height 75 mm (not given: the default); 5 components" in en
    assert "高度 z 从台面量起，单位 mm；没填高度的器件和光源在台面光束高度。" in zh.splitlines()
    assert ("Heights z are measured from the table top, in mm; a component or source without one sits at the "
            "table's beam height.") in en.splitlines()
    given = summarize(fixture("height_source_heights"), lang="en")
    assert "(metric), beam height 120 mm; 12 components" in given


# ── along the beam ───────────────────────────────────────────────────────────────────────────


def test_heights_of_components_and_of_the_beam_along_it():
    up = fixture("height_periscope_up")
    zh, en = summarize(up, lang="zh"), summarize(up, lang="en")
    assert "入口 75 mm、出口 250 mm（升 175 mm）" in line_with(zh, "P1 潜望镜")
    assert "entrance 75 mm, exit 250 mm (up 175 mm)" in line_with(en, "P1 periscope")
    assert "高度 250 mm" in line_with(zh, "M_HIGH") and "光高 250 mm" in line_with(zh, "M_HIGH")
    assert "beam at 75 mm" in line_with(en, "P1 periscope") and "beam at 250 mm" in line_with(en, "M_HIGH")
    down = summarize(fixture("height_periscope_down"), lang="en")
    assert "entrance 200 mm, exit 60 mm (down 140 mm)" in line_with(down, "P1 periscope")
    assert "beam at 203 mm, 3.0 mm above its entrance" in line_with(down, "P1 periscope")
    assert "beam at 63 mm, 3.0 mm above its axis" in line_with(down, "M1 mirror")
    zh_down = summarize(fixture("height_periscope_down"), lang="zh")
    assert "光高 203 mm、高出入口 3.0 mm" in line_with(zh_down, "P1 潜望镜")
    passes = summarize(fixture("height_passes_over_and_under"), lang="zh")
    assert "光高 75 mm、低于光轴 12.7 mm" in line_with(passes, "M_EDGE")


def test_a_pass_is_a_line_of_its_own_before_the_hit_that_ends_its_segment():
    zh = summarize(fixture("height_passes_over_and_under"), lang="zh").splitlines()
    i = next(i for i, ln in enumerate(zh) if "M_EDGE 反射镜（" in ln)
    assert zh[i - 2].strip() == "↷ 从 M_HI 反射镜 下方越过（光高 75 mm，它的光轴在 100 mm）"
    assert zh[i - 1].strip() == "↷ 从 M_LO 反射镜 上方越过（光高 75 mm，它的光轴在 50 mm）"
    en = summarize(fixture("height_periscope_up"), lang="en").splitlines()
    i = next(i for i, ln in enumerate(en) if "M_HIGH mirror (" in ln)
    assert en[i - 1].strip() == "↷ passes over M_LOW mirror (beam at 250 mm, its axis at 75 mm)"


def test_at_most_three_passes_a_segment_and_one_line_for_the_rest():
    comps = [laser("las1", 0, 100)] + [mirror(f"m{i}", 100 + 50 * i, 100, 135, props={"z_mm": 200 + i}) for i in range(5)]
    lay = layout(comps + [det("d", 500, 100, 180)])
    zh = [ln.strip() for ln in summarize(lay, lang="zh").splitlines() if ln.strip().startswith("↷")]
    assert len(zh) == MAX_PASS_LINES + 1 == 4
    assert zh[:3] == [f"↷ 从 M{i} 反射镜 下方越过（光高 75 mm，它的光轴在 {200 + i} mm）" for i in range(3)]
    assert zh[3] == "↷ （另有 2 处越过）"
    en = [ln.strip() for ln in summarize(lay, lang="en").splitlines() if ln.strip().startswith("↷")]
    assert en[3] == "↷ (2 more passed over or under)"


def test_the_passes_of_a_segment_out_of_the_table_come_before_it_leaves():
    lay = layout([laser("las1", 0, 100), mirror("m1", 200, 100, 135, props={"z_mm": 200})])
    en = summarize(lay, lang="en").splitlines()
    i = next(i for i, ln in enumerate(en) if "leaves the table" in ln and "→" in ln)
    assert en[i - 1].strip() == "↷ passes under M1 mirror (beam at 75 mm, its axis at 200 mm)"


def test_a_blocking_iris_says_its_opening_holds_in_height_too():
    zh = summarize(fixture("height_iris_in_height"), lang="zh")
    assert "（命中点（平面内或高度方向）偏离中心超过开口半径）" in line_with(zh, "I_BLOCK")
    plain = layout([laser("las1", 0, 100), comp("ir", "iris", 100, 103, 0, props={"open_mm": 4}), det("d", 300, 100, 180)])
    assert "（命中点偏离中心超过开口半径）" in line_with(summarize(plain, lang="zh"), "IR 光阑"), "no heights: as before"


# ── the three kinds of problem ───────────────────────────────────────────────────────────────


def test_off_the_axis_in_height_by_more_than_a_fifth_of_the_aperture():
    got = problems(fixture("height_passes_over_and_under"))
    off = [p for p in got if p.startswith("off its axis in height")]
    # 12.7 of 25.4 is 50 %; 2.0 of the iris's 5 mm opening is 40 %
    assert off == ["off its axis in height by more than 20% of the aperture: las1 532 nm hits M_EDGE mirror 12.7 mm below "
                   "its axis (50% of the 25.4 mm aperture)",
                   "off its axis in height by more than 20% of the aperture: las1 532 nm hits IRIS iris 2.0 mm below "
                   "its axis (40% of the 5 mm opening)"]
    zh = [p for p in problems(fixture("height_passes_over_and_under"), "zh") if p.startswith("高度偏离")]
    assert zh[1] == "高度偏离 > 20% 口径：las1 532 nm 在 IRIS 光阑 上比光轴低 2.0 mm（开口 5 mm 的 40%）"
    # 20 % itself is not more than 20 %; a blocked hit is not one of the four actions
    edge = layout([laser("las1", 0, 100), mirror("m1", 100, 100, 135, props={"z_mm": 75 + 5.08}), det("d", 100, 300, 270)])
    assert not [p for p in problems(edge) if p.startswith("off its axis in height")]


def test_missed_in_height_is_a_component_nothing_lights_that_a_beam_passes():
    got = problems(fixture("height_periscope_up"))
    assert ("missed in height: nothing lights M_LOW mirror; the beam las1 532 nm passes over it (beam at 250 mm, its axis "
            "at 75 mm, half aperture 12.7 mm)") in got
    zh = problems(fixture("height_periscope_up"), "zh")
    assert ("高度错过：M_LOW 反射镜 没被任何光照到；las1 532 nm 的光从它上方越过（光高 250 mm，它的光轴在 75 mm，口径半宽 12.7 mm）"
            in zh)
    # a component another beam lights is not missed, whatever passes it: M1 at 200 mm is passed by las1 (75) and hit
    # by las2 (200)
    lit = layout([laser("las1", 0, 100), mirror("m1", 200, 100, 135, props={"z_mm": 200}), det("d", 400, 100, 180),
                  comp("las2", "laser", 100, 100, 0, props={"wavelength_nm": 532, "z_mm": 200}),
                  det("u", 200, 300, 270, props={"z_mm": 200})])
    tr = trace_layout(lit)
    assert any(p["component"] == "m1" for s in tr[0]["branches"][0]["segments"] for p in s["passes"])
    assert "m1" in {e["component"] for b in tr[1]["branches"] for e in b["events"]}
    assert not [p for p in problems(lit) if p.startswith("missed in height")]


def _window_passed_three_times() -> dict:
    """A 300 mm window (its face x = 300, y 50 to 350) with its axis at 300 mm; three beams along +x cross its face at
    y = 100, 200, 300: two at 75 mm (dz -225), one at 140 mm (dz -160) - all more than 150 below: passed."""
    return layout([laser("l1", 0, 100), laser("l2", 0, 200), comp("l3", "laser", 0, 300, props={"wavelength_nm": 532, "z_mm": 140}),
                   comp("w", "window", 300, 200, 0, aperture=300, props={"z_mm": 300})])


def test_missed_in_height_is_grouped_by_component_with_the_closest_pass():
    got = [p for p in problems(_window_passed_three_times()) if p.startswith("missed in height")]
    assert got == ["missed in height: nothing lights W window; the beam l3 532 nm passes under it (beam at 140 mm, its "
                   "axis at 300 mm, half aperture 150 mm) (3 beams in all)"]
    zh = [p for p in problems(_window_passed_three_times(), "zh") if p.startswith("高度错过")]
    assert zh == ["高度错过：W 窗片 没被任何光照到；l3 532 nm 的光从它下方越过（光高 140 mm，它的光轴在 300 mm，口径半宽 150 mm）"
                  "（共 3 束光）"]


def test_missed_in_height_is_not_judged_on_a_cut_trace():
    """L1 passes under W and ends on D1 (the one event the budget allows); L2's first hit is cut: what no beam does
    is not concluded, so W, passed and lit by nothing, is not "missed in height" - the light that would light it may
    be the light that was not traced (as v1's near misses)."""
    lay = layout([laser("l1", 0, 100), comp("w", "window", 200, 100, 0, props={"z_mm": 300}), det("d1", 400, 100, 180),
                  laser("l2", 0, 300), det("d2", 400, 300, 180)])
    whole = trace_layout(lay)
    assert [p for p in problems(lay) if p.startswith("missed in height")]
    cut = trace_layout(lay, max_trace_events=1)
    assert cut[0]["branches"][0]["segments"][0]["passes"][0]["component"] == "w" and whole != cut
    text = summarize(lay, cut, lang="en")
    assert "trace cut" in text and "missed in height" not in text


def test_a_spot_beyond_the_aperture_in_height_above_below_and_on_the_axis():
    got = [p for p in problems(fixture("height_mixed")) if p.startswith("spot beyond the aperture in height")]
    # D_S: aperture 6, dz +3, d = 1.757676 (contract 3D §9): 3 + 0.878838 - 3
    assert got == ["spot beyond the aperture in height: the beam las1 785 nm is Ø1.76 mm across on D_S detector, 3.0 mm "
                   "above its axis, 0.88 mm beyond its upper edge (half aperture 3 mm)"]

    def spot(z_det: float, aperture: float) -> list[str]:
        lay = layout([comp("l1", "laser", 0, 100, props={"wavelength_nm": 633, "beam_diameter_mm": 4}),
                      det("d", 200, 100, 180, aperture=aperture, props={"z_mm": z_det})])
        return [p for p in problems(lay) if "in height" in p and p.startswith("spot")]

    below = spot(77.0, 6.0)          # dz -2: 2 + 2.0 - 3 = 1.0 (d grows a little over 200 mm)
    assert len(below) == 1 and "2.0 mm below its axis" in below[0] and "beyond its lower edge" in below[0]
    on_axis = spot(75.0, 3.0)        # dz 0, a 4 mm spot on a 3 mm aperture: beyond both edges
    assert len(on_axis) == 1 and "on its axis" in on_axis[0] and "beyond both edges" in on_axis[0]
    zh = [p for p in problems(layout([comp("l1", "laser", 0, 100, props={"wavelength_nm": 633, "beam_diameter_mm": 4}),
                                      det("d", 200, 100, 180, aperture=3.0, props={"z_mm": 75})]), "zh")
          if p.startswith("光斑在高度方向")]
    assert len(zh) == 1 and "在光轴上，上下沿各超出" in zh[0]
    assert spot(76.0, 8.0) == []      # 1 + 2.0 < 4: it fits


def test_the_kinds_of_heights_come_in_the_agreed_order():
    """Contract 3D §6.3: off center -> off in height -> near misses -> missed in height -> spots -> spots in height."""
    lay = layout([
        comp("l1", "laser", 0, 100, props={"wavelength_nm": 633, "beam_diameter_mm": 30}),
        comp("w1", "window", 100, 100, 0),                                           # spot larger than its 25.4 mm
        det("d1", 300, 105, 180, aperture=20, props={"z_mm": 80}),                     # 5 mm off center, 5 mm off in height
        laser("l2", 0, 500), mirror("m4", 60, 520, 180),                               # passes just outside M4
        comp("hi", "mirror", 200, 500, 135, props={"z_mm": 300}),                     # passed under, lit by nothing
        det("d2", 400, 500, 180),
    ])
    kinds = ["off center", "off its axis in height", "missed aperture", "missed in height", "spot larger",
             "spot beyond the aperture in height"]
    got = [next((k for k in kinds if p.startswith(k)), None) for p in problems(lay)]
    runs = [k for i, k in enumerate(got) if k is not None and (i == 0 or got[i - 1] != k)]      # one per run of a kind
    assert runs == kinds, problems(lay)


def test_a_trace_that_records_its_last_pass_says_so():
    base = [laser("las1", 15, 100), mirror("ma", 2000, 100, 180), mirror("mb", 10, 100, 0)]
    windows = [comp(f"w{i}", "window", 20 + 4.9 * i, 100, 0, props={"z_mm": 500}) for i in range(397)]
    lay = layout(base + windows, width=2100.0)
    tr = trace_layout(lay)
    assert sum(len(s["passes"]) for t in tr for b in t["branches"] for s in b["segments"]) == MAX_TRACE_PASSES
    assert f"（越过的器件记满了 {MAX_TRACE_PASSES} 条，之后的可能没有记）" in summarize(lay, tr, lang="zh")
    assert "later ones may be missing" in summarize(lay, tr, lang="en")
    assert "later ones may be missing" not in summarize(fixture("height_mixed"), lang="en")


def test_a_focus_in_the_vertical_run_of_a_periscope_is_found_and_measured_from_it():
    """A lens focuses a collimated beam 100 mm on; 50 mm after it a periscope goes up 100 mm, so the waist lies in the
    vertical run, about 50 mm after the periscope (contract 3D §3.4: the run is path)."""
    lay = layout([comp("l1", "laser", 0, 100, props={"wavelength_nm": 633, "beam_diameter_mm": 4}),
                  comp("f1", "lens", 50, 100, 0, label="F1", props={"focal_mm": 100}),
                  comp("p1", "periscope", 100, 100, 0, label="P1", props={"z_out_mm": 175}),
                  det("d", 400, 100, 180, props={"z_mm": 175})])
    en = summarize(lay, lang="en")
    focus = [ln.strip() for ln in en.splitlines() if ln.strip().startswith("focus:")]
    assert len(focus) == 1 and focus[0].endswith("after P1, " + focus[0].split(", ")[-1]), focus
    d = float(focus[0].split()[1])
    assert 49.9 < d <= 50.0, focus         # f = 100 from a waist 19.9 m long: just short of the focal length (2 decimals)
    # without the periscope's run the same beam has its focus on the segment after it, 150 mm past the lens
    flat = layout([comp("l1", "laser", 0, 100, props={"wavelength_nm": 633, "beam_diameter_mm": 4}),
                   comp("f1", "lens", 50, 100, 0, label="F1", props={"focal_mm": 100}),
                   comp("p1", "periscope", 100, 100, 0, label="P1"), det("d", 400, 100, 180)])
    flat_focus = [ln.strip() for ln in summarize(flat, lang="en").splitlines() if ln.strip().startswith("focus:")]
    assert len(flat_focus) == 1 and float(flat_focus[0].split()[1]) == pytest.approx(d, abs=0.01)
