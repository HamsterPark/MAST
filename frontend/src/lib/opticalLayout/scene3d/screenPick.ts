// Which drawn beam a click in the 3D view is on: the beams are thin lines on the screen (their width is the 2D
// line's, materials.ts), so they are picked like the 2D canvas picks them, by the distance on the screen
// (LayoutCanvas BEAM_PICK_PX), not by a ray that would have to hit a tube a pixel or two wide. Pure.

import type { Vec3 } from "./mat4.ts";

export interface ScreenSeg {
  ax: number;
  ay: number;
  bx: number;
  by: number;
}

export interface ScreenHit {
  /** index of the segment in the list */
  index: number;
  /** where along it (0 at a, 1 at b), on the screen */
  t: number;
  /** pixels from the pointer */
  dist: number;
}

/** The segment nearest to (px, py) within `maxPx`; of two as near, the earlier one. Null segments are skipped. */
export function nearestOnScreen(segs: ReadonlyArray<ScreenSeg | null>, px: number, py: number, maxPx: number): ScreenHit | null {
  let best: ScreenHit | null = null;
  segs.forEach((s, index) => {
    if (!s) return;
    const dx = s.bx - s.ax;
    const dy = s.by - s.ay;
    const L2 = dx * dx + dy * dy;
    let t = L2 > 0 ? ((px - s.ax) * dx + (py - s.ay) * dy) / L2 : 0;
    t = Math.min(1, Math.max(0, t));
    const dist = Math.hypot(px - (s.ax + dx * t), py - (s.ay + dy * t));
    if (dist > maxPx) return;
    if (best === null || dist < best.dist - 1e-9) best = { index, t, dist };
  });
  return best;
}

/** What a drawn beam tube is for picking: its two ends in the table frame; a vertical joint is never picked. */
export interface PickableTube {
  a: Vec3;
  b: Vec3;
  joint: boolean;
}

/**
 * The tube under the pointer at (px, py), within `maxPx` on the screen: every tube is projected with `toScreen` (null
 * when an end is behind the camera) and the nearest wins (nearestOnScreen). The point on it is the screen parameter
 * taken back along the tube (exact for an orthographic camera, close for a perspective one): the editor's beam pick
 * (2D rule: the nearest segment to a point of the table) then finds this very segment.
 */
export function pickTube<T extends PickableTube>(tubes: readonly T[], toScreen: (p: Vec3) => [number, number] | null, px: number, py: number, maxPx: number): { tube: T; index: number; point: Vec3 } | null {
  const segs: Array<ScreenSeg | null> = tubes.map((t) => {
    if (t.joint) return null;
    const a = toScreen(t.a);
    const b = toScreen(t.b);
    return a && b ? { ax: a[0], ay: a[1], bx: b[0], by: b[1] } : null;
  });
  const hit = nearestOnScreen(segs, px, py, maxPx);
  if (!hit) return null;
  const t = tubes[hit.index]!;
  return { tube: t, index: hit.index, point: [t.a[0] + (t.b[0] - t.a[0]) * hit.t, t.a[1] + (t.b[1] - t.a[1]) * hit.t, t.a[2] + (t.b[2] - t.a[2]) * hit.t] };
}
