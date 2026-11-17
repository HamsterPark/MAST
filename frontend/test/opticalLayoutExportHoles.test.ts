// ════════════════════════════════════════════════════════════════════════════
// Tests for src/lib/opticalLayout/paperHoles.ts: the table's holes in the figure export as one path of
// circles (docs/v2/design/optical_layout_exports.md). MuPDF, and with it the PDF export, fills an SVG
// <pattern> solid black, so the figure draws its holes itself; the screen keeps its pattern.
//
//     cd frontend && npm run test:unit
//
// What is pinned: where the holes are (offset + i * pitch, i >= 0, up to the far edge, as the server's
// PNG draws them), that only the holes in the picture are written, the circle of each, the cap, and the
// wiring in LayoutSvg (the figure has no <pattern>, the screen keeps it).
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { MAX_PAPER_HOLES, paperHolesPath } from "../src/lib/opticalLayout/paperHoles.ts";
import { toScreen, type View } from "../src/lib/opticalLayout/view.ts";
import type { TableSpec } from "../src/lib/opticalLayout/types.ts";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const SVG = readFileSync(join(HERE, "..", "src", "components", "opticallayout", "LayoutSvg.tsx"), "utf8");

const TABLE: TableSpec = { width_mm: 1200, height_mm: 900, hole_pitch_mm: 25, hole_offset_mm: 12.5, grid: "metric" };

/** The whole table in a picture `pad` px round it, at k px/mm (y up on the table, down on the picture). */
function wholeTable(t: TableSpec, k: number, pad = 10): { view: View; width: number; height: number } {
  return { view: { k, ox: pad, oy: pad + t.height_mm * k }, width: Math.ceil(t.width_mm * k + 2 * pad), height: Math.ceil(t.height_mm * k + 2 * pad) };
}

/** The circles of a path: [cx, cy, r] per subpath "M x y a r r 0 1 0 2r 0 a r r 0 1 0 -2r 0 Z". */
function circles(d: string): Array<[number, number, number]> {
  const out: Array<[number, number, number]> = [];
  const re = /M(-?[\d.]+) (-?[\d.]+)a([\d.]+) ([\d.]+) 0 1 0 ([\d.]+) 0a([\d.]+) ([\d.]+) 0 1 0 (-[\d.]+) 0Z/gy;
  let m: RegExpExecArray | null;
  let at = 0;
  while ((m = re.exec(d)) !== null) {
    const [x, y, r1, r2, dx, r3, r4, back] = m.slice(1).map(Number) as [number, number, number, number, number, number, number, number];
    assert.ok(r1 === r2 && r2 === r3 && r3 === r4 && Math.abs(dx - 2 * r1) < 0.011 && Math.abs(back + 2 * r1) < 0.011, m[0]);
    out.push([x + r1, y, r1]);
    at = re.lastIndex;
  }
  assert.equal(at, d.length, "nothing but circles in the path");
  return out;
}

describe("the figure's holes", () => {
  it("are every hole of the table when the whole table is in the picture: 48 x 36 on 1200 x 900 mm at 25 mm", () => {
    const { view, width, height } = wholeTable(TABLE, 0.9);
    const h = paperHolesPath(TABLE, view, width, height, 2.7);
    assert.equal(h.count, 48 * 36);
    assert.equal(h.capped, false);
    const cs = circles(h.d);
    assert.equal(cs.length, h.count);
    // the first at (offset, offset), the last at (offset + 47 pitch, offset + 35 pitch)
    const first = toScreen(view, { x: 12.5, y: 12.5 });
    const last = toScreen(view, { x: 12.5 + 47 * 25, y: 12.5 + 35 * 25 });
    assert.ok(cs.some(([x, y]) => Math.abs(x - first.x) < 0.011 && Math.abs(y - first.y) < 0.011), "a hole at the first position");
    assert.ok(cs.some(([x, y]) => Math.abs(x - last.x) < 0.011 && Math.abs(y - last.y) < 0.011), "a hole at the last position");
    assert.ok(cs.every(([, , r]) => r === 2.7));
  });

  it("start at the offset and never before it, and include a hole right at the far edge", () => {
    const t: TableSpec = { width_mm: 100, height_mm: 50, hole_pitch_mm: 25, hole_offset_mm: 25, grid: "metric" };
    const { view, width, height } = wholeTable(t, 2);
    const cs = circles(paperHolesPath(t, view, width, height, 3).d);
    const xs = [...new Set(cs.map(([x]) => Math.round(((x - view.ox) / view.k) * 1000) / 1000))].sort((a, b) => a - b);
    const ys = [...new Set(cs.map(([, y]) => Math.round(((view.oy - y) / view.k) * 1000) / 1000))].sort((a, b) => a - b);
    assert.deepEqual(xs, [25, 50, 75, 100], "none at 0 (offset - pitch), one at the edge");
    assert.deepEqual(ys, [25, 50]);
  });

  it("are only those in the picture (and a hole's width round it): a figure framed on part of a large table", () => {
    const big: TableSpec = { width_mm: 3000, height_mm: 2000, hole_pitch_mm: 25, hole_offset_mm: 12.5, grid: "metric" };
    // 1100 px across the table's lower-left 1000 x 700 mm
    const k = 1.1;
    const view: View = { k, ox: 0, oy: 700 * k };
    const h = paperHolesPath(big, view, 1100, 770, 3);
    // the picture is x 0-1000 mm, y 0-700 mm: x 12.5 ... 987.5 (40), y 12.5 ... 687.5 (28); the next ones (1012.5,
    // 712.5) are 12.5 mm past the edge, more than a hole's radius (3 px = 2.7 mm)
    assert.equal(h.count, 40 * 28);
    const whole = wholeTable(big, 1100 / 3000);
    assert.equal(paperHolesPath(big, whole.view, whole.width, whole.height, 1.1).count, 120 * 80, "the whole large table: 9600");
  });

  it("are none without a grid or a pitch, and none (counted) past the cap", () => {
    const { view, width, height } = wholeTable(TABLE, 0.9);
    assert.deepEqual(paperHolesPath({ ...TABLE, grid: "none" }, view, width, height, 2), { d: "", count: 0, capped: false });
    assert.deepEqual(paperHolesPath({ ...TABLE, hole_pitch_mm: 0 }, view, width, height, 2), { d: "", count: 0, capped: false });
    const dense: TableSpec = { width_mm: 20000, height_mm: 20000, hole_pitch_mm: 10, hole_offset_mm: 5, grid: "metric" };
    const w = wholeTable(dense, 0.06);
    const h = paperHolesPath(dense, w.view, w.width, w.height, 0.8);
    assert.equal(h.count, 2000 * 2000);
    assert.ok(h.count > MAX_PAPER_HOLES);
    assert.deepEqual([h.d, h.capped], ["", true]);
  });

  it("write under 60 bytes a hole: the large table's 9600 are under 600 kB", () => {
    const big: TableSpec = { width_mm: 3000, height_mm: 2000, hole_pitch_mm: 25, hole_offset_mm: 12.5, grid: "metric" };
    const w = wholeTable(big, 1100 / 3000);
    const h = paperHolesPath(big, w.view, w.width, w.height, 1.1);
    // an absolute move and two arcs a hole (about 56 bytes); a pattern would be a few hundred bytes in all, but
    // MuPDF paints it black
    assert.ok(h.d.length / h.count < 60, String(h.d.length / h.count));
    assert.ok(h.d.length < 600_000, String(h.d.length));
  });
});

describe("in LayoutSvg", () => {
  it("the figure draws its holes as one path and no <pattern>; the screen keeps the pattern", () => {
    const table = SVG.slice(SVG.indexOf("const Table = memo("), SVG.indexOf("// ── rulers"));
    assert.match(table, /paper && showHoles \? paperHolesPath\(table, view, width, height, holeR\) : null/);
    assert.match(table, /\{showHoles && !paper && \(\s*<>\s*<defs>\s*<pattern /, "the pattern is the screen's only");
    assert.match(table, /\{paperHoles && paperHoles\.d && <path d=\{paperHoles\.d\} fill="var\(--mast-border-strong\)" fillOpacity=\{0\.55\} \/>\}/);
    assert.match(SVG, /<Table table=\{layout\.table\} view=\{view\} display=\{display\} paper=\{paper\} width=\{width\} height=\{height\} \/>/);
  });
});
