// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/exportStyle.ts: the figure export (v2 section 8.5).
//
//     cd frontend && npm run test:unit
//
// The figure is the canvas component in its paper variant; what is pinned here is the part that is
// data: every colour the canvas paints with has a print colour (no var() may survive into a file that
// leaves the page, and none may depend on the operator's theme), the frame holds what is drawn and not
// the empty table, and the scale bar is a round length.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { BEAM_OVERHANG_MM, FRAME_PASSES, PAPER_PALETTE, legendOf, paperColor, paperFileName, paperFrame, rotatedBox, scaleBar } from "../src/lib/opticalLayout/exportStyle.ts";
import { traceLayoutDetailed } from "../src/lib/opticalLayout/trace.ts";
import { DARK_P } from "../src/lib/opticalLayout/colors.ts";
import { comp, layoutOf } from "./helpers/opticalLayout.ts";
import { resolveCssVars } from "../src/lib/opticalLayout/exportSvg.ts";
import { glyphShapes, shapeBounds } from "../src/lib/opticalLayout/glyphs.ts";
import { newComponent } from "../src/lib/opticalLayout/edit.ts";
import { toScreen } from "../src/lib/opticalLayout/view.ts";
import type { LayoutComponent, TableSpec } from "../src/lib/opticalLayout/types.ts";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const COMPONENTS = join(HERE, "..", "src", "components", "opticallayout");

const TABLE: TableSpec = { width_mm: 1200, height_mm: 900, hole_pitch_mm: 25, hole_offset_mm: 12.5, grid: "metric" };
const box = (c: LayoutComponent) => rotatedBox(c, shapeBounds(glyphShapes(c)));

describe("print colours", () => {
  it("every design token the canvas files paint with has one", () => {
    const used = new Set<string>();
    for (const f of readdirSync(COMPONENTS)) {
      if (!/^(LayoutSvg|GlyphShapes)\.tsx$/.test(f)) continue;
      for (const m of readFileSync(join(COMPONENTS, f), "utf8").matchAll(/var\((--mast-[a-z0-9-]+)\)/g)) used.add(m[1]!);
    }
    assert.ok(used.size >= 8, [...used].join(", "));
    for (const name of used) assert.ok(paperColor(name), `${name} has no print colour`);
  });

  it("are plain hex colours, white paper and black ink", () => {
    for (const [name, v] of Object.entries(PAPER_PALETTE)) assert.match(v, /^#[0-9a-f]{6}$/, name);
    assert.equal(paperColor("--mast-bg"), "#ffffff");
    assert.equal(paperColor("--mast-panel"), "#ffffff");
    assert.equal(paperColor("--mast-text"), "#000000");
    assert.equal(paperColor(" --mast-text "), "#000000");
    assert.equal(paperColor("--mast-nope"), null);
  });

  it("leave no var() in the file", () => {
    const svg = '<g stroke="var(--mast-text)" fill="var(--mast-panel)"><rect fill="var(--mast-info)" stroke="var(--mast-unknown)"/></g>';
    const out = resolveCssVars(svg, (n) => paperColor(n));
    assert.ok(!out.includes("var("), out);
    assert.match(out, /stroke="#000000" fill="#ffffff"/);
  });
});

describe("the frame of the figure", () => {
  const comps = [newComponent("mirror", "m1", { x: 300, y: 200 }, { angle_deg: 135 }), newComponent("lens", "l1", { x: 500, y: 200 }, { angle_deg: 0 })];

  it("holds the drawings and the beams, not the empty table", () => {
    const f = paperFrame({ table: TABLE, components: comps, texts: [], beamPoints: [{ x: 100, y: 200 }, { x: 300, y: 200 }, { x: 300, y: 400 }], componentBox: box, targetWidth: 1000, pad: 20, footer: 30 });
    assert.ok(f.bounds.minX <= 100 + 1e-9 && f.bounds.minX > 50, JSON.stringify(f.bounds));
    assert.ok(f.bounds.maxY >= 400 - 1e-9 && f.bounds.maxY < 450, JSON.stringify(f.bounds));
    assert.ok(f.bounds.maxX - f.bounds.minX < TABLE.width_mm / 2, "not the whole table");
    // every component's drawing is inside the picture
    for (const c of comps) {
      const b = box(c);
      for (const p of [toScreen(f.view, { x: b.minX, y: b.minY }), toScreen(f.view, { x: b.maxX, y: b.maxY })]) {
        assert.ok(p.x >= 0 && p.x <= f.width && p.y >= 0 && p.y <= f.height, JSON.stringify(p));
      }
    }
  });

  it("is scaled to the target width, with the pad round it and the footer below", () => {
    const f = paperFrame({ table: TABLE, components: comps, texts: [], beamPoints: [], componentBox: box, targetWidth: 800, pad: 20, footer: 30 });
    const w = f.bounds.maxX - f.bounds.minX;
    assert.ok(Math.abs(w * f.view.k - 800) < 1e-6 || f.height <= 1500);
    assert.equal(f.width, Math.ceil(w * f.view.k + 40));
    assert.equal(f.height, Math.ceil((f.bounds.maxY - f.bounds.minY) * f.view.k + 40 + 30));
    const tl = toScreen(f.view, { x: f.bounds.minX, y: f.bounds.maxY });
    assert.ok(Math.abs(tl.x - 20) < 1e-9 && Math.abs(tl.y - 20) < 1e-9);
  });

  it("a tall drawing is limited by the height instead", () => {
    const tall = [newComponent("mirror", "a", { x: 100, y: 0 }), newComponent("mirror", "b", { x: 100, y: 3000 })];
    const f = paperFrame({ table: { ...TABLE, height_mm: 4000 }, components: tall, texts: [], beamPoints: [], componentBox: box, targetWidth: 1000, maxHeight: 900, footer: 30, pad: 0 });
    assert.ok(f.height <= 900 + 1, String(f.height));
  });

  it("a beam that leaves the table is cut a little past the edge, not followed for its metre", () => {
    const f = paperFrame({ table: TABLE, components: comps, texts: [], beamPoints: [{ x: 300, y: 200 }, { x: 2200, y: 200 }], componentBox: box });
    assert.ok(Math.abs(f.bounds.maxX - (TABLE.width_mm + BEAM_OVERHANG_MM)) < 1e-9, JSON.stringify(f.bounds));
  });

  it("an empty layout frames the whole table; a single point still gets an area", () => {
    const empty = paperFrame({ table: TABLE, components: [], texts: [], beamPoints: [], componentBox: box });
    assert.deepEqual([empty.bounds.minX, empty.bounds.maxX, empty.bounds.minY, empty.bounds.maxY], [0, 1200, 0, 900]);
    const one = paperFrame({ table: TABLE, components: [], texts: [{ id: "t1", x_mm: 10, y_mm: 10, text: "x", size: 1 }], beamPoints: [], componentBox: box });
    assert.ok(one.bounds.maxX - one.bounds.minX >= 20 && Number.isFinite(one.view.k));
  });

  it("holds the texts too: a long name at the right edge and a note running right of its anchor are not cut off", () => {
    // a name 250 px wide starting 9 px right of the last component, and a note 300 px wide from its anchor: pixel
    // sizes, so how far they reach in millimetres depends on the scale the frame settles on
    const edge = newComponent("detector", "pm1", { x: 600, y: 200 }, { angle_deg: 180 });
    const all = [...comps, edge];
    const note = { id: "t1", x_mm: 600, y_mm: 140, text: "a note", size: 1 };
    const textBoxes = (v: { k: number; ox: number; oy: number }) => {
      const a = toScreen(v, { x: edge.x_mm, y: edge.y_mm });
      const n = toScreen(v, { x: note.x_mm, y: note.y_mm });
      return [
        { x: a.x + 9, y: a.y + 20, w: 250, h: 18 },
        { x: n.x, y: n.y - 12, w: 300, h: 15 },
        { x: a.x - 60, y: a.y - 200, w: 40, h: 14 }, // above the top of the drawing
      ];
    };
    const plain = paperFrame({ table: TABLE, components: all, texts: [note], beamPoints: [], componentBox: box, targetWidth: 1100, pad: 28, footer: 34 });
    const framed = paperFrame({ table: TABLE, components: all, texts: [note], beamPoints: [], componentBox: box, screenBoxes: textBoxes, targetWidth: 1100, pad: 28, footer: 34 });
    const cut = textBoxes(plain.view).some((b) => b.x + b.w > plain.width);
    assert.ok(cut, "framed on the drawings alone, the texts run off the picture (the bug this is for)");
    for (const b of textBoxes(framed.view)) {
      assert.ok(b.x >= 28 - 0.5 && b.x + b.w <= framed.width - 28 + 0.5, `x: ${JSON.stringify(b)} in ${framed.width}`);
      assert.ok(b.y >= 28 - 0.5 && b.y + b.h <= framed.height - 34 - 28 + 0.5, `y: ${JSON.stringify(b)} in ${framed.height}`);
    }
    assert.ok(framed.view.k < plain.view.k, "a little smaller in scale");
    assert.ok(Math.abs(framed.width - plain.width) <= 2, "the same width");
    assert.ok(FRAME_PASSES >= 3);
  });

  it("asks for the drawings again at the scale it settles on (the figure draws small texts larger)", () => {
    const seen: Array<number | null> = [];
    paperFrame({
      table: TABLE,
      components: comps,
      texts: [],
      beamPoints: [],
      componentBox: (c, k) => {
        seen.push(k);
        return box(c);
      },
    });
    assert.equal(seen[0], null, "first without a scale");
    assert.ok(seen.some((k) => k !== null && k > 0), "then at the frame's scale");
  });

  it("the box of a turned drawing turns with it", () => {
    const b = rotatedBox({ x_mm: 10, y_mm: 20, angle_deg: 90 }, { minX: -40, maxX: 0, minY: -8, maxY: 8 });
    const r = (v: number) => Math.round(v * 1e9) / 1e9 + 0;
    assert.deepEqual([b.minX, b.maxX, b.minY, b.maxY].map(r), [2, 18, -20, 20]);
  });
});

describe("the scale bar", () => {
  it("is 1, 2 or 5 times a power of ten, as long as fits", () => {
    for (const k of [0.05, 0.3, 1, 1.7, 4, 40]) {
      const bar = scaleBar(k, 160);
      assert.ok(bar.px <= 160 + 1e-9 && bar.px > 160 / 2.6, `${k}: ${JSON.stringify(bar)}`);
      const lead = Number(bar.mm.toExponential().split("e")[0]);
      assert.ok([1, 2, 5].includes(lead), `${k}: ${bar.mm}`);
    }
  });

  it("speaks in mm, m or µm as the length asks", () => {
    assert.equal(scaleBar(1, 160).label, "100 mm");
    assert.equal(scaleBar(0.05, 160).label, "2 m");
    assert.equal(scaleBar(400, 160).label, "200 µm");
  });
});

describe("the legend", () => {
  it("lists every wavelength once, in the order of the beams, and 'no light' when some is drawn so", () => {
    const l = layoutOf([
      comp("laser", "a", 0, 100, 0, { props: { wavelengths_nm: [532, 1064], polarization: "linear", pol_angle_deg: 0 } }),
      comp("laser", "b", 0, 300, 0, { props: { wavelength_nm: 532 } }),
      comp("polarizer", "p1", 100, 100, 0, { props: { axis_deg: 90 } }),
      comp("detector", "d1", 300, 100, 180),
    ]);
    const { entries, dark } = legendOf(traceLayoutDetailed(l), DARK_P);
    assert.deepEqual(entries.map((e) => e.nm), [532, 1064]);
    assert.equal(dark, true, "the crossed polariser puts out no light");
    const plain = legendOf(traceLayoutDetailed(layoutOf([comp("laser", "a", 0, 100, 0, { props: { wavelength_nm: 633 } })])), DARK_P);
    assert.deepEqual(plain, { entries: [{ nm: 633 }], dark: false });
    assert.deepEqual(legendOf([], DARK_P), { entries: [], dark: false });
  });
});

describe("file names", () => {
  it("say it is a figure, of which revision, and whether it was saved", () => {
    assert.equal(paperFileName({ id: "shg-demo", revision: 3 }, "svg"), "shg-demo-r3-figure.svg");
    assert.equal(paperFileName({ id: "a b", revision: 0 }, "png", true), "a_b-r0-unsaved-figure.png");
  });
});
