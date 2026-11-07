// Geometry of the optical layout: rounding, angles, component frames, breadboard holes,
// snapping. Pure functions, no React, no fetch, so `node --test` can run them
// (test/opticalLayoutGeometry.test.ts).
//
// Conventions (docs/v2/design/optical_layout.md section 2): mm and degrees, x right, y up,
// angles counter-clockwise from +x. A component's local +x axis points along `angle_deg`
// and its active surface is the segment through the centre along the local y axis,
// `aperture_mm` long.

import type { LayoutComponent, TableSpec, Vec } from "./types.ts";

const DEG = Math.PI / 180;

export function deg2rad(deg: number): number {
  return deg * DEG;
}

export function rad2deg(rad: number): number {
  return rad / DEG;
}

// ── rounding ──────────────────────────────────────────────────────────────────

/**
 * `round(x, nd)` the way Python does it: the exact binary value decides, and only an
 * exact tie goes to the even neighbour. `Math.round(x * 10**nd) / 10**nd` differs on
 * 2.675 (the double is just below the tie, Python gives 2.67, the product rounds up to
 * 267.5) and on exact ties like 0.0625 (Python gives 0.062).
 *
 * The backend rounds trace output to 0.001 with its own `round`, and the shared
 * fixtures are compared at 1e-6, so a one-unit difference in the last place is a
 * failure and has to be avoided on purpose.
 */
export function pyRound(x: number, nd: number): number {
  if (!Number.isFinite(x)) return x;
  const m = 10 ** nd;
  const scaled = x * m;
  if (!Number.isFinite(scaled)) return x;
  const frac = scaled - Math.floor(scaled);
  if (Math.abs(frac - 0.5) > 1e-6) return Math.round(scaled) / m;
  // Within rounding noise of a tie: decide on the exact decimal expansion of |x|.
  const neg = x < 0 || Object.is(x, -0);
  const s = Math.abs(x).toFixed(Math.min(100, nd + 70));
  const dot = s.indexOf(".");
  const intPart = s.slice(0, dot);
  const dec = s.slice(dot + 1);
  const kept = BigInt(intPart + dec.slice(0, nd));
  const tail = dec.slice(nd);
  let n = kept;
  if (tail[0] !== undefined && tail[0] >= "5") {
    const exactTie = tail[0] === "5" && /^0*$/.test(tail.slice(1));
    if (!exactTie || kept % 2n === 1n) n = kept + 1n;
  }
  const out = Number(n) / m;
  return neg ? -out : out;
}

export const round1 = (x: number): number => pyRound(x, 1);
export const round2 = (x: number): number => pyRound(x, 2);
export const round3 = (x: number): number => pyRound(x, 3);

/** An angle wrapped to [0, 360). Not rounded. */
export function wrapAngle(deg: number): number {
  if (!Number.isFinite(deg)) return 0;
  // Not ((deg % 360) + 360) % 360: adding 360 to a small angle and taking it off again
  // leaves dust in the low digits (0.3 comes back as 0.30000000000001137).
  let w = deg % 360;
  if (w < 0) w += 360;
  if (w >= 360) w -= 360; // -1e-17 + 360 is 360 in floating point
  return w === 0 ? 0 : w; // never -0
}

/**
 * The angle the way it is saved: wrapped, rounded to 0.01 degree, wrapped again (359.996
 * rounds up to 360.00, which is not in [0, 360)).
 */
export function canonicalAngle(deg: number): number {
  return wrapAngle(round2(wrapAngle(deg)));
}

/** Smallest signed difference a - b in (-180, 180]. */
export function angleDiff(a: number, b: number): number {
  let d = wrapAngle(a - b);
  if (d > 180) d -= 360;
  return d;
}

// ── vectors ───────────────────────────────────────────────────────────────────

export const vec = (x: number, y: number): Vec => ({ x, y });
export const add = (a: Vec, b: Vec): Vec => ({ x: a.x + b.x, y: a.y + b.y });
export const sub = (a: Vec, b: Vec): Vec => ({ x: a.x - b.x, y: a.y - b.y });
export const scale = (a: Vec, k: number): Vec => ({ x: a.x * k, y: a.y * k });
export const dot = (a: Vec, b: Vec): number => a.x * b.x + a.y * b.y;
export const cross = (a: Vec, b: Vec): number => a.x * b.y - a.y * b.x;
export const length = (a: Vec): number => Math.hypot(a.x, a.y);
export const dist = (a: Vec, b: Vec): number => Math.hypot(a.x - b.x, a.y - b.y);

/** Unit vector, or null for the zero vector. */
export function normalize(a: Vec): Vec | null {
  const l = Math.hypot(a.x, a.y);
  return l > 0 ? { x: a.x / l, y: a.y / l } : null;
}

/** Unit vector pointing `deg` degrees counter-clockwise from +x. */
export function dirOf(deg: number): Vec {
  const r = deg * DEG;
  return { x: Math.cos(r), y: Math.sin(r) };
}

/** Direction angle of a vector in [0, 360). */
export function angleOf(v: Vec): number {
  return wrapAngle(Math.atan2(v.y, v.x) / DEG);
}

/** A point turned `deg` degrees counter-clockwise about a pivot. */
export function rotateAbout(p: Vec, pivot: Vec, deg: number): Vec {
  const r = deg * DEG;
  const c = Math.cos(r);
  const s = Math.sin(r);
  const dx = p.x - pivot.x;
  const dy = p.y - pivot.y;
  return { x: pivot.x + dx * c - dy * s, y: pivot.y + dx * s + dy * c };
}

/** Reflection d - 2 (d.n) n of direction d off a surface with unit normal n. */
export function reflect(d: Vec, n: Vec): Vec {
  const k = 2 * dot(d, n);
  return { x: d.x - k * n.x, y: d.y - k * n.y };
}

// ── component frame ───────────────────────────────────────────────────────────

export interface Frame {
  /** centre */
  c: Vec;
  /** local +x unit vector: the normal / axis direction */
  n: Vec;
  /** local +y unit vector: along the active surface */
  u: Vec;
  /** half aperture */
  half: number;
}

export function frameOf(c: Pick<LayoutComponent, "x_mm" | "y_mm" | "angle_deg" | "aperture_mm">): Frame {
  const n = dirOf(c.angle_deg);
  return { c: { x: c.x_mm, y: c.y_mm }, n, u: { x: -n.y, y: n.x }, half: c.aperture_mm / 2 };
}

/** The two ends of the active surface: centre -/+ u * aperture / 2. */
export function apertureEnds(c: Pick<LayoutComponent, "x_mm" | "y_mm" | "angle_deg" | "aperture_mm">): [Vec, Vec] {
  const f = frameOf(c);
  return [
    { x: f.c.x - f.u.x * f.half, y: f.c.y - f.u.y * f.half },
    { x: f.c.x + f.u.x * f.half, y: f.c.y + f.u.y * f.half },
  ];
}

/** Local coordinates (lx along the normal, ly along the surface) to platform coordinates. */
export function localToWorld(f: Frame, lx: number, ly: number): Vec {
  return { x: f.c.x + f.n.x * lx + f.u.x * ly, y: f.c.y + f.n.y * lx + f.u.y * ly };
}

/** Platform point to local coordinates of a frame. */
export function worldToLocal(f: Frame, p: Vec): Vec {
  const dx = p.x - f.c.x;
  const dy = p.y - f.c.y;
  return { x: dx * f.n.x + dy * f.n.y, y: dx * f.u.x + dy * f.u.y };
}

/** Distance from a point to a segment, and the closest point on it. */
export function closestOnSegment(p: Vec, a: Vec, b: Vec): { point: Vec; t: number; dist: number } {
  const abx = b.x - a.x;
  const aby = b.y - a.y;
  const len2 = abx * abx + aby * aby;
  const t = len2 > 0 ? Math.max(0, Math.min(1, ((p.x - a.x) * abx + (p.y - a.y) * aby) / len2)) : 0;
  const point = { x: a.x + abx * t, y: a.y + aby * t };
  return { point, t, dist: Math.hypot(p.x - point.x, p.y - point.y) };
}

// ── breadboard holes ──────────────────────────────────────────────────────────

/** Hole columns and rows on the table (holes at offset + k * pitch, k = 0 ...). */
export function holeCounts(table: TableSpec): { cols: number; rows: number } {
  const pitch = table.hole_pitch_mm;
  if (!(pitch > 0)) return { cols: 0, rows: 0 };
  const count = (extent: number) => {
    const n = Math.floor((extent - table.hole_offset_mm) / pitch + 1e-9) + 1;
    return n > 0 ? n : 0;
  };
  return { cols: count(table.width_mm), rows: count(table.height_mm) };
}

/** Position of the hole in column `col`, row `row`; both count from 1 at the lower-left. */
export function holePosition(table: TableSpec, col: number, row: number): Vec {
  return {
    x: round2(table.hole_offset_mm + (col - 1) * table.hole_pitch_mm),
    y: round2(table.hole_offset_mm + (row - 1) * table.hole_pitch_mm),
  };
}

export interface NearestHole {
  /** 1-based column (x) and row (y), counted from the lower-left corner, clamped to the table */
  col: number;
  row: number;
  /** the hole's position */
  x: number;
  y: number;
  /** the point minus the hole: how far the point sits right of / above the hole */
  dx: number;
  dy: number;
  /** the point lies inside the table rectangle */
  inside: boolean;
}

/** The hole nearest to a point. Null for a table without a usable hole pitch. */
export function nearestHole(table: TableSpec, p: Vec): NearestHole | null {
  const { cols, rows } = holeCounts(table);
  if (cols === 0 || rows === 0) return null;
  const pitch = table.hole_pitch_mm;
  const idx = (v: number, n: number) =>
    Math.min(n, Math.max(1, Math.round((v - table.hole_offset_mm) / pitch) + 1));
  const col = idx(p.x, cols);
  const row = idx(p.y, rows);
  const h = holePosition(table, col, row);
  return {
    col,
    row,
    x: h.x,
    y: h.y,
    dx: round2(p.x - h.x),
    dy: round2(p.y - h.y),
    inside: p.x >= 0 && p.x <= table.width_mm && p.y >= 0 && p.y <= table.height_mm,
  };
}

export type SnapMode = "off" | "hole" | "half" | "mm";

export const SNAP_MODES: readonly SnapMode[] = ["off", "hole", "half", "mm"];

/** Round `v` onto the lattice origin + k * step. */
function lattice(v: number, origin: number, step: number): number {
  return origin + Math.round((v - origin) / step) * step;
}

/**
 * Snap a point. "hole": onto the holes (the lattice goes on past the table edge, so a
 * component placed outside the table still snaps); "half": onto the midpoints between
 * holes as well; "mm": to whole millimetres.
 */
export function snapPoint(table: TableSpec, p: Vec, mode: SnapMode): Vec {
  if (mode === "off") return { x: round2(p.x), y: round2(p.y) };
  if (mode === "mm") return { x: Math.round(p.x), y: Math.round(p.y) };
  const pitch = table.hole_pitch_mm;
  if (!(pitch > 0)) return { x: round2(p.x), y: round2(p.y) };
  const step = mode === "half" ? pitch / 2 : pitch;
  return {
    x: round2(lattice(p.x, table.hole_offset_mm, step)),
    y: round2(lattice(p.y, table.hole_offset_mm, step)),
  };
}

// ── keyboard steps (section 5) ────────────────────────────────────────────────

export type StepSize = "normal" | "fine" | "coarse";

export const MOVE_STEP_MM: Record<StepSize, number> = { normal: 1, fine: 0.1, coarse: 10 };
export const ROTATE_STEP_DEG: Record<StepSize, number> = { normal: 1, fine: 0.1, coarse: 15 };

/** Shift is the fine step, Alt the coarse one; both held counts as fine (the safe side). */
export function stepSize(mods: { shift?: boolean; alt?: boolean }): StepSize {
  if (mods.shift) return "fine";
  if (mods.alt) return "coarse";
  return "normal";
}

// ── bounds ────────────────────────────────────────────────────────────────────

export interface Bounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

/** Bounding box of the table and everything placed on it (components may sit off the table). */
export function layoutBounds(table: TableSpec, comps: readonly LayoutComponent[], pad = 0): Bounds {
  let minX = 0;
  let minY = 0;
  let maxX = table.width_mm;
  let maxY = table.height_mm;
  for (const c of comps) {
    const r = c.aperture_mm / 2;
    minX = Math.min(minX, c.x_mm - r);
    maxX = Math.max(maxX, c.x_mm + r);
    minY = Math.min(minY, c.y_mm - r);
    maxY = Math.max(maxY, c.y_mm + r);
  }
  return { minX: minX - pad, minY: minY - pad, maxX: maxX + pad, maxY: maxY + pad };
}
