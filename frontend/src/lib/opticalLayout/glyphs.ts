// The drawings of the components, as data.
//
// Every shape is drawn here from simple geometry (lines, rectangles, polygons, a few
// curves). Nothing comes from an icon set or from the gwoptics ComponentLibrary: that
// library is CC BY-NC and cannot go into MAST (docs/v2/design/optical_layout.md section 0).
//
// A glyph is a list of shapes in the component's LOCAL frame, in millimetres: x runs
// along the normal / optical axis (the direction of `angle_deg`), y along the active
// surface, so the active surface is the segment (0, -a/2) to (0, a/2) for every type.
// The canvas places the frame (translate, rotate, flip y) and the palette scales the same
// list into an icon; because the glyph is data, tests can check the geometry without
// rendering anything (test/opticalLayoutGlyphs.test.ts).
//
// Mirrors are drawn with their hatching on the BACK (the -x side), gratings with their
// teeth on the front, receivers (detectors, samples, dumps) with the body behind the
// receiving face, light sources with the body behind the emission point: the drawing
// shows which way the component faces.

import type { LayoutComponent } from "./types.ts";

export type Tone = "main" | "muted" | "accent";
export type Fill = "none" | "body" | "tint" | "ink";
export type Weight = "hair" | "normal" | "thick";

interface Style {
  fill?: Fill;
  weight?: Weight;
  tone?: Tone;
}

export type Shape =
  | ({ k: "line"; x1: number; y1: number; x2: number; y2: number } & Style)
  | ({ k: "rect"; x: number; y: number; w: number; h: number } & Style)
  | ({ k: "poly"; pts: Array<[number, number]> } & Style)
  | ({ k: "path"; d: string; bx: number; by: number; bw: number; bh: number } & Style)
  | ({ k: "circle"; cx: number; cy: number; r: number } & Style);

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

const line = (x1: number, y1: number, x2: number, y2: number, s: Style = {}): Shape => ({ k: "line", x1, y1, x2, y2, ...s });
const rect = (x: number, y: number, w: number, h: number, s: Style = {}): Shape => ({ k: "rect", x, y, w, h, ...s });
const poly = (pts: Array<[number, number]>, s: Style = {}): Shape => ({ k: "poly", pts, ...s });
const circle = (cx: number, cy: number, r: number, s: Style = {}): Shape => ({ k: "circle", cx, cy, r, ...s });

/** The active surface itself, drawn thick: where a ray actually meets the component. */
const face = (h: number, x = 0, s: Style = {}): Shape => line(x, -h, x, h, { weight: "thick", ...s });

function numProp(c: LayoutComponent, key: string): number | null {
  const v = c.props[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** A body that sits behind a receiving face or an emission point (the -x side). */
function behind(length: number, width: number, s: Style = {}): Shape {
  return rect(-length, -width / 2, length, width, { fill: "body", ...s });
}

function mirrorGlyph(a: number): Shape[] {
  const h = a / 2;
  const n = clamp(Math.round(a / 5), 3, 10);
  const step = a / n;
  const d = clamp(a * 0.1, 2, 4);
  const out: Shape[] = [face(h)];
  for (let i = 0; i < n; i++) {
    const y = -h + (i + 0.5) * step;
    out.push(line(0, y, -d, y - d, { weight: "hair", tone: "muted" }));
  }
  return out;
}

export function glyphShapes(c: LayoutComponent): Shape[] {
  const a = c.aperture_mm;
  const h = a / 2;
  const t = clamp(a * 0.06, 1, 3);

  switch (c.type) {
    case "mirror":
      return mirrorGlyph(a);

    case "beam_splitter":
      return [poly([[0, -h], [h, 0], [0, h], [-h, 0]], { fill: "tint" }), line(0, -h, 0, h, { weight: "normal" })];

    case "dichroic":
      return [rect(-t, -h, 2 * t, a, { fill: "tint" }), face(h, 0, { tone: "accent" })];

    case "grating": {
      const n = clamp(Math.round(a / 3), 4, 14);
      const step = a / n;
      const g = clamp(a * 0.08, 1.5, 4);
      const out: Shape[] = [face(h)];
      for (let i = 0; i < n; i++) {
        const y0 = -h + i * step;
        out.push(poly([[0, y0], [g, y0 + step * 0.5], [0, y0 + step]], { fill: "body", weight: "hair" }));
      }
      return out;
    }

    case "lens": {
      const w = clamp(a * 0.3, 3, 14);
      return [{ k: "path", d: `M 0 ${-h} Q ${w} 0 0 ${h} Q ${-w} 0 0 ${-h} Z`, bx: -w / 2, by: -h, bw: w, bh: a, fill: "tint" }];
    }

    case "objective": {
      const len = clamp(a * 1.4, 12, 34);
      const hb = Math.max(h, 4);
      const hf = hb * 0.35;
      return [
        poly([[-0.6 * len, -hb], [0.4 * len, -hf], [0.4 * len, hf], [-0.6 * len, hb]], { fill: "body" }),
        line(0.4 * len, -hf, 0.4 * len, hf, { weight: "thick" }),
        line(-0.6 * len, -hb, -0.6 * len, hb, { weight: "normal" }),
      ];
    }

    case "waveplate":
      return [rect(-t, -h, 2 * t, a, { fill: "tint" }), line(-2.4 * t, -h * 0.3, 2.4 * t, h * 0.3, { weight: "hair", tone: "accent" })];

    case "polarizer":
      return [
        rect(-t, -h, 2 * t, a, { fill: "body" }),
        line(-2.6 * t, -h * 0.5, 2.6 * t, -h * 0.5, { weight: "hair", tone: "muted" }),
        line(-2.6 * t, 0, 2.6 * t, 0, { weight: "hair", tone: "muted" }),
        line(-2.6 * t, h * 0.5, 2.6 * t, h * 0.5, { weight: "hair", tone: "muted" }),
      ];

    case "filter":
      return [
        rect(-1.3 * t, -h, 2.6 * t, a, { fill: "tint" }),
        line(-1.3 * t, -h * 0.5, 1.3 * t, -h * 0.5 - 1.3 * t, { weight: "hair", tone: "muted" }),
        line(-1.3 * t, 0, 1.3 * t, -1.3 * t, { weight: "hair", tone: "muted" }),
        line(-1.3 * t, h * 0.5, 1.3 * t, h * 0.5 - 1.3 * t, { weight: "hair", tone: "muted" }),
      ];

    case "window":
      return [rect(-t, -h, 2 * t, a, { fill: "none" }), line(0, -h, 0, h, { weight: "hair", tone: "muted" })];

    case "nd":
      return [rect(-1.3 * t, -h, 2.6 * t, a, { fill: "ink", tone: "muted" }), line(0, -h, 0, h, { weight: "hair", tone: "main" })];

    case "aotf": {
      const w = a * 0.22;
      return [
        rect(-w, -h * 0.8, 2 * w, a * 0.8, { fill: "body" }),
        line(-w, 0, w, 0, { weight: "hair", tone: "muted" }),
        rect(-0.1 * a, h * 0.8, 0.2 * a, 0.08 * a, { fill: "ink" }),
      ];
    }

    case "prism": {
      const s = a * 0.9;
      return [poly([[-0.289 * s, -0.5 * s], [-0.289 * s, 0.5 * s], [0.577 * s, 0]], { fill: "tint" })];
    }

    case "periscope":
      return [rect(-0.25 * a, -0.35 * a, 0.5 * a, 0.7 * a, { fill: "body" }), circle(0, 0, 0.12 * a, { fill: "ink" }), line(0, -h, 0, h, { weight: "hair", tone: "muted" })];

    case "iris": {
      const open = numProp(c, "open_mm");
      const g = open !== null ? clamp(open / 2, 0, h) : h * 0.35;
      const cap = clamp(a * 0.06, 1, 3);
      return [
        line(0, -h, 0, -g, { weight: "thick" }),
        line(0, g, 0, h, { weight: "thick" }),
        line(-cap, -g, cap, -g, { weight: "normal" }),
        line(-cap, g, cap, g, { weight: "normal" }),
      ];
    }

    case "shutter": {
      if (c.props["closed"] === true) return [rect(-1.2 * t, -h, 2.4 * t, a, { fill: "ink" })];
      return [rect(-1.2 * t, h * 1.1, 2.4 * t, a, { fill: "none", weight: "hair", tone: "muted" }), line(0, -h, 0, h, { weight: "hair", tone: "muted" })];
    }

    case "sample": {
      const out: Shape[] = [rect(-3 * t, -h, 3 * t, a, { fill: "body" }), line(-3 * t, -h * 0.5, 0, -h * 0.5 - 3 * t, { weight: "hair", tone: "muted" }), line(-3 * t, h * 0.5, 0, h * 0.5 - 3 * t, { weight: "hair", tone: "muted" })];
      if (c.props["reflects"] === true) out.push(face(h, 0, { tone: "accent" }));
      else out.push(line(0, -h, 0, h, { weight: "normal" }));
      return out;
    }

    case "detector": {
      const bw = Math.max(a, 14);
      const len = bw * 0.9;
      return [behind(len, bw), face(bw * 0.4), poly([[-len * 0.65, -bw * 0.2], [-len * 0.35, 0], [-len * 0.65, bw * 0.2]], { fill: "tint" })];
    }

    case "camera": {
      const bw = Math.max(a, 14);
      const len = bw * 0.9;
      return [behind(len, bw), face(bw * 0.4), circle(-len * 0.3, 0, bw * 0.25, { fill: "none" })];
    }

    case "spectrometer": {
      const bw = Math.max(a, 14);
      const len = bw * 2;
      const out: Shape[] = [behind(len, bw), face(bw * 0.4)];
      for (let i = -1; i <= 2; i++) out.push(line(-len * 0.5 + i * 2.2, -bw * 0.28, -len * 0.5 + i * 2.2, bw * 0.28, { weight: "hair", tone: "muted" }));
      return out;
    }

    case "power_meter": {
      const bw = Math.max(a, 14);
      const len = bw * 1.1;
      const r = bw * 0.3;
      return [behind(len, bw), face(bw * 0.4), circle(-len * 0.5, 0, r, { fill: "none", weight: "hair" }), line(-len * 0.5, 0, -len * 0.5 + r * 0.7, r * 0.7, { weight: "hair", tone: "accent" })];
    }

    case "beam_dump": {
      const len = clamp(a * 0.5, 8, 16);
      return [rect(-len, -h, len, a, { fill: "ink" }), face(h, 0, { tone: "muted" })];
    }

    case "blocker":
      return [rect(-1.5 * t, -h, 1.5 * t, a, { fill: "ink" })];

    case "fiber_in": {
      const cone = clamp(a * 1.6, 6, 12);
      return [poly([[0, -h], [-cone, -h * 0.3], [-cone, h * 0.3], [0, h]], { fill: "tint" }), line(-cone, 0, -cone - 14, 0, { weight: "thick", tone: "muted" }), face(h)];
    }

    case "fiber_out": {
      const cone = 8;
      return [line(-cone - 18, 0, -cone, 0, { weight: "thick", tone: "muted" }), poly([[-cone, -3], [-cone, 3], [0, 1.4], [0, -1.4]], { fill: "body" }), poly([[0, -2.5], [5, 0], [0, 2.5]], { fill: "ink" })];
    }

    case "laser":
      return [behind(34, 18), line(0, -4, 0, 4, { weight: "thick" }), poly([[0, -3.5], [7, 0], [0, 3.5]], { fill: "ink" }), circle(-28, 0, 2, { fill: "tint", tone: "accent" })];

    case "lamp": {
      const out: Shape[] = [circle(0, 0, 6, { fill: "tint" })];
      for (let i = 0; i < 8; i++) {
        const r = (i * Math.PI) / 4;
        out.push(line(Math.cos(r) * 8, Math.sin(r) * 8, Math.cos(r) * 12, Math.sin(r) * 12, { weight: "hair", tone: "muted" }));
      }
      return out;
    }

    case "led":
      return [behind(10, 10), poly([[-8, -3], [-3, 0], [-8, 3]], { fill: "ink" }), line(-3, -3, -3, 3, { weight: "normal" }), poly([[0, -2], [5, 0], [0, 2]], { fill: "ink" })];

    case "custom":
      return [rect(-2 * t, -h, 4 * t, a, { fill: "tint" }), circle(0, 0, 1.2 * t, { fill: "body" })];

    default:
      // a type this build does not draw: a plain plate, so the component can still be seen and moved
      return [rect(-t, -h, 2 * t, a, { fill: "body" })];
  }
}

export interface ShapeBounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

/** The box around a glyph (strokes not included). */
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
        add(s.cx - s.r, s.cy - s.r);
        add(s.cx + s.r, s.cy + s.r);
        break;
    }
  }
  return minX === Infinity ? { minX: 0, minY: 0, maxX: 0, maxY: 0 } : { minX, minY, maxX, maxY };
}

/** Radius of the smallest circle round the centre that holds the whole glyph. */
export function glyphRadius(shapes: readonly Shape[]): number {
  const b = shapeBounds(shapes);
  return Math.hypot(Math.max(Math.abs(b.minX), Math.abs(b.maxX)), Math.max(Math.abs(b.minY), Math.abs(b.maxY)));
}
