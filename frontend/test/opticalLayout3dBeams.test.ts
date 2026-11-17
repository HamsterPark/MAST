// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/scene3d/beams3d.ts: the light in the 3D view
// (docs/v2/design/optical_layout_3d_view.md section 4).
//
//     cd frontend && npm run test:unit
//
// The 3D view draws the editor's own trace and computes nothing again: every tube is a traced segment at its
// height (segment.z_mm, else the table's beam height), as heavy, as coloured and as dashed as its 2D line
// (colors.ts beamLook of the same power); where the light changes height a vertical tube joins the two
// segments; the envelope is beam.ts's 1/e² size where beamDraw.ts samples its band; the polarisation is drawn
// along the real field (p in the table plane to the left of the travel, s straight up, an ellipse across the
// beam); the power tags are beamDraw.ts's, at the height the light arrives.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { Segment } from "../src/lib/opticalLayout/types.ts";
import { traceLayoutDetailed, type DetailedTrace } from "../src/lib/opticalLayout/trace.ts";
import { beamLook } from "../src/lib/opticalLayout/colors.ts";
import { bandSamples, powerTags, segmentPower } from "../src/lib/opticalLayout/beamDraw.ts";
import { diameterOf, readPolarization, waistInSegment } from "../src/lib/opticalLayout/beam.ts";
import { beamEnvelopes, beamTubes, fieldDirection, parseDash, passMarks, polarizationMarks, powerTags3d, transverseFrame } from "../src/lib/opticalLayout/scene3d/beams3d.ts";
import type { Vec3 } from "../src/lib/opticalLayout/scene3d/mat4.ts";
import { verticalRuns } from "../src/lib/opticalLayout/heights.ts";
import { comp, foldLayout, layoutOf } from "./helpers/opticalLayout.ts";

const near = (a: number, b: number, tol = 1e-9) => assert.ok(Math.abs(a - b) <= tol, `${a} is not within ${tol} of ${b}`);
const nearVec = (a: Vec3, b: Vec3, tol = 1e-9) => a.forEach((v, i) => near(v, b[i]!, tol));
const dot = (a: Vec3, b: Vec3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

/** The same trace with heights on its segments (and the events those segments lead to), as a trace with heights has them. */
function withHeights(trace: DetailedTrace, z: (tree: number, branch: string, seg: number) => number | undefined): DetailedTrace {
  return trace.map((t, ti) => ({
    ...t,
    branches: t.branches.map((b) => ({
      ...b,
      segments: b.segments.map((g, si) => {
        const h = z(ti, b.branch, si);
        return h === undefined ? g : ({ ...g, z_mm: h } as Segment);
      }),
      // an event is where its segment ends: the beam arrives at that segment's height
      events: b.events.map((e, i) => {
        const h = z(ti, b.branch, i);
        return h === undefined ? e : { ...e, z_mm: h };
      }),
    })),
  }));
}

/** laser (Ø1 mm, 532 nm, `pol`) along +x → lens f 50 at 100 → detector at 400 */
function lineLayout(props: Record<string, unknown> = { wavelength_nm: 532, polarization: "linear", pol_angle_deg: 0, beam_diameter_mm: 1 }) {
  return layoutOf([comp("laser", "las1", 0, 100, 0, { props }), comp("lens", "len1", 100, 100, 0, { props: { focal_mm: 50 } }), comp("detector", "det1", 400, 100, 180)]);
}

describe("every traced segment is a tube at its height", () => {
  it("at the table's beam height (75 mm when the table does not say)", () => {
    const l = foldLayout();
    const trace = traceLayoutDetailed(l);
    const tubes = beamTubes(trace, l.table, "dark");
    const segs = trace.flatMap((t) => t.branches.flatMap((b) => b.segments));
    assert.equal(tubes.length, segs.length);
    tubes.forEach((t, i) => {
      const g = segs[i]!;
      assert.equal(t.joint, false);
      nearVec(t.a, [g.x0, g.y0, 75]);
      nearVec(t.b, [g.x1, g.y1, 75]);
    });
    // traced again on a table that says 120 mm: a trace with heights carries it in z_mm, one without leaves it to the table
    const high = { ...l, table: { ...l.table, beam_height_mm: 120 } as typeof l.table };
    const raised = beamTubes(traceLayoutDetailed(high), high.table, "dark");
    assert.equal(raised.length, tubes.length);
    for (const t of raised) assert.equal(t.a[2], 120);
  });

  it("at the segment's own height when the trace gives one, and joined by a vertical tube where it changes", () => {
    const l = foldLayout();
    const trace = withHeights(traceLayoutDetailed(l), (_t, _b, si) => (si === 0 ? 75 : 160));
    const tubes = beamTubes(trace, l.table, "dark");
    const joints = tubes.filter((t) => t.joint);
    assert.equal(joints.length, 1);
    const j = joints[0]!;
    const second = trace[0]!.branches[0]!.segments[1]!;
    nearVec(j.a, [second.x0, second.y0, 75]);
    nearVec(j.b, [second.x0, second.y0, 160]);
    assert.deepEqual(j.ref, { tree: 0, branch: trace[0]!.branches[0]!.branch, segment: 1 });
    const after = tubes.find((t) => !t.joint && t.ref.segment === 1)!;
    assert.equal(after.a[2], 160);
    // the joint looks like the light after the change
    assert.equal(j.color, after.color);
    assert.equal(j.widthPx, after.widthPx);
  });

  it("the joints are the height contract's vertical runs (heights.ts verticalRuns), at the event, from its z_mm", () => {
    const l = foldLayout();
    const base = traceLayoutDetailed(l);
    // what a trace with a periscope at the fold would say: 75 mm before it, 160 mm after; the event at the arriving 75
    const trace = withHeights(base, (_t, _b, si) => (si === 0 ? 75 : 160)).map((t) => ({ ...t, branches: t.branches.map((b) => ({ ...b, events: b.events.map((e, i) => ({ ...e, z_mm: i === 0 ? 75 : 160 })) })) }));
    const runs = trace.flatMap((t) => t.branches.flatMap((b) => verticalRuns(b, l.table)));
    const joints = beamTubes(trace, l.table, "dark").filter((t) => t.joint);
    assert.equal(runs.length, 1);
    assert.equal(joints.length, runs.length);
    const [r, j] = [runs[0]!, joints[0]!];
    nearVec(j.a, [r.x, r.y, r.z0], 1e-3);
    nearVec(j.b, [r.x, r.y, r.z1], 1e-3);
  });

  it("a branch that splits off at another height is joined to its parent", () => {
    const l = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 633 } }),
      comp("beam_splitter", "bs1", 100, 100, 135, { props: { reflect_fraction: 0.5 } }),
      comp("detector", "d1", 300, 100, 180),
      comp("detector", "d2", 100, 300, 270),
    ]);
    const trace = traceLayoutDetailed(l);
    const children = trace[0]!.branches.filter((b) => b.parent !== null);
    assert.equal(children.length, 2);
    const raised = withHeights(trace, (_t, branch) => (branch.endsWith(".r") ? 140 : undefined));
    const joints = beamTubes(raised, l.table, "light").filter((t) => t.joint);
    assert.equal(joints.length, 1);
    nearVec(joints[0]!.a, [100, 100, 75]);
    nearVec(joints[0]!.b, [100, 100, 140]);
  });

  it("as heavy, as coloured and as dashed as its 2D line", () => {
    const l = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelengths_nm: [355, 532, 1064] } }),
      comp("beam_splitter", "bs1", 100, 100, 135, { props: { reflect_fraction: 0.05 } }),
      comp("detector", "d1", 300, 100, 180),
      comp("detector", "d2", 100, 300, 270),
    ]);
    const trace = traceLayoutDetailed(l);
    for (const theme of ["dark", "light"] as const) {
      const tubes = beamTubes(trace, l.table, theme);
      for (const t of tubes) {
        const tree = trace[t.ref.tree]!;
        const br = tree.branches.find((b) => b.branch === t.ref.branch)!;
        const look = beamLook(tree.wavelength_nm, segmentPower(br, br.segments[t.ref.segment]!), theme);
        assert.equal(t.color, look.color);
        assert.equal(t.widthPx, look.width);
        assert.equal(t.opacity, look.opacity);
        assert.deepEqual(t.dash, parseDash(look.dash));
        assert.equal(t.dark, look.dark);
      }
      // the reflected 5 % is thinner than the transmitted 95 %
      const r = tubes.find((t) => t.ref.tree === 1 && t.ref.branch.endsWith(".r"))!;
      const tt = tubes.find((t) => t.ref.tree === 1 && t.ref.branch.endsWith(".t"))!;
      assert.ok(r.widthPx < tt.widthPx);
    }
    const dashOf = (nm: number) => beamTubes(trace, l.table, "dark").find((t) => trace[t.ref.tree]!.wavelength_nm === nm)!.dash;
    assert.deepEqual(dashOf(355), [2, 4], "ultraviolet");
    assert.equal(dashOf(532), null, "visible");
    assert.deepEqual(dashOf(1064), [9, 5], "infrared");
  });

  it("no light is grey and dotted, as in 2D", () => {
    const l = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532, polarization: "linear", pol_angle_deg: 0 } }),
      comp("polarizer", "p1", 100, 100, 0, { props: { axis_deg: 90 } }),
      comp("detector", "d1", 300, 100, 180),
    ]);
    const tubes = beamTubes(traceLayoutDetailed(l), l.table, "dark");
    const last = tubes[tubes.length - 1]!;
    assert.equal(last.dark, true);
    assert.equal(last.color, "var(--mast-faint)");
    assert.deepEqual(last.dash, [1.5, 3.5]);
    assert.equal(tubes[0]!.dark, false);
  });

  it("parseDash", () => {
    assert.deepEqual(parseDash("9 5"), [9, 5]);
    assert.deepEqual(parseDash("1.5,3.5"), [1.5, 3.5]);
    assert.equal(parseDash(null), null);
    assert.equal(parseDash("0 0"), null);
    assert.equal(parseDash("x"), null);
  });
});

describe("the envelope", () => {
  it("is the 1/e² size where the 2D band is sampled, with the waist where beam.ts puts it", () => {
    const l = lineLayout();
    const trace = traceLayoutDetailed(l);
    const envs = beamEnvelopes(trace, l.table, "dark");
    const segs = trace[0]!.branches[0]!.segments;
    assert.equal(envs.length, segs.length);
    envs.forEach((e, i) => {
      const g = segs[i]!;
      const q = g.beam!.q!;
      const L = Math.hypot(g.x1 - g.x0, g.y1 - g.y0);
      assert.deepEqual(e.samples.map((s) => s[0]), bandSamples(q, L));
      for (const [s, r] of e.samples) near(r, diameterOf([q[0] + s, q[1]], 532, 1) / 2, 1e-12);
      const w = waistInSegment(q, L);
      assert.equal(e.waist === null, w === null);
      nearVec(e.a, [g.x0, g.y0, 75]);
    });
    // the lens focuses: the second segment has a waist of a few tens of micrometres
    const focus = envs[1]!;
    assert.ok(focus.waist !== null && focus.waist > 40 && focus.waist < 60, String(focus.waist));
    assert.ok(Math.min(...focus.samples.map((s) => s[1])) < 0.05);
  });

  it("nothing without a beam size, and nothing where there is no light", () => {
    const l = lineLayout({ wavelength_nm: 532 });
    assert.deepEqual(beamEnvelopes(traceLayoutDetailed(l), l.table, "dark"), []);
    const dark = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532, polarization: "linear", pol_angle_deg: 0, beam_diameter_mm: 1 } }),
      comp("polarizer", "p1", 100, 100, 0, { props: { axis_deg: 90 } }),
      comp("detector", "d1", 300, 100, 180),
    ]);
    assert.equal(beamEnvelopes(traceLayoutDetailed(dark), dark.table, "dark").length, 1, "only the segment before the crossed polariser");
  });
});

describe("the polarisation, along the real field", () => {
  const OPTS = { sizeMm: 10, minLengthMm: 30 };
  const markOf = (pol: Record<string, unknown>, angle = 0) => {
    const l = layoutOf([comp("laser", "las1", 200, 200, angle, { props: { wavelength_nm: 532, ...pol } }), comp("beam_dump", "bd1", 200 + 300 * Math.cos((angle * Math.PI) / 180), 200 + 300 * Math.sin((angle * Math.PI) / 180), angle + 180)]);
    return polarizationMarks(traceLayoutDetailed(l), l.table, OPTS);
  };
  const shaft = (m: ReturnType<typeof markOf>[number]): Vec3 => {
    const [a, b] = m.lines[0]!;
    const v: Vec3 = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
    const n = Math.hypot(...v);
    return [v[0] / n, v[1] / n, v[2] / n];
  };

  it("p (0°) lies in the table plane, to the left of the travel; s (90°) points straight up", () => {
    for (const angle of [0, 90, 210]) {
      const t = (angle * Math.PI) / 180;
      const d: Vec3 = [Math.cos(t), Math.sin(t), 0];
      const p = markOf({ polarization: "linear", pol_angle_deg: 0 }, angle);
      assert.equal(p.length, 1);
      assert.equal(p[0]!.kind, "linear");
      nearVec(shaft(p[0]!), transverseFrame(d).eh, 1e-12);
      near(shaft(p[0]!)[2], 0);
      const s = markOf({ polarization: "linear", pol_angle_deg: 90 }, angle);
      nearVec(shaft(s[0]!), [0, 0, 1], 1e-9);
    }
  });

  it("45° is half way between, and every arrow lies across the beam", () => {
    const m = markOf({ polarization: "linear", pol_angle_deg: 45 })[0]!;
    nearVec(shaft(m), [0, Math.SQRT1_2, Math.SQRT1_2], 1e-9);
    nearVec(fieldDirection([1, 0, 0], 45), [0, Math.SQRT1_2, Math.SQRT1_2], 1e-12);
    near(dot(shaft(m), [1, 0, 0]), 0);
    // the mark is `sizeMm` long, at POL_AT along the segment, at the beam's height
    const [a, b] = m.lines[0]!;
    near(Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]), OPTS.sizeMm);
    nearVec(m.at, [200 + 300 * 0.28, 200, 75], 1e-9);
  });

  it("unpolarised light: both directions", () => {
    const m = markOf({})[0]!;
    assert.equal(m.kind, "unpolarized");
    const dirs = [m.lines[0]!, m.lines[5]!].map(([a, b]) => [b[0] - a[0], b[1] - a[1], b[2] - a[2]] as Vec3);
    near(Math.abs(dirs[0]![1]), OPTS.sizeMm);
    near(Math.abs(dirs[1]![2]), OPTS.sizeMm);
  });

  it("elliptical light: the ellipse across the beam, its axes in the ratio tan |χ|", () => {
    const l = layoutOf([
      comp("laser", "las1", 0, 200, 0, { props: { wavelength_nm: 532, polarization: "linear", pol_angle_deg: 30 } }),
      comp("waveplate", "q1", 100, 200, 0, { props: { retardance: "quarter", fast_axis_deg: 0 } }),
      comp("beam_dump", "bd1", 400, 200, 180),
    ]);
    const trace = traceLayoutDetailed(l);
    const marks = polarizationMarks(trace, l.table, OPTS);
    const e = marks.find((m) => m.kind === "ellipse")!;
    const reading = readPolarization(trace[0]!.branches[0]!.segments[1]!.beam!.s);
    assert.equal(e.reading.kind, reading.kind);
    const pts = e.lines.map((s) => s[0]);
    for (const p of pts) near(p[0], e.at[0], 1e-9); // in the plane across the beam (the beam runs along x)
    const major = fieldDirection([1, 0, 0], reading.psi_deg);
    const minor = fieldDirection([1, 0, 0], reading.psi_deg + 90);
    const rel = (p: Vec3): Vec3 => [p[0] - e.at[0], p[1] - e.at[1], p[2] - e.at[2]];
    const a = Math.max(...pts.map((p) => Math.abs(dot(rel(p), major))));
    const b = Math.max(...pts.map((p) => Math.abs(dot(rel(p), minor))));
    near(a, OPTS.sizeMm / 2, 1e-9);
    near(b / a, Math.abs(Math.tan((reading.chi_deg * Math.PI) / 180)), 1e-3);
  });

  it("no mark on a short segment or where there is no light", () => {
    const short = layoutOf([comp("laser", "las1", 0, 200, 0, { props: { wavelength_nm: 532 } }), comp("beam_dump", "bd1", 20, 200, 180)]);
    assert.deepEqual(polarizationMarks(traceLayoutDetailed(short), short.table, OPTS), []);
    const dark = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532, polarization: "linear", pol_angle_deg: 0 } }),
      comp("polarizer", "p1", 100, 100, 0, { props: { axis_deg: 90 } }),
      comp("detector", "d1", 300, 100, 180),
    ]);
    assert.equal(polarizationMarks(traceLayoutDetailed(dark), dark.table, OPTS).length, 1, "only before the crossed polariser");
  });
});

describe("power where a beam ends", () => {
  it("beamDraw.ts's tags, at the height the light arrives", () => {
    const l = lineLayout({ wavelength_nm: 532, power_mw: 20 });
    const trace = traceLayoutDetailed(l);
    const flat = powerTags(trace, "dark");
    const tags = powerTags3d(trace, l.table, "dark");
    assert.equal(tags.length, flat.length);
    tags.forEach((t, i) => {
      assert.equal(t.zh, flat[i]!.zh);
      assert.equal(t.en, flat[i]!.en);
      nearVec(t.at, [flat[i]!.at.x, flat[i]!.at.y, 75]);
    });
    const raised = withHeights(trace, () => 130);
    assert.equal(powerTags3d(raised, l.table, "dark")[0]!.at[2], 130);
  });
});

describe("where a beam passes over or under a component", () => {
  it("a thin line from the beam to the component's axis, at the pass (the contract's segment.passes)", () => {
    const l = foldLayout();
    const base = withHeights(traceLayoutDetailed(l), (_t, _b, si) => (si === 0 ? 250 : undefined));
    // the first segment passes over m_low (axis 75) at x = 80 and under a high one (axis 300) at x = 90
    const trace = base.map((t) => ({
      ...t,
      branches: t.branches.map((b) => ({
        ...b,
        segments: b.segments.map((g, si) => (si === 0 ? ({ ...g, passes: [{ component: "m_low", x: 80, y: 62.5, dz_mm: 175 }, { component: "m_high", x: 90, y: 62.5, dz_mm: -50 }] } as Segment) : g)),
      })),
    }));
    const marks = passMarks(trace, l.table);
    assert.equal(marks.length, 2);
    nearVec(marks[0]!.at, [80, 62.5, 250]);
    nearVec(marks[0]!.axis, [80, 62.5, 75]);
    assert.equal(marks[0]!.component, "m_low");
    nearVec(marks[1]!.axis, [90, 62.5, 300], 1e-9);
    assert.ok(marks[1]!.dz < 0, "under it");
  });

  it("none for a trace without heights, and nothing made of a malformed entry", () => {
    const l = foldLayout();
    const trace = traceLayoutDetailed(l);
    assert.deepEqual(passMarks(trace, l.table), []);
    const odd = trace.map((t) => ({ ...t, branches: t.branches.map((b) => ({ ...b, segments: b.segments.map((g) => ({ ...g, passes: [{ component: "m1", x: "80", y: 1, dz_mm: 3 }, null, { component: "m1", x: 1, y: 2, dz_mm: Number.NaN }] }) as Segment) })) }));
    assert.deepEqual(passMarks(odd, l.table), []);
  });
});
