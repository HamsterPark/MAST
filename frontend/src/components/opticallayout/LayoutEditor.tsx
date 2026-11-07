import { useCallback, useEffect, useMemo, useReducer, useRef, useState, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import { useUiStore } from "@/store";
import { Badge, ErrorNote } from "@/components/ui";
import { OperatorNameField } from "@/components/operator/OperatorNameField";
import { useOperatorName } from "@/hooks/useOperatorPrefs";
import { useNow } from "@/hooks/useNow";
import { useQueryClient } from "@tanstack/react-query";
import { LAYOUTS_KEY, layoutApi, useDeviceOptions, useLayoutComments, useRevisionLayout, useSaveLayout, useSlotOptions } from "@/api/opticalLayoutHooks";
import { LayoutApiError, PngUnavailableError } from "@/api/opticalLayout";
import type { ComponentType, LinksState, OpticalLayout, Vec } from "@/lib/opticalLayout/types";
import { cutPoint, traceLayoutDetailed, truncation } from "@/lib/opticalLayout/trace";
import { componentLevel, diagnostics, hitIndicators, shownNearMisses, traceCutMessage, type Diagnostic } from "@/lib/opticalLayout/indicators";
import { editorReducer, initialEditorState, isDirty } from "@/lib/opticalLayout/editorState";
import { canRedo, canUndo } from "@/lib/opticalLayout/history";
import {
  addComponent,
  addRefusal,
  addText,
  duplicateComponents,
  getComponent,
  lockedAmong,
  moveBy,
  removeComponents,
  rotateBy,
  rotateGroup,
  setPositions,
} from "@/lib/opticalLayout/edit";
import { aimErrorText, aimMirror, beamPathBetween, isAimable, measure, placeAlongBeam, putOnBeam, solveAim, strongestHit } from "@/lib/opticalLayout/align";
import { eventsByComponent } from "@/lib/opticalLayout/trace";
import { layoutBounds, snapPoint, type SnapMode } from "@/lib/opticalLayout/geometry";
import { interpretKey, isEditableTarget } from "@/lib/opticalLayout/keys";
import { errorsOf, validateLayout } from "@/lib/opticalLayout/validate";
import { classifySaveError, saveFailureText, suggestCopyId, withOperator } from "@/lib/opticalLayout/saveFlow";
import { layoutForWrite, openCommentCount } from "@/lib/opticalLayout/parse";
import { exportFileName, resolveCssVars, standaloneSvg } from "@/lib/opticalLayout/exportSvg";
import { toScreen, type View } from "@/lib/opticalLayout/view";
import { typeLabel } from "@/lib/opticalLayout/catalog";
import { LayoutCanvas, type CanvasFocus, type CanvasTool, type ToolPoint } from "./LayoutCanvas";
import { DEFAULT_DISPLAY, LayoutSvg, type DisplayOptions } from "./LayoutSvg";
import { Palette } from "./Palette";
import { Toolbar } from "./Toolbar";
import { ComponentForm, GroupForm, type InspectorCtx } from "./ComponentForm";
import { LayoutForm } from "./LayoutForm";
import { CommentsPanel } from "./CommentsPanel";
import { HistoryPanel } from "./HistoryPanel";
import { CheckPanel } from "./CheckPanel";
import { ConflictBanner, SaveAsDialog } from "./LayoutDialogs";

// The editor of one layout: the canvas in the middle, the palette on the left, the
// inspector / comments / history / beam check on the right. All state of the drawing is
// one reducer (lib/opticalLayout/editorState.ts); this component wires it to the pointer,
// the keyboard and the server, and decides nothing the lib could decide.

export interface LayoutEditorProps {
  initial: OpticalLayout;
  linksState: LinksState;
  /** ids of the layouts on the server, for "save as new" */
  taken: ReadonlySet<string>;
  onDirtyChange: (dirty: boolean) => void;
  /** a copy was saved under a new id: open it */
  onSwitchTo: (id: string) => void;
}

type Tab = "inspector" | "comments" | "history" | "check";
type Notice = { tone: "ok" | "info" | "warn" | "err"; text: string };

const SNAP_KEY = "mast.opticalLayout.snap";
const DISPLAY_KEY = "mast.opticalLayout.display";

function readStored<T>(key: string, fallback: T, valid: (v: unknown) => v is T): T {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return fallback;
    const v = JSON.parse(raw) as unknown;
    return valid(v) ? v : fallback;
  } catch {
    return fallback;
  }
}
function writeStored(key: string, v: unknown) {
  try {
    window.localStorage.setItem(key, JSON.stringify(v));
  } catch {
    /* storage off: the choice holds for this page load */
  }
}
const isSnap = (v: unknown): v is SnapMode => v === "off" || v === "hole" || v === "half" || v === "mm";
const isDisplay = (v: unknown): v is DisplayOptions =>
  typeof v === "object" && v !== null && (Object.keys(DEFAULT_DISPLAY) as (keyof DisplayOptions)[]).every((k) => typeof (v as Record<string, unknown>)[k] === "boolean");

function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

const TAB_BTN = "relative -mb-px whitespace-nowrap border-b-2 px-2.5 py-1.5 text-sm";
/** pixels per millimetre the view is zoomed in to (at least) when it is moved to where the trace stopped */
const CUT_ZOOM = 8;

export function LayoutEditor({ initial, linksState, taken, onDirtyChange, onSwitchTo }: LayoutEditorProps) {
  const [state, dispatch] = useReducer(editorReducer, initial, initialEditorState);
  const present = state.history.present;
  const dirty = isDirty(state);
  const saved = state.baseRevision > 0;
  const theme = useUiStore((s) => s.theme);
  const [operator] = useOperatorName();
  const nowMs = useNow(30_000);

  const [selection, setSelection] = useState<string[]>([]);
  const [tool, setTool] = useState<CanvasTool>("select");
  const [snap, setSnap] = useState<SnapMode>(() => readStored(SNAP_KEY, "hole" as SnapMode, isSnap));
  const [display, setDisplay] = useState<DisplayOptions>(() => ({ ...DEFAULT_DISPLAY, ...readStored(DISPLAY_KEY, DEFAULT_DISPLAY, isDisplay) }));
  const [preview, setPreview] = useState<ReadonlyMap<string, Vec> | null>(null);
  const [tab, setTab] = useState<Tab>("inspector");
  const [notice, setNotice] = useState<Notice | null>(null);
  const [viewingRev, setViewingRev] = useState<number | null>(null);
  const [focus, setFocus] = useState<CanvasFocus | null>(null);
  const [aimFrom, setAimFrom] = useState<string | null>(null);
  const [measureA, setMeasureA] = useState<ToolPoint | null>(null);
  const [measureB, setMeasureB] = useState<ToolPoint | null>(null);
  const [saveAsOpen, setSaveAsOpen] = useState(false);
  const [saveAsError, setSaveAsError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [linksSnapshot, setLinksSnapshot] = useState<LinksState>(linksState);
  const centerRef = useRef<Vec | null>(null);

  const qc = useQueryClient();
  /** the picker's list shows titles, revisions and comment counts: ask for it again after a write it does not know of */
  const refreshList = useCallback(() => void qc.invalidateQueries({ queryKey: LAYOUTS_KEY, exact: true }), [qc]);
  const save = useSaveLayout();
  const revision = useRevisionLayout(present.id, viewingRev);
  const commentsQ = useLayoutComments(saved ? present.id : null);
  const devices = useDeviceOptions(true);
  const slotInfo = useSlotOptions(true);

  const readOnly = viewingRev !== null;

  // ── what is shown: an old revision, a drag in progress, or the layout ──
  const shown = useMemo<OpticalLayout>(() => {
    if (viewingRev !== null && revision.data) return revision.data;
    return preview ? setPositions(present, preview) : present;
  }, [viewingRev, revision.data, preview, present]);
  const trace = useMemo(() => traceLayoutDetailed(shown), [shown]);
  /** the trace ran out of its budget of events: what it says about components no beam reaches cannot be trusted */
  const cut = useMemo(() => truncation(trace), [trace]);
  const indicators = useMemo(() => hitIndicators(shown, trace), [shown, trace]);
  const misses = useMemo(() => shownNearMisses(shown, trace, preview !== null), [shown, trace, preview]);
  const checks = useMemo<Diagnostic[]>(() => (preview ? [] : diagnostics(shown, trace, { indicators, misses })), [shown, trace, indicators, misses, preview]);
  const issues = useMemo(() => validateLayout(present), [present]);
  const problems = checks.filter((d) => d.level !== "ok").length + errorsOf(issues).length;

  const commentCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const c of commentsQ.data ?? []) if (!c.resolved && c.component_id) m.set(c.component_id, (m.get(c.component_id) ?? 0) + 1);
    return m;
  }, [commentsQ.data]);
  const openComments = openCommentCount(commentsQ.data ?? []);

  // ── bookkeeping ──
  const selectedSet = useMemo(() => new Set(selection), [selection]);
  const latest = useRef({ present, selection, tool, readOnly, trace, dirty, state });
  latest.current = { present, selection, tool, readOnly, trace, dirty, state };

  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(
    () => () => {
      onDirtyChange(false);
    },
    [onDirtyChange],
  );
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  useEffect(() => {
    writeStored(SNAP_KEY, snap);
  }, [snap]);
  useEffect(() => {
    writeStored(DISPLAY_KEY, display);
  }, [display]);

  // Selections follow the layout: a component that is gone cannot stay selected.
  useEffect(() => {
    setSelection((s) => {
      const keep = s.filter((id) => present.components.some((c) => c.id === id));
      return keep.length === s.length ? s : keep;
    });
  }, [present]);

  const say = useCallback((tone: Notice["tone"], text: string) => setNotice({ tone, text }), []);
  useEffect(() => {
    if (!notice || notice.tone === "err") return;
    const t = window.setTimeout(() => setNotice(null), 6000);
    return () => window.clearTimeout(t);
  }, [notice]);

  const edit = useCallback((fn: (l: OpticalLayout) => OpticalLayout, key?: string) => {
    const cur = latest.current;
    if (cur.readOnly) return;
    const next = fn(cur.state.history.present);
    dispatch({ type: "edit", layout: next, key, now: Date.now() });
  }, []);

  const selectIds = useCallback((ids: string[]) => setSelection(ids), []);
  /** the banner's button: move the view to the point where the trace was cut and zoom in on it */
  const showCut = useCallback(() => {
    const p = cutPoint(latest.current.trace);
    if (p) setFocus((f) => ({ point: p, nonce: (f?.nonce ?? 0) + 1, center: true, minZoom: CUT_ZOOM }));
  }, []);
  const focusOn = useCallback((id: string) => {
    const c = latest.current.present.components.find((x) => x.id === id);
    if (c) setFocus((f) => ({ point: { x: c.x_mm, y: c.y_mm }, nonce: (f?.nonce ?? 0) + 1 }));
  }, []);

  // ── adding ──
  const placeAt = useCallback(
    (type: ComponentType, at: Vec) => {
      const cur = latest.current.present;
      const refusal = addRefusal(cur, type);
      if (refusal) return say("warn", refusal === "sources" ? tr("光源最多 32 个。", "At most 32 light sources.") : tr("器件最多 400 个。", "At most 400 components."));
      // do not stack a new component exactly on top of another
      let p = at;
      for (let i = 0; i < 24 && cur.components.some((c) => Math.hypot(c.x_mm - p.x, c.y_mm - p.y) < 1); i++) p = { x: p.x + cur.table.hole_pitch_mm, y: p.y - cur.table.hole_pitch_mm };
      const r = addComponent(cur, type, p);
      if (r.id === null) return;
      edit(() => r.layout);
      setSelection([r.id]);
      setTab("inspector");
      setTool("select");
    },
    [edit, say],
  );

  const onAdd = useCallback(
    (type: ComponentType) => {
      const cur = latest.current.present;
      const centre = centerRef.current ?? { x: cur.table.width_mm / 2, y: cur.table.height_mm / 2 };
      placeAt(type, snap === "off" ? centre : snapPoint(cur.table, centre, snap));
    },
    [placeAt, snap],
  );

  // ── deleting, duplicating ──
  const remove = useCallback(
    (ids: string[]) => {
      const cur = latest.current.present;
      const locked = lockedAmong(cur, ids);
      const next = removeComponents(cur, ids);
      if (next === cur) return say("warn", tr("已锁定的器件不会被删除；先解锁。", "Locked components are not deleted; unlock them first."));
      edit(() => next);
      setSelection(locked);
      if (locked.length > 0) say("info", tr(`${locked.length} 个已锁定的器件没有删除。`, `${locked.length} locked components were kept.`));
    },
    [edit, say],
  );
  const duplicate = useCallback(
    (ids: string[]) => {
      const cur = latest.current.present;
      const r = duplicateComponents(cur, ids);
      if (r.ids.length === 0) return say("warn", tr("已经到了器件数量的上限。", "The limit of components is reached."));
      edit(() => r.layout);
      setSelection(r.ids);
    },
    [edit, say],
  );

  // ── tools ──
  const setToolChecked = useCallback((t: CanvasTool) => {
    setTool(t);
    setAimFrom(null);
    setMeasureA(null);
    setMeasureB(null);
  }, []);

  const onToolPoint = useCallback(
    (p: ToolPoint) => {
      const cur = latest.current;
      if (cur.tool === "aim") {
        const comp = p.componentId ? getComponent(cur.present, p.componentId) : undefined;
        if (aimFrom === null) {
          if (comp && isAimable(comp)) {
            setAimFrom(comp.id);
            setSelection([comp.id]);
          } else say("warn", tr("先点一面镜子、分束器或光栅。", "Click a mirror, beam splitter or grating first."));
          return;
        }
        const target = comp ? { componentId: comp.id } : cur.present.table.hole_pitch_mm > 0 && snap !== "off" ? snapPoint(cur.present.table, p.world, snap) : p.world;
        const r = aimMirror(cur.present, cur.trace, aimFrom, target);
        if (!r.ok) return say("warn", aimErrorText(r.error, truncation(cur.trace) !== null));
        edit(() => r.layout);
        const name = getComponent(cur.present, aimFrom)?.label ?? aimFrom;
        say(r.withinAperture ? "ok" : "warn", r.withinAperture ? tr(`已瞄准：${name} 转到 ${r.angle_deg.toFixed(2)}°`, `Aimed: ${name} turned to ${r.angle_deg.toFixed(2)}°`) : tr(`已瞄准：${name} 转到 ${r.angle_deg.toFixed(2)}°，但光打到口径之外了，请再挪一下镜子。`, `Aimed: ${name} turned to ${r.angle_deg.toFixed(2)}°, but the beam now meets it outside its aperture; move the mirror a little.`));
        setAimFrom(null);
        return;
      }
      if (cur.tool === "measure") {
        if (measureA === null || measureB !== null) {
          setMeasureA(p);
          setMeasureB(null);
        } else setMeasureB(p);
      }
    },
    [aimFrom, edit, measureA, measureB, say, snap],
  );

  const actions = useMemo(
    () => ({
      aim: (id: string) => {
        setTool("aim");
        setAimFrom(id);
        setSelection([id]);
      },
      putOnBeam: (id: string): string | null => {
        const cur = latest.current.present;
        const r = putOnBeam(cur, id);
        if (!r) return tr("现在没有光束可以放上去（先放一个光源）。", "There is no beam to move onto yet (add a light source first).");
        edit(() => r.layout);
        return r.beam.distance < 0.005 ? tr("它已经在光束上了。", "It is already on the beam.") : null;
      },
      placeAlong: (id: string, afterId: string, distance: number, alignAxis: boolean): string | null => {
        const cur = latest.current;
        const r = placeAlongBeam(cur.present, cur.trace, id, afterId, distance, { alignAxis });
        if (!r) return tr("那个器件后面现在没有光束。", "No beam leaves that component right now.");
        edit(() => r.layout);
        return r.along.beyondLeg ? tr("这个距离超过了下一个器件，点放在了它后面。", "That distance runs past the next component; the point is behind it.") : null;
      },
      remove,
      duplicate,
      select: (ids: string[]) => {
        setSelection(ids);
        if (ids[0]) focusOn(ids[0]);
      },
      notify: (text: string) => say("info", text),
    }),
    [duplicate, edit, focusOn, remove, say],
  );

  // ── keyboard ──
  const saveRef = useRef<() => Promise<void>>(async () => {});
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isEditableTarget(e.target as HTMLElement | null) || e.defaultPrevented) return;
      const cmd = interpretKey(e);
      if (!cmd) return;
      const cur = latest.current;
      const handled = () => {
        e.preventDefault();
        e.stopPropagation();
      };
      switch (cmd.kind) {
        case "save":
          handled();
          void saveRef.current();
          return;
        case "undo":
          handled();
          if (!cur.readOnly) dispatch({ type: "undo" });
          return;
        case "redo":
          handled();
          if (!cur.readOnly) dispatch({ type: "redo" });
          return;
        case "select-all":
          handled();
          setSelection(cur.present.components.map((c) => c.id));
          return;
        case "escape":
          if (cur.tool !== "select") {
            handled();
            setToolChecked("select");
          } else if (cur.selection.length > 0) {
            handled();
            setSelection([]);
          }
          return;
      }
      if (cur.readOnly || cur.selection.length === 0) return;
      handled();
      const ids = cur.selection;
      if (cmd.kind === "delete") return remove(ids);
      const locked = lockedAmong(cur.present, ids);
      if (locked.length === ids.length) return say("info", tr("所选器件已锁定，先解锁才能移动或旋转。", "The selected components are locked; unlock them to move or rotate."));
      if (cmd.kind === "move") edit((l) => moveBy(l, ids, cmd.dx, cmd.dy), `move:${ids.join(",")}`);
      else if (ids.length === 1) edit((l) => rotateBy(l, ids, cmd.deg), `rotate:${ids[0]}`);
      else {
        const sel = cur.present.components.filter((c) => ids.includes(c.id) && !c.locked);
        const pivot = { x: sel.reduce((s, c) => s + c.x_mm, 0) / sel.length, y: sel.reduce((s, c) => s + c.y_mm, 0) / sel.length };
        edit((l) => rotateGroup(l, ids, cmd.deg, pivot), `rotate:${ids.join(",")}`);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [edit, remove, say, setToolChecked]);

  // ── saving ──
  async function doSave(): Promise<void> {
    const cur = latest.current;
    if (cur.readOnly || busy) return;
    const layout = cur.state.history.present;
    const errors = errorsOf(validateLayout(layout));
    if (errors.length > 0) {
      setTab("check");
      return say("err", tr(`有 ${errors.length} 处问题要先改：${errors[0]?.message ?? ""}`, `${errors.length} problems to fix first: ${errors[0]?.message ?? ""}`));
    }
    const body = withOperator(operator, { layout: layoutForWrite(layout), base_revision: cur.state.baseRevision });
    if (!body.ok) return say("err", body.error);
    try {
      const doc = await save.mutateAsync({ id: layout.id, body: body.value });
      dispatch({ type: "saved", sent: layout, saved: doc.layout });
      setLinksSnapshot(doc.linksState);
      say("ok", tr(`已保存，第 ${doc.layout.revision} 版`, `Saved as revision ${doc.layout.revision}`));
    } catch (e) {
      const f = classifySaveError(e);
      if (f.kind === "conflict") {
        dispatch({ type: "conflict", conflict: { currentRevision: f.currentRevision, updatedBy: f.updatedBy, updatedAt: f.updatedAt, message: f.message } });
        setNotice(null);
      } else say("err", saveFailureText(f));
    }
  }

  saveRef.current = doSave;

  const loadLatest = async () => {
    setBusy(true);
    try {
      const doc = await layoutApi.get(present.id);
      dispatch({ type: "adopt-latest", latest: doc.layout });
      setLinksSnapshot(doc.linksState);
      setViewingRev(null);
      refreshList();
      say("info", tr(`已载入第 ${doc.layout.revision} 版。你之前的修改可以用 Ctrl+Z 找回。`, `Loaded revision ${doc.layout.revision}. Ctrl+Z brings your earlier edits back.`));
    } catch (e) {
      say("err", e instanceof LayoutApiError && e.status === 404 ? saveFailureText({ kind: "missing", message: "" }) : saveFailureText(classifySaveError(e)));
    } finally {
      setBusy(false);
    }
  };

  const saveAs = async (id: string, title: string) => {
    const body = withOperator(operator, { layout: layoutForWrite({ ...present, id, title: title.trim() || present.title, revision: 0 }), base_revision: 0 });
    if (!body.ok) return setSaveAsError(body.error);
    setBusy(true);
    setSaveAsError(null);
    try {
      await layoutApi.put(id, body.value);
      setSaveAsOpen(false);
      say("ok", tr(`已另存为 ${id}`, `Saved as ${id}`));
      refreshList();
      onSwitchTo(id);
    } catch (e) {
      const f = classifySaveError(e);
      setSaveAsError(f.kind === "conflict" ? tr("已经有这个编号的光路图了，换一个。", "A layout with that id exists already; choose another.") : saveFailureText(f));
    } finally {
      setBusy(false);
    }
  };

  const restore = async (rev: number) => {
    setBusy(true);
    try {
      const old = await layoutApi.revision(present.id, rev);
      const layout: OpticalLayout = { ...old, id: present.id };
      const body = withOperator(operator, { layout: layoutForWrite(layout), base_revision: state.baseRevision });
      if (!body.ok) return say("err", body.error);
      const doc = await save.mutateAsync({ id: present.id, body: body.value });
      dispatch({ type: "adopt-latest", latest: doc.layout });
      setViewingRev(null);
      say("ok", tr(`已回到第 ${rev} 版，存为第 ${doc.layout.revision} 版`, `Went back to revision ${rev}, saved as revision ${doc.layout.revision}`));
    } catch (e) {
      const f = classifySaveError(e);
      if (f.kind === "conflict") dispatch({ type: "conflict", conflict: { currentRevision: f.currentRevision, updatedBy: f.updatedBy, updatedAt: f.updatedAt, message: f.message } });
      else say("err", saveFailureText(f));
    } finally {
      setBusy(false);
    }
  };

  // ── export ──
  const exportSvg = () => {
    const b = layoutBounds(shown.table, shown.components, 30);
    const w = b.maxX - b.minX;
    const h = b.maxY - b.minY;
    // about 1.6 px per mm: 1200 mm come out as 1920 px, which opens at a readable size
    const k = Math.min(1.6, 6000 / Math.max(w, h));
    const view: View = { k, ox: -b.minX * k, oy: b.maxY * k };
    const width = Math.ceil(w * k);
    const height = Math.ceil(h * k);
    const markup = renderToStaticMarkup(
      <LayoutSvg layout={shown} trace={trace} indicators={indicators} nearMisses={misses} view={view} width={width} height={height} theme={theme} display={{ ...display, rulers: false }} selected={new Set()} commentCounts={commentCounts} />,
    );
    const css = getComputedStyle(document.documentElement);
    const text = standaloneSvg(resolveCssVars(markup, (n) => css.getPropertyValue(n)));
    download(new Blob([text], { type: "image/svg+xml" }), exportFileName(present, "svg", dirty));
    say("ok", tr("已导出 SVG。", "SVG exported."));
  };

  const exportPng = async () => {
    if (!saved) return say("warn", tr("PNG 由服务器根据已保存的版本渲染，请先保存。", "The PNG is rendered by the server from the saved revision; save first."));
    try {
      const blob = await layoutApi.renderPng(present.id, 2400);
      download(blob, exportFileName({ id: present.id, revision: state.baseRevision }, "png"));
      say(dirty ? "warn" : "ok", dirty ? tr(`已导出 PNG（第 ${state.baseRevision} 版；你未保存的修改不在里面）。`, `PNG exported (revision ${state.baseRevision}; your unsaved edits are not in it).`) : tr("已导出 PNG。", "PNG exported."));
    } catch (e) {
      if (e instanceof PngUnavailableError) {
        say("warn", tr("这个 MAST 服务还不能渲染 PNG（服务器端的渲染器没有装上，或这张图在服务器上不存在）。请先用 SVG 导出。", "This MAST service cannot render PNG yet (its server-side renderer is missing, or the layout is not on the server). Use the SVG export for now."));
      } else say("err", tr(`PNG 导出失败：${(e as Error).message}`, `PNG export failed: ${(e as Error).message}`));
    }
  };

  // ── tool overlays, drawn in screen pixels above the canvas ──
  const renderOverlay = useCallback(
    ({ view, cursor }: { view: View; cursor: Vec | null }) => {
      const nodes: ReactNode[] = [];
      if (tool === "aim" && aimFrom && cursor) {
        const mirror = getComponent(present, aimFrom);
        const hit = mirror ? strongestHit(eventsByComponent(trace).get(aimFrom) ?? []) : null;
        if (mirror && hit) {
          const target = snap !== "off" ? snapPoint(present.table, cursor, snap) : cursor;
          const a = toScreen(view, hit.event.pos);
          const t = toScreen(view, target);
          const sol = solveAim({ dir: hit.event.dir, center: { x: mirror.x_mm, y: mirror.y_mm }, onBeam: hit.event.pos, target });
          nodes.push(<line key="aim" x1={a.x} y1={a.y} x2={t.x} y2={t.y} stroke="var(--mast-accent)" strokeWidth={1.5} strokeDasharray="6 4" />);
          if (typeof sol !== "string") {
            nodes.push(
              <text key="aimt" x={t.x + 10} y={t.y - 8} fontSize={11} fontFamily="ui-monospace, Consolas, monospace" fill="var(--mast-accent)" stroke="var(--mast-panel)" strokeWidth={3} paintOrder="stroke">
                {tr(`→ ${sol.angle_deg.toFixed(2)}°`, `→ ${sol.angle_deg.toFixed(2)}°`)}
              </text>,
            );
          }
        }
      }
      if (tool === "measure") {
        const a = measureA;
        const b = measureB ?? (a && cursor ? { world: cursor, componentId: null } : null);
        if (a && b) {
          const pa = toScreen(view, a.componentId ? centreOf(present, a.componentId, a.world) : a.world);
          const pb = toScreen(view, b.componentId ? centreOf(present, b.componentId, b.world) : b.world);
          const m = measure(a.componentId ? centreOf(present, a.componentId, a.world) : a.world, b.componentId ? centreOf(present, b.componentId, b.world) : b.world);
          const beam = a.componentId && b.componentId ? beamPathBetween(present, trace, a.componentId, b.componentId) : null;
          nodes.push(<line key="m" x1={pa.x} y1={pa.y} x2={pb.x} y2={pb.y} stroke="var(--mast-warn)" strokeWidth={1.6} strokeDasharray="5 3" />);
          nodes.push(<circle key="ma" cx={pa.x} cy={pa.y} r={4} fill="var(--mast-warn)" />);
          nodes.push(<circle key="mb" cx={pb.x} cy={pb.y} r={4} fill="var(--mast-warn)" />);
          nodes.push(
            <text key="mt" x={(pa.x + pb.x) / 2 + 8} y={(pa.y + pb.y) / 2 - 8} fontSize={11} fontFamily="ui-monospace, Consolas, monospace" fill="var(--mast-warn)" stroke="var(--mast-panel)" strokeWidth={3} paintOrder="stroke">
              {`${m.distance.toFixed(2)} mm  (Δx ${m.dx.toFixed(2)}, Δy ${m.dy.toFixed(2)}, ${m.angle_deg.toFixed(2)}°)${beam ? tr(`  沿光束 ${beam.mm.toFixed(2)} mm`, `  along the beam ${beam.mm.toFixed(2)} mm`) : ""}`}
            </text>,
          );
        }
      }
      return nodes;
    },
    [aimFrom, measureA, measureB, present, snap, tool, trace],
  );

  const hint = readOnly
    ? tr(`正在查看第 ${viewingRev} 版（只读）。`, `Viewing revision ${viewingRev} (read only).`)
    : tool === "aim"
      ? aimFrom
        ? tr(`瞄准：${getComponent(present, aimFrom)?.label ?? aimFrom} → 点目标器件，或点台面上的一个点。Esc 取消。`, `Aim: ${getComponent(present, aimFrom)?.label ?? aimFrom} → click the target component, or a point on the table. Esc cancels.`)
        : tr("瞄准：先点一面镜子、分束器或光栅。", "Aim: click a mirror, beam splitter or grating first.")
      : tool === "measure"
        ? tr("测量：点两个点（点器件取它的中心）。再点一下重新开始。", "Measure: click two points (a component counts as its centre). Click again to start over.")
        : null;

  const selectedComps = selection.map((id) => getComponent(shown, id)).filter((c): c is NonNullable<typeof c> => c !== undefined);
  const ctx: InspectorCtx = {
    layout: shown,
    trace,
    indicators,
    issues,
    readOnly,
    edit,
    actions,
    links: { devices: devices.data ?? null, deviceError: devices.isError ? String((devices.error as Error | null)?.message ?? "") || tr("读取失败", "failed") : null, slots: slotInfo.slots, slotValues: slotInfo.values, slotError: slotInfo.error, snapshot: linksSnapshot },
    nowMs,
    commentCount: selectedComps[0] ? (commentCounts.get(selectedComps[0].id) ?? 0) : 0,
  };

  const worst = componentLevel([...indicators.values()].flat());
  /** the alignment level of each hit component, for the dots of the component list */
  const levels = useMemo(() => {
    const m = new Map<string, ReturnType<typeof componentLevel>>();
    for (const [id, hits] of indicators) m.set(id, componentLevel(hits));
    return m;
  }, [indicators]);

  return (
    <div className="space-y-2" data-editor="optical-layout">
      {state.conflict && (
        <ConflictBanner
          conflict={state.conflict}
          baseRevision={state.baseRevision}
          busy={busy}
          onLoadLatest={() => void loadLatest()}
          onSaveAs={() => {
            setSaveAsError(null);
            setSaveAsOpen(true);
          }}
          onKeep={() => dispatch({ type: "dismiss-conflict" })}
        />
      )}

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <OperatorNameField />
        <div className="flex items-center gap-2 text-xs text-mast-muted">
          <span className="font-mono text-mast-text">{present.id}</span>
          <span>{saved ? tr(`第 ${state.baseRevision} 版`, `revision ${state.baseRevision}`) : tr("尚未保存", "not saved yet")}</span>
          {present.updated_by && (
            <span>
              · {present.updated_by}
            </span>
          )}
          {dirty && <Badge tone="WARN">{tr("有未保存的修改", "Unsaved changes")}</Badge>}
          {readOnly && <Badge tone="INFO">{tr("只读", "Read only")}</Badge>}
        </div>
      </div>

      <Toolbar
        onAdd={onAdd}
        tool={tool}
        onTool={setToolChecked}
        snap={snap}
        onSnap={setSnap}
        display={display}
        onDisplay={setDisplay}
        canUndo={canUndo(state.history)}
        canRedo={canRedo(state.history)}
        undoSteps={state.history.past.length}
        onUndo={() => dispatch({ type: "undo" })}
        onRedo={() => dispatch({ type: "redo" })}
        readOnly={readOnly}
        dirty={dirty}
        saving={save.isPending}
        canSave={!readOnly && (dirty || !saved)}
        onSave={() => void doSave()}
        onSaveAs={() => {
          setSaveAsError(null);
          setSaveAsOpen(true);
        }}
        onExportSvg={exportSvg}
        onExportPng={() => void exportPng()}
      />

      {cut && (
        <div role="status" data-trace-cut="" className="flex items-start justify-between gap-3 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-1.5 text-sm text-mast-warn">
          <span>{traceCutMessage(cut)}</span>
          <button type="button" data-trace-cut-locate="" onClick={showCut} className="shrink-0 rounded-mast-ctl border border-mast-warn-border px-2 py-0.5 text-xs font-semibold hover:bg-mast-panel-2">
            {tr("定位到截断处", "Show where it stopped")}
          </button>
        </div>
      )}

      {(notice || hint) && (
        <div
          role={notice?.tone === "err" ? "alert" : "status"}
          className={clsx(
            "flex items-start justify-between gap-3 rounded-mast-ctl border px-3 py-1.5 text-sm",
            notice
              ? notice.tone === "ok"
                ? "border-mast-auto-border bg-mast-auto-bg text-mast-auto"
                : notice.tone === "warn"
                  ? "border-mast-warn-border bg-mast-warn-bg text-mast-warn"
                  : notice.tone === "err"
                    ? "border-mast-danger-border bg-mast-danger-bg text-mast-danger"
                    : "border-mast-info-border bg-mast-info-bg text-mast-info"
              : "border-mast-border bg-mast-panel-2 text-mast-muted",
          )}
        >
          <span>{notice?.text ?? hint}</span>
          {notice && (
            <button type="button" className="shrink-0 opacity-70 hover:opacity-100" aria-label={tr("关闭", "Dismiss")} onClick={() => setNotice(null)}>
              ✕
            </button>
          )}
        </div>
      )}
      {revision.isError && viewingRev !== null && <ErrorNote error={revision.error} label={tr("那一版读取失败", "Could not read that revision")} />}

      <div className="grid gap-3 xl:h-[max(34rem,calc(100vh-25rem))] xl:grid-cols-[11rem_minmax(0,1fr)_21rem]">
        <aside className="hidden min-h-0 xl:block" aria-label={tr("器件面板", "Component palette")}>
          <Palette
            onAdd={onAdd}
            disabled={readOnly}
            refusal={readOnly ? tr("正在看历史版本，不能添加。", "Viewing an old revision: nothing can be added.") : null}
          />
        </aside>

        <div className="h-[30rem] min-w-0 xl:h-auto xl:min-h-[24rem]">
          <LayoutCanvas
            layout={shown}
            trace={trace}
            indicators={indicators}
            nearMisses={misses}
            display={display}
            theme={theme}
            selected={selectedSet}
            commentCounts={commentCounts}
            snap={snap}
            tool={tool}
            readOnly={readOnly}
            fitNonce={0}
            focus={focus}
            centerRef={centerRef}
            onSelect={selectIds}
            onPreview={setPreview}
            onCommitMove={(positions) => edit((l) => setPositions(l, positions), undefined)}
            onDropType={placeAt}
            onToolPoint={onToolPoint}
            onLockedDrag={() => say("info", tr("这个器件已锁定，不会被拖动。要移动它先解锁。", "This component is locked and cannot be dragged. Unlock it to move it."))}
            renderOverlay={renderOverlay}
          />
        </div>

        <aside className="flex max-h-[44rem] min-h-0 flex-col rounded-mast-card border border-mast-border bg-mast-panel xl:max-h-none" aria-label={tr("检查面板", "Inspector")}>
          <div className="flex border-b border-mast-border px-1" role="tablist">
            {(
              [
                ["inspector", tr("属性", "Properties"), null],
                ["comments", tr("评论", "Comments"), openComments > 0 ? openComments : null],
                ["history", tr("历史", "History"), null],
                ["check", tr("检查", "Check"), problems > 0 ? problems : null],
              ] as const
            ).map(([id, label, badge]) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                onClick={() => setTab(id)}
                className={clsx(TAB_BTN, tab === id ? "border-mast-accent font-semibold text-mast-accent" : "border-transparent text-mast-muted hover:text-mast-text")}
              >
                {label}
                {badge !== null && (
                  <span className={clsx("ml-1 rounded-full px-1.5 text-[10px] font-semibold", id === "check" && worst === "bad" ? "bg-mast-danger text-mast-panel" : "bg-mast-accent text-mast-accent-ink")}>{badge}</span>
                )}
              </button>
            ))}
          </div>
          <div className="min-h-0 flex-1 overflow-auto p-3">
            {tab === "inspector" &&
              (selectedComps.length === 1 ? (
                <ComponentForm key={selectedComps[0]!.id} c={selectedComps[0]!} ctx={ctx} />
              ) : selectedComps.length > 1 ? (
                <GroupForm ids={selectedComps.map((c) => c.id)} ctx={ctx} />
              ) : (
                <LayoutForm
                  traceCut={cut !== null}
                  layout={shown}
                  readOnly={readOnly}
                  levels={levels}
                  onSelect={(id) => actions.select([id])}
                  edit={edit}
                  onAddText={() => {
                    const centre = centerRef.current ?? { x: present.table.width_mm / 2, y: present.table.height_mm / 2 };
                    const r = addText(present, centre, tr("文字", "Text"));
                    if (r.id) edit(() => r.layout);
                  }}
                />
              ))}
            {tab === "comments" && <CommentsPanel layout={present} saved={saved} selectedId={selection.length === 1 ? (selection[0] ?? null) : null} onSelect={(id) => actions.select([id])} />}
            {tab === "history" && (
              <HistoryPanel
                layoutId={present.id}
                saved={saved}
                currentRevision={state.baseRevision}
                viewing={viewingRev}
                busy={busy || save.isPending}
                onView={(r) => {
                  setViewingRev(r);
                  setSelection([]);
                  setToolChecked("select");
                }}
                onBack={() => setViewingRev(null)}
                onRestore={(r) => void restore(r)}
              />
            )}
            {tab === "check" && <CheckPanel diagnostics={checks} issues={issues} trace={trace} onSelect={(id) => actions.select([id])} />}
          </div>
        </aside>
      </div>

      <div className="flex flex-wrap items-center gap-3 text-xs text-mast-muted">
        <span>{tr(`${shown.components.length} 个器件 · ${trace.length} 束光`, `${shown.components.length} components · ${trace.length} beams`)}</span>
        {selectedComps.length === 1 && <span>{typeLabel(selectedComps[0]!.type)}</span>}
        <span className="hidden xl:inline">{tr("滚轮缩放，空白处拖动平移，Shift+拖动框选。", "Wheel zooms, drag on empty canvas pans, Shift+drag selects a box.")}</span>
      </div>

      <SaveAsDialog
        key={saveAsOpen ? "open" : "closed"}
        open={saveAsOpen}
        suggested={suggestCopyId(present.id, taken)}
        taken={taken}
        busy={busy}
        error={saveAsError}
        onClose={() => setSaveAsOpen(false)}
        onSave={(id, title) => void saveAs(id, title)}
      />
    </div>
  );
}

function centreOf(l: OpticalLayout, id: string, fallback: Vec): Vec {
  const c = getComponent(l, id);
  return c ? { x: c.x_mm, y: c.y_mm } : fallback;
}
