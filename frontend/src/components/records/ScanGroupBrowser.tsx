import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { components } from "@/api/schema";
import { DegradedNote, EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { displayPath, fileName, groupBySample, groupSummary } from "@/lib/scanAttribution";
import { isUnattributed } from "@/lib/scanCopies";
import { tr } from "@/lib/i18n";
import { ScanGridCard } from "./ScanGridCard";
import type { FlattenMode } from "./scanPreviewQuery";

// Browse by experiment / sample.
//
// The Data tab lists what is on disk, newest first, and a path alone cannot say
// which experiment a file belongs to. That question is answered by the v2
// `file_locations` table, which the ingest writes as it copies files into each
// experiment folder; `/api/scans/attribution` reads it.
//
// The "unassigned" section at the bottom is not a leftovers bin to be tidied
// away: raw session data that no experiment has claimed is normal (a scan taken
// before an experiment was started, or with the ingest off), and hiding it would
// make this view show less than the disk holds.

type ScanFileEntry = components["schemas"]["ScanFileEntry"];
type AttributionEntry = components["schemas"]["FileAttributionEntry"];

function useExperiments() {
  return useQuery({
    queryKey: ["experiments", "list"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/experiments");
      if (error) throw error;
      return data;
    },
  });
}

function useAttribution(experimentId: string | null) {
  return useQuery({
    enabled: !!experimentId,
    queryKey: ["scans", "attribution", experimentId],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/scans/attribution", {
        params: { query: { experiment_id: experimentId as string } },
      });
      if (error) throw error;
      return data;
    },
  });
}

/** An attribution row rendered by the same card the flat view uses. */
function asScanEntry(row: AttributionEntry): ScanFileEntry | null {
  const path = displayPath(row);
  if (!path) return null;
  const name = fileName(row);
  const dot = name.lastIndexOf(".");
  return {
    path,
    name,
    ext: dot >= 0 ? name.slice(dot).toLowerCase() : "",
    // No mtime: this row came from the ledger, and stat-ing every file would be
    // a directory walk per render. `null` means "not read".
    mtime: null,
    size_bytes: row.size_bytes ?? 0,
    kind: row.root_kind === "quarantine" ? "quarantine" : "experiment",
    copies: 1,
    locations: [],
  };
}

export function ScanGroupBrowser({
  scans,
  scansDegraded,
  flatten,
  selected,
  onSelect,
}: {
  /** The flat listing, for the "unassigned" section. */
  scans: ScanFileEntry[];
  scansDegraded: boolean;
  flatten: FlattenMode;
  selected: string | null;
  onSelect: (path: string) => void;
}) {
  const [expId, setExpId] = useState<string | null>(null);
  const exps = useExperiments();

  const experiments = exps.data?.experiments ?? [];
  // Default to the newest experiment rather than making the operator pick one
  // to see anything. `active` is derived, so the query below is called exactly
  // once per render — no conditional hooks.
  const active = expId ?? experiments[0]?.id ?? null;
  const shown = useAttribution(active);

  const groups = useMemo(
    () => groupBySample((shown.data?.files ?? []) as AttributionEntry[]),
    [shown.data],
  );
  const orphans = useMemo(() => scans.filter(isUnattributed), [scans]);

  const unreadable = (exps.data?.degraded ?? false) || exps.isError;

  return (
    <div className="space-y-4">
      {exps.isPending && <Spinner />}
      {exps.isError && <ErrorNote error={exps.error} />}
      {exps.data?.degraded && <DegradedNote what={tr("实验列表", "experiment list")} />}

      {!unreadable && experiments.length === 0 && (
        <EmptyNote
          label={tr(
            "还没有实验。数据仍然可以在「网格」「列表」视图里按时间浏览。",
            "No experiments yet. Data can still be browsed by time in the Grid and List views.",
          )}
        />
      )}

      {experiments.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {experiments.map((e) => (
            <button
              key={e.id}
              onClick={() => setExpId(e.id)}
              title={e.goal || undefined}
              className={
                "rounded-mast-badge border px-2 py-0.5 text-xs " +
                (active === e.id
                  ? "border-mast-accent bg-mast-accent-soft font-medium text-mast-accent"
                  : "border-mast-border text-mast-muted hover:text-mast-text")
              }
            >
              {e.name || e.id.slice(0, 8)}
              {e.sample_name ? ` · ${e.sample_name}` : ""}
            </button>
          ))}
        </div>
      )}

      {active && (
        <div className="space-y-3">
          {shown.isPending && <Spinner />}
          {shown.isError && <ErrorNote error={shown.error} />}
          {shown.data?.degraded && <DegradedNote what={tr("文件归属", "file attribution")} />}
          {shown.data && !shown.data.degraded && groups.length === 0 && (
            <EmptyNote
              label={tr(
                "这个实验名下没有登记文件。自动拷贝把文件收进实验文件夹时才会登记；拷贝关掉、或文件是拷贝功能之前采的，就不会出现在这里。",
                "No files are registered under this experiment. Files are registered when automatic copying moves them into the experiment folder; if copying is off, or the files predate the copy feature, they do not appear here.",
              )}
            />
          )}
          {groups.map((g) => (
            <div key={g.sampleId ?? "__none__"}>
              <div className="mb-1 flex items-baseline gap-2 text-xs font-medium text-mast-muted">
                {g.sampleId
                  ? `${tr("样品", "Sample")} ${g.sampleId.slice(0, 8)}`
                  : tr("未指明样品", "No sample specified")}
                <span className="font-normal text-mast-faint tabular-nums">
                  {groupSummary(g)}
                </span>
              </div>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
                {g.files.map((row) => {
                  const entry = asScanEntry(row);
                  if (!entry) return null;
                  return (
                    <ScanGridCard
                      key={`${row.sha256}:${row.rel_path}`}
                      entry={entry}
                      selected={selected === entry.path}
                      flatten={flatten}
                      onSelect={() => onSelect(entry.path)}
                    />
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}

      <div>
        <div className="mb-1 flex items-baseline gap-2 text-xs font-medium text-mast-muted">
          {tr("未归属（原始会话数据）", "Unassigned (raw session data)")}
          <span className="font-normal text-mast-faint tabular-nums">{orphans.length}</span>
        </div>
        {scansDegraded ? (
          <DegradedNote what={tr("磁盘文件列表", "disk file list")} />
        ) : orphans.length === 0 ? (
          <EmptyNote label={tr("当前这一页里的文件都已经收进实验文件夹。", "All files on this page have been moved into experiment folders.")} />
        ) : (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
            {orphans.map((s) => (
              <ScanGridCard
                key={s.path}
                entry={s}
                selected={selected === s.path}
                flatten={flatten}
                onSelect={() => onSelect(s.path)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
