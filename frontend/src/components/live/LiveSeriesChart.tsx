// Multi-channel curves: the default renderer for LiveHub `scalar` (time series) and
// `xy` (curves built point by point, e.g. delay vs. signal) streams. They differ only
// in the x axis: scalar uses the frame time, xy uses the x value in the frame.
//
// No dual y axes: the alignment of two scales is arbitrary and manufactures
// apparent correlation. With channels of very different magnitude (e.g. a 100 pA
// tunnelling current and a few fA of lock-in X in a pump-probe run) on one axis,
// the small one collapses onto zero, which is exactly the one of interest. So
// there are two layouts:
//   - overlay: all channels share one y axis (similar magnitudes, e.g. XYZ);
//   - split: one small plot per channel, each with its own y scale, aligned x
//     axes and a linked cursor.
// The default is "split" when channel magnitudes differ by more than 30x; the
// user can switch at any time and their choice is kept.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type uPlot from "uplot";
import { useUiStore } from "@/store";
import { EmptyNote } from "@/components/ui";
import { useLiveDraw, useLiveStream, useLiveTick } from "@/hooks/useLive";
import { TREND_RATIO, SMALL_MULTIPLE_RATIO } from "@/lib/chartSize";
import { fmtSI } from "@/lib/units";
import { fmtValue } from "@/lib/live/scale";
import { langTag, tr } from "@/lib/i18n";
import { magnitudeSpread, scalarView, seriesLatest, valueUnit, xyView, type SeriesView } from "@/lib/live/views";
import { readChartTheme, seriesColor } from "./chartTheme";
import { axisStyle, unitValues, useUplot } from "./useUplot";
import { ChannelLegend, Segmented, ToggleChip } from "./controls";

const WINDOWS = [
  { s: 30, label: "30 s" },
  { s: 120, label: "2 min" },
  { s: 600, label: "10 min" },
  { s: 0, label: tr("全部", "All") },
] as const;

/** These units are written with engineering prefixes (96.3 pA); others as number + unit. */
const SI_UNITS = new Set(["A", "V", "m", "Hz", "s", "W", "Ω", "N", "T"]);

/** Above this magnitude ratio the default layout is split. */
const SPLIT_RATIO = 30;

export function isSiUnit(unit: string | null | undefined): boolean {
  return SI_UNITS.has((unit ?? "").trim());
}

export function fmtWithUnit(v: number | null | undefined, unit: string | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const u = (unit ?? "").trim();
  if (u && SI_UNITS.has(u)) return fmtSI(v, u, { digits: 4 });
  return u ? `${fmtValue(v)} ${u}` : fmtValue(v);
}

type Layout = "overlay" | "split";

export function LiveSeriesChart({
  streamId,
  mode,
  compact = false,
}: {
  streamId: string;
  mode: "scalar" | "xy";
  compact?: boolean;
}) {
  const st = useLiveStream(streamId);
  useLiveTick(streamId, 2);
  const [windowS, setWindowS] = useState<number>(120);
  const [paused, setPaused] = useState(false);
  const [hidden, setHidden] = useState<Set<string>>(() => new Set());
  const [layout, setLayout] = useState<Layout | null>(null);
  const [cursorIdx, setCursorIdx] = useState<number | null>(null);
  /** Data most recently drawn (legend values and cursor readout come from here; frozen while paused). */
  const shown = useRef<SeriesView | null>(null);
  /** setData of each small plot (or of the single overlay plot), registered by key. */
  const plots = useRef(new Map<string, (data: uPlot.AlignedData) => void>());
  const syncKey = useRef(`live-${Math.random().toString(36).slice(2)}`).current;

  const buf = st?.buf.kind === "scalar" || st?.buf.kind === "xy" ? st.buf : null;
  const channels = buf ? buf.channels : [];
  const channelKey = channels.join("\u0000");
  const visible = channels.filter((c) => !hidden.has(c));
  const yUnit = st?.meta.y_unit ?? null;
  const yLabel = st ? `${st.meta.y_label ?? tr("值", "Value")}${yUnit ? ` (${yUnit})` : ""}` : tr("值", "Value");
  const xLabel =
    mode === "scalar"
      ? tr("时间", "Time")
      : st
        ? `${st.meta.x_label ?? "x"}${st.meta.x_unit ? ` (${st.meta.x_unit})` : ""}`
        : "x";

  // Point count, latest reading and magnitude spread are read from the buffer at
  // render time: for a stream that has ended and gets no new frames, the draw
  // callback runs only once and triggers no re-render, so using it to decide
  // whether there are points would leave the chart stuck on "empty".
  const latest = buf ? seriesLatest(buf) : null;
  const nBuf = latest?.n ?? 0;
  // Default layout: split when channel magnitudes differ too much (decided automatically only if the user has not chosen)
  const spread = buf && layout === null ? magnitudeSpread(buf, visible) : 1;
  const effLayout: Layout = layout ?? (visible.length > 1 && spread > SPLIT_RATIO ? "split" : "overlay");

  const register = useCallback((key: string, fn: ((d: uPlot.AlignedData) => void) | null) => {
    if (fn) plots.current.set(key, fn);
    else plots.current.delete(key);
  }, []);

  /** Distribute a view to the plots: the overlay plot gets all visible channels, each small plot its own. */
  const pushToPlots = (v: SeriesView) => {
    for (const [key, set] of plots.current) {
      if (key === "__all__") {
        const cols = visible.map((c) => v.data[v.channels.indexOf(c) + 1] ?? []);
        set([v.data[0] ?? [], ...cols] as unknown as uPlot.AlignedData);
      } else {
        const k = v.channels.indexOf(key);
        set([v.data[0] ?? [], v.data[k + 1] ?? []] as unknown as uPlot.AlignedData);
      }
    }
  };
  const pushRef = useRef(pushToPlots);
  pushRef.current = pushToPlots;
  /** A newly mounted plot gets the current data immediately (without waiting for the next frame). */
  const seed = useCallback(() => {
    if (shown.current) pushRef.current(shown.current);
  }, []);

  useLiveDraw(
    streamId,
    (cur) => {
      if (paused || !cur) return;
      const b = cur.buf;
      let v: SeriesView | null = null;
      if (b.kind === "scalar") v = scalarView(b, windowS > 0 ? windowS : null);
      else if (b.kind === "xy") v = xyView(b);
      if (!v) return;
      shown.current = v;
      pushRef.current(v);
    },
    [paused, windowS, channelKey],
  );

  const theme = useUiStore((s) => s.theme);
  const t = useMemo(() => readChartTheme(), [theme]);
  const colorOf = (name: string) => seriesColor(t, channels.indexOf(name));
  // While the cursor is on a plot read the drawn data; otherwise the latest point in the buffer
  const drawn = shown.current;
  const hovering = cursorIdx !== null && drawn !== null && cursorIdx < (drawn.data[0]?.length ?? 0);
  const unitOf = (c: string) => (st ? valueUnit(st.meta, c) : null);
  const values = channels.map((c) => {
    const v = hovering ? drawn!.data[drawn!.channels.indexOf(c) + 1]?.[cursorIdx!] : latest?.values[c];
    return typeof v === "number" ? fmtWithUnit(v, unitOf(c)) : "—";
  });
  // The overlay plot has a single y axis: the unit is written on it only if all visible channels share it
  const visibleUnits = new Set(visible.map(unitOf));
  const sharedUnit = visibleUnits.size === 1 ? [...visibleUnits][0]! : null;
  const xAt = hovering ? drawn!.data[0]?.[cursorIdx!] : latest?.x;

  if (!st) return <EmptyNote label={tr(`等待流 ${streamId} 的元数据…`, `Waiting for metadata of stream ${streamId}…`)} />;

  const toggle = (name: string) =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  const common = { mode, xLabel, xUnit: st.meta.x_unit, syncKey, register, seed, onCursor: setCursorIdx, compact };

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {mode === "scalar" && (
          <Segmented
            value={String(windowS)}
            onChange={(v) => setWindowS(Number(v))}
            options={WINDOWS.map((w) => ({ value: String(w.s), label: w.label }))}
          />
        )}
        {channels.length > 1 && (
          <Segmented
            value={effLayout}
            onChange={(v) => setLayout(v as Layout)}
            options={[
              { value: "overlay", label: tr("合并", "Overlay"), title: tr("所有通道同一个纵轴", "All channels share one y axis") },
              { value: "split", label: tr("分开", "Split"), title: tr("每个通道一张小图，纵轴各自缩放、横轴对齐", "One small plot per channel, each with its own y scale and aligned x axes") },
            ]}
          />
        )}
        <ToggleChip on={paused} onClick={() => setPaused((p) => !p)}>
          {paused ? tr("已暂停 · 继续", "Paused · Resume") : tr("暂停", "Pause")}
        </ToggleChip>
        {paused && <span className="text-mast-warn">{tr("画面已冻结，数据照常在收", "Display frozen; data is still being received")}</span>}
        {layout === null && effLayout === "split" && (
          <span className="text-mast-faint">{tr(`通道量级相差 ${Math.round(spread)} 倍，已分开画`, `Channel magnitudes differ by ${Math.round(spread)}x; plotted separately`)}</span>
        )}
      </div>
      <ChannelLegend
        channels={channels}
        colors={channels.map(colorOf)}
        hidden={hidden}
        onToggle={toggle}
        values={values}
      />
      {!nBuf && (
        <div className="rounded-lg border border-dashed border-mast-border px-3 py-6 text-center text-sm text-mast-muted">
          {mode === "xy" ? tr("还没有点 —— 测量开始后曲线会逐点长出来", "No points yet — the curve grows point by point once the measurement starts") : tr("还没有读数", "No readings yet")}
        </div>
      )}
      {nBuf > 0 &&
        (effLayout === "overlay" || visible.length <= 1 ? (
          <SeriesPlot
            key={`all-${channelKey}-${visible.join("|")}`}
            plotKey="__all__"
            names={visible}
            colors={visible.map(colorOf)}
            yLabel={sharedUnit === yUnit ? yLabel : `${st.meta.y_label ?? tr("值", "Value")}${sharedUnit ? ` (${sharedUnit})` : ""}`}
            yUnit={sharedUnit}
            height="full"
            {...common}
          />
        ) : (
          <div className="space-y-1">
            {visible.map((c) => (
              <SeriesPlot
                key={`one-${c}`}
                plotKey={c}
                names={[c]}
                colors={[colorOf(c)]}
                // Small plots are short and a long vertical label would be clipped; the channel
                // name is in the legend. SI units are already in the tick labels (100 pA), so the
                // axis label does not repeat them; other units are written on the axis
                yLabel={unitOf(c) && !isSiUnit(unitOf(c)) ? unitOf(c)! : ""}
                yUnit={unitOf(c)}
                height="small"
                {...common}
              />
            ))}
          </div>
        ))}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-mast-muted">
        <span>
          {hovering ? tr("游标", "Cursor") : tr("最新", "Latest")}:
          {typeof xAt === "number" ? (
            <span className="ml-1 font-mono text-mast-text">
              {mode === "scalar"
                ? new Date(xAt * 1000).toLocaleTimeString(langTag(), { hour12: false })
                : fmtWithUnit(xAt, st.meta.x_unit)}
            </span>
          ) : (
            <span className="ml-1">—</span>
          )}
        </span>
        <span>{tr(`${nBuf} 点`, `${nBuf} points`)}</span>
        {(shown.current?.skipped ?? 0) > 0 && (
          <span className="text-mast-warn">{tr(`${shown.current!.skipped} 点没有时间戳，没画`, `${shown.current!.skipped} point(s) without a timestamp were not drawn`)}</span>
        )}
        <span>{tr("拖选放大，双击复原", "Drag to zoom, double-click to reset")}</span>
      </div>
    </div>
  );
}

/** One uPlot: the overlay plot (all visible channels) or a small plot (one channel). */
function SeriesPlot({
  plotKey,
  names,
  colors,
  mode,
  xLabel,
  xUnit,
  yLabel,
  yUnit,
  height,
  syncKey,
  register,
  seed,
  onCursor,
  compact,
}: {
  plotKey: string;
  names: string[];
  colors: string[];
  mode: "scalar" | "xy";
  xLabel: string;
  xUnit: string | null;
  yLabel: string;
  yUnit: string | null;
  height: "full" | "small";
  syncKey: string;
  register: (key: string, fn: ((d: uPlot.AlignedData) => void) | null) => void;
  seed: () => void;
  onCursor: (idx: number | null) => void;
  compact: boolean;
}) {
  const theme = useUiStore((s) => s.theme);
  const handle = useUplot(
    (width, h) => {
      const t = readChartTheme();
      const series: uPlot.Series[] = [{ label: xLabel }];
      names.forEach((name, i) => {
        series.push({
          label: name,
          stroke: colors[i],
          width: 2,
          // xy: draw markers while points are few (>= 8 px, ringed in the surface colour), lines only beyond that
          points:
            mode === "xy"
              ? { show: (u: uPlot) => (u.data[0]?.length ?? 0) <= 120, size: 8, width: 2, stroke: t.surface, fill: colors[i] }
              : { show: false },
        });
      });
      return {
        width,
        height: h,
        scales: { x: { time: mode === "scalar" } },
        legend: { show: false },
        // Cursor linked across small plots (same syncKey), x axes aligned for reading
        cursor: { drag: { x: true, y: false }, sync: { key: syncKey } },
        axes: [
          { ...axisStyle(t), label: xLabel, labelGap: 4, ...(mode === "xy" ? { values: unitValues(xUnit) } : {}) },
          {
            ...axisStyle(t),
            ...(yLabel ? { label: yLabel, labelGap: 4 } : {}),
            size: 72,
            values: unitValues(yUnit),
          },
        ],
        series,
        hooks: { setCursor: [(u: uPlot) => onCursor(u.cursor.idx ?? null)] },
      };
    },
    () => [[], ...names.map(() => [])] as unknown as uPlot.AlignedData,
    [theme, mode, xLabel, yLabel, xUnit, yUnit, names.join("|"), colors.join("|")],
    height === "full"
      ? { fallbackW: 720, ratio: TREND_RATIO, minHeight: compact ? 160 : 220, maxHeight: compact ? 240 : 380 }
      : { fallbackW: 720, ratio: SMALL_MULTIPLE_RATIO, minHeight: 130, maxHeight: 200 },
  );

  useEffect(() => {
    register(plotKey, handle.setData);
    seed();
    return () => register(plotKey, null);
  }, [plotKey, handle.setData, handle.gen, register, seed]);

  return <div ref={handle.ref} className="overflow-hidden rounded-lg border border-mast-border bg-mast-bg p-2" />;
}
