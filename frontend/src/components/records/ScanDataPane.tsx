import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { RECORDS_SCANS_POLL_MS } from "@/lib/pollRates";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { DegradedNote, EmptyNote, ErrorNote, Section, Spinner } from "../ui";
import { Button } from "../controls";
import { tr } from "../../lib/i18n";
import { fmtTime } from "./shared";
import { KIND_EXTS, extParam, groupScans, otherCount } from "@/lib/scanKinds";
import { appendScans, pageSummary } from "@/lib/scanPager";
import { copyBadge, parentFolder } from "@/lib/scanCopies";
import { useStickyTab } from "@/hooks/useStickyTab";
import { ScanGridCard } from "./ScanGridCard";
import { ScanPreviewPanel } from "./ScanPreviewPanel";
import { ScanGroupBrowser } from "./ScanGroupBrowser";
import { GRID_FLATTEN_MODES, flattenLabel, type FlattenMode } from "./scanPreviewQuery";

// Data sub-tab:
//   list recent .sxm/.dat/.3ds scans (GET /api/scans/latest) + "Load Latest Scan"
//   + per-file base64 PNG preview (GET /api/scans/preview).
//
// Layout: the list and the preview are side by side, the preview sticky, and the
// list scrolls inside its own box rather than growing the page, so the picture of
// the clicked file never has to be scrolled to. On a narrow screen the preview
// goes above the list instead.
//
// Filtering: the type filter goes to the server as `ext`, which applies it before
// the page slice; a client-side filter over an already truncated list could show
// nothing while matching files sit on disk. The chip counts come from
// `counts_by_ext`, which the server computes over the full discovery result, so a
// chip cannot read 0 for a type that is merely filtered out right now.
//
// Views and paging:
//   * thumbnails in a grid (default), text rows as the list view, and a grouped
//     view for browsing by experiment;
//   * the server takes an offset and the list pages with "Load more";
//   * the server folds byte-identical copies into one entry; the card says xN and
//     lists where they are (the copying itself is what keeps an experiment folder
//     self-contained);
//   * a flatten switch (raw / plane / line / auto), defaulting to line-by-line.

type ScanFileEntry = components["schemas"]["ScanFileEntry"];

/** One page. Not a ceiling: "Load more" asks for the next one. */
const PAGE = 30;

const VIEWS = ["grid", "list", "groups"] as const;
type ViewMode = (typeof VIEWS)[number];
const VIEW_LABEL: Record<ViewMode, string> = {
  grid: tr("网格", "Grid"),
  list: tr("列表", "List"),
  groups: tr("分组", "Groups"),
};

function useLatestScans(kind: string, countKeys: string[], offset: number) {
  const ext = extParam(kind, countKeys);
  return useQuery({
    queryKey: ["scans", "latest", PAGE, offset, ext ?? ""],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/scans/latest", {
        params: { query: ext ? { n: PAGE, offset, ext } : { n: PAGE, offset } },
      });
      if (error) throw error;
      return data;
    },
    // Chip counts describe the whole disk, not this filter, so keeping the last
    // response on screen while the next one loads stops the row from collapsing
    // and re-expanding on every click.
    placeholderData: (prev) => prev,
    refetchInterval: RECORDS_SCANS_POLL_MS,
  });
}

function fmtBytes(b?: number | null): string {
  if (b == null) return "—";
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / 1024 / 1024).toFixed(1)} MB`;
}

function fmtMtime(m?: number | null): string {
  if (m == null) return "—";
  return fmtTime(new Date(m * 1000).toISOString());
}

export function ScanDataPane() {
  const [kind, setKind] = useState<string>("all");
  const [selected, setSelected] = useState<string | null>(null);
  // Counts survive a filter change (they describe the disk, not the filter), so
  // the chip row keeps its labels while the filtered page is in flight.
  const [countsByExt, setCountsByExt] = useState<Record<string, number>>({});
  const [offset, setOffset] = useState(0);
  const [acc, setAcc] = useState<ScanFileEntry[]>([]);

  // Both remembered across navigations: someone who works in the list view should
  // not be handed the grid every time they come back, and the flatten mode is a
  // property of how they read scans, not of this visit.
  const [view, setView] = useStickyTab<ViewMode>("records.data.view", VIEWS, "grid");
  const [flatten, setFlatten] = useStickyTab<FlattenMode>(
    "records.data.flatten",
    ["raw", "plane", "line", "auto"],
    "line",
  );

  // `extParam` reads the count KEYS to expand "other", and the counts come from the
  // query — so the keys have to be held in state rather than read straight off
  // `q.data`, which would make the query key depend on its own result.
  const q = useLatestScans(kind, Object.keys(countsByExt), offset);

  const page: ScanFileEntry[] = useMemo(() => q.data?.scans ?? [], [q.data]);
  // Accumulate pages. `appendScans` drops paths already held, which matters
  // because the rig writes to these directories while the operator pages: a new
  // scan shifts every later entry down one slot, so page 2 starts with the row
  // page 1 ended on.
  useEffect(() => {
    if (!q.data) return;
    setAcc((prev) => (offset === 0 ? page : appendScans(prev, page)));
  }, [q.data, page, offset]);

  // Page one renders straight from the response so the first paint is not a
  // frame behind the effect that fills `acc`.
  const scans = offset === 0 ? page : acc;

  const fresh = q.data?.counts_by_ext as Record<string, number> | undefined;
  const freshKey = fresh ? JSON.stringify(fresh) : "";
  useEffect(() => {
    if (fresh) setCountsByExt(fresh);
    // freshKey, not `fresh`: the object identity changes on every refetch even
    // when the disk has not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [freshKey]);

  // Display always prefers the live response; the state is only the fallback
  // while a filter change is in flight.
  const counts = fresh ?? countsByExt;
  // Chip labels count ENTRIES, not files: the number on the chip has to match
  // the number of cards clicking it produces, or the copy-fold reads as files
  // going missing.
  const collapsedCounts = q.data?.counts_by_ext_collapsed ?? counts;
  const totalAll = Object.values(counts).reduce((a, b) => a + b, 0);
  const totalEntries = Object.values(collapsedCounts).reduce((a, b) => a + b, 0);
  const nOther = otherCount(collapsedCounts);
  const matched = q.data?.total_matched ?? scans.length;
  const collapsed = q.data?.total_collapsed ?? scans.length;

  const resetPaging = () => {
    setOffset(0);
    setAcc([]);
  };

  // A selection the current filter hides must not keep driving the preview —
  // otherwise the highlighted card is invisible and the preview looks stuck.
  const effective =
    (selected && scans.some((s) => s.path === selected) ? selected : null) ||
    scans[0]?.path ||
    null;
  const effectiveEntry = scans.find((s) => s.path === effective) ?? null;

  // Grouped, not just sorted. See lib/scanKinds.groupScans: the source-side
  // exclusion is a blocklist and has now been outrun twice; a heading per kind
  // is what keeps scans visible when the NEXT telemetry writer appears.
  const groups = groupScans(scans);

  const chips: { id: string; label: string }[] = [
    { id: "all", label: `${tr("全部", "All")} (${totalEntries})` },
    // Keep a chip for the ACTIVE filter even when its count is 0, so the
    // operator has something highlighted to click away from.
    ...(kind !== "all" && kind !== "other" && !collapsedCounts[kind]
      ? [{ id: kind, label: `${kind} (0)` }]
      : []),
    ...KIND_EXTS.filter((e) => collapsedCounts[e]).map((e) => ({
      id: e as string,
      label: `${e.replace(".", "").toUpperCase()} (${collapsedCounts[e]})`,
    })),
    ...(nOther || kind === "other" ? [{ id: "other", label: `${tr("其他", "Other")} (${nOther})` }] : []),
  ];

  const preview = effective && (
    <ScanPreviewPanel
      entry={effectiveEntry}
      path={effective}
      flatten={flatten}
      onFlattenChange={setFlatten}
    />
  );

  const cardGrid = (files: ScanFileEntry[]) => (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-3 xl:grid-cols-4">
      {files.map((s) => (
        <ScanGridCard
          key={s.path}
          entry={s}
          selected={effective === s.path}
          // The grid never asks for `auto`: it measures each frame (~3 s the
          // first time) and a screen of cards would queue thirty measurements.
          flatten={flatten === "auto" ? "line" : flatten}
          onSelect={() => setSelected(s.path)}
        />
      ))}
    </div>
  );

  const textRows = (files: ScanFileEntry[]) => (
    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-2">
      {files.map((s) => (
        <button
          key={s.path}
          onClick={() => setSelected(s.path)}
          className={
            "rounded-md border p-2 text-left text-xs transition-colors " +
            (effective === s.path
              ? "border-mast-accent bg-mast-accent/10"
              : "border-mast-border hover:bg-mast-bg/60")
          }
        >
          <div className="flex items-baseline gap-1">
            <div className="truncate font-medium text-mast-text" title={s.path}>
              {s.name}
            </div>
            {/* Which session folder — Nanonis reuses names across sessions, so
                two rows can read `unnamed0033.sxm` and be different scans. */}
            {parentFolder(s.path) && (
              <span className="shrink-0 text-mast-faint">{parentFolder(s.path)}</span>
            )}
            {copyBadge(s) && (
              <span className="ml-auto shrink-0 text-mast-muted" title={tr("磁盘上有多个副本", "Multiple copies on disk")}>
                {copyBadge(s)}
              </span>
            )}
          </div>
          <div className="mt-1 flex justify-between text-mast-muted tabular-nums">
            <span className="uppercase">{s.ext.replace(".", "")}</span>
            <span>{fmtBytes(s.size_bytes)}</span>
          </div>
          <div className="text-mast-muted tabular-nums">{fmtMtime(s.mtime)}</div>
        </button>
      ))}
    </div>
  );

  return (
    <Section
      title={`${tr("扫描数据", "Scan data")}${totalAll ? ` (${scans.length}/${collapsed})` : ""}`}
      actions={
        <div className="flex items-center gap-2">
          {/* Preprocessing, labelling and series for the full data set live in the data gallery; this page only shows the newest files on disk right now. */}
          <Link
            to="/records/gallery"
            className="whitespace-nowrap text-xs text-mast-accent hover:underline"
            title={tr("全部数据的缩略图、自动判据、键盘打分、标签、系列与谱↔前后帧对照", "Thumbnails of all data, automatic criteria, keyboard scoring, labels, series, and spectrum-to-neighbouring-frame comparison")}
          >
            {tr("数据图库（预处理·标记·系列）→", "Data gallery (preprocess · label · series) →")}
          </Link>
          <div className="inline-flex overflow-hidden rounded-mast-ctl border border-mast-border">
            {VIEWS.map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                className={
                  "px-2.5 py-1 text-xs " +
                  (view === v
                    ? "bg-mast-accent-soft font-medium text-mast-accent"
                    : "text-mast-muted hover:text-mast-text")
                }
              >
                {VIEW_LABEL[v]}
              </button>
            ))}
          </div>
          <Button
            variant="primary"
            onClick={() => {
              resetPaging();
              q.refetch();
              setSelected(null);
            }}
          >
            {tr("Load Latest Scan", "Load latest scan")}
          </Button>
        </div>
      }
    >
      {/* Type filter. This tab mixes .sxm topography with .dat spectra and
          whatever generic numeric text happens to sit in a search dir; without
          the filter the newest file wins the whole screen. */}
      {chips.length > 2 && (
        <div className="mb-2 flex flex-wrap items-center gap-1">
          {chips.map((c) => (
            <button
              key={c.id}
              onClick={() => {
                setKind(c.id);
                resetPaging();
              }}
              className={
                "rounded-mast-badge border px-2 py-0.5 text-xs " +
                (kind === c.id
                  ? "border-mast-accent bg-mast-accent-soft font-medium text-mast-accent"
                  : "border-mast-border text-mast-muted hover:text-mast-text")
              }
            >
              {c.label}
            </button>
          ))}
          {/* Thumbnail processing, for every card at once. The panel on the
              right has its own switch including "auto"; this one is what the grid
              renders with. */}
          {view !== "list" && (
            <span className="ml-auto flex items-center gap-1 text-xs text-mast-muted">
              {tr("缩略图", "Thumbnails")}
              <span className="inline-flex overflow-hidden rounded-mast-ctl border border-mast-border">
                {GRID_FLATTEN_MODES.map((m) => (
                  <button
                    key={m}
                    onClick={() => setFlatten(m)}
                    className={
                      "px-2 py-0.5 " +
                      (flatten === m
                        ? "bg-mast-accent-soft font-medium text-mast-accent"
                        : "text-mast-muted hover:text-mast-text")
                    }
                  >
                    {flattenLabel(m)}
                  </button>
                ))}
              </span>
            </span>
          )}
        </div>
      )}
      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} />}
      {q.data?.degraded && <DegradedNote what={tr("扫描数据", "scan data")} />}
      {q.data?.search_dirs && q.data.search_dirs.length > 0 && (
        <div className="mb-2 text-xs text-mast-muted">
          {tr("搜索目录：", "Search directories: ")}{q.data.search_dirs.join(" · ")}
        </div>
      )}

      {/* Empty state: icon + title + description. */}
      {q.data && !q.data.degraded && totalAll === 0 && (
        <div className="rounded-md border border-mast-border bg-mast-bg/40 px-4 py-8 text-center">
          <div className="text-3xl">📡</div>
          <div className="mt-2 font-medium text-mast-text">{tr("暂无扫描数据", "No scan data")}</div>
          <div className="mt-1 text-sm text-mast-muted">
            {tr("扫描文件（.sxm, .dat, .3ds）会自动出现在这里。", "Scan files (.sxm, .dat, .3ds) will appear here automatically.")}
          </div>
        </div>
      )}

      {totalAll > 0 && scans.length === 0 && (
        <EmptyNote
          label={tr(
            `当前筛选（${kind}）下没有文件；共 ${totalEntries} 项。点「全部」看所有。`,
            `No files under the current filter (${kind}); ${totalEntries} in total. Click "All" to see everything.`,
          )}
        />
      )}

      {view === "groups" ? (
        <ScanGroupBrowser
          scans={scans}
          scansDegraded={q.data?.degraded ?? false}
          flatten={flatten === "auto" ? "line" : flatten}
          selected={effective}
          onSelect={setSelected}
        />
      ) : (
        scans.length > 0 && (
          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,440px)]">
            {/* Preview first in the DOM on narrow screens (order-first), second
                on wide ones — so it is never below a long list either way. */}
            {/* Sticky at every width, not only >= lg: below 1024 px the layout is a
                single column, and without sticky the preview would scroll off the
                top before the list is reached. `max-h`+`overflow` on the wrapper
                keeps a tall preview from eating the whole viewport on a short
                window. */}
            <div className="order-first max-h-[52vh] overflow-y-auto sticky top-0 z-10 bg-mast-panel pb-2 lg:order-last lg:max-h-none lg:overflow-visible lg:top-4 lg:self-start">
              <div className="mb-1 text-xs font-medium text-mast-muted">{tr("预览", "Preview")}</div>
              {preview}
            </div>

            <div className="max-h-[70vh] overflow-y-auto pr-1">
              {groups.map(({ group, files }) => (
                <div key={group.id} className="mb-3 last:mb-0">
                  {groups.length > 1 && (
                    <div
                      title={group.hint}
                      className="mb-1 flex items-baseline gap-2 text-xs font-medium text-mast-muted"
                    >
                      {group.label}
                      <span className="font-normal text-mast-faint tabular-nums">
                        {files.length}
                      </span>
                    </div>
                  )}
                  {view === "grid" ? cardGrid(files) : textRows(files)}
                </div>
              ))}

              <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-mast-faint">
                <span>{pageSummary(scans.length, collapsed, matched)}</span>
                {scans.length < collapsed && (
                  <Button
                    onClick={() => setOffset(scans.length)}
                    disabled={q.isFetching}
                  >
                    {q.isFetching ? tr("加载中…", "Loading…") : tr("加载更多", "Load more")}
                  </Button>
                )}
              </div>
            </div>
          </div>
        )
      )}
    </Section>
  );
}
