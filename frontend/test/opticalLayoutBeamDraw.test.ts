// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/beamDraw.ts, beamChart.ts and the beam look of colors.ts: how the canvas
// draws the light (docs/v2/design/optical_layout_v2.md sections 8.2 to 8.4).
//
//     cd frontend && npm run test:unit
//
// The physics is beam.ts's (and pinned by its own tests against the shared fixtures); what is pinned here is
// the drawing: width and opacity follow the power on a log scale, "no light" is a grey dotted line without
// arrows or marks, the envelope band is the real 1/e² size and only where it is visible, every waist gets a
// tick where beam.ts says it is, the polarisation marks are the classic ones, a click finds the beam under
// it, the power tags say what beam.ts's powerTexts says, and the chart lays the profile out sensibly.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { BAND_MIN_PX, POL_MIN_PX, badgeSide, bandPolygonMm, bandSamples, beamObstacles, beamScene, nearestSegment, pickOf, polMarkKind, powerTags, refOf, segmentOf } from "../src/lib/opticalLayout/beamDraw.ts";
import { CHART_BOX, MARK_LABEL_GAP_PX, chartGeometry, profileAt } from "../src/lib/opticalLayout/beamChart.ts";
import { DARK_P, LOOK_OPACITY, LOOK_WIDTH, beamLook, wavelengthColor } from "../src/lib/opticalLayout/colors.ts";
import { beamProfile, diameterOf, powerTexts, readPolarization, waistInSegment } from "../src/lib/opticalLayout/beam.ts";
import { traceLayoutDetailed } from "../src/lib/opticalLayout/trace.ts";
import type { View } from "../src/lib/opticalLayout/view.ts";
import { comp, layoutOf } from "./helpers/opticalLayout.ts";

const near = (a: number, b: number, tol = 1e-6) => assert.ok(Math.abs(a - b) <= tol, `${a} is not within ${tol} of ${b}`);
const view = (k: number): View => ({ k, ox: 20, oy: 600 });
const ALL = { envelope: true, polarization: true };

/** laser (Ø1 mm, 532 nm, linear at `pol`) at x = 0 -> lens f 50 at 100 -> detector at 400 */
function focusLayout(pol: Record<string, unknown> = { polarization: "linear", pol_angle_deg: 0 }) {
  return layoutOf([
    comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532, power_mw: 40, beam_diameter_mm: 1, ...pol } }),
    comp("lens", "len1", 100, 100, 0, { props: { focal_mm: 50 } }),
    comp("detector", "det1", 400, 100, 180),
  ]);
}

describe("the look of a beam follows its power", () => {
  it("width and opacity grow with log10(p) from 1e-3 to 1", () => {
    const ps = [1e-3, 3e-3, 1e-2, 0.1, 0.5, 1];
    const looks = ps.map((p) => beamLook(532, p, "dark"));
    for (let i = 1; i < looks.length; i++) {
      assert.ok(looks[i]!.width > looks[i - 1]!.width, `width at ${ps[i]}`);
      assert.ok(looks[i]!.opacity >= looks[i - 1]!.opacity, `opacity at ${ps[i]}`);
    }
    assert.equal(looks[0]!.width, LOOK_WIDTH.min);
    assert.equal(looks[looks.length - 1]!.width, LOOK_WIDTH.max);
    assert.equal(looks[looks.length - 1]!.opacity, LOOK_OPACITY.max);
    // a tenth of the power is a third of the way down, not nine tenths
    const tenth = beamLook(532, 0.1, "dark").width;
    near(tenth, LOOK_WIDTH.min + ((LOOK_WIDTH.max - LOOK_WIDTH.min) * 2) / 3, 0.01);
  });

  it("below 1e-3 it stays at the thinnest; below 1e-6 it is 'no light', grey and dotted", () => {
    assert.equal(beamLook(532, 1e-5, "dark").width, LOOK_WIDTH.min);
    const dark = beamLook(532, 0.999e-6, "dark");
    assert.equal(dark.dark, true);
    assert.equal(dark.color, "var(--mast-faint)");
    assert.ok(dark.dash);
    assert.equal(beamLook(532, 0, "light").dark, true);
    assert.equal(beamLook(532, Number.NaN, "light").dark, true);
    assert.equal(beamLook(532, DARK_P * 1.01, "light").dark, false);
  });

  it("exactly 1e-6 is still light (strictly below is dark, as render.py and the checks; an OD 6 filter sits there)", () => {
    assert.equal(DARK_P, 1e-6);
    const at = beamLook(532, 1e-6, "dark");
    assert.equal(at.dark, false);
    assert.equal(at.width, LOOK_WIDTH.min);
    assert.equal(at.dash, null);
    // an OD 6 ND filter in a 1 mW beam: 10^-6 of it gets through, drawn as (faint) light with its marks
    const l = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532, power_mw: 1, polarization: "linear", pol_angle_deg: 0 } }),
      comp("nd", "nd1", 100, 100, 0, { props: { od: 6 } }),
      comp("detector", "det1", 400, 100, 180),
    ]);
    const trace = traceLayoutDetailed(l);
    const after = trace[0]!.branches[0]!.segments[1]!;
    assert.ok(after.beam && Math.abs(after.beam.p - 1e-6) < 1e-12, `p after OD 6 is ${after.beam?.p}`);
    const scene = beamScene(trace, view(1), "dark", ALL);
    assert.ok(scene.cores.every((c) => !c.look.dark), "no dark run");
    assert.ok(scene.pols.some((m) => m.x > 20 + 100), "the polarisation after the filter is marked");
  });

  it("keeps the colour of the wavelength and the dashes of the invisible bands", () => {
    assert.equal(beamLook(633, 0.5, "light").color, wavelengthColor(633, "light"));
    assert.ok(beamLook(1064, 1, "dark").dash);
    assert.ok(beamLook(355, 1, "dark").dash);
    assert.equal(beamLook(532, 1, "dark").dash, null);
  });
});

describe("the beam lines", () => {
  it("one run per branch while the power holds; a polariser that halves it starts a new one", () => {
    const l = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532, polarization: "linear", pol_angle_deg: 45 } }),
      comp("polarizer", "pol1", 100, 100, 0, { props: { axis_deg: 0 } }),
      comp("window", "win1", 200, 100, 0),
      comp("detector", "det1", 300, 100, 180),
    ]);
    const scene = beamScene(traceLayoutDetailed(l), view(1), "dark", ALL);
    assert.equal(scene.cores.length, 2, "full power to the polariser, half after it (the window changes nothing)");
    assert.ok(scene.cores[1]!.look.width < scene.cores[0]!.look.width);
    assert.match(scene.cores[1]!.d, /^M[-\d.]+ [-\d.]+L[-\d.]+ [-\d.]+L/, "two segments in one path");
  });

  it("crossed polarisers: the light after them is drawn as no light, with no arrow and no mark", () => {
    const l = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532, polarization: "linear", pol_angle_deg: 0 } }),
      comp("polarizer", "pol1", 100, 100, 0, { props: { axis_deg: 90 } }),
      comp("detector", "det1", 400, 100, 180),
    ]);
    const scene = beamScene(traceLayoutDetailed(l), view(1), "dark", ALL);
    const dark = scene.cores.find((c) => c.look.dark);
    assert.ok(dark, "a dark run");
    // the dark segment runs from x = 100 to 400: nothing of it gets an arrow or a mark
    assert.ok(scene.arrows.every((a) => a.x < 20 + 100 + 1), JSON.stringify(scene.arrows));
    assert.ok(scene.pols.every((m) => m.x < 20 + 100 + 1), JSON.stringify(scene.pols));
    const end = scene.ends.find((e) => e.end === "stop");
    assert.ok(end && end.look.dark, "the end is dark too");
  });

  it("a trace without beam data falls back on the branch's share (an older server)", () => {
    const t = traceLayoutDetailed(focusLayout());
    const stripped = t.map((tree) => ({ ...tree, beam: undefined, branches: tree.branches.map((b) => ({ ...b, segments: b.segments.map(({ beam: _b, ...g }) => g) })) }));
    const scene = beamScene(stripped, view(1), "dark", ALL);
    assert.equal(scene.bands.length, 0);
    assert.equal(scene.pols.length, 0);
    assert.equal(scene.cores[0]!.look.width, LOOK_WIDTH.max);
  });

  it("an arrow on every segment long enough on the screen, none on short ones", () => {
    const t = traceLayoutDetailed(focusLayout());
    assert.equal(beamScene(t, view(1), "dark", ALL).arrows.length, 2, "100 px and 300 px");
    assert.equal(beamScene(t, view(0.5), "dark", ALL).arrows.length, 1, "50 px and 150 px");
  });
});

describe("the envelope band", () => {
  const trace = traceLayoutDetailed(focusLayout());
  const tree = trace[0]!;
  const seg0 = tree.branches[0]!.segments[0]!;
  const seg1 = tree.branches[0]!.segments[1]!;

  it("has the real 1/e² size: its edges are d(z) apart, from beam.ts, in table millimetres", () => {
    const band = bandPolygonMm(seg0, seg0.beam!.q!, 532, tree.beam!.m2!);
    assert.ok(band);
    const nums = band.d.match(/-?[\d.]+/g)!.map(Number);
    // first point of the left edge and last point of the polygon (first of the right edge) are at the start
    const [lx, ly] = [nums[0]!, nums[1]!];
    const [rx, ry] = [nums[nums.length - 2]!, nums[nums.length - 1]!];
    near(Math.hypot(lx - rx, ly - ry), diameterOf(seg0.beam!.q!, 532, tree.beam!.m2!), 2e-4);
    // centred on the start of the segment (y up: the left of a beam running +x is +y)
    near((lx + rx) / 2, seg0.x0, 1e-4);
    near((ly + ry) / 2, seg0.y0, 1e-4);
    assert.ok(ly > ry, "the left edge comes first");
  });

  it("is worked out once per segment and beam: the same input gives the very same string, whatever the view", () => {
    const a = beamScene(trace, view(10), "dark", ALL).bands;
    const b = beamScene(trace, { k: 10, ox: 300, oy: 900 }, "dark", ALL).bands;
    assert.ok(a.length > 0);
    assert.deepEqual(a.map((x) => x.key), b.map((x) => x.key));
    for (let i = 0; i < a.length; i++) assert.ok(Object.is(a[i]!.d, b[i]!.d), "a pan rebuilds no band");
    const q = seg0.beam!.q!;
    const one = bandPolygonMm(seg0, q, 532, tree.beam!.m2!);
    assert.ok(Object.is(one, bandPolygonMm({ ...seg0 }, [q[0], q[1]], 532, tree.beam!.m2!)), "kept by value, not by object");
    const moved = bandPolygonMm({ ...seg0, x0: seg0.x0 + 1, x1: seg0.x1 + 1 }, q, 532, tree.beam!.m2!);
    assert.ok(moved && one && moved.d !== one.d, "a moved segment is worked out again");
  });

  it("is left out where it would be thinner than 1.5 px, drawn where it is wider", () => {
    // Ø1 mm: 1 px at k = 1, 10 px at k = 10
    const thin = beamScene(trace, view(1), "dark", ALL).bands.filter((b) => b.key.endsWith("|0"));
    const wide = beamScene(trace, view(10), "dark", ALL).bands.filter((b) => b.key.endsWith("|0"));
    assert.equal(thin.length, 0);
    assert.equal(wide.length, 1);
    assert.ok(BAND_MIN_PX === 1.5);
    assert.equal(beamScene(trace, view(10), "dark", { envelope: false, polarization: true }).bands.length, 0, "the layer is off");
  });

  it("is sampled densely at a waist and always at both ends and at the waist itself", () => {
    const q = seg1.beam!.q!;
    const L = Math.hypot(seg1.x1 - seg1.x0, seg1.y1 - seg1.y0);
    const s = bandSamples(q, L);
    assert.equal(s[0], 0);
    assert.equal(s[s.length - 1], L);
    const w = waistInSegment(q, L)!;
    assert.ok(w !== null && s.includes(w), "the waist is a sample");
    const i = s.indexOf(w);
    const nearGap = Math.min(s[i + 1]! - w, w - s[i - 1]!);
    const endGap = s[s.length - 1]! - s[s.length - 2]!;
    assert.ok(nearGap < endGap, `${nearGap} near the waist, ${endGap} at the end`);
    assert.ok(s.every((v, j) => j === 0 || v > s[j - 1]!), "sorted, no repeats");
  });

  it("a waist just past the end of a segment (up to 0.001 mm, section 12) stays inside it", () => {
    // z0 = -L - 0.0005: beam.ts reports the waist 0.0005 mm past the end
    const L = 100;
    const q: [number, number] = [-L - 0.0005, 50];
    const w = waistInSegment(q, L);
    assert.ok(w !== null && w > L, "beam.ts reports it, past the end");
    const s = bandSamples(q, L);
    assert.equal(s[s.length - 1], L);
    assert.ok(s.every((v) => v >= 0 && v <= L));
  });

  it("every waist gets a tick, where beam.ts puts it, across the beam", () => {
    const k = 2;
    const scene = beamScene(trace, view(k), "dark", ALL);
    assert.equal(scene.waists.length, 1);
    const tick = scene.waists[0]!;
    const w = waistInSegment(seg1.beam!.q!, 300)!;
    near(tick.at.x, 20 + (100 + w) * k, 1e-6);
    near(tick.at.y, 600 - 100 * k, 1e-6);
    near(tick.x1, tick.x2, 1e-9);
    assert.ok(Math.abs(tick.y1 - tick.y2) >= 12, "long enough to see on a µm focus");
    assert.ok(tick.dMm < 0.1, "the focus of f 50 on Ø1 mm is tens of µm");
  });
});

describe("polarisation marks", () => {
  const marksOf = (pol: Record<string, unknown>) => beamScene(traceLayoutDetailed(focusLayout(pol)), view(1), "dark", ALL).pols;

  it("p is a double arrow, s are dots, unpolarised both, anything else a badge", () => {
    assert.ok(marksOf({ polarization: "linear", pol_angle_deg: 0 }).every((m) => m.kind === "p"));
    assert.ok(marksOf({ polarization: "linear", pol_angle_deg: 90 }).every((m) => m.kind === "s"));
    assert.ok(marksOf({ polarization: "unpolarized" }).every((m) => m.kind === "unpolarized"));
    const diag = marksOf({ polarization: "linear", pol_angle_deg: 45 });
    assert.ok(diag.length > 0 && diag.every((m) => m.kind === "badge"));
    assert.equal(polMarkKind(readPolarization(null)), null, "no light: no mark");
    assert.equal(polMarkKind(readPolarization([0, 0, 1])), "badge", "circular");
  });

  it("at most one per segment, only on segments long enough on the screen", () => {
    const t = traceLayoutDetailed(focusLayout());
    assert.equal(beamScene(t, view(1), "dark", ALL).pols.length, 2);
    assert.equal(beamScene(t, view(0.5), "dark", ALL).pols.length, 1, `the 50 px segment is shorter than ${POL_MIN_PX}`);
    assert.equal(beamScene(t, view(1), "dark", { envelope: true, polarization: false }).pols.length, 0, "the layer is off");
  });

  it("a badge sits beside the beam, below a level beam and left of an upright one (the read-outs are above and right)", () => {
    const m = marksOf({ polarization: "linear", pol_angle_deg: 45 })[0]!;
    assert.ok(m.by > m.y && Math.abs(m.bx - m.x) < 1e-9, JSON.stringify(m));
    assert.deepEqual(badgeSide(1, 0), { nx: 0, ny: 1 });
    assert.deepEqual(badgeSide(-1, 0), { nx: 0, ny: 1 });
    assert.deepEqual(badgeSide(0, 1), { nx: -1, ny: 0 });
    assert.deepEqual(badgeSide(0, -1), { nx: -1, ny: 0 });
  });

  it("marks off the screen are not made", () => {
    const t = traceLayoutDetailed(focusLayout());
    const clipped = beamScene(t, view(1), "dark", { ...ALL, clip: { x0: 0, y0: 0, x1: 100, y1: 100 } });
    assert.equal(clipped.pols.length, 0);
    assert.equal(clipped.arrows.length, 0);
    assert.ok(clipped.cores.length > 0, "the lines are still drawn: they may cross the view");
  });
});

describe("picking a beam", () => {
  const trace = traceLayoutDetailed(focusLayout());

  it("finds the segment under the pointer within the tolerance, with where along it", () => {
    const hit = nearestSegment(trace, { x: 250, y: 101 }, 2);
    assert.ok(hit);
    assert.deepEqual([hit.tree, hit.branch, hit.segment], [0, "0", 1]);
    near(hit.along, 150, 1e-9);
    near(hit.dist, 1, 1e-9);
    assert.equal(nearestSegment(trace, { x: 250, y: 104 }, 2), null);
  });

  it("of two beams on one path, the one with more light", () => {
    const l = layoutOf([
      comp("laser", "a", 0, 100, 0, { props: { wavelengths_nm: [532, 633] } }),
      comp("nd", "nd1", 50, 100, 0, { props: { od: 1 } }),
      comp("detector", "det1", 300, 100, 180),
    ]);
    const t = traceLayoutDetailed(l);
    // both wavelengths share the path; give the second tree more light by dimming the first with a fake
    const dimmed = t.map((tree, i) => (i === 0 ? { ...tree, branches: tree.branches.map((b) => ({ ...b, segments: b.segments.map((g) => ({ ...g, beam: g.beam ? { ...g.beam, p: g.beam.p / 10 } : g.beam })) })) } : tree));
    assert.equal(nearestSegment(dimmed, { x: 150, y: 100 }, 1)!.tree, 1);
    assert.equal(nearestSegment(t, { x: 150, y: 100 }, 1)!.tree, 0, "equal light: the earlier tree");
  });

  it("a pick survives a re-trace by naming source, wavelength and branch", () => {
    const pick = pickOf(trace, { tree: 0, branch: "0", segment: 1 })!;
    assert.deepEqual(pick, { source: "las1", wavelength_nm: 532, branch: "0", segment: 1 });
    const moved = traceLayoutDetailed({ ...focusLayout(), components: focusLayout().components.map((c) => (c.id === "len1" ? { ...c, x_mm: 120 } : c)) });
    assert.deepEqual(refOf(moved, pick), { tree: 0, branch: "0", segment: 1 });
    assert.ok(segmentOf(moved, refOf(moved, pick)!));
    // the lens taken away: the branch has one segment now
    const fewer = traceLayoutDetailed({ ...focusLayout(), components: focusLayout().components.filter((c) => c.id !== "len1") });
    assert.deepEqual(refOf(fewer, pick), { tree: 0, branch: "0", segment: 0 });
    assert.equal(refOf([], pick), null);
  });
});

describe("where the beams run, for the labels", () => {
  it("a level or upright segment is one thin box; an oblique one a row of small squares; capped", () => {
    const t = traceLayoutDetailed(focusLayout());
    const boxes = beamObstacles(t, view(1), 3);
    assert.equal(boxes.length, 2, "two level segments");
    const [a] = boxes;
    assert.deepEqual([a!.x, a!.y, a!.w, a!.h], [20 - 3, 600 - 100 - 3, 100 + 6, 6]);
    const fold = traceLayoutDetailed(layoutOf([comp("laser", "l", 0, 0, 30, { props: { wavelength_nm: 532 } }), comp("detector", "d", 200, 115.47, 210, { aperture: 40 })]));
    const diag = beamObstacles(fold, view(1), 3);
    assert.ok(diag.length > 10 && diag.every((b) => b.w === 6 && b.h === 6), String(diag.length));
    assert.equal(beamObstacles(fold, view(1), 3, 5).length, 5);
  });
});

describe("power tags", () => {
  it("one per beam that ends on a component, as beam.ts writes the power", () => {
    const t = traceLayoutDetailed(focusLayout());
    const tags = powerTags(t, "dark");
    assert.equal(tags.length, 1);
    assert.equal(tags[0]!.componentId, "det1");
    const ev = t[0]!.branches[0]!.events.at(-1)!;
    assert.deepEqual([tags[0]!.zh, tags[0]!.en], [powerTexts(ev.beam!.p, ev.beam!.mw).zh, powerTexts(ev.beam!.p, ev.beam!.mw).en]);
    assert.equal(tags[0]!.en, powerTexts(1, 40).en, "40 mW at the detector: nothing on the way takes any");
  });

  it("beams ending on the same component stack; no beam data, no tags", () => {
    const l = layoutOf([comp("laser", "a", 0, 100, 0, { props: { wavelengths_nm: [532, 633] } }), comp("detector", "det1", 300, 100, 180)]);
    const t = traceLayoutDetailed(l);
    assert.deepEqual(powerTags(t, "dark").map((x) => x.slot), [0, 1]);
    assert.deepEqual(powerTags(t, "dark").map((x) => x.en), [powerTexts(1, null).en, powerTexts(1, null).en], "no power_mw: the share of the source");
    const stripped = t.map((tree) => ({ ...tree, branches: tree.branches.map((b) => ({ ...b, events: b.events.map(({ beam: _b, ...e }) => e) })) }));
    assert.deepEqual(powerTags(stripped, "dark"), []);
  });
});

describe("the spot chart", () => {
  const trace = traceLayoutDetailed(focusLayout());
  const profile = beamProfile(trace[0]!, "0")!;

  it("puts the path on x and the diameter on y, from 0, inside the box", () => {
    const g = chartGeometry(profile, CHART_BOX, { from: 100, to: 400 });
    near(g.xOf(0), g.x0);
    near(g.xOf(400), g.x1);
    near(g.yOf(0), g.y0);
    assert.ok(g.yOf(Math.max(...profile.points.map((p) => p.dMm))) >= g.y1 - 1e-9, "the largest diameter fits");
    assert.ok(g.xTicks.length >= 2 && g.xTicks.every((t) => t.x >= g.x0 - 1e-9 && t.x <= g.x1 + 1e-9));
    assert.ok(g.yTicks.length >= 2 && g.yTicks.every((t) => t.y <= g.y0 + 1e-9 && t.y >= g.y1 - 1e-9));
    assert.ok(g.span && g.span.x0 > g.x0 && Math.abs(g.span.x1 - g.x1) < 1e-9);
    assert.equal(g.waists.length, 1);
    assert.match(g.d, /^M/);
    near(g.pathAt(g.xOf(123)), 123, 1e-9);
  });

  it("names a component only where there is room, and speaks µm for a small beam", () => {
    const crowded = { ...profile, marks: [0, 1, 2, 300].map((x, i) => ({ pathMm: x, componentId: `c${i}`, action: "transmit" })) };
    const g = chartGeometry(crowded);
    assert.deepEqual(g.marks.map((m) => m.labelled), [true, false, false, true]);
    assert.ok(MARK_LABEL_GAP_PX > 10);
    const tiny = { ...profile, points: profile.points.map((p) => ({ ...p, dMm: p.dMm / 100 })) };
    assert.equal(chartGeometry(tiny).yUnit, "µm");
    assert.equal(chartGeometry(profile).yUnit, "mm");
  });

  it("reads the profile at a path", () => {
    const at = profileAt(profile, 50)!;
    near(at.pathMm, 50, 5);
    assert.equal(profileAt({ points: [], marks: [], waists: [] }, 10), null);
  });
});
