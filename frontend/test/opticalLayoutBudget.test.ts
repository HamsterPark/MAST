// ════════════════════════════════════════════════════════════════════════════
// The budget of a whole trace: MAX_TRACE_EVENTS = 5000 events for all trees together
// (docs/v2/design/optical_layout.md section 11.2 point 13).
//
//     cd frontend && npm run test:unit
//
// The 64 events and the intensity floor bound ONE lineage, not the trace: a layout of 361
// beam splitters and 32 lasers of 16 wavelengths still made 233 760 branches and 217 328
// events (17 s in Python, a frozen page here). The rule, the same in trace.py:
//   * events are counted in the order they are produced (trees in order, a tree depth first
//     with the reflected child before the transmitted one, a branch one after the other);
//   * the hit that would be event number budget + 1 is NOT recorded: its branch gets the
//     segment to that point (one more segment than events, like left_table), ends with
//     "budget", and the trace stops there: no later branch, no later tree, no fibre
//     continuation. So the budget branch is the last branch of the last tree, and there is
//     exactly one; a trace that needs exactly the budget is complete;
//   * a ray that finds no hit costs nothing (left_table is judged first).
// The shared fixtures trace_budget_exact / trace_budget_cut pin the same on both sides
// (test/opticalLayoutFixtures.test.ts); THESE are the cases that can be worked out by hand,
// the worst case at the real limit, and what the editor says about a cut trace.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { TRACE_LIMITS, cutPoint, traceLayout, traceLayoutDetailed, truncation } from "../src/lib/opticalLayout/trace.ts";
import { parseTrace } from "../src/lib/opticalLayout/parse.ts";
import { aimErrorText } from "../src/lib/opticalLayout/align.ts";
import { cutDemo, heavyDemo } from "../src/lib/opticalLayout/devMock.ts";
import { errorsOf, validateLayout } from "../src/lib/opticalLayout/validate.ts";
import { LIMITS } from "../src/lib/opticalLayout/types.ts";
import { diagnostics, nearMisses, shownNearMisses, traceCutMessage, traceCutTexts, unlitComponents, type NearMiss } from "../src/lib/opticalLayout/indicators.ts";
import type { Trace } from "../src/lib/opticalLayout/types.ts";
import { budgetLayout, comp, denseSplitterLayout, foldLayout, layoutOf } from "./helpers/opticalLayout.ts";

const eventsIn = (t: Trace) => t.reduce((n, s) => n + s.branches.reduce((m, b) => m + b.events.length, 0), 0);
const branchesIn = (t: Trace) => t.reduce((n, s) => n + s.branches.length, 0);
const walkOrder = (t: Trace) => t.flatMap((s) => s.branches.flatMap((b) => b.events.map((e) => `${s.source}/${b.branch}/${e.component}/${e.action}`)));
const shape = (t: Trace) => t.map((s) => `${s.source}:${s.branches.map((b) => `${b.branch}=${b.end}(${b.segments.length}s,${b.events.length}e)`).join(",")}`);

describe("the budget of a whole trace", () => {
  it("is 5000 events", () => {
    assert.equal(TRACE_LIMITS.maxTraceEvents, 5000);
  });

  it("a trace that needs exactly the budget is complete; with one event less the last hit is the one that is cut", () => {
    const l = foldLayout(); // laser -> m1 (reflect) -> det1 (stop): two events
    const full = traceLayout(l);
    assert.equal(eventsIn(full), 2);
    assert.deepEqual(traceLayout(l, { maxTraceEvents: 2 }), full, "exactly enough is complete");
    assert.equal(truncation(traceLayout(l, { maxTraceEvents: 2 })), null);

    const cut = traceLayout(l, { maxTraceEvents: 1 });
    const b = cut[0]!.branches[0]!;
    assert.equal(b.end, "budget");
    assert.equal(b.events.length, 1, "the mirror hit was recorded");
    assert.equal(b.segments.length, 2, "one segment more than events: the one to the hit that was not recorded");
    assert.deepEqual([b.segments[1]!.x1, b.segments[1]!.y1], [112.5, 200], "it runs to the point where the detector would have been hit");
  });

  it("with a budget of 0 the first hit is the one that is cut, but a ray that hits nothing is not a cut", () => {
    const b = traceLayout(foldLayout(), { maxTraceEvents: 0 })[0]!.branches[0]!;
    assert.equal(b.end, "budget");
    assert.equal(b.events.length, 0);
    assert.equal(b.segments.length, 1);
    const away = layoutOf([comp("laser", "las1", 50, 62.5, 180, { props: { wavelength_nm: 532 } })]);
    const t = traceLayout(away, { maxTraceEvents: 0 });
    assert.equal(t[0]!.branches[0]!.end, "left_table", "no hit, no event, nothing to cut");
    assert.equal(truncation(t), null);
  });

  it("a negative budget counts as 0, a fraction is cut off", () => {
    const l = foldLayout();
    assert.deepEqual(traceLayout(l, { maxTraceEvents: -5 }), traceLayout(l, { maxTraceEvents: 0 }));
    assert.deepEqual(traceLayout(l, { maxTraceEvents: 1.9 }), traceLayout(l, { maxTraceEvents: 1 }));
  });

  it("anything that is not a finite number is the default budget, not no budget (checked on a layout that needs far more than 5000 events)", () => {
    const dense = denseSplitterLayout();
    for (const odd of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      assert.equal(eventsIn(traceLayout(dense, { maxTraceEvents: odd })), 5000, String(odd));
    }
  });

  it("a split costs one event, and its transmitted branch is not made once the reflected one was cut", () => {
    const l = budgetLayout();
    assert.deepEqual(shape(traceLayout(l, { maxTraceEvents: 1 })), ["las1:0=split(1s,1e),0.r=budget(1s,0e)"], "no 0.t, no las2");
    assert.deepEqual(shape(traceLayout(l, { maxTraceEvents: 2 })), ["las1:0=split(1s,1e),0.r=budget(2s,1e)"]);
  });

  it("the transmitted branch is cut where it starts when the reflected one used up the budget exactly (the shared fixture's case)", () => {
    const t = traceLayout(budgetLayout(), { maxTraceEvents: 3 });
    assert.deepEqual(shape(t), ["las1:0=split(1s,1e),0.r=stop(2s,2e),0.t=budget(1s,0e)"], "and las2 is not traced at all");
    assert.deepEqual(t[0]!.branches[2]!.segments, [{ x0: 150, y0: 62.5, x1: 300, y1: 62.5 }]);
  });

  it("the next source is cut at its first hit when the budget is used up between trees", () => {
    assert.deepEqual(shape(traceLayout(budgetLayout(), { maxTraceEvents: 4 })), ["las1:0=split(1s,1e),0.r=stop(2s,2e),0.t=stop(1s,1e)", "las2:0=budget(1s,0e)"]);
    assert.equal(truncation(traceLayout(budgetLayout(), { maxTraceEvents: 5 })), null, "five is all it needs");
  });

  it("no fibre continuation is looked for after a cut, and a continuation can be the cut itself", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("fiber_in", "fi1", 150, 62.5, 180, { props: { continues_at: "fo1" } }),
      comp("laser", "las2", 50, 200, 0, { props: { wavelength_nm: 633 } }),
      comp("detector", "det2", 300, 200, 180),
      comp("fiber_out", "fo1", 300, 400, 0, { props: { wavelength_nm: 532 } }),
      comp("detector", "det3", 500, 400, 180),
    ]);
    assert.deepEqual(traceLayout(l, { maxTraceEvents: 3 }).map((s) => s.source), ["las1", "las2", "fo1"], "three events: complete, the continuation comes last");
    assert.deepEqual(shape(traceLayout(l, { maxTraceEvents: 1 })), ["las1:0=stop(1s,1e)", "las2:0=budget(1s,0e)"], "cut in las2: fo1 is never queued for tracing");
    const t2 = traceLayout(l, { maxTraceEvents: 2 });
    assert.deepEqual(shape(t2), ["las1:0=stop(1s,1e)", "las2:0=stop(1s,1e)", "fo1:0=budget(1s,0e)"]);
    assert.equal(truncation(t2)?.source, "fo1");
  });

  it("a cut trace is the beginning of the complete one: the first k events, in the same order", () => {
    const l = budgetLayout();
    const full = walkOrder(traceLayout(l));
    assert.equal(full.length, 5);
    for (let k = 0; k <= 5; k++) assert.deepEqual(walkOrder(traceLayout(l, { maxTraceEvents: k })), full.slice(0, k), `budget ${k}`);
  });

  it("composes with skip: a component that is left out costs nothing", () => {
    const l = foldLayout();
    assert.equal(traceLayout(l, { maxTraceEvents: 0 })[0]!.branches[0]!.end, "budget");
    const skipped = traceLayout(l, { skip: new Set(["m1"]), maxTraceEvents: 0 });
    assert.equal(skipped[0]!.branches[0]!.end, "left_table", "the beam passes where the mirror was and finds nothing");
  });

  it("the detailed trace the editor draws is cut in the same place as the contract trace", () => {
    const l = budgetLayout();
    for (const k of [0, 1, 3, 4]) {
      const d = traceLayoutDetailed(l, { maxTraceEvents: k });
      const c = traceLayout(l, { maxTraceEvents: k });
      assert.deepEqual(d.map((s) => [s.source, s.branches.map((b) => [b.branch, b.end, b.segments.length, b.events.length])]), c.map((s) => [s.source, s.branches.map((b) => [b.branch, b.end, b.segments.length, b.events.length])]), `budget ${k}`);
    }
  });
});

describe("the worst case at the real limit", () => {
  const dense = denseSplitterLayout();
  const t = traceLayout(dense);

  it("is traced to the budget and no further: exactly 5000 events", () => {
    assert.equal(eventsIn(t), 5000);
    assert.ok(t.length < 512, `the later trees were not traced (${t.length} of 512)`);
  });

  it("has exactly one branch that ends with budget, the last of the last tree, one segment longer than its events", () => {
    const cut = t.flatMap((s) => s.branches.filter((b) => b.end === "budget"));
    assert.equal(cut.length, 1);
    const last = t[t.length - 1]!;
    assert.equal(last.branches[last.branches.length - 1], cut[0]);
    assert.equal(cut[0]!.segments.length, cut[0]!.events.length + 1);
  });

  it("makes at most trees + 2 x events branches: the budget bounds the branches as well", () => {
    assert.ok(branchesIn(t) <= t.length + 2 * 5000, `${branchesIn(t)} branches`);
  });

  it("is the same every time", () => {
    assert.equal(JSON.stringify(traceLayout(dense)), JSON.stringify(t));
  });
});

describe("where a cut trace stopped", () => {
  it("is the end of the last segment of the branch that ends with budget: the point of the hit that was not recorded", () => {
    const t = traceLayout(budgetLayout(), { maxTraceEvents: 3 }); // 0.t is cut on its way from bs1 to det_t
    assert.deepEqual(cutPoint(t), { x: 300, y: 62.5 });
    assert.deepEqual(cutPoint(traceLayoutDetailed(budgetLayout(), { maxTraceEvents: 3 })), { x: 300, y: 62.5 }, "the detailed trace too");
    assert.deepEqual(cutPoint(traceLayout(foldLayout(), { maxTraceEvents: 1 })), { x: 112.5, y: 200 }, "the detector that would have been hit");
  });

  it("is null for a complete trace and for none", () => {
    assert.equal(cutPoint(traceLayout(budgetLayout())), null);
    assert.equal(cutPoint([]), null);
  });

  it("is where truncation() says it stopped: the same branch", () => {
    const t = traceLayout(denseSplitterLayout());
    const cut = truncation(t)!;
    const branch = t.find((s) => s.source === cut.source && s.wavelength_nm === cut.wavelength_nm)!.branches.find((b) => b.branch === cut.branch)!;
    const last = branch.segments[branch.segments.length - 1]!;
    assert.deepEqual(cutPoint(t), { x: last.x1, y: last.y1 });
  });
});

describe("the near misses that are shown", () => {
  const grazing = layoutOf([
    comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
    comp("mirror", "m1", 112.5, 67.5, 135, { aperture: 10 }),
    comp("detector", "det1", 300, 62.5, 180),
  ]);
  const trace = traceLayoutDetailed(grazing);

  it("are the near misses of the trace when nothing is being dragged", () => {
    assert.deepEqual(shownNearMisses(grazing, trace, false), nearMisses(grazing, trace));
    assert.equal(shownNearMisses(grazing, trace, false).length, 1);
  });

  it("are none while a drag is previewed: every frame would pay for them and the drag's end draws them again", () => {
    assert.deepEqual(shownNearMisses(grazing, trace, true), []);
  });

  it("while previewing it is always the same empty list, so nothing that reads it is redrawn for a new one", () => {
    assert.equal(shownNearMisses(grazing, trace, true), shownNearMisses(layoutOf([]), [], true));
  });
});

describe("the dev mock's ?layoutmock=cut layout", () => {
  it("is the worst case, within the model's limits, accepted by the early check, and cut by the budget", () => {
    const l = cutDemo();
    assert.equal(l.components.length, 393);
    assert.equal(l.components.filter((c) => c.type === "laser").length, 32);
    assert.ok(l.components.length <= LIMITS.components && l.components.filter((c) => c.type === "laser").length <= LIMITS.sources);
    assert.deepEqual(errorsOf(validateLayout(l)), []);
    const t = traceLayout(l);
    assert.equal(eventsIn(t), 5000);
    assert.equal(truncation(t)?.events, 5000);
  });
});

describe("the dev mock's heavy-demo", () => {
  it("is the heaviest trace that is not cut: close to the budget, complete, and valid", () => {
    const l = heavyDemo();
    assert.deepEqual(errorsOf(validateLayout(l)), []);
    const t = traceLayout(l);
    assert.equal(truncation(t), null);
    assert.ok(eventsIn(t) > 4500 && eventsIn(t) < 5000, `${eventsIn(t)} events`);
  });
});

describe("truncation(): what a cut trace says about itself, read from the trace alone", () => {
  it("is null for a complete trace and for no trace at all", () => {
    assert.equal(truncation(traceLayout(foldLayout())), null);
    assert.equal(truncation([]), null);
  });

  it("says how much was traced and where it stopped", () => {
    const expected = { reason: "events", events: 3, branches: 3, trees: 1, source: "las1", wavelength_nm: 532, branch: "0.t" };
    const cut = traceLayout(budgetLayout(), { maxTraceEvents: 3 });
    assert.deepEqual(truncation(cut), expected);
    assert.deepEqual(truncation(traceLayoutDetailed(budgetLayout(), { maxTraceEvents: 3 })), expected, "the detailed trace too");
    assert.deepEqual(truncation(JSON.parse(JSON.stringify(cut))), expected, "and after a trip through JSON: there is no extra field on the wire");
  });

  it("reports the first branch that ends with budget", () => {
    const branch = (id: string, end: string) => ({ branch: id, fraction: 1, segments: [], events: [], end });
    const fake = [
      { source: "a", wavelength_nm: null, branches: [branch("0", "stop")] },
      { source: "b", wavelength_nm: 633, branches: [branch("0", "split"), branch("0.r", "budget"), branch("0.t", "budget")] },
    ];
    assert.equal(truncation(fake)?.source, "b");
    assert.equal(truncation(fake)?.branch, "0.r");
  });
});

describe("what the editor reads and says", () => {
  it("the parser keeps an end of budget instead of reading it as a stop", () => {
    const raw = [{ source: "las1", wavelength_nm: 532, branches: [{ branch: "0", fraction: 1, segments: [{ x0: 0, y0: 0, x1: 5, y1: 0 }], events: [], end: "budget" }] }];
    assert.equal(parseTrace(raw)?.[0]?.branches[0]?.end, "budget");
  });

  it("names the numbers and the beam it stopped in", () => {
    const cut = truncation(traceLayout(denseSplitterLayout()))!; // 5000 events, a different number of branches and of beams
    assert.equal(cut.events, 5000);
    assert.ok(cut.branches !== cut.events && cut.trees !== cut.events && cut.trees !== cut.branches, "three different numbers, so a mix-up shows");
    const text = traceCutMessage(cut);
    for (const part of [cut.events, cut.branches, cut.trees, cut.source, cut.branch]) assert.ok(text.includes(String(part)), `"${part}" is in: ${text}`);
    assert.ok(text.includes(`${cut.wavelength_nm} nm`), "and the wavelength");
  });

  it("the English notice counts in the singular where it is one, and in the plural otherwise", () => {
    const one = traceCutTexts(truncation(traceLayout(foldLayout(), { maxTraceEvents: 1 }))!); // 1 event, 1 branch, 1 beam
    assert.ok(one.en.includes("cut after 1 event ("), one.en);
    assert.ok(one.en.includes("1 branch of 1 beam traced"), one.en);
    const many = traceCutTexts(truncation(traceLayout(denseSplitterLayout()))!);
    assert.ok(many.en.includes("cut after 5000 events ("), many.en);
    assert.ok(/\d+ branches of \d+ beams traced/.test(many.en), many.en);
    assert.equal(traceCutMessage(truncation(traceLayout(foldLayout(), { maxTraceEvents: 1 }))!), one.zh, "and tr() picks the Chinese one under node");
  });

  it("the aim tool does not claim that no beam reaches the mirror when the trace was cut", () => {
    const plain = aimErrorText("no-beam");
    const cut = aimErrorText("no-beam", true);
    assert.notEqual(cut, plain);
    assert.ok(cut.includes("截断"), cut);
    assert.equal(aimErrorText("no-target", true), aimErrorText("no-target"), "other answers are the same");
  });

  // A layout in which the complete trace would say "no beam reaches det_far" and "no beam
  // reaches det1" - and a cut trace cannot say it, because the light that would reach them
  // is exactly the light that was not traced.
  const sparse = layoutOf([
    comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
    comp("mirror", "m1", 112.5, 62.5, 135),
    comp("detector", "det1", 112.5, 200, 270),
    comp("detector", "det_far", 300, 300, 180),
  ]);
  const kinds = (l: typeof sparse, k?: number) => diagnostics(l, traceLayoutDetailed(l, k === undefined ? {} : { maxTraceEvents: k })).map((d) => d.kind);

  it("a complete trace lists the components no beam reaches", () => {
    assert.ok(kinds(sparse).includes("unlit"));
    assert.deepEqual(unlitComponents(sparse, traceLayoutDetailed(sparse)), ["det_far"]);
  });

  it("a cut trace does not: it puts the cut first and draws no conclusion from the light that is missing", () => {
    const d = kinds(sparse, 1);
    assert.equal(d[0], "trace_cut");
    assert.ok(!d.includes("unlit"), "no 'no beam reaches it' from a trace that stops short");
    assert.deepEqual(unlitComponents(sparse, traceLayoutDetailed(sparse, { maxTraceEvents: 1 })), []);
  });

  it("a cut trace has no near misses to show (the same rule as the backend's picture)", () => {
    // a beam that passes 2 mm outside a mirror's aperture on its way to a detector: a near miss in the complete trace
    const grazing = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "m1", 112.5, 67.5, 135, { aperture: 10 }),
      comp("detector", "det1", 300, 62.5, 180),
    ]);
    assert.equal(nearMisses(grazing, traceLayoutDetailed(grazing)).length, 1, "the complete trace has one");
    assert.deepEqual(nearMisses(grazing, traceLayoutDetailed(grazing, { maxTraceEvents: 0 })), []);
    // and diagnostics ignores misses it is handed for a cut trace
    const handed: NearMiss = { componentId: "m1", source: "las1", wavelength_nm: 532, branch: "0", x: 0, y: 0, offset_mm: 6, beyond_mm: 1 };
    const cutTrace = traceLayoutDetailed(grazing, { maxTraceEvents: 0 });
    assert.ok(!diagnostics(grazing, cutTrace, { misses: [handed] }).some((d) => d.kind === "miss"));
  });

  it("what the recorded events say stays true: a back hit in the traced part is still a problem", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "m1", 112.5, 62.5, 315), // its back faces the beam
      comp("laser", "las2", 50, 200, 0, { props: { wavelength_nm: 633 } }),
      comp("detector", "det1", 300, 200, 180),
    ]);
    const d = diagnostics(l, traceLayoutDetailed(l, { maxTraceEvents: 1 }));
    assert.equal(d[0]!.kind, "trace_cut", "first, ahead of the red back hit");
    assert.ok(d.some((x) => x.kind === "back_hit"));
    assert.equal(d.filter((x) => x.kind === "trace_cut").length, 1, "one problem, not one per branch");
  });
});
