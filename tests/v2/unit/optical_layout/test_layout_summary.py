"""The text an LLM reads about a layout (contract §7), in Chinese and in English.

An exact narrative is pinned for a small layout, so a change to what a model reads is a visible
change; the rest checks the parts of §7 one by one: the header, the order along the beam, the
annotations and links, splits written branch by branch, each kind of problem, the component list
of a layout without a light source, the comments, the language switch and the 8000-character cut.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from mast.i18n import use_lang
from mast.optical_layout.model import Layout
from mast.optical_layout.summary import MAX_CHARS, summarize
from mast.optical_layout.trace import trace_layout

from ._layouts import comp, dense_field, det, laser, layout, mirror

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "optical_layout"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["layout"]


def lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines()]


# ── the narrative, pinned ────────────────────────────────────────────────────────────────────

DOGLEG_ZH = """\
光路图「Two-mirror walk」（id dogleg，第 1 版）
平台 1200 × 900 mm，孔距 25 mm（公制）；器件 4 个，其中光源 1 个；最近一次保存：fixture 2026-01-01 00:00 UTC
坐标：平台左下角为原点，x 向右，y 向上，单位 mm；角度从 +x 逆时针量。

光路（沿光束的顺序）：
1) L1 激光器（532 nm，在 (50, 62.5) mm，电动：否）
   → M1 反射镜（在 (112.5, 62.5) mm，命中偏离中心 0.0 mm，入射 45.0°，累计 62.5 mm）
   → M2 反射镜（在 (112.5, 187.5) mm，命中偏离中心 0.0 mm，入射 45.0°，累计 187.5 mm）
   → 终止于 D1 探测器（在 (300, 187.5) mm，命中偏离中心 0.0 mm，入射 0.0°，累计 375.0 mm）

需要注意：没有发现问题。
"""

DOGLEG_EN = """\
Optical layout "Two-mirror walk" (id dogleg, revision 1)
Table 1200 x 900 mm, hole pitch 25 mm (metric); 4 components, 1 light source; last saved by fixture 2026-01-01 00:00 UTC
Coordinates: origin at the table's lower left, x to the right, y up, in mm; angles counter-clockwise from +x.

Beam paths (in the order along the beam):
1) L1 laser (532 nm, at (50, 62.5) mm, motorized: no)
   → M1 mirror (at (112.5, 62.5) mm, hit 0.0 mm off center, incidence 45.0 deg, path 62.5 mm)
   → M2 mirror (at (112.5, 187.5) mm, hit 0.0 mm off center, incidence 45.0 deg, path 187.5 mm)
   → ends at D1 detector (at (300, 187.5) mm, hit 0.0 mm off center, incidence 0.0 deg, path 375.0 mm)

Things to look at: none found.
"""


def test_the_narrative_of_a_two_mirror_layout_is_exactly_this():
    lay = fixture("two_mirror_dogleg")
    assert summarize(lay, lang="zh") == DOGLEG_ZH
    assert summarize(lay, lang="en") == DOGLEG_EN


def test_the_language_follows_the_request_unless_one_is_asked_for():
    lay = fixture("two_mirror_dogleg")
    with use_lang("en"):
        assert summarize(lay) == DOGLEG_EN
        assert summarize(lay, lang="zh") == DOGLEG_ZH
    with use_lang("zh"):
        assert summarize(lay) == DOGLEG_ZH


def test_a_dict_a_layout_and_a_precomputed_trace_give_the_same_text():
    raw = fixture("two_mirror_dogleg")
    lay = Layout.model_validate(raw)
    assert summarize(raw, lang="en") == summarize(lay, lang="en") == summarize(lay, trace_layout(lay), lang="en")


# ── annotations, links, motors ───────────────────────────────────────────────────────────────


def _annotated() -> dict:
    c = [laser("las1", 50, 62.5, 0, 532, label="L1", manufacturer="Cobolt", model="Samba", function="pump"),
         comp("hwp1", "waveplate", 150, 62.5, 0, label="HWP1", props={"retardance": "half"}, manufacturer="Thorlabs",
              model="WPH10", motorized=True, actuator="K10CR1", connection="COM15", notes="remember to re-zero"),
         comp("obj1", "objective", 250, 62.5, 180, label="OBJ1", props={"focal_mm": 2.0}, extra=[{"key": "mount", "value": "KM100"}]),
         det("det1", 400, 62.5, 180, label="D1")]
    c[1]["links"] = {"device_id": "hwp_rot", "slot": "excitation_waveplate"}
    return layout(c, lid="shg", title="SHG", updated_by="ana", updated_at="2026-11-03T12:00:00+00:00", revision=12)


def test_each_component_carries_its_annotations_and_motor_and_links():
    text = summarize(_annotated(), lang="en")
    assert "L1 laser (532 nm, Cobolt Samba, power" not in text and "L1 laser (532 nm, Cobolt Samba, function: pump, at (50, 62.5) mm, motorized: no)" in text
    hwp = next(ln for ln in lines(text) if "HWP1" in ln)
    for needle in ("Thorlabs WPH10", "half-wave", "motorized: yes, K10CR1, connected to COM15", "MAST device hwp_rot",
                   "slot excitation_waveplate", "note: remember to re-zero", "path 100.0 mm"):
        assert needle in hwp, (needle, hwp)
    assert "focal length 2 mm" in text and "mount: KM100" in text


def test_the_state_behind_a_link_is_written_when_it_is_known():
    links = {"hwp1": {"device": {"id": "hwp_rot", "known": True, "enabled": True, "connected": True,
                                 "status": {"angle_deg": 22.5, "moving": False}},
                      "slot": {"id": "excitation_waveplate", "known": True, "value": "hwp_532", "valid": True, "unit": None}}}
    en = next(ln for ln in lines(summarize(_annotated(), links=links, lang="en")) if "HWP1" in ln)
    assert "MAST device hwp_rot (open; angle_deg=22.5, moving=False)" in en and "slot excitation_waveplate currently = hwp_532" in en
    zh = next(ln for ln in lines(summarize(_annotated(), links=links, lang="zh")) if "HWP1" in ln)
    assert "MAST 设备 hwp_rot（已打开；angle_deg=22.5, moving=False）" in zh and "槽位 excitation_waveplate 当前 = hwp_532" in zh
    for device, needle in (({"id": "x", "known": False}, "not in the device list"), ({"id": "x", "known": True, "enabled": False}, "disabled"),
                           ({"id": "x", "known": True, "enabled": True, "connected": False}, "not open"),
                           ({"id": "x", "known": None, "error": "boom"}, "state unreadable: boom")):
        got = summarize(_annotated(), links={"hwp1": {"device": device}}, lang="en")
        assert needle in got, needle
    for slot, needle in (({"known": False}, "no such slot"), ({"known": True, "value": None}, "is not signed"),
                         ({"known": True, "value": "hwp_532", "valid": False}, "expired")):
        assert needle in summarize(_annotated(), links={"hwp1": {"slot": slot}}, lang="en"), needle


def test_the_header_names_the_title_the_revision_the_table_and_who_saved_it():
    en = lines(summarize(_annotated(), lang="en"))
    assert en[0] == 'Optical layout "SHG" (id shg, revision 12)'
    assert "4 components, 1 light source; last saved by ana 2026-11-03 12:00 UTC" in en[1]
    assert lines(summarize(layout([], lid="x"), lang="zh"))[0] == "光路图「x」（id x，第 0 版）"


# ── splits ───────────────────────────────────────────────────────────────────────────────────


def test_a_split_is_written_branch_by_branch_with_the_reflected_one_first():
    text = summarize(fixture("beam_splitter_cascade"), lang="en")
    body = lines(text)
    first, second = body.index(next(ln for ln in body if "reflected branch 0.r" in ln)), body.index(next(ln for ln in body if "transmitted branch 0.t" in ln))
    assert first < second
    assert "(intensity 50%)" in body[first] and any("reflected branch 0.t.r (intensity 25%)" in ln for ln in body)
    assert any("splits" in ln and "BS2" in ln for ln in body)
    zh = summarize(fixture("beam_splitter_cascade"), lang="zh")
    assert "↳ 反射支 0.r（强度 50%）" in zh and "↳ 透射支 0.t.t（强度 25%）" in zh
    indent = lambda ln: len(ln) - len(ln.lstrip())  # noqa: E731
    sub = next(ln for ln in body if "reflected branch 0.t.r" in ln)
    assert indent(sub) > indent(next(ln for ln in body if "transmitted branch 0.t " in ln))


def test_a_dichroic_says_whether_it_reflects_or_transmits_and_each_wavelength_is_its_own_path():
    en = summarize(fixture("dichroic_by_wavelength"), lang="en")
    assert "1) L1 laser (532 nm" in en and "2) L1 laser (700 nm" in en and "3) L1 laser (1064 nm" in en
    assert "DM1 dichroic mirror (edge 700 nm, reflects short wavelengths" in en and "reflects)" in en and "transmits)" in en


def test_the_end_of_each_kind_of_beam_is_in_words():
    en = summarize(fixture("filter_blocks_and_passes"), lang="en")
    assert "blocked by F1 filter" in en and "the wavelength is outside the passband" in en
    assert "the shutter is closed" in summarize(fixture("shutter_closed_and_open"), lang="en")
    assert "beyond the open radius" in summarize(fixture("iris_open_diameter"), lang="en")
    assert "event limit (64) reached" in summarize(fixture("max_events_ping_pong"), lang="en")
    assert "light comes in at FI1" in summarize(fixture("fiber_coupling_continues"), lang="en")


# ── problems ─────────────────────────────────────────────────────────────────────────────────


def problems(lay, **kw) -> list[str]:
    out = lines(summarize(lay, lang="en", **kw))
    start = next((i for i, ln in enumerate(out) if ln.startswith("Things to look at")), None)
    if start is None:
        return []
    body = []
    for ln in out[start + 1:]:
        if not ln.startswith("- "):
            break
        body.append(ln[2:])
    return body


def test_a_clean_layout_says_so():
    assert problems(fixture("two_mirror_dogleg")) == []
    assert "Things to look at: none found." in summarize(fixture("two_mirror_dogleg"), lang="en")


def test_a_beam_on_a_back_side_is_a_problem():
    got = problems(fixture("back_side_hits"))
    assert any(p.startswith("back side: the beam las1 532 nm hits the back of M1 mirror") for p in got)
    assert any("D1 detector" in p and "back side" in p for p in got)


def test_a_hit_more_than_a_fifth_of_the_aperture_off_center_is_a_problem():
    got = problems(fixture("off_center_hits"))
    assert [p for p in got if "off center" in p] == [
        "off center by more than 20% of the aperture: las1 532 nm hits M1 mirror -8.9 mm from its center (35% of the 25.4 mm aperture)",
        "off center by more than 20% of the aperture: las2 532 nm hits M1 mirror 8.9 mm from its center (35% of the 25.4 mm aperture)"]
    # exactly at the limit is not a problem: 0.2 * 25.4 = 5.08
    off_center = lambda y: [p for p in problems(layout([laser("las1", 0, y), mirror("m1", 100, 100, 180)])) if "off center" in p]  # noqa: E731
    assert off_center(100 + 5.0) == []
    assert len(off_center(100 + 5.2)) == 1


def test_a_beam_that_leaves_the_table_is_a_problem_with_where():
    got = problems(fixture("leaves_table"))
    assert "leaves the table: las1 532 nm (branch 0) goes out at (2200, 62.5) mm" in got
    assert any("las3" in p and "(-850, -1000)" in p for p in got)


def test_a_near_miss_is_reported_for_a_component_nothing_else_lights():
    got = problems(fixture("miss_aperture"))
    assert any(p.startswith("missed aperture: nothing lights M1 mirror; the beam las1 532 nm passes its surface 8.5 mm beyond") for p in got)
    assert "components no beam reaches (1): M1" in got
    # a component that another beam does hit is not reported as a miss
    lay = layout([laser("las1", 0, 100), laser("las2", 0, 140.0), mirror("m1", 100, 100, 180), det("d", 0, 100, 0)])
    assert not [p for p in problems(lay) if p.startswith("missed aperture")]


def test_components_no_beam_reaches_are_named():
    lay = layout([laser("las1", 0, 100), det("d", 300, 100, 180), mirror("m2", 100, 500, 180, label="Spare"), comp("w", "window", 100, 600, 0)])
    assert "components no beam reaches (2): Spare, W" in problems(lay)


def test_a_loop_and_a_broken_fiber_link_are_problems():
    assert any("event limit reached" in p for p in problems(fixture("max_events_ping_pong")))
    lay = layout([laser("las1", 0, 100), comp("fi", "fiber_in", 100, 100, 180, props={"continues_at": "nobody"})])
    assert any(p.startswith("broken fiber link") for p in problems(lay))
    lay = layout([comp("fi", "fiber_in", 100, 100, 180, props={"continues_at": "fo"}),
                  comp("fo", "fiber_out", 100, 300, 0, props={"wavelength_nm": 450}), laser("las1", 0, 500)])
    assert any("fiber output FO gets no light" in p for p in problems(lay))


def test_a_long_problem_list_is_cut_and_says_how_many_more():
    lay = layout([laser(f"l{i}", 10 + 10 * i, 100) for i in range(20)])         # twenty beams, all leaving the table
    en = lines(summarize(lay, lang="en"))
    head = next(i for i, ln in enumerate(en) if ln.startswith("Things to look at"))
    assert en[head] == "Things to look at (20):"
    shown = [ln for ln in en[head + 1:] if ln.startswith("- ")]
    assert len(shown) == 13 and shown[-1] == "- ... 8 more" and all("leaves the table" in ln for ln in shown[:12])


# ── a layout without a light source ──────────────────────────────────────────────────────────


def test_without_a_source_the_components_and_their_annotations_are_listed():
    lay = layout([mirror("m1", 112.5, 62.5, 135, label="M1", manufacturer="Thorlabs", model="BB1-E02", function="steering"),
                  comp("hwp", "waveplate", 150, 62.5, 0, label="HWP", props={"retardance": "half"}, motorized=True, actuator="K10CR1")])
    en = summarize(lay, lang="en")
    assert "has no light source yet" in en and "the layout has no light source (laser, fiber output, lamp, LED)" in en
    assert "- M1 mirror (Thorlabs BB1-E02, function: steering, at (112.5, 62.5) mm, facing 135 deg)" in en
    assert "- HWP waveplate (half-wave, at (150, 62.5) mm, facing 0 deg, motorized: yes, K10CR1)" in en
    zh = summarize(lay, lang="zh")
    assert "图里还没有光源" in zh and "- M1 反射镜（Thorlabs BB1-E02，作用：steering，在 (112.5, 62.5) mm，朝向 135°）" in zh
    assert "（没有器件）" in summarize(layout([]), lang="zh")


# ── comments ─────────────────────────────────────────────────────────────────────────────────


COMMENTS = [{"id": "c1", "author": "operator", "by": "ana", "text": "Is M1 really 25.4 mm?", "component_id": "m1"},
            {"id": "c2", "author": "agent", "by": "ext:claude", "text": "The beam\nmisses M2.", "component_id": None}]


def test_unresolved_comments_are_listed_with_who_wrote_them():
    en = summarize(fixture("two_mirror_dogleg"), comments=COMMENTS, lang="en")
    assert "Unresolved comments (2):" in en
    assert "- c1 [operator ana · m1] Is M1 really 25.4 mm?" in en and "- c2 [agent ext:claude] The beam misses M2." in en
    zh = summarize(fixture("two_mirror_dogleg"), comments=COMMENTS, lang="zh")
    assert "未解决的评论（2 条）：" in zh and "- c1 [操作员 ana · m1]" in zh
    assert "comments" not in summarize(fixture("two_mirror_dogleg"), lang="en").lower().replace("unresolved comments", "")


def test_many_comments_are_cut_to_ten_lines():
    many = [{"id": f"c{i}", "author": "agent", "by": "ext:a", "text": "x" * 500, "component_id": None} for i in range(25)]
    en = lines(summarize(fixture("two_mirror_dogleg"), comments=many, lang="en"))
    shown = [ln for ln in en if ln.startswith("- c")]
    assert len(shown) == 10 and "- ... 15 more" in en
    assert all(len(ln) < 400 for ln in shown), "one comment does not take the whole page"


# ── the length cap ───────────────────────────────────────────────────────────────────────────


def _big() -> dict:
    comps = [laser("las1", 0, 100)] + [comp(f"w{i}", "window", 10 + i, 100, 0, label=f"WINDOW-{i}", manufacturer="Acme Optics",
                                              model="W-1234", function="clean up the beam", notes="n" * 100) for i in range(60)]
    return layout(comps)


def test_a_long_description_is_cut_at_8000_characters_and_says_so():
    en = summarize(_big(), lang="en")
    assert len(en) == MAX_CHARS and en.endswith("... (summary cut at 8000 characters)")
    zh = summarize(_big(), lang="zh")
    assert len(zh) <= MAX_CHARS and zh.endswith("…（摘要在 8000 字符处截断）")
    short = summarize(_big(), lang="en", limit=500)
    assert len(short) == 500 and short.endswith("(summary cut at 500 characters)")
    assert len(summarize(fixture("two_mirror_dogleg"), lang="en")) < 1000 and "cut at" not in summarize(fixture("two_mirror_dogleg"), lang="en")


def test_the_text_is_deterministic_and_leaves_its_input_alone():
    raw = _annotated()
    before = copy.deepcopy(raw)
    assert summarize(raw, lang="zh") == summarize(raw, lang="zh") and raw == before


# ── a trace the budget cut ───────────────────────────────────────────────────────────────────


def cut_text(lang: str = "en", **kw) -> str:
    lay = fixture("trace_budget_cut")
    return summarize(lay, trace_layout(lay, max_trace_events=3), lang=lang, **kw)


def test_a_cut_trace_says_so_at_the_top_and_as_the_first_problem():
    lay = fixture("trace_budget_cut")
    trace = trace_layout(lay, max_trace_events=3)
    text = cut_text()
    notice = ("trace cut: the layout reached its limit of 3 events (3 branches, 1 beam); it stops in the beam "
              "las1 532 nm, branch 0.t, and nothing after that is traced")
    assert notice in text
    assert text.index(notice) < text.index("Beam paths")                    # up in the header, not only in the list
    got = problems(lay, trace=trace)
    assert got and got[0].startswith(notice)                                # and the first thing to look at
    assert "Things to look at (1):" in text


def test_the_narrative_says_where_the_trace_stops():
    assert "the trace is cut here (the layout's event limit was reached); nothing is traced beyond" in cut_text()
    assert "追迹在这里被截断" in cut_text("zh") and "追迹被截断：整张图的作用次数达到上限（3 次，3 支，1 棵光路树）" in cut_text("zh")


def test_what_no_beam_does_is_not_concluded_from_a_trace_that_stops_short():
    """D_2 is not reached only because L2 was never traced: it is not "a component no beam reaches"."""
    lay = fixture("trace_budget_cut")
    got = problems(lay, trace=trace_layout(lay, max_trace_events=3))
    assert got[0].startswith("trace cut:") and len(got) == 1, got       # only the notice (which names what it left out)
    # the same layout traced to the end is clean
    assert problems(lay, trace=trace_layout(lay)) == []
    # a fibre output is not "without light" when the trace stopped before the fibre input
    fib = fixture("fiber_coupling_continues")
    cut = problems(fib, trace=trace_layout(fib, max_trace_events=1))
    assert cut and cut[0].startswith("trace cut") and not [p for p in cut[1:] if "gets no light" in p], cut


def test_what_a_cut_trace_did_see_is_still_reported():
    lay = fixture("back_side_hits")
    got = problems(lay, trace=trace_layout(lay, max_trace_events=1))
    assert got[0].startswith("trace cut") and any(p.startswith("back side:") for p in got)


def test_a_complete_trace_has_no_cut_line_even_when_it_used_the_whole_budget():
    lay = fixture("trace_budget_cut")
    for trace in (trace_layout(lay), trace_layout(lay, max_trace_events=5)):
        assert "trace cut" not in summarize(lay, trace, lang="en") and "追迹被截断" not in summarize(lay, trace, lang="zh")


def test_the_notice_is_still_there_when_the_text_is_cut_at_8000_characters():
    """The notice is up in the header, because the list of problems is at the end and the text is
    cut from the end."""
    lay = dense_field()
    text = summarize(lay, trace_layout(lay, max_trace_events=400), lang="en")
    assert len(text) == MAX_CHARS and text.endswith("... (summary cut at 8000 characters)")
    assert "trace cut: the layout reached its limit of 400 events" in text[:1200]
