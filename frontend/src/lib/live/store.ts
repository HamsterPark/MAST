// ════════════════════════════════════════════════════════════════════════════
// LiveStore —— 浏览器这一侧每个 LiveHub 流的缓存。
//
// 传输层（lib/liveClient.ts）把 WS 消息和 REST 补齐的结果都交给这里；画图组件
// 只从这里读。两条来源会重叠（重连时 WS 从 since 往后重放、REST 同时在补一段），
// 所以**每一种写入都是幂等的**：同一个 seq 来两次，第二次是 duplicate。
//
// 三条不变量（liveStore.test.ts 逐条钉着）：
//   1. 增量流（xy / map2d / status）跳号就记「缺」，补到了就从「缺」里划掉；补不回来
//      的记 lost 并且在界面上说出来 —— 缺了一块的图不能看起来是完整的。
//   2. 「只看最新」的流（spectrum / scalar）跳号是常态（服务端按 20 Hz 只推最新帧），
//      不记缺；历史帧经 REST 进来时按 seq 插回原位。
//   3. 服务端重启后 seq 从头开始。认出重启（代次标记变了，或新连接上第一份列表里
//      last_seq 比本地游标还小）就清空这个流重来 —— 否则之后每一帧都 seq ≤ 游标，
//      全被当成重复丢掉，图停在重启前那一刻，而且看起来一切正常。
// ════════════════════════════════════════════════════════════════════════════

import { SeqRanges } from "./seqRanges.ts";
import {
  decodeMap2dFrame,
  decodeScalarFrame,
  decodeSpectrumFrame,
  decodeStatusFrame,
  decodeXyFrame,
  epochSeconds,
  isIncrementalKind,
  map2dGeometry,
  type Map2dGeometry,
  type StreamMeta,
} from "./protocol.ts";

export interface AxisState {
  ver: number;
  x: Float64Array;
  unit: string;
}

export interface SpectrumEntry {
  seq: number;
  /** 帧时间（unix 秒）；服务端没给时为 null。 */
  t: number | null;
  y: Float64Array;
  axisVer: number | null;
  meta: Record<string, unknown> | null;
}

export interface SpectrumBuf {
  kind: "spectrum";
  /** 按 seq 升序。 */
  entries: SpectrumEntry[];
}

export interface ScalarBuf {
  kind: "scalar";
  /** 下面四个数组等长、按 seq 升序。 */
  seqs: number[];
  t: (number | null)[];
  /** 通道出现的先后顺序。 */
  channels: string[];
  values: Map<string, (number | null)[]>;
}

export interface XyPoint {
  seq: number;
  t: number | null;
  x: number;
  values: Record<string, number | null>;
}

export interface XyBuf {
  kind: "xy";
  /** 按 seq 升序（采集顺序）。画图时再按 x 排。 */
  points: XyPoint[];
  channels: string[];
}

export interface Map2dBuf {
  kind: "map2d";
  geom: Map2dGeometry | null;
  channels: string[];
  /** 每个通道一幅 nx*ny 的图，行优先（idx = iy*nx + ix），没采到的像素是 NaN。 */
  values: Map<string, Float64Array>;
  /** 每个像素最后一次写入的 seq（0 = 没写过）。同一像素写两次时新的赢。 */
  pixelSeq: Float64Array | null;
  filled: number;
  /**
   * 像素写入日志（环形，存像素下标）。同一个流可能同时挂在好几个视图上（TERS 页
   * 主图 + 数据流列表），每个视图各自记读到了第几条 —— 一个「取走即清空」的脏列表
   * 会让先画的那个视图把后画的那个的增量吃掉。
   */
  log: Int32Array;
  /** 累计写入次数（单调增）。视图落后超过日志容量时整幅重画。 */
  logCount: number;
  last: { ix: number; iy: number } | null;
  /** 落在几何范围外、被拒收的帧数。 */
  outOfRange: number;
}

export interface StatusLine {
  seq: number;
  t: number | null;
  text: string;
  level: string;
}

export interface StatusBuf {
  kind: "status";
  lines: StatusLine[];
}

/** 前端不认识的 kind：原样留最近几帧，通用视图把它们显示成 JSON。 */
export interface RawBuf {
  kind: "raw";
  frames: Array<{ seq: number; frame: unknown }>;
}

export type StreamBuf = SpectrumBuf | ScalarBuf | XyBuf | Map2dBuf | StatusBuf | RawBuf;

export interface StreamState {
  meta: StreamMeta;
  /** 已应用的最大 seq；0 = 还没有任何帧。 */
  cursor: number;
  axis: AxisState | null;
  /** 最近几个版本的波长轴（帧里只带 axis_ver，旧帧可能指向上一版）。 */
  axes: AxisState[];
  /** 每次变化 +1。画图组件拿它判断要不要重画。 */
  version: number;
  received: number;
  invalid: number;
  /** 确认补不回来的帧数。 */
  lost: number;
  missing: SeqRanges;
  /** 这个流被清空重来（服务端重启）的次数。 */
  resets: number;
  /** 最近一次收到帧的本地时刻（ms）。订阅时服务端重放的最新一帧也算。 */
  lastRecvMs: number | null;
  /**
   * 最近一次有证据表明它**正在产出**新帧的本地时刻（ms）：收到 seq 比已知的都大的帧（订阅时
   * 的重放、补齐的旧帧不算），或相隔不久的两份目录之间 last_seq 涨了（轮询模式下，没订阅的
   * 流也看得到）。「跟随最新」的新鲜度看它，不看 lastRecvMs。
   */
  lastLiveMs: number | null;
  /** 最新一帧的帧时间（unix 秒）。 */
  lastT: number | null;
  /** 最近一份完整列表里已经没有它了（服务端淘汰）。本地数据还留着可看。 */
  gone: boolean;
  buf: StreamBuf;
}

export interface StoreCaps {
  spectrum: number;
  scalar: number;
  xy: number;
  status: number;
  raw: number;
  /** 列表还没到时，为未知流暂存的帧数上限。 */
  pending: number;
  /** 保留几个旧版本的波长轴。 */
  axes: number;
}

export const DEFAULT_CAPS: StoreCaps = {
  // spectrum 与服务端的保留一致（契约 §7：200 帧）——瀑布图最多就这么高。
  spectrum: 200,
  scalar: 20_000,
  xy: 20_000,
  status: 1_000,
  raw: 50,
  pending: 500,
  axes: 4,
};

export type ApplyResult =
  | "applied"
  /** 应用了，但它前面有缺口 —— 调用方应当去 REST 补。 */
  | "gap"
  | "duplicate"
  | "invalid"
  /** 流的元数据还没到，先暂存。 */
  | "pending";

type Listener = (id: string) => void;

/** map2d 写入日志容量。一个视图在两次重画之间落后超过这么多条，就整幅重画。 */
export const MAP2D_LOG_CAPACITY = 65_536;

/**
 * 视图读 map2d 增量：从日志第 `from` 条读到最新。返回新的读取位置。
 * `full` = 落后太多（或第一次画），调用方整幅重画、忽略 `idx`。
 */
export function map2dChangesSince(
  b: Map2dBuf,
  from: number,
): { full: boolean; idx: number[]; next: number } {
  const next = b.logCount;
  if (from <= 0 || next - from > b.log.length || from > next) {
    return { full: true, idx: [], next };
  }
  const idx: number[] = [];
  for (let k = from; k < next; k++) idx.push(b.log[k % b.log.length]!);
  return { full: false, idx, next };
}

function emptyBuf(meta: StreamMeta): StreamBuf {
  switch (meta.kind) {
    case "spectrum":
      return { kind: "spectrum", entries: [] };
    case "scalar":
      return { kind: "scalar", seqs: [], t: [], channels: [], values: new Map() };
    case "xy":
      return { kind: "xy", points: [], channels: [] };
    case "map2d": {
      const geom = map2dGeometry(meta);
      return {
        kind: "map2d",
        geom,
        channels: [],
        values: new Map(),
        pixelSeq: geom ? new Float64Array(geom.nx * geom.ny) : null,
        filled: 0,
        log: new Int32Array(MAP2D_LOG_CAPACITY),
        logCount: 0,
        last: null,
        outOfRange: 0,
      };
    }
    case "status":
      return { kind: "status", lines: [] };
    default:
      return { kind: "raw", frames: [] };
  }
}

function sameGeometry(a: Map2dGeometry | null, b: Map2dGeometry | null): boolean {
  if (!a || !b) return a === b;
  return a.nx === b.nx && a.ny === b.ny && a.x0 === b.x0 && a.y0 === b.y0
    && a.dx === b.dx && a.dy === b.dy;
}

/** 升序数组里第一个 key(e) >= target 的位置。 */
function lowerBound<T>(arr: readonly T[], target: number, key: (e: T) => number): number {
  let lo = 0;
  let hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (key(arr[mid]!) < target) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/**
 * 同一个流的两次目录读数相隔不超过这么久，其间 last_seq 涨了才记成「正在产出」（lastLiveMs）：
 * 隔得太久，只知道其间来过帧，不知道是什么时候来的。轮询模式下目录 3 s 一份；WS 模式下目录
 * 只在流声明 / 结束时推，大多隔得很久，靠的是收到的帧。
 */
export const LIST_EVIDENCE_GAP_MS = 20_000;

export class LiveStore {
  private streams = new Map<string, StreamState>();
  private pending = new Map<string, Array<{ seq: number; frame: unknown }>>();
  private listeners = new Set<Listener>();
  private listListeners = new Set<() => void>();
  private listVersion = 0;
  /** 每个流的 meta（含 last_seq）最近一次从目录读到的本地时刻。 */
  private metaSeenMs = new Map<string, number>();
  readonly caps: StoreCaps;
  private now: () => number;

  constructor(opts: { caps?: Partial<StoreCaps>; now?: () => number } = {}) {
    this.caps = { ...DEFAULT_CAPS, ...(opts.caps ?? {}) };
    this.now = opts.now ?? (() => Date.now());
  }

  // ── 订阅变化 ──────────────────────────────────────────────────────────────

  /** 某个流的数据变了（id）。在写入的那个调用栈里同步回调 —— 只许排队，别在里面画。 */
  onChange(cb: Listener): () => void {
    this.listeners.add(cb);
    return () => {
      this.listeners.delete(cb);
    };
  }

  /** 流的列表或某个流的元数据变了。 */
  onList(cb: () => void): () => void {
    this.listListeners.add(cb);
    return () => {
      this.listListeners.delete(cb);
    };
  }

  private emit(id: string): void {
    for (const cb of this.listeners) {
      try {
        cb(id);
      } catch {
        /* 一个坏掉的监听者不许挡住别的 */
      }
    }
  }

  private emitList(): void {
    this.listVersion += 1;
    for (const cb of this.listListeners) {
      try {
        cb();
      } catch {
        /* 同上 */
      }
    }
  }

  getListVersion(): number {
    return this.listVersion;
  }

  // ── 读 ────────────────────────────────────────────────────────────────────

  get(id: string): StreamState | undefined {
    return this.streams.get(id);
  }

  ids(): string[] {
    return [...this.streams.keys()];
  }

  states(): StreamState[] {
    return [...this.streams.values()];
  }

  /** 给订阅消息用的 since 游标。 */
  cursors(ids: Iterable<string>): Record<string, number> {
    const out: Record<string, number> = {};
    for (const id of ids) {
      const st = this.streams.get(id);
      if (st && st.cursor > 0) out[id] = st.cursor;
    }
    return out;
  }

  // ── 写：列表 ──────────────────────────────────────────────────────────────

  /**
   * 合入一份流列表。返回被清空重来的流 id（调用方要按冷启动重新订阅它们）。
   *
   * `full`：这是一份完整列表（REST 列表，或新连接上的第一份）——不在里面的流
   * 标成 gone。`trustLastSeq`：列表里的 last_seq 能不能用来判重启。只有在新连接上
   * 的第一份列表、并且这条连接之前的所有写入都已结束时才成立；同一条连接上的后续
   * 列表可能和 REST 补齐交错到达，拿它判重启会误杀（见文件头第 3 条）。
   */
  applyStreams(
    items: StreamMeta[],
    opts: { full: boolean; trustLastSeq: boolean; newGeneration?: boolean },
  ): string[] {
    const reset: string[] = [];
    const seen = new Set<string>();
    const now = this.now();
    for (const item of items) {
      seen.add(item.id);
      const seenBefore = this.metaSeenMs.get(item.id);
      this.metaSeenMs.set(item.id, now);
      const st = this.streams.get(item.id);
      if (!st) {
        this.streams.set(item.id, this.fresh(item));
        this.replayPending(item.id);
        continue;
      }
      // newGeneration：服务端的 hub id 变了（MAST 重启过）—— 手里的游标全部作废，不论 uid
      if (opts.newGeneration || this.restarted(st, item, opts.trustLastSeq)) {
        const resets = st.resets + 1;
        const next = this.fresh(item);
        next.resets = resets;
        next.version = st.version + 1;
        this.streams.set(item.id, next);
        this.replayPending(item.id);
        reset.push(item.id);
        this.emit(item.id);
        continue;
      }
      const wasGone = st.gone;
      // 上一次读到它的目录之后 last_seq 涨了：这段时间里来过新帧（没订阅的流只能这样看出来）
      const recent = seenBefore !== undefined && now - seenBefore <= LIST_EVIDENCE_GAP_MS;
      if (recent && item.last_seq > Math.max(st.cursor, st.meta.last_seq)) st.lastLiveMs = now;
      st.meta = item;
      st.gone = false;
      if (wasGone) st.version += 1;
    }
    if (opts.full) {
      for (const st of this.streams.values()) {
        if (!seen.has(st.meta.id) && !st.gone) {
          st.gone = true;
          st.version += 1;
        }
      }
    }
    this.emitList();
    return reset;
  }

  private restarted(st: StreamState, item: StreamMeta, trustLastSeq: boolean): boolean {
    if (item.kind !== st.meta.kind) return true;
    if (item.epoch !== null && st.meta.epoch !== null && item.epoch !== st.meta.epoch) return true;
    if (item.kind === "map2d" && !sameGeometry(map2dGeometry(item), map2dGeometry(st.meta))) {
      return true;
    }
    // last_seq 倒退只在流**没有 uid** 时才拿来判重启：有 uid 的服务端在同一进程里 seq
    // 永不回退（契约 §7 扩展 3），重启则 uid 一定变 —— 那时以 uid 为准。
    return trustLastSeq && item.epoch === null && item.last_seq >= 0 && item.last_seq < st.cursor;
  }

  private fresh(meta: StreamMeta): StreamState {
    return {
      meta,
      cursor: 0,
      axis: null,
      axes: [],
      version: 1,
      received: 0,
      invalid: 0,
      lost: 0,
      missing: new SeqRanges(),
      resets: 0,
      lastRecvMs: null,
      lastLiveMs: null,
      lastT: null,
      gone: false,
      buf: emptyBuf(meta),
    };
  }

  private replayPending(id: string): void {
    const queued = this.pending.get(id);
    if (!queued) return;
    this.pending.delete(id);
    for (const q of queued.sort((a, b) => a.seq - b.seq)) this.applyFrame(id, q.seq, q.frame);
  }

  // ── 写：轴 ────────────────────────────────────────────────────────────────

  applyAxis(id: string, ver: number, x: Float64Array, unit: string): void {
    const st = this.streams.get(id);
    if (!st) return;
    if (st.axes.some((a) => a.ver === ver && a.x.length === x.length)) {
      // 同一版本重发（重连时服务端会再推一遍当前轴）——只在它是最新版时更新指针
      if (!st.axis || st.axis.ver <= ver) st.axis = st.axes.find((a) => a.ver === ver) ?? st.axis;
      return;
    }
    const axis: AxisState = { ver, x, unit };
    st.axes = [...st.axes.filter((a) => a.ver !== ver), axis]
      .sort((a, b) => a.ver - b.ver)
      .slice(-this.caps.axes);
    if (!st.axis || ver >= st.axis.ver) st.axis = axis;
    st.version += 1;
    this.emit(id);
  }

  // ── 写：帧 ────────────────────────────────────────────────────────────────

  applyFrame(id: string, seq: number, frame: unknown): ApplyResult {
    const st = this.streams.get(id);
    if (!st) {
      const q = this.pending.get(id) ?? [];
      if (q.length < this.caps.pending) q.push({ seq, frame });
      this.pending.set(id, q);
      return "pending";
    }
    const incremental = isIncrementalKind(st.meta.kind);
    const old = seq <= st.cursor;
    // 比游标和目录里的 last_seq 都新：是现在才产出的帧，不是重放
    const live = seq > Math.max(st.cursor, st.meta.last_seq);
    // 旧帧只有两种情况收：增量流正在补的缺口；只看最新的流把历史帧插回原位。
    if (old && (incremental ? !st.missing.has(seq) : this.hasSeq(st, seq))) return "duplicate";

    let gap = false;
    // 冷启动（游标 0）的第一帧前面有什么，我们不知道也补不回来 —— 不记缺。
    if (!old && incremental && st.cursor > 0 && seq > st.cursor + 1) {
      st.missing.add(st.cursor + 1, seq - 1);
      gap = true;
    }
    // 不论新旧都要划掉：dropped 记下的缺口可能在游标之后，经 REST 补到时 seq > 游标。
    if (incremental) st.missing.remove(seq);
    // 解不出来的帧也推进游标：它确实存在过，只是没法画；不推进的话补齐逻辑会把它
    // 当成「缺」一遍一遍地去要。
    if (!old) st.cursor = seq;
    const ok = this.write(st, seq, frame);
    st.version += 1;
    if (!ok) {
      st.invalid += 1;
      this.emit(id);
      return "invalid";
    }
    st.received += 1;
    st.lastRecvMs = this.now();
    if (live) st.lastLiveMs = st.lastRecvMs;
    this.emit(id);
    return gap ? "gap" : "applied";
  }

  /**
   * 服务端说 [from, to] 没送到。增量流把其中游标之后的部分记成缺，返回「现在有
   * 缺口要补」；只看最新的流忽略（跳号本来就是常态）。
   *
   * 游标推进到 to：游标的意思是「已知存在的最大 seq」（收到的或被宣布丢了的）。
   * 不推进的话，REST 补回来的第一帧会被当成「游标之后又跳了号」，把刚判为补不回来
   * 的那一段重新记成缺 —— lost 就被数两遍。缺口由 missing 记着，重连不影响它。
   */
  applyDropped(id: string, from: number, to: number): boolean {
    const st = this.streams.get(id);
    if (!st || !isIncrementalKind(st.meta.kind)) return false;
    if (to > st.cursor) {
      st.missing.add(Math.max(from, st.cursor + 1, 1), to);
      st.cursor = to;
      st.version += 1;
      this.emit(id);
    }
    return !st.missing.isEmpty();
  }

  /** 这一段确认补不回来了（服务端已经不保留）。 */
  markLost(id: string, from: number, to: number): void {
    const st = this.streams.get(id);
    if (!st || to < from) return;
    let n = 0;
    for (const [a, b] of st.missing.list()) {
      const lo = Math.max(a, from);
      const hi = Math.min(b, to);
      if (hi >= lo) n += hi - lo + 1;
    }
    if (!n) return;
    st.missing.removeRange(from, to);
    st.lost += n;
    st.version += 1;
    this.emit(id);
  }

  // ── 各 kind 的写入 ────────────────────────────────────────────────────────

  private hasSeq(st: StreamState, seq: number): boolean {
    const b = st.buf;
    if (b.kind === "spectrum") {
      const i = lowerBound(b.entries, seq, (e) => e.seq);
      return i < b.entries.length && b.entries[i]!.seq === seq;
    }
    if (b.kind === "scalar") {
      const i = lowerBound(b.seqs, seq, (s) => s);
      return i < b.seqs.length && b.seqs[i] === seq;
    }
    if (b.kind === "raw") return b.frames.some((f) => f.seq === seq);
    return false;
  }

  /** 把一帧写进缓冲。解不出来返回 false。 */
  private write(st: StreamState, seq: number, frame: unknown): boolean {
    const b = st.buf;
    switch (b.kind) {
      case "spectrum": {
        const f = decodeSpectrumFrame(frame);
        if (!f) return false;
        const e: SpectrumEntry = { seq, t: epochSeconds(f.t), y: f.y, axisVer: f.axis_ver, meta: f.meta };
        const i = lowerBound(b.entries, seq, (x) => x.seq);
        b.entries.splice(i, 0, e);
        if (b.entries.length > this.caps.spectrum) b.entries.splice(0, b.entries.length - this.caps.spectrum);
        this.noteT(st, e.t, seq);
        return true;
      }
      case "scalar": {
        const f = decodeScalarFrame(frame);
        if (!f) return false;
        const i = lowerBound(b.seqs, seq, (s) => s);
        for (const name of Object.keys(f.values)) {
          if (!b.values.has(name)) {
            b.channels.push(name);
            b.values.set(name, new Array<number | null>(b.seqs.length).fill(null));
          }
        }
        const t = epochSeconds(f.t);
        b.seqs.splice(i, 0, seq);
        b.t.splice(i, 0, t);
        for (const name of b.channels) {
          const v = f.values[name];
          b.values.get(name)!.splice(i, 0, v === undefined ? null : v);
        }
        const over = b.seqs.length - this.caps.scalar;
        if (over > 0) {
          // 一次多砍 10%，免得满了之后每一帧都 splice 整个数组
          const cut = over + Math.floor(this.caps.scalar / 10);
          b.seqs.splice(0, cut);
          b.t.splice(0, cut);
          for (const arr of b.values.values()) arr.splice(0, cut);
        }
        this.noteT(st, t, seq);
        return true;
      }
      case "xy": {
        const f = decodeXyFrame(frame);
        if (!f) return false;
        for (const name of Object.keys(f.values)) if (!b.channels.includes(name)) b.channels.push(name);
        const p: XyPoint = { seq, t: epochSeconds(f.t), x: f.x, values: f.values };
        const i = lowerBound(b.points, seq, (q) => q.seq);
        b.points.splice(i, 0, p);
        const over = b.points.length - this.caps.xy;
        if (over > 0) b.points.splice(0, over + Math.floor(this.caps.xy / 10));
        this.noteT(st, p.t, seq);
        return true;
      }
      case "map2d": {
        const f = decodeMap2dFrame(frame);
        if (!f) return false;
        const g = b.geom;
        if (!g || !b.pixelSeq) return false;
        if (f.ix < 0 || f.iy < 0 || f.ix >= g.nx || f.iy >= g.ny) {
          b.outOfRange += 1;
          return false;
        }
        const idx = f.iy * g.nx + f.ix;
        if (b.pixelSeq[idx]! > seq) return true; // 这个像素已经有更新的值：旧帧到晚了
        const first = b.pixelSeq[idx] === 0;
        for (const [name, v] of Object.entries(f.values)) {
          let img = b.values.get(name);
          if (!img) {
            img = new Float64Array(g.nx * g.ny).fill(Number.NaN);
            b.values.set(name, img);
            b.channels.push(name);
          }
          img[idx] = v === null ? Number.NaN : v;
        }
        b.pixelSeq[idx] = seq;
        if (first) b.filled += 1;
        b.log[b.logCount % b.log.length] = idx;
        b.logCount += 1;
        if (seq >= st.cursor) b.last = { ix: f.ix, iy: f.iy };
        this.noteT(st, epochSeconds(f.t), seq);
        return true;
      }
      case "status": {
        const f = decodeStatusFrame(frame);
        if (!f) return false;
        const line: StatusLine = { seq, t: epochSeconds(f.t), text: f.text, level: f.level };
        const i = lowerBound(b.lines, seq, (l) => l.seq);
        b.lines.splice(i, 0, line);
        if (b.lines.length > this.caps.status) b.lines.splice(0, b.lines.length - this.caps.status);
        this.noteT(st, line.t, seq);
        return true;
      }
      case "raw": {
        const i = lowerBound(b.frames, seq, (f) => f.seq);
        b.frames.splice(i, 0, { seq, frame });
        if (b.frames.length > this.caps.raw) b.frames.splice(0, b.frames.length - this.caps.raw);
        return true;
      }
    }
  }

  private noteT(st: StreamState, t: number | null, seq: number): void {
    if (t !== null && seq >= st.cursor) st.lastT = t;
  }

  /** 测试与「清空重来」按钮用：丢掉一个流的本地数据（不影响服务端）。 */
  clearLocal(id: string): void {
    const st = this.streams.get(id);
    if (!st) return;
    const next = this.fresh(st.meta);
    next.version = st.version + 1;
    next.resets = st.resets;
    this.streams.set(id, next);
    this.emit(id);
  }
}
