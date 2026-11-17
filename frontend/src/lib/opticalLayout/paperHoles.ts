// The table's holes in the figure export, as one path of circles (docs/v2/design/optical_layout_exports.md).
//
// The canvas fills the table with an SVG <pattern> of one hole, which is cheap and which every browser draws. Programs
// that read the exported figure do not all know <pattern>: MuPDF, and with it the PDF export, fills the table solid
// black. So the figure (LayoutSvg's paper variant) draws each hole itself: one subpath of two arcs per hole, all in a
// single <path>, only the holes that fall inside the picture. The screen keeps its pattern.
//
// Where the holes are: hole_offset_mm + i * hole_pitch_mm from the table's lower-left corner, i >= 0, up to the far
// edge (as the server's PNG draws them, render.py), in both directions.
//
// Pure, so it is tested without a browser (test/opticalLayoutExportHoles.test.ts).

import type { TableSpec } from "./types.ts";
import { toScreen, visibleBounds, type View } from "./view.ts";

/**
 * At most this many holes in one figure. A figure is at most about 1160 x 1530 px and holes are drawn only when they
 * are at least 6 px apart (view.ts holesVisible), which makes about 49 000; past this the holes are left out rather
 * than written into a file of many megabytes.
 */
export const MAX_PAPER_HOLES = 60_000;

export interface PaperHoles {
  /** path data: one circle per hole (empty when there is none, or too many) */
  d: string;
  /** how many holes are in the picture (counted even when there are too many to draw) */
  count: number;
  /** more than MAX_PAPER_HOLES: none are drawn */
  capped: boolean;
}

/** The hole indices i with offset + i * pitch inside [lo, hi] and inside the table [0, size]. */
function indexRange(offset: number, pitch: number, size: number, lo: number, hi: number): [number, number] {
  const from = Math.max(0, Math.ceil((Math.max(lo, 0) - offset) / pitch - 1e-9));
  const to = Math.floor((Math.min(hi, size) - offset) / pitch + 1e-9);
  return [from, to];
}

/**
 * The holes of `table` that fall inside a picture `width` x `height` px drawn at `view`, as circles of radius `r` px:
 * one path, each hole "M x-r y a r r 0 1 0 2r 0 a r r 0 1 0 -2r 0 Z". Coordinates at `digits` decimals.
 */
export function paperHolesPath(table: TableSpec, view: View, width: number, height: number, r: number, digits = 2): PaperHoles {
  const pitch = table.hole_pitch_mm;
  if (!(pitch > 0) || !(r > 0) || table.grid === "none") return { d: "", count: 0, capped: false };
  // the picture in table millimetres, widened by a hole so that one cut by the edge is still drawn
  const vb = visibleBounds(view, width, height);
  const margin = r / view.k;
  const [i0, i1] = indexRange(table.hole_offset_mm, pitch, table.width_mm, vb.minX - margin, vb.maxX + margin);
  const [j0, j1] = indexRange(table.hole_offset_mm, pitch, table.height_mm, vb.minY - margin, vb.maxY + margin);
  const nx = Math.max(0, i1 - i0 + 1);
  const ny = Math.max(0, j1 - j0 + 1);
  const count = nx * ny;
  if (count === 0) return { d: "", count: 0, capped: false };
  if (count > MAX_PAPER_HOLES) return { d: "", count, capped: true };
  const f = 10 ** digits;
  const n = (v: number): string => (Math.round(v * f) / f + 0).toString();
  const rs = n(r);
  const arcs = `a${rs} ${rs} 0 1 0 ${n(2 * r)} 0a${rs} ${rs} 0 1 0 ${n(-2 * r)} 0Z`;
  const parts: string[] = [];
  for (let j = j0; j <= j1; j++) {
    for (let i = i0; i <= i1; i++) {
      const p = toScreen(view, { x: table.hole_offset_mm + i * pitch, y: table.hole_offset_mm + j * pitch });
      parts.push(`M${n(p.x - r)} ${n(p.y)}${arcs}`);
    }
  }
  return { d: parts.join(""), count, capped: false };
}
