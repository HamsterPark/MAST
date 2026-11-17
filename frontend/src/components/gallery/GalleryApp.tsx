// ════════════════════════════════════════════════════════════════════════════
// Data gallery (Records -> Data gallery, /records/gallery).
//
// Design: docs/v2/design/data_gallery.md. Data comes from /api/gallery/*. The "Figures" tab
// generates publication figures from marks and series.
//
// The view lives in the URL query (lib/gallery/route.ts), so deep links can be bookmarked and
// back/forward work. When the URL has no `v`, the last view is restored from
// localStorage["mast.gallery.view"] (validated on read).
//
// This layer handles cross-view concerns: the header, loading marks and refreshing them when
// the page becomes visible again, re-fetching the index and marks the moment a build finishes,
// the figure-job notification, clearing the selection on view change, the bottom action bar
// and the toast.
//
// Automatic incremental builds (design D8) are started by the server on a timer and when this
// page is opened / becomes visible (POST /api/gallery/auto; the server decides whether one
// really starts). They must not disturb someone who is looking at pictures: when one ends with
// nothing changed, nothing happens; with changes, only a light toast and a header chip appear and
// the new index is fetched when the chip is clicked or the view changes (re-fetching it in place
// would reset the card grid, the scroll position, the open lightbox and the selection). Marks
// are never re-read after an automatic build. lib/gallery/autoBuild.ts holds the rules.
//
// NOTE: the view body is memoised on "view + index". This layer subscribes to save state and
// build state, so it re-renders on every rating and on every 1.5 s build poll. If the body were
// rebuilt each time, list views would receive a new `base` array: the card grid would drop back
// to the first 150 cards, scroll position would be lost, and the list would re-sort by the new
// marks mid-rating. So the body is rebuilt only when the view or the index really changes.
// ════════════════════════════════════════════════════════════════════════════

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { api } from "@/api/client";
import { buildKey, isPristine, planAfterBuild } from "@/lib/gallery/autoBuild";
import { newBatch } from "@/lib/gallery/dirs";
import { parseView, navId, viewKey, viewSearch, type GalleryView } from "@/lib/gallery/route";
import { GALLERY_KEYS, useGalleryModel, useGalleryStatus } from "./useGalleryData";
import { saveText, useMarksStore } from "./marksStore";
import { useSelectionStore } from "./selectionStore";
import { DirsView } from "./DirsView";
import { ListView } from "./ListView";
import { AllHead, DirHead, NewHead } from "./ListHeads";
import { SeriesView } from "./SeriesView";
import { MarkedView } from "./MarkedView";
import { SetupView } from "./SetupView";
import { SelectionBar } from "./SelectionBar";
import { GalleryToast, toast } from "./toast";
import { FiguresView } from "./figures/FiguresView";
import { FigureJobChip } from "./figures/figureJob";
import { tr } from "@/lib/i18n";

const LAST_VIEW_KEY = "mast.gallery.view";

/** Nearest ancestor that scrolls (AppLayout's <main>), else the page. */
function scrollParent(el: HTMLElement | null): HTMLElement {
  for (let n = el?.parentElement ?? null; n; n = n.parentElement) {
    const oy = getComputedStyle(n).overflowY;
    if (oy === "auto" || oy === "scroll") return n;
  }
  return (document.scrollingElement as HTMLElement | null) ?? document.documentElement;
}

/** Has the user done nothing yet: no lightbox, first screen, nothing selected, nothing being edited? */
function viewIsPristine(root: HTMLElement | null): boolean {
  const sc = scrollParent(root);
  const ae = document.activeElement;
  const typing = !!ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName);
  const save = useMarksStore.getState().saveState;
  return isPristine({
    lightboxOpen: !!document.querySelector("[data-gallery-lightbox]"),
    scrollTop: sc.scrollTop,
    viewportHeight: sc.clientHeight,
    selected: useSelectionStore.getState().ids.size,
    editing: typing || save === "pending" || save === "bad",
  });
}

function readLastView(): string | null {
  try {
    const raw = localStorage.getItem(LAST_VIEW_KEY);
    return raw && parseView(new URLSearchParams(raw)) ? raw : null;
  } catch {
    return null;
  }
}

const NAV: { id: string; label: string; view: GalleryView }[] = [
  { id: "dirs", label: tr("目录", "Directories"), view: { v: "dirs" } },
  { id: "all-f", label: tr("全部帧", "All frames"), view: { v: "all", k: "f" } },
  { id: "all-s", label: tr("全部谱", "All spectra"), view: { v: "all", k: "s" } },
  { id: "all-g", label: tr("网格谱", "Grid spectra"), view: { v: "all", k: "g" } },
  { id: "marked", label: tr("已标记", "Marked"), view: { v: "marked" } },
  { id: "figures", label: tr("出图", "Figures"), view: { v: "figures" } },
  { id: "setup", label: tr("数据根与构建", "Data roots & build"), view: { v: "setup" } },
];

export function GalleryApp() {
  const [sp, setSp] = useSearchParams();
  const parsed = parseView(sp);
  const vs = parsed ? viewSearch(parsed) : "";
  const view = useMemo<GalleryView>(() => parseView(new URLSearchParams(vs)) ?? { v: "dirs" }, [vs]);
  const vk = viewKey(view);
  const qc = useQueryClient();

  // No view in the URL: restore the last view (replace history so back does not gain an extra step).
  useEffect(() => {
    if (!vs) setSp(readLastView() ?? viewSearch({ v: "dirs" }), { replace: true });
  }, [vs, setSp]);
  useEffect(() => {
    if (!vs) return;
    try {
      localStorage.setItem(LAST_VIEW_KEY, vs);
    } catch {
      /* works without remembering */
    }
  }, [vs]);

  const { q, model } = useGalleryModel();
  const status = useGalleryStatus();

  // Marks: load on first visit; refresh when the page becomes visible again, in case another tab changed the server copy.
  const init = useMarksStore((s) => s.init);
  useEffect(() => {
    void init();
    void useMarksStore.getState().refresh();
  }, [init]);
  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState === "visible") void useMarksStore.getState().refresh();
    };
    const onUnload = () => void useMarksStore.getState().flush();
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("beforeunload", onUnload);
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      window.removeEventListener("beforeunload", onUnload);
      void useMarksStore.getState().flush();
    };
  }, []);

  // Fresh index waiting to be fetched (an automatic build changed something).
  const [staleIndex, setStaleIndex] = useState(false);
  const staleRef = useRef(false);
  const applyIndex = useCallback(() => {
    staleRef.current = false;
    setStaleIndex(false);
    void qc.invalidateQueries({ queryKey: GALLERY_KEYS.index });
  }, [qc]);

  // A build has just ended (its identity changed). Identity, not the running -> not running edge:
  // polling pauses while the page is in the background, so a whole automatic build can run and end
  // without anyone seeing running=true. The first status seen is a past build, not news.
  const lastKey = useRef<string | undefined>(undefined);
  const lastAutoFailed = useRef(0);
  useEffect(() => {
    const d = status.data;
    if (!d) return;
    const key = buildKey(d);
    if (key === null) {
      // Still running. If this is the first status we see, its end is news (not a past build).
      if (lastKey.current === undefined) lastKey.current = "";
      return;
    }
    if (lastKey.current === undefined) {
      lastKey.current = key;
      return;
    }
    if (key === lastKey.current) return;
    lastKey.current = key;
    const plan = planAfterBuild(d, viewIsPristine(rootRef.current));
    if (plan.index === "now") applyIndex();
    else if (plan.index === "later") {
      staleRef.current = true;
      setStaleIndex(true);
    }
    if (plan.marks) void useMarksStore.getState().reload();
    switch (plan.toast) {
      case "updated":
        toast(
          tr(
            `图库已更新：新 ${d.n_new ?? 0} · 变动 ${d.n_changed ?? 0} · 失败 ${d.n_failed ?? 0}`,
            `Gallery updated: ${d.n_new ?? 0} new · ${d.n_changed ?? 0} changed · ${d.n_failed ?? 0} failed`,
          ),
        );
        break;
      case "light":
        toast(
          tr(
            `图库自动更新：新 ${d.n_new ?? 0} · 变动 ${d.n_changed ?? 0}`,
            `Gallery auto-updated: ${d.n_new ?? 0} new · ${d.n_changed ?? 0} changed`,
          ),
        );
        break;
      case "failed":
        // The same files fail again every round: say so once per distinct count, not every ten minutes.
        if ((d.n_failed ?? 0) !== lastAutoFailed.current) {
          lastAutoFailed.current = d.n_failed ?? 0;
          toast(
            tr(
              `图库自动更新：${d.n_failed} 个文件处理失败（详情在「数据根与构建」）`,
              `Gallery auto-update: ${d.n_failed} files failed (see Data roots & build)`,
            ),
            "err",
          );
        }
        break;
      case "error":
        toast(tr(`图库构建出错：${d.message || d.detail || ""}`, `Gallery build failed: ${d.message || d.detail || ""}`), "err");
        break;
      case "cancelled":
        toast(tr("图库构建已取消", "Gallery build cancelled"));
        break;
    }
    if (plan.toast !== "failed") lastAutoFailed.current = 0;
  }, [status.data, applyIndex]);

  // Opening the page (or coming back to it): ask the server for an automatic incremental build.
  // The server applies the rules (switch, roots, already running, >= 1 min since the last build,
  // start-up hold), so any number of tabs can ask. Throttled here only to spare the request.
  const lastAsk = useRef(0);
  useEffect(() => {
    const ask = () => {
      if (document.visibilityState !== "visible") return;
      const t = Date.now();
      if (t - lastAsk.current < 30_000) return;
      lastAsk.current = t;
      void api
        .POST("/api/gallery/auto", { body: { reason: "open" } })
        .then(({ data }) => {
          if (data && !data.degraded) qc.setQueryData(GALLERY_KEYS.status, data);
        })
        .catch(() => {
          /* The next poll shows the real state */
        });
    };
    ask();
    document.addEventListener("visibilitychange", ask);
    return () => document.removeEventListener("visibilitychange", ask);
  }, [qc]);

  // Clear the selection when the view changes. Changing view is also the natural moment to take
  // the index an automatic build produced: nothing on screen is kept across it.
  useEffect(() => {
    useSelectionStore.getState().clear();
    if (staleRef.current) applyIndex();
  }, [vk, applyIndex]);

  // Header height -> CSS variable, so the filter bar sticks below it.
  const rootRef = useRef<HTMLDivElement>(null);
  const headRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const head = headRef.current;
    const root = rootRef.current;
    if (!head || !root || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => root.style.setProperty("--gallery-head", `${head.offsetHeight}px`));
    ro.observe(head);
    return () => ro.disconnect();
  }, []);

  const saveState = useMarksStore((s) => s.saveState);
  const savedAt = useMarksStore((s) => s.savedAt);
  const nMarked = useMarksStore((s) => Object.keys(s.doc.items).length + Object.keys(s.doc.series).length);
  const active = navId(view);

  const indexData = q.data;
  const body = useMemo(() => {
    if (view.v === "setup") return <SetupView />;
    // The figures page does not depend on the local index being built: its artifact list and job state use their own endpoints.
    if (view.v === "figures") return <FiguresView />;
    if (q.isPending) return <Spinner label={tr("读取图库索引…", "Reading gallery index…")} />;
    if (q.isError) return <ErrorNote error={q.error} />;
    if (indexData?.degraded) {
      return (
        <div className="rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2.5 text-sm text-mast-warn">
          {tr("图库后端不可用：", "Gallery backend unavailable: ")}
          {indexData.detail || tr("未知原因", "unknown reason")}.
          <Link to={{ search: `?${viewSearch({ v: "setup" })}` }} className="ml-1 underline">
            {tr("去「数据根与构建」看看", "Check Data roots & build")}
          </Link>
        </div>
      );
    }
    if (!model || !model.built) {
      return (
        <div className="space-y-2">
          <EmptyNote label={tr("还没有构建过图库索引。", "The gallery index has not been built yet.")} />
          <div className="text-sm text-mast-muted">
            {tr("先到", "Go to")}
            <Link to={{ search: `?${viewSearch({ v: "setup" })}` }} className="mx-1 text-mast-accent hover:underline">
              {tr("数据根与构建", "Data roots & build")}
            </Link>
            {tr(
              "加一个数据根（放 .sxm / .dat / .3ds 的目录），再点「增量更新」。",
              "and add a data root (a directory holding .sxm / .dat / .3ds files), then click \"Incremental update\".",
            )}
          </div>
        </div>
      );
    }
    switch (view.v) {
      case "dirs":
        return <DirsView model={model} />;
      case "dir": {
        const base = model.items.filter((it) => it.d === view.d);
        return (
          <ListView key={vk} view="dir" model={model} base={base} head={<DirHead key={view.d} d={view.d} base={base} />} />
        );
      }
      case "all":
        return (
          <ListView
            key={vk}
            view="all"
            model={model}
            base={model.items.filter((it) => it.k === view.k)}
            head={<AllHead k={view.k} />}
          />
        );
      case "new":
        return (
          <ListView
            key={vk}
            view="new"
            model={model}
            base={newBatch(model.items, model.lastBatch)}
            head={<NewHead lastBatch={model.lastBatch} />}
          />
        );
      case "series":
        return <SeriesView key={vk} sid={view.s} model={model} />;
      case "marked":
        return <MarkedView key={vk} model={model} />;
    }
    return null;
  }, [view, vk, q.isPending, q.isError, q.error, indexData, model]);

  return (
    <div ref={rootRef} className="relative pb-20">
      <header
        ref={headRef}
        className="sticky top-0 z-20 flex flex-wrap items-center gap-x-[18px] gap-y-1 border-b border-mast-border bg-mast-bg py-1.5"
      >
        <div>
          <Link to={{ search: `?${viewSearch({ v: "dirs" })}` }} className="text-[1.05rem] font-bold text-mast-text hover:no-underline">
            {tr("数据图库", "Data gallery")}
          </Link>
          <span className="ml-2 text-xs text-mast-faint">
            {tr("缩略图", "Thumbnails")} {model?.generated || "—"}
          </span>
        </div>
        <nav className="flex flex-wrap gap-0.5">
          {NAV.map((n) => (
            <Link
              key={n.id}
              to={{ search: `?${viewSearch(n.view)}` }}
              className={clsx(
                "rounded-[3px] px-[9px] py-[3px] text-sm hover:no-underline",
                active === n.id ? "bg-mast-text text-mast-bg" : "text-mast-text hover:bg-mast-panel-2",
              )}
            >
              {n.label}
              {n.id === "marked" && <b className="ml-1">{nMarked}</b>}
            </Link>
          ))}
        </nav>
        {status.data?.running && (
          <Link
            to={{ search: `?${viewSearch({ v: "setup" })}` }}
            className="rounded-mast-badge bg-mast-info-bg px-1.5 text-xs text-mast-info hover:no-underline"
            title={
              status.data.trigger === "auto"
                ? tr("图库正在自动增量更新", "Gallery automatic incremental update in progress")
                : tr("图库正在构建", "Gallery build in progress")
            }
          >
            {status.data.trigger === "auto" ? tr("自动更新中", "Auto-updating") : tr("构建中", "Building")} {status.data.done}/
            {status.data.total}
          </Link>
        )}
        {staleIndex && (
          <button
            type="button"
            onClick={applyIndex}
            className="rounded-mast-badge bg-mast-accent px-1.5 text-xs text-mast-accent-ink"
            title={tr(
              "自动更新带来了新数据；点一下载入（会回到列表顶部）",
              "The automatic update brought new data; click to load it (the list returns to the top)",
            )}
          >
            {tr("有新数据 · 点此载入", "New data · click to load")}
          </button>
        )}
        <FigureJobChip />
        <div
          className={clsx(
            "ml-auto text-xs",
            saveState === "ok" && "text-mast-auto",
            (saveState === "bad" || saveState === "offline") && "font-semibold text-mast-danger",
            (saveState === "idle" || saveState === "pending" || saveState === "loading") && "text-mast-muted",
          )}
        >
          {saveText(saveState, savedAt)}
        </div>
      </header>

      <div className="pt-3">{body}</div>

      {model && <SelectionBar view={view} model={model} />}
      <GalleryToast />
    </div>
  );
}
