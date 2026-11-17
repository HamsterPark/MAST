// The 3D shapes of the components, as data: each type is a short list of simple solids (boxes, cylinders,
// lenses, a frustum, a cone, a prism, a ring, a ball) in the component's LOCAL frame, the same frame the 2D
// drawings use (glyphs.ts), raised into the third dimension:
//
//   x  along `angle_deg` (the normal of a mirror, the axis of a lens, the emission direction of a source,
//      the direction a receiving face looks to),
//   y  along the active surface (the face is the segment x = 0, -a/2 <= y <= a/2 on the 2D canvas),
//   z  straight up, 0 on the component's optical axis (so the table top is at z = -height).
//
// Every type a ray can meet has exactly one part with `role: "face"` that holds the active surface: the
// front of a mirror, a receiver or a blocker ends exactly at x = 0, the mid-plane of a transmitting optic is
// x = 0, and the face spans the whole aperture in y. Light sources have a part with `role: "port"` instead,
// whose front is the emission point x = 0. test/opticalLayout3dParts.test.ts checks both for every type, so
// the 3D drawing can be read like the 2D one. The posts, holders, bases and pedestals that carry a part down
// to the table have `role: "stand"`.
//
// Each solid is one of a few canonical shapes that fill the unit cube [-0.5, 0.5]³ (the round ones along
// +z); a part is the canonical shape scaled to `size`, turned by `rot` and moved to `at`. The 3D view draws
// every (shape, material) pair as ONE instanced mesh, however many components there are
// (docs/v2/design/optical_layout_3d_view.md section 3). Dimensions follow the 2D drawings where those have
// them (a laser is 40 × 16 mm, a mirror's substrate as thick as its 2D hatching) and catalogue parts where
// they do not (a Ø½" post, a Ø25 mm post holder). Pure, no three.js.

import type { LayoutComponent } from "../types.ts";
import { dichroicHue, filterHue, ndDensity, sampleHasTip, sourceWavelengths } from "../glyphs.ts";
import { compose, emptyBox, growBox, unitCubeCorners, DEG, type Box3, type Mat4, type Vec3 } from "./mat4.ts";

export const SHAPES = ["box", "cyl", "frustum", "cone", "lensVex", "lensCave", "prism", "torus", "sphere"] as const;
export type Shape3 = (typeof SHAPES)[number];

/** "glass" parts are drawn see-through. */
export const MATERIALS = ["solid", "glass"] as const;
export type Mat3 = (typeof MATERIALS)[number];

/** What a part is made of, for its colour (palette.ts gives each a colour per theme). */
export const TINTS = [
  "base",
  "post",
  "mount",
  "body",
  "metal",
  "mirror",
  "grating",
  "glass",
  "window",
  "polarizer",
  "coating",
  "pbs",
  "dark",
  "sensor",
  "ink",
  "port",
  "sample",
  "lamp",
  "housing",
  "wl",
  "nd",
] as const;
export type Tint = (typeof TINTS)[number];

export type PartRole = "face" | "port" | "stand";

export interface Part {
  /** what it is, for the GLB export's node names */
  name: string;
  shape: Shape3;
  mat: Mat3;
  tint: Tint;
  /** the wavelength (nm) whose colour a "wl" tint takes */
  wl?: number;
  /** how dark an ND filter is, 0..1 (glyphs.ts ndDensity) */
  density?: number;
  /** centre in the component's local frame, mm */
  at: Vec3;
  /** extents along the canonical axes before the rotation, mm */
  size: Vec3;
  /** radians: first about x, then y, then z (mat4.ts rotation3) */
  rot?: Vec3;
  role?: PartRole;
}

// ── the canonical shapes (the 3D view builds the same ones; test/opticalLayout3dScene.test.ts checks) ──

/** The narrow end of the frustum, as a fraction of its wide end (both diameters). */
export const FRUSTUM_TOP = 0.4;
/** A bi-convex lens is this thick at its rim, as a fraction of its centre. */
export const LENS_VEX_EDGE = 0.25;
/** A bi-concave lens is this thick at its centre, as a fraction of its rim. */
export const LENS_CAVE_CENTRE = 0.3;
/** The ring: radial width of its tube, as a fraction of its outer diameter. */
export const TORUS_TUBE = 0.18;

// ── mounts ────────────────────────────────────────────────────────────────────

/** A Ø½" post, a Ø25 mm post holder at most 30 mm tall, a base plate 25 × 50 × 8 mm. */
export const POST_D = 12.7;
export const HOLDER_D = 25;
export const HOLDER_H = 30;
export const BASE_SIZE: Vec3 = [25, 50, 8];

const SMALL = 0.05;
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
const pos = (v: number) => Math.max(SMALL, v);

type Extra = Partial<Pick<Part, "rot" | "wl" | "density" | "role">>;

function part(name: string, shape: Shape3, mat: Mat3, tint: Tint, at: Vec3, size: Vec3, extra: Extra = {}): Part {
  return { name, shape, mat, tint, at, size: [pos(size[0]), pos(size[1]), pos(size[2])], ...extra };
}

/** canonical +z turned to local +x (rotation about y by +90°), and to local -x */
const TO_PLUS_X: Vec3 = [0, Math.PI / 2, 0];
const TO_MINUS_X: Vec3 = [0, -Math.PI / 2, 0];

/**
 * A round part along the local x axis from x0 to x1, of diameter d. A frustum or a cone narrows towards +x
 * (`narrowTo` 1) or towards -x (-1).
 */
function alongX(name: string, shape: Shape3, mat: Mat3, tint: Tint, x0: number, x1: number, d: number, extra: Extra = {}, narrowTo: 1 | -1 = 1): Part {
  const lo = Math.min(x0, x1);
  const hi = Math.max(x0, x1);
  return part(name, shape, mat, tint, [(lo + hi) / 2, 0, 0], [d, d, hi - lo], { rot: narrowTo === 1 ? TO_PLUS_X : TO_MINUS_X, ...extra });
}

const box = (name: string, tint: Tint, at: Vec3, size: Vec3, extra: Extra = {}): Part => part(name, "box", "solid", tint, at, size, extra);

/**
 * A post, a post holder and a base under the point (x, y) of a part whose lowest point is `top` (local z),
 * standing on the table at local z = -z. Nothing when there is no room (a beam height lower than the part).
 */
export function stand(x: number, y: number, top: number, z: number): Part[] {
  const floor = -z;
  const room = top - floor;
  if (!(room > 1)) return [];
  const baseH = Math.min(BASE_SIZE[2], room * 0.25);
  const out: Part[] = [box("base", "base", [x, y, floor + baseH / 2], [BASE_SIZE[0], BASE_SIZE[1], baseH], { role: "stand" })];
  const holderH = Math.min(HOLDER_H, (room - baseH) * 0.45);
  if (holderH > 2) out.push(part("post holder", "cyl", "solid", "mount", [x, y, floor + baseH + holderH / 2], [HOLDER_D, HOLDER_D, holderH], { role: "stand" }));
  const postLen = room - baseH;
  out.push(part("post", "cyl", "solid", "post", [x, y, floor + baseH + postLen / 2], [POST_D, POST_D, postLen], { role: "stand" }));
  return out;
}

/** A block from the table up to `top` under a housing that spans x0..x1 and is `width` wide. */
function pedestal(x0: number, x1: number, width: number, top: number, z: number): Part[] {
  const floor = -z;
  const room = top - floor;
  if (!(room > 0.5)) return [];
  const len = Math.abs(x1 - x0);
  return [box("pedestal", "base", [(x0 + x1) / 2, 0, floor + room / 2], [len * 0.8, width * 0.9, room], { role: "stand" })];
}

/** A ring mount round a round optic of diameter d, `depth` thick along the axis. */
function ring(d: number, depth: number, x = 0): Part {
  return alongX("ring mount", "torus", "solid", "mount", x - depth / 2, x + depth / 2, d);
}

/** A bar on the face of a disc along the axis `deg` (from local +y towards straight up; v2 section 4.5). */
function axisBar(x: number, d: number, deg: number, offset = 0, tint: Tint = "ink"): Part {
  const t = deg * DEG;
  // rotating about local x by t turns +y towards +z: the bar runs along cos t · y + sin t · z
  return box("axis", tint, [x, -Math.sin(t) * offset, Math.cos(t) * offset], [0.5, d, Math.max(0.6, d * 0.05)], { rot: [t, 0, 0] });
}

function numProp(c: LayoutComponent, key: string): number | null {
  const v = c.props[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export interface PartContext {
  /** height of the component's optical axis above the table top, mm (heights.ts componentHeight) */
  z: number;
  /** a periscope's output height above the table top, mm (heights.ts periscopeOutHeight) */
  zOut: number;
}

/** A periscope whose output is less than this higher or lower than its input does not change the height. */
export const PERISCOPE_MIN_RISE_MM = 1;

/** The parts of a component in its local frame. */
export function partsOf(c: LayoutComponent, ctx: PartContext): Part[] {
  const a = c.aperture_mm;
  const h = a / 2;
  const z = ctx.z;

  // `as string`: a type this build does not know still gets the default plate
  switch (c.type as string) {
    case "mirror": {
      const t = clamp(a * 0.14, 1.4, 5);
      const side = a + 10;
      return [
        alongX("mirror", "cyl", "solid", "mirror", -t, 0, a, { role: "face" }),
        box("mirror mount", "mount", [-t - 4, 0, 0], [8, side, side]),
        ...stand(-t - 4, 0, -side / 2, z),
      ];
    }

    case "grating": {
      const t = clamp(a * 0.12, 1.4, 4);
      const side = a + 10;
      return [box("grating", "grating", [-t / 2, 0, 0], [t, a, a * 0.8], { role: "face" }), box("grating mount", "mount", [-t - 4, 0, 0], [8, side, side * 0.85]), ...stand(-t - 4, 0, -side * 0.425, z)];
    }

    case "dichroic": {
      const t = clamp(a * 0.1, 1.2, 3);
      const hue = dichroicHue(c);
      return [
        part("substrate", "box", "glass", "glass", [-t / 2, 0, 0], [t, a, a]),
        hue === null ? box("coating", "coating", [-0.2, 0, 0], [0.4, a, a], { role: "face" }) : box("coating", "wl", [-0.2, 0, 0], [0.4, a, a], { role: "face", wl: hue }),
        box("clamp", "mount", [-t / 2, 0, -h - 3], [t + 8, a * 0.6, 6]),
        ...stand(-t / 2, 0, -h - 6, z),
      ];
    }

    case "beam_splitter":
    case "pbs": {
      // a cube turned 45° on the table, the coating on its diagonal (the 2D diamond)
      const s = h * Math.SQRT2;
      const pbs = c.type === "pbs";
      return [
        part(pbs ? "polarizing cube" : "beam splitter cube", "box", "glass", "glass", [0, 0, 0], [s, s, s], { rot: [0, 0, Math.PI / 4] }),
        box("coating", pbs ? "pbs" : "coating", [0, 0, 0], [0.4, a, s], { role: "face" }),
        box("platform", "mount", [0, 0, -s / 2 - 2], [a + 4, a + 4, 4]),
        ...stand(0, 0, -s / 2 - 4, z),
      ];
    }

    case "lens": {
      const f = numProp(c, "focal_mm");
      const ringD = a + 6;
      const optic =
        f !== null && f < 0
          ? alongX("lens", "lensCave", "glass", "glass", -clamp(a * 0.13, 1.25, 5), clamp(a * 0.13, 1.25, 5), a, { role: "face" })
          : alongX("lens", "lensVex", "glass", "glass", -clamp(a * 0.11, 1, 5), clamp(a * 0.11, 1, 5), a, { role: "face" });
      return [optic, ring(ringD, 5), ...stand(0, 0, -ringD / 2, z)];
    }

    case "objective": {
      // the barrel narrows towards +x (the nose that faces the sample); its entrance is the active surface
      const B = Math.max(h + 1.6, 5.5);
      const L = clamp(B * 3.4, 18, 44);
      const x0 = -0.5 * L;
      const x1 = 0.06 * L;
      const x3 = 0.46 * L;
      return [
        alongX("thread", "cyl", "solid", "mount", x0 - 0.08 * L, x0, 1.44 * B),
        alongX("barrel", "cyl", "solid", "body", x0, x1, 2 * B),
        alongX("nose", "frustum", "solid", "body", x1, x3, 2 * B),
        alongX("front lens", "cyl", "glass", "glass", x3 - 0.4, x3 + 0.2, 0.6 * B),
        alongX("entrance", "cyl", "glass", "glass", -0.3, 0.3, a, { role: "face" }),
        box("objective holder", "mount", [x0 * 0.5, 0, -B - 3], [L * 0.45, 2 * B, 6]),
        ...stand(x0 * 0.5, 0, -B - 6, z),
      ];
    }

    case "waveplate": {
      const t = clamp(a * 0.06, 0.7, 1.6);
      const fast = numProp(c, "fast_axis_deg") ?? 0;
      const ringD = a + 10;
      return [alongX("waveplate", "cyl", "glass", "glass", -t, t, a, { role: "face" }), axisBar(t + 0.3, a * 0.92, fast), ring(ringD, 8), ...stand(0, 0, -ringD / 2, z)];
    }

    case "polarizer": {
      const t = clamp(a * 0.07, 0.8, 1.8);
      const axis = numProp(c, "axis_deg") ?? 0;
      const ringD = a + 10;
      // parallel chords along the transmission axis, as on the 2D dial
      return [
        alongX("polarizer", "cyl", "glass", "polarizer", -t, t, a, { role: "face" }),
        axisBar(t + 0.3, a * 0.92, axis),
        axisBar(t + 0.3, a * 0.72, axis, a * 0.26),
        axisBar(t + 0.3, a * 0.72, axis, -a * 0.26),
        ring(ringD, 8),
        ...stand(0, 0, -ringD / 2, z),
      ];
    }

    case "filter": {
      const t = clamp(a * 0.08, 1, 2.2);
      const hue = filterHue(c);
      const ringD = a + 6;
      const optic = hue === null ? alongX("filter", "cyl", "glass", "glass", -t, t, a, { role: "face" }) : alongX("filter", "cyl", "glass", "wl", -t, t, a, { role: "face", wl: hue });
      return [optic, ring(ringD, 5), ...stand(0, 0, -ringD / 2, z)];
    }

    case "window": {
      const t = clamp(a * 0.05, 0.7, 1.5);
      const ringD = a + 6;
      return [alongX("window", "cyl", "glass", "window", -t, t, a, { role: "face" }), ring(ringD, 5), ...stand(0, 0, -ringD / 2, z)];
    }

    case "nd": {
      const t = clamp(a * 0.08, 1, 2.2);
      const ringD = a + 6;
      return [alongX("ND filter", "cyl", "glass", "nd", -t, t, a, { role: "face", density: ndDensity(numProp(c, "od")) }), ring(ringD, 5), ...stand(0, 0, -ringD / 2, z)];
    }

    case "aotf": {
      const w = a * 0.26;
      const H = a * 0.7;
      return [
        part("crystal", "box", "glass", "glass", [0, 0, 0], [2 * w, a, H], { role: "face" }),
        box("transducer", "metal", [0, 0, H / 2 + a * 0.05], [1.4 * w, a * 0.7, a * 0.1]),
        box("connector", "dark", [0, 0, H / 2 + a * 0.13], [0.36 * w, a * 0.2, a * 0.06]),
        box("housing", "mount", [0, 0, -H / 2 - 3], [2 * w + 8, a + 8, 6]),
        ...stand(0, 0, -H / 2 - 6, z),
      ];
    }

    case "prism": {
      const s = a * 0.95;
      const H = s * 0.6;
      // the 2D triangle (-0.289 s, ±0.5 s), (0.577 s, 0): its middle is 0.144 s in front of the centre
      return [
        part("prism", "prism", "glass", "glass", [0.144 * s, 0, 0], [0.866 * s, s, H]),
        part("mid-plane", "box", "glass", "window", [0, 0, 0], [0.3, a, H], { role: "face" }),
        box("prism table", "mount", [0.144 * s, 0, -H / 2 - 2], [s, s + 4, 4]),
        ...stand(0.144 * s, 0, -H / 2 - 4, z),
      ];
    }

    case "periscope":
      return periscopeParts(a, z, ctx.zOut - z);

    case "iris": {
      const open = numProp(c, "open_mm");
      const g = open !== null ? clamp(open, SMALL, a) : a * 0.35;
      const ringD = a + 6;
      const outer = (ringD / 2) * 0.92;
      const depth = Math.max(SMALL, outer - g / 2);
      const width = 2 * outer * Math.tan(Math.PI / 6) * 1.04;
      const leaves: Part[] = [];
      // six blades round a hexagonal opening of inner diameter g
      for (let k = 0; k < 6; k++) {
        const phi = (k * Math.PI) / 3;
        const r = g / 2 + depth / 2;
        leaves.push(box("iris blade", "dark", [0, r * Math.cos(phi), r * Math.sin(phi)], [0.8, depth, width], { rot: [phi, 0, 0] }));
      }
      return [alongX("iris ring", "torus", "solid", "mount", -2, 2, ringD, { role: "face" }), ...leaves, ...stand(0, 0, -ringD / 2, z)];
    }

    case "shutter": {
      const t = clamp(a * 0.06, 0.8, 1.6);
      // the housing reaches past one side of the aperture, where the blade goes when the shutter is open
      const room = a * 0.85;
      const housing = part("shutter housing", "box", "glass", "housing", [0, room / 2, 0], [4.8 * t, a + 2 + room, a + 2], { role: "face" });
      const blade = c.props["closed"] === true ? box("blade (closed)", "dark", [0, 0, 0], [2 * t, a, a]) : box("blade (open)", "dark", [0, h + 1.5 + (room - 0.5) / 2, 0], [2 * t, room - 0.5, a]);
      return [housing, blade, ...stand(0, room / 2, -(a + 2) / 2, z)];
    }

    case "sample": {
      const s = clamp(a * 0.35, 2, 7);
      const reflects = c.props["reflects"] === true;
      const out: Part[] = [box("sample", reflects ? "mirror" : "sample", [-s / 2, 0, 0], [s, a, a], { role: "face" }), box("sample holder", "mount", [-s - 6, 0, 0], [12, a + 12, a + 12])];
      if (sampleHasTip(c)) {
        const len = clamp(a * 0.7, 3.5, 9);
        // the tip points at the sample's face, its apex just in front of it (as on the 2D drawing)
        out.push(alongX("tip", "cone", "solid", "metal", 0.25, 0.25 + len, len * 0.32, {}, -1));
        out.push(box("tip holder", "mount", [len + 3, 0, 0], [5, 5, 5]));
      }
      out.push(...stand(-s - 6, 0, -(a + 12) / 2, z));
      return out;
    }

    case "detector": {
      const W = Math.max(a + 3, 10);
      const L = clamp(W * 0.8, 7, 18);
      return [box("detector", "body", [-(L + 0.5) / 2, 0, 0], [L - 0.5, W, W]), alongX("sensor", "cyl", "solid", "sensor", -0.5, 0, a, { role: "face" }), alongX("connector", "cyl", "solid", "metal", -L - 2.4, -L, 2.6), ...stand(-L / 2, 0, -W / 2, z)];
    }

    case "camera": {
      const W = Math.max(a + 6, 14);
      const L = W * 0.95;
      return [
        box("camera", "body", [-(L + 1.6) / 2, 0, 0], [L - 1.6, W, W]),
        // the flange stops just behind the sensor, so the two faces are not drawn on top of each other
        box("mount flange", "mount", [-0.825, 0, 0], [1.55, a + 2.4, a + 2.4]),
        alongX("sensor", "cyl", "solid", "sensor", -0.3, 0, a, { role: "face" }),
        ...pedestal(-L, 0, W, -W / 2, z),
      ];
    }

    case "spectrometer": {
      const W = Math.max(a + 8, 16);
      const L = W * 1.7;
      const H = W * 0.7;
      const jaw = Math.max(SMALL, (W - a) / 2 - 0.6);
      return [
        box("spectrometer", "body", [-(L + 0.6) / 2, 0, 0], [L - 0.6, W, H]),
        box("slit jaw", "dark", [-0.3, h + 0.6 + jaw / 2, 0], [0.6, jaw, H]),
        box("slit jaw", "dark", [-0.3, -h - 0.6 - jaw / 2, 0], [0.6, jaw, H]),
        box("slit", "sensor", [-0.2, 0, 0], [0.4, a, H * 0.6], { role: "face" }),
        ...pedestal(-L, 0, W, -H / 2, z),
      ];
    }

    case "power_meter": {
      const W = Math.max(a + 4, 12);
      const head = clamp(a * 0.3, 2.5, 6);
      const L = W * 0.9;
      return [
        alongX("sensor head", "cyl", "solid", "body", -head, -0.3, W),
        alongX("sensor", "cyl", "solid", "sensor", -0.3, 0, a, { role: "face" }),
        alongX("head housing", "cyl", "solid", "metal", -head - L, -head, W * 0.6),
        ...stand(-head / 2, 0, -W / 2, z),
      ];
    }

    case "beam_dump": {
      const L = clamp(a * 0.75, 8, 22);
      const H = h + clamp(a * 0.08, 1, 2.5);
      return [box("beam dump", "dark", [-L / 2, 0, 0], [L, 2 * H, 2 * H], { role: "face" }), alongX("mouth", "torus", "solid", "mount", -1.5, 0, a + 2), ...stand(-L / 2, 0, -H, z)];
    }

    case "blocker": {
      const t = clamp(a * 0.1, 1.5, 3.5);
      return [box("blocker", "dark", [-t / 2, 0, 0], [t, a, a], { role: "face" }), ...stand(-t / 2, 0, -h, z)];
    }

    case "fiber_in": {
      const cone = clamp(a * 1.5, 6, 12);
      return [
        alongX("coupler", "frustum", "solid", "metal", -cone, 0, a, { role: "face" }, -1),
        alongX("connector", "cyl", "solid", "metal", -cone - 3.2, -cone, 3.4),
        alongX("fibre", "cyl", "solid", "dark", -cone - 7.2, -cone - 3.2, 1),
        part("fibre coil", "torus", "solid", "dark", [-cone - 11.2, 0, 0], [8, 8, 1.2]),
        ...stand(-cone / 2, 0, -Math.max(h, 3), z),
      ];
    }

    case "fiber_out": {
      const wls = sourceWavelengths(c, 1);
      const out: Part[] = [
        alongX("collimator", "cyl", "solid", "metal", -8, -0.2, 4.6),
        alongX("output window", "cyl", "glass", "glass", -0.2, 0, 3.8, { role: "port" }),
        alongX("connector", "cyl", "solid", "metal", -11, -8, 3.2),
        alongX("fibre", "cyl", "solid", "dark", -15, -11, 1),
        part("fibre coil", "torus", "solid", "dark", [-19, 0, 0], [8, 8, 1.2]),
      ];
      if (wls[0] !== undefined) out.push(alongX("wavelength band", "cyl", "solid", "wl", -5.2, -3.8, 4.9, { wl: wls[0] }));
      out.push(...stand(-4, 0, -2.3, z));
      return out;
    }

    case "laser": {
      const L = 40;
      const W = 16;
      const out: Part[] = [box("laser head", "body", [-(L + 1.6) / 2, 0, 0], [L - 1.6, W, W]), alongX("aperture", "cyl", "solid", "port", -1.6, 0, 6, { role: "port" })];
      sourceWavelengths(c, 3).forEach((wl, i) => out.push(box("wavelength band", "wl", [-12 + i * 3.4 + 1.2, 0, 0], [2.4, W + 0.4, W + 0.4], { wl })));
      out.push(...pedestal(-L, -1.6, W, -W / 2, z));
      return out;
    }

    case "lamp":
      return [part("bulb", "sphere", "glass", "lamp", [0, 0, 0], [12, 12, 12], { role: "port" }), part("socket", "cyl", "solid", "metal", [0, 0, -8], [6, 6, 4]), ...stand(0, 0, -10, z)];

    case "led":
      return [
        part("LED dome", "sphere", "glass", "lamp", [-3.4, 0, 0], [6.8, 6.8, 6.8], { role: "port" }),
        alongX("LED body", "cyl", "solid", "metal", -12, -3.4, 8.4),
        alongX("lead", "cyl", "solid", "metal", -16, -12, 0.6, {}),
        ...stand(-8, 0, -4.2, z),
      ];

    case "custom": {
      const behavior = c.props["behavior"];
      const t = Math.max(clamp(a * 0.12, 1.4, 4), a * 0.3);
      if (behavior === "reflect") return [box("custom (reflects)", "metal", [-t / 2, 0, 0], [t, a, a], { role: "face" }), ...stand(-t / 2, 0, -h, z)];
      if (behavior === "stop") return [box("custom (stops)", "dark", [-t / 2, 0, 0], [t, a, a], { role: "face" }), ...stand(-t / 2, 0, -h, z)];
      return [part("custom (transmits)", "box", "glass", "housing", [0, 0, 0], [t, a, a], { role: "face" }), ...stand(0, 0, -h, z)];
    }

    default: {
      // a type this build does not draw: a plain plate, so the component can still be seen and moved
      const t = clamp(a * 0.06, 1, 3);
      return [box(c.type, "metal", [0, 0, 0], [2 * t, a, a], { role: "face" }), ...stand(0, 0, -h, z)];
    }
  }
}

/**
 * A periscope: two mirrors at 45°, the lower one at the input height sending the beam up (or down) to the
 * upper one at the output height, which sends it on in the same direction, on a post beside the beam. A
 * periscope that does not change the height (`rise` below PERISCOPE_MIN_RISE_MM, the case when it has no
 * output height) is drawn as a closed housing the beam passes through.
 */
function periscopeParts(a: number, z: number, rise: number): Part[] {
  const t = clamp(a * 0.1, 1.4, 4);
  const b = a * 0.3;
  if (Math.abs(rise) < PERISCOPE_MIN_RISE_MM) {
    const H = a * 1.4;
    return [part("periscope housing", "box", "glass", "housing", [0, 0, 0], [2 * b + 6, a + 4, H], { role: "face" }), ...stand(0, 0, -H / 2, z)];
  }
  // The two mirrors of a periscope are parallel. Going up, the input mirror turns +x into +z and the output one
  // +z back into +x: both lie across (1, 0, -1)/√2 (rotation3 turns the plate's thin axis, local x, to
  // (cos φ, 0, -sin φ)), φ = 45°. Going down they lie across (1, 0, 1)/√2, φ = -45°.
  const plateRot: Vec3 = [0, rise > 0 ? Math.PI / 4 : -Math.PI / 4, 0];
  const lowRot = plateRot;
  const highRot = plateRot;
  const w = a;
  const postY = -(a / 2 + 10);
  const lo = Math.min(0, rise);
  const hi = Math.max(0, rise);
  const out: Part[] = [
    box("input mirror", "mirror", [0, 0, 0], [t, w, w], { rot: lowRot, role: "face" }),
    box("output mirror", "mirror", [0, 0, rise], [t, w, w], { rot: highRot }),
    box("mirror arm", "mount", [0, postY / 2, 0], [8, Math.abs(postY), 8]),
    box("mirror arm", "mount", [0, postY / 2, rise], [8, Math.abs(postY), 8]),
  ];
  const floor = -z;
  const top = hi + w / 2;
  if (top - floor > 1) out.push(part("periscope post", "cyl", "solid", "post", [0, postY, (floor + top) / 2], [POST_D * 1.6, POST_D * 1.6, top - floor], { role: "stand" }));
  if (lo - w / 2 - floor > 1) out.push(box("periscope base", "base", [0, postY, floor + 4], [40, 40, 8], { role: "stand" }));
  return out;
}

/** The pose of a part in its component's frame. */
export function partMatrix(p: Part): Mat4 {
  return compose(p.at, p.rot, p.size);
}

/** The box (in the component's frame) the parts take up; without the stands unless asked. */
export function partsBox(parts: readonly Part[], withStands = false): Box3 {
  const b = emptyBox();
  for (const p of parts) {
    if (!withStands && p.role === "stand") continue;
    for (const q of unitCubeCorners(partMatrix(p))) growBox(b, q);
  }
  return b;
}
