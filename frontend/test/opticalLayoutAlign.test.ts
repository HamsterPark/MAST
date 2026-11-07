// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/align.ts — the aim tool, "put on the beam", the
// measurements.
//
//     cd frontend && npm run test:unit
//
// The one the spec names: "the aim tool turns +x into +y, i.e. 135 degrees". The rest
// check the thing the tool is for: after it has run, the trace really goes where the
// operator pointed it, also when the beam does not hit the centre of the mirror.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  aimErrorText,
  aimMirror,
  axisAngleFor,
  beamPathBetween,
  isAimable,
  legsAfter,
  measure,
  nearestBeamPoint,
  placeAlongBeam,
  pointAlongBeam,
  putOnBeam,
  solveAim,
} from "../src/lib/opticalLayout/align.ts";
import { traceLayoutDetailed } from "../src/lib/opticalLayout/trace.ts";
import { reflect, dirOf } from "../src/lib/opticalLayout/geometry.ts";
import { comp, foldLayout, layoutOf } from "./helpers/opticalLayout.ts";

const near = (a: number, b: number, tol = 1e-9) => assert.ok(Math.abs(a - b) <= tol, `${a} is not within ${tol} of ${b}`);

/** Distance from a point to the infinite line through `from` along `dir`. */
function distanceToRay(p: { x: number; y: number }, from: { x: number; y: number }, dir: { x: number; y: number }): number {
  return Math.abs((p.x - from.x) * dir.y - (p.y - from.y) * dir.x);
}

describe("solveAim", () => {
  it("turns +x into +y with a normal at 135 degrees", () => {
    const r = solveAim({ dir: { x: 1, y: 0 }, center: { x: 100, y: 50 }, onBeam: { x: 100, y: 50 }, target: { x: 100, y: 200 } });
    assert.ok(typeof r !== "string");
    near(r.angle_deg, 135);
    near(r.offset_mm, 0);
  });

  it("normal = normalise(-incoming + unit vector to the target), for a few directions", () => {
    for (const [din, tx, ty, expected] of [
      [{ x: 1, y: 0 }, 0, 100, 135],
      [{ x: 1, y: 0 }, 0, -100, 225],
      [{ x: 0, y: 1 }, 100, 0, 315],
      [{ x: -1, y: 0 }, 0, 100, 45],
      [{ x: 1, y: 0 }, -100, 0, 180],
    ] as const) {
      const r = solveAim({ dir: din, center: { x: 0, y: 0 }, onBeam: { x: 0, y: 0 }, target: { x: tx, y: ty } });
      assert.ok(typeof r !== "string", String(r));
      near(r.angle_deg, expected, 1e-9);
    }
  });

  it("a beam that does not hit the centre: the reflection starts where the beam meets the turned mirror", () => {
    // 3 mm above the centre of a mirror at (112.5, 62.5) that now sits at 135 degrees
    const d = { x: 1, y: 0 };
    const target = { x: 112.5, y: 200 };
    const r = solveAim({ dir: d, center: { x: 112.5, y: 62.5 }, onBeam: { x: 115.5, y: 65.5 }, target });
    assert.ok(typeof r !== "string");
    const n = dirOf(r.angle_deg);
    const out = reflect(d, n);
    assert.ok(distanceToRay(target, r.hit, out) < 1e-9, "the reflected ray passes through the target");
    // and the hit point is on the mirror's line
    near((r.hit.x - 112.5) * n.x + (r.hit.y - 62.5) * n.y, 0, 1e-9);
  });

  it("refuses a target straight ahead and a target at the hit point", () => {
    assert.equal(solveAim({ dir: { x: 1, y: 0 }, center: { x: 0, y: 0 }, onBeam: { x: 0, y: 0 }, target: { x: 50, y: 0 } }), "straight-ahead");
    assert.equal(solveAim({ dir: { x: 1, y: 0 }, center: { x: 0, y: 0 }, onBeam: { x: 0, y: 0 }, target: { x: 0, y: 0 } }), "target-at-hit");
  });
});

describe("aimMirror on a layout", () => {
  it("turns a mirror facing the wrong way into the 135 degree fold, exactly", () => {
    const l = foldLayout({ mirrorAngle: 0 });
    const trace = traceLayoutDetailed(l);
    assert.equal(trace[0]!.branches[0]!.end, "back_hit");
    const r = aimMirror(l, trace, "m1", { componentId: "det1" });
    assert.ok(r.ok);
    assert.equal(r.angle_deg, 135);
    assert.equal(r.layout.components.find((c) => c.id === "m1")?.angle_deg, 135);
    // the mirror stays where it was
    assert.equal(r.layout.components.find((c) => c.id === "m1")?.x_mm, 112.5);
    // and now the beam reaches the detector
    const b = traceLayoutDetailed(r.layout)[0]!.branches[0]!;
    assert.equal(b.end, "stop");
    assert.equal(b.events.at(-1)?.component, "det1");
  });

  it("does not touch the layout it was given", () => {
    const l = foldLayout({ mirrorAngle: 0 });
    aimMirror(l, traceLayoutDetailed(l), "m1", { componentId: "det1" });
    assert.equal(l.components[1]!.angle_deg, 0);
  });

  it("an off-centre beam still arrives at the target, to within the 0.01 degree the angle is saved at", () => {
    const l = foldLayout({ laserY: 65.5 });
    const trace = traceLayoutDetailed(l);
    const r = aimMirror(l, trace, "m1", { componentId: "det1" });
    assert.ok(r.ok);
    const after = traceLayoutDetailed(r.layout)[0]!.branches[0]!;
    const hit = after.events[0]!;
    assert.equal(hit.component, "m1");
    const next = after.segments[1]!;
    const dir = { x: next.x1 - next.x0, y: next.y1 - next.y0 };
    const len = Math.hypot(dir.x, dir.y);
    const det = { x: 112.5, y: 200 };
    const miss = distanceToRay(det, hit.pos, { x: dir.x / len, y: dir.y / len });
    // the angle is saved at 0.01 degree (at most 0.005 off), a mirror error shows doubled in the beam
    const bound = 2 * 0.005 * (Math.PI / 180) * Math.hypot(det.x - hit.pos.x, det.y - hit.pos.y);
    assert.ok(miss <= bound * 1.01, `misses the detector centre by ${miss} mm, more than the ${bound} mm the rounding allows`);
  });

  it("aims at a point of the platform as well as at a component", () => {
    const l = foldLayout({ mirrorAngle: 0 });
    const r = aimMirror(l, traceLayoutDetailed(l), "m1", { x: 112.5, y: 400 });
    assert.ok(r.ok);
    assert.equal(r.angle_deg, 135);
  });

  it("a retro-reflection sends the beam back to the source", () => {
    const l = foldLayout({ mirrorAngle: 135 });
    const r = aimMirror(l, traceLayoutDetailed(l), "m1", { componentId: "las1" });
    assert.ok(r.ok);
    assert.equal(r.angle_deg, 180);
  });

  it("works on beam splitters and gratings as well", () => {
    for (const type of ["beam_splitter", "grating", "dichroic"] as const) {
      const l = layoutOf([
        comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
        comp(type, "x1", 112.5, 62.5, 10),
        comp("detector", "det1", 112.5, 200, 270),
      ]);
      const r = aimMirror(l, traceLayoutDetailed(l), "x1", { componentId: "det1" });
      assert.ok(r.ok, type);
      assert.equal(r.angle_deg, 135, type);
    }
  });

  it("tells why it cannot aim", () => {
    const l = foldLayout();
    const t = traceLayoutDetailed(l);
    const err = (id: string, target: Parameters<typeof aimMirror>[3]) => {
      const r = aimMirror(l, t, id, target);
      assert.ok(!r.ok);
      return r.error;
    };
    assert.equal(err("nope", { componentId: "det1" }), "unknown-component");
    assert.equal(err("det1", { componentId: "m1" }), "not-reflective");
    assert.equal(err("las1", { componentId: "det1" }), "not-reflective");
    assert.equal(err("m1", { componentId: "m1" }), "same-component");
    assert.equal(err("m1", { componentId: "nope" }), "no-target");
    assert.equal(err("m1", { x: 500, y: 62.5 }), "straight-ahead");

    const off = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("mirror", "m1", 112.5, 400, 135),
      comp("detector", "det1", 112.5, 600, 270),
    ]);
    const r = aimMirror(off, traceLayoutDetailed(off), "m1", { componentId: "det1" });
    assert.ok(!r.ok);
    assert.equal(r.error, "no-beam");
    for (const e of ["unknown-component", "not-reflective", "no-beam", "no-target", "same-component", "straight-ahead", "target-at-hit"] as const) {
      assert.ok(aimErrorText(e).length > 3, e);
    }
  });

  it("picks the strongest beam when several reach the mirror", () => {
    // The splitter sends 0.2 up to mc, which turns it along +x; the 0.8 goes on to ma, which
    // turns it along +y. Both end on mb: the weak one travelling +x, the strong one +y.
    const l = layoutOf([
      comp("laser", "las1", 0, 0, 0, { props: { wavelength_nm: 532 } }),
      comp("beam_splitter", "bs1", 100, 0, 135, { props: { reflect_fraction: 0.2 } }),
      comp("mirror", "mc", 100, 100, 315),
      comp("mirror", "ma", 300, 0, 135),
      comp("mirror", "mb", 300, 100, 225),
    ]);
    const t = traceLayoutDetailed(l);
    const hits = t[0]!.branches.flatMap((b) => b.events.filter((e) => e.component === "mb").map(() => b.fraction));
    assert.deepEqual(hits.sort(), [0.2, 0.8]);
    // aimed at a point straight ahead of the weak beam, only the strong one can be aimed at it
    const r = aimMirror(l, t, "mb", { x: 500, y: 100 });
    assert.ok(r.ok, "the strong beam (+y) is the one that is aimed");
    assert.equal(r.angle_deg, 315);
  });

  it("a locked mirror can still be aimed (the lock only guards against dragging)", () => {
    const base = foldLayout({ mirrorAngle: 0 });
    const l = { ...base, components: base.components.map((c) => (c.id === "m1" ? { ...c, locked: true } : c)) };
    const r = aimMirror(l, traceLayoutDetailed(l), "m1", { componentId: "det1" });
    assert.ok(r.ok);
  });

  it("knows what can be aimed", () => {
    assert.ok(isAimable(comp("mirror", "a", 0, 0, 0)));
    assert.ok(isAimable(comp("beam_splitter", "a", 0, 0, 0)));
    assert.ok(isAimable(comp("grating", "a", 0, 0, 0)));
    assert.ok(isAimable(comp("sample", "a", 0, 0, 0, { props: { reflects: true } })));
    assert.ok(!isAimable(comp("sample", "a", 0, 0, 0, { props: { reflects: false } })));
    assert.ok(isAimable(comp("custom", "a", 0, 0, 0, { props: { behavior: "reflect" } })));
    assert.ok(!isAimable(comp("lens", "a", 0, 0, 0)));
  });
});

describe("put on the beam", () => {
  it("moves a component to the nearest point of a beam", () => {
    const l = layoutOf([comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }), comp("mirror", "m1", 100, 70, 135)]);
    const b = nearestBeamPoint(l, "m1");
    assert.ok(b);
    near(b.point.x, 100);
    near(b.point.y, 62.5);
    near(b.distance, 7.5);
    const r = putOnBeam(l, "m1");
    assert.ok(r);
    assert.equal(r.layout.components[1]?.y_mm, 62.5);
    assert.equal(r.layout.components[1]?.x_mm, 100);
    assert.equal(l.components[1]?.y_mm, 70, "the original is untouched");
  });

  it("centres a component that sits on the beam but off its centre", () => {
    const l = foldLayout({ laserY: 65.5 });
    const r = putOnBeam(l, "m1");
    assert.ok(r);
    assert.equal(r.layout.components[1]?.y_mm, 65.5);
    const hit = traceLayoutDetailed(r.layout)[0]!.branches[0]!.events[0]!;
    near(hit.offset_mm, 0, 1e-9);
  });

  it("looks at the beams without the component, so its own reflection does not attract it", () => {
    const l = foldLayout();
    // m1 is on the beam; without it the beam runs straight on and passes through its centre
    const b = nearestBeamPoint(l, "m1");
    assert.ok(b);
    near(b.distance, 0, 1e-9);
    assert.deepEqual(b.dir, { x: 1, y: 0 });
  });

  it("will not move a source and has nothing to do without a beam", () => {
    assert.equal(nearestBeamPoint(foldLayout(), "las1"), null);
    assert.equal(nearestBeamPoint(foldLayout(), "nope"), null);
    assert.equal(nearestBeamPoint(layoutOf([comp("mirror", "m1", 0, 0, 0)]), "m1"), null);
    assert.equal(putOnBeam(layoutOf([comp("mirror", "m1", 0, 0, 0)]), "m1"), null);
  });
});

describe("along the beam", () => {
  const l = foldLayout();
  const t = traceLayoutDetailed(l);

  it("a distance behind the source", () => {
    const r = pointAlongBeam(l, t, "las1", 50);
    assert.ok(r);
    near(r.point.x, 100);
    near(r.point.y, 62.5);
    assert.equal(r.beyondLeg, false);
    // the first leg is only 62.5 mm long: 100 mm lies past the mirror, on the extended line
    const past = pointAlongBeam(l, t, "las1", 100);
    assert.ok(past);
    near(past.point.x, 150);
    assert.equal(past.beyondLeg, true);
  });

  it("a distance behind a mirror follows the reflected beam", () => {
    const r = pointAlongBeam(l, t, "m1", 50);
    assert.ok(r);
    near(r.point.x, 112.5);
    near(r.point.y, 112.5);
  });

  it("says when the distance runs past the next component", () => {
    const r = pointAlongBeam(l, t, "m1", 200);
    assert.ok(r);
    assert.equal(r.beyondLeg, true);
    near(r.point.y, 262.5);
  });

  it("has nothing behind a component the beam ends on, or for a silly distance", () => {
    assert.equal(pointAlongBeam(l, t, "det1", 10), null);
    assert.equal(pointAlongBeam(l, t, "m1", -1), null);
    assert.equal(pointAlongBeam(l, t, "m1", Number.NaN), null);
    assert.equal(pointAlongBeam(l, t, "nope", 10), null);
  });

  it("legs behind a beam splitter come strongest first", () => {
    const bs = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("beam_splitter", "bs1", 112.5, 62.5, 135, { props: { reflect_fraction: 0.3 } }),
      comp("detector", "detT", 250, 62.5, 180),
      comp("detector", "detR", 112.5, 200, 270),
    ]);
    const legs = legsAfter(bs, traceLayoutDetailed(bs), "bs1");
    assert.deepEqual(
      legs.map((x) => x.branch),
      ["0.t", "0.r"],
    );
    const second = pointAlongBeam(bs, traceLayoutDetailed(bs), "bs1", 10, 1);
    assert.ok(second);
    near(second.point.y, 72.5);
  });

  it("placing a component there: position, and optionally the axis along the beam", () => {
    const plain = placeAlongBeam(l, t, "m1", "las1", 20);
    assert.ok(plain);
    assert.equal(plain.layout.components[1]?.x_mm, 70);
    assert.equal(plain.layout.components[1]?.angle_deg, 135, "angle untouched");

    const lens = layoutOf([...l.components, comp("lens", "len1", 0, 0, 77)]);
    const withAxis = placeAlongBeam(lens, traceLayoutDetailed(lens), "len1", "m1", 40, { alignAxis: true });
    assert.ok(withAxis);
    const c = withAxis.layout.components.find((x) => x.id === "len1")!;
    assert.equal(c.x_mm, 112.5);
    assert.equal(c.y_mm, 102.5);
    assert.equal(c.angle_deg, 90, "a transmissive element's axis follows the beam");

    const det = layoutOf([...l.components, comp("detector", "det2", 0, 0, 0)]);
    const facing = placeAlongBeam(det, traceLayoutDetailed(det), "det2", "las1", 20, { alignAxis: true });
    assert.equal(facing?.layout.components.find((x) => x.id === "det2")?.angle_deg, 180, "a detector faces into the beam");
  });

  it("cannot place a component after itself", () => {
    assert.equal(placeAlongBeam(l, t, "m1", "m1", 10), null);
    assert.equal(placeAlongBeam(l, t, "nope", "las1", 10), null);
  });

  it("knows which types have an axis that can follow the beam", () => {
    assert.equal(axisAngleFor(comp("lens", "a", 0, 0, 0), { x: 0, y: 1 }), 90);
    assert.equal(axisAngleFor(comp("detector", "a", 0, 0, 0), { x: 0, y: 1 }), 270);
    assert.equal(axisAngleFor(comp("mirror", "a", 0, 0, 0), { x: 0, y: 1 }), null);
  });
});

describe("measuring", () => {
  it("two points", () => {
    const m = measure({ x: 0, y: 0 }, { x: 3, y: 4 });
    assert.equal(m.distance, 5);
    assert.equal(m.dx, 3);
    assert.equal(m.dy, 4);
    near(m.angle_deg, 53.13010235415598, 1e-9);
    assert.equal(measure({ x: 5, y: 5 }, { x: 5, y: 5 }).angle_deg, 0);
  });

  it("the path along the beam between two components", () => {
    const l = foldLayout();
    const t = traceLayoutDetailed(l);
    near(beamPathBetween(l, t, "las1", "det1")!.mm, 200);
    near(beamPathBetween(l, t, "las1", "m1")!.mm, 62.5);
    near(beamPathBetween(l, t, "m1", "det1")!.mm, 137.5);
    assert.equal(beamPathBetween(l, t, "det1", "m1"), null, "the beam does not run backwards");
    assert.equal(beamPathBetween(l, t, "m1", "m1"), null);
    assert.equal(beamPathBetween(l, t, "nope", "m1"), null);
  });

  it("follows a branch of a beam splitter, not the other one", () => {
    const bs = layoutOf([
      comp("laser", "las1", 50, 62.5, 0, { props: { wavelength_nm: 532 } }),
      comp("beam_splitter", "bs1", 112.5, 62.5, 135, { props: { reflect_fraction: 0.3 } }),
      comp("detector", "detT", 250, 62.5, 180),
      comp("detector", "detR", 112.5, 200, 270),
    ]);
    const t = traceLayoutDetailed(bs);
    const r = beamPathBetween(bs, t, "las1", "detR");
    near(r!.mm, 200);
    assert.equal(r!.branch, "0.r");
    near(beamPathBetween(bs, t, "bs1", "detT")!.mm, 137.5);
    assert.equal(beamPathBetween(bs, t, "detT", "detR"), null);
  });
});
