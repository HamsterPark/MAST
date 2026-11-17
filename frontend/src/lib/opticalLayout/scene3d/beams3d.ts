// The light in the 3D view, as data: every traced segment as a tube, the 1/e² envelope as a surface round it,
// the polarisation as the field's real direction, the power where a beam ends.
//
// Nothing is traced or computed again here: the segments, their power and their beam state are the editor's TS
// trace (trace.ts), the same one the 2D canvas draws. What a beam looks like comes from the same functions as on
// the 2D canvas: the colour, the dash and the width and opacity by power are colors.ts beamLook of
// beamDraw.ts segmentPower (so the tube of a branch is as heavy as its 2D line, "no light" a thin grey dotted
// one), the envelope is sampled where beamDraw.ts samples its band (bandSamples) at the size beam.ts gives
// (diameterOf), the polarisation is read with beam.ts readPolarization. The only thing the third dimension adds
// is the height: a segment runs at heights.ts segmentHeight, and where two segments that meet run at different
// heights (a periscope) a vertical tube joins them. Pure, so it is tested without a browser
// (test/opticalLayout3dBeams.test.ts).

import type { Segment, StokesNorm, TableSpec } from "../types.ts";
import type { DetailedTrace } from "../trace.ts";
import { DARK_P, beamLook, type BeamLook, type ThemeName } from "../colors.ts";
import { BAND_OPACITY, POL_AT, bandSamples, powerTags, segmentPower, type SegRef } from "../beamDraw.ts";
import { diameterOf, readPolarization, waistInSegment, type PolReading } from "../beam.ts";
import { eventHeight, segmentHeight, tableBeamHeight } from "./heights.ts";
import type { Vec3 } from "./mat4.ts";

/** Two segments that meet count as one height when their heights differ by less than this (mm). */
export const HEIGHT_EPS_MM = 1e-6;

export interface TubeSpec {
  /** `tree|branch|segment`; a vertical joint ends in `^` */
  key: string;
  /** the segment it draws; a joint carries the segment it leads into */
  ref: SegRef;
  /** a vertical joint between two heights, not a traced segment */
  joint: boolean;
  a: Vec3;
  b: Vec3;
  color: string;
  opacity: number;
  /** the 2D line width of this power (px) */
  widthPx: number;
  /** on / off lengths of the dash pattern (px), or null for a solid line */
  dash: [number, number] | null;
  /** no light (p < 1e-6): drawn grey and dotted */
  dark: boolean;
}

/** "2 4" → [2, 4]; anything else (a solid line) → null. */
export function parseDash(dash: string | null): [number, number] | null {
  if (!dash) return null;
  const n = dash
    .trim()
    .split(/[\s,]+/)
    .map(Number)
    .filter((v) => Number.isFinite(v) && v >= 0);
  return n.length >= 2 && n[0]! + n[1]! > 0 ? [n[0]!, n[1]!] : null;
}

function lookCache(wl: number | null, theme: ThemeName): (p: number) => BeamLook {
  const looks = new Map<number, BeamLook>();
  return (p: number) => {
    let l = looks.get(p);
    if (!l) {
      l = beamLook(wl, p, theme);
      looks.set(p, l);
    }
    return l;
  };
}

function tube(key: string, ref: SegRef, joint: boolean, a: Vec3, b: Vec3, look: BeamLook): TubeSpec {
  return { key, ref, joint, a, b, color: look.color, opacity: look.opacity, widthPx: look.width, dash: parseDash(look.dash), dark: look.dark };
}

const segLen = (g: Segment) => Math.hypot(g.x1 - g.x0, g.y1 - g.y0);

/**
 * Every segment of the trace as a tube at its height, and a vertical joint wherever the light changes height
 * between two segments that meet (in a branch, or from a branch to the children that split off its end).
 */
export function beamTubes(trace: DetailedTrace, table: TableSpec, theme: ThemeName): TubeSpec[] {
  const out: TubeSpec[] = [];
  trace.forEach((tree, ti) => {
    const lookOf = lookCache(tree.wavelength_nm, theme);
    /** the height a branch ends at, for the children that start there */
    const endZ = new Map<string, number>();
    for (const br of tree.branches) {
      const segs = br.segments;
      let prevZ: number | null = br.parent !== null ? (endZ.get(br.parent) ?? null) : null;
      for (let si = 0; si < segs.length; si++) {
        const g = segs[si]!;
        const z = segmentHeight(g, table);
        const look = lookOf(segmentPower(br, g));
        const ref: SegRef = { tree: ti, branch: br.branch, segment: si };
        const key = `${ti}|${br.branch}|${si}`;
        if (prevZ !== null && Math.abs(z - prevZ) > HEIGHT_EPS_MM) out.push(tube(`${key}^`, ref, true, [g.x0, g.y0, prevZ], [g.x0, g.y0, z], look));
        if (segLen(g) > 0) out.push(tube(key, ref, false, [g.x0, g.y0, z], [g.x1, g.y1, z], look));
        prevZ = z;
      }
      if (prevZ !== null) endZ.set(br.branch, prevZ);
    }
  });
  return out;
}

// ── the envelope ──────────────────────────────────────────────────────────────

export interface EnvelopeSpec {
  key: string;
  ref: SegRef;
  /** the start of the segment */
  a: Vec3;
  /** unit vector along the segment */
  dir: Vec3;
  /** [distance from `a` (mm), 1/e² radius there (mm)], from the start to the end of the segment */
  samples: Array<[number, number]>;
  /** where the segment has its waist (mm from `a`), or null */
  waist: number | null;
  color: string;
  opacity: number;
}

/**
 * The 1/e² envelope of every segment with a Gaussian beam (v2 sections 4.4 and 8.2), sampled where the 2D band is
 * (dense at a waist): as on the 2D canvas, not for a tree without a beam size and not where there is no light.
 */
export function beamEnvelopes(trace: DetailedTrace, table: TableSpec, theme: ThemeName): EnvelopeSpec[] {
  const out: EnvelopeSpec[] = [];
  trace.forEach((tree, ti) => {
    const wl = tree.wavelength_nm;
    const m2 = tree.beam?.m2 ?? null;
    if (wl === null || m2 === null) return;
    const lookOf = lookCache(wl, theme);
    for (const br of tree.branches) {
      br.segments.forEach((g, si) => {
        const q = g.beam?.q ?? null;
        const L = segLen(g);
        if (q === null || !(L > 0)) return;
        const look = lookOf(segmentPower(br, g));
        if (look.dark) return;
        const samples: Array<[number, number]> = [];
        for (const s of bandSamples(q, L)) {
          const r = diameterOf([q[0] + s, q[1]], wl, m2) / 2;
          if (!Number.isFinite(r)) return;
          samples.push([s, r]);
        }
        const z = segmentHeight(g, table);
        const w = waistInSegment(q, L);
        out.push({
          key: `${ti}|${br.branch}|${si}`,
          ref: { tree: ti, branch: br.branch, segment: si },
          a: [g.x0, g.y0, z],
          dir: [(g.x1 - g.x0) / L, (g.y1 - g.y0) / L, 0],
          samples,
          waist: w === null ? null : Math.min(L, Math.max(0, w)),
          color: look.color,
          opacity: Math.round(BAND_OPACITY * look.opacity * 1000) / 1000,
        });
      });
    }
  });
  return out;
}

// ── polarisation ──────────────────────────────────────────────────────────────

export type PolMarkKind = "linear" | "unpolarized" | "ellipse";

export interface PolMark3 {
  key: string;
  kind: PolMarkKind;
  reading: PolReading;
  /** where on the beam the mark sits */
  at: Vec3;
  /** the mark, as line segments in table millimetres */
  lines: Array<[Vec3, Vec3]>;
}

export interface PolOptions {
  /** the length of a double arrow, tip to tip, mm */
  sizeMm: number;
  /** a segment shorter than this gets no mark, mm */
  minLengthMm: number;
}

const ELLIPSE_STEPS = 24;

const add3 = (a: Vec3, b: Vec3, k = 1): Vec3 => [a[0] + b[0] * k, a[1] + b[1] * k, a[2] + b[2] * k];

/**
 * The transverse frame of a ray travelling along `d` (in the table plane): e_h = z × d, in the table plane to the
 * LEFT of the direction of travel, and e_v = z, straight up (v2 section 4.1). Polarisation angles go from e_h
 * towards e_v.
 */
export function transverseFrame(d: Vec3): { eh: Vec3; ev: Vec3 } {
  return { eh: [-d[1], d[0], 0], ev: [0, 0, 1] };
}

/** The field direction of linear light at ψ degrees: cos ψ · e_h + sin ψ · e_v. */
export function fieldDirection(d: Vec3, psiDeg: number): Vec3 {
  const { eh, ev } = transverseFrame(d);
  const t = (psiDeg * Math.PI) / 180;
  return add3([eh[0] * Math.cos(t), eh[1] * Math.cos(t), eh[2] * Math.cos(t)], ev, Math.sin(t));
}

/** A double arrow through `at` along the unit vector `e`, `len` tip to tip, its heads in the plane of `e` and `d`. */
function doubleArrow(at: Vec3, e: Vec3, d: Vec3, len: number): Array<[Vec3, Vec3]> {
  const half = len / 2;
  const tipA = add3(at, e, half);
  const tipB = add3(at, e, -half);
  const head = len * 0.18;
  return [
    [tipB, tipA],
    [tipA, add3(add3(tipA, e, -head), d, head * 0.7)],
    [tipA, add3(add3(tipA, e, -head), d, -head * 0.7)],
    [tipB, add3(add3(tipB, e, head), d, head * 0.7)],
    [tipB, add3(add3(tipB, e, head), d, -head * 0.7)],
  ];
}

/**
 * The polarisation of every lit segment long enough for it, drawn where the field really points (v2 section 8.3,
 * in three dimensions): linear light (p in the table plane, s vertical, any angle between) as a double arrow
 * along the field, unpolarised light as two crossed double arrows, circular and elliptical light as the ellipse
 * the field draws in the plane across the beam (semi-axes along ψ and across it, in the ratio tan |χ|).
 * One mark per segment, POL_AT along it as on the 2D canvas.
 */
export function polarizationMarks(trace: DetailedTrace, table: TableSpec, opts: PolOptions): PolMark3[] {
  const out: PolMark3[] = [];
  trace.forEach((tree, ti) => {
    for (const br of tree.branches) {
      br.segments.forEach((g, si) => {
        const L = segLen(g);
        if (!(L >= opts.minLengthMm) || !g.beam || g.beam.s === null || !(segmentPower(br, g) >= DARK_P)) return;
        const reading = readPolarization(g.beam.s as StokesNorm);
        if (reading.kind === "none") return;
        const z = segmentHeight(g, table);
        const d: Vec3 = [(g.x1 - g.x0) / L, (g.y1 - g.y0) / L, 0];
        const at: Vec3 = [g.x0 + (g.x1 - g.x0) * POL_AT, g.y0 + (g.y1 - g.y0) * POL_AT, z];
        const key = `${ti}|${br.branch}|${si}`;
        const { eh, ev } = transverseFrame(d);
        if (reading.kind === "unpolarized") {
          out.push({ key, kind: "unpolarized", reading, at, lines: [...doubleArrow(at, eh, d, opts.sizeMm), ...doubleArrow(at, ev, d, opts.sizeMm)] });
          return;
        }
        if (reading.kind === "linear") {
          out.push({ key, kind: "linear", reading, at, lines: doubleArrow(at, fieldDirection(d, reading.psi_deg), d, opts.sizeMm) });
          return;
        }
        // circular or elliptical: the ellipse in the plane across the beam
        const major = fieldDirection(d, reading.psi_deg);
        const minor = fieldDirection(d, reading.psi_deg + 90);
        const A = opts.sizeMm / 2;
        const B = A * Math.min(1, Math.abs(Math.tan((reading.chi_deg * Math.PI) / 180)));
        const pts: Vec3[] = [];
        for (let i = 0; i <= ELLIPSE_STEPS; i++) {
          const t = (2 * Math.PI * i) / ELLIPSE_STEPS;
          pts.push(add3(add3(at, major, A * Math.cos(t)), minor, B * Math.sin(t)));
        }
        const lines: Array<[Vec3, Vec3]> = [];
        for (let i = 0; i < ELLIPSE_STEPS; i++) lines.push([pts[i]!, pts[i + 1]!]);
        out.push({ key, kind: "ellipse", reading, at, lines });
      });
    }
  });
  return out;
}

// ── where a beam passes over or under a component ─────────────────────────────

/**
 * A place where a beam crosses a component in the top view but at another height, so it goes over or under it
 * instead of hitting it (the height contract's `passes` of a segment, docs/v2/design/optical_layout_3d.md section
 * 3.5). The 3D view draws a thin vertical line from the beam to the component's axis there: where the 2D drawing
 * shows a beam running through a mirror, the 3D one shows by how much it misses.
 */
export interface PassMark {
  key: string;
  component: string;
  /** on the beam */
  at: Vec3;
  /** at the height of the component's axis (its entrance, for a periscope) */
  axis: Vec3;
  /** beam height minus the axis height, mm: > 0 the beam passes over */
  dz: number;
}

/** Every pass of the trace; none for a trace without heights. */
export function passMarks(trace: DetailedTrace, table: TableSpec): PassMark[] {
  const out: PassMark[] = [];
  trace.forEach((tree, ti) => {
    for (const br of tree.branches) {
      br.segments.forEach((g, si) => {
        const passes = (g as { passes?: unknown }).passes;
        if (!Array.isArray(passes)) return;
        const z = segmentHeight(g, table);
        passes.forEach((p: unknown, pi) => {
          if (!p || typeof p !== "object") return;
          const q = p as { component?: unknown; x?: unknown; y?: unknown; dz_mm?: unknown };
          if (typeof q.component !== "string" || typeof q.x !== "number" || typeof q.y !== "number" || typeof q.dz_mm !== "number") return;
          if (![q.x, q.y, q.dz_mm].every(Number.isFinite)) return;
          out.push({ key: `${ti}|${br.branch}|${si}|${pi}`, component: q.component, at: [q.x, q.y, z], axis: [q.x, q.y, z - q.dz_mm], dz: q.dz_mm });
        });
      });
    }
  });
  return out;
}

// ── power where a beam ends ───────────────────────────────────────────────────

export interface PowerTag3 {
  key: string;
  componentId: string;
  at: Vec3;
  zh: string;
  en: string;
  color: string;
  slot: number;
}

/** beamDraw.ts powerTags (the power of every beam that ends on the front of a component), at the height it arrives. */
export function powerTags3d(trace: DetailedTrace, table: TableSpec, theme: ThemeName): PowerTag3[] {
  const z = new Map<string, number>();
  trace.forEach((tree, ti) => {
    for (const br of tree.branches) {
      const ev = br.events[br.events.length - 1];
      const last = br.segments[br.segments.length - 1];
      const h = (ev ? eventHeight(ev) : null) ?? (last ? segmentHeight(last, table) : null);
      if (h !== null) z.set(`${ti}|${br.branch}`, h);
    }
  });
  return powerTags(trace, theme).map((t) => ({ key: t.key, componentId: t.componentId, at: [t.at.x, t.at.y, z.get(t.key) ?? tableBeamHeight(table)], zh: t.zh, en: t.en, color: t.color, slot: t.slot }));
}
