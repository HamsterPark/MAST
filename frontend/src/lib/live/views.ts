// 缓存 → 画图数组。纯函数，画图组件每帧调一次（只在流变了的那一帧）。
//
// 这里集中了「画出来的东西是不是测到的东西」的几个判断，所以单独成文件、单独测：
//   · 光谱的轴版本对不上（帧指向的轴还没到/已过期）时按像素号画，并且说出来 ——
//     用错版本的轴画，峰会整体平移到错误的波长上，而图看起来完全正常；
//   · 历史叠加只叠同一版轴、同样长度的谱，换过光栅/中心波长之前的谱不混进来；
//   · 标量和 xy 的 null 是「这一点没有读数」，保留为 null 让线断开，绝不当 0。

import { tr } from "../i18n.ts";
import type { Map2dGeometry, StreamMeta } from "./protocol.ts";
import type { AxisState, ScalarBuf, SpectrumEntry, StreamState, XyBuf } from "./store.ts";
import { ascending, convertAxis, isNmUnit, laserLineOf, type XUnit } from "./spectrumAxis.ts";

// ── 光谱 ───────────────────────────────────────────────────────────────────

export type SpectrumXUnit = XUnit | "px" | "other";

export interface SpectrumView {
  /** 升序的横轴（已按所选单位换算）。 */
  x: Float64Array;
  /** [最新, 上一条, …]，与 x 等长。 */
  ys: Float64Array[];
  seqs: number[];
  latest: SpectrumEntry;
  xUnit: SpectrumXUnit;
  /** 横轴原始单位（other 时显示它）。 */
  rawUnit: string;
  laserNm: number | null;
  /** 横轴为什么不是请求的样子；null = 一切照请求。 */
  axisNote: string | null;
  /** 原始轴是降序、显示时翻过：显示下标 i 对应原始下标 n-1-i。 */
  reversed: boolean;
}

export interface SpectrumViewOpts {
  unit: XUnit;
  /** 叠加几条历史谱（不含最新那条）。 */
  overlay: number;
  /** 暂停时冻结在这个 seq（含）；null = 跟随最新。 */
  uptoSeq: number | null;
}

/**
 * 帧指向的轴。axis_ver = 0 是服务端的「这一帧没有轴」（卫星的波长轴还没到时，枢纽按像素号
 * 发帧）——绝不能拿之后才到的轴去套它。帧里干脆没有 axis_ver 时才用当前轴。
 */
function axisFor(st: StreamState, ver: number | null): AxisState | null {
  if (ver === null) return st.axis;
  if (ver === 0) return null;
  return st.axes.find((a) => a.ver === ver) ?? null;
}

export function spectrumView(st: StreamState, opts: SpectrumViewOpts): SpectrumView | null {
  if (st.buf.kind !== "spectrum") return null;
  const entries = st.buf.entries;
  let end = entries.length - 1;
  if (opts.uptoSeq !== null) {
    while (end >= 0 && entries[end]!.seq > opts.uptoSeq) end -= 1;
  }
  if (end < 0) return null;
  const latest = entries[end]!;
  const n = latest.y.length;
  const axis = axisFor(st, latest.axisVer);
  let axisNote: string | null = null;
  let x: Float64Array;
  let xUnit: SpectrumXUnit;
  let rawUnit = "";
  const laserNm = laserLineOf(latest.meta, st.meta.meta);

  if (axis && axis.x.length === n) {
    rawUnit = axis.unit;
    if (isNmUnit(axis.unit)) {
      if (opts.unit === "cm-1") {
        const conv = convertAxis(axis.x, "cm-1", laserNm);
        if (conv) {
          x = conv;
          xUnit = "cm-1";
        } else {
          x = axis.x;
          xUnit = "nm";
          axisNote = tr(
            "没有激发线（laser_line_nm），不能换算成拉曼位移，仍按波长显示",
            "No excitation line (laser_line_nm); cannot convert to Raman shift, showing wavelength",
          );
        }
      } else {
        x = axis.x;
        xUnit = "nm";
      }
    } else {
      x = axis.x;
      xUnit = "other";
      if (opts.unit === "cm-1") {
        axisNote = tr(
          `横轴单位是「${axis.unit || "未标注"}」，不是 nm，无法换算`,
          `X-axis unit is "${axis.unit || "unlabelled"}", not nm; cannot convert`,
        );
      }
    }
  } else {
    x = new Float64Array(n);
    for (let i = 0; i < n; i++) x[i] = i;
    xUnit = "px";
    axisNote = !axis
      ? latest.axisVer === null || latest.axisVer === 0
        ? tr(
            "这一帧没有波长轴（光谱仪的轴还没到），按像素号显示",
            "This frame has no wavelength axis (the spectrometer axis has not arrived); showing pixel index",
          )
        : tr(
            `这一帧指向的波长轴（版本 ${latest.axisVer}）还没到或已过期，按像素号显示`,
            `The wavelength axis this frame points to (version ${latest.axisVer}) has not arrived or is stale; showing pixel index`,
          )
      : tr(
          `波长轴长度 ${axis.x.length} 与谱长度 ${n} 不一致，按像素号显示`,
          `Wavelength axis length ${axis.x.length} does not match spectrum length ${n}; showing pixel index`,
        );
  }

  const ys: Float64Array[] = [latest.y];
  const seqs: number[] = [latest.seq];
  for (let i = end - 1; i >= 0 && ys.length <= opts.overlay; i--) {
    const e = entries[i]!;
    if (e.y.length !== n || e.axisVer !== latest.axisVer) continue;
    ys.push(e.y);
    seqs.push(e.seq);
  }
  const asc = ascending(x, ys);
  return {
    x: asc.x,
    ys: asc.ys,
    seqs,
    latest,
    xUnit,
    rawUnit,
    laserNm,
    axisNote,
    reversed: asc.reversed,
  };
}

/** 瀑布图的行：与最新一帧同轴版本、同长度的谱，新的在前，最多 maxRows 行。 */
export function waterfallRows(
  st: StreamState,
  maxRows: number,
  uptoSeq: number | null,
): Array<{ seq: number; t: number | null; y: Float64Array }> {
  if (st.buf.kind !== "spectrum") return [];
  const entries = st.buf.entries;
  let end = entries.length - 1;
  if (uptoSeq !== null) while (end >= 0 && entries[end]!.seq > uptoSeq) end -= 1;
  if (end < 0) return [];
  const ref = entries[end]!;
  const out: Array<{ seq: number; t: number | null; y: Float64Array }> = [];
  for (let i = end; i >= 0 && out.length < maxRows; i--) {
    const e = entries[i]!;
    if (e.y.length !== ref.y.length || e.axisVer !== ref.axisVer) continue;
    out.push({ seq: e.seq, t: e.t, y: e.y });
  }
  return out;
}

// ── 标量时间序列 ───────────────────────────────────────────────────────────

export interface SeriesView {
  /** uPlot AlignedData：[x, ...每个通道]。 */
  data: (number | null)[][];
  channels: string[];
  /** 没有时间戳、因此没法画在时间轴上的点数。 */
  skipped: number;
}

/** 最近 windowS 秒（null = 全部）。x 是 unix 秒。 */
export function scalarView(buf: ScalarBuf, windowS: number | null): SeriesView {
  let tMax = -Infinity;
  for (const t of buf.t) if (t !== null && t > tMax) tMax = t;
  const from = windowS !== null && Number.isFinite(tMax) ? tMax - windowS : -Infinity;
  const xs: number[] = [];
  const cols = buf.channels.map(() => [] as (number | null)[]);
  let skipped = 0;
  for (let i = 0; i < buf.t.length; i++) {
    const t = buf.t[i];
    if (t === null || t === undefined) {
      skipped += 1;
      continue;
    }
    if (t < from) continue;
    xs.push(t);
    buf.channels.forEach((name, k) => cols[k]!.push(buf.values.get(name)?.[i] ?? null));
  }
  return { data: [xs, ...cols], channels: [...buf.channels], skipped };
}

// ── xy 曲线 ────────────────────────────────────────────────────────────────

/**
 * 按 x 升序排（uPlot 的要求），x 相同按采集顺序。单调扫描（延迟线从小到大或从大到小）
 * 排完就是原曲线；来回扫的同一个流会被排成「按 x 的函数」，那种情况生产方应当每一程
 * 声明一个流（流名里带 run_id）。
 */
export function xyView(buf: XyBuf): SeriesView {
  const order = buf.points.map((_, i) => i);
  order.sort((a, b) => {
    const pa = buf.points[a]!;
    const pb = buf.points[b]!;
    return pa.x - pb.x || pa.seq - pb.seq;
  });
  const xs: number[] = [];
  const cols = buf.channels.map(() => [] as (number | null)[]);
  for (const i of order) {
    const p = buf.points[i]!;
    xs.push(p.x);
    buf.channels.forEach((name, k) => {
      const v = p.values[name];
      cols[k]!.push(v === undefined ? null : v);
    });
  }
  return { data: [xs, ...cols], channels: [...buf.channels], skipped: 0 };
}

/**
 * 某个通道的**值**的单位。顺序：declare 的 meta.units[通道] → meta.value_unit →
 * （仅 scalar / xy，且没有给 units 表时）y_unit。map2d 绝不退到 y_unit：在 map2d 里 y_unit
 * 是 y **轴**的单位（例如 nm），拿它当值的单位，强度就会被写成「2538 nm」。给了 units 表而
 * 表里没有这个通道，是生产方说「单位不知道」（例如 sig<N>_mean 取决于那个信号槽接了什么），
 * 也不退到 y_unit。没有就是 null（只写数，不编单位）。
 */
export function valueUnit(meta: StreamMeta, channel: string | null): string | null {
  const m = meta.meta;
  const units = m.units;
  const perChannel = typeof units === "object" && units !== null && !Array.isArray(units);
  if (channel && perChannel) {
    const u = (units as Record<string, unknown>)[channel];
    if (typeof u === "string" && u) return u;
  }
  if (typeof m.value_unit === "string" && m.value_unit) return m.value_unit;
  if (meta.kind === "map2d" || perChannel) return null;
  return meta.y_unit;
}

/** 缓冲里的点数、最新一点的横坐标与各通道读数（图例常驻显示的那一行）。 */
export function seriesLatest(buf: ScalarBuf | XyBuf): {
  n: number;
  x: number | null;
  values: Record<string, number | null>;
} {
  if (buf.kind === "scalar") {
    const i = buf.seqs.length - 1;
    const values: Record<string, number | null> = {};
    for (const c of buf.channels) values[c] = i >= 0 ? (buf.values.get(c)?.[i] ?? null) : null;
    return { n: buf.seqs.length, x: i >= 0 ? (buf.t[i] ?? null) : null, values };
  }
  const p = buf.points[buf.points.length - 1];
  const values: Record<string, number | null> = {};
  for (const c of buf.channels) values[c] = p?.values[c] ?? null;
  return { n: buf.points.length, x: p?.x ?? null, values };
}

/**
 * 各通道 max|v| 的最大/最小比（不到两个有数的通道时为 1）。比值大 = 同一个纵轴上
 * 小的那条会被压成一条直线，该分开画。
 */
export function magnitudeSpread(buf: ScalarBuf | XyBuf, channels: string[]): number {
  const mags: number[] = [];
  for (const c of channels) {
    let m = 0;
    if (buf.kind === "scalar") {
      for (const v of buf.values.get(c) ?? []) if (v !== null && Number.isFinite(v)) m = Math.max(m, Math.abs(v));
    } else {
      for (const p of buf.points) {
        const v = p.values[c];
        if (v !== null && v !== undefined && Number.isFinite(v)) m = Math.max(m, Math.abs(v));
      }
    }
    if (m > 0) mags.push(m);
  }
  if (mags.length < 2) return 1;
  return Math.max(...mags) / Math.min(...mags);
}

// ── map2d 坐标 ─────────────────────────────────────────────────────────────

/** 像素中心的物理坐标。 */
export function pixelCoord(g: Map2dGeometry, ix: number, iy: number): { x: number; y: number } {
  return { x: g.x0 + ix * g.dx, y: g.y0 + iy * g.dy };
}

/**
 * 整幅图的物理范围（像素边缘，不是中心）。x 向右增大、y 向上增大：dy < 0 时
 * iy = 0 在最上面 —— 方向跟着物理坐标走，不跟着数组下标走。
 */
export function mapExtent(g: Map2dGeometry): { xMin: number; xMax: number; yMin: number; yMax: number } {
  const xa = g.x0 - g.dx / 2;
  const xb = g.x0 + (g.nx - 0.5) * g.dx;
  const ya = g.y0 - g.dy / 2;
  const yb = g.y0 + (g.ny - 0.5) * g.dy;
  return { xMin: Math.min(xa, xb), xMax: Math.max(xa, xb), yMin: Math.min(ya, yb), yMax: Math.max(ya, yb) };
}

/**
 * 像素 (ix, iy) 在画布上的位置（以像素格为单位，原点左上）。
 * 返回画布格坐标 (cx, cy)：cx 随物理 x 增大向右，cy 随物理 y 增大向上（即 cy 变小）。
 */
export function pixelToCanvasCell(g: Map2dGeometry, ix: number, iy: number): { cx: number; cy: number } {
  const cx = g.dx > 0 ? ix : g.nx - 1 - ix;
  const cy = g.dy > 0 ? g.ny - 1 - iy : iy;
  return { cx, cy };
}

/** 画布格坐标 → 像素下标（悬停读数用；pixelToCanvasCell 的逆）。 */
export function canvasCellToPixel(g: Map2dGeometry, cx: number, cy: number): { ix: number; iy: number } {
  const ix = g.dx > 0 ? cx : g.nx - 1 - cx;
  const iy = g.dy > 0 ? g.ny - 1 - cy : cy;
  return { ix, iy };
}
