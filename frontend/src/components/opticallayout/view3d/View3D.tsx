import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent as ReactDragEvent, type MutableRefObject, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import type { ComponentType, OpticalLayout, Vec } from "@/lib/opticalLayout/types";
import { isComponentType } from "@/lib/opticalLayout/types";
import type { DetailedTrace } from "@/lib/opticalLayout/trace";
import type { ThemeName } from "@/lib/opticalLayout/colors";
import type { SegRef } from "@/lib/opticalLayout/beamDraw";
import type { Layers } from "@/lib/opticalLayout/layers";
import { snapPoint, type SnapMode } from "@/lib/opticalLayout/geometry";
import { createFrameThrottle } from "@/lib/opticalLayout/frame";
import { tableBeamHeight } from "@/lib/opticalLayout/scene3d/heights";
import { DRAG_THRESHOLD_PX, dragPositions, dragSnapMode, selectionOnClick, selectionOnPress, startDrag, type Drag3 } from "@/lib/opticalLayout/scene3d/pick";
import { VIEW_PRESETS, type ViewPreset } from "@/lib/opticalLayout/scene3d/views";
import { view3dKey } from "@/lib/opticalLayout/scene3d/keys3d";
import { isEditableTarget } from "@/lib/opticalLayout/keys";
import { export3dFileName, isSoftwareRenderer, probeWebgl } from "@/lib/opticalLayout/scene3d/files";
import { BEAM_PICK_PX, PALETTE_MIME, TEMPLATE_MIME, type CanvasFocus, type CanvasTool, type ToolPoint } from "../LayoutCanvas";
import { Engine, type Projection } from "./engine";
import { PNG_SCALES, disposeExportScene, exportScene, glbOf } from "./exporters";

// The 3D view of the layout editor (docs/v2/design/optical_layout_3d_view.md). It takes the place of the 2D
// canvas and the same props: the same layout, the same trace, the same selection, layers and snap mode. Clicking a
// component selects it (the inspector follows); dragging moves it across the table, previewed through the same
// frame throttle and committed as ONE undo step, exactly as on the 2D canvas; the editor's keys (arrows, [ ],
// Delete, Ctrl+Z) act on the selection whichever view is shown. The 2D data model stays the only source: this
// view never writes anything the 2D canvas could not.
//
// Loaded on demand (LayoutEditor: React.lazy), so three.js is fetched the first time somebody opens the 3D view.

export interface View3DProps {
  layout: OpticalLayout;
  trace: DetailedTrace;
  display: Layers;
  theme: ThemeName;
  selected: ReadonlySet<string>;
  commentCounts?: ReadonlyMap<string, number>;
  snap: SnapMode;
  tool: CanvasTool;
  readOnly: boolean;
  fitNonce: number;
  focus?: CanvasFocus | null;
  centerRef?: MutableRefObject<Vec | null>;
  onSelect: (ids: string[]) => void;
  onPreview: (positions: ReadonlyMap<string, Vec> | null) => void;
  onCommitMove: (positions: ReadonlyMap<string, Vec>) => void;
  onDropType: (type: ComponentType, at: Vec) => void;
  onDropTemplate?: (id: string, at: Vec) => void;
  onToolPoint: (p: ToolPoint) => void;
  onLockedDrag?: () => void;
  beamPick?: SegRef | null;
  onBackgroundClick?: (world: Vec, k: number) => boolean;
  /** back to the 2D view (the button of the message when 3D cannot be drawn) */
  onExit: () => void;
  /** whether the layout has unsaved edits (the export file names say so) */
  dirty: boolean;
  notify?: (tone: "ok" | "info" | "warn" | "err", text: string) => void;
}

/** "no-webgl": the browser has no WebGL 2; "error": making the renderer failed. A lost context is shown over the view (Viewport). */
type Failure = "no-webgl" | "error";

export default function View3D(props: View3DProps) {
  const [probe] = useState(() => probeWebgl(() => document.createElement("canvas")));
  const [failure, setFailure] = useState<Failure | null>(probe.ok ? null : "no-webgl");
  const [attempt, setAttempt] = useState(0);
  if (failure) return <NoView failure={failure} onExit={props.onExit} onRetry={failure === "no-webgl" ? null : () => (setFailure(null), setAttempt((a) => a + 1))} />;
  return <Viewport key={attempt} {...props} low={drawCheaply(probe.renderer)} onFail={setFailure} onReload={() => setAttempt((a) => a + 1)} />;
}

/**
 * The cheap settings for a software renderer (files.ts isSoftwareRenderer); localStorage "mast.ol3d.quality" set to
 * "high" or "low" overrides the guess (for a fast machine whose driver hides its name, or a slow one that does not).
 */
function drawCheaply(renderer: string): boolean {
  let forced: string | null = null;
  try {
    forced = window.localStorage.getItem("mast.ol3d.quality");
  } catch {
    forced = null;
  }
  return forced === "low" ? true : forced === "high" ? false : isSoftwareRenderer(renderer);
}

function NoView({ failure, onExit, onRetry }: { failure: Failure; onExit: () => void; onRetry: (() => void) | null }) {
  const text =
    failure === "no-webgl"
      ? tr("这个浏览器画不了 3D 视图（需要 WebGL 2；远程桌面、没有显卡驱动或关掉了硬件加速时会这样）。2D 视图照常可用。", "This browser cannot draw the 3D view (it needs WebGL 2, which a remote desktop, a missing graphics driver or switched-off hardware acceleration can take away). The 2D view works as usual.")
      : tr("3D 视图出错了。可以重试，或回到 2D 视图。", "The 3D view ran into an error. Try again, or go back to the 2D view.");
  return (
    <div role="alert" data-view3d-unavailable={failure} className="flex h-full min-h-[320px] w-full flex-col items-center justify-center gap-3 rounded-mast-card border border-mast-border bg-mast-panel p-6 text-center text-sm text-mast-muted">
      <p className="max-w-lg">{text}</p>
      <div className="flex gap-2">
        {onRetry && (
          <button type="button" className="rounded-mast-ctl border border-mast-border-strong px-3 py-1 text-mast-text hover:bg-mast-panel-2" onClick={onRetry}>
            {tr("重试", "Try again")}
          </button>
        )}
        <button type="button" className="rounded-mast-ctl bg-mast-accent px-3 py-1 font-semibold text-mast-accent-ink hover:opacity-90" onClick={onExit}>
          {tr("回到 2D 视图", "Back to the 2D view")}
        </button>
      </div>
    </div>
  );
}

interface Press {
  pointer: number;
  x: number;
  y: number;
  moved: boolean;
  /** the component under the pointer when it went down */
  hit: string | null;
  additive: boolean;
  drag: Drag3 | null;
  /** a press on a locked component (or with another tool): never moves anything; says so once */
  locked: boolean;
  warned: boolean;
  /** what a click with the aim or measure tool hands to the editor */
  tool: ToolPoint | null;
}

const BTN = "px-2 py-1 text-xs text-mast-text hover:bg-mast-panel-2 disabled:opacity-50";

function Viewport(props: View3DProps & { low: boolean; onFail: (f: Failure) => void; onReload: () => void }) {
  const { layout, trace, display, theme, selected, commentCounts, readOnly, beamPick, fitNonce, focus } = props;
  const hostRef = useRef<HTMLDivElement | null>(null);
  const engineRef = useRef<Engine | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [projection, setProjection] = useState<Projection>("perspective");
  const [exportOpen, setExportOpen] = useState(false);
  const exportRef = useRef<HTMLDivElement | null>(null);
  const [busy, setBusy] = useState(false);
  /** the graphics context is gone until the browser gives it back (the canvas stays, with a message over it) */
  const [lost, setLost] = useState(false);
  const latest = useRef(props);
  latest.current = props;
  const press = useRef<Press | null>(null);
  const lastMove = useRef<ReadonlyMap<string, Vec> | null>(null);

  // ── the engine: made once, sized with its box ──
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let engine: Engine;
    try {
      engine = new Engine(
        host,
        latest.current.theme,
        {
          onContextLost: () => setLost(true),
          onContextRestored: () => setLost(false),
          onTarget: (x, y) => {
            const ref = latest.current.centerRef;
            if (ref) ref.current = { x, y };
          },
        },
        { low: props.low },
      );
    } catch {
      props.onFail("error");
      return;
    }
    engineRef.current = engine;
    const r = host.getBoundingClientRect();
    engine.setSize(r.width, r.height);
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver((entries) => {
      const e = entries[0];
      if (e) engine.setSize(e.contentRect.width, e.contentRect.height);
    });
    ro?.observe(host);
    const perf = readPerfFlag();
    if (perf) installPerfHook(engine);
    return () => {
      ro?.disconnect();
      if (perf) removePerfHook();
      engine.dispose();
      engineRef.current = null;
    };
    // the engine lives as long as this view; everything else reaches it through `latest`
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── what it shows ──
  useEffect(() => {
    engineRef.current?.update({ layout, trace, display, theme, selected, hover, beamPick: beamPick ?? null, commentCounts, interactive: !readOnly });
  }, [layout, trace, display, theme, selected, hover, beamPick, commentCounts, readOnly]);

  // ── fit and focus requests of the editor ──
  const fitSeen = useRef(fitNonce);
  useEffect(() => {
    if (fitSeen.current === fitNonce) return;
    fitSeen.current = fitNonce;
    engineRef.current?.fit();
  }, [fitNonce]);
  const focusSeen = useRef<number | null>(focus?.nonce ?? null);
  useEffect(() => {
    if (!focus || focusSeen.current === focus.nonce) return;
    focusSeen.current = focus.nonce;
    engineRef.current?.focusOn(focus.point.x, focus.point.y, tableBeamHeight(latest.current.layout.table), focus.center ? (focus.minZoom ?? 0) : 0);
  }, [focus]);

  // ── preview through a frame throttle, as the 2D canvas does (at most one trace per frame) ──
  const moveThrottle = useMemo(
    () => createFrameThrottle<ReadonlyMap<string, Vec>>((m) => latest.current.onPreview(m), (cb) => requestAnimationFrame(cb), (id) => cancelAnimationFrame(id)),
    [],
  );
  const hoverThrottle = useMemo(
    () =>
      createFrameThrottle<{ x: number; y: number }>(
        (p) => {
          const e = engineRef.current;
          if (!e || press.current) return;
          const hit = e.pickComponent(p.x, p.y);
          setHover((h) => (h === (hit?.id ?? null) ? h : (hit?.id ?? null)));
        },
        (cb) => requestAnimationFrame(cb),
        (id) => cancelAnimationFrame(id),
      ),
    [],
  );
  useEffect(
    () => () => {
      moveThrottle.cancel();
      hoverThrottle.cancel();
    },
    [moveThrottle, hoverThrottle],
  );

  const endPress = useCallback(
    (commit: boolean) => {
      const p = press.current;
      press.current = null;
      if (!p) return;
      if (p.drag) {
        moveThrottle.cancel();
        if (commit && p.moved && lastMove.current) latest.current.onCommitMove(lastMove.current);
        lastMove.current = null;
        latest.current.onPreview(null);
      }
    },
    [moveThrottle],
  );

  // Escape during a drag puts everything back (before the editor sees the key)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && press.current?.drag) {
        e.stopPropagation();
        endPress(false);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [endPress]);

  // the view keys (scene3d/keys3d.ts): 1 top, 2 front, 3 side, 4 isometric, 0 fit, 5 perspective / orthographic
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || isEditableTarget(e.target as HTMLElement | null)) return;
      const k = view3dKey(e);
      const eng = engineRef.current;
      if (!k || !eng) return;
      if (k.kind === "preset") eng.setView(k.preset);
      else if (k.kind === "fit") eng.fit();
      else setProjection((p) => (p === "perspective" ? "orthographic" : "perspective"));
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    engineRef.current?.setProjection(projection);
  }, [projection]);

  // the export menu closes on a click elsewhere, and on Escape (which it spends, as the toolbar's menus do, so the
  // editor does not also leave focus mode or clear the selection)
  useEffect(() => {
    if (!exportOpen) return;
    const onDown = (e: MouseEvent) => {
      if (exportRef.current && !exportRef.current.contains(e.target as Node)) setExportOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      setExportOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [exportOpen]);

  // ── the pointer ──
  // Captured on the box round the canvas, before OrbitControls (on the canvas) sees it: a press on a component
  // selects and drags it and never turns the view; a press on the empty table turns the view, and is a click
  // (pick the beam under it, or clear the selection) when it did not move.

  const toolPoint = (x: number, y: number, componentId: string | null): ToolPoint | null => {
    const e = engineRef.current;
    const h = tableBeamHeight(latest.current.layout.table);
    const p = e?.pointAtHeight(x, y, h) ?? null;
    if (!p && componentId) {
      const c = latest.current.layout.components.find((k) => k.id === componentId);
      return c ? { world: { x: c.x_mm, y: c.y_mm }, componentId } : null;
    }
    return p ? { world: { x: p[0], y: p[1] }, componentId } : null;
  };

  const onPointerDownCapture = (e: ReactPointerEvent<HTMLDivElement>) => {
    const eng = engineRef.current;
    if (!eng || e.button !== 0) return;
    const cur = latest.current;
    const hit = eng.pickComponent(e.clientX, e.clientY);
    const additive = e.shiftKey || e.ctrlKey || e.metaKey;
    const p: Press = { pointer: e.pointerId, x: e.clientX, y: e.clientY, moved: false, hit: hit?.id ?? null, additive, drag: null, locked: false, warned: true, tool: null };
    press.current = p;
    if (!hit) return; // the orbit controls take it
    // a press on a component never turns the view
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    // another tool, or an old revision: nothing is selected or moved (as on the 2D canvas); a tool gets the click
    if (cur.tool !== "select" || cur.readOnly) {
      p.locked = true;
      if (cur.tool !== "select") p.tool = toolPoint(e.clientX, e.clientY, hit.id);
      return;
    }
    const next = selectionOnPress([...cur.selected], hit.id, additive);
    cur.onSelect(next);
    if (additive) {
      p.locked = true;
      return;
    }
    const drag = startDrag(cur.layout, next, hit.id, { x: hit.point[0], y: hit.point[1] }, hit.point[2]);
    if (!drag) {
      p.locked = true;
      p.warned = false;
      return;
    }
    p.drag = drag;
  };

  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const p = press.current;
    if (!p) {
      if (e.buttons === 0) hoverThrottle.push({ x: e.clientX, y: e.clientY });
      return;
    }
    if (p.pointer !== e.pointerId) return;
    if (!p.moved && Math.hypot(e.clientX - p.x, e.clientY - p.y) < DRAG_THRESHOLD_PX) return;
    p.moved = true;
    if (p.locked && !p.warned) {
      p.warned = true;
      latest.current.onLockedDrag?.();
    }
    const d = p.drag;
    const eng = engineRef.current;
    if (!d || !eng) return;
    const at = eng.pointAtHeight(e.clientX, e.clientY, d.planeZ);
    if (!at) return;
    const positions = dragPositions(d, { x: at[0], y: at[1] }, latest.current.layout.table, dragSnapMode(latest.current.snap, e));
    lastMove.current = positions;
    moveThrottle.push(positions);
  };

  const onPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const p = press.current;
    if (!p || p.pointer !== e.pointerId) return;
    const cur = latest.current;
    const moved = p.moved;
    endPress(true);
    if (moved) return;
    if (p.hit) {
      if (p.tool) cur.onToolPoint(p.tool);
      else if (cur.tool === "select") {
        const only = selectionOnClick([...cur.selected], p.hit, p.additive);
        if (only) cur.onSelect(only);
      }
      return;
    }
    // a click on the empty view
    if (cur.tool !== "select") {
      const tp = toolPoint(e.clientX, e.clientY, null);
      if (tp) cur.onToolPoint(tp);
      return;
    }
    const eng = engineRef.current;
    const beam = cur.display.beams ? eng?.pickBeam(e.clientX, e.clientY, BEAM_PICK_PX) : null;
    // the editor picks the beam through the 2D rule (nearest segment within BEAM_PICK_PX / k mm): handing it the very
    // point on the beam and a tolerance of a hundredth of a millimetre makes that the segment that was clicked
    const picked = beam ? (cur.onBackgroundClick?.({ x: beam.point[0], y: beam.point[1] }, BEAM_PICK_PX / 0.01) ?? false) : (cur.onBackgroundClick?.({ x: Number.MAX_SAFE_INTEGER, y: Number.MAX_SAFE_INTEGER }, 1) ?? false);
    if (!picked) cur.onSelect([]);
  };

  const onPointerCancel = () => endPress(false);

  // ── drops from the palette, onto the table top ──
  const dropPoint = (e: ReactDragEvent<HTMLDivElement>): Vec | null => {
    const at = engineRef.current?.pointAtHeight(e.clientX, e.clientY, 0);
    if (!at) return null;
    const cur = latest.current;
    const w = { x: at[0], y: at[1] };
    return cur.snap !== "off" ? snapPoint(cur.layout.table, w, cur.snap) : w;
  };
  const onDragOver = (e: ReactDragEvent<HTMLDivElement>) => {
    if (readOnly || !(e.dataTransfer.types.includes(PALETTE_MIME) || e.dataTransfer.types.includes(TEMPLATE_MIME))) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  };
  const onDrop = (e: ReactDragEvent<HTMLDivElement>) => {
    if (readOnly) return;
    const template = e.dataTransfer.getData(TEMPLATE_MIME);
    const type = e.dataTransfer.getData(PALETTE_MIME);
    const w = dropPoint(e);
    if (!w) return;
    if (template && props.onDropTemplate) {
      e.preventDefault();
      props.onDropTemplate(template, w);
    } else if (isComponentType(type)) {
      e.preventDefault();
      props.onDropType(type, w);
    }
  };

  // ── exports ──
  const download = (blob: Blob, name: string) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 2000);
  };
  const exportPng = async (scale: number) => {
    const eng = engineRef.current;
    if (!eng || busy) return;
    setBusy(true);
    try {
      const { blob, scale: s } = await eng.snapshot(scale);
      download(blob, export3dFileName(latest.current.layout, "png", latest.current.dirty, s));
      latest.current.notify?.(s < scale ? "warn" : "ok", s < scale ? tr(`已导出 3D PNG（显卡最多只能画到 ${s}×）。`, `3D PNG exported (the graphics card goes up to ${s}× only).`) : tr(`已导出 3D PNG（${s}×）。`, `3D PNG exported (${s}×).`));
    } catch (err) {
      latest.current.notify?.("err", tr(`3D PNG 导出失败：${(err as Error).message}`, `3D PNG export failed: ${(err as Error).message}`));
    } finally {
      setBusy(false);
    }
  };
  const exportGlb = async (instancing: boolean) => {
    if (busy) return;
    setBusy(true);
    const cur = latest.current;
    const root = exportScene(cur.layout, cur.trace, cur.display, cur.theme, { instancing });
    try {
      const glb = await glbOf(root);
      download(new Blob([glb], { type: "model/gltf-binary" }), export3dFileName(cur.layout, "glb", cur.dirty, 1, instancing ? "-instanced" : ""));
      cur.notify?.(
        "ok",
        instancing
          ? tr("已导出 GLB（保留实例，EXT_mesh_gpu_instancing；单位米，Y 轴向上）。", "GLB exported (instances kept, EXT_mesh_gpu_instancing; metres, Y up).")
          : tr("已导出 GLB（单位米，Y 轴向上；Blender 里按真实尺寸打开，每个器件一个对象）。", "GLB exported (metres, Y up; Blender opens it at its real size, one object per component)."),
      );
    } catch (err) {
      cur.notify?.("err", tr(`GLB 导出失败：${(err as Error).message}`, `GLB export failed: ${(err as Error).message}`));
    } finally {
      disposeExportScene(root);
      setBusy(false);
    }
  };

  const hoverComp = hover ? layout.components.find((c) => c.id === hover) : undefined;
  const cursor = press.current?.drag?.origin.size ? "grabbing" : hoverComp ? (hoverComp.locked || readOnly || props.tool !== "select" ? "pointer" : "move") : "grab";

  return (
    <div className="relative h-full min-h-[320px] w-full overflow-hidden rounded-mast-card border border-mast-border bg-mast-bg" data-view3d="">
      {lost && (
        <div role="alert" data-view3d-context-lost="" className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 bg-mast-panel p-6 text-center text-sm text-mast-muted">
          <p className="max-w-lg">{tr("3D 视图的图形上下文丢失了（显卡驱动重置或同时打开了太多 3D 页面），正在等浏览器恢复。也可以重新载入，或回到 2D 视图。", "The 3D view lost its graphics context (a driver reset, or too many 3D pages at once) and is waiting for the browser to give it back. You can also reload it, or go back to the 2D view.")}</p>
          <div className="flex gap-2">
            <button type="button" className="rounded-mast-ctl border border-mast-border-strong px-3 py-1 text-mast-text hover:bg-mast-panel-2" onClick={props.onReload}>
              {tr("重新载入 3D", "Reload the 3D view")}
            </button>
            <button type="button" className="rounded-mast-ctl bg-mast-accent px-3 py-1 font-semibold text-mast-accent-ink hover:opacity-90" onClick={props.onExit}>
              {tr("回到 2D 视图", "Back to the 2D view")}
            </button>
          </div>
        </div>
      )}
      <div
        ref={hostRef}
        className="absolute inset-0 touch-none select-none"
        style={{ cursor }}
        onPointerDownCapture={onPointerDownCapture}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerCancel}
        onPointerLeave={() => setHover(null)}
        onContextMenu={(e) => e.preventDefault()}
        onDragOver={onDragOver}
        onDrop={onDrop}
      />

      {/* view controls */}
      <div className="absolute right-2 top-2 flex flex-col items-end gap-1.5" data-view3d-controls="">
        <div className="flex overflow-hidden rounded-mast-ctl border border-mast-border bg-mast-panel shadow-mast" role="group" aria-label={tr("视角", "View")}>
          {VIEW_PRESETS.map((v, i) => (
            <button key={v} type="button" className={clsx(BTN, i > 0 && "border-l border-mast-border")} title={`${presetLabel(v)} (${i + 1})`} onClick={() => engineRef.current?.setView(v)}>
              {presetLabel(v)}
            </button>
          ))}
          <button type="button" className={clsx(BTN, "border-l border-mast-border")} title={tr("显示全部 (0)", "Fit everything (0)")} onClick={() => engineRef.current?.fit()}>
            {tr("适应", "Fit")}
          </button>
        </div>
        <div className="flex overflow-hidden rounded-mast-ctl border border-mast-border bg-mast-panel shadow-mast" role="group" aria-label={tr("投影", "Projection")}>
          {(["perspective", "orthographic"] as const).map((p, i) => (
            <button key={p} type="button" aria-pressed={projection === p} className={clsx(BTN, i > 0 && "border-l border-mast-border", projection === p && "font-semibold text-mast-accent")} title={tr("透视 / 正交 (5)", "Perspective / orthographic (5)")} onClick={() => setProjection(p)}>
              {p === "perspective" ? tr("透视", "Persp.") : tr("正交", "Ortho.")}
            </button>
          ))}
        </div>
        <div className="relative" ref={exportRef}>
          <button type="button" aria-haspopup="menu" aria-expanded={exportOpen} disabled={busy} className={clsx(BTN, "rounded-mast-ctl border border-mast-border bg-mast-panel shadow-mast")} onClick={() => setExportOpen((o) => !o)}>
            {busy ? tr("导出中…", "Exporting…") : tr("导出 3D ▾", "Export 3D ▾")}
          </button>
          {exportOpen && (
            <div role="menu" className="absolute right-0 top-full z-30 mt-1 min-w-[12rem] rounded-mast-ctl border border-mast-border bg-mast-panel p-1.5 shadow-mast" onClick={() => setExportOpen(false)}>
              {PNG_SCALES.map((s) => (
                <MenuButton key={s} onClick={() => void exportPng(s)}>
                  {tr(`PNG（${s}× 分辨率）`, `PNG (${s}× resolution)`)}
                </MenuButton>
              ))}
              <div className="my-1 border-t border-mast-border" />
              <MenuButton onClick={() => void exportGlb(false)} title={tr("glTF 二进制，给 Blender / CAD 用：每个器件一个对象，任何读取器都能打开", "glTF binary, for Blender or CAD: one object per component, opens in any reader")}>
                GLB (glTF)
              </MenuButton>
              <MenuButton
                onClick={() => void exportGlb(true)}
                title={tr("文件更小：同样的零件存一次、按实例摆放（EXT_mesh_gpu_instancing），但不是每个导入器都认，器件也不再是单独的对象", "Smaller: each part is stored once and placed by instances (EXT_mesh_gpu_instancing), but not every importer reads it and the components are no longer objects of their own")}
              >
                {tr("GLB（保留实例）", "GLB (instances kept)")}
              </MenuButton>
            </div>
          )}
        </div>
      </div>

      <div className="pointer-events-none absolute bottom-2 left-2 right-2 font-mono text-[11px] text-mast-muted" aria-live="off">
        <span className="rounded px-1" style={{ background: "color-mix(in srgb, var(--mast-panel) 82%, transparent)" }}>
          {tr("左键拖空白处旋转，右键平移，滚轮缩放；拖器件沿台面移动；1 俯 2 正 3 侧 4 等轴 0 适应 5 透视/正交", "Drag empty space to orbit, right-drag to pan, wheel to zoom; drag a component to move it on the table; 1 top 2 front 3 side 4 iso 0 fit 5 persp./ortho.")}
        </span>
      </div>
    </div>
  );
}

function MenuButton({ onClick, children, title }: { onClick: () => void; children: ReactNode; title?: string }) {
  return (
    <button type="button" role="menuitem" title={title} className="block w-full rounded px-2 py-1.5 text-left text-sm text-mast-text hover:bg-mast-panel-2" onClick={onClick}>
      {children}
    </button>
  );
}

function presetLabel(v: ViewPreset): string {
  switch (v) {
    case "top":
      return tr("俯视", "Top");
    case "front":
      return tr("正视", "Front");
    case "side":
      return tr("侧视", "Side");
    case "iso":
      return tr("等轴", "Iso");
  }
}

// ── measuring (only when localStorage "mast.ol3d.perf" is "1"; see docs/v2/design/optical_layout_3d_view.md) ──

const PERF_KEY = "mast.ol3d.perf";

function readPerfFlag(): boolean {
  try {
    return window.localStorage.getItem(PERF_KEY) === "1";
  } catch {
    return false;
  }
}

interface SpinResult {
  intervals: number[];
  work: number[];
  calls: number;
  triangles: number;
  instances: number;
}

function installPerfHook(engine: Engine): void {
  (window as unknown as { __ol3d?: unknown }).__ol3d = {
    /** turns the camera `deg` degrees about the vertical through the target every frame, `frames` times */
    spin(frames = 240, deg = 1.5): Promise<SpinResult> {
      return new Promise((resolve) => {
        const intervals: number[] = [];
        engine.frameWork = [];
        let last = 0;
        let n = 0;
        const step = (now: number) => {
          if (n > 0) intervals.push(now - last);
          last = now;
          if (n++ >= frames) {
            const work = engine.frameWork ?? [];
            engine.frameWork = null;
            const info = engine.renderer.info.render;
            resolve({ intervals, work, calls: info.calls, triangles: info.triangles, instances: engine.content.nodes.reduce((s, x) => s + x.parts.length, 0) + engine.content.tubes.length });
            return;
          }
          const t = engine.controls.target;
          const p = engine.camera.position;
          const a = (deg * Math.PI) / 180;
          const dx = p.x - t.x;
          const dy = p.y - t.y;
          p.x = t.x + dx * Math.cos(a) - dy * Math.sin(a);
          p.y = t.y + dx * Math.sin(a) + dy * Math.cos(a);
          engine.camera.lookAt(t);
          engine.requestRender();
          requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
      });
    },
    view(preset: ViewPreset) {
      engine.setView(preset);
    },
    /** where a component's centre is in the page (for a test to click it) */
    screenOf(id: string) {
      return engine.screenOf(id);
    },
    /** where the middle of the longest drawn beam segment is in the page, and which segment it is */
    beamPoint() {
      let best: { t: (typeof engine.content.tubes)[number]; len: number } | null = null;
      for (const t of engine.content.tubes) {
        const len = Math.hypot(t.b[0] - t.a[0], t.b[1] - t.a[1], t.b[2] - t.a[2]);
        if (!t.joint && !t.dark && (!best || len > best.len)) best = { t, len };
      }
      if (!best) return null;
      const { a, b, ref } = best.t;
      const at = engine.pageOf([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2]);
      return at ? { ...at, ref } : null;
    },
    /** what the renderer is (SwiftShader or a GPU) */
    renderer(): string {
      const gl = engine.renderer.getContext();
      const ext = gl.getExtension("WEBGL_debug_renderer_info");
      return String(ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER));
    },
    /** what the renderer holds on the GPU now (three's own count), and whether it draws cheaply */
    memory() {
      const info = engine.renderer.info;
      return { geometries: info.memory.geometries, textures: info.memory.textures, programs: info.programs?.length ?? 0, low: engine.options.low };
    },
  };
}

function removePerfHook(): void {
  delete (window as unknown as { __ol3d?: unknown }).__ol3d;
}
