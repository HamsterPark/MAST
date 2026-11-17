// What a click and a drag in the 3D view mean, as plain math: where the pointer's ray meets a level plane,
// where a dragged component goes (the 2D canvas's rule, with its snapping), what a click does to the selection
// (the 2D canvas's rule too), how many millimetres a pixel is at some distance from the camera. Pure, so it is
// tested without a browser (test/opticalLayout3dPick.test.ts).
//
// A drag in 3D moves components across the table, never up or down: the pointer's ray is cut with the level
// plane through the point where the component was grabbed, and the component follows that point. The rest is
// exactly the 2D canvas's drag (LayoutCanvas.tsx onMove): the lead component's new place is snapped with
// geometry.ts snapPoint in the editor's snap mode (Ctrl or Cmd held: no snapping), the others move by the same
// amount, locked ones are not dragged at all. The editor receives positions as from the 2D canvas, previews them
// through the same throttle and commits them as one undo step (edit.ts setPositions).

import type { OpticalLayout, TableSpec, Vec } from "../types.ts";
import { snapPoint, type SnapMode } from "../geometry.ts";
import type { Vec3 } from "./mat4.ts";

/** Below this |direction z| a ray runs level with the plane: no point (it would be at the horizon). */
export const LEVEL_EPS = 1e-6;
/** Rays that meet the plane further away than this (mm) are treated as missing it (a ray close to the horizon). */
export const FAR_MM = 1e6;

/** Where the ray origin + t · dir (t > 0) meets the level plane z = h; null when it does not (or too far out). */
export function rayAtHeight(origin: Vec3, dir: Vec3, h: number): Vec3 | null {
  if (Math.abs(dir[2]) < LEVEL_EPS) return null;
  const t = (h - origin[2]) / dir[2];
  if (!(t > 0)) return null;
  const p: Vec3 = [origin[0] + dir[0] * t, origin[1] + dir[1] * t, h];
  if (Math.hypot(p[0] - origin[0], p[1] - origin[1]) > FAR_MM) return null;
  return p;
}

/** A drag that started: the components it moves (where they were) and where the pointer met its plane. */
export interface Drag3 {
  /** the component under the pointer: its place is the one that is snapped */
  lead: string;
  /** the unlocked components that move, and where they were */
  origin: Map<string, Vec>;
  /** where the pointer's ray met the drag plane when the drag started */
  start: Vec;
  /** the height of the drag plane (where the component was grabbed), mm */
  planeZ: number;
}

/**
 * The components a press on `id` drags, given the selection after the press: the selected ones that are not
 * locked. Null when nothing moves (the component is locked or not there): the editor says why (onLockedDrag).
 */
export function startDrag(layout: OpticalLayout, selection: readonly string[], id: string, start: Vec, planeZ: number): Drag3 | null {
  const comp = layout.components.find((c) => c.id === id);
  if (!comp || comp.locked) return null;
  const origin = new Map<string, Vec>();
  for (const c of layout.components) if (selection.includes(c.id) && !c.locked) origin.set(c.id, { x: c.x_mm, y: c.y_mm });
  if (!origin.has(id)) return null;
  return { lead: id, origin, start, planeZ };
}

/**
 * Where the dragged components go when the pointer is at `now` on the drag plane (LayoutCanvas.tsx onMove): the
 * lead moves with the pointer and is snapped (`mode` "off": not), the others keep their offsets to it.
 */
export function dragPositions(d: Drag3, now: Vec, table: TableSpec, mode: SnapMode): Map<string, Vec> {
  const lead = d.origin.get(d.lead);
  const positions = new Map<string, Vec>();
  if (!lead) return positions;
  let target = { x: lead.x + (now.x - d.start.x), y: lead.y + (now.y - d.start.y) };
  if (mode !== "off") target = snapPoint(table, target, mode);
  const dx = target.x - lead.x;
  const dy = target.y - lead.y;
  for (const [id, o] of d.origin) positions.set(id, { x: o.x + dx, y: o.y + dy });
  return positions;
}

/** The snap mode of a drag: the editor's, or none while Ctrl / Cmd is held (as on the 2D canvas). */
export function dragSnapMode(snap: SnapMode, mods: { ctrlKey?: boolean; metaKey?: boolean }): SnapMode {
  return mods.ctrlKey || mods.metaKey ? "off" : snap;
}

/**
 * The selection right after a press on a component (LayoutCanvas.tsx handlers.onDown): with Shift, Ctrl or Cmd
 * the component is added or taken out; otherwise a component that is already selected keeps the selection as it
 * is (so a group can be dragged), and any other one becomes the selection.
 */
export function selectionOnPress(selected: readonly string[], id: string, additive: boolean): string[] {
  if (additive) return selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id];
  return selected.includes(id) ? [...selected] : [id];
}

/**
 * The selection when the press ends without a drag (LayoutCanvas.tsx onUp): a plain click on one member of a
 * larger selection selects that member alone; otherwise null (the selection stays).
 */
export function selectionOnClick(selected: readonly string[], id: string, additive: boolean): string[] | null {
  return !additive && selected.length > 1 && selected.includes(id) ? [id] : null;
}

/** How far (px) the pointer moves before a press becomes a drag (the 2D canvas's threshold). */
export const DRAG_THRESHOLD_PX = 3;

/**
 * Millimetres per screen pixel at distance `dist` (mm) in front of a perspective camera with a vertical field of
 * view of `fovDeg`, on a view `heightPx` pixels tall.
 */
export function mmPerPxPerspective(dist: number, fovDeg: number, heightPx: number): number {
  return (2 * dist * Math.tan((fovDeg * Math.PI) / 360)) / Math.max(1, heightPx);
}

/** Millimetres per screen pixel of an orthographic camera whose view is `spanMm` tall (top - bottom, over zoom). */
export function mmPerPxOrthographic(spanMm: number, heightPx: number): number {
  return spanMm / Math.max(1, heightPx);
}
