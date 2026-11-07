// Pixel-by-pixel map: the default renderer for LiveHub `map2d` streams (TERS maps,
// alignment maps, raster scans).
//
// An offscreen nx x ny canvas holds the current colour of every pixel. When new
// pixels arrive only those pixels are updated (from the store's write log, each
// view reads its own position), then the whole canvas is blitted onto the visible
// one without interpolation (one data pixel is one square). The whole image is
// recomputed only when the colour-scale range changes.
//
// Two invariants:
//   - pixels not yet acquired are drawn in the background colour, not the lowest
//     colour of the scale: 0 is a legitimate reading, "not measured yet" is not 0;
//   - orientation follows physical coordinates: x increases to the right and y
//     upwards (row 0 is at the top when dy < 0). The axes show physical
//     coordinates, so the image and the numbers never contradict each other.

import { useMemo, useRef, useState } from "react";
import { useUiStore } from "@/store";
import { EmptyNote } from "@/components/ui";
import { SelectField } from "@/components/controls";
import { useLiveDraw, useLiveStream, useLiveTick } from "@/hooks/useLive";
import { useElementWidth } from "@/hooks/useElementWidth";
import { map2dChangesSince, type Map2dBuf } from "@/lib/live/store";
import { canvasCellToPixel, mapExtent, pixelCoord, pixelToCanvasCell, valueUnit } from "@/lib/live/views";
import {
  COLORMAP_LABELS,
  COLORMAPS,
  colormapGradient,
  colormapLut,
  lutIndex,
  type ColormapName,
} from "@/lib/live/colormap";
import { finiteRange, fmtTick, niceTicks, percentileRange, symmetricRange, type Range } from "@/lib/live/scale";
import { tr } from "@/lib/i18n";
import { hexRgb, readChartTheme } from "./chartTheme";
import { Segmented } from "./controls";
import { fmtWithUnit } from "./LiveSeriesChart";

type ScaleMode = "pct" | "minmax" | "sym";

const SCALE_MODES: { value: ScaleMode; label: string; title: string }[] = [
  { value: "pct", label: "2–98%", title: tr("分位数色标：一个尖峰撑不开色标（真实极值另列）", "Percentile scale: a single spike cannot stretch the scale (true extremes are listed separately)") },
  { value: "minmax", label: "min–max", title: tr("按真实最小/最大值", "Use the true minimum / maximum") },
  { value: "sym", label: tr("±对称", "±symmetric"), title: tr("关于 0 对称（有正负号的量，配红蓝色表）", "Symmetric about 0 (for signed quantities, with a red-blue colormap)") },
];

/** Minimum interval (ms) between colour-scale range recomputations; during pixel-by-pixel build every new pixel can shift the percentiles. */
const RANGE_REFRESH_MS = 500;

export function LiveMap2d({ streamId, compact = false }: { streamId: string; compact?: boolean }) {
  const theme = useUiStore((s) => s.theme);
  const st = useLiveStream(streamId);
  useLiveTick(streamId, 2);
  const [channel, setChannel] = useState<string | null>(null);
  const [cmap, setCmap] = useState<ColormapName>("viridis");
  const [scaleMode, setScaleMode] = useState<ScaleMode>("pct");
  const [hover, setHover] = useState<{ ix: number; iy: number } | null>(null);
  const [boxRef, boxW] = useElementWidth<HTMLDivElement>();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const off = useRef<{ canvas: HTMLCanvasElement; img: ImageData; buf: Map2dBuf; channel: string; logPos: number } | null>(null);
  const rangeRef = useRef<{ range: Range; at: number; key: string } | null>(null);
  const [range, setRange] = useState<Range | null>(null);

  const buf = st?.buf.kind === "map2d" ? st.buf : null;
  const geom = buf?.geom ?? null;
  const channels = buf?.channels ?? [];
  const ch = channel && channels.includes(channel) ? channel : (channels[0] ?? null);

  // Canvas size: physical aspect ratio, width follows the container, height is capped
  const size = useMemo(() => {
    if (!geom) return null;
    const ext = mapExtent(geom);
    const aspect = (ext.yMax - ext.yMin) / (ext.xMax - ext.xMin || 1);
    const maxH = compact ? 260 : 480;
    const avail = Math.max(160, (boxW ?? 480) - 96); // room on the right for the colour bar
    let w = avail;
    let h = w * aspect;
    if (h > maxH) {
      h = maxH;
      w = h / aspect;
    }
    return { w: Math.max(40, Math.round(w)), h: Math.max(40, Math.round(h)) };
  }, [geom, boxW, compact]);

  const paint = () => {
    const canvas = canvasRef.current;
    if (!canvas || !buf || !geom || !ch || !size) return;
    const img = buf.values.get(ch);
    if (!img) return;
    // Colour-scale range: recomputed at a throttled rate (percentiles keep changing
    // during the build, but there is no need to repaint the whole image per pixel)
    const key = `${ch}|${scaleMode}`;
    const now = Date.now();
    let r = rangeRef.current;
    let full = false;
    if (!r || r.key !== key || now - r.at > RANGE_REFRESH_MS) {
      const base = scaleMode === "minmax" ? finiteRange(img) : percentileRange(img);
      const next = base ? (scaleMode === "sym" ? symmetricRange(base) : base) : null;
      if (next) {
        const changed = !r || r.key !== key || r.range.min !== next.min || r.range.max !== next.max;
        r = { range: next, at: now, key };
        rangeRef.current = r;
        if (changed) {
          full = true;
          setRange(next); // colour-bar ticks are drawn by React: re-render only when the range really changed
        }
      }
    }
    if (!r) return;
    const t = readChartTheme();
    const empty = hexRgb(t.panel2);
    const lut = colormapLut(cmap);
    // Offscreen canvas: a new stream, channel, colormap or a replaced buffer (stream reset) → repaint everything
    let o = off.current;
    if (!o || o.buf !== buf || o.channel !== `${ch}|${cmap}|${theme}`) {
      const c = document.createElement("canvas");
      c.width = geom.nx;
      c.height = geom.ny;
      const ctx = c.getContext("2d");
      if (!ctx) return;
      o = { canvas: c, img: ctx.createImageData(geom.nx, geom.ny), buf, channel: `${ch}|${cmap}|${theme}`, logPos: 0 };
      off.current = o;
      full = true;
    }
    const changes = map2dChangesSince(buf, o.logPos);
    o.logPos = changes.next;
    const put = (idx: number) => {
      const ix = idx % geom.nx;
      const iy = Math.floor(idx / geom.nx);
      const { cx, cy } = pixelToCanvasCell(geom, ix, iy);
      const p = 4 * (cy * geom.nx + cx);
      const li = lutIndex(img[idx]!, r!.range.min, r!.range.max);
      const d = o!.img.data;
      if (li < 0) {
        d[p] = empty[0];
        d[p + 1] = empty[1];
        d[p + 2] = empty[2];
      } else {
        d[p] = lut[3 * li]!;
        d[p + 1] = lut[3 * li + 1]!;
        d[p + 2] = lut[3 * li + 2]!;
      }
      d[p + 3] = 255;
    };
    if (full || changes.full) {
      for (let idx = 0; idx < geom.nx * geom.ny; idx++) put(idx);
    } else {
      for (const idx of changes.idx) put(idx);
    }
    o.canvas.getContext("2d")?.putImageData(o.img, 0, 0);

    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(size.w * dpr) || canvas.height !== Math.round(size.h * dpr)) {
      canvas.width = Math.round(size.w * dpr);
      canvas.height = Math.round(size.h * dpr);
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(o.canvas, 0, 0, canvas.width, canvas.height);
    // Current scan position: thin accent-coloured frame
    if (buf.last) {
      const { cx, cy } = pixelToCanvasCell(geom, buf.last.ix, buf.last.iy);
      const cw = canvas.width / geom.nx;
      const chh = canvas.height / geom.ny;
      ctx.strokeStyle = t.accent;
      ctx.lineWidth = Math.max(1, dpr);
      ctx.strokeRect(cx * cw + 0.5, cy * chh + 0.5, Math.max(2, cw - 1), Math.max(2, chh - 1));
    }
  };
  const paintRef = useRef(paint);
  paintRef.current = paint;

  // Channel / colormap / theme changes: they are part of the offscreen cache key, so the image is repainted; a scale-mode change alters the rangeRef key
  useLiveDraw(streamId, () => paintRef.current(), [ch, cmap, scaleMode, size?.w, size?.h, theme]);

  if (!st) return <EmptyNote label={tr(`等待流 ${streamId} 的元数据…`, `Waiting for metadata of stream ${streamId}…`)} />;
  if (!geom) {
    return (
      <div className="rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-sm text-mast-warn">
        {tr(
          "这个 map2d 流的元数据里没有合法的几何（meta 需要 nx、ny、x0、y0、dx、dy），没法画。",
          "The metadata of this map2d stream has no valid geometry (meta needs nx, ny, x0, y0, dx, dy), so it cannot be drawn.",
        )}
        {tr(` 已收到 ${st.received + st.invalid} 帧。`, ` ${st.received + st.invalid} frame(s) received.`)}
      </div>
    );
  }

  const total = geom.nx * geom.ny;
  const filled = buf?.filled ?? 0;
  const ext = mapExtent(geom);
  const xTicks = niceTicks(ext.xMin, ext.xMax, 4);
  const yTicks = niceTicks(ext.yMin, ext.yMax, 4);
  const xStep = xTicks.length > 1 ? xTicks[1]! - xTicks[0]! : 1;
  const yStep = yTicks.length > 1 ? yTicks[1]! - yTicks[0]! : 1;
  const cTicks = range ? niceTicks(range.min, range.max, 4) : [];
  const cStep = cTicks.length > 1 ? cTicks[1]! - cTicks[0]! : 1;
  const real = ch && buf?.values.get(ch) ? finiteRange(buf.values.get(ch)!) : null;
  const hv = hover && ch ? buf?.values.get(ch)?.[hover.iy * geom.nx + hover.ix] : undefined;
  const hc = hover ? pixelCoord(geom, hover.ix, hover.iy) : null;
  // The value unit is not y_unit (that belongs to the y axis); see valueUnit
  const vUnit = valueUnit(st.meta, ch);

  const onMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const cx = Math.floor(((e.clientX - rect.left) / rect.width) * geom.nx);
    const cy = Math.floor(((e.clientY - rect.top) / rect.height) * geom.ny);
    if (cx < 0 || cy < 0 || cx >= geom.nx || cy >= geom.ny) return setHover(null);
    setHover(canvasCellToPixel(geom, cx, cy));
  };

  return (
    <div className="space-y-2" ref={boxRef}>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {channels.length > 1 && (
          <div className="w-44">
            <SelectField
              value={ch ?? ""}
              onChange={(v) => setChannel(v)}
              options={channels.map((c) => ({ value: c, label: c }))}
            />
          </div>
        )}
        <Segmented
          value={cmap}
          onChange={(v) => setCmap(v as ColormapName)}
          options={COLORMAPS.map((c) => ({ value: c, label: COLORMAP_LABELS[c] }))}
        />
        <Segmented value={scaleMode} onChange={(v) => setScaleMode(v as ScaleMode)} options={SCALE_MODES} />
      </div>

      <div className="flex items-stretch gap-3">
        <div className="min-w-0">
          <div className="flex">
            {/* y-axis ticks (physical coordinates, increasing upwards) */}
            <div className="relative mr-1 w-12 shrink-0 text-right font-mono text-[10px] text-mast-faint" style={{ height: size?.h }}>
              {yTicks.map((v) => (
                <span
                  key={v}
                  className="absolute right-0 -translate-y-1/2"
                  style={{ top: `${(1 - (v - ext.yMin) / (ext.yMax - ext.yMin || 1)) * 100}%` }}
                >
                  {fmtTick(v, yStep)}
                </span>
              ))}
            </div>
            <canvas
              ref={canvasRef}
              onMouseMove={onMove}
              onMouseLeave={() => setHover(null)}
              className="block cursor-crosshair rounded border border-mast-border"
              style={{ width: size?.w, height: size?.h, imageRendering: "pixelated" }}
            />
          </div>
          {/* x-axis ticks */}
          <div className="relative ml-[52px] h-4 font-mono text-[10px] text-mast-faint" style={{ width: size?.w }}>
            {xTicks.map((v) => (
              <span
                key={v}
                className="absolute -translate-x-1/2"
                style={{ left: `${((v - ext.xMin) / (ext.xMax - ext.xMin || 1)) * 100}%` }}
              >
                {fmtTick(v, xStep)}
              </span>
            ))}
          </div>
          <div className="ml-[52px] text-center text-[11px] text-mast-muted" style={{ width: size?.w }}>
            x{geom.x_unit ? ` (${geom.x_unit})` : ""} · y{geom.y_unit ? ` (${geom.y_unit})` : ""} {tr("向上", "up")}
          </div>
        </div>

        {/* colour bar */}
        {range && (
          <div className="flex shrink-0 items-stretch gap-1" style={{ height: size?.h }}>
            <div
              className="w-3 rounded-sm border border-mast-border"
              style={{ background: colormapGradient(cmap) }}
              aria-label={tr("色标", "Colour bar")}
            />
            <div className="relative w-14 font-mono text-[10px] text-mast-faint">
              {cTicks.map((v) => (
                <span
                  key={v}
                  className="absolute left-0 -translate-y-1/2"
                  style={{ top: `${(1 - (v - range.min) / (range.max - range.min || 1)) * 100}%` }}
                >
                  {vUnit ? fmtWithUnit(v, vUnit) : fmtTick(v, cStep)}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-mast-muted">
        <span>
          {tr("游标：", "Cursor: ")}
          {hover && hc ? (
            <span className="ml-1 font-mono text-mast-text">
              [{hover.ix}, {hover.iy}] ({fmtTick(hc.x, xStep / 10)}, {fmtTick(hc.y, yStep / 10)}) →{" "}
              {hv === undefined || Number.isNaN(hv) ? tr("未采集", "not acquired") : fmtWithUnit(hv, vUnit)}
            </span>
          ) : (
            <span className="ml-1">{tr("移到图上读数", "Hover over the map to read a value")}</span>
          )}
        </span>
        <span>
          {geom.nx}×{geom.ny} · {tr("已采", "acquired")} {filled}/{total} ({((filled / total) * 100).toFixed(1)}%)
        </span>
        {real && (
          <span>
            {tr("实际范围", "Actual range")} {fmtWithUnit(real.min, vUnit)} … {fmtWithUnit(real.max, vUnit)}
          </span>
        )}
        {(buf?.outOfRange ?? 0) > 0 && <span className="text-mast-warn">{tr(`${buf!.outOfRange} 帧落在图外，未画`, `${buf!.outOfRange} frame(s) fell outside the map and were not drawn`)}</span>}
        {ch && <span>{tr("通道", "Channel")} {ch}</span>}
      </div>
    </div>
  );
}
