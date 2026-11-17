// ════════════════════════════════════════════════════════════════════════════
// Scan-map geometry — the arithmetic between the three coordinate spaces the
// map lives in, and the measurements made across them.
//
//   world  — Nanonis stage frame, METRES, +y up (lab convention)
//   stage  — Konva stage pixels before the wheel zoom, +y down
//   screen — pixels on the canvas element after the zoom (k, x, y)
//
// Everything a click, the cursor readout or the ruler needs is a conversion
// between those three, and every one of them is easy to get subtly wrong (a
// missing Y flip, a zoom applied twice, a sign on a rotation). They are pure
// here so that each is pinned by a test instead of checked by eye at 25×.
//
// No imports from ../api/schema, so `node --test` runs this without a build.
// ════════════════════════════════════════════════════════════════════════════

export interface Pt {
  x: number;
  y: number;
}

export interface Zoom {
  k: number;
  x: number;
  y: number;
}

/**
 * Wheel-zoom ceiling. STS point groups sit a few nanometres apart, and
 * on a map fitted to a few hundred nanometres 25× would leave them one blob.
 * At 200× a 500 nm map shows ~2.5 nm across the canvas; grid steps go
 * sub-nanometre there, which visibleStepNm / fmtNm handle (tested).
 */
export const MAX_ZOOM = 200;

/** Clamp a zoom factor to [1, MAX_ZOOM]; nonsense reads as 1. */
export function clampZoom(k: number, max: number = MAX_ZOOM): number {
  if (!Number.isFinite(k)) return 1;
  return Math.min(max, Math.max(1, k));
}

// ── world ↔ stage ──────────────────────────────────────────────────────────

/** The metres→pixels fit of a bounding box into the canvas. */
export interface ViewFit {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
  /** pixels per metre */
  scale: number;
  offX: number;
  offY: number;
  canvasH: number;
}

/** Smallest window drawn around a single point (or nothing but a point). */
const FIXED_SPAN_M = 1e-7;

/**
 * Fit a world box into a canvas: squared (equal x/y scale, so footprints keep
 * their shape), padded by 14 %, centred, Y flipped so +y points up.
 * A degenerate box — one point — gets a fixed ~100 nm window rather than a dot.
 */
export function fitView(
  b: { minX: number; maxX: number; minY: number; maxY: number },
  canvasW: number,
  canvasH: number,
  pad: number,
): ViewFit {
  let { minX, maxX, minY, maxY } = b;
  if (maxX - minX <= 0 && maxY - minY <= 0) {
    minX -= FIXED_SPAN_M / 2;
    maxX += FIXED_SPAN_M / 2;
    minY -= FIXED_SPAN_M / 2;
    maxY += FIXED_SPAN_M / 2;
  }
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  const half = Math.max(maxX - minX, maxY - minY, FIXED_SPAN_M) / 2;
  const p = half * 0.14;
  minX = cx - half - p;
  maxX = cx + half + p;
  minY = cy - half - p;
  maxY = cy + half + p;
  const spanX = maxX - minX;
  const spanY = maxY - minY;
  const scale = Math.min((canvasW - 2 * pad) / spanX, (canvasH - 2 * pad) / spanY);
  const offX = pad + (canvasW - 2 * pad - spanX * scale) / 2;
  const offY = pad + (canvasH - 2 * pad - spanY * scale) / 2;
  return { minX, maxX, minY, maxY, scale, offX, offY, canvasH };
}

export function worldToStage(v: ViewFit, xm: number, ym: number): Pt {
  return {
    x: v.offX + (xm - v.minX) * v.scale,
    y: v.canvasH - (v.offY + (ym - v.minY) * v.scale),
  };
}

export function stageToWorld(v: ViewFit, px: number, py: number): Pt {
  return {
    x: v.minX + (px - v.offX) / v.scale,
    y: v.minY + (v.canvasH - py - v.offY) / v.scale,
  };
}

/** Where a screen (canvas-element) point is in stage coordinates. */
export function screenToStage(p: Pt, zoom: Zoom): Pt {
  const k = Number.isFinite(zoom?.k) && zoom.k > 0 ? zoom.k : 1;
  const tx = Number.isFinite(zoom?.x) ? zoom.x : 0;
  const ty = Number.isFinite(zoom?.y) ? zoom.y : 0;
  return { x: (p.x - tx) / k, y: (p.y - ty) / k };
}

/** The world box currently visible on a W×H canvas under `zoom`. */
export function visibleWorld(
  v: ViewFit,
  zoom: Zoom,
  canvasW: number,
  canvasH: number,
): { minX: number; maxX: number; minY: number; maxY: number } {
  const a = screenToStage({ x: 0, y: 0 }, zoom);
  const b = screenToStage({ x: canvasW, y: canvasH }, zoom);
  const wa = stageToWorld(v, a.x, a.y);
  const wb = stageToWorld(v, b.x, b.y);
  return {
    minX: Math.min(wa.x, wb.x),
    maxX: Math.max(wa.x, wb.x),
    minY: Math.min(wa.y, wb.y),
    maxY: Math.max(wa.y, wb.y),
  };
}

/**
 * Grid positions (nm) inside [minNm, maxNm], as integer multiples of the step.
 *
 * Built from multiples rather than by repeatedly adding the step, which drifts
 * (0.1 + 0.1 + 0.1 ≠ 0.3); and bounded to the VISIBLE range, because walking
 * the whole fitted extent would be wasteful — harmless at 1×, but the step shrinks
 * with zoom, and at high zoom that was hundreds of off-canvas grid lines and
 * labels rebuilt on every wheel tick.
 */
export function gridTicks(minNm: number, maxNm: number, stepNm: number, maxCount = 400): number[] {
  if (![minNm, maxNm, stepNm].every(Number.isFinite) || stepNm <= 0 || maxNm < minNm) return [];
  const i0 = Math.ceil(minNm / stepNm - 1e-9);
  const i1 = Math.floor(maxNm / stepNm + 1e-9);
  if (i1 - i0 + 1 > maxCount) return [];
  const out: number[] = [];
  // `+ 0` turns the −0 that ceil(−ε) produces into 0.
  for (let i = i0; i <= i1; i++) out.push(i * stepNm + 0);
  return out;
}

// ── hit-testing the underlay ───────────────────────────────────────────────

/**
 * Is a world point inside a rotated rectangle (centre, size, angle in degrees,
 * counter-clockwise positive — the convention the canvas draws with)?
 */
export function inFootprint(
  x: number,
  y: number,
  cx: number,
  cy: number,
  w: number,
  h: number,
  angleDeg = 0,
): boolean {
  if (![x, y, cx, cy, w, h].every(Number.isFinite)) return false;
  const a = ((Number.isFinite(angleDeg) ? angleDeg : 0) * Math.PI) / 180;
  const dx = x - cx;
  const dy = y - cy;
  // Rotate the offset by −a into the rectangle's own axes.
  const lx = dx * Math.cos(a) + dy * Math.sin(a);
  const ly = -dx * Math.sin(a) + dy * Math.cos(a);
  return Math.abs(lx) <= Math.abs(w) / 2 && Math.abs(ly) <= Math.abs(h) / 2;
}

interface FootprintLike {
  center_x_m?: number | null;
  center_y_m?: number | null;
  width_m?: number | null;
  height_m?: number | null;
  angle_deg?: number | null;
}

/**
 * The items whose footprint contains a world point, TOPMOST FIRST.
 *
 * The list is in draw order (the underlay comes oldest-first so the newest
 * frame paints on top), so the last match is the picture the operator is
 * actually looking at.
 */
export function footprintsAt<T extends FootprintLike>(items: T[], x: number, y: number): T[] {
  const hits: T[] = [];
  for (const it of items) {
    if (it.center_x_m == null || it.center_y_m == null || !it.width_m || !it.height_m) continue;
    if (inFootprint(x, y, it.center_x_m, it.center_y_m, it.width_m, it.height_m, it.angle_deg ?? 0)) {
      hits.push(it);
    }
  }
  return hits.reverse();
}

// ── cursor readout and the ruler ───────────────────────────────────────────

const MINUS = "−";

/**
 * Decimal places for a nanometre coordinate shown at `nmPerPx` nanometres per
 * screen pixel: enough that the last digit is about one pixel, never more —
 * a cursor cannot be placed more precisely than that, so more digits would be
 * noise dressed up as precision. Grows as the operator zooms in.
 */
export function coordDecimals(nmPerPx: number): number {
  if (!Number.isFinite(nmPerPx) || nmPerPx <= 0) return 1;
  return Math.min(4, Math.max(0, Math.ceil(-Math.log10(nmPerPx))));
}

/** A signed nanometre value with a real minus sign ("−56.7"), never "-0". */
export function fmtSignedNm(vNm: number, decimals: number): string {
  if (!Number.isFinite(vNm)) return "";
  const d = Math.min(6, Math.max(0, Math.round(Number.isFinite(decimals) ? decimals : 0)));
  const s = (+(vNm + 0).toFixed(d)).toFixed(d);
  if (Number(s) === 0) return (0).toFixed(d);
  return s.startsWith("-") ? MINUS + s.slice(1) : s;
}

export interface Measurement {
  /** metres */
  d: number;
  dx: number;
  dy: number;
}

/** Distance and components from A to B (world metres, +y up). */
export function measure(a: Pt, b: Pt): Measurement {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  return { d: Math.hypot(dx, dy), dx, dy };
}

/** "d = 123.4 nm (Δx 100.0 nm, Δy −72.3 nm)". */
export function fmtMeasurement(m: Measurement, decimals: number, zh = false): string {
  const f = (v: number) => `${fmtSignedNm(v * 1e9, decimals)} nm`;
  return zh
    ? `d = ${f(m.d)}（Δx ${f(m.dx)}，Δy ${f(m.dy)}）`
    : `d = ${f(m.d)} (Δx ${f(m.dx)}, Δy ${f(m.dy)})`;
}

// ── numbered badges that do not sit on each other ─────────────────────────

/**
 * Nudge round badges (radius `r`, screen px) apart, greedily, in order: each
 * keeps its anchor if that is free, else takes the nearest free spot on a ring
 * around it. Earlier badges win, so step 1 never moves to make room for step 7.
 *
 * Plan steps are often STS points a few nanometres apart; at the fitted zoom
 * their numbers land on one another and the route reads as a smudge.
 */
export function layoutBadges(anchors: Pt[], r: number, gap = 2): Pt[] {
  const out: Pt[] = [];
  const minD = 2 * r + gap;
  const free = (p: Pt) => out.every((q) => Math.hypot(p.x - q.x, p.y - q.y) >= minD - 1e-9);
  for (const a of anchors) {
    if (!Number.isFinite(a?.x) || !Number.isFinite(a?.y)) {
      out.push(a);
      continue;
    }
    let placed: Pt | null = free(a) ? a : null;
    for (let ring = 1; !placed && ring <= 4; ring++) {
      const rad = ring * minD;
      const n = 8 * ring;
      for (let i = 0; i < n && !placed; i++) {
        // Start up-right (where the badge naturally sits) and go round.
        const th = -Math.PI / 4 + (2 * Math.PI * i) / n;
        const p = { x: a.x + rad * Math.cos(th), y: a.y + rad * Math.sin(th) };
        if (free(p)) placed = p;
      }
    }
    out.push(placed ?? a);
  }
  return out;
}

// ── focusing on one thing ──────────────────────────────────────────────────

/**
 * The zoom that centres a stage point and makes `sizePx` (its footprint at 1×)
 * fill about `fill` of the canvas — or, for a point, shows `pointSpanPx` of
 * 1× pixels around it. Clamped to [1, maxK].
 */
export function zoomToward(
  p: Pt,
  sizePx: number,
  canvasW: number,
  canvasH: number,
  opts: { maxK?: number; fill?: number; pointSpanPx?: number } = {},
): Zoom {
  const maxK = opts.maxK ?? MAX_ZOOM;
  const fill = opts.fill ?? 0.4;
  const pointSpan = opts.pointSpanPx ?? 60;
  const span = Number.isFinite(sizePx) && sizePx > 0 ? sizePx / fill : pointSpan;
  const k = clampZoom(Math.min(canvasW, canvasH) / span, maxK);
  return { k, x: canvasW / 2 - p.x * k, y: canvasH / 2 - p.y * k };
}

// ── drawing helpers ────────────────────────────────────────────────────────

/** "#38bdf8" + 0.1 → "rgba(56,189,248,0.1)". Unparseable input comes back as is. */
export function withAlpha(hex: string, alpha: number): string {
  const m = /^#?([0-9a-f]{6})$/i.exec((hex ?? "").trim());
  if (!m) return hex;
  const n = parseInt(m[1]!, 16);
  const a = Math.min(1, Math.max(0, Number.isFinite(alpha) ? alpha : 1));
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

/**
 * Approximate rendered width of a one-line string: CJK and full-width glyphs at
 * one em, everything else at 0.6 em. Good enough to size a tooltip box or keep a
 * label on the canvas; not a layout engine.
 */
export function textWidthPx(s: string, fontSize: number): number {
  let w = 0;
  for (const ch of s ?? "") {
    const c = ch.codePointAt(0) ?? 0;
    const wide =
      (c >= 0x2e80 && c <= 0x9fff) || (c >= 0xac00 && c <= 0xd7af) || (c >= 0xff00 && c <= 0xffef) ||
      (c >= 0x3000 && c <= 0x303f);
    w += wide ? fontSize : 0.6 * fontSize;
  }
  return w;
}

/** WCAG relative-luminance contrast ratio of two "#rrggbb" colours (1…21), or null. */
export function contrastRatio(a: string, b: string): number | null {
  const lum = (hex: string): number | null => {
    const m = /^#?([0-9a-f]{6})$/i.exec((hex ?? "").trim());
    if (!m) return null;
    const n = parseInt(m[1]!, 16);
    const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * ch[0]! + 0.7152 * ch[1]! + 0.0722 * ch[2]!;
  };
  const la = lum(a);
  const lb = lum(b);
  if (la == null || lb == null) return null;
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * The outline a marker dot needs to stand off the map background.
 *
 * Kind colours are a fixed cross-language contract, so they cannot be retuned
 * per theme; but several of them sit close to one background or the other —
 * the crash red is 2.3:1 on the dark map, STS green and pulse amber about 2:1
 * on the light one. Below `min` contrast the dot gets an outline in the
 * theme's ink; otherwise a background-coloured halo, which separates it from a
 * thumbnail underneath without changing its colour.
 */
export function dotOutline(color: string, bg: string, ink: string, min = 3): string {
  const r = contrastRatio(color, bg);
  return r != null && r < min ? ink : bg;
}
