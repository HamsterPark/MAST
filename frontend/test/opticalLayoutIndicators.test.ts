// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/indicators.ts and validate.ts — the alignment
// indicators of section 5 (yellow beyond 20 % of the aperture, red for the back of a
// mirror or a miss) and the early check against the rules of section 3.
//
//     cd frontend && npm run test:unit
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  OFFSET_WARN_FRACTION,
  componentLevel,
  diagnostics,
  effectiveAperture,
  hitIndicators,
  judgeHit,
  nearMisses,
  unlitComponents,
  worseOf,
} from "../src/lib/opticalLayout/indicators.ts";
import { traceLayoutDetailed } from "../src/lib/opticalLayout/trace.ts";
import { errorsOf, issuesFor, jsonBytes, validateComponent, validateLayout } from "../src/lib/opticalLayout/validate.ts";
import { addComponent, emptyLayout, setAnnotation, setProp } from "../src/lib/opticalLayout/edit.ts";
import { LIMITS } from "../src/lib/opticalLayout/types.ts";
import { comp, foldLayout, layoutOf } from "./helpers/opticalLayout.ts";

describe("hit indicators", () => {
  it("a hit on the centre is fine", () => {
    const l = foldLayout();
    const m = hitIndicators(l, traceLayoutDetailed(l));
    const h = m.get("m1")![0]!;
    assert.equal(h.level, "ok");
    assert.equal(h.offset_mm, 0);
    assert.ok(Math.abs(h.incidence_deg - 45) < 1e-9);
    assert.equal(h.path_mm, 62.5);
    assert.equal(h.leg_mm, 62.5);
    assert.ok(Math.abs(m.get("det1")![0]!.leg_mm - 137.5) < 1e-9, "the leg is measured from the previous event");
    assert.equal(componentLevel(m.get("m1")), "ok");
  });

  it("turns yellow beyond 20 % of the aperture (the whole aperture, not the half)", () => {
    assert.equal(OFFSET_WARN_FRACTION, 0.2);
    const aperture = 25.4;
    const limit = 0.2 * aperture; // 5.08 mm along the surface
    const dy = (limit - 0.1) / Math.SQRT2; // the beam is off the centre by dy; along the surface that is dy * sqrt(2)
    const inside = foldLayout({ laserY: 62.5 + dy });
    assert.equal(hitIndicators(inside, traceLayoutDetailed(inside)).get("m1")![0]!.level, "ok");
    const outside = foldLayout({ laserY: 62.5 + (limit + 0.1) / Math.SQRT2 });
    const h = hitIndicators(outside, traceLayoutDetailed(outside)).get("m1")![0]!;
    assert.equal(h.level, "warn");
    assert.equal(h.reason, "off_center");
  });

  it("the sign of the offset does not matter", () => {
    const l = foldLayout({ laserY: 62.5 - 6 });
    const h = hitIndicators(l, traceLayoutDetailed(l)).get("m1")![0]!;
    assert.ok(h.offset_mm > 0, "below the centre is +");
    assert.equal(h.level, "warn");
  });

  it("the back of a mirror is red", () => {
    const l = foldLayout({ mirrorAngle: 315 });
    const h = hitIndicators(l, traceLayoutDetailed(l)).get("m1")![0]!;
    assert.equal(h.level, "bad");
    assert.equal(h.reason, "back_hit");
  });

  it("a blocked beam is yellow, and an iris is judged by its opening", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 65.5, 0, { props: { wavelength_nm: 532 } }),
      comp("iris", "ir1", 100, 62.5, 0, { props: { open_mm: 4 } }),
    ]);
    const h = hitIndicators(l, traceLayoutDetailed(l)).get("ir1")![0]!;
    assert.equal(h.level, "warn");
    assert.equal(h.reason, "blocked");
    const iris = l.components[1]!;
    assert.equal(effectiveAperture(iris), 4);
    assert.equal(effectiveAperture({ ...iris, props: {} }), 25.4);
    assert.equal(judgeHit(iris, "transmit", 1).level, "warn", "1 mm off centre on a 4 mm opening is 25 %");
    assert.equal(judgeHit(iris, "transmit", 0.5).level, "ok");
  });

  it("worse levels win", () => {
    assert.equal(worseOf("ok", "warn"), "warn");
    assert.equal(worseOf("bad", "warn"), "bad");
    assert.equal(worseOf("ok", "ok"), "ok");
    assert.equal(componentLevel(undefined), "ok");
  });
});

describe("near misses", () => {
  it("a beam just outside the aperture is found", () => {
    // 10 mm above the centre meets the surface 14.1 mm from it: 1.4 mm past the edge
    const l = foldLayout({ laserY: 72.5 });
    const t = traceLayoutDetailed(l);
    assert.equal(t[0]!.branches[0]!.events.length, 0, "the trace itself has no event for a miss");
    const m = nearMisses(l, t);
    assert.equal(m.length, 1);
    assert.equal(m[0]!.componentId, "m1");
    assert.ok(Math.abs(m[0]!.beyond_mm - (10 * Math.SQRT2 - 12.7)) < 1e-9);
  });

  it("a beam far from every component is not a near miss", () => {
    const l = foldLayout({ laserY: 200 });
    assert.deepEqual(nearMisses(l, traceLayoutDetailed(l)), []);
  });

  it("a hit is not a miss, and neither is a beam that merely ends on the component", () => {
    const l = foldLayout();
    assert.deepEqual(nearMisses(l, traceLayoutDetailed(l)), []);
  });

  it("is reported once per component and beam", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 72.5, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "m1", 112.5, 62.5, 135),
      comp("mirror", "m2", 160, 62.5, 135),
    ]);
    const m = nearMisses(l, traceLayoutDetailed(l));
    assert.deepEqual(m.map((x) => x.componentId).sort(), ["m1", "m2"]);
  });
});

describe("the beam check", () => {
  it("lists components no beam reaches, but only when there is a beam at all", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "m1", 112.5, 62.5, 135),
      comp("detector", "dark", 800, 800, 270),
    ]);
    assert.deepEqual(unlitComponents(l, traceLayoutDetailed(l)), ["dark"]);
    const noSource = layoutOf([comp("mirror", "m1", 0, 0, 0)]);
    assert.equal(diagnostics(noSource, traceLayoutDetailed(noSource)).length, 0);
  });

  it("reports back hits, off-centre hits, near misses, a beam leaving the table, and unlit components, worst first", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "m1", 112.5, 62.5, 315, { label: "Fold" }),
      comp("laser", "las2", 50, 296, 0, { props: { wavelength_nm: 1064 } }),
      comp("mirror", "m2", 112.5, 290, 135),
      comp("laser", "las3", 50, 600, 0, { props: { wavelength_nm: 633 } }),
      comp("detector", "dark", 800, 800, 270),
    ]);
    const d = diagnostics(l, traceLayoutDetailed(l));
    const kinds = d.map((x) => x.kind);
    assert.ok(kinds.includes("back_hit"));
    assert.ok(kinds.includes("off_center"));
    assert.ok(kinds.includes("left_table"));
    assert.ok(kinds.includes("unlit"));
    assert.equal(d[0]!.level, "bad", "the worst problems come first");
    const rank = { bad: 0, warn: 1, ok: 2 } as const;
    for (let i = 1; i < d.length; i++) assert.ok(rank[d[i - 1]!.level] <= rank[d[i]!.level]);
    assert.ok(d.find((x) => x.kind === "back_hit")!.message.includes("Fold"), "the label is shown");
    assert.equal(d.find((x) => x.kind === "unlit")!.componentId, "dark");
    for (const x of d) assert.ok(x.message.length > 0);
  });

  it("says nothing about a well-aligned layout (the lit detector is not unlit)", () => {
    const l = foldLayout();
    assert.deepEqual(diagnostics(l, traceLayoutDetailed(l)), []);
  });

  it("reports the 64-event stop", () => {
    const l = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "mA", 0, 62.5, 0),
      comp("mirror", "mB", 100, 62.5, 180),
    ]);
    assert.ok(diagnostics(l, traceLayoutDetailed(l)).some((x) => x.kind === "max_events"));
  });
});

describe("validation", () => {
  it("accepts a layout built by the editor", () => {
    assert.deepEqual(errorsOf(validateLayout(foldLayout())), []);
  });

  it("rejects what section 3 rejects", () => {
    const l = layoutOf([
      { ...comp("mirror", "bad id", 0, 0, 0), id: "bad id" },
      comp("mirror", "far", 200_000, 0, 0),
      comp("mirror", "thin", 0, 0, 0, { aperture: 600 }),
    ]);
    // the editor's own setters clamp the aperture, so write the bad value directly
    l.components[2] = { ...l.components[2]!, aperture_mm: 600 };
    const e = errorsOf(validateLayout(l)).map((i) => i.path);
    assert.ok(e.includes("components[bad id].id"));
    assert.ok(e.includes("components[far].x_mm"));
    assert.ok(e.includes("components[thin].aperture_mm"));
  });

  it("rejects an unknown type and unknown parameters", () => {
    const l = layoutOf([{ ...comp("mirror", "m1", 0, 0, 0), type: "teleporter" as never }]);
    assert.ok(errorsOf(validateLayout(l)).some((i) => i.path === "components[m1].type"));
    const extra = layoutOf([{ ...comp("mirror", "m1", 0, 0, 0), props: { edge_nm: 500 } }]);
    assert.ok(errorsOf(validateLayout(extra)).some((i) => i.path === "components[m1].props.edge_nm"));
  });

  it("wants the parameters a type needs", () => {
    const noWl = layoutOf([comp("laser", "las1", 0, 0, 0, { props: {} })]);
    assert.ok(errorsOf(validateLayout(noWl)).some((i) => i.path === "components[las1].props.wavelength_nm"));
    const list = layoutOf([comp("laser", "las1", 0, 0, 0, { props: { wavelengths_nm: [532, 1064] } })]);
    assert.deepEqual(errorsOf(validateLayout(list)), [], "a list of wavelengths does instead of the single one");
    const noEdge = layoutOf([comp("dichroic", "dm1", 0, 0, 135, { props: { reflects: "short" } })]);
    assert.ok(errorsOf(validateLayout(noEdge)).some((i) => i.path === "components[dm1].props.edge_nm"));
    const badEnum = layoutOf([comp("dichroic", "dm1", 0, 0, 135, { props: { edge_nm: 550, reflects: "medium" } })]);
    assert.ok(errorsOf(validateLayout(badEnum)).some((i) => i.path === "components[dm1].props.reflects"));
    const badNum = layoutOf([comp("beam_splitter", "bs1", 0, 0, 135, { props: { reflect_fraction: 1.5 } })]);
    assert.ok(errorsOf(validateLayout(badNum)).some((i) => i.path === "components[bs1].props.reflect_fraction"));
    const nan = layoutOf([comp("beam_splitter", "bs1", 0, 0, 135, { props: { reflect_fraction: "half" } })]);
    assert.ok(errorsOf(validateLayout(nan)).some((i) => i.path === "components[bs1].props.reflect_fraction"));
  });

  it("limits the annotations: 200 characters, notes 2000, 20 extras, key 40, value 200", () => {
    let l = layoutOf([comp("mirror", "m1", 0, 0, 0)]);
    l = setAnnotation(l, "m1", { model: "x".repeat(201), notes: "y".repeat(2001) });
    const paths = errorsOf(validateLayout(l)).map((i) => i.path);
    assert.ok(paths.includes("components[m1].annotations.model"));
    assert.ok(paths.includes("components[m1].annotations.notes"));
    const ok = setAnnotation(l, "m1", { model: "x".repeat(200), notes: "y".repeat(2000) });
    assert.deepEqual(errorsOf(validateLayout(ok)), []);
    const many = setAnnotation(ok, "m1", { extra: Array.from({ length: 21 }, (_, i) => ({ key: `k${i}`, value: "v" })) });
    assert.ok(errorsOf(validateLayout(many)).some((i) => i.path === "components[m1].annotations.extra"));
    const longKey = setAnnotation(ok, "m1", { extra: [{ key: "k".repeat(41), value: "v" }, { key: "", value: "v" }, { key: "a", value: "v".repeat(201) }] });
    assert.equal(errorsOf(validateLayout(longKey)).length, 3);
    const dup = setAnnotation(ok, "m1", { extra: [{ key: "a", value: "1" }, { key: "a", value: "2" }] });
    assert.equal(validateLayout(dup).filter((i) => i.level === "warn").length, 1, "a repeated name is a warning, not an error");
  });

  it("rejects duplicate ids, a bad layout id and a bad table", () => {
    const dup = layoutOf([comp("mirror", "m1", 0, 0, 0), comp("mirror", "m1", 1, 1, 0)]);
    assert.ok(errorsOf(validateLayout(dup)).some((i) => i.message.includes("m1")));
    const l = { ...emptyLayout("not valid!", "t"), table: { width_mm: 0, height_mm: 900, hole_pitch_mm: -1, hole_offset_mm: -1, grid: "metric" } };
    const paths = errorsOf(validateLayout(l)).map((i) => i.path);
    assert.deepEqual(paths.sort(), ["id", "table.hole_offset_mm", "table.hole_pitch_mm", "table.width_mm"]);
  });

  it("a fibre input pointing at nothing is a warning", () => {
    const l = layoutOf([comp("fiber_in", "fi1", 0, 0, 180, { props: { continues_at: "ghost" } })]);
    const issues = validateLayout(l);
    assert.deepEqual(errorsOf(issues), []);
    assert.equal(issues.filter((i) => i.level === "warn").length, 1);
  });

  it("counts the limits", () => {
    const many = layoutOf(Array.from({ length: LIMITS.components + 1 }, (_, i) => comp("mirror", `m${i}`, i, 0, 0)));
    assert.ok(errorsOf(validateLayout(many)).some((i) => i.path === "components"));
    const lasers = layoutOf(Array.from({ length: LIMITS.sources + 1 }, (_, i) => comp("laser", `l${i}`, i, 0, 0)));
    assert.ok(errorsOf(validateLayout(lasers)).some((i) => i.message.includes(String(LIMITS.sources))));
  });

  it("an empty title is only a warning", () => {
    const l = { ...foldLayout(), title: "  " };
    assert.deepEqual(errorsOf(validateLayout(l)), []);
    assert.ok(validateLayout(l).some((i) => i.path === "title"));
  });

  it("finds the issues of one field of one component", () => {
    const l = layoutOf([comp("laser", "las1", 0, 0, 0, { props: {} })]);
    const issues = validateLayout(l);
    assert.equal(issuesFor(issues, "las1", ".props").length, 1);
    assert.equal(issuesFor(issues, "las1", ".aperture_mm").length, 0);
    assert.equal(issuesFor(issues, "other", ".props").length, 0);
  });

  it("the size is counted in UTF-8 bytes, as the server counts it", () => {
    const small = emptyLayout("a", "A");
    const withCjk = { ...small, notes: "光路图".repeat(10) };
    assert.equal(jsonBytes(withCjk) - jsonBytes(small), 30 * 3 - 0, "three bytes per character");
    assert.ok(jsonBytes(small) < LIMITS.jsonBytes);
  });

  it("edits through the setters keep a layout valid", () => {
    let l = emptyLayout("main", "Main");
    for (const t of ["laser", "mirror", "beam_splitter", "dichroic", "filter", "iris", "shutter", "fiber_in"] as const) {
      l = addComponent(l, t, { x: 100, y: 100 }).layout;
    }
    l = setProp(l, "fi1", "continues_at", "ghost");
    assert.deepEqual(errorsOf(validateLayout(l)), []);
    assert.deepEqual(errorsOf(validateComponent(l.components[0]!, l)), []);
  });
});
