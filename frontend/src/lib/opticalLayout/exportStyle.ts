// The paper-style export (docs/v2/design/optical_layout_v2.md section 8.5): white background, black line
// art, coloured beams, names, a scale bar, holes only if asked for; a figure that goes straight into a
// paper or a group-meeting slide.
//
// The picture is the canvas component itself (<LayoutSvg variant="paper">) rendered to a string, so it
// cannot drift from what the editor draws. What makes it "paper" is decided here, as data:
//   * PAPER_PALETTE: every design token the canvas paints with, mapped to a print colour (the file must not
//     keep a var(), and must not depend on the theme the operator happens to use);
//   * paperFrame(): the part of the table worth printing (the components, the beams, the notes; not the
//     empty table), and the scale it is drawn at;
//   * scaleBar(): a round length for the bar.
// Pure, so it is tested without a browser (test/opticalLayoutExport.test.ts).

import type { Bounds } from "./geometry.ts";
import type { LayoutComponent, LayoutText, TableSpec, Vec } from "./types.ts";
import type { View } from "./view.ts";

/** Print colours of the design tokens: black ink, greys for housings, a pale blue for glass. */
export const PAPER_PALETTE: Readonly<Record<string, string>> = Object.freeze({
  "--mast-bg": "#ffffff",
  "--mast-panel": "#ffffff",
  "--mast-panel-2": "#f3f3f3",
  "--mast-border": "#d4d4d4",
  "--mast-border-strong": "#9b9b9b",
  "--mast-text": "#000000",
  "--mast-muted": "#4a4a4a",
  "--mast-faint": "#8a8a8a",
  "--mast-info": "#3d7fb8",
  "--mast-accent": "#000000",
  "--mast-accent-soft": "#e6e6e6",
  "--mast-warn": "#b45309",
  "--mast-danger": "#b91c1c",
  "--mast-auto": "#047857",
});

/** The print colour of a design token, or null for one the palette does not know. */
export function paperColor(name: string): string | null {
  return PAPER_PALETTE[name.trim()] ?? null;
}

/** Fonts that every viewer has, so the file looks the same in a slide program as in the browser. */
export const PAPER_FONT = "Helvetica, Arial, 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif";

export interface ScaleBar {
  /** the length the bar stands for, mm */
  mm: number;
  /** its length on the picture, px */
  px: number;
  label: string;
}

/** A bar of 1, 2 or 5 times a power of ten millimetres, as long as possible but no longer than `maxPx`. */
export function scaleBar(k: number, maxPx = 160): ScaleBar {
  const limit = Math.max(1e-9, maxPx / Math.max(k, 1e-12));
  let best = 0;
  for (let e = -3; e <= 7; e++) {
    for (const f of [1, 2, 5]) {
      const v = f * 10 ** e;
      if (v <= limit * (1 + 1e-9)) best = v;
    }
  }
  if (best === 0) best = 1e-3;
  const mm = Number(best.toPrecision(6));
  const label = mm >= 1000 ? `${Number((mm / 1000).toPrecision(6))} m` : mm >= 1 ? `${mm} mm` : `${Number((mm * 1000).toPrecision(6))} µm`;
  return { mm, px: mm * k, label };
}

export interface PaperFrame {
  view: View;
  width: number;
  height: number;
  /** what was framed, mm */
  bounds: Bounds;
}

export interface FrameInput {
  table: TableSpec;
  components: readonly LayoutComponent[];
  texts: readonly LayoutText[];
  /** where the beams run, mm (each branch's polyline) */
  beamPoints: Iterable<Vec>;
  /**
   * a component's drawing, as a box in table coordinates, when the figure is drawn at `k` px/mm (its texts and
   * dials are drawn larger at a low zoom); null: before the scale is known
   */
  componentBox: (c: LayoutComponent, k: number | null) => Bounds;
  /**
   * the boxes (px) of what the figure writes at `view`: names, power tags, text notes. They keep their pixel size
   * whatever the scale, so how far they reach in millimetres is only known once the scale is: the frame is worked
   * out again with them until it holds them all.
   */
  screenBoxes?: (view: View) => Iterable<{ x: number; y: number; w: number; h: number }>;
  /** picture width the drawing is scaled to (px) */
  targetWidth?: number;
  /** and the height it may not exceed */
  maxHeight?: number;
  /** room round the drawing, px */
  pad?: number;
  /** room under the drawing for the scale bar, px */
  footer?: number;
}

/** Beams that leave the table run on for a metre; the picture stops them this far past the table edge (mm). */
export const BEAM_OVERHANG_MM = 30;

/** At most this many passes of framing the texts (each one shrinks the scale a little; two or three settle it). */
export const FRAME_PASSES = 6;

/**
 * The frame of the paper picture: the box round the components' drawings, the notes and the beams (clipped
 * to the table plus a little), scaled to `targetWidth` (or less, when the drawing is tall), with `pad`
 * pixels round it and `footer` pixels below for the scale bar. An empty layout frames the whole table.
 *
 * Then, with `screenBoxes`, the texts: framed at that scale, a name or a power at the edge of the drawing (or a
 * note running right of its anchor) would be cut off by the edge of the picture. Their boxes are turned back into
 * millimetres and added, the frame is worked out again (a little smaller in scale), and so on until every box is
 * inside; the box only grows, so it settles in a pass or two.
 */
export function paperFrame(input: FrameInput): PaperFrame {
  const { table } = input;
  const targetWidth = input.targetWidth ?? 1100;
  const maxHeight = input.maxHeight ?? 1500;
  const pad = input.pad ?? 28;
  const footer = input.footer ?? 34;
  const box = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
  const add = (x: number, y: number) => {
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    box.minX = Math.min(box.minX, x);
    box.maxX = Math.max(box.maxX, x);
    box.minY = Math.min(box.minY, y);
    box.maxY = Math.max(box.maxY, y);
  };
  const addComponents = (k: number | null) => {
    for (const c of input.components) {
      const b = input.componentBox(c, k);
      add(b.minX, b.minY);
      add(b.maxX, b.maxY);
    }
  };
  addComponents(null);
  for (const t of input.texts) add(t.x_mm, t.y_mm);
  const lo = { x: -BEAM_OVERHANG_MM, y: -BEAM_OVERHANG_MM };
  const hi = { x: table.width_mm + BEAM_OVERHANG_MM, y: table.height_mm + BEAM_OVERHANG_MM };
  for (const p of input.beamPoints) add(Math.min(hi.x, Math.max(lo.x, p.x)), Math.min(hi.y, Math.max(lo.y, p.y)));
  if (box.minX === Infinity) {
    box.minX = 0;
    box.minY = 0;
    box.maxX = table.width_mm;
    box.maxY = table.height_mm;
  }
  const frameOf = (): PaperFrame => {
    // a single point or a line still gets an area
    const w = Math.max(box.maxX - box.minX, 20);
    const h = Math.max(box.maxY - box.minY, 20);
    const cx = (box.minX + box.maxX) / 2;
    const cy = (box.minY + box.maxY) / 2;
    const bounds = { minX: cx - w / 2, maxX: cx + w / 2, minY: cy - h / 2, maxY: cy + h / 2 };
    const k = Math.min(targetWidth / w, Math.max(1, maxHeight - footer) / h);
    const width = Math.ceil(w * k + 2 * pad);
    const height = Math.ceil(h * k + 2 * pad + footer);
    return { view: { k, ox: pad - bounds.minX * k, oy: pad + bounds.maxY * k }, width, height, bounds };
  };
  let frame = frameOf();
  if (!input.screenBoxes && input.components.length === 0) return frame;
  for (let pass = 0; pass < FRAME_PASSES; pass++) {
    const { k, ox, oy } = frame.view;
    const before = { ...box };
    addComponents(k);
    if (input.screenBoxes) {
      for (const b of input.screenBoxes(frame.view)) {
        add((b.x - ox) / k, (oy - (b.y + b.h)) / k);
        add((b.x + b.w - ox) / k, (oy - b.y) / k);
      }
    }
    // settled: nothing reaches more than a quarter of a pixel past the box
    const slack = 0.25 / k;
    if (box.minX >= before.minX - slack && box.maxX <= before.maxX + slack && box.minY >= before.minY - slack && box.maxY <= before.maxY + slack) break;
    frame = frameOf();
  }
  return frame;
}

/** The box round a component's drawing in table coordinates, from its glyph box in its own frame. */
export function rotatedBox(c: Pick<LayoutComponent, "x_mm" | "y_mm" | "angle_deg">, local: Bounds): Bounds {
  const t = c.angle_deg * (Math.PI / 180);
  const cos = Math.cos(t);
  const sin = Math.sin(t);
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const [lx, ly] of [
    [local.minX, local.minY],
    [local.minX, local.maxY],
    [local.maxX, local.minY],
    [local.maxX, local.maxY],
  ] as const) {
    const x = c.x_mm + lx * cos - ly * sin;
    const y = c.y_mm + lx * sin + ly * cos;
    minX = Math.min(minX, x);
    maxX = Math.max(maxX, x);
    minY = Math.min(minY, y);
    maxY = Math.max(maxY, y);
  }
  return { minX, minY, maxX, maxY };
}

export interface LegendEntry {
  /** null: light of no known wavelength */
  nm: number | null;
}

/**
 * The legend of the figure: every wavelength the trace carries, in the order the beams are listed, each once;
 * and whether some of it is drawn as "no light" (crossed polarisers, an opaque filter), which the legend then
 * explains too.
 */
export function legendOf(trace: ReadonlyArray<{ wavelength_nm: number | null; branches: ReadonlyArray<{ fraction: number; segments: ReadonlyArray<{ beam?: { p: number } }> }> }>, darkP: number): { entries: LegendEntry[]; dark: boolean } {
  const seen = new Set<number | null>();
  const entries: LegendEntry[] = [];
  let dark = false;
  for (const t of trace) {
    if (!seen.has(t.wavelength_nm)) {
      seen.add(t.wavelength_nm);
      entries.push({ nm: t.wavelength_nm });
    }
    for (const b of t.branches) for (const g of b.segments) if (!((g.beam ? g.beam.p : b.fraction) >= darkP)) dark = true;
  }
  return { entries, dark };
}

/** A file name for the paper export: `<id>-r<revision>-figure.<ext>`. */
export function paperFileName(layout: { id: string; revision: number }, ext: "svg" | "png", unsaved = false): string {
  const id = layout.id.replace(/[^A-Za-z0-9_-]/g, "_") || "layout";
  return `${id}-r${Math.max(0, Math.floor(layout.revision))}${unsaved ? "-unsaved" : ""}-figure.${ext}`;
}
