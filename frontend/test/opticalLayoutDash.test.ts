// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/dash.ts: dash patterns drawn as separate dashes, for the figure export
// (viewers such as MuPDF ignore stroke-dasharray and would draw a "no light" beam solid).
//
//     cd frontend && npm run test:unit
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { MAX_DASHES, dashedPath, parseDash, polylineOf } from "../src/lib/opticalLayout/dash.ts";

/** The dashes of path data, as [x0, y0, x1, y1, ...] per subpath. */
function subpaths(d: string): number[][] {
  return d
    .split("M")
    .filter(Boolean)
    .map((s) => s.split(/[L\s]+/).filter(Boolean).map(Number));
}
const len = (p: number[]) => {
  let L = 0;
  for (let i = 2; i < p.length; i += 2) L += Math.hypot(p[i]! - p[i - 2]!, p[i + 1]! - p[i - 1]!);
  return L;
};

describe("a dash pattern", () => {
  it("is read as SVG reads it: an odd count repeated, nonsense refused", () => {
    assert.deepEqual(parseDash("1.5 3.5"), [1.5, 3.5]);
    assert.deepEqual(parseDash("9,5"), [9, 5]);
    assert.deepEqual(parseDash("3"), [3, 3]);
    assert.deepEqual(parseDash("2 1 4"), [2, 1, 4, 2, 1, 4]);
    for (const bad of [null, undefined, "", "0 0", "a b", "-1 2"]) assert.equal(parseDash(bad), null, String(bad));
  });

  it("is laid along a line as separate dashes of the pattern's length, gaps between", () => {
    const d = dashedPath([{ x: 0, y: 0 }, { x: 100, y: 0 }], [6, 4])!;
    const parts = subpaths(d);
    assert.equal(parts.length, 10);
    for (const [i, p] of parts.entries()) {
      assert.equal(p[0], i * 10, "each dash starts a period further on");
      assert.ok(Math.abs(len(p) - 6) < 1e-9);
    }
  });

  it("the last dash is cut at the end of the line; a dash keeps a corner it runs round", () => {
    const short = subpaths(dashedPath([{ x: 0, y: 0 }, { x: 13, y: 0 }], [6, 4])!);
    assert.equal(short.length, 2);
    assert.ok(Math.abs(len(short[1]!) - 3) < 1e-9, "10 to 13");
    // an L: 8 along x, then up; the second dash (10..16) starts 2 past the corner, the first (0..6) before it;
    // with a dash 0..9 the first one runs round the corner as one subpath with the corner in it
    const corner = subpaths(dashedPath([{ x: 0, y: 0 }, { x: 8, y: 0 }, { x: 8, y: 20 }], [9, 3])!);
    assert.deepEqual(corner[0], [0, 0, 8, 0, 8, 1], "one dash, round the corner");
    assert.ok(Math.abs(len(corner[0]!) - 9) < 1e-9);
    assert.deepEqual(corner[1]!.slice(0, 2), [8, 4]);
  });

  it("is scaled (screen pixels written in millimetres) and rounded as asked", () => {
    const d = dashedPath([{ x: 0, y: 0 }, { x: 1, y: 0 }], [1.5, 1.5], 1 / 7, 4)!;
    const parts = subpaths(d);
    assert.ok(Math.abs(len(parts[0]!) - 1.5 / 7) < 1e-4);
    assert.ok(/^[ML\d. -]+$/.test(d), d);
  });

  it("gives up (null: keep stroke-dasharray) past MAX_DASHES, and draws nothing for a line of no length", () => {
    assert.equal(dashedPath([{ x: 0, y: 0 }, { x: (MAX_DASHES + 10) * 2, y: 0 }], [1, 1]), null);
    assert.equal(dashedPath([{ x: 5, y: 5 }, { x: 5, y: 5 }], [1, 1]), "");
    assert.equal(dashedPath([{ x: 5, y: 5 }], [1, 1]), "");
  });
});

describe("a beam line's points", () => {
  it("are read back from the canvas's path data (absolute M then L), anything else is refused", () => {
    assert.deepEqual(polylineOf("M10 20L30.5 -4.25L40 1e-7"), [
      { x: 10, y: 20 },
      { x: 30.5, y: -4.25 },
      { x: 40, y: 1e-7 },
    ]);
    assert.equal(polylineOf("M0 0L10 0M20 0L30 0"), null, "two subpaths");
    assert.equal(polylineOf("M0 0l10 0"), null, "relative");
    assert.equal(polylineOf("M0 0"), null, "a point");
    assert.equal(polylineOf("M0 0C1 1 2 2 3 3"), null, "a curve");
  });
});
