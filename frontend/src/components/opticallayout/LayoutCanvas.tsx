import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent as ReactDragEvent, type MutableRefObject, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { tr } from "@/lib/i18n";
import type { ComponentType, OpticalLayout, Vec } from "@/lib/opticalLayout/types";
import { isComponentType } from "@/lib/opticalLayout/types";
import type { DetailedTrace } from "@/lib/opticalLayout/trace";
import type { HitIndicator, NearMiss } from "@/lib/opticalLayout/indicators";
import type { ThemeName } from "@/lib/opticalLayout/colors";
import { layoutBounds, nearestHole, snapPoint, type SnapMode } from "@/lib/opticalLayout/geometry";
import { EMPTY_RESIZE_MEMORY, centerOn, fitView, formatLength, panBy, toScreen, toWorld, viewAfterResize, zoomAt, type ResizeMemory, type View } from "@/lib/opticalLayout/view";
import { createFrameThrottle } from "@/lib/opticalLayout/frame";
import { LayoutSvg, type ComponentHandlers, type DisplayOptions } from "./LayoutSvg";
import { nearestSegment, type SegRef, type SegmentHit } from "@/lib/opticalLayout/beamDraw";
import { beamAt, polarizationText, powerText, spotText } from "@/lib/opticalLayout/beam";
import { wavelengthColor, wavelengthLabel } from "@/lib/opticalLayout/colors";

// The interactive canvas: pan, zoom, select, drag, drop from the palette, the tools'
// clicks. The picture itself is <LayoutSvg>, which is memoised; everything that changes
// at pointer speed (the cursor read-out, the rubber band, the tool previews) lives in a
// second, transparent <svg> on top, so a mouse move never repaints the layout.
//
// Dragging a component does not touch the undo history on every move. The canvas hands the
// positions to `onPreview` through a frame throttle (at most one per animation frame, so
// the ray trace runs at most once per frame), and commits ONE history step on release.

export const PALETTE_MIME = "application/x-mast-optical-type";
/** what the palette's assemblies carry when dragged (a template id, lib/opticalLayout/templates.ts) */
export const TEMPLATE_MIME = "application/x-mast-optical-template";

export type CanvasTool = "select" | "aim" | "measure";

export interface ToolPoint {
  world: Vec;
  /** the component under the pointer, if any */
  componentId: string | null;
}

export interface CanvasFocus {
  point: Vec;
  /** a new value asks for the view to move there */
  nonce: number;
  /** move the view so that the point is in the middle even when it is already in view (the default pans only when it is not) */
  center?: boolean;
  /** with `center`: zoom in to at least this many pixels per millimetre */
  minZoom?: number;
}

interface Props {
  layout: OpticalLayout;
  trace: DetailedTrace;
  indicators: ReadonlyMap<string, HitIndicator[]>;
  nearMisses: readonly NearMiss[];
  display: DisplayOptions;
  theme: ThemeName;
  selected: ReadonlySet<string>;
  commentCounts?: ReadonlyMap<string, number>;
  snap: SnapMode;
  tool: CanvasTool;
  /** an old revision: nothing can be changed */
  readOnly: boolean;
  /** a new value fits the whole table into the view */
  fitNonce: number;
  focus?: CanvasFocus | null;
  /** kept up to date with the platform point at the middle of the view, for "click to add" */
  centerRef?: MutableRefObject<Vec | null>;
  onSelect: (ids: string[]) => void;
  onPreview: (positions: ReadonlyMap<string, Vec> | null) => void;
  onCommitMove: (positions: ReadonlyMap<string, Vec>) => void;
  onDropType: (type: ComponentType, at: Vec) => void;
  /** an assembly dropped from the palette: its anchor goes here */
  onDropTemplate?: (id: string, at: Vec) => void;
  onToolPoint: (p: ToolPoint) => void;
  onLockedDrag?: () => void;
  /** drawn above the layout, in screen pixels; re-created freely, nothing below it repaints */
  renderOverlay?: (ctx: { view: View; cursor: Vec | null }) => ReactNode;
  /** the beam segment the beam inspector shows, highlighted on the canvas */
  beamPick?: SegRef | null;
  /**
   * A plain click on the empty canvas with the select tool, before it clears the selection: the editor picks the
   * beam under it (and says so by returning true, which keeps the canvas from clearing anything).
   */
  onBackgroundClick?: (world: Vec, k: number) => boolean;
}

/** How close (px) the pointer has to be to a beam for its tooltip, or for a click to pick it. */
export const BEAM_PICK_PX = 6;

const DRAG_THRESHOLD_PX = 3;

/** A translucent panel behind the read-out text (Tailwind cannot put an opacity on a var() colour). */
const READOUT_BG = { background: "color-mix(in srgb, var(--mast-panel) 82%, transparent)" } as const;

type Drag =
  | { kind: "pan"; pointer: number; sx: number; sy: number; view: View; moved: boolean }
  | { kind: "move"; pointer: number; sx: number; sy: number; start: Vec; lead: string; origin: Map<string, Vec>; moved: boolean; clicked: string; additive: boolean }
  | { kind: "band"; pointer: number; sx: number; sy: number; cx: number; cy: number; additive: boolean }
  | { kind: "locked"; pointer: number; sx: number; sy: number; warned: boolean };

function useElementSize(): [(node: HTMLDivElement | null) => void, { w: number; h: number }] {
  const [size, setSize] = useState({ w: 0, h: 0 });
  const observer = useRef<ResizeObserver | null>(null);
  const ref = useCallback((node: HTMLDivElement | null) => {
    observer.current?.disconnect();
    observer.current = null;
    if (!node) return;
    const publish = (w: number, h: number) => setSize((p) => (Math.abs(p.w - w) < 1 && Math.abs(p.h - h) < 1 ? p : { w: Math.floor(w), h: Math.floor(h) }));
    // the content box, as the observer reports it (the border-box would make a 2 px "resize" right after loading)
    publish(node.clientWidth, node.clientHeight);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const e = entries[0];
      if (e) publish(e.contentRect.width, e.contentRect.height);
    });
    ro.observe(node);
    observer.current = ro;
  }, []);
  // No effect cleanup that disconnects: React calls the ref with null when the element goes, which disconnects.
  // A cleanup effect would also run in StrictMode's simulated unmount, which does not call the ref again, and
  // leave the canvas deaf to every later resize (focus mode, the window) in development.
  return [ref, size];
}

export function LayoutCanvas(props: Props) {
  const { layout, trace, indicators, nearMisses, display, theme, selected, commentCounts, snap, tool, readOnly, fitNonce, focus, renderOverlay } = props;
  const [boxRef, size] = useElementSize();
  const svgHost = useRef<HTMLDivElement | null>(null);
  const [view, setView] = useState<View | null>(null);
  const [cursor, setCursor] = useState<Vec | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [band, setBand] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const [panning, setPanning] = useState(false);

  // Always the latest props and view for the handlers, which are created once.
  const latest = useRef({ ...props, view });
  latest.current = { ...props, view };
  const drag = useRef<Drag | null>(null);
  const lastMove = useRef<ReadonlyMap<string, Vec> | null>(null);
  const pendingToolClick = useRef<ToolPoint | null>(null);
  const fitted = useRef<{ nonce: number; w: number; h: number } | null>(null);
  const focused = useRef<number | null>(null);

  if (props.centerRef && view && size.w > 0) props.centerRef.current = toWorld(view, size.w / 2, size.h / 2);

  // ── fit ──
  const fitNow = useCallback(() => {
    const l = latest.current.layout;
    return fitView(layoutBounds(l.table, l.components, 30), size.w, size.h, 40);
  }, [size.w, size.h]);

  useEffect(() => {
    if (size.w < 40 || size.h < 40) return;
    if (view === null || fitted.current === null || fitted.current.nonce !== fitNonce) {
      fitted.current = { nonce: fitNonce, w: size.w, h: size.h };
      setView(fitNow());
    }
  }, [size.w, size.h, fitNonce, view, fitNow]);

  // ── a resize (focus mode, the window) keeps what was in view in view: the middle stays in the middle and the
  // zoom grows or shrinks with the canvas, so going into focus mode enlarges what one was looking at; going
  // back to a size it had (leaving focus mode) puts back the view it had, unless somebody panned or zoomed ──
  const lastSize = useRef<{ w: number; h: number } | null>(null);
  const resizeMemory = useRef<ResizeMemory>(EMPTY_RESIZE_MEMORY);
  /** the size before a resize that came during a drag: the view follows it when the drag ends */
  const resizedInDrag = useRef<{ w: number; h: number } | null>(null);
  const followResize = useCallback((prev: { w: number; h: number }, now: { w: number; h: number }) => {
    if (prev.w < 40 || prev.h < 40 || now.w < 40 || now.h < 40) return;
    if (prev.w === now.w && prev.h === now.h) return;
    const v = latest.current.view;
    if (!v) return;
    const r = viewAfterResize(resizeMemory.current, v, prev, now);
    resizeMemory.current = r.mem;
    setView(r.view);
  }, []);
  useEffect(() => {
    const prev = lastSize.current;
    lastSize.current = { w: size.w, h: size.h };
    if (!prev) return;
    // not under a drag: what is being dragged would jump away from the pointer
    if (drag.current) {
      if (!resizedInDrag.current) resizedInDrag.current = prev;
      return;
    }
    followResize(prev, size);
  }, [size.w, size.h, followResize]);

  // ── move the view to a requested point, keeping the zoom ──
  useEffect(() => {
    if (!focus || focused.current === focus.nonce || !view || size.w < 40) return;
    focused.current = focus.nonce;
    if (focus.center) {
      setView(centerOn(view, focus.point, size.w, size.h, focus.minZoom ?? 0));
      return;
    }
    const s = toScreen(view, focus.point);
    const inside = s.x > 60 && s.x < size.w - 60 && s.y > 40 && s.y < size.h - 40;
    if (!inside) setView(panBy(view, size.w / 2 - s.x, size.h / 2 - s.y));
  }, [focus, view, size.w, size.h]);

  // ── wheel zoom: a native listener, because React's wheel handlers are passive ──
  useEffect(() => {
    const el = svgHost.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const dy = Math.max(-240, Math.min(240, e.deltaY));
      const factor = Math.exp(-dy * 0.0016);
      setView((v) => (v ? zoomAt(v, factor, e.clientX - rect.left, e.clientY - rect.top) : v));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  // ── frame-throttled updates ──
  const moveThrottle = useMemo(
    () => createFrameThrottle<ReadonlyMap<string, Vec>>((m) => latest.current.onPreview(m), (cb) => requestAnimationFrame(cb), (id) => cancelAnimationFrame(id)),
    [],
  );
  const panThrottle = useMemo(() => createFrameThrottle<View>((v) => setView(v), (cb) => requestAnimationFrame(cb), (id) => cancelAnimationFrame(id)), []);
  const cursorThrottle = useMemo(() => createFrameThrottle<Vec | null>((p) => setCursor(p), (cb) => requestAnimationFrame(cb), (id) => cancelAnimationFrame(id)), []);
  useEffect(
    () => () => {
      moveThrottle.cancel();
      panThrottle.cancel();
      cursorThrottle.cancel();
    },
    [moveThrottle, panThrottle, cursorThrottle],
  );

  // ── helpers ──
  const localPoint = (e: { clientX: number; clientY: number }) => {
    const r = svgHost.current?.getBoundingClientRect();
    return { x: e.clientX - (r?.left ?? 0), y: e.clientY - (r?.top ?? 0) };
  };

  const endDrag = useCallback(
    (commit: boolean) => {
      const d = drag.current;
      drag.current = null;
      pendingToolClick.current = null;
      setPanning(false);
      setBand(null);
      // a resize that came during the drag: the view follows it now
      const before = resizedInDrag.current;
      resizedInDrag.current = null;
      if (before && lastSize.current) followResize(before, lastSize.current);
      if (!d) return;
      if (d.kind === "move") {
        moveThrottle.cancel();
        if (commit && d.moved) {
          // the last position, exactly: recomputed from the pointer, not from the last frame
          const last = lastMove.current;
          if (last) latest.current.onCommitMove(last);
        }
        lastMove.current = null;
        latest.current.onPreview(null);
      } else if (d.kind === "pan") {
        panThrottle.flush();
      }
    },
    [moveThrottle, panThrottle, followResize],
  );

  // Escape while dragging puts everything back.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && drag.current) {
        e.stopPropagation();
        endDrag(false);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [endDrag]);

  // ── pointer: components ──
  const handlers: ComponentHandlers = useMemo(
    () => ({
      onDown: (id, e) => {
        const cur = latest.current;
        const v = cur.view;
        if (!v || e.button !== 0) return;
        e.stopPropagation();
        const host = svgHost.current;
        const rect = host?.getBoundingClientRect();
        const px = e.clientX - (rect?.left ?? 0);
        const py = e.clientY - (rect?.top ?? 0);
        host?.setPointerCapture(e.pointerId);
        if (cur.tool !== "select" || cur.readOnly) {
          drag.current = { kind: "locked", pointer: e.pointerId, sx: px, sy: py, warned: true };
          pendingToolClick.current = { world: toWorld(v, px, py), componentId: id };
          return;
        }
        const additive = e.shiftKey || e.ctrlKey || e.metaKey;
        let next: string[];
        if (additive) next = cur.selected.has(id) ? [...cur.selected].filter((x) => x !== id) : [...cur.selected, id];
        else next = cur.selected.has(id) ? [...cur.selected] : [id];
        cur.onSelect(next);
        const comp = cur.layout.components.find((c) => c.id === id);
        if (!comp || additive) {
          drag.current = { kind: "locked", pointer: e.pointerId, sx: px, sy: py, warned: true };
          return;
        }
        if (comp.locked) {
          drag.current = { kind: "locked", pointer: e.pointerId, sx: px, sy: py, warned: false };
          return;
        }
        const origin = new Map<string, Vec>();
        for (const c of cur.layout.components) if (next.includes(c.id) && !c.locked) origin.set(c.id, { x: c.x_mm, y: c.y_mm });
        drag.current = { kind: "move", pointer: e.pointerId, sx: px, sy: py, start: toWorld(v, px, py), lead: id, origin, moved: false, clicked: id, additive };
      },
      onEnter: (id) => setHover(id),
      onLeave: (id) => setHover((h) => (h === id ? null : h)),
    }),
    [],
  );

  // ── pointer: background ──
  const onBackgroundDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    const v = view;
    if (!v) return;
    const p = localPoint(e);
    if (e.button === 1 || e.button === 2) {
      e.preventDefault();
    } else if (e.button !== 0) {
      return;
    }
    e.currentTarget.setPointerCapture(e.pointerId);
    if (e.button === 0 && e.shiftKey && tool === "select" && !readOnly) {
      drag.current = { kind: "band", pointer: e.pointerId, sx: p.x, sy: p.y, cx: p.x, cy: p.y, additive: true };
      return;
    }
    drag.current = { kind: "pan", pointer: e.pointerId, sx: p.x, sy: p.y, view: v, moved: false };
    pendingToolClick.current = e.button === 0 ? { world: toWorld(v, p.x, p.y), componentId: null } : null;
  };

  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const v = view;
    if (!v) return;
    const p = localPoint(e);
    const w = toWorld(v, p.x, p.y);
    cursorThrottle.push(w);
    const d = drag.current;
    if (!d || d.pointer !== e.pointerId) return;
    const dist = Math.hypot(p.x - d.sx, p.y - d.sy);
    if (d.kind === "pan") {
      if (!d.moved && dist < DRAG_THRESHOLD_PX) return;
      d.moved = true;
      setPanning(true);
      panThrottle.push(panBy(d.view, p.x - d.sx, p.y - d.sy));
    } else if (d.kind === "band") {
      d.cx = p.x;
      d.cy = p.y;
      setBand({ x0: d.sx, y0: d.sy, x1: p.x, y1: p.y });
    } else if (d.kind === "locked") {
      if (!d.warned && dist >= DRAG_THRESHOLD_PX) {
        d.warned = true;
        latest.current.onLockedDrag?.();
      }
    } else if (d.kind === "move") {
      if (!d.moved && dist < DRAG_THRESHOLD_PX) return;
      d.moved = true;
      const lead = d.origin.get(d.lead);
      if (!lead) return;
      let target = { x: lead.x + (w.x - d.start.x), y: lead.y + (w.y - d.start.y) };
      const mode = e.ctrlKey || e.metaKey ? "off" : latest.current.snap;
      if (mode !== "off") target = snapPoint(latest.current.layout.table, target, mode);
      const dx = target.x - lead.x;
      const dy = target.y - lead.y;
      const positions = new Map<string, Vec>();
      for (const [id, o] of d.origin) positions.set(id, { x: o.x + dx, y: o.y + dy });
      lastMove.current = positions;
      moveThrottle.push(positions);
    }
  };

  const onUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d || d.pointer !== e.pointerId) return;
    const cur = latest.current;
    const click = pendingToolClick.current;
    if (d.kind === "band") {
      const v = cur.view;
      if (v) {
        const a = toWorld(v, Math.min(d.sx, d.cx), Math.max(d.sy, d.cy));
        const b = toWorld(v, Math.max(d.sx, d.cx), Math.min(d.sy, d.cy));
        if (Math.hypot(d.cx - d.sx, d.cy - d.sy) >= DRAG_THRESHOLD_PX) {
          const inside = cur.layout.components.filter((c) => c.x_mm >= a.x && c.x_mm <= b.x && c.y_mm >= a.y && c.y_mm <= b.y).map((c) => c.id);
          cur.onSelect(d.additive ? [...new Set([...cur.selected, ...inside])] : inside);
        }
      }
      endDrag(false);
      return;
    }
    if (d.kind === "pan") {
      const moved = d.moved;
      endDrag(true);
      if (!moved && click) {
        if (cur.tool === "select") {
          const k = cur.view?.k;
          if (!(k && cur.onBackgroundClick?.(click.world, k))) cur.onSelect([]);
        } else cur.onToolPoint(click);
      }
      return;
    }
    if (d.kind === "locked") {
      endDrag(false);
      if (click && cur.tool !== "select") cur.onToolPoint(click);
      return;
    }
    // move
    const moved = d.moved;
    const id = d.clicked;
    endDrag(true);
    if (!moved && !d.additive && cur.selected.size > 1 && cur.selected.has(id)) cur.onSelect([id]);
  };

  const onCancel = () => endDrag(false);

  // ── drop from the palette ──
  const onDragOver = (e: ReactDragEvent<HTMLDivElement>) => {
    if (readOnly || !(e.dataTransfer.types.includes(PALETTE_MIME) || e.dataTransfer.types.includes(TEMPLATE_MIME))) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  };
  const onDrop = (e: ReactDragEvent<HTMLDivElement>) => {
    if (readOnly || !view) return;
    const template = e.dataTransfer.getData(TEMPLATE_MIME);
    if (template && props.onDropTemplate) {
      e.preventDefault();
      const p = localPoint(e);
      let w = toWorld(view, p.x, p.y);
      if (snap !== "off") w = snapPoint(layout.table, w, snap);
      props.onDropTemplate(template, w);
      return;
    }
    const type = e.dataTransfer.getData(PALETTE_MIME);
    if (!isComponentType(type)) return;
    e.preventDefault();
    const p = localPoint(e);
    let w = toWorld(view, p.x, p.y);
    if (snap !== "off") w = snapPoint(layout.table, w, snap);
    props.onDropType(type, w);
  };

  // ── read-out ──
  const hole = cursor ? nearestHole(layout.table, cursor) : null;
  const k = view?.k ?? 1;

  // ── the beam under the pointer: its power, spot and polarisation right there (section 8.4) ──
  // Not while something is being dragged, panned or boxed, not over a component (its own glow says what is
  // under the pointer), only with the select tool, and only over beams that are drawn (the Beams layer).
  const busy = panning || band !== null || (drag.current !== null && drag.current.kind === "move" && drag.current.moved);
  const beamsShown = display.beams;
  const beamHover: SegmentHit | null = useMemo(
    () => (cursor && view && beamsShown && tool === "select" && !busy && hover === null ? nearestSegment(trace, cursor, BEAM_PICK_PX / view.k) : null),
    [cursor, view, beamsShown, tool, busy, hover, trace],
  );

  const cursorStyle = panning ? "grabbing" : tool === "select" ? "default" : "crosshair";

  return (
    <div ref={boxRef} className="relative h-full min-h-[320px] w-full overflow-hidden rounded-mast-card border border-mast-border bg-mast-bg">
      <div
        ref={svgHost}
        className="absolute inset-0 touch-none select-none"
        style={{ cursor: cursorStyle }}
        onPointerDown={onBackgroundDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onCancel}
        onPointerLeave={() => cursorThrottle.push(null)}
        onContextMenu={(e) => e.preventDefault()}
        onDragOver={onDragOver}
        onDrop={onDrop}
      >
        {view && size.w > 0 && (
          <LayoutSvg
            layout={layout}
            trace={trace}
            indicators={indicators}
            nearMisses={nearMisses}
            view={view}
            width={size.w}
            height={size.h}
            theme={theme}
            display={display}
            selected={selected}
            hover={hover}
            commentCounts={commentCounts}
            handlers={handlers}
            beamPick={props.beamPick}
          />
        )}
        {view && size.w > 0 && (
          <svg width={size.w} height={size.h} className="pointer-events-none absolute left-0 top-0" aria-hidden="true">
            {renderOverlay?.({ view, cursor })}
            {band && (
              <rect
                x={Math.min(band.x0, band.x1)}
                y={Math.min(band.y0, band.y1)}
                width={Math.abs(band.x1 - band.x0)}
                height={Math.abs(band.y1 - band.y0)}
                fill="var(--mast-accent-soft)"
                stroke="var(--mast-accent)"
                strokeWidth={1}
                strokeDasharray="4 3"
              />
            )}
          </svg>
        )}
      </div>

      {/* view controls */}
      <div className="absolute right-2 top-2 flex flex-col overflow-hidden rounded-mast-ctl border border-mast-border bg-mast-panel text-sm shadow-mast" role="group" aria-label={tr("视图", "View")}>
        <button type="button" className="px-2.5 py-1 text-mast-text hover:bg-mast-panel-2" title={tr("放大", "Zoom in")} aria-label={tr("放大", "Zoom in")} onClick={() => setView((v) => (v ? zoomAt(v, 1.25, size.w / 2, size.h / 2) : v))}>
          +
        </button>
        <button type="button" className="border-t border-mast-border px-2.5 py-1 text-mast-text hover:bg-mast-panel-2" title={tr("缩小", "Zoom out")} aria-label={tr("缩小", "Zoom out")} onClick={() => setView((v) => (v ? zoomAt(v, 0.8, size.w / 2, size.h / 2) : v))}>
          −
        </button>
        <button type="button" className="border-t border-mast-border px-2 py-1 text-xs text-mast-text hover:bg-mast-panel-2" title={tr("显示整张台面", "Fit the whole table")} onClick={() => setView(fitNow())}>
          {tr("适应", "Fit")}
        </button>
      </div>

      {beamHover && view && <BeamTooltip hit={beamHover} trace={trace} layout={layout} view={view} theme={theme} width={size.w} height={size.h} />}

      {/* read-out */}
      <div className="pointer-events-none absolute bottom-5 left-9 right-2 flex flex-wrap items-center gap-x-4 gap-y-0.5 font-mono text-[11px] text-mast-muted" aria-live="off">
        <span className="rounded px-1" style={READOUT_BG}>
          {cursor ? `x ${cursor.x.toFixed(2)}  y ${cursor.y.toFixed(2)} mm` : "x —  y —"}
        </span>
        {hole && (
          <span className="rounded px-1" style={READOUT_BG}>
            {tr(
              `最近的孔：第 ${hole.row} 行、第 ${hole.col} 列，偏 Δx ${hole.dx.toFixed(2)}、Δy ${hole.dy.toFixed(2)} mm${hole.inside ? "" : "（台面外）"}`,
              `nearest hole: row ${hole.row}, col ${hole.col}, off Δx ${hole.dx.toFixed(2)}, Δy ${hole.dy.toFixed(2)} mm${hole.inside ? "" : " (outside the table)"}`,
            )}
          </span>
        )}
        <span className="rounded px-1" style={READOUT_BG}>{tr(`比例 ${formatLength(1 / k)}/px`, `scale ${formatLength(1 / k)}/px`)}</span>
      </div>
    </div>
  );
}

/** The tooltip of the beam under the pointer: which beam, then power, spot and polarisation at that very point. */
function BeamTooltip({ hit, trace, layout, view, theme, width, height }: { hit: SegmentHit; trace: DetailedTrace; layout: OpticalLayout; view: View; theme: ThemeName; width: number; height: number }) {
  const tree = trace[hit.tree];
  const seg = tree?.branches.find((b) => b.branch === hit.branch)?.segments[hit.segment];
  if (!tree || !seg) return null;
  const here = beamAt(tree, seg, hit.along);
  const name = layout.components.find((c) => c.id === tree.source)?.label || tree.source;
  const p = toScreen(view, hit.at);
  // beside the pointer, turned back inwards near the right and bottom edges
  const left = p.x + 230 > width ? p.x - 14 - 220 : p.x + 14;
  const top = p.y + 90 > height ? p.y - 14 - 70 : p.y + 14;
  return (
    <div className="pointer-events-none absolute z-10 max-w-[220px] rounded-mast-ctl border border-mast-border bg-mast-panel px-2 py-1 text-xs shadow-mast" style={{ left, top }} role="tooltip" data-beam-tooltip="">
      <div className="flex items-center gap-1.5 font-semibold text-mast-text">
        <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: wavelengthColor(tree.wavelength_nm, theme) }} />
        <span className="truncate">{name}</span>
        <span className="font-mono font-normal text-mast-muted">{wavelengthLabel(tree.wavelength_nm)}</span>
      </div>
      {here ? (
        <div className="font-mono text-mast-muted">
          <div>
            {powerText(here.p, here.mw)}
            {here.d_mm !== null && ` · ${spotText(here.d_mm)}`}
          </div>
          <div className="font-sans">{polarizationText(here.s)}</div>
        </div>
      ) : (
        <div className="text-mast-faint">{tr("这条追迹没有光束数据", "this trace has no beam data")}</div>
      )}
    </div>
  );
}
