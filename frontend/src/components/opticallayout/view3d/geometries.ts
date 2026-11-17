// The canonical shapes of lib/opticalLayout/scene3d/parts.ts as three.js geometries: each fills the unit cube
// [-0.5, 0.5]³, the round ones along +z, so a part's matrix (its size, turn and place) is all an instance needs.
// Built once and shared by the live view and the GLB export.

import { BoxGeometry, BufferGeometry, CylinderGeometry, Float32BufferAttribute, LatheGeometry, SphereGeometry, TorusGeometry, Vector2 } from "three";
import { FRUSTUM_TOP, LENS_CAVE_CENTRE, LENS_VEX_EDGE, TORUS_TUBE, type Shape3 } from "../../../lib/opticalLayout/scene3d/parts.ts";

/**
 * How finely the round shapes are made. "low" is for the frames of a camera move on a software renderer
 * (engine.ts): about a third of the triangles, swapped in while the view turns and out when it stops.
 */
export type Detail = "high" | "low";

/** Segments round a round shape: a multiple of 4, so the shape reaches its full size along both axes. */
const ROUND: Record<Detail, number> = { high: 20, low: 8 };
/** Points of a lens's profile, per surface. */
const PROFILE: Record<Detail, number> = { high: 6, low: 3 };

/** three's cylinders and lathes run along y: turn them to run along z (+y goes to +z). */
function alongZ<T extends BufferGeometry>(g: T): T {
  g.rotateX(Math.PI / 2);
  return g;
}

/** A lens profile: thickness `centre` on the axis and `rim` at the edge, both of the unit height, a parabola between. */
function lensProfile(centre: number, rim: number, n: number): Vector2[] {
  const pts: Vector2[] = [];
  // the front surface from the axis out to the rim, then the back surface from the rim back to the axis
  for (let i = 0; i <= n; i++) {
    const r = (0.5 * i) / n;
    const t = (r / 0.5) ** 2;
    pts.push(new Vector2(r, (centre + (rim - centre) * t) / 2));
  }
  for (let i = n; i >= 0; i--) {
    const r = (0.5 * i) / n;
    const t = (r / 0.5) ** 2;
    pts.push(new Vector2(r, -(centre + (rim - centre) * t) / 2));
  }
  // LatheGeometry turns the profile about y with the points given from top to bottom (y falling)
  return pts.map((p) => new Vector2(Math.max(1e-4, p.x), p.y)).reverse();
}

/** The triangular prism: (-0.5, -0.5), (-0.5, 0.5), (0.5, 0) in x and y, from z = -0.5 to 0.5, flat-shaded. */
function prismGeometry(): BufferGeometry {
  const a = [-0.5, -0.5];
  const b = [0.5, 0];
  const c = [-0.5, 0.5];
  const v = (p: number[], z: number) => [p[0]!, p[1]!, z];
  const tris: number[][] = [
    // top and bottom (counter-clockwise seen from outside)
    [...v(a, 0.5), ...v(b, 0.5), ...v(c, 0.5)],
    [...v(a, -0.5), ...v(c, -0.5), ...v(b, -0.5)],
  ];
  const quad = (p: number[], q: number[]) => {
    tris.push([...v(p, -0.5), ...v(q, -0.5), ...v(q, 0.5)]);
    tris.push([...v(p, -0.5), ...v(q, 0.5), ...v(p, 0.5)]);
  };
  quad(a, b);
  quad(b, c);
  quad(c, a);
  const g = new BufferGeometry();
  g.setAttribute("position", new Float32BufferAttribute(tris.flat(), 3));
  g.computeVertexNormals();
  return g;
}

function build(shape: Shape3, detail: Detail): BufferGeometry {
  const round = ROUND[detail];
  switch (shape) {
    case "box":
      return new BoxGeometry(1, 1, 1);
    case "cyl":
      return alongZ(new CylinderGeometry(0.5, 0.5, 1, round, 1));
    case "frustum":
      return alongZ(new CylinderGeometry(0.5 * FRUSTUM_TOP, 0.5, 1, round, 1));
    case "cone":
      return alongZ(new CylinderGeometry(0, 0.5, 1, round, 1));
    case "lensVex":
      return alongZ(new LatheGeometry(lensProfile(1, LENS_VEX_EDGE, PROFILE[detail]), round));
    case "lensCave":
      return alongZ(new LatheGeometry(lensProfile(LENS_CAVE_CENTRE, 1, PROFILE[detail]), round));
    case "prism":
      return prismGeometry();
    case "torus": {
      // outer diameter 1, a tube TORUS_TUBE wide, then stretched along z to the unit height
      const tube = TORUS_TUBE / 2;
      const g = new TorusGeometry(0.5 - tube, tube, detail === "high" ? 8 : 4, round);
      g.scale(1, 1, 1 / (2 * tube));
      return g;
    }
    case "sphere":
      return detail === "high" ? new SphereGeometry(0.5, 16, 10) : new SphereGeometry(0.5, 8, 6);
  }
}

const cache = new Map<string, BufferGeometry>();

/** The shared geometry of a canonical shape (bounding box and sphere computed, for picking). */
export function shapeGeometry(shape: Shape3, detail: Detail = "high"): BufferGeometry {
  const key = shape + ":" + detail;
  let g = cache.get(key);
  if (!g) {
    g = build(shape, detail);
    g.computeBoundingBox();
    g.computeBoundingSphere();
    cache.set(key, g);
  }
  return g;
}

/**
 * A beam tube: radius 1, from z = 0 to z = 1, open at its ends (a tube two or three pixels wide shows no ends,
 * and thousands of them cost half the vertices without). Eight sides: the width on the screen stays within 8 %
 * of the 2D line's whichever way the tube is turned. An instance stretches it along z to the length of its
 * segment; the beam material sets the radius on the screen (materials.ts).
 */
export function tubeGeometry(): BufferGeometry {
  const g = new CylinderGeometry(1, 1, 1, 8, 1, true);
  g.translate(0, 0.5, 0);
  g.rotateX(Math.PI / 2);
  g.computeBoundingSphere();
  return g;
}
