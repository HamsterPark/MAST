// Dash patterns as geometry, for the figure export.
//
// Some programs that read an SVG ignore stroke-dasharray (MuPDF, and with it the PDFs and previews made with
// it, draws a dashed line solid): a "no light" beam, an infrared one, an open shutter would all look like plain
// lines. The figure draws its dashes as separate short segments instead, which every viewer draws alike. The
// canvas keeps stroke-dasharray (cheaper, and the browser draws it).

import type { Vec } from "./types.ts";

/** At most this many dashes in one path; past it the caller keeps stroke-dasharray. */
export const MAX_DASHES = 20000;

/** "1.5 3.5" → [1.5, 3.5]; an odd count is repeated, as SVG does; null for no usable pattern. */
export function parseDash(dash: string | null | undefined): number[] | null {
  if (!dash) return null;
  const v = dash
    .trim()
    .split(/[\s,]+/)
    .map(Number);
  if (v.length === 0 || v.some((x) => !Number.isFinite(x) || x < 0)) return null;
  if (!(v.reduce((a, b) => a + b, 0) > 0)) return null;
  return v.length % 2 === 1 ? [...v, ...v] : v;
}

/**
 * The dashes of `pattern` (dash, gap, dash, gap, ...) laid along the polyline `pts`, the pattern multiplied by
 * `scale` (1 / k writes screen pixels in millimetres), as path data: one subpath per dash, a dash that runs round
 * a corner keeps the corner. `digits`: decimals written. null when the line would need more than MAX_DASHES.
 */
export function dashedPath(pts: readonly Vec[], pattern: readonly number[], scale = 1, digits = 2): string | null {
  const pat = pattern.map((v) => v * scale);
  const period = pat.reduce((a, b) => a + b, 0);
  if (pts.length < 2 || !(period > 0) || pat.length % 2 !== 0) return "";
  let length = 0;
  for (let s = 0; s + 1 < pts.length; s++) length += Math.hypot(pts[s + 1]!.x - pts[s]!.x, pts[s + 1]!.y - pts[s]!.y);
  if ((length / period) * (pat.length / 2) > MAX_DASHES) return null;
  const f = 10 ** digits;
  const r = (v: number) => (Math.round(v * f) / f + 0).toString();
  const out: string[] = [];
  let i = 0;
  let left = pat[0]!;
  let open = false;
  for (let s = 0; s + 1 < pts.length; s++) {
    const a = pts[s]!;
    const b = pts[s + 1]!;
    const L = Math.hypot(b.x - a.x, b.y - a.y);
    if (!(L > 0)) continue;
    const ux = (b.x - a.x) / L;
    const uy = (b.y - a.y) / L;
    let t = 0;
    while (t < L - 1e-12) {
      const step = Math.min(left, L - t);
      if (i % 2 === 0) {
        // a dash: start it here, or carry it on (round a corner) from the segment before
        if (!open) {
          out.push(`M${r(a.x + ux * t)} ${r(a.y + uy * t)}`);
          open = true;
        }
        out.push(`L${r(a.x + ux * (t + step))} ${r(a.y + uy * (t + step))}`);
      }
      t += step;
      left -= step;
      if (left <= 1e-12) {
        // this dash or gap is used up: the next one
        i = (i + 1) % pat.length;
        left = pat[i]!;
        if (i % 2 === 1) open = false;
      }
    }
  }
  return out.join("");
}

/** The points of path data made only of absolute M and L commands (the canvas's beam lines), or null. */
export function polylineOf(d: string): Vec[] | null {
  const pts: Vec[] = [];
  const re = /([ML])\s*(-?[\d.]+(?:e-?\d+)?)[\s,]+(-?[\d.]+(?:e-?\d+)?)/gy;
  let m: RegExpExecArray | null;
  let at = 0;
  while ((m = re.exec(d)) !== null) {
    if (m[1] === "M" && pts.length > 0) return null;
    pts.push({ x: Number(m[2]), y: Number(m[3]) });
    at = re.lastIndex;
  }
  return at === d.length && pts.length >= 2 ? pts : null;
}
