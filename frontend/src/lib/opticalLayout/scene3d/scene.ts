// A layout as the 3D view draws it: every component placed in the table's frame (x and y from the layout, z
// from heights.ts, turned about z by its angle) with its parts (parts.ts), and the instances those parts make
// in the view's instanced meshes, one mesh per (shape, material). Pure: the view copies the matrices into
// three.js as they are (test/opticalLayout3dScene.test.ts compares them with three's own composition).

import type { LayoutComponent, OpticalLayout, TableSpec } from "../types.ts";
import { componentHeight, periscopeOutHeight } from "./heights.ts";
import { partMatrix, partsBox, partsOf, type Mat3, type Part, type Shape3, type Tint } from "./parts.ts";
import { applyPoint, boxEdges, compose, emptyBox, growBox, multiply, DEG, type Box3, type Mat4, type Vec3 } from "./mat4.ts";

export interface ComponentNode {
  id: string;
  component: LayoutComponent;
  /** height of the optical axis above the table top, mm */
  z: number;
  /** a periscope's output height (the others: z) */
  zOut: number;
  /** local → table frame */
  frame: Mat4;
  parts: Part[];
  /** in the local frame, the stands left out (what the selection outline goes round) */
  box: Box3;
}

/** The local frame of a component in the table's: at (x, y, z), its local x along `angle_deg`. */
export function componentFrame(c: Pick<LayoutComponent, "x_mm" | "y_mm" | "angle_deg">, z: number): Mat4 {
  return compose([c.x_mm, c.y_mm, z], [0, 0, c.angle_deg * DEG], [1, 1, 1]);
}

export function componentNode(c: LayoutComponent, table: TableSpec): ComponentNode {
  const z = componentHeight(c, table);
  const zOut = c.type === "periscope" ? periscopeOutHeight(c, table) : z;
  const parts = partsOf(c, { z, zOut });
  return { id: c.id, component: c, z, zOut, frame: componentFrame(c, z), parts, box: partsBox(parts) };
}

export function sceneComponents(layout: OpticalLayout): ComponentNode[] {
  return layout.components.map((c) => componentNode(c, layout.table));
}

/** The world box of a node (its local box turned and moved). */
export function worldBox(n: ComponentNode): Box3 {
  const b = emptyBox();
  for (const [p, q] of boxEdges(n.box)) {
    growBox(b, applyPoint(n.frame, p));
    growBox(b, applyPoint(n.frame, q));
  }
  return b;
}

/** The 12 edges of a node's outline, in the table frame. */
export function outlineEdges(n: ComponentNode, pad = 1.5): Array<[Vec3, Vec3]> {
  const box: Box3 = { min: [n.box.min[0] - pad, n.box.min[1] - pad, n.box.min[2] - pad], max: [n.box.max[0] + pad, n.box.max[1] + pad, n.box.max[2] + pad] };
  return boxEdges(box).map(([p, q]) => [applyPoint(n.frame, p), applyPoint(n.frame, q)]);
}

export type BucketKey = `${Shape3}:${Mat3}`;

export interface Instance {
  /** the component it belongs to */
  id: string;
  /** index of the part in its component's list */
  part: number;
  matrix: Mat4;
  tint: Tint;
  wl?: number;
  density?: number;
}

/** Every part of every component, sorted into the instanced meshes that draw them. */
export function instanceBuckets(nodes: readonly ComponentNode[]): Map<BucketKey, Instance[]> {
  const out = new Map<BucketKey, Instance[]>();
  for (const n of nodes) {
    n.parts.forEach((p, i) => {
      const key: BucketKey = `${p.shape}:${p.mat}`;
      const inst: Instance = { id: n.id, part: i, matrix: multiply(n.frame, partMatrix(p)), tint: p.tint };
      if (p.wl !== undefined) inst.wl = p.wl;
      if (p.density !== undefined) inst.density = p.density;
      const list = out.get(key);
      if (list) list.push(inst);
      else out.set(key, [inst]);
    });
  }
  return out;
}

/** Where a component's label sits: just above the top of its outline, over its centre. */
export function labelAnchor(n: ComponentNode): Vec3 {
  return [n.component.x_mm, n.component.y_mm, n.z + Math.max(n.box.max[2], 4) + 4];
}
