import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { components } from "@/api/schema";
import { Badge, Card, DegradedNote, EmptyNote, ErrorNote, Section, Spinner } from "@/components/ui";
import { Button, Field, RadioGroup, SelectField } from "@/components/controls";
import { EnvSeriesChart } from "@/components/envhistory/EnvSeriesChart";
import { SpectrumViewer, type SpectrumLike } from "@/components/envhistory/SpectrumViewer";
import {
  RANGES,
  channelLabel,
  coverageOf,
  fmtBytes,
  fmtSpan,
  fmtTs,
  rangeWindow,
  spectrumPickerLabel,
  statusLabel,
  statusTone,
  worstOf,
} from "@/lib/envHistory";
import { ENV_HISTORY_SERIES_POLL_MS, ENV_HISTORY_STATUS_POLL_MS } from "@/lib/pollRates";
import { useStickyTab } from "@/hooks/useStickyTab";
import { tr } from "@/lib/i18n";

type Status = components["schemas"]["EnvHistoryStatus"];
type Series = components["schemas"]["EnvSeriesResponse"];
type SpectraList = components["schemas"]["SpectraListResponse"];

// Environment history — the long-term record of the things that are true of the machine
// rather than of the measurement: temperature, vacuum, helium, field, the
// current the tip sits at while nobody is driving it, and the noise spectra.
//
// ── where each number comes from ───────────────────────────────────────────
// Nothing on this page touches hardware. The recorder aggregates the existing
// 2-second environment stream into permanent one-minute buckets and archives a
// spectrum every half hour off the current monitor's segment stream; these
// endpoints read that SQLite store and nothing else. Watching a year of
// temperature costs the instrument zero packets.
//
// ── what the curves mean ───────────────────────────────────────────────────
// Every point is a bucket, drawn as a mean line inside its true min/max band.
// When the server merges buckets for a wide window it re-aggregates by weight —
// the extremes and the worst status survive the merge, so zooming out can never
// hide an excursion that really happened.
//
// Tunnelling current is gated: only readings taken while nothing was driving the
// instrument are counted, because during a scan that signal is topography, not
// environment. The gaps are real and `instrument_quiet` explains them.

const STATUS_KEY = ["env-history", "status"] as const;

/**
 * A series' unit, from the sensor list.
 *
 * Used to decide what may be overlaid on what: only same-unit series are
 * offered as a comparison, because they are the only ones that can honestly
 * share one Y axis.
 */
function unitOf(list: { sensors?: { sensor: string; unit?: string | null }[] } | undefined,
                name: string): string {
  return list?.sensors?.find((s) => s.sensor === name)?.unit ?? "";
}

/** Series the operator most likely wants first, in order of preference. */
const PREFERRED = ["temperature", "vacuum", "helium_level", "tunnel_current"];

type View = "series" | "spectra";

const VIEWS: { value: View; label: string }[] = [
  { value: "series", label: tr("趋势", "Trends") },
  { value: "spectra", label: tr("噪声谱", "Noise spectra") },
];

export default function EnvHistoryPage() {
  // The noise-spectrum view is one operators look at, leave, and come back to, so
  // the selected view is remembered. This page's switcher is a RadioGroup, not
  // SubTabs, so the SubTabs memory mechanism does not apply.
  const [view, setView] = useStickyTab<View>(
    "env-history.view", VIEWS.map((v) => v.value), "series");
  const [sensor, setSensor] = useState<string>("");
  // Comparison series. The backend reads all sensors in one tick and stores them
  // on one time base, so two curves should land on the same bucket grid. Showing
  // one curve at a time and flipping between them would instead look like two
  // non-overlapping data sets (a symptom of serial-port handle contention, not of
  // scheduling); overlaying them makes simultaneity visible.
  const [compareSensor, setCompareSensor] = useState<string>("");
  // Stores the span itself, not an index into RANGES: an index-based default
  // silently changes meaning when an entry is inserted. `null` means "all" and
  // is a valid value.
  const [rangeS, setRangeS] = useState<number | null>(86400);
  const [channel, setChannel] = useState<string>("current");
  const [primaryId, setPrimaryId] = useState<number | null>(null);
  const [compareId, setCompareId] = useState<number | null>(null);

  const status = useQuery({
    queryKey: STATUS_KEY,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/env-history/status");
      if (error) throw error;
      return data as Status;
    },
    refetchInterval: ENV_HISTORY_STATUS_POLL_MS,
  });

  const sensors = useQuery({
    queryKey: ["env-history", "sensors"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/env-history/sensors");
      if (error) throw error;
      return data;
    },
    refetchInterval: ENV_HISTORY_SERIES_POLL_MS,
  });

  const names = useMemo(
    () => (sensors.data?.sensors ?? []).map((s) => s.sensor).filter(Boolean),
    [sensors.data],
  );

  // Pick a default series once the list arrives, preferring the ones an
  // operator actually opens this page for.
  useEffect(() => {
    if (sensor || !names.length) return;
    const pick = PREFERRED.find((p) => names.includes(p)) ?? names[0];
    if (pick) setSensor(pick);
  }, [names, sensor]);

  const window = useMemo(() => rangeWindow(rangeS, Date.now()), [rangeS]);

  const series = useQuery({
    queryKey: ["env-history", "series", sensor, window.since ?? "all"],
    enabled: !!sensor,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/env-history/series", {
        params: { query: { sensor, since: window.since, max_points: 2000 } },
      });
      if (error) throw error;
      return data as Series;
    },
    refetchInterval: ENV_HISTORY_SERIES_POLL_MS,
  });

  // Fetch the second series with the same window and parameters; only then are the two curves comparable.
  const compareSeries = useQuery({
    queryKey: ["env-history", "series", compareSensor, window.since ?? "all"],
    enabled: !!compareSensor && compareSensor !== sensor,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/env-history/series", {
        params: { query: { sensor: compareSensor, since: window.since, max_points: 2000 } },
      });
      if (error) throw error;
      return data as Series;
    },
    refetchInterval: ENV_HISTORY_SERIES_POLL_MS,
  });

  const spectra = useQuery({
    queryKey: ["env-history", "spectra", channel],
    enabled: view === "spectra",
    queryFn: async () => {
      const { data, error } = await api.GET("/api/env-history/spectra", {
        params: { query: { channel, limit: 200 } },
      });
      if (error) throw error;
      return data as SpectraList;
    },
    refetchInterval: ENV_HISTORY_SERIES_POLL_MS,
  });

  const metas = spectra.data?.spectra ?? [];
  useEffect(() => {
    if (!metas.length) {
      setPrimaryId(null);
      return;
    }
    const newest = metas[0];
    if (newest && (primaryId == null || !metas.some((m) => m.id === primaryId))) {
      setPrimaryId(newest.id); // newest first from the backend
    }
  }, [metas, primaryId]);

  const primary = useSpectrum(primaryId);
  const compare = useSpectrum(compareId);

  const st = status.data;
  const degraded = !!st?.degraded;
  const points = series.data?.points ?? [];
  const worst = worstOf(points);
  const cover = coverageOf(points);
  const unit = series.data?.unit ?? "";
  // Only offer comparison series with the same unit. Kelvin against kelvin is
  // comparable; kelvin against pascal would need a second Y axis (whose relative
  // height is the plotter's choice and suggests correlations nobody measured) or
  // forcing two quantities onto one axis, so such options are not offered.
  const comparePoints =
    compareSensor && compareSensor !== sensor ? (compareSeries.data?.points ?? []) : [];
  const compareCover = coverageOf(comparePoints);

  return (
    <div className="p-5">
      <Section
        title={tr("环境历史", "Environment history")}
        subtitle={tr(
          "温度 / 真空 / 液氦 / 磁场 / 隧道电流的长期记录，以及噪声谱快照。全部读本地库，不占用仪器通信。",
          "Long-term record of temperature / vacuum / liquid helium / field / tunnelling current, plus noise-spectrum snapshots. Everything is read from the local store and uses no instrument communication.",
        )}
        actions={
          <RadioGroup value={view} onChange={setView} options={VIEWS} />
        }
      >
        {degraded && <DegradedNote what={tr("环境历史", "Environment history")} />}
        <RecorderStatusCard status={st} loading={status.isLoading} />
      </Section>

      {view === "series" ? (
        <Section title={tr("趋势", "Trends")}>
          <Card>
            <div className="mb-3 flex flex-wrap items-end gap-3">
              <Field label={tr("序列", "Series")}>
                <SelectField
                  value={sensor}
                  onChange={setSensor}
                  options={names.map((n) => ({ value: n, label: n }))}
                />
              </Field>
              <Field label={tr("叠加对比", "Overlay comparison")}>
                <SelectField
                  value={compareSensor}
                  onChange={setCompareSensor}
                  options={[
                    { value: "", label: tr("（不对比）", "(no comparison)") },
                    ...names
                      .filter((n) => n !== sensor && unitOf(sensors.data, n) === unit)
                      .map((n) => ({ value: n, label: n })),
                  ]}
                />
              </Field>
              <div>
                <div className="mb-1 text-xs text-mast-faint">{tr("时间范围", "Time range")}</div>
                <div className="flex flex-wrap gap-1">
                  {RANGES.map((r) => (
                    <Button
                      key={r.label}
                      variant={r.s === rangeS ? "primary" : "ghost"}
                      onClick={() => setRangeS(r.s)}
                    >
                      {r.label}
                    </Button>
                  ))}
                </div>
              </div>
              <div className="ml-auto flex items-center gap-2 text-xs">
                {/* Tone follows severity: "unavailable" (the mildest non-ok) is not drawn as red as "alarm". */}
                {worst !== "ok" && <Badge tone={statusTone(worst)}>{statusLabel(worst)}</Badge>}
                {cover.excluded > 0 && (
                  <span
                    className="text-mast-faint"
                    title={tr(
                      "被排除的读数：传感器当时没给出有效值（多个通道共用一个串口时，" +
                        "同一时刻只有一个拿得到句柄，另一个就是空的），" +
                        "或者（隧道电流）仪器当时正在被驱动 —— 那种情况下这个信号是形貌不是环境。" +
                        "它们不进统计。",
                      "Excluded readings: the sensor returned no valid value at the time " +
                        "(when several channels share one serial port only one can hold the handle " +
                        "at a time and the other reads empty), or (tunnelling current) the instrument " +
                        "was being driven, in which case the signal is topography, not environment. " +
                        "They are not counted in the statistics.",
                    )}
                  >
                    {tr(
                      `已计入 ${cover.counted} 条 · 未计入 ${cover.excluded} 条`,
                      `${cover.counted} counted · ${cover.excluded} excluded`,
                    )}
                  </span>
                )}
              </div>
            </div>

            {/* Counted / excluded for both series side by side answer "are they saved
                at the same time?". With a shared serial port only one channel holds the
                handle at a time, so the two sides come out exactly complementary; both
                counts growing means both channels are recording. */}
            {compareSensor && compareSensor !== sensor && (
              <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-mast-muted">
                <span>
                  {sensor}{tr("：已计入 ", ": counted ")}
                  <span className="tabular-nums">{cover.counted}</span>
                  {tr(" · 未计入 ", " · excluded ")}
                  <span className="tabular-nums">{cover.excluded}</span>
                </span>
                <span>
                  {compareSensor}{tr("：已计入 ", ": counted ")}
                  <span className="tabular-nums">{compareCover.counted}</span>
                  {tr(" · 未计入 ", " · excluded ")}
                  <span className="tabular-nums">{compareCover.excluded}</span>
                </span>
                {cover.counted > 0 && compareCover.counted > 0 ? (
                  <span className="text-mast-auto">{tr("两路都在记录", "Both channels are recording")}</span>
                ) : (
                  <span className="text-mast-warn">
                    {tr(
                      "只有一路有读数——多为共用串口时另一路拿不到句柄",
                      "Only one channel has readings, usually because the other could not get the shared serial port handle",
                    )}
                  </span>
                )}
              </div>
            )}

            {series.isLoading && <Spinner />}
            {series.error != null && <ErrorNote error={series.error} />}
            {!series.isLoading && !series.error && !names.length && (
              <EmptyNote
                label={tr(
                  "还没有任何历史。记录器需要跑够一个统计桶（默认一分钟）才会出现第一个点。",
                  "No history yet. The recorder needs to fill one statistics bucket (one minute by default) before the first point appears.",
                )}
              />
            )}
            {/* Buckets exist but nothing is drawable: "this sensor gave no valid reading in
                this period" is a fact to state, not an empty state. */}
            {!series.isLoading && !series.error && !!names.length && !!sensor &&
              cover.buckets > 0 && cover.drawable === 0 && (
                <EmptyNote
                  label={tr(
                    `${sensor} 在这个时间范围内没有任何有效读数（${cover.buckets} 个统计桶，` +
                      `${cover.excluded} 条读数全部被排除）。记录器一直在跑 —— 是这个传感器没读到值。`,
                    `${sensor} has no valid readings in this time range (${cover.buckets} statistics buckets, ` +
                      `all ${cover.excluded} readings excluded). The recorder is running; the sensor simply returned no values.`,
                  )}
                />
              )}
            {!series.isLoading && !series.error && !!names.length && (
              <EnvSeriesChart
                sensor={sensor}
                unit={series.data?.unit ?? ""}
                points={points}
                bucketS={series.data?.bucket_s_effective ?? 60}
                thinned={!!series.data?.thinned}
                compare={
                  compareSensor && compareSensor !== sensor
                    ? { sensor: compareSensor, points: comparePoints }
                    : null
                }
              />
            )}
          </Card>
        </Section>
      ) : (
        <Section title={tr("噪声谱", "Noise spectra")}>
          <Card>
            <div className="mb-3 flex flex-wrap items-end gap-3">
              <Field label={tr("通道", "Channel")}>
                <SelectField
                  value={channel}
                  onChange={setChannel}
                  options={[
                    { value: "current", label: channelLabel("current") },
                    { value: "z", label: channelLabel("z") },
                  ]}
                />
              </Field>
              <Field label={tr("快照", "Snapshot")}>
                <SelectField
                  value={primaryId == null ? "" : String(primaryId)}
                  onChange={(v) => setPrimaryId(v ? Number(v) : null)}
                  options={metas.map((m) => ({
                    value: String(m.id),
                    label: spectrumPickerLabel(m, fmtTs),
                  }))}
                />
              </Field>
              <Field label={tr("对比", "Compare")}>
                <SelectField
                  value={compareId == null ? "" : String(compareId)}
                  onChange={(v) => setCompareId(v ? Number(v) : null)}
                  options={[
                    { value: "", label: tr("（不对比）", "(no comparison)") },
                    ...metas
                      .filter((m) => m.id !== primaryId)
                      .map((m) => ({
                        value: String(m.id),
                        label: spectrumPickerLabel(m, fmtTs),
                      })),
                  ]}
                />
              </Field>
            </div>

            {spectra.isLoading && <Spinner />}
            {spectra.error != null && <ErrorNote error={spectra.error} />}
            {!spectra.isLoading && !spectra.error && (
              <SpectrumViewer primary={primary} compare={compare} />
            )}
            {channel === "z" && !metas.length && (
              <p className="mt-2 text-xs text-mast-faint">
                {tr(
                  "Z 噪声谱默认关闭（设置 → 环境历史 → 记录 Z 噪声谱）。它是这套记录里唯一会主动占用仪器通信的部分，需要 Osci2T 双通道模块。",
                  "The Z noise spectrum is off by default (Settings → Environment history → Record Z noise spectrum). It is the only part of this recording that actively uses instrument communication, and it needs the Osci2T dual-channel module.",
                )}
              </p>
            )}
          </Card>
        </Section>
      )}
    </div>
  );
}

function useSpectrum(id: number | null) {
  const q = useQuery({
    queryKey: ["env-history", "spectrum", id],
    enabled: id != null,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/env-history/spectra/{spectrum_id}", {
        params: { path: { spectrum_id: id as number } },
      });
      if (error) throw error;
      return data;
    },
    // A stored spectrum never changes — fetch once and keep it, so flipping
    // back and forth between two snapshots to compare them is instant.
    staleTime: Infinity,
  });
  const d = q.data;
  if (!d || !d.found) return null;
  return {
    id: d.id,
    ts: d.ts,
    channel: d.channel,
    unit: d.unit,
    fs_hz: d.fs_hz,
    span_s: d.span_s,
    n_segments: d.n_segments,
    freqs_hz: d.freqs_hz ?? [],
    psd: d.psd ?? [],
  } satisfies SpectrumLike;
}

function RecorderStatusCard({ status, loading }: { status?: Status; loading: boolean }) {
  if (loading) return <Spinner />;
  if (!status) return null;
  const sink = (status.sink ?? {}) as Record<string, unknown>;
  const store = (status.store ?? {}) as Record<string, unknown>;
  const sweep = (status.sweep ?? {}) as Record<string, unknown>;
  const cur = status.spectra?.current;
  const z = status.spectra?.z;
  const disabled = !!sink.disabled;

  return (
    <Card className="mt-3">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <span className="flex items-center gap-2">
          <Badge tone={status.recording ? "AUTO" : "INFO"}>
            {status.recording
              ? tr("记录中", "Recording")
              : status.enabled
                ? tr("已暂停", "Paused")
                : tr("已关闭", "Off")}
          </Badge>
          {disabled && (
            <span
              className="text-mast-warn"
              title={tr(
                "写入失败后自动静默 60 秒，随后自行恢复。实时监控与 CSV 不受影响。",
                "After a write failure, writing is silenced for 60 s and then resumes by itself. Live monitoring and CSV are unaffected.",
              )}
            >
              {tr("写入暂时禁用", "Writing temporarily disabled")}
            </span>
          )}
        </span>
        <Stat label={tr("聚合粒度", "Aggregation")} value={fmtSpan(status.bucket_s ?? 0)} />
        <Stat label={tr("统计桶", "Buckets")} value={tr(`${num(store.bucket_rows)} 条`, `${num(store.bucket_rows)} rows`)} />
        <Stat label={tr("噪声谱", "Noise spectra")} value={tr(`${num(store.spectra_rows)} 条`, `${num(store.spectra_rows)} rows`)} />
        <Stat label={tr("库大小", "Store size")} value={fmtBytes(num(store.db_bytes))} />
        {cur && (
          <Stat
            label={tr("电流谱", "Current spectrum")}
            value={
              cur.last_ts
                ? tr(
                    `${fmtTs(cur.last_ts)}（攒 ${cur.n_accum ?? 0} 段）`,
                    `${fmtTs(cur.last_ts)} (${cur.n_accum ?? 0} segments accumulated)`,
                  )
                : tr(`攒 ${cur.n_accum ?? 0} 段`, `${cur.n_accum ?? 0} segments accumulated`)
            }
            hint={
              cur.insufficient_quiet
                ? tr(
                    "窗口早已到点但安静段一直不够——仪器长时间在忙。窗口会自动延长。",
                    "The window is overdue but there have not been enough quiet segments because the instrument has been busy. The window extends automatically.",
                  )
                : undefined
            }
          />
        )}
        {status.z_enabled && z && (
          <Stat
            label={tr("Z 谱", "Z spectrum")}
            value={
              z.last_ts
                ? fmtTs(z.last_ts)
                : String(
                    (z.last_result as Record<string, unknown> | undefined)?.skipped ??
                      tr("等待安静窗口", "waiting for a quiet window"),
                  )
            }
          />
        )}
        {sweep.cutoff != null && (
          <Stat
            label={tr("上次清扫", "Last sweep")}
            value={tr(`删 ${num(sweep.rows_pruned)} 条`, `${num(sweep.rows_pruned)} rows removed`)}
            hint={tr(
              `早于 ${String(sweep.cutoff).slice(0, 19)} 的逐条读数已清理（告警行永久保留）`,
              `Per-reading rows older than ${String(sweep.cutoff).slice(0, 19)} were removed (alert rows are kept permanently)`,
            )}
          />
        )}
      </div>
    </Card>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <span className="flex items-baseline gap-1.5" title={hint}>
      <span className="text-xs text-mast-faint">{label}</span>
      <span className="tabular-nums">{value}</span>
      {hint && <span className="text-xs text-mast-warn">*</span>}
    </span>
  );
}

function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}
