import { useEffect, useMemo, useRef, useState } from "react";
import { Stage, Layer, Rect, Circle, Line, Text, Group, Label, Tag, Image as KonvaImage } from "react-konva";
import Konva from "konva";
import type { components } from "@/api/schema";
import { useUiStore } from "@/store";
import { useElementWidth, useViewportHeight } from "@/hooks/useElementWidth";
import { MAP_RATIO, sizeChart } from "@/lib/chartSize";
import {
  DEFAULT_LAYERS,
  STALE_EPOCH_OPACITY,
  boundsForMode,
  drawnHistory,
  drawnScanImages,
  exportFileName,
  fmtCount,
  fmtNm,
  inverseStageTransform,
  isStaleEpoch,
  kindCounts,
  markerDetailRows,
  pinnedLabel,
  scanImageDetailRows,
  stageToScreen,
  visibleStepNm,
  type DetailRow,
  type MapLayers,
  type MapViewMode,
} from "@/lib/scanMapView";
import {
  clampZoom,
  coordDecimals,
  dotOutline,
  fitView,
  fmtMeasurement,
  fmtSignedNm,
  footprintsAt,
  gridTicks,
  layoutBadges,
  stageToWorld,
  textWidthPx,
  visibleWorld,
  withAlpha,
  worldToStage,
  zoomToward,
  type Pt,
} from "@/lib/scanMapGeometry";
import { composeMapPng, downloadCanvas, type LegendEntry } from "@/components/vision/scanMapExport";
import { LiveScanImage, LiveScanLine, type LiveScanState } from "@/components/vision/LiveScanLayer";
import { ALL_TIME, fmtDateTime, latestActionIndex, parseStamp, type TimeWindow } from "@/lib/scanMapTime";
import { ClickArbiter } from "@/lib/scanMapClicks";
import { Modal } from "@/components/controls";
import { CopyButton } from "@/components/gallery/clipboard";
import { getLang, tr } from "@/lib/i18n";

type ScanMap = components["schemas"]["ScanMapResponse"];
type Marker = components["schemas"]["MapMarkerView"];
type ScanImg = components["schemas"]["ScanImage"];
type MapAnalysis = components["schemas"]["ScanMapAnalysisResponse"];

// Native Konva render of the live experiment map. Coordinates are the Nanonis
// stage frame, in METRES; we auto-fit every spatial element (frame + tip +
// markers + plan) into the canvas with a metres→pixels affine and flip Y so +y
// points up (lab convention).
//   • rotated footprints pivot about their CENTRE, not the top-left corner
//   • marker colours match the backend KIND_STYLE exactly
//   • hover tooltip (label · kind · time), an nm grid, axis ticks and a scale
//     bar so the map reads as a scientific surface map
//   • a cursor readout, a ruler, clickable thumbnails and a time window
//   • honest empty / degraded states
//
// Sizes on screen do not change with the wheel zoom: strokes use
// strokeScaleEnabled={false} and radii / offsets are divided by the zoom, so a
// dot is a dot at 1× and at 25×, and only the SURFACE (footprints, thumbnails,
// keep-out radii) grows.

// The canvas is sized from the width its container actually got, so a wide monitor
// uses the available space; 620 is the fallback for the first frame, before the
// ResizeObserver has reported.
const FALLBACK_W = 620;
const PAD = 44;

// kind → colour, mirroring mast/io/exp_map.py KIND_STYLE so the Konva map and
// the backend matplotlib legend never disagree.
const KIND_COLOR = {
  scan: "#3b82f6",
  sts: "#22c55e",
  pulse: "#f59e0b",
  tip_shape: "#ef4444",
  move: "#94a3b8",
  manual: "#e879f9",
  plan: "#38bdf8",
  frame: "#06b6d4",
  tip: "#f43f5e",
  coarse_move: "#8b5cf6",
  approach: "#14b8a6",
  crash: "#991b1b",
  manual_avoid: "#e879f9",
};
const KIND_LABEL = {
  scan: tr("扫图", "Scan"),
  sts: tr("STS 点谱", "STS spectrum"),
  pulse: tr("电脉冲", "Pulse"),
  tip_shape: tr("修针尖", "Tip shaping"),
  move: tr("移动", "Move"),
  manual: tr("手动操作", "Manual action"),
  plan: tr("计划", "Plan"),
  frame: tr("扫描框", "Scan frame"),
  tip: tr("针尖", "Tip"),
  coarse_move: tr("粗动换区", "Coarse move"),
  approach: tr("进针", "Approach"),
  crash: tr("撞针", "Tip crash"),
  manual_avoid: tr("人工避让区", "Manual avoid zone"),
};
// The stale-generation fade (STALE_EPOCH_OPACITY) lives in lib/scanMapView.ts:
// markers and saved-scan thumbnails must fade by the SAME factor, and the
// thumbnail rule (scanImageOpacity) is decided there, where it is tested.
// Chrome colours — grid, ticks, scale bar, tooltip. Konva paints onto a canvas
// and cannot read a CSS variable, so the palette has to be resolved in JS;
// hard-coding the dark one would leave the map a black slab in light mode.
//
// The MARKER colours above are deliberately NOT themed: they mirror
// mast/io/exp_map.py KIND_STYLE so the Konva map and the backend matplotlib
// legend never disagree, and "scan is blue" must mean the same thing in a
// screenshot regardless of who took it.
//
// `ink` is the colour of things the operator put on the map or must find at a
// glance (the ruler, the latest-action ring): the highest-contrast colour the
// theme has, and no marker kind's colour. `planInk` is the plan hue darkened
// for strokes and numbers on a light background, where #38bdf8 is 2:1.
const CHROME = {
  dark: {
    bg: "#0b1017", grid: "#1e293b", tick: "#64748b", axis: "#94a3b8",
    bar: "#e2e8f0", tipRing: "#ffffff",
    tooltipBg: "#111827", tooltipBorder: "#334155", tooltipText: "#e2e8f0",
    ink: "#f8fafc", chipBg: "rgba(11, 16, 23, 0.86)", accent: "#28d0e6",
    planInk: "#38bdf8", badgeRing: "#0b1017",
  },
  light: {
    bg: "#f8fafc", grid: "#dde3ec", tick: "#64748b", axis: "#475569",
    bar: "#1e293b", tipRing: "#ffffff",
    tooltipBg: "#ffffff", tooltipBorder: "#cbd5e1", tooltipText: "#0f172a",
    ink: "#0f172a", chipBg: "rgba(248, 250, 252, 0.92)", accent: "#0b6f83",
    planInk: "#0369a1", badgeRing: "#0369a1",
  },
} as const;
type Chrome = (typeof CHROME)["dark"] | (typeof CHROME)["light"];

/** Plan badge fill, both themes: the plan hue, with dark numerals (8:1). */
const BADGE_FILL = "#38bdf8";
const BADGE_TEXT = "#04222a";
const BADGE_R = 7;
/** Konva node name of a clickable history marker (it opens its own popup). */
const MARKER_NODE = "scanmap-marker";
/** Konva node name of chrome that only describes the pointer (left out of exports). */
const TRANSIENT_NODE = "scanmap-transient";

function kindColor(kind?: string | null): string {
  return (KIND_COLOR as Record<string, string>)[(kind ?? "").toLowerCase()] ?? "#94a3b8";
}
function kindLabel(kind?: string | null): string {
  return (KIND_LABEL as Record<string, string>)[kind ?? ""] ?? "";
}

/** Kind → colour, for components that list markers beside the map (the
 *  marker list). The table stays here, next to its cross-language twin's
 *  parity test; this only lends it out. */
export function scanMapKindColor(kind?: string | null): string {
  return kindColor(kind);
}
/** Kind → display name ("" for an unknown kind). */
export function scanMapKindLabel(kind?: string | null): string {
  return kindLabel((kind ?? "").toLowerCase());
}

/** Legend / export order of the marker kinds. */
const KIND_ORDER = [
  "scan", "sts", "pulse", "tip_shape", "approach", "crash", "coarse_move", "move", "manual",
] as const;

/** Konva's double-click window as Konva itself reads it at runtime (it is not
 *  in its typings; 400 ms by default). */
function konvaDblClickMs(): number {
  const v = (Konva as unknown as { dblClickWindow?: unknown }).dblClickWindow;
  return typeof v === "number" && v > 0 ? v : 400;
}

/** "13:41:30", or a word saying there is no usable time. */
function clockOf(ts?: string | null): string {
  const t = parseStamp(ts);
  return t == null ? tr("无时间", "no time") : fmtDateTime(t).slice(11);
}

// ── the cursor, without re-rendering the map on every mouse move ───────────
//
// The readout chip and the ruler's rubber band follow the cursor; the hundreds
// of shapes on the map do not. A tiny store lets those two subscribe on their
// own, so a mouse move re-renders a chip and one Konva layer, not the canvas.

interface CursorStore {
  get: () => Pt | null;
  set: (p: Pt | null) => void;
  subscribe: (fn: () => void) => () => void;
}

function createCursorStore(): CursorStore {
  let cur: Pt | null = null;
  const subs = new Set<() => void>();
  return {
    get: () => cur,
    set: (p) => {
      if (p === cur || (p && cur && p.x === cur.x && p.y === cur.y)) return;
      cur = p;
      subs.forEach((f) => f());
    },
    subscribe: (fn) => {
      subs.add(fn);
      return () => {
        subs.delete(fn);
      };
    },
  };
}

function useCursor(store: CursorStore, enabled = true): Pt | null {
  const [p, setP] = useState<Pt | null>(() => (enabled ? store.get() : null));
  useEffect(() => {
    if (!enabled) {
      setP(null);
      return;
    }
    setP(store.get());
    return store.subscribe(() => setP(store.get()));
  }, [store, enabled]);
  return p;
}

interface MeasurePts {
  a: Pt | null;
  b: Pt | null;
}

export interface FocusRequest {
  /** markerKeys() key of the marker to bring into view */
  key: string;
  /** bump to re-focus on the same marker */
  seq: number;
}

// Which elements are in view — and which are drawn at all — now lives in
// lib/scanMapView.ts (boundsForMode, drawnHistory, drawnScanImages), where it
// is covered by npm run test:unit. So do the grid step and the chrome
// transform (visibleStepNm / inverseStageTransform / stageToScreen), and every
// conversion between screen, stage and metres (lib/scanMapGeometry.ts).

export function ScanMapCanvas({
  map,
  analysis,
  layers = DEFAULT_LAYERS,
  viewMode = "fit",
  timeWindow = ALL_TIME,
  highlightKey = null,
  markerKeyList,
  focusRequest = null,
  exportName = "",
  exportCaption = "",
  live = null,
}: {
  map: ScanMap;
  analysis?: MapAnalysis | null;
  layers?: MapLayers;
  viewMode?: MapViewMode;
  /** Only history markers and saved images inside this window are drawn. The
   *  route, the live frame, the tip and the analysis overlays are not filtered. */
  timeWindow?: TimeWindow;
  /** Ring this marker (a markerKeys() key) — the list below the map hovers it. */
  highlightKey?: string | null;
  /** markerKeys(map.markers), when the caller already has them. */
  markerKeyList?: string[];
  /** Bring a marker into view and open its details — the list's row click.
   *  Acted on once per `seq`. Needs `markerKeyList`. */
  focusRequest?: FocusRequest | null;
  /** Sample name for the exported PNG's file name and caption. */
  exportName?: string;
  /** Extra caption for the exported PNG (e.g. the time window). */
  exportCaption?: string;
  /** The row-by-row live scan (useLiveScan); absent = only the 1/8 milestone image. */
  live?: LiveScanState | null;
}) {
  const c: Chrome = CHROME[useUiStore((st) => st.theme) === "light" ? "light" : "dark"];
  const [hover, setHover] = useState<{ x: number; y: number; text: string } | null>(null);
  // The underlay under the cursor, topmost first. Thumbnails are not Konva hit
  // targets (the scan footprint drawn over each one would always win), so they
  // are hit-tested here from the pointer position instead.
  const [hoverImgs, setHoverImgs] = useState<{ key: string; imgs: ScanImg[]; at: Pt } | null>(null);
  // Click a marker or a thumbnail for its full details. The hover tooltip is
  // one or two short lines, so everything else needs somewhere to go.
  const [detail, setDetail] = useState<{ marker: Marker | null; images: ScanImg[] } | null>(null);
  const [measureOn, setMeasureOn] = useState(false);
  const [pts, setPts] = useState<MeasurePts>({ a: null, b: null });
  const cursor = useMemo(createCursorStore, []);
  // Single click vs double click vs a tap and its echo (lib/scanMapClicks.ts):
  // a thumbnail's popup waits out Konva's own double-click window.
  const clicks = useMemo(() => new ClickArbiter(konvaDblClickMs()), []);
  useEffect(() => () => clicks.cancel(), [clicks]);
  const stageRef = useRef<Konva.Stage | null>(null);
  const [boxRef, measuredW] = useElementWidth<HTMLDivElement>();
  const viewportH = useViewportHeight();
  // Konva's Stage needs concrete pixels — `width: 100%` does nothing to a canvas.
  const { width: CANVAS_W, height: CANVAS_H } = sizeChart(measuredW, FALLBACK_W, {
    ratio: MAP_RATIO,
    minHeight: 320,
    maxHeight: 900,
    viewportH,
  });
  // Wheel zoom + drag pan.
  // Zoom is about the cursor; drag pans when zoomed; double-click resets.
  const [zoom, setZoom] = useState<{ k: number; x: number; y: number }>({ k: 1, x: 0, y: 0 });
  // Zoom is a screen-space transform anchored to the extent it was applied to.
  // Keeping it across a view change would drag the operator to an unpredictable
  // corner of the new one — the same reason double-click resets.
  useEffect(() => {
    setZoom({ k: 1, x: 0, y: 0 });
  }, [viewMode]);

  // Esc leaves the ruler (and clears it), as does switching it off.
  useEffect(() => {
    if (!measureOn) return;
    const h = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setMeasureOn(false);
        setPts({ a: null, b: null });
      }
    };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, [measureOn]);

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  function onWheel(e: any) {
    e.evt.preventDefault();
    const stage = e.target.getStage();
    const pointer = stage?.getPointerPosition();
    if (!pointer) return;
    const factor = e.evt.deltaY < 0 ? 1.15 : 1 / 1.15;
    setZoom((z) => {
      const k = clampZoom(z.k * factor);
      if (k === z.k) return z;
      if (k === 1) return { k: 1, x: 0, y: 0 };
      // keep the world point under the cursor fixed while scaling
      const wx = (pointer.x - z.x) / z.k;
      const wy = (pointer.y - z.y) / z.k;
      return { k, x: pointer.x - wx * k, y: pointer.y - wy * k };
    });
  }

  const fit = useMemo(
    () => boundsForMode(viewMode, map, analysis, layers, map.piezo_half_range_m),
    [viewMode, map, analysis, layers],
  );
  const view = useMemo(
    () => (fit.bounds ? fitView(fit.bounds, CANVAS_W, CANVAS_H, PAD) : null),
    [fit, CANVAS_W, CANVAS_H],
  );

  const allMarkers = map.markers ?? [];
  const history = useMemo(
    () => drawnHistory(allMarkers, map.current_epoch, layers, timeWindow),
    [allMarkers, map.current_epoch, layers, timeWindow],
  );
  // The stream is drawing this frame (live, or just ended and waiting for its
  // .sxm): the milestone image of the same frame would be a staler second copy.
  const liveShown = !!live?.show && layers.live;
  const images = useMemo(
    () => drawnScanImages(map.scan_images ?? [], layers, timeWindow, liveShown),
    [map.scan_images, layers, timeWindow, liveShown],
  );
  const imageList = useMemo(() => images.map((d) => d.img), [images]);
  const latestIndex = useMemo(() => latestActionIndex(allMarkers), [allMarkers]);

  // A row clicked in the marker list: centre the marker, zoom to it, open its
  // details. Keyed on `seq` alone on purpose — the map re-polls every 3 s, and
  // re-running on every new `map` would yank the view back after the operator
  // has panned away. And never on mount: the request outlives this canvas (it
  // lives in the panel), and a canvas mounted again — after a degraded poll —
  // replayed it, popping the popup open by itself and panning by the fallback
  // width of the first frame, which threw the marker off-screen.
  const focusSeen = useRef<number | null>(focusRequest?.seq ?? null);
  useEffect(() => {
    if (!focusRequest || focusRequest.seq === focusSeen.current) return;
    focusSeen.current = focusRequest.seq;
    if (!view || !markerKeyList) return;
    const i = markerKeyList.indexOf(focusRequest.key);
    const m = i >= 0 ? allMarkers[i] : undefined;
    if (!m || m.x_m == null || m.y_m == null) return;
    const p = worldToStage(view, m.x_m, m.y_m);
    const size = hasFootprint(m) ? Math.max(Math.abs(m.w_m!), Math.abs(m.h_m!)) * view.scale : 0;
    setZoom(zoomToward(p, size, CANVAS_W, CANVAS_H));
    setDetail({ marker: m, images: footprintsAt(imageList, m.x_m, m.y_m) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusRequest?.seq]);

  // The cursor is "the thing under the pointer" in the cursor and click logic;
  // keep the pointer → metres conversion in one place.
  function pointerWorld(): Pt | null {
    const st = stageRef.current;
    if (!st || !view) return null;
    // The stage's LIVE transform: during a drag Konva moves the stage before
    // onDragEnd updates React state, so `zoom` would be one drag behind.
    const rel = st.getRelativePointerPosition();
    if (!rel) return null;
    return stageToWorld(view, rel.x, rel.y);
  }

  if (!view) {
    return (
      <div
        ref={boxRef}
        className="flex w-full items-center justify-center rounded-lg border border-mast-border text-center text-sm text-mast-muted"
        style={{ height: CANVAS_H, background: c.bg }}
      >
        {tr("等待扫描范围…", "Waiting for the scan range…")}
        <br />
        {tr(
          "开始扫描或读取仪器状态后，这里会显示扫描框、针尖与所有带位置的操作。",
          "Once a scan starts or the instrument state is read, the scan frame, tip and all positioned actions appear here.",
        )}
      </div>
    );
  }
  const { minX, maxX, scale } = view;
  const toPx = (xm: number, ym: number): Pt => worldToStage(view, xm, ym);
  const k = zoom.k;

  const f = map.frame;
  const t = map.tip_xyz;
  // Planned steps ride in `markers` in EXECUTION ORDER (PlanOverlay.snapshot is
  // next-first and the API preserves it), which is where the step numbers and
  // the "next" highlight come from — there is no separate ordering field.
  const planned = allMarkers.filter(
    (m) => (m.status ?? "") === "planned" && m.x_m != null && m.y_m != null,
  );
  const latestDrawn = history.find((d) => d.index === latestIndex) ?? null;
  const highlighted =
    highlightKey && markerKeyList
      ? (history.find((d) => markerKeyList[d.index] === highlightKey) ?? null)
      : null;

  // Grid lines on nice nm boundaries. The step follows the VISIBLE span, so
  // zooming in subdivides the grid instead of leaving it at whole-map spacing
  // — and keeps the scale bar a constant fraction of the canvas. Only the
  // visible stretch is generated: the step shrinks with zoom, and the old loop
  // over the whole fitted extent built hundreds of off-canvas lines at 25×.
  const spanNm = (maxX - minX) * 1e9;
  const stepNm = visibleStepNm(spanNm, k);
  const vis = visibleWorld(view, zoom, CANVAS_W, CANVAS_H);
  const gridXs = gridTicks(vis.minX * 1e9 - stepNm, vis.maxX * 1e9 + stepNm, stepNm);
  const gridYs = gridTicks(vis.minY * 1e9 - stepNm, vis.maxY * 1e9 + stepNm, stepNm);

  // Chrome lives in an un-zoomed layer: the numbers that tell the operator the
  // scale must stay on the canvas at any zoom (see inverseStageTransform).
  const chrome = inverseStageTransform(zoom);
  const scaleBarPx = stepNm * 1e-9 * scale * k;
  // Grid lines span the VISIBLE window inset by PAD/2 — identical to the old
  // fixed [PAD/2, CANVAS_H-PAD/2] box at 1×, but panning no longer walks off
  // the end of them.
  const gridTop = (PAD / 2 - zoom.y) / k;
  const gridBottom = (CANVAS_H - PAD / 2 - zoom.y) / k;
  const gridLeft = (PAD / 2 - zoom.x) / k;
  const gridRight = (CANVAS_W - PAD / 2 - zoom.x) / k;

  // Axis numbers that actually land on the canvas, with the unit on the last
  // one of each axis. The top band (toolbar) and bottom band (x numbers) are
  // kept clear of y numbers.
  const xTicks = gridXs
    .map((gx) => ({ v: gx, s: stageToScreen(toPx(gx * 1e-9, 0), zoom).x }))
    .filter((tk) => tk.s >= PAD / 2 && tk.s <= CANVAS_W - PAD / 2);
  const yTicks = gridYs
    .map((gy) => ({ v: gy, s: stageToScreen(toPx(0, gy * 1e-9), zoom).y }))
    .filter((tk) => tk.s >= 40 && tk.s <= CANVAS_H - 36);
  const topY = yTicks.reduce<number | null>((best, tk) => (best == null || tk.s < best ? tk.s : best), null);

  // Nanometres per SCREEN pixel — the precision of anything placed by the cursor.
  const nmPerPx = 1e9 / (scale * k);
  const decimals = coordDecimals(nmPerPx);

  // Numbered badges for the plan, then the candidate sequence, laid out
  // together in screen space so no number sits on another.
  const showUpcoming = layers.upcoming ? (analysis?.upcoming ?? []).filter((p) => p.x_m != null && p.y_m != null) : [];
  const skipFirstUpcoming = layers.next && analysis?.next_position?.x_m != null;
  const upSide = Math.max((analysis?.frame_size_m ?? 0) * scale, 6 / k);
  const planAnchors: Pt[] = layers.plan
    ? planned.map((m) => {
        const p = toPx(m.x_m!, m.y_m!);
        const w = Math.abs(m.w_m ?? 0) * scale;
        const h = Math.abs(m.h_m ?? 0) * scale;
        return w > 0 && h > 0 ? { x: p.x + w / 2, y: p.y - h / 2 } : { x: p.x + 9 / k, y: p.y - 9 / k };
      })
    : [];
  const upIdx = showUpcoming.map((_, i) => i).filter((i) => !(i === 0 && skipFirstUpcoming));
  const upAnchors: Pt[] = upIdx.map((i) => {
    const q = toPx(showUpcoming[i]!.x_m!, showUpcoming[i]!.y_m!);
    return { x: q.x - upSide / 2, y: q.y - upSide / 2 };
  });
  const placed = layoutBadges(
    [...planAnchors, ...upAnchors].map((a) => ({ x: a.x * k, y: a.y * k })),
    BADGE_R,
    2,
  ).map((p) => ({ x: p.x / k, y: p.y / k }));
  const planBadges = placed.slice(0, planAnchors.length);
  const upBadges = placed.slice(planAnchors.length);

  // What the pointer is over: a marker (Konva hit) and/or the underlay (ours).
  const tipLines: string[] = [];
  if (hover) tipLines.push(hover.text);
  if (hoverImgs && hoverImgs.imgs.length > 0) {
    const top = hoverImgs.imgs[0]!;
    tipLines.push(
      `${tr("扫描图", "Image")} ${top.name || "?"}` +
        (top.stale_epoch ? tr(" · 粗动前", " · pre-move") : "") +
        (hoverImgs.imgs.length > 1 ? ` +${hoverImgs.imgs.length - 1}` : ""),
    );
  }
  const tipAnchor = hover ? { x: hover.x, y: hover.y } : hoverImgs ? hoverImgs.at : null;

  const cursorStyle = measureOn ? "crosshair" : hover || hoverImgs ? "pointer" : k > 1 ? "grab" : "default";

  /** The saved images under the pointer, topmost first. */
  function underlayAtPointer(): ScanImg[] {
    const w = pointerWorld();
    return w ? footprintsAt(imageList, w.x, w.y) : [];
  }

  function openDetail(marker: Marker) {
    if (measureOn) return;
    setDetail({ marker, images: underlayAtPointer() });
  }

  function onStageMove() {
    const w = pointerWorld();
    cursor.set(w);
    if (!w) return;
    const under = footprintsAt(imageList, w.x, w.y);
    const key = under.map((im) => im.path || im.name).join("|");
    const st = stageRef.current;
    const rel = st?.getRelativePointerPosition();
    setHoverImgs((prev) => {
      if (!under.length) return prev ? null : prev;
      if (prev && prev.key === key) return prev;
      return { key, imgs: under, at: rel ? { x: rel.x, y: rel.y } : toPx(w.x, w.y) };
    });
  }

  function onStageClick(e: Konva.KonvaEventObject<MouseEvent | TouchEvent>) {
    // Konva reports every button as a click; only the primary one places a
    // ruler end or opens a popup.
    if ("button" in e.evt && e.evt.button !== 0) return;
    // A tap is followed by the browser's own click: one input, one action —
    // or the ruler drops A and B on the same spot.
    if (!clicks.accept(e.type === "tap" ? "tap" : "click")) return;
    if (measureOn) {
      const w = pointerWorld();
      if (!w) return;
      // A, then B; a third click starts a new measurement.
      setPts((p) => (p.a && !p.b ? { a: p.a, b: w } : { a: w, b: null }));
      return;
    }
    // A marker opened its own popup (and named itself MARKER_NODE); any other
    // click — empty map, a plan frame, the tip — is a click on the underlay.
    if (e.target.hasName?.(MARKER_NODE)) return;
    const under = underlayAtPointer();
    if (under.length === 0) return;
    // Not at once: the popup's backdrop would take the second click of a
    // double click, and a zoomed-in operator could no longer double-click a
    // thumbnail to reset the view. A double click cancels it.
    clicks.later(() => setDetail({ marker: null, images: under }));
  }

  function toggleMeasure() {
    // A thumbnail clicked just before must not open its popup over the ruler
    // the operator has just switched to.
    clicks.cancel();
    setMeasureOn((on) => !on);
    setPts({ a: null, b: null });
  }

  /** The view as drawn — scale bar, axes, ruler and all — plus the caption
   *  and the legend, which otherwise live in HTML outside the picture. */
  function exportPng() {
    const st = stageRef.current;
    if (!st) return;
    const ratio = 2;
    // Hide what only describes the pointer (the hover tooltip) for the capture.
    // Clearing React state would not do: the export runs inside the click, and
    // the mouse-leave that clears the tooltip may not have re-rendered yet — a
    // headless export did come out with "Plan 3/7" printed on it.
    const transient = st.find(`.${TRANSIENT_NODE}`);
    transient.forEach((n) => {
      n.hide();
    });
    let src: HTMLCanvasElement;
    try {
      src = st.toCanvas({ pixelRatio: ratio });
    } finally {
      transient.forEach((n) => {
        n.show();
      });
    }
    const counts = kindCounts(history.map((d) => d.m));
    const entries: LegendEntry[] = [];
    if (f && f.center_x_m != null && f.width_m != null) entries.push({ label: kindLabel("frame"), color: KIND_COLOR.frame });
    if (t && t.x_m != null && t.y_m != null) entries.push({ label: kindLabel("tip"), color: KIND_COLOR.tip });
    const order: string[] = [...KIND_ORDER, ...Object.keys(counts).filter((kd) => !(KIND_ORDER as readonly string[]).includes(kd))];
    for (const kd of order) {
      if (counts[kd]) entries.push({ label: kindLabel(kd) || kd, color: kindColor(kd), count: String(counts[kd]) });
    }
    if (layers.plan && planned.length > 0) {
      entries.push({ label: kindLabel("plan"), color: KIND_COLOR.plan, count: String(planned.length) });
    }
    if (latestDrawn) entries.push({ label: tr("最新操作", "Latest action"), color: c.ink, ring: true });
    const now = Date.now();
    const caption = [
      exportName || tr("扫描地图", "Scan map"),
      exportCaption,
      tr(`导出于 ${fmtDateTime(now)}`, `exported ${fmtDateTime(now)}`),
    ]
      .filter(Boolean)
      .join("  ·  ");
    const font = getComputedStyle(document.body).fontFamily || "sans-serif";
    const png = composeMapPng(src, { ratio, bg: c.bg, ink: c.ink, muted: c.tick, entries, caption, font });
    downloadCanvas(png, exportFileName(exportName, new Date(now)));
  }

  const detailTitle = detail
    ? detail.marker
      ? detail.marker.label || kindLabel(detail.marker.kind) || detail.marker.kind || tr("地图标记", "Map marker")
      : detail.images[0]?.name || tr("扫描图", "Scan image")
    : "";

  return (
    // `w-full` (not `inline-block`) so the box width comes from the layout and
    // never from the canvas inside it — measuring a shrink-to-fit box that
    // contains the thing being sized is a feedback loop.
    <div
      ref={boxRef}
      data-scanmap="canvas"
      className="relative w-full overflow-x-auto rounded-lg border border-mast-border"
      style={{ background: c.bg, cursor: cursorStyle }}
    >
      <Stage
        ref={stageRef}
        width={CANVAS_W}
        height={CANVAS_H}
        scaleX={k}
        scaleY={k}
        x={zoom.x}
        y={zoom.y}
        // Panning is allowed once the view has moved at all: a focus from the
        // marker list can centre a large footprint at 1×, translated.
        draggable={k > 1 || zoom.x !== 0 || zoom.y !== 0}
        onWheel={onWheel}
        onDblClick={() => {
          clicks.cancel();
          if (!measureOn) setZoom({ k: 1, x: 0, y: 0 });
        }}
        // A pan that starts while a thumbnail's popup is still waiting means
        // the click was not "open this": the popup would land mid-drag and
        // its backdrop eat the rest of it.
        onDragStart={() => clicks.cancel()}
        onDragEnd={(e) => {
          const st = e.target.getStage();
          if (st && e.target === st) setZoom((z) => ({ ...z, x: st.x(), y: st.y() }));
        }}
        onMouseMove={onStageMove}
        onClick={onStageClick}
        onTap={onStageClick}
        onMouseLeave={() => {
          cursor.set(null);
          setHover(null);
          setHoverImgs(null);
        }}
      >
        {/* Grid LINES are surface geometry — they belong to the zoomed world.
            Their extent is the visible stage window, not the canvas box, or
            panning walks off the end of them. */}
        <Layer listening={false} visible={layers.grid}>
          {gridXs.map((gx, i) => {
            const px = toPx(gx * 1e-9, 0).x;
            return <Line key={`gx${i}`} points={[px, gridTop, px, gridBottom]} stroke={c.grid} strokeWidth={0.6} strokeScaleEnabled={false} />;
          })}
          {gridYs.map((gy, i) => {
            const py = toPx(0, gy * 1e-9).y;
            return <Line key={`gy${i}`} points={[gridLeft, py, gridRight, py]} stroke={c.grid} strokeWidth={0.6} strokeScaleEnabled={false} />;
          })}
        </Layer>

        <Layer>
          {/* the reachable piezo area — only worth drawing when the view is the
              whole range, where it is the one thing giving the dots a scale */}
          {viewMode === "full" && map.piezo_half_range_m != null && (
            <PiezoRangeShape half={map.piezo_half_range_m} toPx={toPx} stroke={c.tick} zoomK={k} />
          )}

          {/* saved-scan surface mosaic — real .sxm thumbnails placed by footprint.
              One written before the last coarse move shows surface from a dead
              coordinate frame: faded like a stale marker, hidden with that layer.
              Outside the time window: not drawn. */}
          {images.map(({ img, index, opacity }) => (
            <ScanImageShape key={`si-${index}`} img={img} toPx={toPx} scale={scale} opacity={opacity} />
          ))}

          {/* the scan in progress, row by row, over the saved images it may
              overlap (it is newer than all of them) */}
          {layers.live && live?.show && live.frameId && live.meta && (
            <LiveScanImage frameId={live.frameId} meta={live.meta} toPx={toPx} scale={scale} />
          )}

          {/* keep-out discs: where the surface is damaged or contaminated.
              A dashed rim and a faint wash, so the surface under them stays
              readable. Under the markers so the events that caused them do too. */}
          {layers.avoid &&
            (analysis?.avoid_zones ?? []).map((z, i) => (
              <AvoidZoneShape key={`az-${i}`} zone={z} toPx={toPx} scale={scale} />
            ))}

          {/* the route the strategy would walk next — ghost frames */}
          {layers.upcoming && (
            <UpcomingRoute
              positions={showUpcoming}
              side={upSide}
              /* the first upcoming position IS next_position; drawing both
                 would double-strike one frame with two different labels */
              skipFirst={!!skipFirstUpcoming}
              toPx={toPx}
              color={c.planInk}
            />
          )}

          {/* planned route: dashed polyline through plan markers, in order */}
          {layers.plan && (
            <PlanRoute
              plan={planned}
              toPx={toPx}
              scale={scale}
              zoomK={k}
              color={c.planInk}
              onHover={(text, x, y) =>
                setHover((h) => (h && h.text === text && h.x === x && h.y === y ? h : { x, y, text }))
              }
              onOut={() => setHover(null)}
            />
          )}

          {/* history markers (rect footprint when sized, else dot) */}
          {history.map(({ m, index }) => {
            const stale = isStaleEpoch(m, map.current_epoch);
            return (
              <MarkerShape
                key={`mk-${index}`}
                m={m}
                stale={stale}
                latest={index === latestIndex}
                toPx={toPx}
                scale={scale}
                zoomK={k}
                halo={c.bg}
                ink={c.ink}
                onHover={(text) => {
                  const p = toPx(m.x_m!, m.y_m!);
                  setHover((h) => (h && h.text === text && h.x === p.x && h.y === p.y ? h : { x: p.x, y: p.y, text }));
                }}
                onOut={() => setHover(null)}
                onSelect={() => openDetail(m)}
              />
            );
          })}

          {/* the most recent operation, so "what just happened" is findable at
              a glance in a crowded map */}
          {latestDrawn && (
            <FocusRing m={latestDrawn.m} toPx={toPx} scale={scale} zoomK={k} color={c.ink} halo={c.bg} gapPx={5} />
          )}
          {highlighted && (
            <FocusRing m={highlighted.m} toPx={toPx} scale={scale} zoomK={k} color={c.accent} halo={c.bg} gapPx={9} dashed />
          )}

          {/* live current scan frame */}
          {f && f.center_x_m != null && f.center_y_m != null && f.width_m != null && f.height_m != null && (
            <FrameShape frame={f} toPx={toPx} scale={scale} zoomK={k} />
          )}

          {/* the scan line at the scan front, in the live frame's colour, turning
              with the frame — only while the backend says the scan is running */}
          {layers.live && live?.usable && live.status?.active && live.meta && (
            <LiveScanLine meta={live.meta} status={live.status} toPx={toPx} zoomK={k} />
          )}

          {/* the recommended next scan position, on top of everything it had to
              avoid to get there */}
          {layers.next && analysis?.next_position?.x_m != null && analysis.next_position.y_m != null && (
            <NextPositionShape
              at={toPx(analysis.next_position.x_m, analysis.next_position.y_m)}
              sizePx={(analysis.frame_size_m ?? 0) * scale}
              zoomK={k}
              color={c.planInk}
              halo={c.bg}
            />
          )}

          {/* step numbers: drawn last among the route shapes so no frame
              covers them; a badge pushed off its point keeps a hairline to it */}
          {planBadges.map((b, i) => (
            <Badge key={`pb-${i}`} at={b} from={planAnchors[i]!} n={i + 1} zoomK={k} ring={c.badgeRing} lead={c.planInk} />
          ))}
          {upBadges.map((b, i) => (
            <Badge
              key={`ub-${i}`}
              at={b}
              from={upAnchors[i]!}
              n={upIdx[i]! + 1}
              zoomK={k}
              ring={c.planInk}
              lead={c.planInk}
              hollow
              hollowFill={c.bg}
            />
          ))}

          {/* tip position + crosshair */}
          {t && t.x_m != null && t.y_m != null && (
            <TipShape at={toPx(t.x_m, t.y_m)} ring={c.tipRing} halo={c.bg} zoomK={k} />
          )}
        </Layer>

        {/* The ruler lives on the surface: its ends stay on the features they
            were put on when the operator zooms or pans. Its own layer, so the
            rubber band following the cursor redraws one line, not the map. */}
        <Layer listening={false}>
          <MeasureOverlay
            pts={pts}
            live={measureOn}
            cursor={cursor}
            toPx={toPx}
            zoom={zoom}
            canvasW={CANVAS_W}
            decimals={decimals}
            ink={c.ink}
            halo={c.bg}
          />
        </Layer>

        {/* Chrome — axis numbers, scale bar, tooltip. This layer carries the
            INVERSE of the stage transform, so it stays pinned to the viewport
            at any zoom/pan: zoomed in, the operator still gets a readable scale
            instead of a scale that slid off the canvas. */}
        <Layer listening={false} {...chrome}>
          {layers.grid && (
            <>
              {/* tick labels ride the world in x, the canvas edge in y; the last
                  number of each axis carries the unit */}
              {/* haloed: zoomed in, the numbers sit on top of thumbnails */}
              {xTicks.map((tk, i) => {
                const label = fmtNm(tk.v, stepNm) + (i === xTicks.length - 1 ? " nm" : "");
                return <Text key={`lx${i}`} x={tk.s - 40} width={80} align="center" y={CANVAS_H - 16} text={label} fontSize={9} fill={c.tick} stroke={c.bg} strokeWidth={2.5} fillAfterStrokeEnabled wrap="none" />;
              })}
              {yTicks.map((tk, i) => {
                const label = fmtNm(tk.v, stepNm) + (tk.s === topY ? " nm" : "");
                return <Text key={`ly${i}`} x={4} y={tk.s - 5} text={label} fontSize={9} fill={c.tick} stroke={c.bg} strokeWidth={2.5} fillAfterStrokeEnabled wrap="none" />;
              })}
            </>
          )}
          {/* scale bar: a background-coloured underlay keeps it legible over a thumbnail */}
          <Line
            points={[PAD, CANVAS_H - 30, PAD, CANVAS_H - 26, PAD + scaleBarPx, CANVAS_H - 26, PAD + scaleBarPx, CANVAS_H - 30]}
            stroke={c.bg}
            strokeWidth={5}
            opacity={0.7}
            lineCap="round"
            lineJoin="round"
          />
          <Line points={[PAD, CANVAS_H - 26, PAD + scaleBarPx, CANVAS_H - 26]} stroke={c.bar} strokeWidth={2} />
          <Line points={[PAD, CANVAS_H - 30, PAD, CANVAS_H - 22]} stroke={c.bar} strokeWidth={2} />
          <Line points={[PAD + scaleBarPx, CANVAS_H - 30, PAD + scaleBarPx, CANVAS_H - 22]} stroke={c.bar} strokeWidth={2} />
          <Text x={PAD} y={CANVAS_H - 44} text={`${fmtNm(stepNm, stepNm)} nm`} fontSize={10} fontStyle="bold" fill={c.bar} stroke={c.bg} strokeWidth={3} fillAfterStrokeEnabled />
          {tipAnchor && tipLines.length > 0 && (() => {
            const h = stageToScreen(tipAnchor, zoom);
            const wPx = Math.min(Math.max(...tipLines.map((l) => textWidthPx(l, 11))) + 14, 320);
            const hPx = tipLines.length * 14 + 8;
            const x = Math.max(4, Math.min(h.x + 10, CANVAS_W - wPx - 4));
            const y = h.y - hPx - 6 < 4 ? h.y + 12 : h.y - hPx - 6;
            return (
              <Label x={x} y={y} opacity={0.96} name={TRANSIENT_NODE}>
                <Tag fill={c.tooltipBg} stroke={c.tooltipBorder} strokeWidth={1} cornerRadius={4} />
                <Text text={tipLines.join("\n")} fontSize={11} lineHeight={1.25} padding={5} fill={c.tooltipText} wrap="none" />
              </Label>
            );
          })()}
        </Layer>
      </Stage>

      {/* tools — top left */}
      <div className="absolute left-2 top-2 flex items-center gap-1.5">
        <button
          type="button"
          onClick={toggleMeasure}
          aria-pressed={measureOn}
          title={tr("在地图上量两点间的距离", "Measure the distance between two points on the map")}
          className={
            "rounded-mast-ctl border px-2 py-0.5 text-xs transition-colors " +
            (measureOn
              ? "border-mast-accent bg-mast-accent font-semibold text-mast-accent-ink"
              : "border-mast-border text-mast-muted hover:text-mast-text")
          }
          style={measureOn ? undefined : { background: c.chipBg }}
        >
          {tr("测距", "Measure")}
        </button>
        <button
          type="button"
          onClick={exportPng}
          data-scanmap="export"
          title={tr("把当前视图（含比例尺与图例）存成 PNG", "Save the current view, with scale bar and legend, as a PNG")}
          className="rounded-mast-ctl border border-mast-border px-2 py-0.5 text-xs text-mast-muted transition-colors hover:text-mast-text"
          style={{ background: c.chipBg }}
        >
          {tr("导出 PNG", "Export PNG")}
        </button>
        {measureOn && (
          <span
            className="pointer-events-none rounded border border-mast-border px-1.5 py-0.5 text-[11px] text-mast-text"
            style={{ background: c.chipBg }}
          >
            {pts.a && !pts.b
              ? tr("再点终点 B", "Now click point B")
              : pts.a && pts.b
                ? tr("再点一下开始新的测量 · Esc 退出", "Click again to start a new measurement · Esc to exit")
                : tr("点起点 A · Esc 退出", "Click point A · Esc to exit")}
          </span>
        )}
      </div>

      {/* cursor readout + zoom state — top right. Also where a view mode says it
          could not be honoured: silently showing a different area than the
          selected button claims is worse than empty. */}
      <div
        className="pointer-events-none absolute right-2 top-2 rounded border border-mast-border px-2 py-1 text-right"
        style={{ background: c.chipBg }}
      >
        <CursorReadout cursor={cursor} decimals={decimals} />
        <div className="text-[10px] leading-tight text-mast-muted">
          {fit.fellBack
            ? viewMode === "follow"
              ? tr("无实时扫描框 · 已自动拟合", "No live scan frame · auto-fitted")
              : tr("压电范围未知 · 已自动拟合", "Piezo range unknown · auto-fitted")
            : k > 1
              ? tr(
                  `${k.toFixed(1)}× · 拖拽平移 · 双击复位`,
                  `${k.toFixed(1)}× · drag to pan · double-click to reset`,
                )
              : tr("滚轮缩放", "Wheel to zoom")}
        </div>
      </div>

      <Modal open={detail != null} onClose={() => setDetail(null)} title={detailTitle}>
        {detail && (
          <div className="space-y-3 text-sm">
            {detail.marker && (
              <div className="space-y-1">
                <DetailRowList rows={markerDetailRows(detail.marker, map.current_epoch)} />
                {markerDetailRows(detail.marker, map.current_epoch).length === 0 && (
                  <p className="text-mast-muted">
                    {tr("这个标记只记录了位置，没有别的信息。", "This marker only records a position; there is no other information.")}
                  </p>
                )}
              </div>
            )}
            {detail.images.length > 0 && (
              <div className={detail.marker ? "space-y-1 border-t border-mast-border pt-3" : "space-y-1"}>
                {detail.marker && (
                  <div className="mb-1 text-xs text-mast-muted">
                    {tr("这个位置下面的扫描图", "Scan image under this spot")}
                  </div>
                )}
                <DetailRowList rows={scanImageDetailRows(detail.images[0]!, Date.now())} />
                {detail.images[0]!.path && (
                  <div className="pt-1">
                    <CopyButton text={detail.images[0]!.path} label={tr("复制路径", "Copy path")} />
                  </div>
                )}
                {detail.images.length > 1 && (
                  <div className="pt-2 text-xs text-mast-muted">
                    {tr("下面还压着：", "Also underneath: ")}
                    {detail.images
                      .slice(1)
                      .map((im) => {
                        const tm = parseStamp(im.mtime);
                        return `${im.name}${tm != null ? ` (${fmtDateTime(tm).slice(5, 16)})` : ""}`;
                      })
                      .join(tr("、", ", "))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}

function DetailRowList({ rows }: { rows: DetailRow[] }) {
  return (
    <>
      {rows.map((r) => (
        <div key={r.label} className="flex gap-3">
          <span className="w-20 shrink-0 text-mast-muted">{r.label}</span>
          <span className={r.mono ? "min-w-0 break-all font-mono tabular-nums text-mast-text" : "min-w-0 text-mast-text"}>
            {r.value}
          </span>
        </div>
      ))}
    </>
  );
}

/** "x 123.4 nm  y −56.7 nm" for wherever the cursor is; dashes when it is off the map. */
function CursorReadout({ cursor, decimals }: { cursor: CursorStore; decimals: number }) {
  const p = useCursor(cursor);
  const fmt = (v: number | undefined) => (v == null ? "—" : `${fmtSignedNm(v * 1e9, decimals)} nm`);
  return (
    <div className="font-mono text-[11px] tabular-nums text-mast-text">
      <span className="text-mast-muted">x</span> {fmt(p?.x)}
      <span className="ml-2 text-mast-muted">y</span> {fmt(p?.y)}
    </div>
  );
}

/** The ruler: A → B (or A → cursor while B is being chosen), with the distance. */
function MeasureOverlay({
  pts,
  live,
  cursor,
  toPx,
  zoom,
  canvasW,
  decimals,
  ink,
  halo,
}: {
  pts: MeasurePts;
  live: boolean;
  cursor: CursorStore;
  toPx: (x: number, y: number) => Pt;
  zoom: { k: number; x: number; y: number };
  canvasW: number;
  decimals: number;
  ink: string;
  halo: string;
}) {
  const rubber = useCursor(cursor, live && !!pts.a && !pts.b);
  if (!pts.a) return null;
  const bW = pts.b ?? rubber;
  const k = zoom.k;
  const a = toPx(pts.a.x, pts.a.y);
  const dot = (p: Pt, key: string) => (
    <Circle key={key} x={p.x} y={p.y} radius={3.5 / k} fill={ink} stroke={halo} strokeWidth={1.5} strokeScaleEnabled={false} />
  );
  if (!bW) return <Group>{dot(a, "a")}</Group>;
  const b = toPx(bW.x, bW.y);
  const m = { d: Math.hypot(bW.x - pts.a.x, bW.y - pts.a.y), dx: bW.x - pts.a.x, dy: bW.y - pts.a.y };
  const text = fmtMeasurement(m, decimals, getLang() === "zh");
  const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  // The caption's centre sits off the midpoint along the line's upward normal,
  // so it never lies on the line it describes; then it is kept on the canvas.
  const wPx = textWidthPx(text, 11) * 1.05;
  const hPx = 14;
  const sa = stageToScreen(a, zoom);
  const sb = stageToScreen(b, zoom);
  const len = Math.hypot(sb.x - sa.x, sb.y - sa.y) || 1;
  let nx = -(sb.y - sa.y) / len;
  let ny = (sb.x - sa.x) / len;
  if (ny > 0) {
    nx = -nx;
    ny = -ny;
  }
  // Clear the line by the caption's half-extent in the normal's direction.
  const reach = Math.abs(nx) * (wPx / 2) + Math.abs(ny) * (hPx / 2) + 6;
  const sm = stageToScreen(mid, zoom);
  const cx = Math.min(Math.max(sm.x + nx * reach, wPx / 2 + 4), canvasW - wPx / 2 - 4);
  const cy = Math.max(sm.y + ny * reach, hPx / 2 + 36);
  const dx = cx - wPx / 2 - sm.x;
  const dy = cy - hPx / 2 - sm.y;
  const dashed = !pts.b;
  return (
    <Group>
      <Line points={[a.x, a.y, b.x, b.y]} stroke={halo} strokeWidth={4} opacity={0.7} strokeScaleEnabled={false} />
      <Line points={[a.x, a.y, b.x, b.y]} stroke={ink} strokeWidth={1.5} dash={dashed ? [5, 4] : undefined} strokeScaleEnabled={false} />
      {dot(a, "a")}
      {dot(b, "b")}
      <Text
        {...pinnedLabel(mid, dx, dy, k)}
        text={text}
        fontSize={11}
        fontStyle="bold"
        fill={ink}
        stroke={halo}
        strokeWidth={3}
        fillAfterStrokeEnabled
        wrap="none"
      />
    </Group>
  );
}

function _footprintPx(
  m: { x_m?: number | null; y_m?: number | null; w_m?: number | null; h_m?: number | null; angle_deg?: number | null },
  toPx: (x: number, y: number) => Pt,
  scale: number,
) {
  const c = toPx(m.x_m!, m.y_m!);
  return {
    // Position the Rect at the CENTRE and shift its origin there via offset, so
    // Konva rotates about the centre (not the top-left corner) — the core fix.
    cx: c.x,
    cy: c.y,
    w: Math.abs(m.w_m!) * scale,
    h: Math.abs(m.h_m!) * scale,
    rotation: -(m.angle_deg ?? 0), // lab CCW-positive → Konva CW-positive screen
  };
}

function hasFootprint(m: { w_m?: number | null; h_m?: number | null }): boolean {
  return m.w_m != null && m.h_m != null && m.w_m > 0 && m.h_m > 0;
}

function ScanImageShape({
  img,
  toPx,
  scale,
  opacity,
}: {
  img: ScanImg;
  toPx: (x: number, y: number) => Pt;
  scale: number;
  opacity: number;
}) {
  const [el, setEl] = useState<HTMLImageElement | null>(null);
  useEffect(() => {
    if (!img.image_b64) return;
    const image = new window.Image();
    image.src = `data:image/png;base64,${img.image_b64}`;
    const done = () => setEl(image);
    if (image.complete) done();
    else image.onload = done;
    return () => {
      image.onload = null;
    };
  }, [img.image_b64]);
  if (!el || img.center_x_m == null || img.center_y_m == null || !img.width_m || !img.height_m) {
    return null;
  }
  const c = toPx(img.center_x_m, img.center_y_m);
  const w = img.width_m * scale;
  const h = img.height_m * scale;
  return (
    <KonvaImage
      image={el}
      x={c.x}
      y={c.y}
      width={w}
      height={h}
      offsetX={w / 2}
      offsetY={h / 2}
      rotation={-(img.angle_deg ?? 0)}
      opacity={opacity}
      listening={false}
    />
  );
}

function MarkerShape({
  m,
  stale,
  latest,
  toPx,
  scale,
  zoomK,
  halo,
  ink,
  onHover,
  onOut,
  onSelect,
}: {
  m: Marker;
  stale: boolean;
  latest: boolean;
  toPx: (x: number, y: number) => Pt;
  scale: number;
  zoomK: number;
  halo: string;
  ink: string;
  onHover: (text: string) => void;
  onOut: () => void;
  onSelect: () => void;
}) {
  if (m.x_m == null || m.y_m == null) return null;
  const color = kindColor(m.kind);
  const failed = (m.status ?? "done") === "failed";
  // A superseded coordinate generation: real history, but the sample stage has
  // moved since, so this is not where it happened any more. Faded ONCE — the
  // footprint branch used to apply the factor twice.
  const opacity = stale ? STALE_EPOCH_OPACITY : 1;
  const tip =
    `${m.label || kindLabel(m.kind) || m.kind || tr("操作", "Action")}` +
    ` · ${clockOf(m.timestamp)}` +
    (failed ? tr(" · 失败", " · failed") : "") +
    (stale ? tr(" · 粗动前的旧坐标系", " · pre-coarse-move coordinates") : "") +
    (latest ? tr(" · 最新", " · latest") : "");
  const hoverProps = {
    name: MARKER_NODE,
    onMouseEnter: () => onHover(tip),
    onMouseMove: () => onHover(tip),
    onMouseLeave: onOut,
    onClick: onSelect,
    onTap: onSelect,
  };

  if (hasFootprint(m)) {
    const fp = _footprintPx(m, toPx, scale);
    return (
      <Rect
        x={fp.cx}
        y={fp.cy}
        width={fp.w}
        height={fp.h}
        offsetX={fp.w / 2}
        offsetY={fp.h / 2}
        rotation={fp.rotation}
        stroke={color}
        strokeWidth={1.5}
        strokeScaleEnabled={false}
        // An outline and the faintest wash: the thumbnail under a scan frame
        // IS that scan, and a 22 % blue fill over it tinted the surface.
        fill={m.kind === "scan" && !failed ? withAlpha(color, 0.06) : undefined}
        opacity={opacity}
        dash={failed ? [4, 4] : undefined}
        {...hoverProps}
      />
    );
  }
  const c = toPx(m.x_m, m.y_m);
  // A failed point operation is drawn hollow, so it does not read as done. A
  // crash is the exception: the crash IS the event (its status is always
  // "failed"), and it is the one marker that must never fade into the map.
  const hollow = failed && (m.kind ?? "") !== "crash";
  const outline = dotOutline(color, halo, ink);
  return (
    <Circle
      x={c.x}
      y={c.y}
      radius={4.5 / zoomK}
      fill={hollow ? halo : color}
      stroke={hollow ? color : outline}
      strokeWidth={hollow ? 1.75 : outline === halo ? 1 : 1.25}
      strokeScaleEnabled={false}
      opacity={opacity}
      {...hoverProps}
    />
  );
}

/** A ring around one marker: the latest action, or the row hovered in the list. */
function FocusRing({
  m,
  toPx,
  scale,
  zoomK,
  color,
  halo,
  gapPx,
  dashed = false,
}: {
  m: Marker;
  toPx: (x: number, y: number) => Pt;
  scale: number;
  zoomK: number;
  color: string;
  halo: string;
  gapPx: number;
  dashed?: boolean;
}) {
  if (m.x_m == null || m.y_m == null) return null;
  const g = gapPx / zoomK;
  if (hasFootprint(m)) {
    const fp = _footprintPx(m, toPx, scale);
    const w = fp.w + 2 * g;
    const h = fp.h + 2 * g;
    const common = { x: fp.cx, y: fp.cy, width: w, height: h, offsetX: w / 2, offsetY: h / 2, rotation: fp.rotation, strokeScaleEnabled: false, listening: false } as const;
    return (
      <Group listening={false}>
        <Rect {...common} stroke={halo} strokeWidth={4} opacity={0.75} />
        <Rect {...common} stroke={color} strokeWidth={2} dash={dashed ? [5, 3] : undefined} />
      </Group>
    );
  }
  const c = toPx(m.x_m, m.y_m);
  const r = 4.5 / zoomK + g;
  return (
    <Group listening={false}>
      <Circle x={c.x} y={c.y} radius={r} stroke={halo} strokeWidth={4} opacity={0.75} strokeScaleEnabled={false} />
      <Circle x={c.x} y={c.y} radius={r} stroke={color} strokeWidth={2} dash={dashed ? [4, 3] : undefined} strokeScaleEnabled={false} />
    </Group>
  );
}

/** A keep-out disc from the map analysis: dashed rim, faint wash. */
function AvoidZoneShape({
  zone,
  toPx,
  scale,
}: {
  zone: NonNullable<MapAnalysis["avoid_zones"]>[number];
  toPx: (x: number, y: number) => Pt;
  scale: number;
}) {
  const p = toPx(zone.x_m, zone.y_m);
  const r = zone.radius_m * scale;
  if (!Number.isFinite(r) || r <= 0) return null;
  const color = kindColor(zone.kind);
  // Separate alphas for fill and rim — a node-level opacity fades both, which
  // is how the old 0.12 disc ended up a muddy solid with an invisible edge.
  return (
    <Circle
      x={p.x}
      y={p.y}
      radius={r}
      fill={withAlpha(color, 0.05)}
      stroke={withAlpha(color, 0.85)}
      strokeWidth={1.25}
      dash={[6, 4]}
      strokeScaleEnabled={false}
      listening={false}
    />
  );
}

/** Where the analysis says to scan next: a crosshair plus the frame outline. */
function NextPositionShape({
  at,
  sizePx,
  zoomK,
  color,
  halo,
}: {
  at: Pt;
  sizePx: number;
  zoomK: number;
  color: string;
  halo: string;
}) {
  const half = Math.max(sizePx, 6 / zoomK) / 2;
  const arm = 6 / zoomK;
  return (
    <Group listening={false}>
      <Rect
        x={at.x}
        y={at.y}
        width={half * 2}
        height={half * 2}
        offsetX={half}
        offsetY={half}
        stroke={color}
        strokeWidth={1.6}
        dash={[5, 3]}
        strokeScaleEnabled={false}
      />
      <Line points={[at.x - half - arm, at.y, at.x + half + arm, at.y]} stroke={color} strokeWidth={1} strokeScaleEnabled={false} />
      <Line points={[at.x, at.y - half - arm, at.x, at.y + half + arm]} stroke={color} strokeWidth={1} strokeScaleEnabled={false} />
      {/* half is a WORLD offset (the frame grows with zoom, the label must keep
          clearing it); 0/-14 are the screen gap, so they do not. Above the
          LEFT corner: the top-right corner is where a plan step's number
          badge sits, and the next position is very often plan step 1. */}
      <Text
        {...pinnedLabel({ x: at.x - half, y: at.y - half }, 0, -14, zoomK)}
        text={tr("建议下一个位置", "Suggested next position")}
        fontSize={10}
        fill={color}
        stroke={halo}
        strokeWidth={3}
        fillAfterStrokeEnabled
      />
    </Group>
  );
}

/** The published route: dashed polyline, step frames, the next step ringed.
 *
 *  The steps arrive in execution order and shrink from the front as operations
 *  land (PlanOverlay.advance), so the first element is always what happens next
 *  and the length is always how much is left — no extra fields needed. Matches
 *  what the backend matplotlib renderer has always drawn (exp_map.render_map_figure).
 *  The step NUMBERS are badges drawn by the caller, laid out with the
 *  candidate sequence's so that none lands on another. */
function PlanRoute({
  plan,
  toPx,
  scale,
  zoomK,
  color,
  onHover,
  onOut,
}: {
  plan: Marker[];
  toPx: (x: number, y: number) => Pt;
  scale: number;
  zoomK: number;
  color: string;
  onHover: (text: string, x: number, y: number) => void;
  onOut: () => void;
}) {
  const first = plan[0];
  if (!first) return null;
  const pts = plan.flatMap((m) => {
    const p = toPx(m.x_m!, m.y_m!);
    return [p.x, p.y];
  });
  const head = toPx(first.x_m!, first.y_m!);
  return (
    <Group>
      {plan.length >= 2 && (
        <Line points={pts} stroke={color} strokeWidth={1.4} dash={[6, 4]} opacity={0.75} strokeScaleEnabled={false} listening={false} />
      )}
      {plan.map((m, i) => {
        const p = toPx(m.x_m!, m.y_m!);
        const w = Math.abs(m.w_m ?? 0) * scale;
        const h = Math.abs(m.h_m ?? 0) * scale;
        const tip =
          tr(`计划 ${i + 1}/${plan.length}`, `Plan ${i + 1}/${plan.length}`) +
          (m.label ? ` · ${m.label}` : "") +
          (i === 0 ? tr(" · 下一步", " · next") : "");
        const hoverProps = {
          onMouseEnter: () => onHover(tip, p.x, p.y),
          onMouseMove: () => onHover(tip, p.x, p.y),
          onMouseLeave: onOut,
        };
        return w > 0 && h > 0 ? (
          <Rect
            key={`plan-${i}`}
            x={p.x}
            y={p.y}
            width={w}
            height={h}
            offsetX={w / 2}
            offsetY={h / 2}
            rotation={-(m.angle_deg ?? 0)}
            stroke={color}
            strokeWidth={i === 0 ? 1.8 : 1.2}
            dash={[4, 4]}
            opacity={i === 0 ? 0.95 : 0.6}
            strokeScaleEnabled={false}
            {...hoverProps}
          />
        ) : (
          <Circle
            key={`plan-${i}`}
            x={p.x}
            y={p.y}
            radius={4 / zoomK}
            stroke={color}
            strokeWidth={1.2}
            opacity={i === 0 ? 0.95 : 0.6}
            strokeScaleEnabled={false}
            {...hoverProps}
          />
        );
      })}
      {/* The step about to be executed, ringed so it reads at a glance. No text
          label here: the route, the candidate sequence and the suggested-next
          label all converge on the same few hundred nanometres, and a sentence
          pinned to this point would land on top of them. The plan title and the
          remaining step count live in the chip row above the canvas, where they
          are readable at any zoom. */}
      <Circle x={head.x} y={head.y} radius={9 / zoomK} stroke={color} strokeWidth={1.6} opacity={0.9} strokeScaleEnabled={false} listening={false} />
    </Group>
  );
}

/** A numbered step badge at a constant screen size. Solid for a step someone
 *  committed to (the plan), hollow for one the analysis merely proposes. */
function Badge({
  at,
  from,
  n,
  zoomK,
  ring,
  lead,
  hollow = false,
  hollowFill,
}: {
  at: Pt;
  from: Pt;
  n: number;
  zoomK: number;
  ring: string;
  lead: string;
  hollow?: boolean;
  hollowFill?: string;
}) {
  const r = hollow ? BADGE_R - 1 : BADGE_R;
  const moved = Math.hypot(at.x - from.x, at.y - from.y) * zoomK > r;
  return (
    <Group listening={false}>
      {moved && (
        <Line points={[from.x, from.y, at.x, at.y]} stroke={lead} strokeWidth={0.75} opacity={0.7} strokeScaleEnabled={false} />
      )}
      <Circle
        x={at.x}
        y={at.y}
        radius={r / zoomK}
        fill={hollow ? hollowFill : BADGE_FILL}
        stroke={ring}
        strokeWidth={hollow ? 1 : 1.5}
        dash={hollow ? [2, 1.5] : undefined}
        strokeScaleEnabled={false}
      />
      <Text
        {...pinnedLabel(at, -r, -r, zoomK)}
        width={2 * r}
        height={2 * r}
        align="center"
        verticalAlign="middle"
        text={String(n)}
        fontSize={n >= 10 ? 8 : hollow ? 9 : 10}
        fontStyle="bold"
        fill={hollow ? lead : BADGE_TEXT}
      />
    </Group>
  );
}

/** The positions the current strategy would walk next — ghost frames.
 *
 *  Distinct from the published plan: nobody committed to these, they are what
 *  the analysis recomputes from the record every time. Same "not yet" hue,
 *  fainter, hollow numbers, so the two channels read as related but not the
 *  same claim. */
function UpcomingRoute({
  positions,
  side,
  skipFirst,
  toPx,
  color,
}: {
  positions: NonNullable<MapAnalysis["upcoming"]>;
  side: number;
  skipFirst: boolean;
  toPx: (x: number, y: number) => Pt;
  color: string;
}) {
  if (!positions.length) return null;
  const half = side / 2;
  return (
    <Group listening={false}>
      {positions.length >= 2 && (
        <Line
          points={positions.flatMap((p) => {
            const q = toPx(p.x_m!, p.y_m!);
            return [q.x, q.y];
          })}
          stroke={color}
          strokeWidth={1}
          dash={[2, 4]}
          opacity={0.4}
          strokeScaleEnabled={false}
        />
      )}
      {positions.map((p, i) => {
        if (i === 0 && skipFirst) return null;
        const q = toPx(p.x_m!, p.y_m!);
        return (
          <Rect
            key={`up-${i}`}
            x={q.x}
            y={q.y}
            width={side}
            height={side}
            offsetX={half}
            offsetY={half}
            stroke={color}
            strokeWidth={1}
            dash={[2, 3]}
            opacity={0.5}
            strokeScaleEnabled={false}
          />
        );
      })}
    </Group>
  );
}

/** The reachable piezo area, as a dashed square. Drawn only in the whole-range
 *  view, where without it the nanometre-scale markers have nothing to scale
 *  against and the map reads as a few dots in an empty field. */
function PiezoRangeShape({
  half,
  toPx,
  stroke,
  zoomK,
}: {
  half: number;
  toPx: (x: number, y: number) => Pt;
  stroke: string;
  zoomK: number;
}) {
  const a = toPx(-half, -half);
  const b = toPx(half, half);
  return (
    <Group listening={false}>
      <Rect
        x={Math.min(a.x, b.x)}
        y={Math.min(a.y, b.y)}
        width={Math.abs(b.x - a.x)}
        height={Math.abs(b.y - a.y)}
        stroke={stroke}
        strokeWidth={1}
        dash={[4, 4]}
        opacity={0.7}
        strokeScaleEnabled={false}
      />
      <Text
        {...pinnedLabel({ x: Math.min(a.x, b.x), y: Math.min(a.y, b.y) }, 4, 4, zoomK)}
        text={tr(`压电范围 ±${(half * 1e6).toFixed(2)} µm`, `Piezo range ±${(half * 1e6).toFixed(2)} µm`)}
        fontSize={9}
        fill={stroke}
      />
    </Group>
  );
}

function FrameShape({
  frame,
  toPx,
  scale,
  zoomK,
}: {
  frame: NonNullable<ScanMap["frame"]>;
  toPx: (x: number, y: number) => Pt;
  scale: number;
  zoomK: number;
}) {
  const fp = _footprintPx(
    { x_m: frame.center_x_m, y_m: frame.center_y_m, w_m: frame.width_m, h_m: frame.height_m, angle_deg: frame.angle_deg },
    toPx,
    scale,
  );
  const arm = 8 / zoomK;
  return (
    <Group listening={false}>
      <Rect x={fp.cx} y={fp.cy} width={fp.w} height={fp.h} offsetX={fp.w / 2} offsetY={fp.h / 2} rotation={fp.rotation} stroke="#06b6d4" strokeWidth={2} strokeScaleEnabled={false} />
      <Line points={[fp.cx - arm, fp.cy, fp.cx + arm, fp.cy]} stroke="#06b6d4" strokeWidth={1} strokeScaleEnabled={false} />
      <Line points={[fp.cx, fp.cy - arm, fp.cx, fp.cy + arm]} stroke="#06b6d4" strokeWidth={1} strokeScaleEnabled={false} />
    </Group>
  );
}

function TipShape({ at, ring, halo, zoomK }: { at: Pt; ring: string; halo: string; zoomK: number }) {
  const arm = 10 / zoomK;
  return (
    <Group listening={false}>
      <Line points={[at.x - arm, at.y, at.x + arm, at.y]} stroke="#f43f5e" strokeWidth={0.8} opacity={0.5} strokeScaleEnabled={false} />
      <Line points={[at.x, at.y - arm, at.x, at.y + arm]} stroke="#f43f5e" strokeWidth={0.8} opacity={0.5} strokeScaleEnabled={false} />
      <Circle x={at.x} y={at.y} radius={5 / zoomK} fill="#f43f5e" stroke={ring} strokeWidth={1} strokeScaleEnabled={false} />
      {/* haloed: the tip is usually over the image it is scanning */}
      <Text
        {...pinnedLabel(at, 8, -6, zoomK)}
        text={tr("针尖", "Tip")}
        fontSize={11}
        fontStyle="bold"
        fill="#f43f5e"
        stroke={halo}
        strokeWidth={3}
        fillAfterStrokeEnabled
      />
    </Group>
  );
}

// Legend reused by the page header — matches the backend KIND_STYLE palette.
// With `counts`, each marker kind shows how many are on the map ("STS 14", or
// "5/14" when the layers or the time window hide some); a kind with none drawn
// is dimmed rather than removed, so the legend does not jump around.
export function ScanMapLegend({
  counts,
  planSteps,
  showLatest = false,
}: {
  counts?: Record<string, { shown: number; total: number }>;
  planSteps?: number;
  showLatest?: boolean;
} = {}) {
  // [label, colour, what to count: a marker kind, the plan's steps, or nothing
  // (the live frame and the tip are single live elements, not a tally)]
  const items: [string, string, string | null][] = [
    [tr("扫描框", "Scan frame"), KIND_COLOR.frame, null],
    [tr("针尖", "Tip"), KIND_COLOR.tip, null],
    [tr("扫图", "Scan"), KIND_COLOR.scan, "scan"],
    ["STS", KIND_COLOR.sts, "sts"],
    [tr("电脉冲", "Pulse"), KIND_COLOR.pulse, "pulse"],
    [tr("修针尖", "Tip shaping"), KIND_COLOR.tip_shape, "tip_shape"],
    [tr("进针", "Approach"), KIND_COLOR.approach, "approach"],
    [tr("撞针", "Tip crash"), KIND_COLOR.crash, "crash"],
    [tr("粗动换区", "Coarse move"), KIND_COLOR.coarse_move, "coarse_move"],
    [tr("移动", "Move"), KIND_COLOR.move, "move"],
    [tr("手动", "Manual"), KIND_COLOR.manual, "manual"],
    [tr("计划", "Plan"), KIND_COLOR.plan, "@plan"],
  ];
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-mast-muted">
      {items.map(([label, color, what]) => {
        let count: string | null = null;
        let empty = false;
        if (what === "@plan") {
          if (planSteps != null) {
            count = String(planSteps);
            empty = planSteps === 0;
          }
        } else if (what && counts) {
          const n = counts[what] ?? { shown: 0, total: 0 };
          count = fmtCount(n.shown, n.total);
          empty = n.shown === 0;
        }
        return (
          <span key={label} className={"inline-flex items-center gap-1.5" + (empty ? " opacity-50" : "")}>
            <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: color }} />
            {label}
            {count != null && <span className="tabular-nums text-mast-text">{count}</span>}
          </span>
        );
      })}
      {showLatest && (
        <span className="inline-flex items-center gap-1.5" title={tr("最近记录的一个操作外面画了一圈", "The most recently recorded operation is ringed")}>
          <span className="inline-block h-2.5 w-2.5 rounded-full border-2 border-mast-text" />
          {tr("最新操作", "Latest action")}
        </span>
      )}
    </div>
  );
}
