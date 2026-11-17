// The preset views of the 3D view (top, front, side, isometric) and fitting the scene into one. Pure.
//
// The world is z-up (mat4.ts), so a camera's `up` stays (0, 0, 1) and OrbitControls orbits about z. Every
// view looks at the middle of what is drawn from just far enough to hold all of it:
//
//   top    straight down, +y up the screen and +x to the right (the 2D canvas's picture). The direction is a
//          hair off vertical: looking exactly along `up` leaves the camera's roll undefined.
//   front  from the front edge (y = 0) towards +y, level: +x to the right, z up.
//   side   from the right (+x) towards -x, level: +y to the right, z up.
//   iso    from the front left and above, at the isometric elevation (35.26°): the near corner is the
//          table's origin, +x runs up to the right and +y up to the left.
//
// "Just far enough" is exact for the box round the scene: every one of its 8 corners lands inside the picture
// (fitBoxDistance, a closed form), not only the sphere round it, which for a flat table leaves most of the
// picture empty.

import type { Box3, Vec3 } from "./mat4.ts";

export type ViewPreset = "top" | "front" | "side" | "iso";
export const VIEW_PRESETS: readonly ViewPreset[] = ["top", "front", "side", "iso"];

/** From the target towards the camera, unit length. */
export function presetDirection(preset: ViewPreset): Vec3 {
  switch (preset) {
    case "top": {
      const e = 1e-4;
      return [0, -e, Math.sqrt(1 - e * e)];
    }
    case "front":
      return [0, -1, 0];
    case "side":
      return [1, 0, 0];
    case "iso": {
      const k = 1 / Math.sqrt(3);
      return [-k, -k, k];
    }
  }
}

export interface CameraPose {
  position: Vec3;
  target: Vec3;
}

export function boxCentre(b: Box3): Vec3 {
  return [(b.min[0] + b.max[0]) / 2, (b.min[1] + b.max[1]) / 2, (b.min[2] + b.max[2]) / 2];
}

export function boxRadius(b: Box3): number {
  return Math.hypot(b.max[0] - b.min[0], b.max[1] - b.min[1], b.max[2] - b.min[2]) / 2;
}

const dot = (a: Vec3, b: Vec3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a: Vec3, b: Vec3): Vec3 => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const unit = (a: Vec3): Vec3 => {
  const n = Math.hypot(a[0], a[1], a[2]);
  return n > 0 ? [a[0] / n, a[1] / n, a[2] / n] : [1, 0, 0];
};

/** The screen's right and up of a camera that looks along -dir with z up (three's lookAt). */
export function screenAxes(dir: Vec3): { right: Vec3; up: Vec3 } {
  const f: Vec3 = [-dir[0], -dir[1], -dir[2]];
  const right = unit(cross(f, [0, 0, 1]));
  return { right, up: cross(right, f) };
}

function corners(b: Box3): Vec3[] {
  const out: Vec3[] = [];
  for (const x of [b.min[0], b.max[0]]) for (const y of [b.min[1], b.max[1]]) for (const z of [b.min[2], b.max[2]]) out.push([x, y, z]);
  return out;
}

/**
 * How far from the box's centre, along `dir`, a perspective camera (vertical field of view `fovDeg`, width / height
 * `aspect`) has to be for all 8 corners to be in the picture with `margin` to spare. A corner at `rel` from the
 * centre is `D - rel·dir` in front of the camera and `rel·right`, `rel·up` off its axis; it is inside when both
 * offsets are within depth · tan(half angle) / margin, so D = max over the corners of rel·dir + margin · max(...).
 */
export function fitBoxDistance(box: Box3, dir: Vec3, fovDeg: number, aspect: number, margin = 1.06): number {
  const c = boxCentre(box);
  const { right, up } = screenAxes(dir);
  const tv = Math.tan((fovDeg * Math.PI) / 360);
  const th = tv * Math.max(1e-3, aspect);
  let d = 1;
  for (const p of corners(box)) {
    const rel: Vec3 = [p[0] - c[0], p[1] - c[1], p[2] - c[2]];
    d = Math.max(d, dot(rel, dir) + margin * Math.max(Math.abs(dot(rel, right)) / th, Math.abs(dot(rel, up)) / tv), dot(rel, dir) + 1);
  }
  return d;
}

/** The camera of a preset that holds the whole box. */
export function presetPose(preset: ViewPreset, box: Box3, fovDeg: number, aspect: number): CameraPose {
  const target = boxCentre(box);
  const dir = presetDirection(preset);
  const d = fitBoxDistance(box, dir, fovDeg, aspect);
  return { target, position: [target[0] + dir[0] * d, target[1] + dir[1] * d, target[2] + dir[2] * d] };
}

/** The height (mm) an orthographic camera looking along -dir has to show for the whole box to fit, on a view of width / height `aspect`. */
export function orthoBoxSpan(box: Box3, dir: Vec3, aspect: number, margin = 1.06): number {
  const c = boxCentre(box);
  const { right, up } = screenAxes(dir);
  let h = 1;
  for (const p of corners(box)) {
    const rel: Vec3 = [p[0] - c[0], p[1] - c[1], p[2] - c[2]];
    h = Math.max(h, 2 * Math.abs(dot(rel, up)), (2 * Math.abs(dot(rel, right))) / Math.max(1e-3, aspect));
  }
  return h * margin;
}
