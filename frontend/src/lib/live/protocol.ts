// ════════════════════════════════════════════════════════════════════════════
// LiveHub 线上格式（契约 docs/v2/design/ters_satellite_integration.md §7）。
//
// 这一层只做「把一段文本变成一个有类型的消息」，不持有任何状态。和 lib/ws.ts 的
// parseFrame 同一个立场：**一条认不出的消息是一个要忽略的事实，不是要抛的错误**。
// LiveHub 的 kind 是开放集合 —— 以后谁新声明一种 kind，老前端必须照样能把它列出来
// （用通用画法），而不是整条连接被一条新消息掐断。
//
//   客户端 → 服务端
//     {"op":"subscribe","streams":["*"] | [id…],"since":{id: seq}}
//     {"op":"unsubscribe","streams":[id…]}
//   服务端 → 客户端（实现见 MASTv2/mast/api/routes/live.py 的模块说明）
//     {"kind":"streams","hub","items":[meta…]}     每次都是完整目录；hub 变了 = MAST 重启过
//     {"kind":"axis","stream","ver","x","unit"}
//     {"kind":"frame","stream","seq","frame"}
//     {"kind":"frames","stream","items":[{seq, frame}…]}   增量流与重放的合批
//     {"kind":"ping"}                               15 s 没别的消息时
//     {"kind":"dropped","stream","from","to"}
//     {"kind":"error","op","message"}               我们发的消息格式不对（连接照常）
//   每个流的 meta 带 uid：流被重建（换 kind / 地图尺寸、MAST 重启）时换一个，客户端据此清屏。
//
// 帧（按流的 kind）：
//   spectrum {t, y:[float], axis_ver, meta?}
//   scalar   {t, values:{name: float}}
//   xy       {t, x, values:{name: float}}
//   map2d    {t, ix, iy, values:{name: float}}   declare 的 meta 给 {nx,ny,x0,y0,dx,dy,x_unit,y_unit}
//   status   {t, text, level}
//
// 数值里的 null：Python 的 json.dumps 默认把 NaN 写成裸 `NaN`，那不是合法 JSON，
// 浏览器的 JSON.parse 会整条拒收。服务端应当把非有限值写成 null；这里把 null 当
// 「这一点没有读数」—— **绝不当 0**（0 是合法读数，见 LiveCurrentChart 的同一条规矩）。
// ════════════════════════════════════════════════════════════════════════════

/** 前端认得、有专门画法的 kind。别的 kind 照样列出，用通用视图。 */
export const KNOWN_KINDS = ["spectrum", "scalar", "xy", "map2d", "status"] as const;
export type KnownKind = (typeof KNOWN_KINDS)[number];

export function isKnownKind(k: string): k is KnownKind {
  return (KNOWN_KINDS as readonly string[]).includes(k);
}

/**
 * 增量流：每一帧都是图的一部分，丢一帧图就缺一块 —— 缺了要补。
 * spectrum / scalar 是「只看最新」：服务端推给浏览器时按流限速、只留最新一帧，
 * seq 跳号是常态，不是丢帧。
 */
export function isIncrementalKind(kind: string): boolean {
  return kind === "xy" || kind === "map2d" || kind === "status";
}

export interface StreamMeta {
  id: string;
  /** 开放集合。不认识的 kind 用通用视图显示，不丢弃。 */
  kind: string;
  title: string;
  source: string;
  x_label: string | null;
  x_unit: string | null;
  y_label: string | null;
  y_unit: string | null;
  meta: Record<string, unknown>;
  retain: number | null;
  ended: boolean;
  /** 服务端这个流最新的 seq；不知道时为 -1。 */
  last_seq: number;
  /** 流的代次标记（服务端的 uid）：变了就清空重来。没有时靠 last_seq 倒退来识别重启。 */
  epoch: string | null;
}

export interface Map2dGeometry {
  nx: number;
  ny: number;
  x0: number;
  y0: number;
  dx: number;
  dy: number;
  x_unit: string;
  y_unit: string;
}

/** 单幅图允许的最大像素数。契约是 ≤ 262144；放宽到 4 倍是为了容忍，不是为了鼓励。 */
export const MAP2D_MAX_PIXELS = 1_048_576;

export type LiveMessage =
  | { kind: "streams"; hub: string | null; items: StreamMeta[] }
  | { kind: "axis"; stream: string; ver: number; x: Float64Array; unit: string }
  | { kind: "frame"; stream: string; seq: number; frame: unknown }
  | { kind: "frames"; stream: string; items: Array<{ seq: number; frame: unknown }> }
  | { kind: "ping" }
  | { kind: "dropped"; stream: string; from: number; to: number }
  | { kind: "error"; op: string | null; message: string }
  | { kind: "ignored"; why: string };

// ── 小工具 ──────────────────────────────────────────────────────────────────

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function finite(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function str(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

/** 非负整数 seq；别的一律 null。 */
function seqOf(v: unknown): number | null {
  const n = finite(v);
  return n !== null && n >= 0 && Number.isInteger(n) ? n : null;
}

/** 一串数字（允许 null）→ Float64Array，null 变 NaN。不是数组时返回 null。 */
export function toFloat64(v: unknown): Float64Array | null {
  if (!Array.isArray(v)) return null;
  const out = new Float64Array(v.length);
  for (let i = 0; i < v.length; i++) {
    const x = v[i];
    out[i] = typeof x === "number" && Number.isFinite(x) ? x : Number.NaN;
  }
  return out;
}

/** values 对象 → 名字到数值；null / 非数值写成 null（没有读数）。 */
function valuesOf(v: unknown): Record<string, number | null> | null {
  if (!isObj(v)) return null;
  const out: Record<string, number | null> = {};
  for (const [k, x] of Object.entries(v)) {
    out[k] = typeof x === "number" && Number.isFinite(x) ? x : null;
  }
  return out;
}

// ── 流的元数据 ──────────────────────────────────────────────────────────────

/** 服务端给的一项 → StreamMeta。缺 id 或 kind 的项丢弃（没法挂到任何东西上）。 */
export function normalizeStreamMeta(raw: unknown): StreamMeta | null {
  if (!isObj(raw)) return null;
  // 键名以契约的 declare(stream_id, …) 为准；`stream` / `stream_id` 也认，免得
  // 一个键名之差让整页列表是空的。
  const id = str(raw.id) ?? str(raw.stream_id) ?? str(raw.stream);
  const kind = str(raw.kind);
  if (!id || !kind) return null;
  const last = finite(raw.last_seq);
  const epochRaw = raw.uid ?? raw.epoch;
  return {
    id,
    kind,
    title: str(raw.title) ?? id,
    source: str(raw.source) ?? "",
    x_label: str(raw.x_label),
    x_unit: str(raw.x_unit),
    y_label: str(raw.y_label),
    y_unit: str(raw.y_unit),
    meta: isObj(raw.meta) ? raw.meta : {},
    retain: finite(raw.retain),
    ended: raw.ended === true,
    last_seq: last !== null && last >= 0 ? Math.floor(last) : -1,
    epoch: typeof epochRaw === "string" || typeof epochRaw === "number" ? String(epochRaw) : null,
  };
}

/**
 * map2d 的几何（declare 的 meta 里）。缺字段或不合理就返回 null —— 调用方要把
 * 「这个流没法画」说出来，而不是拿默认值硬画一幅位置全错的图。
 */
export function map2dGeometry(meta: StreamMeta): Map2dGeometry | null {
  const m = meta.meta;
  const nx = finite(m.nx);
  const ny = finite(m.ny);
  if (nx === null || ny === null) return null;
  if (!Number.isInteger(nx) || !Number.isInteger(ny) || nx < 1 || ny < 1) return null;
  if (nx * ny > MAP2D_MAX_PIXELS) return null;
  const dx = finite(m.dx) ?? 1;
  const dy = finite(m.dy) ?? 1;
  if (dx === 0 || dy === 0) return null;
  return {
    nx,
    ny,
    x0: finite(m.x0) ?? 0,
    y0: finite(m.y0) ?? 0,
    dx,
    dy,
    x_unit: str(m.x_unit) ?? meta.x_unit ?? "",
    y_unit: str(m.y_unit) ?? meta.y_unit ?? "",
  };
}

// ── 消息 ────────────────────────────────────────────────────────────────────

/** 解一条服务端文本消息。**永不抛。** */
export function parseLiveMessage(raw: string): LiveMessage {
  let msg: unknown;
  try {
    msg = JSON.parse(raw);
  } catch {
    return { kind: "ignored", why: "malformed JSON" };
  }
  if (!isObj(msg)) return { kind: "ignored", why: "not an object" };
  switch (msg.kind) {
    case "ping":
      return { kind: "ping" };
    case "streams": {
      if (!Array.isArray(msg.items)) return { kind: "ignored", why: "streams without items" };
      const items: StreamMeta[] = [];
      for (const it of msg.items) {
        const m = normalizeStreamMeta(it);
        if (m) items.push(m);
      }
      return { kind: "streams", hub: str(msg.hub), items };
    }
    case "frames": {
      const stream = str(msg.stream);
      if (!stream || !Array.isArray(msg.items)) return { kind: "ignored", why: "frames missing stream/items" };
      const items: Array<{ seq: number; frame: unknown }> = [];
      for (const it of msg.items) {
        if (!isObj(it)) continue;
        const seq = seqOf(it.seq);
        if (seq !== null) items.push({ seq, frame: it.frame });
      }
      return { kind: "frames", stream, items };
    }
    case "error":
      return { kind: "error", op: str(msg.op), message: str(msg.message) ?? "" };
    case "axis": {
      const stream = str(msg.stream);
      const ver = finite(msg.ver);
      const x = toFloat64(msg.x);
      if (!stream || ver === null || !x) return { kind: "ignored", why: "axis missing fields" };
      return { kind: "axis", stream, ver, x, unit: str(msg.unit) ?? "" };
    }
    case "frame": {
      const stream = str(msg.stream);
      const seq = seqOf(msg.seq);
      if (!stream || seq === null) return { kind: "ignored", why: "frame missing stream/seq" };
      return { kind: "frame", stream, seq, frame: msg.frame };
    }
    case "dropped": {
      const stream = str(msg.stream);
      const from = seqOf(msg.from);
      const to = seqOf(msg.to);
      if (!stream || from === null || to === null || to < from) {
        return { kind: "ignored", why: "dropped missing fields" };
      }
      return { kind: "dropped", stream, from, to };
    }
    default:
      return { kind: "ignored", why: `unknown kind ${String(msg.kind)}` };
  }
}

export interface SubscribeOp {
  op: "subscribe";
  streams: string[];
  since?: Record<string, number>;
}

export interface UnsubscribeOp {
  op: "unsubscribe";
  streams: string[];
}

/** 订阅消息。`since` 只带真有游标（>0）的流 —— 冷启动的流让服务端自己决定给多少。 */
export function subscribeOp(streams: string[], cursors: Record<string, number>): SubscribeOp {
  const since: Record<string, number> = {};
  for (const id of streams) {
    const c = cursors[id];
    if (typeof c === "number" && c > 0) since[id] = c;
  }
  const op: SubscribeOp = { op: "subscribe", streams: [...streams] };
  if (Object.keys(since).length) op.since = since;
  return op;
}

export function unsubscribeOp(streams: string[]): UnsubscribeOp {
  return { op: "unsubscribe", streams: [...streams] };
}

// ── 帧 ──────────────────────────────────────────────────────────────────────

export interface SpectrumFrame {
  t: number | null;
  y: Float64Array;
  axis_ver: number | null;
  meta: Record<string, unknown> | null;
}

export interface ScalarFrame {
  t: number | null;
  values: Record<string, number | null>;
}

export interface XyFrame {
  t: number | null;
  x: number;
  values: Record<string, number | null>;
}

export interface Map2dFrame {
  t: number | null;
  ix: number;
  iy: number;
  values: Record<string, number | null>;
}

export interface StatusFrame {
  t: number | null;
  text: string;
  level: string;
}

export function decodeSpectrumFrame(f: unknown): SpectrumFrame | null {
  if (!isObj(f)) return null;
  const y = toFloat64(f.y);
  if (!y) return null;
  return {
    t: finite(f.t),
    y,
    axis_ver: finite(f.axis_ver),
    meta: isObj(f.meta) ? f.meta : null,
  };
}

export function decodeScalarFrame(f: unknown): ScalarFrame | null {
  if (!isObj(f)) return null;
  const values = valuesOf(f.values);
  if (!values) return null;
  return { t: finite(f.t), values };
}

export function decodeXyFrame(f: unknown): XyFrame | null {
  if (!isObj(f)) return null;
  const x = finite(f.x);
  const values = valuesOf(f.values);
  if (x === null || !values) return null;
  return { t: finite(f.t), x, values };
}

export function decodeMap2dFrame(f: unknown): Map2dFrame | null {
  if (!isObj(f)) return null;
  const ix = finite(f.ix);
  const iy = finite(f.iy);
  const values = valuesOf(f.values);
  if (ix === null || iy === null || !values) return null;
  if (!Number.isInteger(ix) || !Number.isInteger(iy)) return null;
  return { t: finite(f.t), ix, iy, values };
}

export function decodeStatusFrame(f: unknown): StatusFrame | null {
  if (!isObj(f)) return null;
  const text = str(f.text);
  if (text === null) return null;
  return { t: finite(f.t), text, level: str(f.level) ?? "info" };
}

/**
 * 帧时间 t → unix 秒。契约没写单位：Python 侧 time.time() 是秒；万一给的是毫秒
 * （> 1e11，即公元 5138 年以后的秒数），按毫秒折算。null 原样返回。
 */
export function epochSeconds(t: number | null): number | null {
  if (t === null || !Number.isFinite(t)) return null;
  return t > 1e11 ? t / 1000 : t;
}

// ── REST（轮询兜底 / 补齐） ─────────────────────────────────────────────────

export interface RestAxis {
  ver: number;
  x: Float64Array;
  unit: string;
}

export interface RestFrames {
  hub: string | null;
  meta: StreamMeta | null;
  /** 当前横轴。 */
  axis: RestAxis | null;
  /** 返回的帧引用到的其它（较旧的）横轴版本。 */
  axes: RestAxis[];
  frames: Array<{ seq: number; frame: unknown }>;
  /** 被 limit 截断了：用最后一帧的 seq 当 since 接着取。 */
  more: boolean;
  /** since 之后有一段已经不在服务端保留区里（补不回来了）。 */
  dropped: { from: number; to: number } | null;
}

function restAxis(v: unknown): RestAxis | null {
  if (!isObj(v)) return null;
  const ver = finite(v.ver);
  const x = toFloat64(v.x);
  return ver !== null && x ? { ver, x, unit: str(v.unit) ?? "" } : null;
}

/** GET /api/live/streams/{id}?since=&limit= 的响应体。认不出的部分丢弃。 */
export function parseRestFrames(raw: unknown): RestFrames {
  const out: RestFrames = { hub: null, meta: null, axis: null, axes: [], frames: [], more: false, dropped: null };
  if (!isObj(raw)) return out;
  out.hub = str(raw.hub);
  out.meta = normalizeStreamMeta(raw.meta);
  out.axis = restAxis(raw.axis);
  if (Array.isArray(raw.axes)) {
    for (const a of raw.axes) {
      const ax = restAxis(a);
      if (ax) out.axes.push(ax);
    }
  }
  out.more = raw.more === true;
  if (isObj(raw.dropped)) {
    const from = seqOf(raw.dropped.from);
    const to = seqOf(raw.dropped.to);
    if (from !== null && to !== null && to >= from) out.dropped = { from, to };
  }
  if (Array.isArray(raw.frames)) {
    for (const it of raw.frames) {
      if (!isObj(it)) continue;
      const seq = seqOf(it.seq);
      if (seq === null) continue;
      out.frames.push({ seq, frame: it.frame });
    }
    out.frames.sort((a, b) => a.seq - b.seq);
  }
  return out;
}

/** GET /api/live/streams 的响应体（`{hub, items:[…]}` 或裸数组都认）。 */
export function parseRestStreamList(raw: unknown): { hub: string | null; items: StreamMeta[] } | null {
  const items = Array.isArray(raw) ? raw : isObj(raw) ? (raw.items ?? raw.streams) : null;
  if (!Array.isArray(items)) return null;
  const out: StreamMeta[] = [];
  for (const it of items) {
    const m = normalizeStreamMeta(it);
    if (m) out.push(m);
  }
  return { hub: isObj(raw) ? str(raw.hub) : null, items: out };
}

/** 只要流的列表（不关心 hub id）时用。 */
export function parseRestStreams(raw: unknown): StreamMeta[] | null {
  return parseRestStreamList(raw)?.items ?? null;
}

/** 流 id 拼进 URL：每段各自转义，斜杠保留（后端路由是 `{stream_id:path}`）。 */
export function streamPath(id: string): string {
  return id.split("/").map(encodeURIComponent).join("/");
}
