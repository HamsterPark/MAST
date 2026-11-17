// The view of the canvas: platform coordinates (mm, y up) to screen pixels (y down) and
// back, zoom about a point, fitting, and the ruler ticks. Pure, so the arithmetic that
// every drag and every wheel tick depends on is tested (test/opticalLayoutView.test.ts).

import type { Bounds } from "./geometry.ts";
import type { Vec } from "./types.ts";

/** screen x = ox + x * k, screen y = oy - y * k   (k pixels per millimetre) */
export interface View {
  k: number;
  ox: number;
  oy: number;
}

/** 0.02 px/mm shows a 100 m table in 2 px; 40 px/mm shows a 0.1 mm aperture as 4 px. */
export const MIN_K = 0.02;
export const MAX_K = 40;

export function clampK(k: number): number {
  return Math.min(MAX_K, Math.max(MIN_K, k));
}

export function toScreen(v: View, p: Vec): Vec {
  return { x: v.ox + p.x * v.k, y: v.oy - p.y * v.k };
}

export function toWorld(v: View, sx: number, sy: number): Vec {
  return { x: (sx - v.ox) / v.k, y: (v.oy - sy) / v.k };
}

/** Zoom by `factor` about the screen point (sx, sy): the platform point under it stays under it. */
export function zoomAt(v: View, factor: number, sx: number, sy: number): View {
  const k = clampK(v.k * factor);
  if (k === v.k) return v;
  const w = toWorld(v, sx, sy);
  return { k, ox: sx - w.x * k, oy: sy + w.y * k };
}

export function panBy(v: View, dx: number, dy: number): View {
  return dx === 0 && dy === 0 ? v : { k: v.k, ox: v.ox + dx, oy: v.oy + dy };
}

/** The view that shows `b` centred in a width x height viewport with `pad` pixels around it. */
export function fitView(b: Bounds, width: number, height: number, pad = 24): View {
  const w = Math.max(1e-6, b.maxX - b.minX);
  const h = Math.max(1e-6, b.maxY - b.minY);
  const availW = Math.max(10, width - 2 * pad);
  const availH = Math.max(10, height - 2 * pad);
  const k = clampK(Math.min(availW / w, availH / h));
  const cx = (b.minX + b.maxX) / 2;
  const cy = (b.minY + b.maxY) / 2;
  return { k, ox: width / 2 - cx * k, oy: height / 2 + cy * k };
}

/**
 * The view with the platform point `p` in the middle of a width x height viewport, zoomed in to at least `minK`
 * pixels per millimetre (a view that is closer than that keeps its zoom; the limits of the zoom hold).
 */
export function centerOn(v: View, p: Vec, width: number, height: number, minK = 0): View {
  const k = clampK(Math.max(v.k, minK));
  return { k, ox: width / 2 - p.x * k, oy: height / 2 + p.y * k };
}

/**
 * The view after the viewport changed size from `from` to `to`: the platform point in the middle stays in the
 * middle, and the zoom changes by the smaller of the two growth factors, so everything that was in view still
 * is (and larger, when the viewport grew).
 */
export function rescaleView(v: View, from: { w: number; h: number }, to: { w: number; h: number }): View {
  if (!(from.w > 0 && from.h > 0 && to.w > 0 && to.h > 0)) return v;
  const c = toWorld(v, from.w / 2, from.h / 2);
  const k = clampK(v.k * Math.min(to.w / from.w, to.h / from.h));
  return { k, ox: to.w / 2 - c.x * k, oy: to.h / 2 + c.y * k };
}

/** What the canvas remembers of the sizes it had while nobody panned or zoomed, to go back to them exactly. */
export interface ResizeMemory {
  /** the earlier sizes and the views they had, oldest first */
  stack: ReadonlyArray<{ w: number; h: number; view: View }>;
  /** the view the last resize produced: the memory holds only while the view is still that one */
  last: View | null;
}

export const EMPTY_RESIZE_MEMORY: ResizeMemory = { stack: [], last: null };

const sameView = (a: View, b: View) => a.k === b.k && a.ox === b.ox && a.oy === b.oy;

/**
 * The view after a resize from `from` to `to`. Growing and shrinking by the smaller factor (rescaleView) are not
 * each other's inverse, so a size the viewport already had since the view was last touched gets back the view
 * it had then, exactly: entering and leaving focus mode is a round trip. Panning or zooming in between forgets.
 */
export function viewAfterResize(mem: ResizeMemory, current: View, from: { w: number; h: number }, to: { w: number; h: number }): { view: View; mem: ResizeMemory } {
  const stack = mem.last !== null && sameView(mem.last, current) ? mem.stack : [];
  const i = stack.findIndex((e) => e.w === to.w && e.h === to.h);
  if (i >= 0) {
    const view = stack[i]!.view;
    return { view, mem: { stack: stack.slice(0, i), last: view } };
  }
  const view = rescaleView(current, from, to);
  return { view, mem: { stack: [...stack, { w: from.w, h: from.h, view: current }].slice(-8), last: view } };
}

/** The platform rectangle a viewport shows. */
export function visibleBounds(v: View, width: number, height: number): Bounds {
  const a = toWorld(v, 0, height);
  const b = toWorld(v, width, 0);
  return { minX: a.x, minY: a.y, maxX: b.x, maxY: b.y };
}

/** 1, 2 or 5 times a power of ten, the one nearest to `range / count`. */
export function niceStep(range: number, count: number): number {
  if (!(range > 0) || !(count > 0)) return 1;
  const raw = range / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const n = raw / mag;
  const f = n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10;
  return f * mag;
}

/** Multiples of `step` inside [min, max]. */
export function tickValues(min: number, max: number, step: number): number[] {
  if (!(step > 0) || !(max >= min)) return [];
  const out: number[] = [];
  const first = Math.ceil(min / step - 1e-9);
  const last = Math.floor(max / step + 1e-9);
  if (last - first > 2000) return out;
  for (let i = first; i <= last; i++) out.push(Math.round(i * step * 1e6) / 1e6 + 0); // + 0: no -0
  return out;
}

/** Holes are drawn only while neighbouring ones are far enough apart to be told apart. */
export function holesVisible(pitchMm: number, k: number): boolean {
  return pitchMm * k >= 6;
}

/** Whether the whole-pitch grid lines have room too (they take over where the holes thin out). */
export function minorGridVisible(pitchMm: number, k: number): boolean {
  return pitchMm * k >= 3;
}

/** A length for a label: "0.35 mm", "12.5 mm", "1.234 m". */
export function formatLength(mm: number): string {
  const a = Math.abs(mm);
  if (a >= 10_000) return `${(mm / 1000).toFixed(2)} m`;
  if (a >= 100) return `${mm.toFixed(1)} mm`;
  return `${mm.toFixed(2)} mm`;
}
