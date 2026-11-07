// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/labels.ts, textPlan.ts and listing.ts — which texts
// the canvas draws when the view is crowded, and which layout opens first.
//
//     cd frontend && npm run test:unit
//
// The rule being pinned: names, alignment read-outs and near-miss notes compete for the
// same pixels. Whatever the operator is working on (selected, hovered) always shows;
// problems come before plain text; nothing is drawn on top of something else; and zooming
// in gives everything room.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { labelSize, placeLabels, type LabelBox } from "../src/lib/opticalLayout/labels.ts";
import { ALL_READOUTS_MIN_K, PROBLEM_TEXT_MIN_K, hitMarks, missText, planTexts, readoutText, signedText, worstHit } from "../src/lib/opticalLayout/textPlan.ts";
import { layoutOptionLabel, pickDefaultLayout } from "../src/lib/opticalLayout/listing.ts";
import { hitIndicators, nearMisses } from "../src/lib/opticalLayout/indicators.ts";
import { traceLayoutDetailed } from "../src/lib/opticalLayout/trace.ts";
import type { View } from "../src/lib/opticalLayout/view.ts";
import type { LayoutSummary } from "../src/lib/opticalLayout/types.ts";
import { comp, foldLayout, layoutOf } from "./helpers/opticalLayout.ts";

const box = (id: string, x: number, y: number, w = 40, h = 14, force = false): LabelBox => ({ id, x, y, w, h, force });

describe("placing labels", () => {
  it("draws everything that does not overlap", () => {
    const shown = placeLabels([box("a", 0, 0), box("b", 100, 0), box("c", 0, 50)]);
    assert.deepEqual([...shown].sort(), ["a", "b", "c"]);
  });

  it("drops a label that overlaps one already placed; the first one wins", () => {
    const shown = placeLabels([box("a", 0, 0), box("b", 20, 5), box("c", 100, 0)]);
    assert.deepEqual([...shown].sort(), ["a", "c"]);
  });

  it("keeps a small gap between neighbours", () => {
    assert.deepEqual([...placeLabels([box("a", 0, 0, 40, 14), box("b", 41, 0, 40, 14)])].sort(), ["a"], "1 px apart is too close");
    assert.deepEqual([...placeLabels([box("a", 0, 0, 40, 14), box("b", 43, 0, 40, 14)])].sort(), ["a", "b"]);
    assert.deepEqual([...placeLabels([box("a", 0, 0, 40, 14), box("b", 41, 0, 40, 14)], 0)].sort(), ["a", "b"]);
  });

  it("forced labels always show, and are placed first so an earlier plain one gives way", () => {
    const shown = placeLabels([box("plain", 0, 0), box("chosen", 10, 5, 40, 14, true)]);
    assert.deepEqual([...shown], ["chosen"]);
    const both = placeLabels([box("f1", 0, 0, 40, 14, true), box("f2", 10, 5, 40, 14, true)]);
    assert.deepEqual([...both].sort(), ["f1", "f2"], "two chosen ones are both drawn even if they touch");
  });

  it("an empty list is an empty set", () => {
    assert.equal(placeLabels([]).size, 0);
  });

  it("sizes grow with the text, and CJK characters take more room", () => {
    assert.ok(labelSize("MMMMMM").w > labelSize("MM").w);
    assert.ok(labelSize("光路图").w > labelSize("abc").w);
    assert.ok(labelSize("x", 22).h > labelSize("x", 11).h);
  });
});

describe("the read-out texts", () => {
  it("never shows -0.00", () => {
    assert.equal(signedText(-0.0001), "0.00");
    assert.equal(signedText(-4), "-4.00");
    assert.equal(signedText(45, 1), "45.0");
  });

  it("reads 'offset · incidence', with the number of hits when there are several", () => {
    const l = foldLayout();
    const hit = hitIndicators(l, traceLayoutDetailed(l)).get("m1")![0]!;
    assert.equal(readoutText(hit, 1), "Δ0.00 mm · 45.0°");
    assert.equal(readoutText(hit, 3), "Δ0.00 mm · 45.0° ×3");
  });

  it("the worst hit speaks for the component, the strongest beam first among equals", () => {
    const mk = (level: "ok" | "warn" | "bad", fraction: number) => ({ level, fraction }) as never;
    assert.equal(worstHit([mk("ok", 1), mk("bad", 0.1), mk("warn", 1)])?.level, "bad");
    assert.equal(worstHit([mk("warn", 0.2), mk("warn", 0.9)])?.fraction, 0.9);
    assert.equal(worstHit([]), undefined);
  });

  it("a near miss reads 'misses by N mm'", () => {
    assert.match(missText({ beyond_mm: 12.34 } as never), /12\.3/);
  });
});

describe("planning the texts of a layout", () => {
  const view = (k: number): View => ({ k, ox: 50, oy: 800 });
  const base = (l = foldLayout(), over: Partial<Parameters<typeof planTexts>[0]> = {}) => {
    const t = traceLayoutDetailed(l);
    return planTexts({
      components: l.components,
      indicators: hitIndicators(l, t),
      misses: nearMisses(l, t),
      view: view(1.5),
      selected: new Set(),
      hover: null,
      showLabels: true,
      showReadouts: true,
      ...over,
    });
  };
  const kinds = (items: ReturnType<typeof planTexts>) => items.map((i) => `${i.kind}:${i.key.slice(2)}`).sort();

  it("zoomed in, every component has its name and every hit its numbers", () => {
    const items = base();
    assert.deepEqual(kinds(items), ["label:det1", "label:las1", "label:m1", "readout:det1", "readout:m1"]);
  });

  it("zoomed out, plain read-outs give way, names stay", () => {
    assert.ok(0.6 < ALL_READOUTS_MIN_K);
    const items = base(foldLayout(), { view: view(0.6) });
    assert.deepEqual(kinds(items), ["label:det1", "label:las1", "label:m1"]);
  });

  it("zoomed out, the selected component still shows its numbers, and so does a problem", () => {
    const sel = base(foldLayout(), { view: view(0.6), selected: new Set(["m1"]) });
    assert.ok(kinds(sel).includes("readout:m1"));
    const bad = base(foldLayout({ mirrorAngle: 315 }), { view: view(0.6) });
    assert.ok(kinds(bad).includes("readout:m1"), "a back hit is always spoken");
    assert.equal(bad.find((i) => i.key === "r:m1")?.level, "bad");
  });

  it("as a thumbnail, not even the problems get a text unless they are selected", () => {
    const k = PROBLEM_TEXT_MIN_K / 2;
    const l = foldLayout({ mirrorAngle: 315 });
    assert.ok(!kinds(base(l, { view: view(k), showLabels: false })).includes("readout:m1"));
    assert.ok(kinds(base(l, { view: view(k), showLabels: false, selected: new Set(["m1"]) })).includes("readout:m1"));
    const miss = foldLayout({ laserY: 72.5 });
    assert.ok(!base(miss, { view: view(k), showLabels: false }).some((i) => i.kind === "miss"));
    assert.ok(base(miss, { view: view(PROBLEM_TEXT_MIN_K * 1.1), showLabels: false }).some((i) => i.kind === "miss"));
  });

  it("each can be switched off on its own", () => {
    assert.deepEqual(kinds(base(foldLayout(), { showLabels: false })), ["readout:det1", "readout:m1"]);
    assert.deepEqual(kinds(base(foldLayout(), { showReadouts: false })), ["label:det1", "label:las1", "label:m1"]);
    assert.deepEqual(base(foldLayout(), { showLabels: false, showReadouts: false }), []);
  });

  it("near misses are spoken in red, as a note beside the cross", () => {
    const items = base(foldLayout({ laserY: 72.5 }));
    const miss = items.find((i) => i.kind === "miss");
    assert.ok(miss);
    assert.equal(miss.level, "bad");
    assert.match(miss.text, /1\.4/);
  });

  it("a read-out beats a name when they collide (a problem outranks a label)", () => {
    // two mirrors a few pixels apart: the second one's name would sit on the first one's warning
    const l = layoutOf([
      comp("laser", "las1", 50, 70, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "mA", 112.5, 62.5, 135, { label: "A" }),
    ]);
    const items = base(l, { view: view(0.4) });
    const readout = items.find((i) => i.kind === "readout");
    assert.ok(readout, "the off-centre warning is there");
    assert.equal(readout.level, "warn");
  });

  it("crowded names thin out instead of piling up; the hovered one always shows", () => {
    const comps = Array.from({ length: 6 }, (_, i) => comp("mirror", `m${i}`, 100 + i * 4, 100, 135, { label: `Mirror ${i}` }));
    const l = layoutOf(comps);
    const few = base(l, { view: view(1), showReadouts: false });
    assert.ok(few.length < 6 && few.length >= 1, `${few.length} names drawn`);
    const withHover = base(l, { view: view(1), showReadouts: false, hover: "m5" });
    assert.ok(withHover.some((i) => i.key === "l:m5" && i.emphasis));
    // far enough apart, every name fits
    const spread = layoutOf(comps.map((c, i) => ({ ...c, x_mm: 100 + i * 120 })));
    assert.equal(base(spread, { view: view(1), showReadouts: false }).length, 6);
  });

  it("no two drawn texts overlap, except the chosen ones", () => {
    const comps = Array.from({ length: 40 }, (_, i) => comp("mirror", `m${i}`, 50 + (i % 8) * 18, 50 + Math.floor(i / 8) * 14, 135, { label: `Label ${i}` }));
    const l = layoutOf([comp("laser", "las1", 10, 50, 0, { props: { wavelength_nm: 532 } }), ...comps]);
    const items = base(l, { view: view(1) });
    const boxes = items.map((i) => {
      const { w, h } = labelSize(i.text, i.kind === "label" ? 11 : 10.4);
      return { id: i.key, x: i.x, y: i.kind === "label" ? i.y - 11 : i.y - 11, w, h };
    });
    for (let a = 0; a < boxes.length; a++) {
      for (let b = a + 1; b < boxes.length; b++) {
        const p = boxes[a]!;
        const q = boxes[b]!;
        const overlap = p.x < q.x + q.w && q.x < p.x + p.w && p.y < q.y + q.h && q.y < p.y + p.h;
        assert.ok(!overlap, `${p.id} overlaps ${q.id}`);
      }
    }
  });

  it("one ring per component that is hit, in the colour of its worst hit", () => {
    const l = foldLayout({ mirrorAngle: 315 });
    const marks = hitMarks(l.components, hitIndicators(l, traceLayoutDetailed(l)), view(1));
    assert.deepEqual(marks.map((m) => [m.key, m.level]), [["m1", "bad"]]);
  });
});

describe("opening a layout", () => {
  const s = (id: string, updated_at: string, over: Partial<LayoutSummary> = {}): LayoutSummary => ({ id, title: "", revision: 1, updated_at, updated_by: "", components: 0, open_comments: 0, ...over });

  it("the most recently updated one opens first", () => {
    assert.equal(pickDefaultLayout([s("old", "2026-11-01T10:00:00+00:00"), s("new", "2026-11-03T10:00:00+00:00"), s("mid", "2026-11-02T10:00:00+00:00")]), "new");
  });

  it("the first one wins a tie, and unreadable times lose to readable ones", () => {
    assert.equal(pickDefaultLayout([s("a", "2026-11-03T10:00:00+00:00"), s("b", "2026-11-03T10:00:00+00:00")]), "a");
    assert.equal(pickDefaultLayout([s("a", ""), s("b", "2026-11-03T10:00:00+00:00")]), "b");
    assert.equal(pickDefaultLayout([s("a", "nonsense"), s("b", "")]), "a");
    assert.equal(pickDefaultLayout([]), null);
  });

  it("an entry reads title, revision, and open comments when there are any", () => {
    assert.equal(layoutOptionLabel(s("main", "", { title: "SHG path", revision: 3 })), "SHG path · r3");
    assert.match(layoutOptionLabel(s("main", "", { title: "", revision: 3, open_comments: 2 })), /^main · r3 · .*2/);
  });
});
