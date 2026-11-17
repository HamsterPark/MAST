// ════════════════════════════════════════════════════════════════════════════
// The scan in progress, row by row — the pure half of the scan map's live layer.
//
// Backend: mast/monitoring/scan_live.py; design: docs/v2/design/scan_map_live.md.
// While a scan runs the backend publishes each newly completed row of the frame
// as LiveHub map2d pixels on `local/scan/live/<n>` (one stream per scan frame)
// and keeps `local/scan/live` (scalar) up to date: active, session, rows done,
// rows, direction, interval. Everything decidable in plain data is decided here
// and tested with `node --test`; the Konva component only blits the result.
//
// The picture is processed the way the saved .sxm thumbnails are
// (mast/webui/scan_preview.py): each row minus its own least-squares line, a
// 1–99 % percentile range, afmhot. When the finished .sxm replaces the live
// image on the map, it does not change colour under the operator's eyes.
//
// No imports from ../api/schema, so this runs under node --test with no build.
// ════════════════════════════════════════════════════════════════════════════

import { map2dChangesSince } from "./live/store.ts";

export const LIVE_STATUS_STREAM = "local/scan/live";
export const LIVE_FRAME_PREFIX = "local/scan/live/";

export function liveFrameStreamId(session: number): string {
  return `${LIVE_FRAME_PREFIX}${session}`;
}

// ── the status stream ──────────────────────────────────────────────────────

export interface LiveStatus {
  active: boolean;
  session: number | null;
  rowsDone: number;
  rows: number;
  /** +1 up-scan (bottom → top), −1 down-scan (top → bottom), 0 unknown. */
  dir: 1 | -1 | 0;
  intervalS: number | null;
  degraded: boolean;
  /** A scan is running but its frame is too large to grab (> 4 MiB): the map
   *  keeps the 1/8 snapshots. Lines x pixels of that frame. */
  tooLarge: { lines: number; pixels: number } | null;
  /** The TCP circuit breaker is not closed: no grab goes out until it is. */
  commsHold: boolean;
}

interface ScalarLike {
  kind: string;
  values?: Map<string, (number | null)[]>;
}

function latest(buf: ScalarLike, key: string): number | null {
  const arr = buf.values?.get(key);
  if (!arr || !arr.length) return null;
  const v = arr[arr.length - 1];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** The newest status the scalar stream carries, or null without one. */
export function readLiveStatus(buf: ScalarLike | null | undefined): LiveStatus | null {
  if (!buf || buf.kind !== "scalar") return null;
  const active = latest(buf, "active");
  if (active === null) return null;
  const on = active >= 0.5;
  const session = on ? latest(buf, "session") : null;
  const dir = latest(buf, "dir");
  const big = on && (latest(buf, "too_large") ?? 0) >= 0.5;
  return {
    active: on,
    session: session !== null && session >= 1 ? Math.round(session) : null,
    rowsDone: on ? Math.max(0, latest(buf, "rows_done") ?? 0) : 0,
    rows: on ? Math.max(0, latest(buf, "rows") ?? 0) : 0,
    dir: on && dir !== null ? (dir > 0 ? 1 : dir < 0 ? -1 : 0) : 0,
    intervalS: on ? latest(buf, "interval_s") : null,
    degraded: on && (latest(buf, "degraded") ?? 0) >= 0.5,
    tooLarge: big
      ? { lines: Math.round(latest(buf, "src_lines") ?? 0), pixels: Math.round(latest(buf, "src_pixels") ?? 0) }
      : null,
    commsHold: on && (latest(buf, "comms_hold") ?? 0) >= 0.5,
  };
}

// ── a session's geometry ───────────────────────────────────────────────────

export interface LiveScanMeta {
  nx: number;
  ny: number;
  cx: number;
  cy: number;
  w: number;
  h: number;
  angleDeg: number;
  scanDir: "up" | "down";
  /** "nan_front" = inferred from the scan front; "assumed" = guessed. */
  row0Source: string;
  valueKey: string;
  unit: string;
  channel: string;
  /** Scan lines in the source frame, and source lines per published row. */
  srcLines: number;
  binY: number;
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** The geometry the backend declared, or null when it cannot be placed. */
export function parseLiveScanMeta(m: Record<string, unknown> | null | undefined): LiveScanMeta | null {
  if (!m) return null;
  const nx = num(m.nx);
  const ny = num(m.ny);
  const cx = num(m.center_x_m);
  const cy = num(m.center_y_m);
  const w = num(m.width_m);
  const h = num(m.height_m);
  if (nx === null || ny === null || cx === null || cy === null || w === null || h === null) return null;
  if (!Number.isInteger(nx) || !Number.isInteger(ny) || nx < 1 || ny < 1 || !(w > 0) || !(h > 0)) return null;
  const key = typeof m.value_key === "string" && m.value_key ? m.value_key : "";
  const units = (m.units && typeof m.units === "object" ? (m.units as Record<string, unknown>) : {});
  return {
    nx,
    ny,
    cx,
    cy,
    w,
    h,
    angleDeg: num(m.angle_deg) ?? 0,
    scanDir: m.scan_dir === "up" ? "up" : "down",
    row0Source: typeof m.row0_source === "string" ? m.row0_source : "assumed",
    valueKey: key,
    unit: typeof units[key] === "string" ? (units[key] as string) : "",
    channel: typeof m.channel === "string" ? m.channel : "",
    srcLines: num(m.src_lines) ?? ny,
    binY: Array.isArray(m.bin) ? Math.max(1, num(m.bin[0]) ?? 1) : 1,
  };
}

/** "123/256" in SOURCE scan lines — the published rows may be binned. */
export function linesDoneLabel(meta: Pick<LiveScanMeta, "srcLines" | "binY"> | null, rowsDone: number, rows: number): string {
  if (!meta) return `${rowsDone}/${rows}`;
  return `${Math.min(meta.srcLines, rowsDone * meta.binY)}/${meta.srcLines}`;
}

// ── the picture ────────────────────────────────────────────────────────────

/**
 * One row minus its own least-squares line, over the finite pixels only (the
 * .sxm thumbnails' default "line" flatten). Fewer than two finite pixels:
 * the row minus its mean. NaN stays NaN.
 */
export function detrendRow(src: ArrayLike<number>, start: number, n: number, out: Float64Array, outStart: number): void {
  let s = 0, sx = 0, sy = 0, sxx = 0, sxy = 0;
  for (let i = 0; i < n; i++) {
    const v = src[start + i]!;
    if (!Number.isFinite(v)) continue;
    s += 1; sx += i; sy += v; sxx += i * i; sxy += i * v;
  }
  let a = 0;
  let b = 0;
  if (s >= 2) {
    const den = s * sxx - sx * sx;
    b = den !== 0 ? (s * sxy - sx * sy) / den : 0;
    a = (sy - b * sx) / s;
  } else if (s === 1) {
    a = sy;
  }
  for (let i = 0; i < n; i++) {
    const v = src[start + i]!;
    out[outStart + i] = Number.isFinite(v) ? v - (a + b * i) : Number.NaN;
  }
}

/**
 * The [lo, hi] percentile range of the finite values, estimated from at most
 * `maxSamples` evenly strided samples — an exact sort of a 512² image on
 * every update is a frame budget the map does not have. Null without data; a
 * flat image gets a symmetric unit-free span so it still draws.
 */
export function robustRange(
  values: ArrayLike<number>,
  lo = 0.01,
  hi = 0.99,
  maxSamples = 20_000,
): [number, number] | null {
  const stride = Math.max(1, Math.floor(values.length / maxSamples));
  const sample: number[] = [];
  for (let i = 0; i < values.length; i += stride) {
    const v = values[i]!;
    if (Number.isFinite(v)) sample.push(v);
  }
  if (!sample.length) return null;
  sample.sort((p, q) => p - q);
  const at = (f: number) => sample[Math.min(sample.length - 1, Math.max(0, Math.round(f * (sample.length - 1))))]!;
  let a = at(lo);
  let b = at(hi);
  if (!(b > a)) {
    a = sample[0]!;
    b = sample[sample.length - 1]!;
  }
  if (!(b > a)) {
    a -= 0.5;
    b += 0.5;
  }
  return [a, b];
}

/** True when the range moved by more than `tol` of its span — repaint everything then. */
export function rangeMoved(a: [number, number] | null, b: [number, number] | null, tol = 0.1): boolean {
  if (!a || !b) return a !== b;
  const span = Math.max(a[1] - a[0], b[1] - b[0], Number.EPSILON);
  return Math.abs(a[0] - b[0]) > tol * span || Math.abs(a[1] - b[1]) > tol * span;
}

let AFMHOT: Uint8ClampedArray | null = null;

/** matplotlib's afmhot as a 256-step RGB table: r = 2t, g = 2t − ½, b = 2t − 1, clipped. */
export function afmhotLut(): Uint8ClampedArray {
  if (AFMHOT) return AFMHOT;
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    const c = (x: number) => Math.round(255 * Math.min(1, Math.max(0, x)));
    lut[3 * i] = c(2 * t);
    lut[3 * i + 1] = c(2 * t - 0.5);
    lut[3 * i + 2] = c(2 * t - 1);
  }
  AFMHOT = lut;
  return lut;
}

/** The subset of the store's Map2dBuf the model reads. */
export interface Map2dLike {
  kind: string;
  values: Map<string, Float64Array>;
  log: Int32Array;
  logCount: number;
}

/**
 * The live image, kept up to date incrementally from a LiveStore map2d buffer.
 *
 * `rgba` is an nx × ny RGBA picture with canvas row 0 at the TOP; the stream
 * has iy = 0 at the bottom. A pixel with no data is fully transparent — the
 * part of the frame not scanned yet shows the map underneath, as on Nanonis.
 *
 * Driven by the store's per-view write log (map2dChangesSince): only rows that
 * received pixels are re-flattened and repainted — including rows filled in
 * late by a REST backfill after the WebSocket dropped frames, which arrive out
 * of seq order but still go through the log. A view that fell further behind
 * than the log holds is told to start over, and repaints the whole picture.
 */
export class LiveImageModel {
  readonly nx: number;
  readonly ny: number;
  readonly flat: Float64Array;
  readonly rgba: Uint8ClampedArray<ArrayBuffer>;
  range: [number, number] | null = null;
  private cursor = 0;
  private readonly lut: Uint8ClampedArray;

  constructor(nx: number, ny: number, lut: Uint8ClampedArray = afmhotLut()) {
    this.nx = nx;
    this.ny = ny;
    this.flat = new Float64Array(nx * ny).fill(Number.NaN);
    this.rgba = new Uint8ClampedArray(nx * ny * 4);
    this.lut = lut;
  }

  /** Bring the picture up to date. Returns the rows repainted (iy), "all", or null (nothing to read). */
  update(buf: Map2dLike | null | undefined, valueKey: string): number[] | "all" | null {
    if (!buf || buf.kind !== "map2d") return null;
    const values = buf.values.get(valueKey) ?? buf.values.values().next().value;
    if (!values || values.length !== this.nx * this.ny) return null;
    const ch = map2dChangesSince(buf as Parameters<typeof map2dChangesSince>[0], this.cursor);
    this.cursor = ch.next;
    let rows: number[];
    if (ch.full) {
      rows = Array.from({ length: this.ny }, (_, i) => i);
    } else {
      const set = new Set<number>();
      for (const idx of ch.idx) set.add(Math.floor(idx / this.nx));
      rows = [...set];
    }
    if (!rows.length) return [];
    for (const iy of rows) detrendRow(values, iy * this.nx, this.nx, this.flat, iy * this.nx);
    const next = robustRange(this.flat);
    if (ch.full || rangeMoved(this.range, next)) {
      this.range = next;
      for (let iy = 0; iy < this.ny; iy++) this.paintRow(iy);
      return "all";
    }
    for (const iy of rows) this.paintRow(iy);
    return rows.sort((a, b) => a - b);
  }

  private paintRow(iy: number): void {
    const r = this.range;
    const top = this.ny - 1 - iy;
    for (let ix = 0; ix < this.nx; ix++) {
      const v = this.flat[iy * this.nx + ix]!;
      const o = (top * this.nx + ix) * 4;
      if (!Number.isFinite(v) || !r) {
        this.rgba[o + 3] = 0;
        continue;
      }
      const t = Math.min(1, Math.max(0, (v - r[0]) / (r[1] - r[0])));
      const k = Math.round(t * 255) * 3;
      this.rgba[o] = this.lut[k]!;
      this.rgba[o + 1] = this.lut[k + 1]!;
      this.rgba[o + 2] = this.lut[k + 2]!;
      this.rgba[o + 3] = 255;
    }
  }

  /** Rows (iy) holding at least one pixel, and of those, rows still missing some. */
  coverage(): { rowsWithData: number; partial: number[] } {
    let rowsWithData = 0;
    const partial: number[] = [];
    for (let iy = 0; iy < this.ny; iy++) {
      let have = 0;
      for (let ix = 0; ix < this.nx; ix++) if (Number.isFinite(this.flat[iy * this.nx + ix]!)) have++;
      if (have > 0) rowsWithData++;
      if (have > 0 && have < this.nx) partial.push(iy);
    }
    return { rowsWithData, partial };
  }
}

// ── the scan line ──────────────────────────────────────────────────────────

/**
 * The scan line in world metres: across the frame's width, at the scan front.
 * A down-scan runs top to bottom, an up-scan bottom to top; the line rotates
 * with the frame (angle counter-clockwise, about the centre — the convention
 * the canvas draws footprints with).
 */
export function scanLineWorld(
  meta: Pick<LiveScanMeta, "cx" | "cy" | "w" | "h" | "angleDeg">,
  rowsDone: number,
  rows: number,
  dir: number,
): { a: { x: number; y: number }; b: { x: number; y: number }; frac: number } | null {
  if (!(rows > 0) || !Number.isFinite(rowsDone) || dir === 0) return null;
  const frac = Math.min(1, Math.max(0, rowsDone / rows));
  const yl = dir > 0 ? -meta.h / 2 + frac * meta.h : meta.h / 2 - frac * meta.h;
  const th = ((Number.isFinite(meta.angleDeg) ? meta.angleDeg : 0) * Math.PI) / 180;
  const c = Math.cos(th);
  const s = Math.sin(th);
  const at = (xl: number) => ({ x: meta.cx + xl * c - yl * s, y: meta.cy + xl * s + yl * c });
  return { a: at(-meta.w / 2), b: at(meta.w / 2), frac };
}

// ── when to draw it, when to fall back ─────────────────────────────────────

/**
 * How long after the last status the feed still counts as live. The backend
 * publishes the status on every successful tick (at most 10 s apart) whether or
 * not a row completed, so this measures "the feed is alive", also for a slow
 * scan whose rows come a minute apart. A busy data port publishes nothing; so
 * does a hung service — after this long the map falls back to the PNG.
 */
export function liveStaleMs(intervalS: number | null): number {
  const iv = intervalS !== null && Number.isFinite(intervalS) && intervalS > 0 ? intervalS : 1;
  return 3 * iv * 1000 + 5_000;
}

/**
 * Whether the row-by-row feed is live, so the stream is drawn instead of the
 * 1/8 milestone PNG: the backend says a session is active, the session's
 * stream has pixels, and the status is recent enough. Anything else — switched
 * off, WebSocket and polling both down, a session that has not reached its
 * first row, a stalled feed — falls back to the PNG. (Right after a scan ends
 * the view keeps the stream's picture for a grace period; see LiveScanLayer.)
 */
export function liveScanUsable(
  status: LiveStatus | null,
  feed: { filled: number; lastStatusMs: number | null } | null,
  nowMs: number,
): boolean {
  if (!status?.active || status.session === null || !feed) return false;
  if (!(feed.filled > 0) || feed.lastStatusMs === null) return false;
  return nowMs - feed.lastStatusMs <= liveStaleMs(status.intervalS);
}

/**
 * A stream that still misses frames while nothing new is arriving needs its
 * backfill re-kicked: the client's backfill loop gives up after a bounded number
 * of pages per run and is otherwise re-triggered only by new frames — which
 * stop coming the moment the scan ends.
 */
export function needsBackfillKick(
  st: { missing: { isEmpty(): boolean }; lastRecvMs: number | null } | null | undefined,
  nowMs: number,
  quietMs = 3_000,
): boolean {
  if (!st || st.missing.isEmpty()) return false;
  return st.lastRecvMs === null || nowMs - st.lastRecvMs >= quietMs;
}
