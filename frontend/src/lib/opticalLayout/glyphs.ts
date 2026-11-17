// The drawings of the components, as data.
//
// Every shape is drawn here from simple geometry (lines, rectangles, polygons, a few
// curves). Nothing comes from an icon set or from the gwoptics ComponentLibrary: that
// library is CC BY-NC and cannot go into MAST (docs/v2/design/optical_layout.md section 0).
// The look follows the top views of textbooks and vendor catalogues: glass is a pale blue
// with a blue outline, housings are a flat grey with an ink outline, absorbers are solid,
// coatings are the heavy line (docs/v2/design/optical_layout_v2.md section 8.1).
//
// A glyph is a list of shapes in the component's LOCAL frame, in millimetres: x runs
// along the normal / optical axis (the direction of `angle_deg`), y along the active
// surface, so the active surface is the segment (0, -a/2) to (0, a/2) for every type.
// Every type that a ray can meet draws exactly that segment as one shape with
// `role: "face"`, LAST, so no decoration covers it and the geometry can be read off the
// drawing (test/opticalLayoutGlyphs.test.ts checks both). Light sources have no face;
// they emit from (0, 0).
//
// The canvas places the frame (translate, rotate, flip y) and the palette scales the same
// list into an icon; because the glyph is data, tests check the geometry without rendering.
// Two kinds of shape stay upright on the screen whatever the component's angle: `text`
// (λ/2, P, OD 1) and `dial`, a small face-on disc that shows the axis of a wave plate or a
// polariser the way the beam badges show polarisation (horizontal = in the table plane,
// up = up, seen by light that travels along `angle_deg`; section 8.3).
//
// Mirrors are drawn with their hatching on the BACK (the -x side), gratings with their
// teeth on the front, receivers (detectors, samples, dumps) with the body behind the
// receiving face, light sources with the body behind the emission point: the drawing
// shows which way the component faces.
//
// Small details carry `min`, the zoom (pixels per millimetre) below which they are left out:
// a hatch line or a "λ/2" that would be a speck only costs drawing time. The figure export
// (paperShapes) keeps every text and dial instead, at a readable size.

import type { LayoutComponent } from "./types.ts";

/** Stroke colour of a shape: line art, softer lines, glass outlines, a wavelength's colour. */
export type Tone = "ink" | "muted" | "faint" | "glass" | "wl" | "none";
/** Fill of a shape: the table colour, a housing, glass, an absorber, a grey of a given density, a wavelength. */
export type Fill = "none" | "body" | "metal" | "glass" | "dark" | "grey" | "wl";
export type Weight = "hair" | "fine" | "normal" | "thick";
/** What a shape is, where a test or the painter needs to know. */
export type Role = "face" | "hatch" | "teeth" | "detail";

export interface Style {
  fill?: Fill;
  /** fill opacity, 0..1 (each fill has a default) */
  fo?: number;
  weight?: Weight;
  tone?: Tone;
  /** stroke opacity, 0..1 */
  so?: number;
  /** stroke dash pattern in screen pixels */
  dash?: string;
  /** wavelength in nm for tone / fill "wl" */
  wl?: number;
  /** drawn only when the view shows at least this many pixels per millimetre */
  min?: number;
  role?: Role;
}

export type Shape =
  | ({ k: "line"; x1: number; y1: number; x2: number; y2: number } & Style)
  | ({ k: "rect"; x: number; y: number; w: number; h: number; rx?: number } & Style)
  | ({ k: "poly"; pts: Array<[number, number]> } & Style)
  | ({ k: "path"; d: string; bx: number; by: number; bw: number; bh: number } & Style)
  | ({ k: "circle"; cx: number; cy: number; r: number } & Style)
  /**
   * upright text centred on (x, y); `size` is the font size in millimetres. For the figure, which draws it larger
   * when the zoom is low: `fit`, the largest size (mm) it has room for; `out`, the end of the part (-y or +y) it
   * sits beyond, so it moves out by what it grew
   */
  | ({ k: "text"; x: number; y: number; text: string; size: number; fit?: number; out?: -1 | 1 } & Style)
  /** upright face-on disc of radius r: an axis at `deg` (counter-clockwise from horizontal), as one line ("axis") or as parallel lines ("grid"); `out` as for a text */
  | ({ k: "dial"; cx: number; cy: number; r: number; deg: number; mark: "axis" | "grid"; out?: -1 | 1 } & Style);

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

const line = (x1: number, y1: number, x2: number, y2: number, s: Style = {}): Shape => ({ k: "line", x1, y1, x2, y2, ...s });
const rect = (x: number, y: number, w: number, h: number, s: Style & { rx?: number } = {}): Shape => ({ k: "rect", x, y, w, h, ...s });
const poly = (pts: Array<[number, number]>, s: Style = {}): Shape => ({ k: "poly", pts, ...s });
const circle = (cx: number, cy: number, r: number, s: Style = {}): Shape => ({ k: "circle", cx, cy, r, ...s });
const text = (x: number, y: number, t: string, size: number, s: Style & { fit?: number; out?: -1 | 1 } = {}): Shape => ({ k: "text", x, y, text: t, size, min: 6.5 / size, ...s });

/** Pixels on the screen a detail needs before it is drawn, turned into the zoom it needs. */
const minFor = (px: number, mm: number) => px / Math.max(mm, 1e-6);

// ── the active surface ────────────────────────────────────────────────────────

/** A reflecting surface: the heavy coating line. */
const coating = (h: number, s: Style = {}): Shape => line(0, -h, 0, h, { weight: "thick", tone: "ink", role: "face", ...s });
/** A receiving surface (sensor, slit, mouth of a dump). */
const sensor = (h: number, s: Style = {}): Shape => line(0, -h, 0, h, { weight: "thick", tone: "ink", role: "face", ...s });
/** The mid-plane of a transmitting optic: thin, in the glass colour, still exact. */
const midPlane = (h: number, s: Style = {}): Shape => line(0, -h, 0, h, { weight: "fine", tone: "glass", role: "face", ...s });

// ── helpers ───────────────────────────────────────────────────────────────────

function numProp(c: LayoutComponent, key: string): number | null {
  const v = c.props[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** The wavelengths a source names (single or list), at most `n`. */
export function sourceWavelengths(c: LayoutComponent, n = 3): number[] {
  const one = numProp(c, "wavelength_nm");
  if (one !== null && one > 0) return [one];
  const list = c.props["wavelengths_nm"];
  if (!Array.isArray(list)) return [];
  return list.filter((v): v is number => typeof v === "number" && Number.isFinite(v) && v > 0).slice(0, n);
}

/** The wavelength whose colour stands for a filter's pass band, or null without one. */
export function filterHue(c: LayoutComponent): number | null {
  const lo = numProp(c, "pass_min_nm");
  const hi = numProp(c, "pass_max_nm");
  if (lo !== null && hi !== null) return (lo + hi) / 2;
  if (lo !== null) return lo + 40;
  if (hi !== null) return Math.max(1, hi - 40);
  return null;
}

/** The wavelength whose colour stands for what a dichroic reflects: a little on the reflecting side of its edge. */
export function dichroicHue(c: LayoutComponent): number | null {
  const edge = numProp(c, "edge_nm");
  if (edge === null || edge <= 0) return null;
  return c.props["reflects"] === "long" ? edge + 60 : Math.max(1, edge - 60);
}

/** ND plates are greyer the denser they are: the fill opacity for an optical density. */
export function ndDensity(od: number | null): number {
  if (od === null || !(od >= 0)) return 0.22;
  return clamp(0.12 + 0.18 * od, 0.12, 0.88);
}

/** The words that ask for a tip on a sample: STM / TERS work says so in the label or the function. */
const TIP_WORDS = /\b(?:TERS|STM|tip)\b|针尖/i;

export function sampleHasTip(c: LayoutComponent): boolean {
  return TIP_WORDS.test(`${c.label} ${c.annotations?.function ?? ""}`);
}

/** A coil of fibre behind a connector that ends at x = `x0`, the fibre leaving towards -x. */
function fibreTail(x0: number): Shape[] {
  return [
    line(x0, 0, x0 - 4, 0, { weight: "normal", tone: "muted" }),
    circle(x0 - 7.6, 0, 3.6, { weight: "fine", tone: "muted" }),
    circle(x0 - 8.2, 0.35, 3.0, { weight: "hair", tone: "muted", min: 1.2 }),
  ];
}

/** A plate across the axis: glass of a given half-thickness. */
function plate(h: number, t: number, s: Style = {}): Shape {
  return rect(-t, -h, 2 * t, 2 * h, { fill: "glass", tone: "glass", weight: "fine", ...s });
}

/** Where a dial or a text sits beyond the +y / -y end of a part of half-length `h`. */
const beyond = (h: number, r: number) => h + 1.2 + r;

// ── the drawings ──────────────────────────────────────────────────────────────

function mirrorGlyph(a: number): Shape[] {
  const h = a / 2;
  const t = clamp(a * 0.14, 1.4, 5);
  const n = clamp(Math.round(a / 3.2), 4, 14);
  const step = a / n;
  const d = clamp(a * 0.1, 1.2, 3);
  const out: Shape[] = [rect(-t, -h, t, a, { fill: "metal", tone: "muted", weight: "hair" })];
  const min = minFor(22, a);
  for (let i = 0; i < n; i++) {
    const y = -h + (i + 0.5) * step;
    out.push(line(-t, y + d * 0.5, -t - d, y - d * 0.5, { weight: "hair", tone: "muted", role: "hatch", min }));
  }
  out.push(coating(h));
  return out;
}

function cube(h: number): Shape {
  return poly(
    [
      [0, -h],
      [h, 0],
      [0, h],
      [-h, 0],
    ],
    { fill: "glass", tone: "glass", weight: "fine" },
  );
}

export function glyphShapes(c: LayoutComponent): Shape[] {
  const a = c.aperture_mm;
  const h = a / 2;

  // `as string`: the switch also draws types this build's list may not name yet (pbs arrives with v2's types)
  switch (c.type as string) {
    case "mirror":
      return mirrorGlyph(a);

    case "beam_splitter":
      return [cube(h), coating(h, { weight: "normal" })];

    case "pbs": {
      const size = clamp(h * 0.42, 1.8, 7);
      // the "P" sits in the half of the cube before the coating: an upright letter fits up to about 0.9 h
      return [cube(h), text(-h * 0.42, 0, "P", size, { tone: "glass", fit: h * 0.9 }), coating(h, { weight: "normal" })];
    }

    case "dichroic": {
      const t = clamp(a * 0.1, 1.2, 3);
      const hue = dichroicHue(c);
      return [rect(-t, -h, t, a, { fill: "glass", tone: "glass", weight: "fine" }), coating(h, hue === null ? { tone: "glass" } : { tone: "wl", wl: hue })];
    }

    case "grating": {
      const t = clamp(a * 0.12, 1.4, 4);
      const n = clamp(Math.round(a / 3), 4, 14);
      const step = a / n;
      const g = clamp(a * 0.07, 1, 3);
      const out: Shape[] = [rect(-t, -h, t, a, { fill: "metal", tone: "muted", weight: "hair" })];
      const min = minFor(20, a);
      for (let i = 0; i < n; i++) {
        const y0 = -h + i * step;
        out.push(poly([[0, y0], [g, y0 + step * 0.78], [0, y0 + step]], { fill: "metal", tone: "ink", weight: "hair", role: "teeth", min }));
      }
      out.push(coating(h));
      return out;
    }

    case "lens": {
      const f = numProp(c, "focal_mm");
      const unknown = f === null || f === 0;
      const dash = unknown ? "3 2" : undefined;
      if (f !== null && f < 0) {
        // bi-concave: thick at the rim, thin in the middle
        const e = clamp(a * 0.13, 1.5, 5);
        const s = clamp(a * 0.09, 0.9, 3.6);
        const d = `M ${-e} ${-h} L ${e} ${-h} Q ${e - 2 * s} 0 ${e} ${h} L ${-e} ${h} Q ${-e + 2 * s} 0 ${-e} ${-h} Z`;
        return [{ k: "path", d, bx: -e, by: -h, bw: 2 * e, bh: a, fill: "glass", tone: "glass", weight: "normal" }, midPlane(h)];
      }
      // bi-convex (also when the focal length is not given: then the outline is dashed)
      const e = clamp(a * 0.035, 0.5, 1.4);
      const b = clamp(a * 0.15, 1.4, 6);
      const d = `M ${-e} ${-h} L ${e} ${-h} Q ${e + 2 * b} 0 ${e} ${h} L ${-e} ${h} Q ${-e - 2 * b} 0 ${-e} ${-h} Z`;
      return [{ k: "path", d, bx: -e - b, by: -h, bw: 2 * (e + b), bh: a, fill: "glass", tone: "glass", weight: "normal", ...(dash ? { dash } : {}) }, midPlane(h)];
    }

    case "objective": {
      // the barrel narrows towards +x (the nose that faces the sample); its entrance is the active surface
      const B = Math.max(h + 1.6, 5.5);
      const L = clamp(B * 3.4, 18, 44);
      const x0 = -0.5 * L;
      const x1 = 0.06 * L;
      const x2 = 0.36 * L;
      const x3 = 0.46 * L;
      const out: Shape[] = [
        // the mounting thread behind the barrel
        rect(x0 - 0.08 * L, -0.72 * B, 0.08 * L, 1.44 * B, { fill: "metal", tone: "ink", weight: "fine" }),
        poly(
          [
            [x0, -B],
            [x1, -B],
            [x2, -0.46 * B],
            [x3, -0.3 * B],
            [x3, 0.3 * B],
            [x2, 0.46 * B],
            [x1, B],
            [x0, B],
          ],
          { fill: "metal", tone: "ink", weight: "normal" },
        ),
        rect(x0 + 0.1 * L, -B, 0.1 * L, 2 * B, { fill: "none", tone: "muted", weight: "hair", role: "detail", min: minFor(14, B) }),
        line(x3, -0.26 * B, x3, 0.26 * B, { tone: "glass", weight: "normal" }),
        midPlane(h, { weight: "normal" }),
      ];
      return out;
    }

    case "waveplate": {
      const t = clamp(a * 0.06, 0.7, 1.6);
      const r = clamp(a * 0.2, 2, 4.5);
      const fast = numProp(c, "fast_axis_deg") ?? 0;
      const ret = c.props["retardance"];
      const label = ret === "half" ? "λ/2" : ret === "quarter" ? "λ/4" : "λ/?";
      const size = clamp(a * 0.26, 2.4, 5);
      return [
        plate(h, t),
        { k: "dial", cx: 0, cy: beyond(h, r), r, deg: fast, mark: "axis", tone: "ink", weight: "fine", role: "detail", min: minFor(10, 2 * r), out: 1 },
        text(0, -beyond(h, size * 0.55), label, size, { tone: ret === "half" || ret === "quarter" ? "ink" : "faint", out: -1 }),
        midPlane(h),
      ];
    }

    case "polarizer": {
      const t = clamp(a * 0.07, 0.8, 1.8);
      const r = clamp(a * 0.2, 2, 4.5);
      const axis = numProp(c, "axis_deg") ?? 0;
      return [
        plate(h, t, { fill: "glass", fo: 0.24 }),
        line(-t, -h * 0.55, t, -h * 0.55, { tone: "glass", weight: "hair", min: minFor(18, a) }),
        line(-t, 0, t, 0, { tone: "glass", weight: "hair", min: minFor(18, a) }),
        line(-t, h * 0.55, t, h * 0.55, { tone: "glass", weight: "hair", min: minFor(18, a) }),
        { k: "dial", cx: 0, cy: beyond(h, r), r, deg: axis, mark: "grid", tone: "ink", weight: "fine", role: "detail", min: minFor(10, 2 * r), out: 1 },
        midPlane(h),
      ];
    }

    case "filter": {
      // a coloured-glass plate: tinted with its pass band when it has one, with the slant lines of absorbing glass
      const t = clamp(a * 0.08, 1, 2.2);
      const hue = filterHue(c);
      const tinted: Style = hue === null ? {} : { fill: "wl", wl: hue, fo: 0.42, tone: "wl" };
      const ink: Style = hue === null ? { tone: "glass" } : { tone: "wl", wl: hue };
      const out: Shape[] = [plate(h, t, tinted)];
      const min = minFor(16, a);
      for (const y of [-0.6, -0.2, 0.2, 0.6]) out.push(line(-t, y * h - t, t, y * h + t, { ...ink, weight: "hair", role: "detail", min }));
      out.push(midPlane(h, hue === null ? {} : { tone: "wl", wl: hue }));
      return out;
    }

    case "window": {
      const t = clamp(a * 0.05, 0.7, 1.5);
      return [plate(h, t, { fo: 0.08 }), midPlane(h)];
    }

    case "nd": {
      const t = clamp(a * 0.08, 1, 2.2);
      const od = numProp(c, "od");
      const size = clamp(a * 0.2, 2.4, 4.5);
      const out: Shape[] = [rect(-t, -h, 2 * t, a, { fill: "grey", fo: ndDensity(od), tone: "ink", weight: "fine" })];
      if (od !== null) out.push(text(0, -beyond(h, size * 0.55), `OD ${Number(od.toFixed(2))}`, size, { tone: "muted", out: -1 }));
      out.push(midPlane(h, { tone: "ink", so: 0.7 }));
      return out;
    }

    case "aotf": {
      const w = a * 0.26;
      return [
        rect(-w, -h, 2 * w, a, { fill: "glass", tone: "glass", weight: "fine" }),
        line(-w, h * 0.25, w, h * 0.25, { tone: "glass", weight: "hair", role: "detail", min: minFor(18, a) }),
        line(-w, h * 0.5, w, h * 0.5, { tone: "glass", weight: "hair", role: "detail", min: minFor(18, a) }),
        line(-w, h * 0.75, w, h * 0.75, { tone: "glass", weight: "hair", role: "detail", min: minFor(18, a) }),
        rect(-0.7 * w, h, 1.4 * w, 0.1 * a, { fill: "metal", tone: "ink", weight: "fine" }),
        rect(-0.18 * w, h + 0.1 * a, 0.36 * w, 0.08 * a, { fill: "dark", tone: "ink", weight: "hair" }),
        midPlane(h),
      ];
    }

    case "prism": {
      const s = a * 0.95;
      return [
        poly(
          [
            [-0.289 * s, -0.5 * s],
            [-0.289 * s, 0.5 * s],
            [0.577 * s, 0],
          ],
          { fill: "glass", tone: "glass", weight: "normal" },
        ),
        midPlane(h),
      ];
    }

    case "periscope": {
      const b = a * 0.3;
      const r = a * 0.17;
      const out: Shape[] = [
        rect(-b, -b, 2 * b, 2 * b, { fill: "metal", tone: "muted", weight: "fine", rx: b * 0.2 }),
        circle(0, 0, r, { fill: "body", tone: "ink", weight: "fine" }),
        line(-r * 0.7, -r * 0.7, r * 0.7, r * 0.7, { tone: "ink", weight: "normal" }),
        circle(0, 0, r * 0.22, { fill: "dark", tone: "none" }),
      ];
      // with an exit height its direction matters (docs/v2/design/optical_layout_3d.md section 3.4: light along +x goes
      // from z_mm to z_out_mm): an arrowhead on its +x side says which way that is
      if (numProp(c, "z_out_mm") !== null) {
        out.push(
          poly(
            [
              [b * 1.15, -b * 0.45],
              [b * 1.75, 0],
              [b * 1.15, b * 0.45],
            ],
            { fill: "dark", tone: "ink", weight: "hair", role: "detail" },
          ),
        );
      }
      out.push(midPlane(h, { weight: "hair" }));
      return out;
    }

    case "iris": {
      const open = numProp(c, "open_mm");
      const g = open !== null ? clamp(open / 2, 0, h) : h * 0.35;
      const H = h + clamp(a * 0.06, 0.8, 2);
      const t = clamp(a * 0.05, 0.7, 1.6);
      const tip = Math.min(t * 0.9, Math.max(0, h - g));
      return [
        rect(-t * 1.6, -H, 3.2 * t, 2 * H, { fill: "none", tone: "muted", weight: "hair" }),
        poly([[-t, -H], [t, -H], [t, -g], [-t, -g - tip]], { fill: "dark", tone: "ink", weight: "hair" }),
        poly([[-t, H], [t, H], [t, g], [-t, g + tip]], { fill: "dark", tone: "ink", weight: "hair" }),
        line(0, -h, 0, h, { weight: "hair", tone: "faint", role: "face" }),
      ];
    }

    case "shutter": {
      const t = clamp(a * 0.06, 0.8, 1.6);
      // the housing reaches past one side of the aperture, where the blade goes when the shutter is open
      const room = a * 0.85;
      const out: Shape[] = [rect(-2.4 * t, -h - 1, 4.8 * t, a + 2 + room, { fill: "metal", fo: 0.45, tone: "muted", weight: "fine", rx: t })];
      if (c.props["closed"] === true) {
        out.push(rect(-t, -h, 2 * t, a, { fill: "dark", tone: "ink", weight: "fine" }));
        out.push(sensor(h, { weight: "normal" }));
      } else {
        out.push(rect(-t, h + 1.5, 2 * t, room - 0.5, { fill: "dark", tone: "ink", weight: "hair" }));
        out.push(line(0, -h, 0, h, { weight: "fine", tone: "faint", dash: "3 2", role: "face" }));
      }
      return out;
    }

    case "sample": {
      const s = clamp(a * 0.35, 2, 7);
      const out: Shape[] = [rect(-s, -h, s, a, { fill: "metal", tone: "ink", weight: "fine" })];
      for (const y of [-0.5, 0, 0.5]) out.push(line(-s, y * h - s * 0.4, -s * 0.35, y * h + s * 0.25, { weight: "hair", tone: "muted", role: "hatch", min: minFor(20, a) }));
      if (sampleHasTip(c)) {
        const len = clamp(a * 0.7, 3.5, 9);
        out.push(poly([[0.25, 0], [len, -len * 0.16], [len, len * 0.16]], { fill: "dark", tone: "ink", weight: "hair" }));
      }
      out.push(c.props["reflects"] === true ? coating(h) : sensor(h, { tone: "muted" }));
      return out;
    }

    case "detector": {
      const W = Math.max(a + 3, 10);
      const L = clamp(W * 0.8, 7, 18);
      return [
        rect(-L, -W / 2, L, W, { fill: "metal", tone: "ink", weight: "normal", rx: 0.8 }),
        rect(-L - 2.4, -1.3, 2.4, 2.6, { fill: "metal", tone: "ink", weight: "fine" }),
        poly([[-0.66 * L, -0.2 * W], [-0.66 * L, 0.2 * W], [-0.4 * L, 0]], { fill: "none", tone: "ink", weight: "fine", role: "detail", min: minFor(12, W) }),
        line(-0.4 * L, -0.2 * W, -0.4 * L, 0.2 * W, { tone: "ink", weight: "fine", role: "detail", min: minFor(12, W) }),
        sensor(h),
      ];
    }

    case "camera": {
      const W = Math.max(a + 6, 14);
      const L = W * 0.95;
      return [
        rect(-L, -W / 2, L - 1.6, W, { fill: "metal", tone: "ink", weight: "normal", rx: 1.2 }),
        rect(-1.6, -(h + 1.2), 1.6, a + 2.4, { fill: "metal", tone: "ink", weight: "fine" }),
        circle(-L * 0.52, 0, W * 0.2, { fill: "body", tone: "ink", weight: "fine", role: "detail", min: minFor(10, W) }),
        circle(-L * 0.52, 0, W * 0.08, { fill: "glass", tone: "glass", weight: "hair", role: "detail", min: minFor(14, W) }),
        sensor(h),
      ];
    }

    case "spectrometer": {
      const W = Math.max(a + 8, 16);
      const L = W * 1.7;
      const min = minFor(16, W);
      return [
        rect(-L, -W / 2, L, W, { fill: "metal", tone: "ink", weight: "normal", rx: 1.2 }),
        line(0, -W / 2, 0, -h - 0.6, { tone: "ink", weight: "normal" }),
        line(0, h + 0.6, 0, W / 2, { tone: "ink", weight: "normal" }),
        line(-0.86 * L, -0.12 * W, -0.74 * L, 0.12 * W, { tone: "ink", weight: "fine", role: "detail", min }),
        line(-0.8 * L, 0, -0.3 * L, -0.28 * W, { tone: "wl", wl: 460, weight: "fine", role: "detail", min }),
        line(-0.8 * L, 0, -0.3 * L, 0, { tone: "wl", wl: 540, weight: "fine", role: "detail", min }),
        line(-0.8 * L, 0, -0.3 * L, 0.28 * W, { tone: "wl", wl: 630, weight: "fine", role: "detail", min }),
        line(-0.27 * L, -0.34 * W, -0.27 * L, 0.34 * W, { tone: "ink", weight: "normal", role: "detail", min }),
        sensor(h),
      ];
    }

    case "power_meter": {
      const W = Math.max(a + 4, 12);
      const head = clamp(a * 0.3, 2.5, 6);
      const L = W * 0.9;
      const r = W * 0.26;
      const cx = -head - L * 0.5;
      return [
        rect(-head, -W / 2, head, W, { fill: "metal", tone: "ink", weight: "normal", rx: 0.6 }),
        rect(-head - L, -W * 0.3, L, W * 0.6, { fill: "metal", tone: "ink", weight: "fine", rx: 0.8 }),
        circle(cx, 0, r, { fill: "body", tone: "ink", weight: "hair", role: "detail", min: minFor(12, W) }),
        line(cx, 0, cx + r * 0.65, r * 0.7, { tone: "ink", weight: "fine", role: "detail", min: minFor(12, W) }),
        sensor(h),
      ];
    }

    case "beam_dump": {
      const L = clamp(a * 0.75, 8, 22);
      const H = h + clamp(a * 0.08, 1, 2.5);
      return [
        rect(-L, -H, L, 2 * H, { fill: "dark", tone: "ink", weight: "fine", rx: 0.8 }),
        poly([[0.001, -h], [-0.82 * L, 0], [0.001, h]], { fill: "body", tone: "ink", weight: "fine" }),
        line(0, -h, 0, h, { weight: "fine", tone: "faint", dash: "3 2", role: "face" }),
      ];
    }

    case "blocker": {
      const t = clamp(a * 0.1, 1.5, 3.5);
      return [rect(-t, -h, t, a, { fill: "dark", tone: "ink", weight: "fine" }), sensor(h, { weight: "normal" })];
    }

    case "fiber_in": {
      const cone = clamp(a * 1.5, 6, 12);
      const ferrule = 1.3;
      return [
        poly([[0, -h], [-cone, -ferrule], [-cone, ferrule], [0, h]], { fill: "metal", tone: "ink", weight: "fine" }),
        rect(-cone - 3.2, -ferrule - 0.4, 3.2, 2 * ferrule + 0.8, { fill: "metal", tone: "ink", weight: "fine" }),
        ...fibreTail(-cone - 3.2),
        sensor(h),
      ];
    }

    case "fiber_out": {
      const wls = sourceWavelengths(c, 1);
      const out: Shape[] = [
        ...fibreTail(-11),
        rect(-11, -1.6, 3, 3.2, { fill: "metal", tone: "ink", weight: "fine" }),
        rect(-8, -2.3, 8, 4.6, { fill: "metal", tone: "ink", weight: "fine", rx: 0.5 }),
      ];
      if (wls[0] !== undefined) out.push(rect(-5.2, -2.3, 1.4, 4.6, { fill: "wl", wl: wls[0], fo: 0.9, tone: "none" }));
      out.push(line(0, -1.9, 0, 1.9, { tone: "glass", weight: "normal" }));
      return out;
    }

    case "laser": {
      const L = 40;
      const W = 16;
      const out: Shape[] = [
        rect(-L, -W / 2, L - 1.6, W, { fill: "metal", tone: "ink", weight: "normal", rx: 1.4 }),
        rect(-1.6, -3, 1.6, 6, { fill: "metal", tone: "ink", weight: "fine" }),
        rect(-L + 3, -W * 0.28, 6, W * 0.56, { fill: "none", tone: "muted", weight: "hair", rx: 0.6, role: "detail", min: minFor(10, W) }),
      ];
      sourceWavelengths(c, 3).forEach((wl, i) => out.push(rect(-12 + i * 3.4, -W / 2, 2.4, W, { fill: "wl", wl, fo: 0.95, tone: "none" })));
      out.push(line(0, -1.6, 0, 1.6, { tone: "ink", weight: "thick" }));
      return out;
    }

    case "lamp": {
      const out: Shape[] = [
        circle(0, 0, 6, { fill: "glass", tone: "glass", weight: "normal" }),
        { k: "path", d: "M -3 1.5 L -1.5 -1.5 L 0 1.5 L 1.5 -1.5 L 3 1.5", bx: -3, by: -1.5, bw: 6, bh: 3, fill: "none", tone: "ink", weight: "fine" },
      ];
      for (let i = 0; i < 8; i++) {
        const r = (i * Math.PI) / 4 + Math.PI / 8;
        out.push(line(Math.cos(r) * 8, Math.sin(r) * 8, Math.cos(r) * 11, Math.sin(r) * 11, { weight: "hair", tone: "muted" }));
      }
      return out;
    }

    case "led": {
      const r = 3.4;
      return [
        line(-10, -1.6, -14, -1.6, { tone: "muted", weight: "fine" }),
        line(-10, 1.6, -14, 1.6, { tone: "muted", weight: "fine" }),
        rect(-10, -r - 0.8, 10 - r, 2 * r + 1.6, { fill: "metal", tone: "ink", weight: "fine", rx: 0.5 }),
        { k: "path", d: `M ${-r} ${-r} A ${r} ${r} 0 0 1 ${-r} ${r} Z`, bx: -r, by: -r, bw: r, bh: 2 * r, fill: "glass", tone: "glass", weight: "fine" },
        poly([[-8.6, -1.8], [-8.6, 1.8], [-5.6, 0]], { fill: "dark", tone: "ink", weight: "hair", role: "detail", min: 1.5 }),
        line(-5.6, -1.8, -5.6, 1.8, { tone: "ink", weight: "fine", role: "detail", min: 1.5 }),
      ];
    }

    case "custom": {
      const behavior = c.props["behavior"];
      const t = clamp(a * 0.12, 1.4, 4);
      if (behavior === "reflect") return [rect(-t, -h, t, a, { fill: "metal", tone: "muted", weight: "fine", dash: "3 2" }), coating(h)];
      if (behavior === "stop") return [rect(-t, -h, t, a, { fill: "dark", tone: "ink", weight: "fine", dash: "3 2" }), sensor(h)];
      return [rect(-t, -h, 2 * t, a, { fill: "glass", tone: "glass", weight: "fine", dash: "3 2" }), midPlane(h)];
    }

    default: {
      // a type this build does not draw: a plain plate, so the component can still be seen and moved
      const t = clamp(a * 0.06, 1, 3);
      return [rect(-t, -h, 2 * t, a, { fill: "metal", tone: "muted", weight: "fine" }), line(0, -h, 0, h, { weight: "normal", tone: "ink", role: "face" })];
    }
  }
}

export interface ShapeBounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

/** The smallest font (px) the figure draws a glyph's text at, and the smallest radius (px) of a dial. */
export const PAPER_TEXT_MIN_PX = 8;
export const PAPER_DIAL_MIN_PX = 5;

/**
 * A glyph as the figure export draws it at zoom k (pixels per millimetre). The canvas leaves out a "P", a "λ/2", an
 * "OD 3" or an axis dial below the zoom that makes them legible; a figure is read zoomed in, printed or projected,
 * so it keeps them all: a text at least PAPER_TEXT_MIN_PX high (but no larger than its `fit`), a dial at least
 * PAPER_DIAL_MIN_PX in radius, each moved out by what it grew when it sits beyond an end of the part (`out`). The
 * other details keep their `min`.
 */
export function paperShapes(shapes: readonly Shape[], k: number): Shape[] {
  return shapes.map((s) => {
    if (s.k === "text") {
      const size = Math.min(Math.max(s.size, PAPER_TEXT_MIN_PX / k), s.fit ?? Infinity);
      const grow = Math.max(0, size - s.size) * 0.55;
      const { min: _min, ...rest } = s;
      return { ...rest, size, y: s.y + (s.out ?? 0) * grow };
    }
    if (s.k === "dial") {
      const r = Math.max(s.r, PAPER_DIAL_MIN_PX / k);
      const { min: _min, ...rest } = s;
      return { ...rest, r, cy: s.cy + (s.out ?? 0) * (r - s.r) };
    }
    return s;
  });
}

/** The box around a glyph (strokes not included; a text counts as a box of its size). */
export function shapeBounds(shapes: readonly Shape[]): ShapeBounds {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  const add = (x: number, y: number) => {
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    maxX = Math.max(maxX, x);
    maxY = Math.max(maxY, y);
  };
  for (const s of shapes) {
    switch (s.k) {
      case "line":
        add(s.x1, s.y1);
        add(s.x2, s.y2);
        break;
      case "rect":
        add(s.x, s.y);
        add(s.x + s.w, s.y + s.h);
        break;
      case "poly":
        for (const [x, y] of s.pts) add(x, y);
        break;
      case "path":
        add(s.bx, s.by);
        add(s.bx + s.bw, s.by + s.bh);
        break;
      case "circle":
      case "dial":
        add(s.cx - s.r, s.cy - s.r);
        add(s.cx + s.r, s.cy + s.r);
        break;
      case "text": {
        // upright on the screen whatever the angle: count it as a square as wide as the text
        const half = Math.max(s.size * 0.5, (s.text.length * s.size * 0.62) / 2);
        add(s.x - half, s.y - half);
        add(s.x + half, s.y + half);
        break;
      }
    }
  }
  return minX === Infinity ? { minX: 0, minY: 0, maxX: 0, maxY: 0 } : { minX, minY, maxX, maxY };
}

/** Radius of the smallest circle round the centre that holds the whole glyph. */
export function glyphRadius(shapes: readonly Shape[]): number {
  const b = shapeBounds(shapes);
  return Math.hypot(Math.max(Math.abs(b.minX), Math.abs(b.maxX)), Math.max(Math.abs(b.minY), Math.abs(b.maxY)));
}

/** The face of a glyph (the active surface), or undefined for a light source. */
export function faceOf(shapes: readonly Shape[]): Extract<Shape, { k: "line" }> | undefined {
  for (let i = shapes.length - 1; i >= 0; i--) {
    const s = shapes[i]!;
    if (s.k === "line" && s.role === "face") return s;
  }
  return undefined;
}

/** Where a glyph sits on the screen relative to its component's centre: the box a label has to stay clear of. */
export interface ScreenBox {
  /** offset of the middle of the box from the component's centre, px (screen y grows downwards) */
  dx: number;
  dy: number;
  /** half width and half height, px */
  ex: number;
  ey: number;
}

/** The screen box of a glyph whose local box is `b`, turned by `angleDeg` and drawn at `k` px/mm. */
export function screenBox(b: ShapeBounds, angleDeg: number, k: number): ScreenBox {
  const t = angleDeg * (Math.PI / 180);
  const cos = Math.cos(t);
  const sin = Math.sin(t);
  const c = Math.abs(cos);
  const s = Math.abs(sin);
  const mx = (b.minX + b.maxX) / 2;
  const my = (b.minY + b.maxY) / 2;
  const hx = (b.maxX - b.minX) / 2;
  const hy = (b.maxY - b.minY) / 2;
  // local x is (cos, sin) on the table, local y is (-sin, cos); the screen flips y
  const wx = mx * cos - my * sin;
  const wy = mx * sin + my * cos;
  return { dx: wx * k, dy: -wy * k, ex: (hx * c + hy * s) * k, ey: (hx * s + hy * c) * k };
}
