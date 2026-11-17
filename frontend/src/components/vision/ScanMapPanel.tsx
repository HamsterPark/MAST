import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { tr } from "@/lib/i18n";
import { api } from "@/api/client";
import { Card, Spinner, ErrorNote, DegradedNote, EmptyNote } from "@/components/ui";
import { ScanMapCanvas, ScanMapLegend, type FocusRequest } from "@/components/vision/ScanMapCanvas";
import { ScanMapControls } from "@/components/vision/ScanMapControls";
import { ScanMapMarkerList, type MarkerRow } from "@/components/vision/ScanMapMarkerList";
import { ScanMapTimeBar } from "@/components/vision/ScanMapTimeBar";
import { useLiveScan } from "@/components/vision/LiveScanLayer";
import { linesDoneLabel } from "@/lib/scanLive";
import { useCurrentScope } from "@/api/scope";
import { useUiStore } from "@/store";
import { useNow } from "@/hooks/useNow";
import {
  MAX_ANALYSIS_AGE_MS,
  analysisAutoEnabled,
  analysisSignature,
  drawnHistory,
  kindCounts,
  markerKeys,
  nextAnalysisDelayMs,
} from "@/lib/scanMapView";
import {
  isAllTime,
  latestActionIndex,
  parseStamp,
  resolveWindow,
  summarize,
  timeDomain,
  windowLabel,
  type TimeFilter,
} from "@/lib/scanMapTime";

// The scan map, everywhere it appears. Two pages embed it (the vision page and
// the chat page's vision buffer); sharing one component keeps them in step —
// private copies of the queries once left one page without the analysis overlay.
// One component, one set of queries (TanStack dedupes them by key), one set of
// layer switches out of the shared store.

/** GET /api/scan-map — the live map, 3 s poll. */
export function useScanMap() {
  return useQuery({
    queryKey: ["scan-map"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/scan-map");
      if (error) throw error;
      return data;
    },
    refetchInterval: 3000,
  });
}

/**
 * GET /api/scan-map/analysis — coverage, keep-out zones, where to go next.
 *
 * Never on an interval. `analyze_map` rasterises the whole piezo area (~1 s),
 * which is exactly why the backend keeps it off the map poll — every fetch here
 * is an explicit decision, either the operator's button or the refresh effect
 * below reacting to the record actually changing.
 */
export function useMapAnalysis() {
  return useQuery({
    queryKey: ["scan-map", "analysis"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/scan-map/analysis");
      if (error) throw error;
      return data;
    },
    enabled: false,
    staleTime: Infinity,
  });
}

/**
 * Keep the analysis overlays honest without polling for them.
 *
 * Analysis layers that only refresh on a manual "run analysis" press would show
 * keep-out discs and a recommendation computed before the latest work — worse
 * than showing nothing, because it looks current. This refetches when the map poll shows the record actually changed
 * (a marker recorded, a coarse move, a route published or advanced), throttled
 * so a burst of markers collapses into one analysis, plus a slow floor for the
 * inputs that produce no marker at all (the live scan size feeds frame_size_m).
 */
function useAutoAnalysis(
  map: ReturnType<typeof useScanMap>["data"],
  analysisQ: ReturnType<typeof useMapAnalysis>,
) {
  const layers = useUiStore((s) => s.mapLayers);
  const wanted = analysisAutoEnabled(layers) && !!map && !map.degraded;
  const signature = analysisSignature(map);
  const refetch = analysisQ.refetch;
  const lastFetchedAt = analysisQ.dataUpdatedAt;
  const lastSig = useRef<string | null>(null);

  useEffect(() => {
    if (!wanted) return;
    const stale = lastFetchedAt > 0 && Date.now() - lastFetchedAt > MAX_ANALYSIS_AGE_MS;
    if (lastSig.current === signature && !stale) return;

    const wait = nextAnalysisDelayMs(Date.now(), lastFetchedAt || null);
    if (wait <= 0) {
      lastSig.current = signature;
      void refetch();
      return;
    }
    // Inside the throttle window: defer rather than drop, so the state AFTER
    // the burst is what ends up on screen.
    const t = setTimeout(() => {
      lastSig.current = signature;
      void refetch();
    }, wait);
    return () => clearTimeout(t);
  }, [wanted, signature, lastFetchedAt, refetch]);
}

export function ScanMapPanel({
  compact = false,
  importer,
  analysisPanel,
}: {
  /** Tighter layout for an embedded view (no per-layer hint text). */
  compact?: boolean;
  /** Optional importer row, rendered above the map (vision page only). */
  importer?: ReactNode;
  /** Optional analysis panel, given the shared query state (vision page only). */
  analysisPanel?: (a: {
    analysis: ReturnType<typeof useMapAnalysis>["data"];
    onAnalyze: () => void;
    isFetching: boolean;
    isError: boolean;
    error: unknown;
  }) => ReactNode;
}) {
  const q = useScanMap();
  const map = q.data;
  const analysisQ = useMapAnalysis();
  useAutoAnalysis(map, analysisQ);

  const layers = useUiStore((s) => s.mapLayers);
  const viewMode = useUiStore((s) => s.mapViewMode);

  // The time window is per view and starts at "all" on every visit: persisted,
  // a "last 1 h" set yesterday would open today's map looking empty.
  const [timeFilter, setTimeFilter] = useState<TimeFilter>({ kind: "all" });
  // "Last 1 h" slides with the clock even when no new marker arrives.
  const now = useNow(30_000);
  const timeWindow = useMemo(() => resolveWindow(timeFilter, now), [timeFilter, now]);

  const markers = useMemo(() => map?.markers ?? [], [map?.markers]);
  const keys = useMemo(() => markerKeys(markers), [markers]);
  // Executed operations that can be drawn at all — the population the time
  // bar summarises and the legend totals, independent of the layer switches.
  const executed = useMemo(
    () =>
      markers.filter(
        (m) =>
          (m.status ?? "") !== "planned" &&
          m.x_m != null && m.y_m != null && Number.isFinite(m.x_m) && Number.isFinite(m.y_m),
      ),
    [markers],
  );
  // Marker stamps carry the offset of their own date (the backend adds it), so
  // they read as the instants they are in any browser (lib/scanMapTime.ts).
  const stamps = useMemo(() => executed.map((m) => parseStamp(m.timestamp)), [executed]);
  const domain = useMemo(() => timeDomain(stamps), [stamps]);
  const summary = useMemo(() => summarize(stamps, timeWindow), [stamps, timeWindow]);
  // The same selection the canvas draws, so "STS 14" is true of the map below it.
  const drawn = useMemo(
    () => drawnHistory(markers, map?.current_epoch, layers, timeWindow),
    [markers, map?.current_epoch, layers, timeWindow],
  );
  const legendCounts = useMemo(() => {
    const shown = kindCounts(drawn.map((d) => d.m));
    const total = kindCounts(executed);
    const out: Record<string, { shown: number; total: number }> = {};
    for (const k of new Set([...Object.keys(shown), ...Object.keys(total)])) {
      out[k] = { shown: shown[k] ?? 0, total: total[k] ?? 0 };
    }
    return out;
  }, [drawn, executed]);
  const latestIndex = latestActionIndex(markers);
  const latestShown = drawn.some((d) => d.index === latestIndex);

  // The list under the map: its rows are exactly what the map draws.
  const rows = useMemo<MarkerRow[]>(
    () => drawn.map((d) => ({ key: keys[d.index] ?? String(d.index), index: d.index, m: d.m })),
    [drawn, keys],
  );
  const [highlightKey, setHighlightKey] = useState<string | null>(null);
  const [focusRequest, setFocusRequest] = useState<FocusRequest | null>(null);

  // The sample's name for the exported PNG. ScanMapResponse.sample_label is
  // never filled in by the backend; the scope endpoint is the frontend's
  // single source for "which sample is this".
  const scope = useCurrentScope();
  const sampleName = scope.data?.sample?.name || map?.sample_label || "";

  // The scan in progress, row by row (LiveHub). Subscribed only while the
  // "live scan" layer is on; the canvas falls back to the 1/8 milestone image
  // whenever this is not usable.
  const live = useLiveScan(layers.live);

  const planned = markers.filter((m) => (m.status ?? "") === "planned");
  const upcoming = analysisQ.data?.upcoming ?? [];

  return (
    <>
      {importer && <div className="mb-3">{importer}</div>}
      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} />}
      {map && map.degraded && <DegradedNote what={tr("扫描地图", "scan map")} />}
      {map && !map.degraded && (
        <div className="space-y-3" data-scanmap="panel">
          <ScanMapControls compact={compact} />
          <ScanMapTimeBar
            filter={timeFilter}
            onChange={setTimeFilter}
            timeWindow={timeWindow}
            domain={domain}
            summary={summary}
            now={now}
          />
          <ScanMapLegend
            counts={legendCounts}
            planSteps={layers.plan ? planned.length : undefined}
            showLatest={latestShown}
          />
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-mast-muted">
            {layers.plan && planned.length > 0 && (
              <span>
                {tr("计划", "Plan")}
                {map.plan_title ? ` · ${map.plan_title}` : ""} ·{" "}
                {tr(`剩 ${planned.length} 步`, `${planned.length} steps left`)}
              </span>
            )}
            {layers.upcoming && upcoming.length > 0 && (
              <span>
                {tr(
                  `候选序列 · 接下来 ${upcoming.length} 个位置`,
                  `Candidate sequence · next ${upcoming.length} positions`,
                )}
              </span>
            )}
            {layers.live && live.usable && live.status && (
              <span className="text-mast-text">
                {tr("实时扫描", "Live scan")} ·{" "}
                {tr(
                  `第 ${linesDoneLabel(live.meta, live.status.rowsDone, live.status.rows)} 行`,
                  `line ${linesDoneLabel(live.meta, live.status.rowsDone, live.status.rows)}`,
                )}
                {live.status.intervalS
                  ? tr(` · 每 ${live.status.intervalS.toFixed(1)} s`, ` · every ${live.status.intervalS.toFixed(1)} s`)
                  : ""}
                {live.status.degraded && (
                  <span className="text-mast-warn">{tr(" · 已限速", " · rate-limited")}</span>
                )}
              </span>
            )}
            {layers.live && live.usable && live.meta?.scanDir === "up" && (
              <span
                className="text-mast-warn"
                title={tr(
                  "上扫时，抓到的帧里第 0 行是画面上边还是下边，是按扫描前沿（未采到的行是 NaN）在哪一端推断的，没有在真机上核对过。若图像上下颠倒，请报告。",
                  "For an up-scan, whether row 0 of the grabbed frame is the top or the bottom of the picture is inferred from which end the scan front (the NaN rows) starts at. It has not been checked on a real controller; please report an upside-down image.",
                )}
              >
                {tr("上扫：行序为推断，未在真机核对", "Up-scan: row order inferred, not verified on hardware")}
              </span>
            )}
            {layers.live && live.lost > 0 && (
              <span className="text-mast-warn">
                {live.lostUnit === "row"
                  ? tr(`实时扫描有 ${live.lost} 行没收到`, `Live scan: ${live.lost} rows not received`)
                  : tr(`实时扫描有 ${live.lost} 个像素没收到`, `Live scan: ${live.lost} pixels not received`)}
              </span>
            )}
            {layers.live && live.status?.tooLarge && (
              <span
                className="text-mast-muted"
                title={tr(
                  "每次抓整帧要搬 4 MiB 以上，会长时间占住 data 口；这一帧不实时显示，地图照旧用 1/8 进度快照。1024×1024 及以下照常实时显示。",
                  "Grabbing the whole frame would move more than 4 MiB each time and hold the data port; this frame is not streamed and the map keeps the 1/8-progress snapshots. 1024×1024 and smaller stream as usual.",
                )}
              >
                {tr(
                  `帧太大（${live.status.tooLarge.pixels}×${live.status.tooLarge.lines}），退回 1/8 快照`,
                  `Frame too large (${live.status.tooLarge.pixels}×${live.status.tooLarge.lines}): 1/8 snapshots instead`,
                )}
              </span>
            )}
            {layers.live && live.status?.commsHold && (
              <span className="text-mast-warn">
                {tr("通信熔断中，实时扫描暂停", "Link breaker open: live scan paused")}
              </span>
            )}
            {analysisQ.isFetching && <span>{tr("分析中…", "Analyzing…")}</span>}
            {analysisQ.isError && <span className="text-mast-warn">
                {tr("分析失败（图层暂用上次结果）", "Analysis failed (layers keep the last result)")}
              </span>}
          </div>
          <div className="flex flex-wrap items-start gap-4">
            {/* flex-1 gives the canvas a width from the LAYOUT rather than from
                its own content, which is what makes measuring it safe; the
                min-w keeps it from being squeezed to nothing next to the stats
                card, and wraps below it instead.

                `basis-full` on wide screens gives the map the whole row: with
                three items sharing one flex row the map got
                `container − stats(~250) − analysis(≤420) − gaps`, about 700 px
                less than the page had. The two short cards wrap underneath. */}
            <div className="min-w-[320px] flex-1 lg:basis-full">
              <ScanMapCanvas
                map={map}
                analysis={analysisQ.data}
                layers={layers}
                viewMode={viewMode}
                timeWindow={timeWindow}
                markerKeyList={keys}
                highlightKey={highlightKey}
                focusRequest={focusRequest}
                exportName={sampleName}
                exportCaption={isAllTime(timeWindow) ? "" : windowLabel(timeWindow, domain, now)}
                live={live}
              />
              <div className="mt-3">
                <ScanMapMarkerList
                  rows={rows}
                  currentEpoch={map.current_epoch}
                  latestIndex={latestIndex}
                  highlightKey={highlightKey}
                  onHover={setHighlightKey}
                  onPick={(key) => setFocusRequest((r) => ({ key, seq: (r?.seq ?? 0) + 1 }))}
                  defaultOpen={!compact}
                />
              </div>
            </div>
            <Card className="min-w-[220px] flex-1 space-y-2 text-sm">
              <StatRow label={tr("样品", "Sample")} value={sampleName || "—"} />
              <StatRow
                label={tr("扫描框", "Scan frame")}
                value={map.frame ? fmtSize(map.frame.width_m, map.frame.height_m) : "—"}
                mono
              />
              <StatRow label={tr("针尖 X", "Tip X")} value={fmtNm(map.tip_xyz?.x_m)} mono />
              <StatRow label={tr("针尖 Y", "Tip Y")} value={fmtNm(map.tip_xyz?.y_m)} mono />
              <StatRow label={tr("针尖 Z", "Tip Z")} value={fmtNm(map.tip_xyz?.z_m)} mono />
              <StatRow label={tr("标记数", "Markers")} value={String(map.marker_count)} mono />
            </Card>
            {analysisPanel?.({
              analysis: analysisQ.data,
              onAnalyze: () => void analysisQ.refetch(),
              isFetching: analysisQ.isFetching,
              isError: analysisQ.isError,
              error: analysisQ.error,
            })}
          </div>
          {map.markers && map.markers.length === 0 && (
            <EmptyNote label={tr("尚无定位操作记录（开始实验后会逐步标在图上）。", "No positioning actions recorded yet (they appear on the map as the experiment proceeds).")} />
          )}
        </div>
      )}
    </>
  );
}

function StatRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-6">
      <span className="text-mast-muted">{label}</span>
      <span className={mono ? "tabular-nums" : undefined}>{value}</span>
    </div>
  );
}

function fmtNm(m?: number | null): string {
  if (m == null) return "—";
  return `${(m * 1e9).toLocaleString(undefined, { maximumFractionDigits: 1 })} nm`;
}

function fmtSize(w?: number | null, h?: number | null): string {
  if (w == null || h == null) return "—";
  return `${(w * 1e9).toFixed(0)}×${(h * 1e9).toFixed(0)} nm`;
}
