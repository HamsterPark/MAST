// The table in the 3D view: its slab, the area its holes cover (drawn as one repeating texture, one tile per
// hole, so 10 000 holes cost four vertices), its grid lines, and the box round everything that is drawn.
// Pure (test/opticalLayout3dScene.test.ts).
//
// The holes are where geometry.ts puts them: at hole_offset + k · pitch on both axes, k = 0, 1, ... while they
// are on the table (holeCounts). The textured patch reaches half a pitch round the outer holes, clipped to the
// table, and its texture coordinates count pitches from half a pitch before the first hole, so the middle of
// every tile is a hole.

import type { LayoutComponent, TableSpec } from "../types.ts";
import { holeCounts } from "../geometry.ts";
import { emptyBox, growBox, isEmptyBox, type Box3, type Vec3 } from "./mat4.ts";
import { componentHeight, periscopeOutHeight, tableBeamHeight } from "./heights.ts";

/** How thick the table is drawn, mm (its top is z = 0). */
export const TABLE_THICKNESS_MM = 60;
/** The hole's diameter as a share of the pitch (M6 in 25 mm, ¼-20 in 1"). */
export const HOLE_SHARE = 0.26;

export interface HolePatch {
  /** the rectangle of the table top the hole texture covers, mm */
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  /** texture coordinates at (x0, y0) and (x1, y1): one unit per pitch, a hole at the middle of every unit */
  u0: number;
  v0: number;
  u1: number;
  v1: number;
}

/** Where the holes are drawn, or null for a table without holes. */
export function holePatch(table: TableSpec): HolePatch | null {
  if (table.grid === "none") return null;
  const pitch = table.hole_pitch_mm;
  const { cols, rows } = holeCounts(table);
  if (!(pitch > 0) || cols === 0 || rows === 0) return null;
  const o = table.hole_offset_mm;
  const first = o - pitch / 2;
  const x0 = Math.max(0, first);
  const y0 = Math.max(0, first);
  const x1 = Math.min(table.width_mm, o + (cols - 1) * pitch + pitch / 2);
  const y1 = Math.min(table.height_mm, o + (rows - 1) * pitch + pitch / 2);
  if (!(x1 > x0 && y1 > y0)) return null;
  return { x0, y0, x1, y1, u0: (x0 - first) / pitch, v0: (y0 - first) / pitch, u1: (x1 - first) / pitch, v1: (y1 - first) / pitch };
}

/** A grid step (mm) for lines across the table: a round 1, 2 or 5 · 10^n near a twelfth of its longer side. */
export function gridStep(table: TableSpec): number {
  const target = Math.max(table.width_mm, table.height_mm) / 12;
  const p = 10 ** Math.floor(Math.log10(Math.max(target, 1e-6)));
  for (const m of [1, 2, 5, 10]) if (m * p >= target) return m * p;
  return 10 * p;
}

/** Grid lines on the table top: every `gridStep` mm, inside the table, at most `max` of each direction. */
export function gridLines(table: TableSpec, max = 200): Array<[Vec3, Vec3]> {
  if (table.grid === "none") return [];
  const step = gridStep(table);
  const out: Array<[Vec3, Vec3]> = [];
  const z = 0.15;
  for (let i = 1; i * step < table.width_mm && i <= max; i++) out.push([[i * step, 0, z], [i * step, table.height_mm, z]]);
  for (let j = 1; j * step < table.height_mm && j <= max; j++) out.push([[0, j * step, z], [table.width_mm, j * step, z]]);
  return out;
}

/** The table slab as a box: the top at z = 0. */
export function tableBox(table: TableSpec): Box3 {
  return { min: [0, 0, -TABLE_THICKNESS_MM], max: [table.width_mm, table.height_mm, 0] };
}

/**
 * The box round what the view shows: the table, and every component (a sphere of its aperture round its
 * height, which also holds a raised periscope's output), up to its beam height at least.
 */
export function sceneBox(table: TableSpec, components: readonly LayoutComponent[]): Box3 {
  const b = emptyBox();
  const t = tableBox(table);
  growBox(b, t.min);
  growBox(b, t.max);
  growBox(b, [0, 0, tableBeamHeight(table)]);
  for (const c of components) {
    const r = Math.max(10, c.aperture_mm / 2 + 10);
    const lo = Math.min(componentHeight(c, table), c.type === "periscope" ? periscopeOutHeight(c, table) : Infinity);
    const hi = Math.max(componentHeight(c, table), c.type === "periscope" ? periscopeOutHeight(c, table) : -Infinity);
    growBox(b, [c.x_mm - r, c.y_mm - r, lo - r]);
    growBox(b, [c.x_mm + r, c.y_mm + r, hi + r]);
  }
  return isEmptyBox(b) ? tableBox(table) : b;
}
