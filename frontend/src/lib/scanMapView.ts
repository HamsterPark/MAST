// ════════════════════════════════════════════════════════════════════════════
// Scan-map view rules — which layers are drawn, and what area is in view.
//
// Pure functions, deliberately: the map grew to eight kinds of overlay on one
// 620×460 canvas (surface mosaic, keep-out discs, planned route, the route
// ahead, twelve marker kinds, grid, live frame, tip), and "which of those is
// visible" plus "what patch of surface is in view" are now decisions with
// enough edge cases to deserve tests. Konva rendering itself is not testable
// here (no DOM), so everything that CAN be decided in plain data is decided in
// this file and the component just draws the answer.
//
// No imports from ../api/schema on purpose: the structural types below are the
// small subset actually read, which keeps this module runnable under
// `node --test` (npm run test:unit) with no build step.
// ════════════════════════════════════════════════════════════════════════════

import { tr } from "./i18n.ts";
import {
  ALL_TIME,
  browserOffsetMin,
  fmtAgo,
  fmtDateTime,
  fmtUtcOffset,
  inWindow,
  parseStamp,
  stampOffsetMin,
  wallClockOf,
  type TimeWindow,
} from "./scanMapTime.ts";

/** A drawable layer the operator can switch off. */
export type MapLayerId =
  | "underlay" // saved .sxm thumbnails placed by real stage footprint
  | "live" // the scan in progress, row by row (LiveHub), and its scan line
  | "grid" // nm grid + axis ticks
  | "avoid" // keep-out discs from the analysis
  | "history" // executed markers in the LIVE coordinate generation
  | "stale" // executed markers from a superseded generation (faded)
  | "plan" // the published route (status="planned")
  | "upcoming" // the route the strategy would walk next (ghost frames)
  | "next"; // the single recommended next position

export type MapLayers = Record<MapLayerId, boolean>;

/** How much surface the canvas fits into view. */
export type MapViewMode =
  | "fit" // everything there is to draw (the historical behaviour)
  | "follow" // the live scan frame and its immediate surroundings
  | "full"; // the whole reachable piezo area

export const MAP_LAYER_IDS: MapLayerId[] = [
  "underlay", "live", "grid", "avoid", "history", "stale", "plan", "upcoming", "next",
];

/** Human labels for the layer switches (kept next to the ids on purpose). */
export const MAP_LAYER_LABELS: Record<MapLayerId, string> = {
  underlay: tr("扫描底图", "Scan underlay"),
  live: tr("实时扫描", "Live scan"),
  grid: tr("网格刻度", "Grid and ticks"),
  avoid: tr("避让区", "Keep-out zones"),
  history: tr("历史操作", "History"),
  stale: tr("旧坐标系", "Old coordinates"),
  plan: tr("计划路线", "Planned route"),
  upcoming: tr("候选序列", "Candidate sequence"),
  next: tr("建议位置", "Suggested position"),
};

/** One-line rationale shown under each switch. */
export const MAP_LAYER_HINTS: Record<MapLayerId, string> = {
  underlay: tr("已保存 .sxm 按真实舞台坐标铺底", "Saved .sxm images laid out at their real stage coordinates"),
  live: tr(
    "正在扫的这一帧逐行画在扫描框上，带扫描线；不可用时退回 1/8 进度快照",
    "The frame being scanned, drawn row by row in the scan frame with a scan line; falls back to the 1/8-progress snapshot",
  ),
  grid: tr("nm 网格与坐标轴刻度", "nm grid and axis ticks"),
  avoid: tr("破坏/污染区域，规划会绕开", "Damaged / contaminated areas; planning routes around them"),
  history: tr("当前坐标系里已执行的操作", "Operations already executed in the current coordinate system"),
  stale: tr(
    "粗动前的旧坐标与旧扫描图（淡显；关掉则完全隐藏）",
    "Markers and scan images from before the coarse move (faded; hidden when switched off)",
  ),
  plan: tr("已发布的计划路线，按执行顺序编号", "Published planned route, numbered in execution order"),
  upcoming: tr("当前策略接下来会走的位置", "Positions the current strategy would visit next"),
  next: tr("分析给出的下一个推荐位置", "Next recommended position from the analysis"),
};

export const DEFAULT_LAYERS: MapLayers = {
  underlay: true, live: true, grid: true, avoid: true, history: true,
  stale: true, plan: true, upcoming: true, next: true,
};

export interface MapPreset {
  id: string;
  label: string;
  hint: string;
  layers: MapLayers;
}

/**
 * Preset layer sets.
 *
 * A preset is a one-shot setter, NOT a mode: clicking it writes these eight
 * booleans and then every switch stays independent. That is why there is no
 * "custom" state to store — {@link matchPreset} derives the highlight from the
 * booleans themselves, so the two controls can never disagree about what is
 * actually being drawn.
 */
export const MAP_PRESETS: MapPreset[] = [
  {
    id: "all",
    label: tr("全部", "All"),
    hint: tr("所有图层", "All layers"),
    layers: { ...DEFAULT_LAYERS },
  },
  {
    id: "minimal",
    label: tr("精简", "Minimal"),
    hint: tr("只看表面与当前坐标系里做过什么", "Only the surface and what was done in the current coordinate system"),
    layers: {
      underlay: true, live: true, grid: false, avoid: false, history: true,
      stale: false, plan: false, upcoming: false, next: false,
    },
  },
  {
    id: "planning",
    label: tr("规划聚焦", "Planning focus"),
    hint: tr("去掉缩略图与旧坐标，突出去哪儿", "Hide thumbnails and old coordinates to highlight where to go"),
    layers: {
      underlay: false, live: false, grid: true, avoid: true, history: true,
      stale: false, plan: true, upcoming: true, next: true,
    },
  },
];

export const MAP_VIEW_MODES: { id: MapViewMode; label: string; hint: string }[] = [
  { id: "fit", label: tr("自动拟合", "Auto fit"), hint: tr("把所有内容装进画面", "Fit everything into view") },
  { id: "follow", label: tr("跟随扫描框", "Follow scan frame"), hint: tr("只看当前扫描框附近", "Only the area around the current scan frame") },
  {
    id: "full",
    label: tr("压电全域", "Full piezo range"),
    hint: tr("整个可达范围，看清位置在量程的哪里", "The whole reachable range, showing where the position sits within it"),
  },
];

/**
 * Fill in any layer the stored value is missing.
 *
 * Persisted UI state outlives the code that wrote it: a layer added later would
 * otherwise arrive as `undefined` and read as OFF, so an operator who used the
 * map once would never see the new overlay and would have no way to know it
 * existed.
 */
export function withLayerDefaults(stored?: Partial<MapLayers> | null): MapLayers {
  const out = { ...DEFAULT_LAYERS };
  for (const id of MAP_LAYER_IDS) {
    const v = stored?.[id];
    if (typeof v === "boolean") out[id] = v;
  }
  return out;
}

/** The preset whose layer set is exactly this one, or null (= custom). */
export function matchPreset(layers: MapLayers): string | null {
  for (const p of MAP_PRESETS) {
    if (MAP_LAYER_IDS.every((id) => p.layers[id] === layers[id])) return p.id;
  }
  return null;
}

// ── analysis refresh ───────────────────────────────────────────────────────

/**
 * Smallest gap between two AUTOMATIC analysis calls.
 *
 * `analyze_map` rasterises the whole piezo area and walks the candidate route —
 * a ~1 s budget the backend deliberately keeps OFF the 3 s map poll. But the
 * overlays it feeds (keep-out discs, the recommendation, the route ahead) are
 * now layers the operator can leave switched on, and a layer that only updates
 * when someone remembers to press a button is a layer that quietly lies.
 *
 * 10 s resolves that: markers arrive in bursts (the manual-activity watcher
 * ticks at 1.5 s, an import writes many at once), and this collapses a burst
 * into at most one analysis per 10 s while still guaranteeing the LAST change
 * lands — see {@link nextAnalysisDelayMs}, which asks for a trailing call
 * rather than dropping it.
 */
export const MIN_AUTO_ANALYSIS_INTERVAL_MS = 10_000;

/**
 * How stale an analysis may get while its layers are on, even with no new
 * markers. Not everything it depends on produces one: the frame size comes from
 * the live scan width, so changing the scan size in Nanonis moves every
 * candidate position without touching the record.
 */
export const MAX_ANALYSIS_AGE_MS = 60_000;

interface MapLike {
  marker_count?: number | null;
  current_epoch?: number | null;
  degraded?: boolean | null;
  markers?: { status?: string | null }[] | null;
}

/**
 * A value that changes exactly when the analysis would come out different.
 *
 * Everything in it rides on the 3 s map poll that is happening anyway, so
 * watching for changes costs no extra request:
 *   • `marker_count` — an operation was recorded (executed markers only)
 *   • `current_epoch` — a coarse move invalidated every old coordinate
 *   • planned-step count — a route was published, cleared, or advanced
 */
export function analysisSignature(map?: MapLike | null): string {
  if (!map) return "";
  const planned = (map.markers ?? []).filter(
    (m) => (m?.status ?? "") === "planned",
  ).length;
  return `${map.marker_count ?? 0}|${map.current_epoch ?? 0}|${planned}`;
}

/** Whether any layer on screen actually needs the analysis payload. */
export function analysisAutoEnabled(layers: MapLayers): boolean {
  return layers.avoid || layers.next || layers.upcoming;
}

/**
 * Milliseconds to wait before the next automatic analysis: 0 = go now.
 *
 * Leading-edge with a trailing catch-up. A burst of markers fires this many
 * times; the first call goes immediately, the rest collapse into ONE delayed
 * call after the interval, so the operator ends up looking at the state after
 * the burst rather than the state in the middle of it.
 */
export function nextAnalysisDelayMs(
  now: number,
  lastFetchedAt: number | null | undefined,
  minIntervalMs: number = MIN_AUTO_ANALYSIS_INTERVAL_MS,
): number {
  if (lastFetchedAt == null || !Number.isFinite(lastFetchedAt)) return 0;
  const elapsed = now - lastFetchedAt;
  if (!Number.isFinite(elapsed) || elapsed >= minIntervalMs) return 0;
  // Clock stepped backwards (NTP correction, a manual system-time change). The
  // stored timestamp is now in the future, and subtracting it would schedule the
  // next analysis that far out — the overlays would sit frozen for as long as the
  // jump. An unusable timestamp means "no idea when we last ran": go now.
  if (elapsed < 0) return 0;
  return Math.max(0, minIntervalMs - elapsed);
}

// ── view bounds ────────────────────────────────────────────────────────────

export interface Bounds {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

export interface BoundsResult {
  bounds: Bounds | null;
  /** True when the requested mode could not be honoured and fit was used. */
  fellBack: boolean;
}

interface XY {
  x_m?: number | null;
  y_m?: number | null;
  w_m?: number | null;
  h_m?: number | null;
}

interface FrameLike {
  center_x_m?: number | null;
  center_y_m?: number | null;
  width_m?: number | null;
  height_m?: number | null;
}

interface BoundsMap {
  frame?: FrameLike | null;
  tip_xyz?: { x_m?: number | null; y_m?: number | null } | null;
  markers?: ({ status?: string | null; coord_epoch?: number | null } & XY)[] | null;
  scan_images?: ScanImageLike[] | null;
  current_epoch?: number | null;
}

interface BoundsAnalysis {
  avoid_zones?: { x_m: number; y_m: number; radius_m: number }[] | null;
  next_position?: { x_m?: number | null; y_m?: number | null } | null;
  upcoming?: { x_m?: number | null; y_m?: number | null }[] | null;
  frame_size_m?: number | null;
}

/** Half-width of the window that "follow" puts around the live scan frame:
 *  the frame plus enough context to see what is next to it. */
const FOLLOW_FRAME_FACTOR = 1.4;

/** Accumulates the metre-space extent of everything actually drawn. */
class Extent {
  readonly xs: number[] = [];
  readonly ys: number[] = [];

  add(x?: number | null, y?: number | null, w?: number | null, h?: number | null): void {
    if (x == null || y == null || !Number.isFinite(x) || !Number.isFinite(y)) return;
    const hw = Math.abs(w ?? 0) / 2;
    const hh = Math.abs(h ?? 0) / 2;
    this.xs.push(x - hw, x + hw);
    this.ys.push(y - hh, y + hh);
  }

  bounds(): Bounds | null {
    if (!this.xs.length || !this.ys.length) return null;
    return {
      minX: Math.min(...this.xs), maxX: Math.max(...this.xs),
      minY: Math.min(...this.ys), maxY: Math.max(...this.ys),
    };
  }
}

// ── saved-scan thumbnails (the surface underlay) ───────────────────────────

/** Opacity of a saved-scan thumbnail drawn at full strength. */
export const SCAN_IMAGE_OPACITY = 0.92;

/**
 * Opacity multiplier for anything whose coordinate belongs to a superseded
 * coordinate generation. A lateral coarse move slides the sample stage, so
 * those numbers now address a different patch of surface — the thing is real
 * history, but it is NOT where it is drawn any more. Faded rather than hidden:
 * the operator should still be able to see that work happened.
 */
export const STALE_EPOCH_OPACITY = 0.22;

/** The subset of `ScanImage` the view rules read. Structural, so tests need no API types. */
export interface ScanImageLike {
  center_x_m?: number | null;
  center_y_m?: number | null;
  width_m?: number | null;
  height_m?: number | null;
  angle_deg?: number | null;
  stale_epoch?: boolean | null;
  name?: string | null;
  path?: string | null;
  mtime?: string | null;
  /** The scan in progress (the vision monitor's 1/8 milestone image), not a saved .sxm. */
  live?: boolean | null;
}

/**
 * How a saved-scan thumbnail is drawn: its opacity, or `null` when it is not
 * drawn at all.
 *
 * A `.sxm` written before the last lateral coarse move carries header
 * coordinates from a frame that no longer exists, so drawn at full strength it
 * paints a picture over surface it never imaged — and looks exactly as
 * trustworthy as a current one. That is the thing `stale_epoch` exists to
 * prevent. Such an image follows the same
 * rule as a stale marker: faded by the same factor, and hidden with the
 * "old coordinates" layer.
 */
export function scanImageOpacity(
  img: { stale_epoch?: boolean | null },
  layers: Pick<MapLayers, "underlay" | "stale">,
): number | null {
  if (!layers.underlay) return null;
  if (img?.stale_epoch === true) {
    return layers.stale ? SCAN_IMAGE_OPACITY * STALE_EPOCH_OPACITY : null;
  }
  return SCAN_IMAGE_OPACITY;
}

/** True when this marker's coordinate predates a lateral coarse move. */
export function isStaleEpoch(
  m: { coord_epoch?: number | null },
  currentEpoch?: number | null,
): boolean {
  return currentEpoch != null && m.coord_epoch != null && m.coord_epoch < currentEpoch;
}

/**
 * The metre-space box to fit into the canvas, for the requested mode.
 *
 * Only what is actually DRAWN is fitted — a switched-off layer must not keep
 * pushing the view open around content nobody can see, which is the whole
 * reason the "Minimal" preset is a usable escape from a crowded map.
 */
export function boundsForMode(
  mode: MapViewMode,
  map: BoundsMap | null | undefined,
  analysis: BoundsAnalysis | null | undefined,
  layers: MapLayers,
  piezoHalfM?: number | null,
): BoundsResult {
  if (mode === "full") {
    const half = Number(piezoHalfM ?? 0);
    if (Number.isFinite(half) && half > 0) {
      return { bounds: { minX: -half, maxX: half, minY: -half, maxY: half }, fellBack: false };
    }
    // No range to show: fitting is better than an empty canvas, but say so.
    return { ...fitBounds(map, analysis, layers), fellBack: true };
  }

  if (mode === "follow") {
    const f = map?.frame;
    const cx = f?.center_x_m;
    const cy = f?.center_y_m;
    const w = Math.abs(Number(f?.width_m ?? 0));
    const h = Math.abs(Number(f?.height_m ?? 0));
    const span = Math.max(w, h);
    if (cx != null && cy != null && Number.isFinite(cx) && Number.isFinite(cy) && span > 0) {
      const half = (span * FOLLOW_FRAME_FACTOR) / 2;
      return {
        bounds: { minX: cx - half, maxX: cx + half, minY: cy - half, maxY: cy + half },
        fellBack: false,
      };
    }
    // Nothing is being scanned right now — there is no frame to follow.
    return { ...fitBounds(map, analysis, layers), fellBack: true };
  }

  return fitBounds(map, analysis, layers);
}

function fitBounds(
  map: BoundsMap | null | undefined,
  analysis: BoundsAnalysis | null | undefined,
  layers: MapLayers,
): BoundsResult {
  const ext = new Extent();

  const f = map?.frame;
  if (f) ext.add(f.center_x_m, f.center_y_m, f.width_m, f.height_m);
  const t = map?.tip_xyz;
  if (t) ext.add(t.x_m, t.y_m, 0, 0);

  for (const m of map?.markers ?? []) {
    const planned = (m?.status ?? "") === "planned";
    if (planned) {
      if (!layers.plan) continue;
    } else if (isStaleEpoch(m, map?.current_epoch)) {
      if (!layers.stale) continue;
    } else if (!layers.history) {
      continue;
    }
    ext.add(m.x_m, m.y_m, m.w_m, m.h_m);
  }

  for (const s of map?.scan_images ?? []) {
    // The same rule the canvas draws by: an old-coordinate image hidden with its
    // layer must not keep holding the view open around empty canvas.
    if (scanImageOpacity(s, layers) == null) continue;
    ext.add(s.center_x_m, s.center_y_m, s.width_m, s.height_m);
  }

  if (analysis) {
    // Keep-out discs and the recommendation must be inside the fitted view, or
    // the overlay silently points off-canvas. NOT piezo_half_range_m: fitting
    // the whole ±1.5 µm range would collapse every real nanometre-scale marker
    // — that is what the explicit "Full piezo range" mode is for.
    if (layers.avoid) {
      for (const z of analysis.avoid_zones ?? []) {
        ext.add(z.x_m, z.y_m, z.radius_m * 2, z.radius_m * 2);
      }
    }
    const size = analysis.frame_size_m ?? 0;
    if (layers.next) {
      const np = analysis.next_position;
      if (np) ext.add(np.x_m, np.y_m, size, size);
    }
    if (layers.upcoming) {
      for (const p of analysis.upcoming ?? []) ext.add(p.x_m, p.y_m, size, size);
    }
  }

  return { bounds: ext.bounds(), fellBack: false };
}

// ── map chrome under wheel zoom ────────────────────────────────────────────
//
// The map's textual chrome — axis tick numbers, the "X nm" axis label, the nm
// scale bar and its caption — must not be laid out in STAGE coordinates pinned
// to the canvas edges (`y = CANVAS_H - 16`, `x = 4`, `x = PAD`). The wheel zoom
// is a transform on the Stage, so such coordinates would be scaled and
// translated with everything else: the moment the operator zooms in to read
// detail, every number that gives the scale would slide off the canvas.
//
// Chrome is viewport furniture, not surface geometry. These two helpers are
// what the component needs to keep it that way, and they are pure so the
// arithmetic is checked here rather than by eye at 25×.

/** "Nice" round nm step for the grid / scale bar given a visible span in nm. */
export function niceStepNm(spanNm: number): number {
  const raw = spanNm / 5;
  const pow = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-6))));
  const norm = raw / pow;
  const nice = norm >= 5 ? 5 : norm >= 2 ? 2 : 1;
  return nice * pow;
}

/**
 * Grid / scale-bar step for what is ACTUALLY visible at zoom `k`.
 *
 * At k=1 this is `niceStepNm(spanNm)`, unchanged. Zoomed in, the visible span
 * is `spanNm / k`, so the step shrinks with it — which is what keeps the scale
 * bar a constant fraction of the canvas instead of growing k× until it runs off
 * the edge, and what makes the grid subdivide as the operator zooms in rather
 * than staying at the whole-map spacing.
 */
export function visibleStepNm(spanNm: number, zoomK: number): number {
  const k = Number.isFinite(zoomK) && zoomK > 0 ? zoomK : 1;
  return niceStepNm(spanNm / k);
}

/**
 * Layer transform that CANCELS a stage transform, so a layer carrying it is
 * pinned to the viewport however the stage is zoomed and panned.
 *
 * Konva composes stage∘layer, so with stage `p ↦ k·p + t` this layer's
 * `p ↦ p/k − t/k` composes to the identity. Cheaper and far less error-prone
 * than counter-scaling every Text node and recomputing its anchor.
 */
export function inverseStageTransform(zoom: { k: number; x: number; y: number }): {
  scaleX: number;
  scaleY: number;
  x: number;
  y: number;
} {
  const k = Number.isFinite(zoom?.k) && zoom.k > 0 ? zoom.k : 1;
  const tx = Number.isFinite(zoom?.x) ? zoom.x : 0;
  const ty = Number.isFinite(zoom?.y) ? zoom.y : 0;
  // `+` normalises -0 → 0: harmless to Konva, but a -0 offset on an un-zoomed
  // map is a confusing thing to read back out of a debug dump.
  return { scaleX: 1 / k, scaleY: 1 / k, x: +(-tx / k) + 0, y: +(-ty / k) + 0 };
}

/**
 * Konva props that keep a Text anchored to a WORLD feature while rendering at a
 * constant SCREEN size under stage zoom `k`.
 *
 * Why this exists on top of {@link inverseStageTransform}: the map's GLOBAL
 * chrome — axis ticks, the scale bar — lives in an inverse-transformed layer,
 * which is right for furniture pinned to the canvas edges. But five labels
 * belong to a moving feature and therefore stay in the zoomed world layer: the
 * tip label, the suggested-next-position label, the piezo-range label, and the
 * plan / candidate step numbers. Those would scale with the stage, so at the 25×
 * the zoom allows a 10 px caption would render 250 px tall and bury the surface
 * it labels.
 *
 * The whole layer cannot simply be inverted here — that would unpin the labels
 * from the features they name. So the anchor stays in world space and only the
 * glyphs are counter-scaled:
 *
 *   • `dxWorld/dyWorld` — offsets that SHOULD grow with zoom (half a frame
 *     width: the label must clear the frame however big it is drawn);
 *   • `dxPx/dyPx` — the visual gap between feature and caption, which should
 *     look the same at 1× and 25×, so it is divided by `k`.
 *
 * Pure, so the arithmetic is checked here rather than by eye at 25×.
 */
export function pinnedLabel(
  anchor: { x: number; y: number },
  dxPx: number,
  dyPx: number,
  zoomK: number,
): { x: number; y: number; scaleX: number; scaleY: number } {
  const k = Number.isFinite(zoomK) && zoomK > 0 ? zoomK : 1;
  return {
    x: anchor.x + dxPx / k,
    y: anchor.y + dyPx / k,
    scaleX: 1 / k,
    scaleY: 1 / k,
  };
}

/** Where a stage-space point lands on screen under the stage transform. Used to
 *  anchor viewport-fixed chrome (tick labels) to moving world features. */
export function stageToScreen(
  p: { x: number; y: number },
  zoom: { k: number; x: number; y: number },
): { x: number; y: number } {
  const k = Number.isFinite(zoom?.k) && zoom.k > 0 ? zoom.k : 1;
  const tx = Number.isFinite(zoom?.x) ? zoom.x : 0;
  const ty = Number.isFinite(zoom?.y) ? zoom.y : 0;
  return { x: p.x * k + tx, y: p.y * k + ty };
}

/**
 * Format a nanometre value for an axis tick / scale caption, at a precision the
 * grid step actually justifies.
 *
 * The old code was `Math.round(nm)`. That was fine while the step was derived
 * from the whole-map span, but once it follows the zoom (visibleStepNm) a
 * zoomed-in map has sub-nanometre steps — and rounding then prints "0" for
 * every tick and "0 nm" on the scale bar. Decimals come from the step, so the
 * label is never more precise than the thing it is labelling.
 */
export function fmtNm(valueNm: number, stepNm: number): string {
  if (!Number.isFinite(valueNm)) return "";
  const step = Number.isFinite(stepNm) && stepNm > 0 ? stepNm : 1;
  // The epsilon keeps a step that is a power of ten up to float noise (0.1 can
  // come out of arithmetic as 0.09999999999999999) from asking for an extra
  // decimal place.
  const decimals = Math.min(6, Math.max(0, Math.ceil(-Math.log10(step) - 1e-9)));
  // `+` normalises -0 to 0 and drops trailing zeros ("1.50" → "1.5"). Axis
  // numbers take a real minus sign (U+2212): a hyphen is shorter than the digits
  // and reads as a dash at 9 px.
  const s = String(+(valueNm + 0).toFixed(decimals));
  return s.startsWith("-") ? "−" + s.slice(1) : s;
}

// ── marker detail (clicking a marker opens a popup with details and time) ────

/** The subset of `MapMarkerView` this needs. Structural, so tests need no API types. */
export interface MarkerLike {
  kind?: string | null;
  label?: string | null;
  skill_name?: string | null;
  status?: string | null;
  source?: string | null;
  timestamp?: string | null;
  coord_epoch?: number | null;
  x_m?: number | null;
  y_m?: number | null;
  w_m?: number | null;
  h_m?: number | null;
  angle_deg?: number | null;
}

export interface DetailRow {
  label: string;
  value: string;
  /** Render in the mono/tabular face — coordinates and sizes. */
  mono?: boolean;
}

function nm(v: number | null | undefined): string | null {
  if (v == null || !Number.isFinite(v)) return null;
  return `${(v * 1e9).toFixed(2)} nm`;
}

/**
 * Rows for the marker popup.
 *
 * Why a popup at all: the hover tooltip is one line capped at 200 px with
 * `wrap="none"`, so it can carry a label and a clock time and nothing else.
 * Everything the marker actually knows — which skill put it there, whether it
 * succeeded, how big the frame was, which coordinate generation it belongs to
 * — had no way to reach the screen.
 *
 * Omits what it does not have rather than printing a placeholder: a marker with
 * no size is a point operation (a pulse, a spectrum), not a zero-sized scan,
 * and a row reading "Size 0 × 0" would be false.
 */
export function markerDetailRows(
  m: MarkerLike,
  currentEpoch?: number | null,
): DetailRow[] {
  const rows: DetailRow[] = [];
  if (m.timestamp) {
    // In the browser's time zone, through the same formatter as an image's
    // "Modified" — two clocks in one popup must not disagree by a time zone.
    const t = parseStamp(m.timestamp);
    rows.push({ label: tr("时间", "Time"), value: t == null ? m.timestamp.replace("T", " ") : fmtDateTime(t), mono: true });
    // A browser zones away from the instrument also gets the PC's own wall
    // clock: that is what its file names and the lab notebook say. Both parts
    // come from the stamp itself — the wall clock as written and the offset of
    // ITS date — so a winter marker read in summer is not an hour off.
    const own = stampOffsetMin(m.timestamp);
    const wall = wallClockOf(m.timestamp);
    if (t != null && own != null && wall && browserOffsetMin(t) !== own) {
      rows.push({ label: tr("仪器时钟", "Instrument clock"), value: `${wall} (${fmtUtcOffset(own)})`, mono: true });
    }
  }
  if (m.skill_name) rows.push({ label: tr("技能", "Skill"), value: m.skill_name, mono: true });
  if (m.status) rows.push({ label: tr("状态", "Status"), value: statusLabelOf(m.status) });
  const x = nm(m.x_m);
  const y = nm(m.y_m);
  if (x && y) rows.push({ label: tr("位置", "Position"), value: `X ${x} · Y ${y}`, mono: true });
  const w = nm(m.w_m);
  const h = nm(m.h_m);
  if (w && h) rows.push({ label: tr("尺寸", "Size"), value: `${w} × ${h}`, mono: true });
  if (m.angle_deg != null && Number.isFinite(m.angle_deg) && m.angle_deg !== 0) {
    rows.push({ label: tr("角度", "Angle"), value: `${m.angle_deg.toFixed(1)}°`, mono: true });
  }
  if (m.coord_epoch != null) {
    // Stale epochs are the one thing here that changes what the operator should
    // DO: the stage has moved since, so these coordinates no longer point at
    // the place the operation happened. Say it in words, not just by fading.
    const stale = currentEpoch != null && m.coord_epoch !== currentEpoch;
    rows.push({
      label: tr("坐标代次", "Coordinate generation"),
      value: `#${m.coord_epoch}${stale ? tr("（粗动前的旧坐标系，位置已不再对应）", " (old coordinate system from before a coarse move; the position no longer matches)") : ""}`,
    });
  }
  if (m.source) rows.push({ label: tr("来源", "Source"), value: m.source });
  return rows;
}

function statusLabelOf(s: string): string {
  if (s === "planned") return tr("计划中", "Planned");
  if (s === "failed") return tr("失败", "Failed");
  if (s === "done") return tr("已完成", "Done");
  return s;
}

// ── what is actually drawn (layers × time window) ──────────────────────────
//
// One decision, three readers: the canvas draws these, the legend counts them,
// and the marker list lists them. Computing it once here is what keeps "STS 14"
// in the legend true of the map next to it.

/** The subset of `MapMarkerView` the history rules read. */
export interface HistoryMarkerLike {
  kind?: string | null;
  status?: string | null;
  coord_epoch?: number | null;
  timestamp?: string | null;
  x_m?: number | null;
  y_m?: number | null;
}

const finite = (v: number | null | undefined): v is number => v != null && Number.isFinite(v);

/**
 * Executed markers that the canvas draws, with their index in `map.markers`.
 *
 * Planned steps are not history (the route draws them, unaffected by the time
 * filter); a marker without a position cannot be drawn; the live/stale split
 * follows the two layer switches; and the time window drops what is outside it
 * — except an undated marker, which is always kept: hiding work because its
 * clock reading is missing would make it vanish for a reason nobody can see.
 */
export function drawnHistory<T extends HistoryMarkerLike>(
  markers: T[] | null | undefined,
  currentEpoch: number | null | undefined,
  layers: Pick<MapLayers, "history" | "stale">,
  window: TimeWindow = ALL_TIME,
): { m: T; index: number }[] {
  const out: { m: T; index: number }[] = [];
  (markers ?? []).forEach((m, index) => {
    if (!m || (m.status ?? "") === "planned") return;
    if (!finite(m.x_m) || !finite(m.y_m)) return;
    if (!(isStaleEpoch(m, currentEpoch) ? layers.stale : layers.history)) return;
    if (!inWindow(parseStamp(m.timestamp), window)) return;
    out.push({ m, index });
  });
  return out;
}

/** Saved-scan thumbnails the canvas draws, with opacity and original index. */
export function drawnScanImages<T extends ScanImageLike>(
  images: T[] | null | undefined,
  layers: Pick<MapLayers, "underlay" | "stale"> & Partial<Pick<MapLayers, "live">>,
  window: TimeWindow = ALL_TIME,
  /** The row-by-row live stream is drawing the scan in progress: its 1/8
   *  milestone image would be a second, staler copy of the same frame. */
  liveStreamShown = false,
): { img: T; index: number; opacity: number }[] {
  const out: { img: T; index: number; opacity: number }[] = [];
  (images ?? []).forEach((img, index) => {
    if (!img) return;
    // The milestone image belongs to the "live scan" layer, and steps aside
    // for the stream; it is the fallback when the stream is unavailable.
    if (img.live === true && (layers.live === false || liveStreamShown)) return;
    const opacity = scanImageOpacity(img, layers);
    if (opacity == null) return;
    // An image whose mtime could not be read is undated: kept, like a marker.
    // The scan in progress is not filtered at all, like the live frame it sits
    // in: its mtime is its newest milestone's, and a window closed before that
    // hid the very scan the operator was watching.
    if (img.live !== true && !inWindow(parseStamp(img.mtime), window)) return;
    out.push({ img, index, opacity });
  });
  return out;
}

/** How many markers of each kind (lower-cased). */
export function kindCounts(markers: { kind?: string | null }[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const m of markers) {
    const k = (m?.kind ?? "").toLowerCase();
    out[k] = (out[k] ?? 0) + 1;
  }
  return out;
}

/** "14", or "5/14" when the map shows fewer than were recorded. */
export function fmtCount(shown: number, total: number): string {
  return shown === total ? String(shown) : `${shown}/${total}`;
}

/**
 * A stable identity for each marker, the same across 3 s polls.
 *
 * `MapMarkerView` carries no row id, and the array index is not stable: the
 * backend returns the newest 2000 rows, so once a scope outgrows that window
 * every index shifts by one on each new operation. Built from what the row
 * says instead, with an occurrence suffix for exact duplicates.
 */
export function markerKeys(
  markers: { kind?: string | null; timestamp?: string | null; skill_name?: string | null;
    label?: string | null; x_m?: number | null; y_m?: number | null; status?: string | null }[],
): string[] {
  const seen = new Map<string, number>();
  return markers.map((m) => {
    const base = [m?.status ?? "", m?.timestamp ?? "", m?.kind ?? "", m?.skill_name ?? "",
      m?.label ?? "", m?.x_m ?? "", m?.y_m ?? ""].join("|");
    const n = seen.get(base) ?? 0;
    seen.set(base, n + 1);
    return `${base}#${n}`;
  });
}

/**
 * Rows for a saved-scan thumbnail's popup: which file it is, where and how big,
 * when it was written, and whether its coordinates still mean anything.
 */
export function scanImageDetailRows(img: ScanImageLike, nowMs?: number | null): DetailRow[] {
  const rows: DetailRow[] = [];
  if (img.name) rows.push({ label: tr("文件", "File"), value: img.name, mono: true });
  if (img.path) rows.push({ label: tr("路径", "Path"), value: img.path, mono: true });
  const cx = nm(img.center_x_m);
  const cy = nm(img.center_y_m);
  if (cx && cy) rows.push({ label: tr("中心", "Centre"), value: `X ${cx} · Y ${cy}`, mono: true });
  const w = nm(img.width_m);
  const h = nm(img.height_m);
  if (w && h) rows.push({ label: tr("尺寸", "Size"), value: `${w} × ${h}`, mono: true });
  if (img.angle_deg != null && Number.isFinite(img.angle_deg) && img.angle_deg !== 0) {
    rows.push({ label: tr("角度", "Angle"), value: `${img.angle_deg.toFixed(1)}°`, mono: true });
  }
  const t = parseStamp(img.mtime);
  if (t != null) {
    const ago = nowMs != null && Number.isFinite(nowMs) ? fmtAgo(t, nowMs) : null;
    rows.push({
      label: tr("修改时间", "Modified"),
      value: `${fmtDateTime(t)}${ago ? tr(`（${ago}）`, ` (${ago})`) : ""}`,
      mono: true,
    });
  } else {
    rows.push({
      label: tr("修改时间", "Modified"),
      value: tr("读不到（按时间筛选时照常显示）", "Unknown (always shown when filtering by time)"),
    });
  }
  rows.push(
    img.stale_epoch
      ? {
          label: tr("坐标", "Coordinates"),
          value: tr(
            "粗动之前写的文件：头里的坐标属于已经作废的坐标系，这张图现在画在它从没扫过的表面上，所以淡显。",
            "Written before the last coarse move: its header coordinates belong to a coordinate system that no longer applies, so it now sits over surface it never imaged. That is why it is faded.",
          ),
        }
      : { label: tr("坐标", "Coordinates"), value: tr("当前坐标系", "Current coordinate system") },
  );
  return rows;
}

// ── the marker list under the map ──────────────────────────────────────────

/**
 * Rows for the marker list: the markers the map draws, newest first, narrowed
 * by kind and by a free-text query over label, skill and kind name.
 *
 * "Newest" is record order (the index), not the timestamp — see
 * latestActionIndex in scanMapTime.ts for why.
 */
export function filterMarkerRows<T extends { index: number; m: { kind?: string | null; label?: string | null; skill_name?: string | null } }>(
  rows: T[],
  opts: { kind?: string; query?: string },
  kindName: (kind?: string | null) => string = () => "",
): T[] {
  const kind = (opts.kind ?? "").trim().toLowerCase();
  const q = (opts.query ?? "").trim().toLowerCase();
  return rows
    .filter((r) => !kind || (r.m.kind ?? "").toLowerCase() === kind)
    .filter((r) => {
      if (!q) return true;
      const hay = [r.m.label, r.m.skill_name, r.m.kind, kindName(r.m.kind)]
        .map((v) => (v ?? "").toLowerCase())
        .join("\n");
      return hay.includes(q);
    })
    .sort((a, b) => b.index - a.index);
}

/**
 * The kinds the list's filter offers: those its rows have, plus the one
 * selected even when no row has it any more. Built from the
 * rows alone, a kind that left them — the time window moved — vanished from
 * the options while it still filtered: the select fell back to showing "All
 * kinds" above a list narrowed to nothing. Kept, the select says what is
 * filtering, and the choice is still there when the window widens again.
 */
export function markerKindOptions(rows: { m: { kind?: string | null } }[], selected: string): string[] {
  const kinds = new Set(rows.map((r) => (r.m.kind ?? "").toLowerCase()).filter(Boolean));
  const sel = selected.trim().toLowerCase();
  if (sel) kinds.add(sel);
  return [...kinds].sort();
}

// ── exporting the view ─────────────────────────────────────────────────────

/**
 * "scanmap_Au(111)_#3_20261112-154210.png": the sample and the moment, in a
 * name every OS accepts. Characters Windows forbids in file names, control
 * characters and whitespace become "_"; an empty or all-forbidden sample name
 * falls back to "map".
 */
export function exportFileName(sample: string | null | undefined, at: Date): string {
  const clean = (sample ?? "")
    // eslint-disable-next-line no-control-regex
    .replace(/[<>:"/\\|?*\u0000-\u001f\s]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^[_.]+|[_.]+$/g, "")
    .slice(0, 60);
  const p = (n: number) => String(n).padStart(2, "0");
  const stamp =
    `${at.getFullYear()}${p(at.getMonth() + 1)}${p(at.getDate())}-` +
    `${p(at.getHours())}${p(at.getMinutes())}${p(at.getSeconds())}`;
  return `scanmap_${clean || "map"}_${stamp}.png`;
}
