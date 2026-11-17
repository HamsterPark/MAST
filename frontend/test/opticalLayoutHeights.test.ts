// ════════════════════════════════════════════════════════════════════════════
// Heights, docs/v2/design/optical_layout_3d.md, as the editor handles them: the fields the catalogue
// offers (props.z_mm on every type, a periscope's z_out_mm), the table's beam height, what early
// validation refuses (the server's rules in MASTv2/mast/optical_layout/model.py), how a layout is read
// and written back (an absent beam height stays absent), the setters, and how a height is read with
// its defaults (heights.ts, which the 3D view reads too).
//
//     cd frontend && npm run test:unit
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { propField, propFields, visiblePropFields } from "../src/lib/opticalLayout/catalog.ts";
import { changeType, normalizePropValue, setBeamHeight, setProp } from "../src/lib/opticalLayout/edit.ts";
import { errorsOf, validateLayout } from "../src/lib/opticalLayout/validate.ts";
import { layoutForWrite, parseLayout } from "../src/lib/opticalLayout/parse.ts";
import {
  DEFAULT_BEAM_HEIGHT_MM,
  beamHeightOf,
  componentZ,
  heightMark,
  heightText,
  periscopeHeights,
  portsOf,
  segmentZ,
  usesHeights,
  verticalRuns,
} from "../src/lib/opticalLayout/heights.ts";
import { COMPONENT_TYPES, LIMITS } from "../src/lib/opticalLayout/types.ts";
import type { ComponentType, OpticalLayout } from "../src/lib/opticalLayout/types.ts";
import { comp, layoutOf } from "./helpers/opticalLayout.ts";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { traceLayout, traceLayoutDetailed } from "../src/lib/opticalLayout/trace.ts";
import { parseTrace } from "../src/lib/opticalLayout/parse.ts";
import { diagnostics, heightMisses, hitIndicators, judgeHit, nearMisses, offHeight } from "../src/lib/opticalLayout/indicators.ts";
import { CHECK_ORDER, beamChecks, beamProfile, segmentStartPath, spotHeightOverflowMm, verticalRunBefore } from "../src/lib/opticalLayout/beam.ts";
import { planTexts } from "../src/lib/opticalLayout/textPlan.ts";
import { labelSize } from "../src/lib/opticalLayout/labels.ts";
import { glyphShapes, screenBox, shapeBounds } from "../src/lib/opticalLayout/glyphs.ts";
import type { View } from "../src/lib/opticalLayout/view.ts";
import type { LayoutComponent } from "../src/lib/opticalLayout/types.ts";

const paths = (l: OpticalLayout) => errorsOf(validateLayout(l)).map((i) => i.path);
const base = (t: ComponentType): Record<string, unknown> => (t === "laser" || t === "fiber_out" ? { wavelength_nm: 532 } : t === "dichroic" ? { edge_nm: 600 } : {});

describe("the catalogue: a height on every type, an exit height on a periscope", () => {
  it("z_mm: ±10000 mm at 0.01 mm, shown with the position", () => {
    for (const t of COMPONENT_TYPES) {
      const f = propField(t, "z_mm");
      assert.ok(f, t);
      assert.deepEqual({ kind: f.kind, min: f.min, max: f.max, decimals: f.decimals, group: f.group, unit: f.unit }, { kind: "number", min: -10000, max: 10000, decimals: 2, group: "position", unit: "mm" }, t);
      assert.ok(f.label.length > 0 && (f.hint ?? "").length > 0, t);
    }
    assert.equal(LIMITS.zAbs, 10000);
  });

  it("z_out_mm only on a periscope, the same range", () => {
    for (const t of COMPONENT_TYPES) assert.equal(propField(t, "z_out_mm") === undefined, t !== "periscope", t);
    const f = propField("periscope", "z_out_mm")!;
    assert.deepEqual({ min: f.min, max: f.max, decimals: f.decimals, group: f.group }, { min: -10000, max: 10000, decimals: 2, group: "position" });
  });

  it("the heights are not among the type's own parameters in the inspector, even when set", () => {
    const own = (t: ComponentType, p: Record<string, unknown> = {}) => visiblePropFields(t, p).map((f) => f.key);
    assert.deepEqual(own("periscope", { z_mm: 80, z_out_mm: 250 }), ["throughput"]);
    assert.deepEqual(own("pbs", { z_mm: 80 }), []);
    assert.ok(propFields("pbs").every((f) => f.group === "position"), "a PBS has nothing but its height");
  });

  it("values are rounded as the server keeps them", () => {
    assert.equal(normalizePropValue("mirror", "z_mm", 120.004), 120);
    assert.equal(normalizePropValue("periscope", "z_out_mm", 250.006), 250.01);
    assert.ok(Object.is(normalizePropValue("mirror", "z_mm", -0.004), 0), "no -0");
  });
});

describe("early validation of heights says what the server would say", () => {
  const one = (t: ComponentType, props: Record<string, unknown>) => layoutOf([comp(t, "c1", 100, 100, 0, { props: { ...base(t), ...props } })]);

  it("z_mm on every type, within ±10000", () => {
    for (const t of COMPONENT_TYPES) {
      assert.deepEqual(paths(one(t, { z_mm: 10000 })), [], t);
      assert.deepEqual(paths(one(t, { z_mm: -10000 })), [], t);
      assert.deepEqual(paths(one(t, { z_mm: 10000.01 })), ["components[c1].props.z_mm"], t);
      assert.deepEqual(paths(one(t, { z_mm: "120" })), ["components[c1].props.z_mm"], t);
    }
  });

  it("z_out_mm only on a periscope", () => {
    assert.deepEqual(paths(one("periscope", { z_out_mm: 250 })), []);
    assert.deepEqual(paths(one("periscope", { z_out_mm: -10000.5 })), ["components[c1].props.z_out_mm"]);
    assert.deepEqual(paths(one("mirror", { z_out_mm: 250 })), ["components[c1].props.z_out_mm"]);
  });

  it("the table's beam height: above 0 (also at 0.01 mm), at most 10000, absent is fine", () => {
    const t = (bh: unknown) => paths(layoutOf([], { beam_height_mm: bh as number }));
    assert.deepEqual(paths(layoutOf([])), []);
    for (const good of [0.005, 75, 10000]) assert.deepEqual(t(good), [], String(good));
    for (const bad of [0, -1, 0.004, 10000.01, Number.NaN, "75"]) assert.deepEqual(t(bad), ["table.beam_height_mm"], String(bad));
  });
});

describe("reading and writing a layout with heights", () => {
  const raw = (table: Record<string, unknown>) => ({ schema: 1, id: "h", table: { width_mm: 1200, height_mm: 900, ...table }, components: [] });

  it("an absent (or null) beam height stays absent, and the PUT body has no such key", () => {
    for (const table of [{}, { beam_height_mm: null }]) {
      const l = parseLayout(raw(table));
      assert.equal("beam_height_mm" in l.table, false);
      assert.equal("beam_height_mm" in layoutForWrite(l).table, false);
      assert.equal(JSON.stringify(layoutForWrite(l)).includes("beam_height_mm"), false);
    }
  });

  it("a beam height the server sent goes back as it came", () => {
    const l = parseLayout(raw({ beam_height_mm: 120 }));
    assert.equal(l.table.beam_height_mm, 120);
    assert.equal(layoutForWrite(l).table.beam_height_mm, 120);
  });

  it("setBeamHeight rounds to 0.01 mm and null removes the key", () => {
    const l = layoutOf([]);
    const a = setBeamHeight(l, 120.004);
    assert.equal(a.table.beam_height_mm, 120);
    assert.equal(setBeamHeight(a, 120), a, "the same value is no change");
    const b = setBeamHeight(a, null);
    assert.equal("beam_height_mm" in b.table, false);
    assert.equal(setBeamHeight(b, null), b);
    assert.equal(l.table.beam_height_mm, undefined, "the argument is left alone");
  });

  it("setProp sets and clears a height; changing the type keeps z_mm and drops z_out_mm", () => {
    let l = layoutOf([comp("periscope", "p1", 100, 100, 0)]);
    l = setProp(l, "p1", "z_mm", 80.004);
    l = setProp(l, "p1", "z_out_mm", 250);
    assert.deepEqual(l.components[0]!.props, { z_mm: 80, z_out_mm: 250 });
    const m = changeType(l, "p1", "mirror");
    assert.deepEqual(m.components[0]!.props, { z_mm: 80 });
    assert.deepEqual(setProp(l, "p1", "z_mm", null).components[0]!.props, { z_out_mm: 250 });
  });
});

describe("heights.ts: a height with its defaults", () => {
  const l = layoutOf([
    comp("mirror", "m", 0, 0, 0),
    comp("mirror", "hi", 0, 0, 0, { props: { z_mm: 200 } }),
    comp("periscope", "p", 0, 0, 0),
    comp("periscope", "q", 0, 0, 0, { props: { z_out_mm: 250 } }),
    comp("periscope", "r", 0, 0, 0, { props: { z_mm: 60, z_out_mm: 300 } }),
  ]);
  const [m, hi, p, q, r] = l.components as [typeof l.components[0], typeof l.components[0], typeof l.components[0], typeof l.components[0], typeof l.components[0]];

  it("H is the table's beam height, 75 when it has none", () => {
    assert.equal(DEFAULT_BEAM_HEIGHT_MM, 75);
    assert.equal(beamHeightOf(l.table), 75);
    assert.equal(beamHeightOf({ beam_height_mm: 120 }), 120);
  });

  it("a component's height, a periscope's two heights", () => {
    assert.equal(componentZ(m, l.table), 75);
    assert.equal(componentZ(hi, l.table), 200);
    assert.equal(componentZ(m, { beam_height_mm: 120 }), 120, "an unset height follows the table");
    assert.deepEqual(periscopeHeights(p, l.table), { zIn: 75, zOut: 75 });
    assert.deepEqual(periscopeHeights(q, l.table), { zIn: 75, zOut: 250 });
    assert.deepEqual(periscopeHeights(r, l.table), { zIn: 60, zOut: 300 });
    // a height and no exit height: it leaves at the height it was entered, not at H
    const s = comp("periscope", "s", 0, 0, 0, { props: { z_mm: 120 } });
    assert.deepEqual(periscopeHeights(s, l.table), { zIn: 120, zOut: 120 });
    assert.deepEqual(periscopeHeights(s, { beam_height_mm: 200 }), { zIn: 120, zOut: 120 });
  });

  it("the ports: a periscope is entered at z_mm along +x and at z_out_mm the other way; everything else at its height", () => {
    assert.deepEqual(portsOf(r, l.table, 1), { entry: 60, exit: 300 });
    assert.deepEqual(portsOf(r, l.table, -0.5), { entry: 300, exit: 60 });
    assert.deepEqual(portsOf(hi, l.table, -1), { entry: 200, exit: 200 });
    assert.deepEqual(portsOf(hi, l.table, 1), { entry: 200, exit: 200 });
  });

  it("a segment of an older server's trace (no z_mm) is at H", () => {
    assert.equal(segmentZ({}, l.table), 75);
    assert.equal(segmentZ({ z_mm: 250 }, l.table), 250);
    assert.equal(segmentZ({}, { beam_height_mm: 140 }), 140);
  });

  it("the vertical runs of a branch: where the segment after an event is at another height", () => {
    const branch = {
      segments: [{ z_mm: 75 }, { z_mm: 250 }, { z_mm: 250 }],
      events: [
        { component: "per1", x: 200, y: 100, z_mm: 75 },
        { component: "m1", x: 600, y: 100, z_mm: 250 },
        { component: "det1", x: 600, y: 400, z_mm: 250 },
      ],
    };
    assert.deepEqual(verticalRuns(branch, l.table), [{ component: "per1", x: 200, y: 100, z0: 75, z1: 250 }]);
    // an older server's trace has no heights: no runs
    assert.deepEqual(verticalRuns({ segments: [{}, {}], events: [{ component: "m", x: 0, y: 0 }] }, l.table), []);
    // a branch that ends after the periscope (64 events) has no segment after it: no run to draw
    assert.deepEqual(verticalRuns({ segments: [{ z_mm: 75 }], events: [{ component: "per1", x: 0, y: 0, z_mm: 75 }] }, l.table), []);
  });

  it("usesHeights: a beam height or a z anywhere", () => {
    assert.equal(usesHeights(layoutOf([comp("mirror", "m", 0, 0, 0)])), false);
    assert.equal(usesHeights(l), true);
    assert.equal(usesHeights(layoutOf([comp("mirror", "m", 0, 0, 0, { props: { z_mm: 75 } })])), true, "even at the default");
    assert.equal(usesHeights(layoutOf([], { beam_height_mm: 75 })), true);
  });

  it("the mark under a name: a height off the beam height, a periscope that goes up or down", () => {
    assert.equal(heightMark(m, l.table), null);
    assert.deepEqual(heightMark(hi, l.table), { text: "z 200", rise: 0 });
    assert.equal(heightMark(p, l.table), null, "a periscope that goes nowhere is like any component");
    assert.deepEqual(heightMark(q, l.table), { text: "z 75→250", rise: 1 });
    assert.deepEqual(heightMark(comp("periscope", "d", 0, 0, 0, { props: { z_mm: 200, z_out_mm: 87.5 } }), l.table), { text: "z 200→87.5", rise: -1 });
    assert.deepEqual(heightMark(comp("periscope", "s", 0, 0, 0, { props: { z_mm: 120 } }), l.table), { text: "z 120", rise: 0 }, "raised, no exit height: a height, no arrow");
    assert.equal(heightMark(hi, { beam_height_mm: 200 }), null, "at the table's beam height: nothing to say");
    assert.deepEqual(heightMark(m, { beam_height_mm: 200 }), null);
  });

  it("heights read the way a person writes them", () => {
    assert.equal(heightText(75), "75");
    assert.equal(heightText(87.7), "87.7");
    assert.equal(heightText(112.05), "112.05");
    assert.equal(heightText(-0.001), "0");
    assert.equal(heightText(-12.5), "-12.5");
  });
});

// ── the trace and what the editor reads off it (3D sections 3 to 6) ─────────────────────────

const FIXTURE_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "tests", "fixtures", "optical_layout");
const fixtureLayout = (name: string): OpticalLayout => parseLayout(JSON.parse(readFileSync(join(FIXTURE_DIR, `${name}.json`), "utf8")).layout);

describe("the trace with heights", () => {
  it("a periscope lifts the beam; the vertical run is path; what is passed is listed", () => {
    const t = traceLayout(fixtureLayout("height_periscope_up"));
    const br = t[0]!.branches[0]!;
    assert.deepEqual(br.segments.map((s) => s.z_mm), [75, 250, 250]);
    assert.deepEqual(br.events.map((e) => [e.component, e.z_mm, e.dz_mm, e.path_mm]), [["per1", 75, 0, 150], ["m_high", 250, 0, 725], ["det1", 250, 0, 1025]]);
    assert.deepEqual(br.segments[1]!.passes, [{ component: "m_low", x: 400, y: 100, dz_mm: 175 }]);
  });

  it("passes at one point go by id (code points), whatever the order of the layout", () => {
    for (const order of [["b_mirror", "a_mirror"], ["a_mirror", "b_mirror"]]) {
      const l = layoutOf([comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532 } }), ...order.map((id) => comp("mirror", id, 100, 100, 135, { props: { z_mm: 200 } })), comp("detector", "d", 300, 100, 180)]);
      assert.deepEqual(traceLayout(l)[0]!.branches[0]!.segments[0]!.passes!.map((p) => p.component), ["a_mirror", "b_mirror"]);
    }
  });

  it("dz_mm keeps its hundredths (heights are on a 0.01 mm grid), rounded to 0.001 like every length", () => {
    const l = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "m1", 100, 100, 135, { props: { z_mm: 75.25 } }),
      comp("detector", "over", 100, 200, 270, { aperture: 25.4, props: { z_mm: 87.75 } }),
      comp("detector", "d", 100, 300, 270, { props: { z_mm: 75.07 } }),
    ]);
    const br = traceLayout(l)[0]!.branches[0]!;
    // 75 - 75.07 is -0.06999999999999318 in floating point: -0.07 after the rounding
    assert.deepEqual(br.events.map((e) => [e.component, e.z_mm, e.dz_mm]), [["m1", 75, -0.25], ["d", 75, -0.07]]);
    assert.deepEqual(br.segments[1]!.passes, [{ component: "over", x: 100, y: 200, dz_mm: -12.75 }]);
  });

  it("a periscope with a height and no exit height: the beam leaves at the height it came in, no vertical run", () => {
    const l = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532, z_mm: 120 } }),
      comp("periscope", "p", 200, 100, 0, { props: { z_mm: 120 } }),
      comp("detector", "d", 400, 100, 180, { props: { z_mm: 120 } }),
    ]);
    const br = traceLayout(l)[0]!.branches[0]!;
    assert.deepEqual(br.segments.map((s) => s.z_mm), [120, 120]);
    assert.deepEqual(br.events.map((e) => [e.component, e.z_mm, e.dz_mm, e.path_mm]), [["p", 120, 0, 200], ["d", 120, 0, 400]]);
    assert.deepEqual(verticalRuns(br, l.table), []);
  });

  it("a server's trace with heights is read with them, an older one without", () => {
    const raw = JSON.parse(JSON.stringify(traceLayout(fixtureLayout("height_mixed"))));
    const back = parseTrace(raw)!;
    assert.deepEqual(back, raw);
    delete raw[0].branches[0].segments[0].z_mm;
    raw[0].branches[0].segments[0].passes = [{ component: 7, x: 0, y: 0, dz_mm: 1 }, { component: "ok", x: 1, y: 2, dz_mm: 3 }];
    raw[0].branches[0].events[0].dz_mm = "3";
    const loose = parseTrace(raw)!;
    assert.equal(loose[0]!.branches[0]!.segments[0]!.z_mm, undefined);
    assert.deepEqual(loose[0]!.branches[0]!.segments[0]!.passes, [{ component: "ok", x: 1, y: 2, dz_mm: 3 }]);
    assert.equal(loose[0]!.branches[0]!.events[0]!.dz_mm, undefined);
  });
});

describe("what the editor reads off a trace with heights", () => {
  it("a near miss only at the component's height", () => {
    const beside = (z?: number) =>
      layoutOf([comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532 } }), comp("mirror", "m1", 200, 115, 135, { props: z === undefined ? {} : { z_mm: z } }), comp("detector", "d", 400, 100, 180)]);
    const misses = (l: OpticalLayout) => nearMisses(l, traceLayoutDetailed(l)).map((m) => m.componentId);
    assert.deepEqual(misses(beside()), ["m1"]);
    assert.deepEqual(misses(beside(200)), []);
    assert.deepEqual(misses(beside(87.7)), ["m1"], "at the edge of the height it still counts");
  });

  it("off the axis in height: more than 20 % of the usable aperture, the four actions", () => {
    const m = comp("mirror", "m", 0, 0, 135);
    const iris = comp("iris", "i", 0, 0, 0, { props: { open_mm: 5 } });
    assert.equal(offHeight(m, "reflect", 5.09), true);
    assert.equal(offHeight(m, "reflect", 5.08), false, "20 % itself is not more");
    assert.equal(offHeight(m, "blocked", 12), false);
    assert.equal(offHeight(iris, "transmit", -1.01), true, "an iris by its opening");
    assert.deepEqual(judgeHit(m, "reflect", 0, -6), { level: "warn", reason: "off_height" });
    assert.deepEqual(judgeHit(m, "reflect", 6, -6), { level: "warn", reason: "off_center" }, "off centre comes first");
  });

  it("missed in height: grouped by component, the closest pass, how many; none on a cut trace", () => {
    const three = layoutOf([
      comp("laser", "l1", 0, 100, 0, { props: { wavelength_nm: 532 } }),
      comp("laser", "l2", 0, 200, 0, { props: { wavelength_nm: 532 } }),
      comp("laser", "l3", 0, 300, 0, { props: { wavelength_nm: 532, z_mm: 140 } }),
      comp("window", "w", 300, 200, 0, { aperture: 300, props: { z_mm: 300 } }),
    ]);
    const got = heightMisses(three, traceLayoutDetailed(three));
    assert.deepEqual(got.map((m) => [m.componentId, m.source, m.z_mm, m.dz_mm, m.beams]), [["w", "l3", 140, -160, 3]]);
    assert.deepEqual(heightMisses(three, traceLayoutDetailed(three, { maxTraceEvents: 0 })), got, "no event, no cut");
    const cutOne = layoutOf([
      comp("laser", "l1", 0, 100, 0, { props: { wavelength_nm: 532 } }),
      comp("window", "w", 200, 100, 0, { props: { z_mm: 300 } }),
      comp("detector", "d1", 400, 100, 180),
      comp("laser", "l2", 0, 300, 0, { props: { wavelength_nm: 532 } }),
      comp("detector", "d2", 400, 300, 180),
    ]);
    assert.equal(heightMisses(cutOne, traceLayoutDetailed(cutOne)).length, 1);
    assert.deepEqual(heightMisses(cutOne, traceLayoutDetailed(cutOne, { maxTraceEvents: 1 })), [], "a cut trace says nothing about what no beam does");
  });

  it("the diagnostics of a layout with heights, and none of them without", () => {
    const l = fixtureLayout("height_passes_over_and_under");
    const kinds = diagnostics(l, traceLayoutDetailed(l)).map((d) => `${d.kind}|${d.componentId}`);
    for (const k of ["off_height|m_edge", "off_height|iris1", "height_miss|m_hi", "height_miss|m_lo", "height_miss|m_out"]) assert.ok(kinds.includes(k), k);
    const flat = layoutOf([comp("laser", "l1", 20, 100, 0, { props: { wavelength_nm: 633, beam_diameter_mm: 30 } }), comp("window", "w1", 100, 100, 0), comp("detector", "d", 300, 100, 180, { aperture: 40 })]);
    const t = traceLayoutDetailed(flat);
    assert.ok(beamChecks(flat, t).some((c) => c.kind === "spot_overflow"));
    assert.ok(!beamChecks(flat, t).some((c) => c.kind === "spot_height"), "no heights: the spot in the plane only");
    assert.ok(!diagnostics(flat, t).some((d) => d.kind === "off_height" || d.kind === "height_miss"));
  });

  it("the spot in height: |dz| + d/2 past half the aperture, grouped like the spot in the plane", () => {
    const l = fixtureLayout("height_mixed");
    const t = traceLayoutDetailed(l);
    const spot = beamChecks(l, t).filter((c) => c.kind === "spot_height");
    assert.deepEqual(spot.map((c) => c.componentId), ["det_small"]);
    assert.ok(Math.abs(spot[0]!.byMm! - 0.878838) < 5e-7);
    const ds = l.components.find((c) => c.id === "det_small")!;
    assert.equal(spotHeightOverflowMm(ds, { action: "stop", dz_mm: 2, beam: { p: 1, mw: null, d_mm: 2, s: null } }), null, "2 + 1 = 3: it fits");
    assert.equal(spotHeightOverflowMm(ds, { action: "back_hit", dz_mm: 3, beam: { p: 1, mw: null, d_mm: 2, s: null } }), null);
    assert.equal(spotHeightOverflowMm(ds, { action: "stop", beam: { p: 1, mw: null, d_mm: 2, s: null } }), null, "no height: an older server");
  });

  it("the kinds of heights sit in the order of the summary", () => {
    const at = (k: string) => CHECK_ORDER.indexOf(k);
    assert.ok(at("off_center") < at("off_height") && at("off_height") < at("miss"));
    assert.ok(at("miss") < at("height_miss") && at("height_miss") < at("spot_overflow"));
    assert.ok(at("spot_overflow") < at("spot_height") && at("spot_height") < at("extinguished"));
  });

  it("the profile of a beam runs through a periscope's vertical run", () => {
    const l = fixtureLayout("height_periscope_up");
    const t = traceLayoutDetailed(l);
    const br = t[0]!.branches[0]!;
    assert.equal(verticalRunBefore(br, 1), 175);
    assert.equal(verticalRunBefore(br, 2), 0);
    assert.equal(segmentStartPath(br, 1), 150 + 175);
    const prof = beamProfile(t[0]!, "0")!;
    assert.deepEqual(prof.marks.map((m) => m.pathMm), [150, 725, 1025]);
    const xs = prof.points.map((p) => p.pathMm);
    assert.ok(xs.every((x, i) => i === 0 || x >= xs[i - 1]! - 1e-9), "in order along the path");
    // the curve is continuous: the run is sampled from the periscope (150) to the start of the next segment (325)
    assert.ok(xs.some((x) => Math.abs(x - 150) < 1e-9) && xs.some((x) => x > 150 && x < 325) && xs.some((x) => Math.abs(x - 325) < 1e-9));
    const at725 = prof.points.find((p) => Math.abs(p.pathMm - 725) < 1e-9)!;
    assert.ok(Math.abs(at725.dMm - 1.114077) < 5e-7, "the spot at M_HIGH, the run counted");
  });

  it("hit indicators carry dz", () => {
    const l = fixtureLayout("height_periscope_down");
    const hits = hitIndicators(l, traceLayoutDetailed(l));
    assert.deepEqual(hits.get("det1")!.map((h) => [h.dz_mm, h.reason]), [[3, "off_height"]]);
  });
});

describe("the height marks of the 2D canvas (3D section 7)", () => {
  const view = (k: number): View => ({ k, ox: 50, oy: 800 });
  const extent = (k: number) => (c: LayoutComponent) => screenBox(shapeBounds(glyphShapes(c)), c.angle_deg, k);
  const plan = (l: OpticalLayout, k: number, withMarks: boolean) =>
    planTexts({ components: l.components, indicators: new Map(), misses: [], view: view(k), selected: new Set(), hover: null, showLabels: true, showReadouts: false, extent: extent(k), movable: true, ...(withMarks ? { heightMark: (c: LayoutComponent) => heightMark(c, l.table) } : {}) });

  it("a name gets its height as a second line, centred under it, only where there is something to say", () => {
    const l = layoutOf([comp("mirror", "m1", 100, 100, 135, { label: "M1" }), comp("mirror", "m2", 300, 100, 135, { label: "M2", props: { z_mm: 120 } }), comp("periscope", "p1", 500, 100, 0, { label: "P1", props: { z_out_mm: 250 } })]);
    const [m1, m2, p1] = plan(l, 2, true);
    assert.equal(m1!.sub, undefined);
    assert.deepEqual({ text: m2!.sub!.text, rise: m2!.sub!.rise }, { text: "z 120", rise: 0 });
    assert.deepEqual({ text: p1!.sub!.text, rise: p1!.sub!.rise }, { text: "z 75→250", rise: 1 });
    // the second line is below the first and centred with it under the drawing
    assert.ok(m2!.sub!.dy > 0);
    const nameW = labelSize("M2", 11).w;
    const subW = labelSize("z 120", m2!.sub!.px).w;
    assert.ok(Math.abs(m2!.x + nameW / 2 - (m2!.x + m2!.sub!.dx + subW / 2)) < 1e-9);
  });

  it("without marks (a layout without heights) every name is where it always was", () => {
    const l = foldish();
    assert.deepEqual(plan(l, 2, true), plan(l, 2, false));
    assert.deepEqual(plan(l, 0.5, true), plan(l, 0.5, false));
  });

  it("a periscope with an exit height shows which way is +x (an arrowhead), one without does not", () => {
    const shapes = (props: Record<string, unknown>) => glyphShapes(comp("periscope", "p", 0, 0, 0, { props }));
    assert.equal(shapes({}).filter((s) => s.role === "detail").length, 0);
    const arrow = shapes({ z_out_mm: 250 }).filter((s) => s.role === "detail");
    assert.equal(arrow.length, 1);
    assert.ok(arrow[0]!.k === "poly" && arrow[0]!.pts.every(([x]) => x > 0), "on the +x side");
    assert.equal(shapes({ z_out_mm: 250 }).at(-1)!.role, "face", "the working surface stays the last shape");
  });
});

function foldish(): OpticalLayout {
  return layoutOf([
    comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
    comp("mirror", "m1", 112.5, 62.5, 135),
    comp("periscope", "p1", 200, 62.5, 0),
    comp("detector", "det1", 112.5, 200, 270),
  ]);
}

describe("a focus in a periscope's vertical run", () => {
  it("the spot chart finds it where the summary does: about 50 mm after the periscope", () => {
    const l = layoutOf([
      comp("laser", "l1", 0, 100, 0, { props: { wavelength_nm: 633, beam_diameter_mm: 4 } }),
      comp("lens", "f1", 50, 100, 0, { props: { focal_mm: 100 } }),
      comp("periscope", "p1", 100, 100, 0, { props: { z_out_mm: 175 } }),
      comp("detector", "d", 400, 100, 180, { props: { z_mm: 175 } }),
    ]);
    const prof = beamProfile(traceLayoutDetailed(l)[0]!, "0")!;
    assert.equal(prof.waists.length, 1);
    assert.ok(prof.waists[0]!.pathMm > 149.9 && prof.waists[0]!.pathMm <= 150, String(prof.waists[0]!.pathMm));
  });
});

describe("missed in height is about components nothing lights", () => {
  it("a component one beam passes and another hits is not missed", () => {
    const l = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "m1", 200, 100, 135, { props: { z_mm: 200 } }),
      comp("detector", "d", 400, 100, 180),
      comp("laser", "las2", 100, 100, 0, { props: { wavelength_nm: 532, z_mm: 200 } }),
      comp("detector", "u", 200, 300, 270, { props: { z_mm: 200 } }),
    ]);
    const t = traceLayoutDetailed(l);
    assert.ok(t[0]!.branches[0]!.segments.some((g) => (g.passes ?? []).some((p) => p.component === "m1")), "las1 passes under M1");
    assert.deepEqual(heightMisses(l, t), []);
  });

  it("passes are in the order along the ray, not of their ids", () => {
    const l = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "z_near", 100, 100, 135, { props: { z_mm: 200 } }),
      comp("mirror", "a_far", 200, 100, 135, { props: { z_mm: 200 } }),
      comp("detector", "d", 300, 100, 180),
    ]);
    assert.deepEqual(traceLayout(l)[0]!.branches[0]!.segments[0]!.passes!.map((p) => p.component), ["z_near", "a_far"]);
  });
});
