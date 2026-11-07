// ════════════════════════════════════════════════════════════════════════════
// View math for the coarse (sample-stage scale) map: fit, zoom, pan, and
// counter-scaling of chrome.
//
// Why a separate module: `CoarseMapPanel.tsx` draws SVG, while the piezo scan map
// (`ScanMapCanvas.tsx`) draws a Konva canvas. The two zoom mechanisms cannot be
// shared: Konva uses the Stage scale/offset, SVG uses `viewBox`. What they share is
// a rule, that chrome must be counter-scaled when zooming, and a rule cannot be
// enforced by shared code, only by shared tests.
//
// ── Why this module exists: a mistake already made twice ─────────────────
//
// In SVG, stroke-width and font-size are in user units, and zooming shrinks the
// viewBox by a factor k, so the same user unit becomes k times thicker on screen.
// Adding wheel zoom without compensation would make text 10x larger at 10x zoom,
// the same defect that the piezo map had with labels not following zoom.
//
// So sizes fall into two classes, and the distinction is named here (otherwise the
// next person would delete `/ k` as redundant):
//
//   - **data**   : physically meaningful quantities: site coordinates, uncertainty
//                  radii, the piezo-range box, the travel-budget boundary. They
//                  MUST scale with zoom; that is what zooming in is for.
//   - **chrome** : quantities that exist only to be visible: stroke width, dash
//                  length, font size, marker radius, the MINIMUM visible radius of
//                  an uncertainty blob, crosshair arm length. They are divided by k,
//                  otherwise zooming in blows the furniture up.
//
// `chromeAt()` is the single entry point for this rule. `test/coarseMapView.test.ts`
// pins the data-unchanged / chrome-divided-by-k boundary itself.
// ════════════════════════════════════════════════════════════════════════════

/** Margin left around the data bounding box when fitting, as a fraction of the span. */
export const COARSE_PAD = 0.14;

/** Minimum zoom = 1: the fitted view is the outermost; zooming out further adds no information and only shrinks the map to a dot. */
export const COARSE_ZOOM_MIN = 1;

/**
 * Maximum zoom.
 *
 * 40x is derived, not arbitrary: the map's purpose is to avoid returning to places
 * already visited, and the default site spacing is 100 steps with uncertainty radii
 * of tens of steps. The fitted span is typically hundreds to thousands of steps; at
 * 40x the visible span is on the order of ten steps, smaller than one site's
 * uncertainty blob, and zooming further leaves only a colour patch with nothing to
 * read. The piezo map uses 25x; this one is larger because the coarse span has a
 * wider dynamic range.
 */
export const COARSE_ZOOM_MAX = 40;

export type CoarseSitePt = {
  x_steps: number;
  y_steps: number;
  uncertainty_steps: number;
  position_known?: boolean;
};

/** Fit result: a SQUARE stage coordinate system centred on (cx, cy) with half-side `half`. */
export type CoarseFit = {
  /** Data centre (steps). Stage coordinates are offsets from it. */
  cx: number;
  cy: number;
  /** Data span (steps), excluding PAD. All chrome base sizes derive from it. */
  span: number;
  /** Viewport half-side (steps), including PAD. At k=1 the viewBox is [-half, +half]^2. */
  half: number;
};

/** Zoom/pan state. `x`/`y` is the VIEWPORT CENTRE in stage coordinates (steps). */
export type CoarseZoom = { k: number; x: number; y: number };

export const COARSE_ZOOM_RESET: CoarseZoom = { k: 1, x: 0, y: 0 };

function num(v: unknown, fallback = 0): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

/**
 * Fit a square viewport to the set of sites.
 *
 * `null` means nothing can be drawn (no sites). Do not invent a default view here:
 * a map with an empty coordinate system and a map saying "no sites yet" convey
 * different information.
 */
export function fitCoarse(
  sites: readonly CoarseSitePt[] | null | undefined,
  siteSpacingSteps?: number | null,
): CoarseFit | null {
  if (!sites || sites.length === 0) return null;
  // Sites with a known position take priority; if there are none, fall back to all (drawn, but dashed as uncertain).
  const known = sites.filter((s) => s.position_known);
  const pts = known.length ? known : sites;
  const xs = pts.map((s) => num(s.x_steps));
  const ys = pts.map((s) => num(s.y_steps));
  const rMax = Math.max(...pts.map((s) => num(s.uncertainty_steps)), 0);
  // Leave at least one site spacing, otherwise a single site collapses the map to a dot.
  const margin = Math.max(rMax * 2, num(siteSpacingSteps, 0) || 100);
  const minX = Math.min(...xs) - margin;
  const maxX = Math.max(...xs) + margin;
  const minY = Math.min(...ys) - margin;
  const maxY = Math.max(...ys) + margin;
  const span = Math.max(maxX - minX, maxY - minY, 1);
  return {
    cx: (minX + maxX) / 2,
    cy: (minY + maxY) / 2,
    span,
    half: span / 2 + span * COARSE_PAD,
  };
}

/** Site (steps) -> stage coordinates. SVG y points down, sample y points up, so y is negated. */
export function toStage(
  fit: CoarseFit,
  p: { x_steps: number; y_steps: number },
): { x: number; y: number } {
  return { x: num(p.x_steps) - fit.cx, y: -(num(p.y_steps) - fit.cy) };
}

/** Currently visible half-side (steps). Zooming in by k shrinks the visible span by k. */
export function visibleHalf(fit: CoarseFit, zoom: CoarseZoom): number {
  const k = clampK(zoom?.k);
  return fit.half / k;
}

function clampK(k: unknown): number {
  const v = num(k, 1);
  return Math.min(COARSE_ZOOM_MAX, Math.max(COARSE_ZOOM_MIN, v));
}

/**
 * Clamp the viewport centre into the fit box so the map cannot be dragged out of view.
 *
 * The allowed range tightens with k: at k=1, h=half and the only valid centre is 0
 * (the fitted view itself). This also means zooming back to 1x re-centres
 * automatically without separate code.
 */
export function clampPan(fit: CoarseFit, zoom: CoarseZoom): CoarseZoom {
  const k = clampK(zoom?.k);
  const h = fit.half / k;
  const lim = Math.max(0, fit.half - h);
  const cx = Math.min(lim, Math.max(-lim, num(zoom?.x)));
  const cy = Math.min(lim, Math.max(-lim, num(zoom?.y)));
  // `+ 0` normalises -0 to 0: harmless for SVG, but a dump of an unzoomed map
  // showing "-0 -0 ..." looks like a bug.
  return { k, x: cx + 0, y: cy + 0 };
}

/**
 * SVG `viewBox` string.
 *
 * Warning: at k=1 and no pan it must equal, character for character, the formula used
 * before zoom existed (`${-half} ${-half} ${2*half} ${2*half}`); otherwise the default
 * view would change silently for users who never zoom. A test pins this.
 */
export function coarseViewBox(fit: CoarseFit, zoom: CoarseZoom = COARSE_ZOOM_RESET): string {
  const z = clampPan(fit, zoom);
  const h = fit.half / z.k;
  return `${z.x - h} ${z.y - h} ${2 * h} ${2 * h}`;
}

/**
 * Zoom anchored at the cursor: the point under the cursor stays fixed on screen.
 *
 * `fx`/`fy` is the cursor position normalised within the SVG element (0..1, top-left
 * is 0). The element and its viewBox are both square, so this is a plain linear
 * mapping and needs no `getScreenCTM`.
 *
 * Without the anchor the viewport would always shrink towards the fit centre, the
 * site being inspected would drift out of view, and zooming in would turn into
 * zooming in and then searching for it again.
 */
export function zoomAtPoint(
  fit: CoarseFit,
  zoom: CoarseZoom,
  fx: number,
  fy: number,
  factor: number,
): CoarseZoom {
  const z = clampPan(fit, zoom);
  const kNext = clampK(z.k * num(factor, 1));
  if (kNext === z.k) return z;
  const h = fit.half / z.k;
  const hNext = fit.half / kNext;
  // Stage point under the cursor (before zooming)
  const wx = z.x - h + num(fx) * 2 * h;
  const wy = z.y - h + num(fy) * 2 * h;
  // After zooming, keep the same stage point at the same normalised position
  return clampPan(fit, {
    k: kNext,
    x: wx - hNext * (2 * num(fx) - 1),
    y: wy - hNext * (2 * num(fy) - 1),
  });
}

/**
 * Drag to pan. `dfx`/`dfy` is the pointer displacement as a fraction of the element side.
 *
 * The viewport centre moves opposite to the pointer, so dragging feels like grabbing
 * the map rather than moving the window.
 */
export function panByFraction(
  fit: CoarseFit,
  zoom: CoarseZoom,
  dfx: number,
  dfy: number,
): CoarseZoom {
  const z = clampPan(fit, zoom);
  const h = fit.half / z.k;
  return clampPan(fit, {
    k: z.k,
    x: z.x - num(dfx) * 2 * h,
    y: z.y - num(dfy) * 2 * h,
  });
}

/**
 * The single entry point for chrome: stroke width, font size, dash length and minimum
 * visible radius all go through it.
 *
 * `base` is derived from the fit span (e.g. `fit.span / 300`); after dividing by k its
 * on-screen size is independent of zoom. Data quantities (site coordinates,
 * uncertainty radii, piezo box, travel-budget box) never go through here: they should
 * scale with zoom, which is what zooming in is for.
 *
 * The function is short enough to look inlinable. Do not inline it: its value is not
 * the arithmetic but that every call site states which class a number belongs to, and
 * that a test can pin the whole rule in one place.
 */
export function chromeAt(base: number, zoom: CoarseZoom | number): number {
  const k = clampK(typeof zoom === "number" ? zoom : zoom?.k);
  return num(base) / k;
}

/** Whether the zoom differs from the fitted view; decides whether to show the reset button. */
export function isZoomed(zoom: CoarseZoom | null | undefined): boolean {
  if (!zoom) return false;
  return clampK(zoom.k) > COARSE_ZOOM_MIN + 1e-9 || num(zoom.x) !== 0 || num(zoom.y) !== 0;
}

/**
 * Display text for the zoom factor.
 *
 * Whole factors have no decimals (`4×`, not `4.0×`): the label sits next to the map
 * and conveys magnitude, not precision.
 */
export function fmtZoom(zoom: CoarseZoom | number): string {
  const k = clampK(typeof zoom === "number" ? zoom : zoom?.k);
  return (k < 10 && k % 1 !== 0 ? k.toFixed(1) : String(Math.round(k))) + "×";
}
