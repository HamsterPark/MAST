// Column-major 4×4 matrices as plain number arrays, the layout of three.js's Matrix4.elements, so the 3D
// view copies them over with fromArray. Only what the scene needs: compose a pose from a translation, a
// rotation and a size, multiply, transform points and directions. Pure (no three.js), so the mapping of a
// layout to the scene is tested without a browser (test/opticalLayout3dParts.test.ts).
//
// The world of the 3D view is the table's own frame: x and y in millimetres as on the 2D canvas, z up from
// the table top (docs/v2/design/optical_layout_3d_view.md section 2).

export type Vec3 = [number, number, number];
/** 16 numbers, column-major: element (row r, column c) is m[c * 4 + r]. */
export type Mat4 = number[];

export function identity(): Mat4 {
  return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
}

/** a · b (b is applied first). */
export function multiply(a: Mat4, b: Mat4): Mat4 {
  const out = new Array<number>(16);
  for (let c = 0; c < 4; c++) {
    for (let r = 0; r < 4; r++) {
      let s = 0;
      for (let k = 0; k < 4; k++) s += a[k * 4 + r]! * b[c * 4 + k]!;
      out[c * 4 + r] = s;
    }
  }
  return out;
}

/**
 * The rotation R = Rz · Ry · Rx (first about x, then about y, then about z, all about the fixed axes), as
 * a 3×3 row-major list.
 */
export function rotation3(rx: number, ry: number, rz: number): number[] {
  const cx = Math.cos(rx);
  const sx = Math.sin(rx);
  const cy = Math.cos(ry);
  const sy = Math.sin(ry);
  const cz = Math.cos(rz);
  const sz = Math.sin(rz);
  // Rz·Ry·Rx written out
  return [
    cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx,
    sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx,
    -sy, cy * sx, cy * cx,
  ];
}

/** T · R · S: scale by `size`, rotate (rotation3), then move to `at`. */
export function compose(at: Vec3, rot: Vec3 | undefined, size: Vec3): Mat4 {
  const r = rot ? rotation3(rot[0], rot[1], rot[2]) : [1, 0, 0, 0, 1, 0, 0, 0, 1];
  const [sx, sy, sz] = size;
  return [
    r[0]! * sx, r[3]! * sx, r[6]! * sx, 0,
    r[1]! * sy, r[4]! * sy, r[7]! * sy, 0,
    r[2]! * sz, r[5]! * sz, r[8]! * sz, 0,
    at[0], at[1], at[2], 1,
  ];
}

export function applyPoint(m: Mat4, p: Vec3): Vec3 {
  const [x, y, z] = p;
  return [m[0]! * x + m[4]! * y + m[8]! * z + m[12]!, m[1]! * x + m[5]! * y + m[9]! * z + m[13]!, m[2]! * x + m[6]! * y + m[10]! * z + m[14]!];
}

/** A direction: the translation does not apply. */
export function applyDir(m: Mat4, v: Vec3): Vec3 {
  const [x, y, z] = v;
  return [m[0]! * x + m[4]! * y + m[8]! * z, m[1]! * x + m[5]! * y + m[9]! * z, m[2]! * x + m[6]! * y + m[10]! * z];
}

export interface Box3 {
  min: Vec3;
  max: Vec3;
}

export function emptyBox(): Box3 {
  return { min: [Infinity, Infinity, Infinity], max: [-Infinity, -Infinity, -Infinity] };
}

export function isEmptyBox(b: Box3): boolean {
  return !(b.min[0] <= b.max[0] && b.min[1] <= b.max[1] && b.min[2] <= b.max[2]);
}

export function growBox(b: Box3, p: Vec3): void {
  for (let i = 0; i < 3; i++) {
    if (p[i]! < b.min[i]!) b.min[i] = p[i]!;
    if (p[i]! > b.max[i]!) b.max[i] = p[i]!;
  }
}

/** The 8 corners of the unit cube [-0.5, 0.5]³ under `m`: the box a part of extents `size` takes up. */
export function unitCubeCorners(m: Mat4): Vec3[] {
  const out: Vec3[] = [];
  for (const x of [-0.5, 0.5]) for (const y of [-0.5, 0.5]) for (const z of [-0.5, 0.5]) out.push(applyPoint(m, [x, y, z]));
  return out;
}

/** The 12 edges of a box, as pairs of corners (for an outline). */
export function boxEdges(b: Box3): Array<[Vec3, Vec3]> {
  const [x0, y0, z0] = b.min;
  const [x1, y1, z1] = b.max;
  const c = (i: number): Vec3 => [i & 1 ? x1 : x0, i & 2 ? y1 : y0, i & 4 ? z1 : z0];
  const pairs: Array<[number, number]> = [
    [0, 1], [2, 3], [4, 5], [6, 7],
    [0, 2], [1, 3], [4, 6], [5, 7],
    [0, 4], [1, 5], [2, 6], [3, 7],
  ];
  return pairs.map(([i, j]) => [c(i), c(j)]);
}

export const DEG = Math.PI / 180;
