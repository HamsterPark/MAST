// The chart of the beam inspector (docs/v2/design/optical_layout_v2.md section 8.4): the 1/e² diameter
// along the optical path of one beam, x = path from the source (mm), y = diameter, a vertical line at
// every component the path meets. The numbers come from beam.ts (beamProfile); this file only lays
// them out in a box of pixels: scales, round ticks, the curve, the marks, the span of the picked segment.
// Pure, so it is tested without a browser (test/opticalLayoutBeamDraw.test.ts).

import type { BeamProfile } from "./beam.ts";
import { niceStep, tickValues } from "./view.ts";

export interface ChartBox {
  width: number;
  height: number;
  /** room for the axes: left (y labels), bottom (x labels), top (component names), right */
  left: number;
  bottom: number;
  top: number;
  right: number;
}

export const CHART_BOX: ChartBox = { width: 300, height: 170, left: 42, bottom: 20, top: 16, right: 8 };

export interface ChartGeometry {
  /** the curve, SVG path data */
  d: string;
  /** the plot area */
  x0: number;
  x1: number;
  y0: number;
  y1: number;
  xTicks: Array<{ x: number; label: string }>;
  yTicks: Array<{ y: number; label: string }>;
  /** the unit of the y axis: µm when the largest diameter is below 0.1 mm */
  yUnit: "mm" | "µm";
  marks: Array<{ x: number; componentId: string; action: string; labelled: boolean }>;
  waists: Array<{ x: number; y: number; dMm: number }>;
  /** the picked segment's span on the x axis, if given */
  span: { x0: number; x1: number } | null;
  /** path (mm) at a pixel x, for the hover read-out */
  pathAt: (px: number) => number;
  /** pixel x of a path (mm) */
  xOf: (pathMm: number) => number;
  /** pixel y of a diameter (mm) */
  yOf: (dMm: number) => number;
}

const trim = (v: number) => String(Number(v.toPrecision(6)));

/** Marks closer than this (px) to the previous labelled one get no name on top of the chart. */
export const MARK_LABEL_GAP_PX = 34;

/**
 * Lays a beam profile out in `box`. The y axis starts at 0 (a focus reads as a dip to the axis) and ends at the
 * largest diameter plus a little; `span` (path mm) shades the picked segment.
 */
export function chartGeometry(profile: BeamProfile, box: ChartBox = CHART_BOX, span: { from: number; to: number } | null = null): ChartGeometry {
  const pts = profile.points;
  const x0 = box.left;
  const x1 = box.width - box.right;
  const y0 = box.height - box.bottom;
  const y1 = box.top;
  let minP = Infinity;
  let maxP = -Infinity;
  let maxD = 0;
  for (const p of pts) {
    minP = Math.min(minP, p.pathMm);
    maxP = Math.max(maxP, p.pathMm);
    maxD = Math.max(maxD, p.dMm);
  }
  for (const m of profile.marks) {
    minP = Math.min(minP, m.pathMm);
    maxP = Math.max(maxP, m.pathMm);
  }
  if (!Number.isFinite(minP)) {
    minP = 0;
    maxP = 1;
  }
  if (!(maxP > minP)) maxP = minP + 1;
  if (!(maxD > 0)) maxD = 1;
  const yUnit: "mm" | "µm" = maxD < 0.1 ? "µm" : "mm";
  const yScale = yUnit === "µm" ? 1000 : 1;
  // a round top for the y axis
  const yStep = niceStep(maxD * yScale * 1.1, 4);
  const yTop = Math.max(yStep, Math.ceil((maxD * yScale * 1.05) / yStep) * yStep) / yScale;
  const xOf = (pathMm: number) => x0 + ((pathMm - minP) / (maxP - minP)) * (x1 - x0);
  const yOf = (dMm: number) => y0 - (dMm / yTop) * (y0 - y1);
  const pathAt = (px: number) => minP + ((px - x0) / (x1 - x0)) * (maxP - minP);
  const d = pts.map((p, i) => `${i === 0 ? "M" : "L"}${xOf(p.pathMm).toFixed(2)} ${yOf(p.dMm).toFixed(2)}`).join("");
  const xStep = niceStep(maxP - minP, 4);
  const xTicks = tickValues(minP, maxP, xStep).map((v) => ({ x: xOf(v), label: trim(v) }));
  const yTicks = tickValues(0, yTop * yScale, yStep).map((v) => ({ y: yOf(v / yScale), label: trim(v) }));
  let lastLabelled = -Infinity;
  const marks = profile.marks.map((m) => {
    const x = xOf(m.pathMm);
    const labelled = x - lastLabelled >= MARK_LABEL_GAP_PX;
    if (labelled) lastLabelled = x;
    return { x, componentId: m.componentId, action: m.action, labelled };
  });
  const waists = profile.waists.map((w) => ({ x: xOf(w.pathMm), y: yOf(w.dMm), dMm: w.dMm }));
  const sp = span ? { x0: Math.max(x0, xOf(Math.min(span.from, span.to))), x1: Math.min(x1, xOf(Math.max(span.from, span.to))) } : null;
  return { d, x0, x1, y0, y1, xTicks, yTicks, yUnit, marks, waists, span: sp && sp.x1 >= sp.x0 ? sp : null, pathAt, xOf, yOf };
}

/** The profile point nearest to a path (mm), for the hover read-out. */
export function profileAt(profile: BeamProfile, pathMm: number): { pathMm: number; dMm: number } | null {
  let best: { pathMm: number; dMm: number } | null = null;
  for (const p of profile.points) if (!best || Math.abs(p.pathMm - pathMm) < Math.abs(best.pathMm - pathMm)) best = p;
  return best;
}
