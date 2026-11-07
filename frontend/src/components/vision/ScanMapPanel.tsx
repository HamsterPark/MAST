import { useEffect, useRef, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { tr } from "@/lib/i18n";
import { api } from "@/api/client";
import { Card, Spinner, ErrorNote, DegradedNote, EmptyNote } from "@/components/ui";
import { ScanMapCanvas, ScanMapLegend } from "@/components/vision/ScanMapCanvas";
import { ScanMapControls } from "@/components/vision/ScanMapControls";
import { useUiStore } from "@/store";
import {
  MAX_ANALYSIS_AGE_MS,
  analysisAutoEnabled,
  analysisSignature,
  nextAnalysisDelayMs,
} from "@/lib/scanMapView";

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

  const planned = (map?.markers ?? []).filter((m) => (m.status ?? "") === "planned");
  const upcoming = analysisQ.data?.upcoming ?? [];

  return (
    <>
      {importer && <div className="mb-3">{importer}</div>}
      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} />}
      {map && map.degraded && <DegradedNote what={tr("扫描地图", "scan map")} />}
      {map && !map.degraded && (
        <div className="space-y-3">
          <ScanMapControls compact={compact} />
          <ScanMapLegend />
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
              />
            </div>
            <Card className="min-w-[220px] flex-1 space-y-2 text-sm">
              <StatRow label={tr("样品", "Sample")} value={map.sample_label || "—"} />
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
