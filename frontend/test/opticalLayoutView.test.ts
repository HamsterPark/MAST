// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/view.ts and colors.ts — the screen mapping every drag
// and every wheel tick goes through, and how a beam is painted.
//
//     cd frontend && npm run test:unit
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  MAX_K,
  MIN_K,
  centerOn,
  fitView,
  formatLength,
  holesVisible,
  minorGridVisible,
  niceStep,
  panBy,
  tickValues,
  toScreen,
  toWorld,
  visibleBounds,
  zoomAt,
  type View,
} from "../src/lib/opticalLayout/view.ts";
import {
  FIXED_COLORS,
  beamStyle,
  spectralBand,
  relativeLuminance,
  toHex,
  visibleRgb,
  wavelengthColor,
  wavelengthLabel,
} from "../src/lib/opticalLayout/colors.ts";

const near = (a: number, b: number, tol = 1e-9) => assert.ok(Math.abs(a - b) <= tol, `${a} is not within ${tol} of ${b}`);

describe("platform to screen", () => {
  const v: View = { k: 2, ox: 100, oy: 500 };

  it("flips y: up on the table is up on the screen", () => {
    assert.deepEqual(toScreen(v, { x: 0, y: 0 }), { x: 100, y: 500 });
    assert.deepEqual(toScreen(v, { x: 10, y: 20 }), { x: 120, y: 460 });
  });

  it("converts back", () => {
    for (const p of [{ x: 0, y: 0 }, { x: 12.5, y: -7 }, { x: 1187.5, y: 887.5 }]) {
      const s = toScreen(v, p);
      const w = toWorld(v, s.x, s.y);
      near(w.x, p.x);
      near(w.y, p.y);
    }
  });
});

describe("zoom and pan", () => {
  it("zoom keeps the point under the cursor where it is", () => {
    const v: View = { k: 1, ox: 40, oy: 300 };
    const before = toWorld(v, 250, 120);
    const z = zoomAt(v, 1.25, 250, 120);
    assert.equal(z.k, 1.25);
    const after = toWorld(z, 250, 120);
    near(after.x, before.x);
    near(after.y, before.y);
    const out = zoomAt(z, 1 / 1.25, 250, 120);
    near(out.k, 1);
    near(out.ox, v.ox, 1e-9);
    near(out.oy, v.oy, 1e-9);
  });

  it("zoom stops at the limits and then changes nothing", () => {
    const v: View = { k: MAX_K, ox: 0, oy: 0 };
    assert.equal(zoomAt(v, 2, 10, 10), v);
    const z = zoomAt({ k: MAX_K / 1.5, ox: 0, oy: 0 }, 4, 0, 0);
    assert.equal(z.k, MAX_K);
    const tiny = zoomAt({ k: MIN_K * 1.1, ox: 0, oy: 0 }, 0.01, 0, 0);
    assert.equal(tiny.k, MIN_K);
  });

  it("pan moves the origin and nothing else; a zero pan is the same view", () => {
    const v: View = { k: 3, ox: 10, oy: 20 };
    assert.deepEqual(panBy(v, 5, -7), { k: 3, ox: 15, oy: 13 });
    assert.equal(panBy(v, 0, 0), v);
  });

  it("fit centres the bounds with room round them", () => {
    const v = fitView({ minX: 0, minY: 0, maxX: 1200, maxY: 900 }, 1000, 800, 20);
    // limited by the width: (1000 - 40) / 1200 = 0.8
    near(v.k, 0.8);
    const c = toScreen(v, { x: 600, y: 450 });
    near(c.x, 500);
    near(c.y, 400);
    const tall = fitView({ minX: 0, minY: 0, maxX: 100, maxY: 1000 }, 1000, 500, 0);
    near(tall.k, 0.5);
  });

  it("fit survives a tiny or empty viewport", () => {
    const v = fitView({ minX: 0, minY: 0, maxX: 0, maxY: 0 }, 0, 0);
    assert.ok(Number.isFinite(v.k) && v.k >= MIN_K && v.k <= MAX_K);
  });

  it("the visible rectangle is what a viewport shows", () => {
    const v: View = { k: 2, ox: 100, oy: 500 };
    const b = visibleBounds(v, 400, 300);
    near(b.minX, -50);
    near(b.maxX, 150);
    near(b.minY, 100);
    near(b.maxY, 250);
  });
});

describe("centring on a point", () => {
  const v: View = { k: 2, ox: 10, oy: 500 };

  it("puts the point in the middle of the viewport", () => {
    const c = centerOn(v, { x: 100, y: 40 }, 800, 600);
    const s = toScreen(c, { x: 100, y: 40 });
    near(s.x, 400);
    near(s.y, 300);
    assert.equal(c.k, 2, "the zoom is kept");
  });

  it("zooms in to at least the asked scale, about that point, and never zooms out", () => {
    const zoomed = centerOn(v, { x: 100, y: 40 }, 800, 600, 8);
    assert.equal(zoomed.k, 8);
    const s = toScreen(zoomed, { x: 100, y: 40 });
    near(s.x, 400);
    near(s.y, 300);
    assert.equal(centerOn({ k: 12, ox: 0, oy: 0 }, { x: 1, y: 1 }, 800, 600, 8).k, 12, "already closer than that");
  });

  it("stays within the zoom limits", () => {
    assert.equal(centerOn(v, { x: 0, y: 0 }, 800, 600, 1000).k, MAX_K);
  });
});

describe("rulers and thresholds", () => {
  it("steps are 1, 2 or 5 times a power of ten", () => {
    assert.equal(niceStep(1200, 12), 100);
    assert.equal(niceStep(1000, 5), 200);
    assert.equal(niceStep(10, 10), 1);
    assert.equal(niceStep(0.3, 6), 0.05);
    assert.equal(niceStep(0, 5), 1);
    for (const range of [3, 17, 140, 950, 4000]) {
      const s = niceStep(range, 8);
      const mant = s / 10 ** Math.floor(Math.log10(s));
      assert.ok([1, 2, 5].some((m) => Math.abs(m - mant) < 1e-9), `${s}`);
    }
  });

  it("ticks are multiples of the step inside the range", () => {
    assert.deepEqual(tickValues(0, 500, 100), [0, 100, 200, 300, 400, 500]);
    assert.deepEqual(tickValues(-250, 250, 250), [-250, 0, 250]);
    assert.deepEqual(tickValues(10, 90, 100), []);
    assert.deepEqual(tickValues(0, 1, 0.25), [0, 0.25, 0.5, 0.75, 1]);
    assert.deepEqual(tickValues(5, 0, 1), []);
    assert.deepEqual(tickValues(0, 1e9, 1), [], "refuses to build a million ticks");
  });

  it("holes are drawn only while they are far enough apart", () => {
    assert.equal(holesVisible(25, 0.2), false);
    assert.equal(holesVisible(25, 0.3), true);
    assert.equal(minorGridVisible(25, 0.1), false);
    assert.equal(minorGridVisible(25, 0.2), true);
  });

  it("lengths read well", () => {
    assert.equal(formatLength(0.35), "0.35 mm");
    assert.equal(formatLength(137.5), "137.5 mm");
    assert.equal(formatLength(12500), "12.50 m");
  });
});

describe("beam colours", () => {
  it("name the band of a wavelength", () => {
    assert.equal(spectralBand(532), "visible");
    assert.equal(spectralBand(380), "visible");
    assert.equal(spectralBand(780), "visible");
    assert.equal(spectralBand(355), "uv");
    assert.equal(spectralBand(1064), "ir");
    assert.equal(spectralBand(null), "unknown");
    assert.equal(spectralBand(undefined), "unknown");
    assert.equal(spectralBand(0), "unknown");
    assert.equal(spectralBand(Number.NaN), "unknown");
  });

  it("visible colours follow the spectrum", () => {
    const [r1, g1, b1] = visibleRgb(450);
    assert.ok(b1 > r1 && b1 > g1, "450 nm is blue");
    const [r2, g2, b2] = visibleRgb(532);
    assert.ok(g2 > r2 && g2 > b2, "532 nm is green");
    const [r3, g3, b3] = visibleRgb(650);
    assert.ok(r3 > g3 && r3 > b3, "650 nm is red");
    const [r4, g4] = visibleRgb(590);
    assert.ok(r4 > 0.9 && g4 > 0.4 && g4 < 0.9, "590 nm is orange / yellow");
  });

  it("the colour is a hex string, and differs between wavelengths", () => {
    const colors = [450, 500, 532, 590, 650].map((w) => wavelengthColor(w, "dark"));
    for (const c of colors) assert.match(c, /^#[0-9a-f]{6}$/);
    assert.equal(new Set(colors).size, colors.length);
    assert.equal(toHex([1, 0, 0.5]), "#ff0080");
  });

  it("is visible on the page and on the table of each theme, for every visible wavelength", () => {
    // the real backgrounds, from src/index.css: --mast-bg and --mast-panel of each theme
    const BACKGROUNDS = {
      light: ["#f4f6f9", "#ffffff"],
      dark: ["#0a0e13", "#141a22"],
    } as const;
    const rgb = (hex: string): [number, number, number] => {
      const n = parseInt(hex.slice(1), 16);
      return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
    };
    const contrast = (a: string, b: string) => {
      const [hi, lo] = [relativeLuminance(rgb(a)), relativeLuminance(rgb(b))].sort((x, y) => y - x);
      return (hi! + 0.05) / (lo! + 0.05);
    };
    for (const theme of ["light", "dark"] as const) {
      for (let w = 380; w <= 780; w += 10) {
        for (const bg of BACKGROUNDS[theme]) {
          const c = contrast(wavelengthColor(w, theme), bg);
          assert.ok(c >= 3, `${w} nm on ${theme} ${bg}: ${c.toFixed(2)}:1`);
        }
      }
      for (const band of ["uv", "ir", "unknown"] as const) {
        for (const bg of BACKGROUNDS[theme]) {
          const c = contrast(FIXED_COLORS[band][theme], bg);
          assert.ok(c >= 3, `fixed ${band} on ${theme} ${bg}: ${c.toFixed(2)}:1`);
        }
      }
    }
  });

  it("keeps the hue: yellow stays yellowish (red and green high, blue low) on the light page", () => {
    const n = parseInt(wavelengthColor(580, "light").slice(1), 16);
    const r = (n >> 16) & 255;
    const g = (n >> 8) & 255;
    const b = n & 255;
    assert.ok(r > 100 && g > 100 && b < 40, `#${n.toString(16)}`);
  });

  it("infrared and ultraviolet get a fixed colour and a dashed line", () => {
    assert.equal(wavelengthColor(1064, "dark"), FIXED_COLORS.ir.dark);
    assert.equal(wavelengthColor(1064, "light"), FIXED_COLORS.ir.light);
    assert.equal(wavelengthColor(355, "dark"), FIXED_COLORS.uv.dark);
    assert.equal(wavelengthColor(null, "light"), FIXED_COLORS.unknown.light);
    assert.ok(beamStyle(1064, 1, "dark").dash);
    assert.ok(beamStyle(355, 1, "dark").dash);
    assert.notEqual(beamStyle(1064, 1, "dark").dash, beamStyle(355, 1, "dark").dash, "the two dash patterns differ");
    assert.equal(beamStyle(532, 1, "dark").dash, null);
    assert.equal(beamStyle(null, 1, "dark").dash, null);
  });

  it("a weaker branch is thinner and fainter", () => {
    const full = beamStyle(532, 1, "dark");
    const half = beamStyle(532, 0.5, "dark");
    const faint = beamStyle(532, 0.01, "dark");
    assert.ok(full.width > half.width && half.width > faint.width);
    assert.ok(full.opacity > half.opacity && half.opacity > faint.opacity);
    assert.equal(full.opacity, 1);
    assert.ok(faint.width >= 1 && faint.opacity > 0.3, "still visible");
    assert.deepEqual(beamStyle(532, Number.NaN, "dark"), full, "an unreadable fraction counts as full");
    assert.equal(beamStyle(532, 5, "dark").width, full.width, "clamped");
  });

  it("the label for a ray without a wavelength is a dash", () => {
    assert.equal(wavelengthLabel(532), "532 nm");
    assert.equal(wavelengthLabel(532.5), "532.5 nm");
    assert.equal(wavelengthLabel(null), "—");
  });
});
