// Live spectrum: the default renderer for LiveHub `spectrum` streams.
//
// The newest spectrum uses the accent colour; overlaid history spectra are light
// grey and fade with age (they are the same quantity at different times, not
// different channels, so they get no categorical colours). The x axis can switch
// between nm and Raman shift in cm⁻¹, which is only possible when the excitation
// line is known (see the top of spectrumAxis.ts for why no default such as 633 nm
// is assumed).
//
// The waterfall lives in the same component because it must stay horizontally
// aligned with the spectrum above: each column takes the value at that position
// in uPlot's current x range, so zooming the spectrum zooms the waterfall too.

import { useMemo, useRef, useState } from "react";
import type uPlot from "uplot";
import clsx from "clsx";
import { useUiStore } from "@/store";
import { Badge, EmptyNote } from "@/components/ui";
import { useLiveDraw, useLiveStream, useLiveTick } from "@/hooks/useLive";
import { requestHistory } from "@/lib/liveClient";
import { TREND_RATIO } from "@/lib/chartSize";
import type { StreamState } from "@/lib/live/store";
import { spectrumView, waterfallRows, type SpectrumView } from "@/lib/live/views";
import { ramanShiftCm1, unitLabel, type XUnit } from "@/lib/live/spectrumAxis";
import { colormapLut, lutIndex } from "@/lib/live/colormap";
import { fmtValue, percentileRange } from "@/lib/live/scale";
import { hexRgb, readChartTheme, withAlpha } from "./chartTheme";
import { axisStyle, sciValues, useUplot } from "./useUplot";
import { Segmented, ToggleChip } from "./controls";
import { langTag, tr } from "@/lib/i18n";

const OVERLAYS = [0, 3, 5, 10] as const;
const WATERFALL_ROWS = 200;
const WATERFALL_ROW_PX = 2;

interface WaterfallInfo {
  rows: number;
  newest: number | null;
  oldest: number | null;
  min: number;
  max: number;
}

export function LiveSpectrumChart({
  streamId,
  compact = false,
}: {
  streamId: string;
  /** Small chart in the stream list: no overlay and no waterfall by default. */
  compact?: boolean;
}) {
  const theme = useUiStore((s) => s.theme);
  const st = useLiveStream(streamId);
  useLiveTick(streamId, 2);
  const [unit, setUnit] = useState<XUnit>("nm");
  const [overlay, setOverlay] = useState<number>(compact ? 0 : 3);
  const [pausedAt, setPausedAt] = useState<number | null>(null);
  const [waterfall, setWaterfall] = useState(!compact);
  const [hover, setHover] = useState<{ x: number; y: number | null } | null>(null);
  const wfCanvas = useRef<HTMLCanvasElement | null>(null);
  const wfOffscreen = useRef<HTMLCanvasElement | null>(null);
  const wfInfo = useRef<WaterfallInfo | null>(null);
  /** uPlot's setScale callback must call the latest waterfall draw function. */
  const drawWaterfallRef = useRef<() => void>(() => {});

  // View used for the header text (twice a second); the one used for drawing is computed separately in the frame callback
  const view: SpectrumView | null = st ? spectrumView(st, { unit, overlay: 0, uptoSeq: pausedAt }) : null;
  const xUnit = view?.xUnit ?? "px";
  const xLabel =
    xUnit === "nm" || xUnit === "cm-1"
      ? unitLabel(xUnit)
      : xUnit === "px"
        ? tr("像素", "Pixel")
        : `${st?.meta.x_label ?? "x"}${view?.rawUnit ? ` (${view.rawUnit})` : ""}`;
  const yLabel = st
    ? `${st.meta.y_label ?? tr("强度", "Intensity")}${st.meta.y_unit ? ` (${st.meta.y_unit})` : " (counts)"}`
    : tr("强度", "Intensity");
  const laser = view?.laserNm ?? null;

  const handle = useUplot(
    (width, height) => {
      const t = readChartTheme();
      const series: uPlot.Series[] = [
        { label: xLabel },
        { label: tr("最新", "Latest"), stroke: t.accent, width: 2, points: { show: false } },
      ];
      for (let i = 0; i < overlay; i++) {
        series.push({
          label: tr(`历史 ${i + 1}`, `History ${i + 1}`),
          stroke: withAlpha(t.faint, Math.max(0.25, 0.8 - i * 0.07)),
          width: 1,
          points: { show: false },
        });
      }
      return {
        width,
        height,
        scales: { x: { time: false } },
        legend: { show: false },
        cursor: { drag: { x: true, y: false }, points: { show: false } },
        axes: [
          { ...axisStyle(t), label: xLabel, labelGap: 4 },
          { ...axisStyle(t), label: yLabel, labelGap: 4, size: 64, values: sciValues },
        ],
        series,
        hooks: {
          setCursor: [
            (u: uPlot) => {
              const i = u.cursor.idx;
              if (i == null) return setHover(null);
              const x = u.data[0]?.[i];
              const y = u.data[1]?.[i];
              setHover(typeof x === "number" ? { x, y: typeof y === "number" ? y : null } : null);
            },
          ],
          // Repaint the waterfall on zoom / double-click reset
          setScale: [() => drawWaterfallRef.current()],
        },
      };
    },
    () => [[], []] as unknown as uPlot.AlignedData,
    [theme, overlay, xLabel, yLabel],
    { fallbackW: 720, ratio: TREND_RATIO, minHeight: compact ? 180 : 240, maxHeight: compact ? 260 : 420 },
  );

  drawWaterfallRef.current = () => {
    const u = handle.plot.current;
    const cur = st;
    if (!waterfall || !u || !cur || !wfCanvas.current) return;
    const off = wfOffscreen.current ?? (wfOffscreen.current = document.createElement("canvas"));
    wfInfo.current = paintWaterfall(wfCanvas.current, off, u, cur, unit, pausedAt);
  };

  useLiveDraw(
    streamId,
    (cur) => {
      if (!cur) return;
      const v = spectrumView(cur, { unit, overlay, uptoSeq: pausedAt });
      if (!v) {
        handle.setData([[], [], ...Array.from({ length: overlay }, () => [])] as unknown as uPlot.AlignedData);
      } else {
        // Pad with empty series while history is shorter than the overlay count (uPlot needs the series count to match its options)
        const ys: (Float64Array | (number | null)[])[] = [...v.ys];
        while (ys.length < overlay + 1) ys.push(new Array<number | null>(v.x.length).fill(null));
        handle.setData([v.x, ...ys] as unknown as uPlot.AlignedData);
      }
      drawWaterfallRef.current();
    },
    [unit, overlay, pausedAt, waterfall, handle.gen, handle.width],
  );

  const latest = view?.latest ?? null;
  const metaBits = useMemo(() => frameMetaBits(latest?.meta ?? null), [latest]);
  const frames = st?.buf.kind === "spectrum" ? st.buf.entries.length : 0;
  const newestSeq = st?.buf.kind === "spectrum" ? (st.buf.entries.at(-1)?.seq ?? null) : null;
  const behind = pausedAt !== null && newestSeq !== null ? newestSeq - pausedAt : 0;

  if (!st) return <EmptyNote label={tr(`等待流 ${streamId} 的元数据…`, `Waiting for metadata of stream ${streamId}…`)} />;

  const toggleWaterfall = () => {
    const next = !waterfall;
    setWaterfall(next);
    // Opening the waterfall: fetch the history kept by the server instead of starting to accumulate from now
    if (next) void requestHistory(streamId, WATERFALL_ROWS);
  };

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Segmented
          value={unit}
          onChange={(u) => setUnit(u as XUnit)}
          options={[
            { value: "nm", label: "nm" },
            {
              value: "cm-1",
              label: "cm⁻¹",
              disabled: laser === null,
              title: laser === null ? tr("没有激发线（laser_line_nm），不能换算", "No excitation line (laser_line_nm); cannot convert") : tr("拉曼位移", "Raman shift"),
            },
          ]}
        />
        {!compact && (
          <Segmented
            value={String(overlay)}
            onChange={(v) => setOverlay(Number(v))}
            options={OVERLAYS.map((n) => ({ value: String(n), label: n === 0 ? tr("不叠加", "No overlay") : tr(`叠加 ${n}`, `Overlay ${n}`) }))}
          />
        )}
        <button
          type="button"
          onClick={() => setPausedAt(pausedAt === null ? newestSeq : null)}
          className={clsx(
            "rounded-mast-ctl border px-2.5 py-1",
            pausedAt !== null
              ? "border-mast-warn-border bg-mast-warn-bg text-mast-warn"
              : "border-mast-border text-mast-muted hover:text-mast-text",
          )}
        >
          {pausedAt !== null ? tr("已暂停 · 继续", "Paused · Resume") : tr("暂停", "Pause")}
        </button>
        <ToggleChip on={waterfall} onClick={toggleWaterfall}>
          {tr("瀑布图", "Waterfall")}
        </ToggleChip>
        <span className="text-mast-faint">{laser !== null ? tr(`激发线 ${laser} nm`, `Excitation ${laser} nm`) : tr("无激发线信息", "No excitation line info")}</span>
        {pausedAt !== null && behind > 0 && (
          <span className="text-mast-warn">{tr(`画面冻结在 #${pausedAt}，之后又来了 ${behind} 帧`, `Display frozen at #${pausedAt}; ${behind} more frame(s) arrived since`)}</span>
        )}
      </div>

      {view?.axisNote && <div className="text-xs text-mast-warn">{view.axisNote}</div>}

      <div ref={handle.ref} className="relative overflow-hidden rounded-lg border border-mast-border bg-mast-bg p-2">
        {!frames && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-mast-muted">
            {tr("还没有收到谱 —— 采集开始后会自动出现", "No spectrum received yet — it appears automatically once acquisition starts")}
          </div>
        )}
      </div>

      {waterfall && (
        <div className="rounded-lg border border-mast-border bg-mast-bg p-2">
          <div className="mb-1 flex flex-wrap items-center justify-between gap-2 text-[11px] text-mast-faint">
            <span>{tr("瀑布图：每行一条谱，最新在上；与上图同轴、同缩放 · 色表 inferno，1%–99.5% 分位", "Waterfall: one spectrum per row, newest on top; same axis and zoom as above · inferno colormap, 1%–99.5% percentile")}</span>
            <WaterfallLegend info={wfInfo.current} />
          </div>
          <canvas ref={wfCanvas} className="block w-full" style={{ height: WATERFALL_ROW_PX }} />
        </div>
      )}

      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-mast-muted">
        <span>
          {tr("游标：", "Cursor: ")}
          {hover ? (
            <span className="ml-1 font-mono text-mast-text">
              {fmtX(hover.x, xUnit)}
              {xUnit === "nm" && laser !== null && ` (${ramanShiftCm1(hover.x, laser).toFixed(1)} cm⁻¹)`}
              {" → "}
              {fmtValue(hover.y)}
            </span>
          ) : (
            <span className="ml-1">{tr("移到谱上读数；拖选放大，双击复原", "Hover over the spectrum to read; drag to zoom, double-click to reset")}</span>
          )}
        </span>
        {latest && (
          <span className="font-mono">
            #{latest.seq}
            {latest.t !== null && ` · ${new Date(latest.t * 1000).toLocaleTimeString(langTag(), { hour12: false })}`}
          </span>
        )}
        <span>{tr(`缓存 ${frames} 帧`, `${frames} frame(s) buffered`)}</span>
        {metaBits.map((b) => (
          <Badge key={b}>{b}</Badge>
        ))}
      </div>
    </div>
  );
}

/**
 * Paint the waterfall. Each column takes its value at the current uPlot x range
 * (binary search on the display axis), each row is one spectrum. Drawn first onto
 * a width x rows offscreen canvas, then scaled onto the visible canvas without
 * interpolation.
 */
function paintWaterfall(
  canvas: HTMLCanvasElement,
  off: HTMLCanvasElement,
  u: uPlot,
  st: StreamState,
  unit: XUnit,
  uptoSeq: number | null,
): WaterfallInfo | null {
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  const v = spectrumView(st, { unit, overlay: 0, uptoSeq });
  const rows = waterfallRows(st, WATERFALL_ROWS, uptoSeq);
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 1;
  const cssH = Math.max(WATERFALL_ROW_PX, rows.length * WATERFALL_ROW_PX);
  if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.height = `${cssH}px`;
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!v || !rows.length) return null;

  // uPlot's bbox is in canvas pixels; the visible waterfall canvas has the same width, so the same pixel units apply
  const plotLeft = u.bbox.left;
  const plotW = Math.max(1, Math.round(u.bbox.width));
  const xs = v.x;
  const n = xs.length;
  const min = u.scales.x?.min ?? xs[0]!;
  const max = u.scales.x?.max ?? xs[n - 1]!;
  const colIdx = new Int32Array(plotW);
  for (let c = 0; c < plotW; c++) {
    const xv = min + ((c + 0.5) / plotW) * (max - min);
    if (n === 0 || xv < xs[0]! || xv > xs[n - 1]!) {
      colIdx[c] = -1;
      continue;
    }
    let lo = 0;
    let hi = n - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (xs[mid]! <= xv) lo = mid;
      else hi = mid;
    }
    const near = Math.abs(xs[hi]! - xv) < Math.abs(xs[lo]! - xv) ? hi : lo;
    colIdx[c] = v.reversed ? n - 1 - near : near;
  }

  // Colour scale: percentiles over a sample of all rows
  const per = Math.min(256, rows[0]!.y.length);
  const sample = new Float64Array(rows.length * per);
  let k = 0;
  for (const r of rows) {
    const step = Math.max(1, Math.floor(r.y.length / per));
    for (let i = 0; i < r.y.length && k < sample.length; i += step) sample[k++] = r.y[i]!;
  }
  const range = percentileRange(sample.subarray(0, k), 0.01, 0.995);
  if (!range) return null;

  off.width = plotW;
  off.height = rows.length;
  const octx = off.getContext("2d");
  if (!octx) return null;
  const img = octx.createImageData(plotW, rows.length);
  const lut = colormapLut("inferno");
  const bg = hexRgb(readChartTheme().panel2);
  rows.forEach((r, row) => {
    for (let c = 0; c < plotW; c++) {
      const p = 4 * (row * plotW + c);
      const ix = colIdx[c]!;
      const li = ix < 0 || ix >= r.y.length ? -1 : lutIndex(r.y[ix]!, range.min, range.max);
      if (li < 0) {
        img.data[p] = bg[0];
        img.data[p + 1] = bg[1];
        img.data[p + 2] = bg[2];
      } else {
        img.data[p] = lut[3 * li]!;
        img.data[p + 1] = lut[3 * li + 1]!;
        img.data[p + 2] = lut[3 * li + 2]!;
      }
      img.data[p + 3] = 255;
    }
  });
  octx.putImageData(img, 0, 0);
  ctx.imageSmoothingEnabled = false;
  // uPlot's canvas sits inside p-2 padding; both containers use the same padding, so the left edges align
  ctx.drawImage(off, 0, 0, plotW, rows.length, plotLeft, 0, plotW, canvas.height);
  return {
    rows: rows.length,
    newest: rows[0]!.t,
    oldest: rows[rows.length - 1]!.t,
    min: range.min,
    max: range.max,
  };
}

function WaterfallLegend({ info }: { info: WaterfallInfo | null }) {
  if (!info) return null;
  const span = info.newest !== null && info.oldest !== null ? info.newest - info.oldest : null;
  return (
    <span className="font-mono">
      {tr(`${info.rows} 行`, `${info.rows} row(s)`)}{span !== null && tr(` · 跨 ${span.toFixed(1)} s`, ` · spanning ${span.toFixed(1)} s`)} · {fmtValue(info.min)} … {fmtValue(info.max)}
    </span>
  );
}

function fmtX(x: number, unit: string): string {
  if (unit === "nm") return `${x.toFixed(2)} nm`;
  if (unit === "cm-1") return `${x.toFixed(1)} cm⁻¹`;
  if (unit === "px") return tr(`像素 ${Math.round(x)}`, `Pixel ${Math.round(x)}`);
  return fmtValue(x);
}

/**
 * Frame meta fields worth showing at a glance: exposure, frame number, manual
 * acquisition (frames forwarded by the satellite); the averaged spectrum of
 * AcquireSpectrum (final / frames_averaged) and the scan point of composite skills
 * (ix, iy, mode).
 */
function frameMetaBits(meta: Record<string, unknown> | null): string[] {
  if (!meta) return [];
  const out: string[] = [];
  const exp = meta.exposure_ms;
  if (typeof exp === "number") out.push(tr(`曝光 ${exp} ms`, `Exposure ${exp} ms`));
  const avg = meta.frames_averaged;
  if (meta.final === true) out.push(typeof avg === "number" ? tr(`平均谱 · ${avg} 帧`, `Averaged spectrum · ${avg} frames`) : tr("平均谱", "Averaged spectrum"));
  const fi = meta.frame_index;
  if (typeof fi === "number") out.push(tr(`帧 ${fi}`, `Frame ${fi}`));
  if (meta.manual === true) out.push(tr("人工采集", "Manual acquisition"));
  if (typeof meta.ix === "number" && typeof meta.iy === "number") out.push(tr(`点 (${meta.ix}, ${meta.iy})`, `Point (${meta.ix}, ${meta.iy})`));
  if (typeof meta.mode === "string" && meta.mode) out.push(meta.mode);
  const tag = meta.tag;
  if (typeof tag === "string" && tag) out.push(tag);
  return out.slice(0, 6);
}
