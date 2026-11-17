// ════════════════════════════════════════════════════════════════════════════
// Pure-logic tests for src/lib/scanMapGeometry.ts — the conversions behind the
// cursor readout, the ruler, clicking a thumbnail, badge placement and focus.
//
//     cd frontend && npm run test:unit
//
// Every function here turns a pixel into a claim about the surface ("the
// cursor is at x = 123.4 nm", "these two points are 37 nm apart", "you clicked
// Au111_003.sxm"). A sign error or a zoom applied twice produces a confident,
// wrong number — nothing throws — so each conversion is pinned.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  MAX_ZOOM,
  clampZoom,
  contrastRatio,
  coordDecimals,
  dotOutline,
  fitView,
  fmtMeasurement,
  fmtSignedNm,
  footprintsAt,
  gridTicks,
  inFootprint,
  layoutBadges,
  measure,
  screenToStage,
  stageToWorld,
  textWidthPx,
  visibleWorld,
  withAlpha,
  worldToStage,
  zoomToward,
} from "../src/lib/scanMapGeometry.ts";
import { stageToScreen } from "../src/lib/scanMapView.ts";

const nm = (v: number) => v * 1e-9;
const close = (a: number, b: number, eps = 1e-9) => Math.abs(a - b) <= eps * Math.max(1, Math.abs(b));

const FIT = fitView({ minX: nm(-500), maxX: nm(500), minY: nm(-300), maxY: nm(300) }, 1200, 900, 44);

describe("fitView / worldToStage / stageToWorld", () => {
  it("round-trips a world point through the stage", () => {
    for (const [x, y] of [[0, 0], [nm(123.4), nm(-56.7)], [nm(-500), nm(300)]]) {
      const p = worldToStage(FIT, x!, y!);
      const back = stageToWorld(FIT, p.x, p.y);
      assert.ok(close(back.x, x!, 1e-12) && close(back.y, y!, 1e-12), `${x},${y} → ${back.x},${back.y}`);
    }
  });

  it("puts +y UP on the canvas (lab convention)", () => {
    const lo = worldToStage(FIT, 0, nm(-100));
    const hi = worldToStage(FIT, 0, nm(100));
    assert.ok(hi.y < lo.y, "a larger y must be drawn higher, i.e. at a smaller pixel row");
  });

  it("keeps x and y at the same scale so footprints keep their shape", () => {
    const a = worldToStage(FIT, 0, 0);
    const b = worldToStage(FIT, nm(100), nm(100));
    assert.ok(close(b.x - a.x, a.y - b.y, 1e-9));
  });

  it("gives a single point a ~100 nm window instead of a zero span", () => {
    const v = fitView({ minX: 0, maxX: 0, minY: 0, maxY: 0 }, 600, 600, 44);
    assert.ok(Number.isFinite(v.scale) && v.scale > 0);
    assert.ok(v.maxX - v.minX > nm(100) && v.maxX - v.minX < nm(200));
  });
});

describe("screen ↔ stage under zoom", () => {
  it("inverts stageToScreen exactly", () => {
    const zoom = { k: 7.5, x: -1234, y: 87 };
    const p = { x: 321, y: 654 };
    const s = stageToScreen(p, zoom);
    const back = screenToStage(s, zoom);
    assert.ok(close(back.x, p.x) && close(back.y, p.y));
  });

  it("is the identity at 1×", () => {
    assert.deepEqual(screenToStage({ x: 10, y: 20 }, { k: 1, x: 0, y: 0 }), { x: 10, y: 20 });
  });

  it("reports the visible world box shrinking as the operator zooms in", () => {
    const all = visibleWorld(FIT, { k: 1, x: 0, y: 0 }, 1200, 900);
    const z = visibleWorld(FIT, { k: 4, x: -1800, y: -1350 }, 1200, 900);
    assert.ok(close((all.maxX - all.minX) / (z.maxX - z.minX), 4, 1e-9));
    assert.ok(z.minY < z.maxY, "min/max must come out ordered despite the Y flip");
  });
});

describe("gridTicks", () => {
  it("lands on exact multiples, without accumulated float drift", () => {
    // Repeated `+= 0.1` reaches 0.7999999999999999 at the 8th line; i·step does not drift.
    const t = gridTicks(0, 1, 0.1);
    assert.equal(t.length, 11);
    t.forEach((v, i) => assert.equal(v, i * 0.1 + 0));
    assert.deepEqual(gridTicks(-1, 1, 0.5), [-1, -0.5, 0, 0.5, 1]);
  });

  it("never yields −0 (it would print as a stray minus)", () => {
    assert.ok(gridTicks(0, 0.3, 0.1).every((v) => !Object.is(v, -0)));
  });

  it("only covers the range it is given (the visible one)", () => {
    // At high zoom the step is tiny; walking the whole fitted extent meant
    // hundreds of off-canvas lines per wheel tick.
    const t = gridTicks(100, 105, 1);
    assert.deepEqual(t, [100, 101, 102, 103, 104, 105]);
  });

  it("refuses to generate an unbounded number of lines", () => {
    assert.deepEqual(gridTicks(0, 1e6, 0.001), []);
    assert.deepEqual(gridTicks(0, 1, 0), []);
    assert.deepEqual(gridTicks(Number.NaN, 1, 0.1), []);
  });
});

// ── clicking the underlay ───────────────────────────────────────────────────

describe("inFootprint", () => {
  it("is a plain box test with no rotation", () => {
    assert.equal(inFootprint(0.9, 0.4, 0, 0, 2, 1), true);
    assert.equal(inFootprint(1.1, 0, 0, 0, 2, 1), false);
    assert.equal(inFootprint(0, 0.6, 0, 0, 2, 1), false);
  });

  it("rotates counter-clockwise, the way the canvas draws angle_deg", () => {
    // A 2×1 box turned 90° CCW stands 1 wide and 2 tall.
    assert.equal(inFootprint(0, 0.9, 0, 0, 2, 1, 90), true);
    assert.equal(inFootprint(0.9, 0, 0, 0, 2, 1, 90), false);
    // 45°: the corner direction (1,1) is now along the long axis.
    assert.equal(inFootprint(0.6, 0.6, 0, 0, 2, 0.2, 45), true);
    assert.equal(inFootprint(0.6, -0.6, 0, 0, 2, 0.2, 45), false);
  });

  it("says no to garbage instead of yes", () => {
    assert.equal(inFootprint(Number.NaN, 0, 0, 0, 1, 1), false);
  });
});

describe("footprintsAt", () => {
  const img = (name: string, cx: number, w = nm(100)) => ({
    name, center_x_m: cx, center_y_m: 0, width_m: w, height_m: w, angle_deg: 0,
  });

  it("returns what is under the point, the one drawn last first", () => {
    // The underlay is drawn oldest-first, so the last match is on top.
    const list = [img("old", 0), img("mid", nm(20)), img("far", nm(500)), img("new", nm(10))];
    assert.deepEqual(footprintsAt(list, nm(15), 0).map((i) => i.name), ["new", "mid", "old"]);
    assert.deepEqual(footprintsAt(list, nm(500), 0).map((i) => i.name), ["far"]);
    assert.deepEqual(footprintsAt(list, nm(5000), 0), []);
  });

  it("skips an image with no placement", () => {
    assert.deepEqual(footprintsAt([{ name: "x", center_x_m: null, center_y_m: 0, width_m: 1, height_m: 1 }], 0, 0), []);
  });
});

// ── the readout and the ruler ───────────────────────────────────────────────

describe("coordDecimals", () => {
  it("shows about one screen pixel of precision, more as the map zooms in", () => {
    assert.equal(coordDecimals(3), 0); // 3 nm per pixel
    assert.equal(coordDecimals(0.9), 1);
    assert.equal(coordDecimals(0.05), 2);
    assert.equal(coordDecimals(0.004), 3);
    assert.ok(coordDecimals(0.0001) <= 4, "capped");
  });

  it("falls back to one decimal on nonsense", () => {
    assert.equal(coordDecimals(0), 1);
    assert.equal(coordDecimals(Number.NaN), 1);
  });
});

describe("fmtSignedNm", () => {
  it("uses a real minus sign", () => {
    assert.equal(fmtSignedNm(-56.74, 1), "−56.7");
    assert.equal(fmtSignedNm(123.44, 1), "123.4");
  });

  it("never prints −0", () => {
    assert.equal(fmtSignedNm(-0.04, 1), "0.0");
    assert.equal(fmtSignedNm(-0, 0), "0");
  });

  it("returns empty rather than NaN", () => {
    assert.equal(fmtSignedNm(Number.NaN, 1), "");
  });
});

describe("measure", () => {
  it("is a 3-4-5 triangle", () => {
    const m = measure({ x: nm(10), y: nm(20) }, { x: nm(40), y: nm(-20) });
    assert.ok(close(m.d, nm(50)));
    assert.ok(close(m.dx, nm(30)));
    assert.ok(close(m.dy, nm(-40)), "Δy is B − A in lab coordinates, +y up");
  });

  it("is zero between a point and itself", () => {
    assert.equal(measure({ x: 1, y: 2 }, { x: 1, y: 2 }).d, 0);
  });

  it("formats d with both components, in nm, with real minus signs", () => {
    const m = measure({ x: 0, y: 0 }, { x: nm(100), y: nm(-72.3) });
    assert.equal(fmtMeasurement(m, 1), "d = 123.4 nm (Δx 100.0 nm, Δy −72.3 nm)");
    assert.equal(fmtMeasurement(m, 1, true), "d = 123.4 nm（Δx 100.0 nm，Δy −72.3 nm）");
  });
});

// ── plan badges ─────────────────────────────────────────────────────────────

describe("layoutBadges", () => {
  it("leaves badges that do not touch exactly where they were", () => {
    const a = [{ x: 0, y: 0 }, { x: 100, y: 0 }, { x: 0, y: 100 }];
    assert.deepEqual(layoutBadges(a, 7), a);
  });

  it("pulls apart badges that would sit on one another", () => {
    // Three planned STS points 4 nm apart at ~1 px/nm.
    const a = [{ x: 0, y: 0 }, { x: 4, y: 2 }, { x: 8, y: 4 }];
    const out = layoutBadges(a, 7);
    for (let i = 0; i < out.length; i++) {
      for (let j = i + 1; j < out.length; j++) {
        const d = Math.hypot(out[i]!.x - out[j]!.x, out[i]!.y - out[j]!.y);
        assert.ok(d >= 14, `badges ${i + 1} and ${j + 1} overlap (${d.toFixed(1)} px apart)`);
      }
    }
  });

  it("never moves an earlier badge to make room for a later one", () => {
    const out = layoutBadges([{ x: 5, y: 5 }, { x: 5, y: 5 }], 7);
    assert.deepEqual(out[0], { x: 5, y: 5 });
  });

  it("keeps a moved badge near its anchor", () => {
    const out = layoutBadges([{ x: 0, y: 0 }, { x: 0, y: 0 }], 7);
    assert.ok(Math.hypot(out[1]!.x, out[1]!.y) <= 4 * 16 + 1e-9);
  });
});

// ── focusing ────────────────────────────────────────────────────────────────

describe("zoomToward", () => {
  it("centres the target on the canvas", () => {
    const z = zoomToward({ x: 300, y: 200 }, 20, 1000, 800);
    const s = stageToScreen({ x: 300, y: 200 }, z);
    assert.ok(close(s.x, 500) && close(s.y, 400));
  });

  it("zooms a small footprint in, and never past the ceiling or below 1×", () => {
    assert.ok(zoomToward({ x: 0, y: 0 }, 50, 1000, 800).k > 1);
    assert.equal(zoomToward({ x: 0, y: 0 }, 0.001, 1000, 800).k, MAX_ZOOM);
    assert.equal(zoomToward({ x: 0, y: 0 }, 5000, 1000, 800).k, 1);
  });

  it("still zooms toward a point, which has no footprint", () => {
    assert.ok(zoomToward({ x: 0, y: 0 }, 0, 1000, 800).k > 1);
  });
});

describe("helpers", () => {
  it("clampZoom keeps the zoom in range and reads nonsense as 1", () => {
    assert.equal(clampZoom(0.5), 1);
    assert.equal(clampZoom(1e9), MAX_ZOOM);
    assert.equal(clampZoom(Number.NaN), 1);
  });

  it("withAlpha turns a hex colour into rgba", () => {
    assert.equal(withAlpha("#38bdf8", 0.1), "rgba(56,189,248,0.1)");
    assert.equal(withAlpha("not-a-colour", 0.1), "not-a-colour");
  });

  it("textWidthPx counts CJK as wider than Latin", () => {
    assert.ok(textWidthPx("扫描", 10) > textWidthPx("ab", 10));
    assert.equal(textWidthPx("", 10), 0);
  });
});

describe("dot outlines", () => {
  const DARK_BG = "#0b1017";
  const LIGHT_BG = "#f8fafc";
  const DARK_INK = "#f8fafc";
  const LIGHT_INK = "#0f172a";

  it("computes WCAG contrast", () => {
    assert.ok(Math.abs(contrastRatio("#000000", "#ffffff")! - 21) < 1e-9);
    assert.equal(contrastRatio("#123456", "#123456"), 1);
    assert.equal(contrastRatio("nope", "#ffffff"), null);
  });

  it("rings the crash red on the dark map, where it nearly vanishes", () => {
    assert.equal(dotOutline("#991b1b", DARK_BG, DARK_INK), DARK_INK);
  });

  it("rings STS green and pulse amber on the light map", () => {
    assert.equal(dotOutline("#22c55e", LIGHT_BG, LIGHT_INK), LIGHT_INK);
    assert.equal(dotOutline("#f59e0b", LIGHT_BG, LIGHT_INK), LIGHT_INK);
  });

  it("keeps a background halo where the colour already stands out", () => {
    assert.equal(dotOutline("#22c55e", DARK_BG, DARK_INK), DARK_BG);
    assert.equal(dotOutline("#3b82f6", DARK_BG, DARK_INK), DARK_BG);
  });
});
