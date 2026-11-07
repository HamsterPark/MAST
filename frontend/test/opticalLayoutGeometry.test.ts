// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/geometry.ts — the arithmetic every other part of the
// optical layout editor stands on.
//
//     cd frontend && npm run test:unit
//
// The rounding tests pin the one place where TypeScript and Python disagree if written
// naively: `round(x, n)` goes by the exact binary value and takes exact ties to the
// even neighbour, `Math.round(x * 10 ** n) / 10 ** n` does neither. The trace fixtures
// are compared at 1e-6 mm, so a 0.001 difference in the last place is a failure.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  MOVE_STEP_MM,
  ROTATE_STEP_DEG,
  angleDiff,
  angleOf,
  apertureEnds,
  canonicalAngle,
  closestOnSegment,
  dirOf,
  frameOf,
  holeCounts,
  holePosition,
  layoutBounds,
  localToWorld,
  nearestHole,
  pyRound,
  reflect,
  rotateAbout,
  round2,
  round3,
  snapPoint,
  stepSize,
  worldToLocal,
  wrapAngle,
} from "../src/lib/opticalLayout/geometry.ts";
import { comp } from "./helpers/opticalLayout.ts";
import type { TableSpec } from "../src/lib/opticalLayout/types.ts";

const near = (a: number, b: number, tol = 1e-9) => assert.ok(Math.abs(a - b) <= tol, `${a} is not within ${tol} of ${b}`);

const METRIC: TableSpec = { width_mm: 1200, height_mm: 900, hole_pitch_mm: 25, hole_offset_mm: 12.5, grid: "metric" };
const IMPERIAL: TableSpec = { width_mm: 1219.2, height_mm: 914.4, hole_pitch_mm: 25.4, hole_offset_mm: 12.7, grid: "metric" };

describe("pyRound agrees with Python's round()", () => {
  it("goes by the exact binary value, not by the product", () => {
    // Python: round(2.675, 2) == 2.67 (the double is 2.67499999999999982...), round(1.005, 2) == 1.0
    assert.equal(pyRound(2.675, 2), 2.67);
    assert.equal(pyRound(1.005, 2), 1);
    assert.equal(pyRound(1.0005, 3), 1);
    // and the naive way gets the first one wrong, which is why it is not used
    assert.equal(Math.round(2.675 * 100) / 100, 2.68);
  });

  it("takes exact ties to the even neighbour", () => {
    // 0.0625 = 1/16 is exact in binary: Python round(0.0625, 3) == 0.062, round(0.1875, 3) == 0.188
    assert.equal(pyRound(0.0625, 3), 0.062);
    assert.equal(pyRound(0.1875, 3), 0.188);
    assert.equal(pyRound(0.3125, 3), 0.312);
    assert.equal(pyRound(0.4375, 3), 0.438);
    assert.equal(pyRound(2.5, 0), 2);
    assert.equal(pyRound(3.5, 0), 4);
    assert.equal(pyRound(-2.5, 0), -2);
    assert.equal(pyRound(0.5, 0), 0);
  });

  it("rounds ordinary numbers the ordinary way, and keeps the sign of zero", () => {
    assert.equal(pyRound(62.5004, 3), 62.5);
    assert.equal(pyRound(62.5006, 3), 62.501);
    assert.equal(pyRound(-62.5006, 3), -62.501);
    assert.equal(pyRound(112.5, 2), 112.5);
    assert.ok(Object.is(pyRound(-0, 3), -0));
    assert.ok(Object.is(pyRound(-0.0001, 3), -0), "Python gives -0.0 as well");
    assert.equal(round2(0.1 + 0.2), 0.3);
    assert.equal(round3(137.4999999), 137.5);
  });

  it("leaves non-finite numbers alone instead of throwing", () => {
    assert.ok(Number.isNaN(pyRound(Number.NaN, 2)));
    assert.equal(pyRound(Infinity, 2), Infinity);
  });
});

describe("angles", () => {
  it("wrap into [0, 360)", () => {
    assert.equal(wrapAngle(-90), 270);
    assert.equal(wrapAngle(360), 0);
    assert.equal(wrapAngle(720.5), 0.5);
    assert.equal(wrapAngle(-360), 0);
    assert.ok(Object.is(wrapAngle(-0), 0), "never -0");
    assert.equal(wrapAngle(Number.NaN), 0);
  });

  it("are saved at 0.01 degree and wrapped again after rounding", () => {
    assert.equal(canonicalAngle(135), 135);
    assert.equal(canonicalAngle(135.004), 135);
    assert.equal(canonicalAngle(135.006), 135.01);
    // 359.996 rounds to 360.00, which is not in [0, 360)
    assert.equal(canonicalAngle(359.996), 0);
    assert.equal(canonicalAngle(-0.004), 0);
    assert.equal(canonicalAngle(-45), 315);
  });

  it("differ by the short way round", () => {
    assert.equal(angleDiff(10, 350), 20);
    assert.equal(angleDiff(350, 10), -20);
    assert.equal(angleDiff(180, 0), 180);
  });

  it("turn into unit vectors counter-clockwise from +x, and back", () => {
    near(dirOf(0).x, 1);
    near(dirOf(90).y, 1);
    near(dirOf(135).x, -Math.SQRT1_2);
    near(dirOf(135).y, Math.SQRT1_2);
    for (const a of [0, 1, 45, 90, 135, 179.99, 180, 270, 359.99]) near(angleOf(dirOf(a)), a, 1e-9);
  });
});

describe("reflection", () => {
  it("a 45 degree fold turns +x into +y", () => {
    const d = reflect({ x: 1, y: 0 }, dirOf(135));
    near(d.x, 0);
    near(d.y, 1);
  });

  it("does not depend on which way the normal points", () => {
    const a = reflect({ x: 1, y: 0.3 }, dirOf(40));
    const b = reflect({ x: 1, y: 0.3 }, dirOf(220));
    near(a.x, b.x);
    near(a.y, b.y);
  });
});

describe("component frame", () => {
  const c = comp("mirror", "m1", 100, 50, 90, { aperture: 20 });

  it("has its normal along angle_deg and its surface along the local y axis", () => {
    const f = frameOf(c);
    near(f.n.x, 0);
    near(f.n.y, 1);
    near(f.u.x, -1);
    near(f.u.y, 0);
    assert.equal(f.half, 10);
  });

  it("the active surface is centre -/+ u * aperture / 2", () => {
    const [a, b] = apertureEnds(c);
    near(a.x, 110);
    near(a.y, 50);
    near(b.x, 90);
    near(b.y, 50);
  });

  it("local and platform coordinates convert both ways", () => {
    const f = frameOf(comp("mirror", "m2", 10, 20, 30));
    for (const [lx, ly] of [[0, 0], [3, -4], [-7.5, 12]] as const) {
      const w = localToWorld(f, lx, ly);
      const l = worldToLocal(f, w);
      near(l.x, lx);
      near(l.y, ly);
    }
  });

  it("rotates a point about a pivot", () => {
    const p = rotateAbout({ x: 10, y: 0 }, { x: 0, y: 0 }, 90);
    near(p.x, 0);
    near(p.y, 10);
    const q = rotateAbout({ x: 12, y: 5 }, { x: 10, y: 5 }, 180);
    near(q.x, 8);
    near(q.y, 5);
  });

  it("finds the closest point of a segment", () => {
    const r = closestOnSegment({ x: 5, y: 3 }, { x: 0, y: 0 }, { x: 10, y: 0 });
    near(r.point.x, 5);
    near(r.dist, 3);
    const end = closestOnSegment({ x: 15, y: 0 }, { x: 0, y: 0 }, { x: 10, y: 0 });
    near(end.point.x, 10);
    near(end.t, 1);
    const degenerate = closestOnSegment({ x: 1, y: 1 }, { x: 0, y: 0 }, { x: 0, y: 0 });
    near(degenerate.dist, Math.SQRT2);
  });
});

describe("breadboard holes", () => {
  it("count: holes at offset + k * pitch inside the table", () => {
    assert.deepEqual(holeCounts(METRIC), { cols: 48, rows: 36 });
    assert.deepEqual(holeCounts(IMPERIAL), { cols: 48, rows: 36 });
    assert.deepEqual(holeCounts({ ...METRIC, hole_pitch_mm: 0 }), { cols: 0, rows: 0 });
    assert.deepEqual(holeCounts({ ...METRIC, width_mm: 5 }), { cols: 0, rows: 36 });
  });

  it("positions count from 1 at the lower-left hole", () => {
    assert.deepEqual(holePosition(METRIC, 1, 1), { x: 12.5, y: 12.5 });
    assert.deepEqual(holePosition(METRIC, 5, 3), { x: 112.5, y: 62.5 });
    assert.deepEqual(holePosition(IMPERIAL, 3, 2), { x: 63.5, y: 38.1 });
  });

  it("the nearest hole is given as row, column and deviation (point minus hole)", () => {
    const exact = nearestHole(METRIC, { x: 112.5, y: 62.5 });
    assert.deepEqual(exact, { col: 5, row: 3, x: 112.5, y: 62.5, dx: 0, dy: 0, inside: true });
    const off = nearestHole(METRIC, { x: 120, y: 70 });
    assert.equal(off?.col, 5);
    assert.equal(off?.row, 3);
    assert.equal(off?.dx, 7.5);
    assert.equal(off?.dy, 7.5);
    const left = nearestHole(METRIC, { x: 100, y: 55 });
    assert.equal(left?.dx, -12.5);
    assert.equal(left?.dy, -7.5);
  });

  it("clamps to the table and says when the point is outside it", () => {
    const out = nearestHole(METRIC, { x: -80, y: 2000 });
    assert.equal(out?.col, 1);
    assert.equal(out?.row, 36);
    assert.equal(out?.inside, false);
    assert.equal(nearestHole({ ...METRIC, hole_pitch_mm: 0 }, { x: 1, y: 1 }), null);
  });

  it("snaps onto the lattice, which goes on past the table edge", () => {
    assert.deepEqual(snapPoint(METRIC, { x: 118, y: 70 }, "hole"), { x: 112.5, y: 62.5 });
    assert.deepEqual(snapPoint(METRIC, { x: 118, y: 70 }, "half"), { x: 112.5, y: 75 });
    assert.deepEqual(snapPoint(METRIC, { x: 118.4, y: 70.6 }, "mm"), { x: 118, y: 71 });
    assert.deepEqual(snapPoint(METRIC, { x: 118.456, y: 70.614 }, "off"), { x: 118.46, y: 70.61 });
    assert.deepEqual(snapPoint(METRIC, { x: -30, y: -5 }, "hole"), { x: -37.5, y: -12.5 });
    assert.deepEqual(snapPoint(IMPERIAL, { x: 60, y: 40 }, "hole"), { x: 63.5, y: 38.1 });
    assert.deepEqual(snapPoint({ ...METRIC, hole_pitch_mm: 0 }, { x: 1.234, y: 2.345 }, "hole"), { x: 1.23, y: 2.35 });
  });
});

describe("keyboard steps", () => {
  it("move 1 mm, Shift 0.1 mm, Alt 10 mm", () => {
    assert.equal(MOVE_STEP_MM[stepSize({})], 1);
    assert.equal(MOVE_STEP_MM[stepSize({ shift: true })], 0.1);
    assert.equal(MOVE_STEP_MM[stepSize({ alt: true })], 10);
  });

  it("rotate 1 degree, Shift 0.1, Alt 15", () => {
    assert.equal(ROTATE_STEP_DEG[stepSize({})], 1);
    assert.equal(ROTATE_STEP_DEG[stepSize({ shift: true })], 0.1);
    assert.equal(ROTATE_STEP_DEG[stepSize({ alt: true })], 15);
  });

  it("Shift and Alt together give the smaller step", () => {
    assert.equal(stepSize({ shift: true, alt: true }), "fine");
  });
});

describe("bounds", () => {
  it("cover the table and anything placed off it", () => {
    const b = layoutBounds(METRIC, [comp("mirror", "m1", -50, 950, 0, { aperture: 20 })], 10);
    assert.deepEqual(b, { minX: -70, minY: -10, maxX: 1210, maxY: 970 });
    assert.deepEqual(layoutBounds(METRIC, []), { minX: 0, minY: 0, maxX: 1200, maxY: 900 });
  });
});
