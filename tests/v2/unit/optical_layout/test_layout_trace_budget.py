"""The budget of a whole trace (contract section 11): at most ``MAX_TRACE_EVENTS`` events in all.

The per-lineage cap (64) and the fraction floor bound one ray, not the trace: a compliant 60 KB
layout - 361 beam splitters packed at 45 degrees and 32 lasers with 16 wavelengths each - traces to
233 760 branches and 217 328 events and took 17 s. These tests pin the rule
that bounds it: where the cut falls, that the order is the one the front end also uses, that
nothing is traced after it, that a trace needing exactly the budget is complete, and that no fixture
and no realistic layout is ever cut.
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

from mast.optical_layout import trace as T
from mast.optical_layout.trace import trace_layout, truncation

from ._layouts import budget_layout, comp, dense_field, det, laser, layout, mirror

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "optical_layout"


def flat_events(trace) -> list[dict]:
    """The events in the order the trace was produced (the branches are listed in that order)."""
    return [ev for tree in trace for br in tree["branches"] for ev in br["events"]]


def spine(trace) -> list[tuple]:
    """(source, wavelength, branch id) of every branch, in order."""
    return [(t["source"], t["wavelength_nm"], b["branch"]) for t in trace for b in t["branches"]]


def budget_branches(trace) -> list[tuple]:
    return [(t["source"], b["branch"]) for t in trace for b in t["branches"] if b["end"] == "budget"]


# ── the rule ───────────────────────────────────────────────────────────────────────────────


def test_the_budget_is_the_number_the_contract_names():
    assert T.MAX_TRACE_EVENTS == 5000


def test_a_trace_that_needs_exactly_the_budget_is_complete_and_one_more_event_cuts_it():
    lay = budget_layout()
    full = trace_layout(lay)
    assert len(flat_events(full)) == 5 and truncation(full) is None
    assert trace_layout(lay, max_trace_events=5) == full           # exactly enough: complete
    assert trace_layout(lay, max_trace_events=500) == full
    four = trace_layout(lay, max_trace_events=4)                   # one short
    assert truncation(four) is not None and len(flat_events(four)) == 4
    for n in range(0, 5):
        cut = trace_layout(lay, max_trace_events=n)
        assert len(flat_events(cut)) == n, n                       # never more, never fewer
        assert budget_branches(cut) and len(budget_branches(cut)) == 1, n


def test_the_default_budget_is_the_module_constant_read_when_the_trace_is_made(monkeypatch):
    lay = budget_layout()
    with monkeypatch.context() as m:
        m.setattr(T, "MAX_TRACE_EVENTS", 2)
        assert len(flat_events(trace_layout(lay))) == 2
        assert len(flat_events(trace_layout(lay, max_trace_events=4))) == 4      # an explicit one wins
    assert len(flat_events(trace_layout(lay))) == 5


def test_a_negative_budget_is_no_budget_at_all_not_a_crash():
    cut = trace_layout(budget_layout(), max_trace_events=-3)
    assert flat_events(cut) == [] and cut[0]["branches"][0]["end"] == "budget"


@pytest.mark.parametrize("name", ["budget", "cascade", "fiber", "ping_pong", "dichroic"])
def test_a_cut_trace_is_the_beginning_of_the_whole_one_in_the_order_it_is_made(name):
    """For every budget n: the events kept are the first n of the complete trace, the branches before
    the cut branch are the complete trace's branches, and nothing follows the cut branch."""
    lay = {
        "budget": budget_layout(),
        "cascade": layout([laser("las1", 50, 62.5, 0, 532), comp("bs1", "beam_splitter", 150, 62.5, 135),
                           comp("bs2", "beam_splitter", 250, 62.5, 135), det("da", 150, 250, 270),
                           det("db", 250, 250, 270), det("dc", 400, 62.5, 180)]),
        "fiber": json.loads((FIXTURES / "fiber_coupling_continues.json").read_text(encoding="utf-8"))["layout"],
        "ping_pong": json.loads((FIXTURES / "max_events_ping_pong.json").read_text(encoding="utf-8"))["layout"],
        "dichroic": json.loads((FIXTURES / "dichroic_by_wavelength.json").read_text(encoding="utf-8"))["layout"],
    }[name]
    full = trace_layout(lay)
    total = len(flat_events(full))
    assert total >= 5
    assert truncation(full) is None
    for n in range(0, total + 2):
        cut = trace_layout(lay, max_trace_events=n)
        if n >= total:
            assert cut == full, n
            continue
        assert flat_events(cut) == flat_events(full)[:n], n
        marks = budget_branches(cut)
        assert len(marks) == 1, (n, marks)
        last_tree = cut[-1]
        assert last_tree["branches"][-1]["end"] == "budget", n                    # the last branch of the last tree
        head = spine(cut)
        assert head == spine(full)[:len(head)], n                                 # the same trees and branches, in order
        # the branches before the cut one are the complete ones
        assert cut[:-1] == full[:len(cut) - 1], n
        done = last_tree["branches"][:-1]
        assert done == full[len(cut) - 1]["branches"][:len(done)], n


def test_the_cut_branch_runs_to_the_hit_it_could_not_record_and_has_one_segment_more_than_events():
    lay = budget_layout()
    full = trace_layout(lay)
    for n in range(0, 5):
        cut = trace_layout(lay, max_trace_events=n)
        br = cut[-1]["branches"][-1]
        assert br["end"] == "budget" and len(br["segments"]) == len(br["events"]) + 1, n
        nxt = flat_events(full)[n]
        end = br["segments"][-1]
        assert (end["x1"], end["y1"]) == (nxt["x"], nxt["y"]), n                  # up to the hit it could not record
        for a, b in zip(br["segments"], br["segments"][1:]):
            assert (a["x1"], a["y1"]) == (b["x0"], b["y0"])


def test_the_budget_is_for_the_whole_trace_not_for_each_tree():
    """Four events kept in all: the first tree takes all four, the second tree is cut at its first hit
    (a per-tree budget would let it have four of its own)."""
    cut = trace_layout(budget_layout(), max_trace_events=4)
    assert [t["source"] for t in cut] == ["las1", "las2"]
    assert len(flat_events(cut[:1])) == 4
    assert cut[1]["branches"] == [{"branch": "0", "fraction": 1.0, "events": [], "end": "budget",
                                   "segments": [{"x0": 50.0, "y0": 400.0, "x1": 300.0, "y1": 400.0}]}]


def test_the_reflected_branch_is_followed_to_its_end_before_the_transmitted_one_is_cut():
    cut = trace_layout(budget_layout(), max_trace_events=3)
    assert [t["source"] for t in cut] == ["las1"]                      # las2 is never traced
    by = {b["branch"]: b for b in cut[0]["branches"]}
    assert list(by) == ["0", "0.r", "0.t"]
    assert [e["component"] for e in by["0.r"]["events"]] == ["m1", "det_r"] and by["0.r"]["end"] == "stop"
    assert by["0.t"]["events"] == [] and by["0.t"]["end"] == "budget"


def test_the_trace_stops_where_it_is_cut_and_a_fibre_continuation_is_not_started():
    """A fibre output lit by a fibre input is another tree, found after the direct sources. Cut inside
    the direct sources there is none; when the direct sources fit it starts and is cut at its first hit."""
    lay = json.loads((FIXTURES / "fiber_coupling_continues.json").read_text(encoding="utf-8"))["layout"]
    full = trace_layout(lay)
    assert [(t["source"], t["wavelength_nm"]) for t in full] == [
        ("las1", 532.0), ("las3", 532.0), ("las4", 633.0), ("fo1", 532.0), ("fo1", 633.0)]
    assert [len(flat_events([t])) for t in full] == [1, 1, 1, 2, 2]
    for n in (0, 1, 2):                                                 # inside the three direct sources
        cut = trace_layout(lay, max_trace_events=n)
        assert [t["source"] for t in cut] == ["las1", "las3", "las4"][:n + 1], n
        assert len(budget_branches(cut)) == 1, n
    cut = trace_layout(lay, max_trace_events=3)                         # the direct sources just fit
    assert [t["source"] for t in cut] == ["las1", "las3", "las4", "fo1"]
    assert cut[3]["branches"][0]["events"] == [] and cut[3]["branches"][0]["end"] == "budget"
    cut = trace_layout(lay, max_trace_events=4)                         # the continuation gets the mirror, not the detector
    assert [e["component"] for e in cut[3]["branches"][0]["events"]] == ["m1"]
    assert cut[3]["branches"][0]["end"] == "budget" and len(cut) == 4


def test_the_notice_is_read_off_the_trace_alone_and_survives_json():
    lay = budget_layout()
    assert truncation(trace_layout(lay)) is None
    assert truncation([]) is None
    cut = trace_layout(lay, max_trace_events=3)
    notice = truncation(cut)
    assert notice == {"reason": "events", "events": 3, "branches": 3, "trees": 1, "source": "las1",
                      "wavelength_nm": 532.0, "branch": "0.t"}
    assert truncation(json.loads(json.dumps(cut))) == notice
    cut4 = truncation(trace_layout(lay, max_trace_events=4))
    assert cut4 == {"reason": "events", "events": 4, "branches": 4, "trees": 2, "source": "las2",
                    "wavelength_nm": 633.0, "branch": "0"}


def test_the_notice_does_not_change_the_trace_it_is_read_from():
    cut = trace_layout(budget_layout(), max_trace_events=3)
    before = copy.deepcopy(cut)
    truncation(cut)
    assert cut == before


# ── the worst case ───────────────────────────────────────────────────────────────────


def test_the_audits_worst_case_is_cut_at_the_budget_and_fast():
    from mast.optical_layout.model import MAX_DOCUMENT_BYTES, Layout

    lay = dense_field()
    assert len(json.dumps(lay).encode()) <= MAX_DOCUMENT_BYTES
    Layout.model_validate(lay)                                          # a compliant layout
    started = time.perf_counter()
    trace = trace_layout(lay)
    elapsed = time.perf_counter() - started
    events = flat_events(trace)
    assert len(events) == T.MAX_TRACE_EVENTS
    notice = truncation(trace)
    assert notice is not None and notice["events"] == T.MAX_TRACE_EVENTS
    # a split makes at most two branches, so the one number bounds the branches too
    assert notice["branches"] <= notice["trees"] + 2 * notice["events"]
    assert len(json.dumps(trace)) < 4_000_000                           # was 61 MB
    assert elapsed < 10, f"{elapsed:.1f} s (it took 17 s before the budget; about 0.4 s now)"


# ── what must never be cut ───────────────────────────────────────────────────────────────────


def test_no_shared_fixture_is_cut_by_the_contracts_budget():
    for path in sorted(FIXTURES.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if "trace_options" in doc:
            continue                                                    # these are made with a smaller budget on purpose
        trace = trace_layout(doc["layout"])
        assert truncation(trace) is None, path.name
        assert len(flat_events(trace)) <= T.MAX_TRACE_EVENTS // 50, path.name      # at least 50 times the room


def realistic_layouts() -> dict[str, dict]:
    """Layouts a laboratory might draw, from ordinary to generous - none of them is a pathological field."""
    out: dict[str, dict] = {}
    # a TERS-like table: 3 lasers x 2 wavelengths, 12 mirrors, 4 splitters, a detector
    comps = [laser(f"las{s}", 20, 100 + 40 * s, 0, [532.0, 633.0], aperture=5.0) for s in range(3)]
    comps += [mirror(f"m{k}", 80 + 25 * k, 100 + 40 * (k % 3), 45 + 90 * (k % 2)) for k in range(12)]
    comps += [comp(f"bs{k}", "beam_splitter", 100 + 60 * k, 120, 45 if k % 2 else 135) for k in range(4)]
    comps += [det("d0", 500, 120, 180)]
    out["ters table"] = layout(comps, width=900, height=600)
    # an interferometer: a laser of four wavelengths through six splitters with six mirrors
    comps = [laser("las0", 10, 200, 0, [405.0, 532.0, 633.0, 785.0], aperture=5.0)]
    comps += [comp(f"bs{k}", "beam_splitter", 60 + 50 * k, 200, 45 if k % 2 else 135) for k in range(6)]
    comps += [mirror(f"m{k}", 60 + 50 * k, 200 + 60 * (1 if k % 2 else -1), 45 + 90 * (k % 2)) for k in range(6)]
    out["interferometer"] = layout(comps, width=900, height=600)
    # a beam-distribution board: one source of sixteen wavelengths down a chain of eight splitters
    comps = [laser("las0", 10, 100, 0, [400.0 + 25 * n for n in range(16)], aperture=5.0)]
    comps += [comp(f"bs{k}", "beam_splitter", 60 + 60 * k, 100, 135) for k in range(8)]
    comps += [det(f"d{k}", 60 + 60 * k, 300, 270) for k in range(8)]
    out["distribution board"] = layout(comps, width=900, height=600)
    # a ring: four mirrors and a splitter, four wavelengths, light going round up to the 64 interactions
    comps = [laser("las0", 10, 100, 0, [405.0, 532.0, 633.0, 785.0], aperture=5.0),
             comp("bs0", "beam_splitter", 100, 100, 135),
             mirror("m0", 300, 100, 135), mirror("m1", 300, 300, 225), mirror("m2", 100, 300, 315),
             det("d0", 400, 100, 180)]
    out["ring"] = layout(comps, width=900, height=600)
    return out


@pytest.mark.parametrize("name", sorted(realistic_layouts()))
def test_a_realistic_layout_is_never_cut_and_has_room_to_spare(name):
    trace = trace_layout(realistic_layouts()[name])
    assert truncation(trace) is None
    assert len(flat_events(trace)) <= T.MAX_TRACE_EVENTS // 5, "a layout like this should use a fifth of the budget at most"
