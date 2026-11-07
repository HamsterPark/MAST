// ════════════════════════════════════════════════════════════════════════════
// liveClient.ts —— 浏览器到 LiveHub 的那一条连接（/ws/live），外加 REST 兜底。
//
// 契约 §3 决策 5：新增的数据采集全部经 LiveHub 推到 MAST 页面上实时绘图。这个文件
// 负责「数据到得了浏览器」这一半；画图在 components/live/，缓存在 lib/live/store.ts。
//
// 和 lib/ws.ts（/ws/events）同一个立场：**一个悄悄死掉的传输比从没连上还糟**，因为
// 屏幕上的曲线会继续画着已经不再真实的东西。所以：
//   · 断线 → 指数退避（全抖动）重连，重连时按每个流的 since 游标续传；
//   · 服务端说 dropped → 增量流（xy / map2d / status）用 REST 补齐，补不回来的
//     记成 lost，界面上说出来；
//   · WS 连不上超过 2 s（或浏览器没有 WebSocket）→ 退回 REST 轮询，WS 照样在后台重试；
//   · 后台标签页：不画（requestAnimationFrame 本来就停），轮询降到 1/5；后台超过 60 s
//     重连成 `?max_hz=1`（每个流每秒最多推一次），回到前台恢复全速，并用 REST 把这段
//     时间里被合并掉的光谱帧补进瀑布图。
//
// ── 协议（契约 §7；服务端实现 MASTv2/mast/api/routes/live.py） ─────────────
//   WS /ws/live[?max_hz=]
//     → {"op":"subscribe","streams":[id…],"since":{id: seq}}   {"op":"unsubscribe",…}
//     ← streams / axis / frame / frames / ping / dropped / error（解析见 lib/live/protocol.ts）
//   GET /api/live/streams                         → {hub, items:[meta + last_seq + uid]}
//   GET /api/live/streams/{id}?since=&limit=      → {hub, meta, axis, axes, frames, more, dropped}
//     since：-1 = 只要最新一帧；n ≥ 0 = seq > n 的帧（0 = 保留区里全部）
//   认证与其余 API 相同：同源，浏览器自带 cookie / Basic 头，这里不额外带任何凭据。
//
// ── 分层 ───────────────────────────────────────────────────────────────────
// 没有 React。决策函数（何时轮询、URL）单独导出，可以不起浏览器直接测；连接本身通过
// configureLiveEnv 注入假的 WebSocket / fetch 来测（test/liveClient.test.ts）。
// React 绑定在 hooks/useLive.ts。
// ════════════════════════════════════════════════════════════════════════════

import { backoffDelayMs } from "./ws.ts";
import { tr } from "./i18n.ts";
import { LiveStore } from "./live/store.ts";
import {
  parseLiveMessage,
  parseRestFrames,
  parseRestStreamList,
  streamPath,
  subscribeOp,
  unsubscribeOp,
  type RestFrames,
  type StreamMeta,
} from "./live/protocol.ts";

// ── 状态 ───────────────────────────────────────────────────────────────────

export type LiveStatus =
  /** 没有任何视图在看 —— 什么都不开，按设计如此。 */
  | "idle"
  | "connecting"
  /** WS 活着。只有这个状态下不轮询。 */
  | "open"
  | "reconnecting"
  /** 连续失败够多次，推送基本可以认为不可用（仍在重试，也在轮询）。 */
  | "failed"
  /** 浏览器没有 WebSocket：只能轮询。 */
  | "unsupported";

export interface LiveConnState {
  status: LiveStatus;
  /** REST 轮询兜底正在跑。 */
  polling: boolean;
  /** 后台降频中（max_hz=1）。 */
  slow: boolean;
  /** 最近一次收到任何东西（WS 消息或 REST 响应）的本地时刻。 */
  lastTrafficMs: number | null;
  /** 给人看的最近一条传输层问题；恢复后清空。 */
  problem: string | null;
}

// ── 可调参数 ───────────────────────────────────────────────────────────────

export const LIVE_BACKOFF_BASE_MS = 500;
/** 重连间隔上限。轮询兜底在跑，所以这里不必像 /ws/events 那样等满 30 s。 */
export const LIVE_BACKOFF_MAX_MS = 15_000;
/** 连续失败几次后状态升级为 failed（只是给界面的提示，重试不停）。 */
export const LIVE_FAIL_AFTER_ATTEMPTS = 4;
/** 多久一点消息都没有就认为 socket 半死。服务端 15 s 没别的消息就发 ping，这里给 3 倍。 */
export const LIVE_IDLE_MS = 45_000;
/** WS 不在 open 状态多久之后开始 REST 轮询。短暂的重连抖动不值得发一轮请求。 */
export const LIVE_POLL_START_AFTER_MS = 2_000;
/** 轮询间隔：帧 / 列表。后台标签页乘以 LIVE_HIDDEN_SLOWDOWN。 */
export const LIVE_POLL_FRAMES_MS = 1_000;
export const LIVE_POLL_LIST_MS = 3_000;
export const LIVE_HIDDEN_SLOWDOWN = 5;
/** 新连接上等第一份流列表多久（拿它判重启之后再发订阅）。服务端连上就先发目录。 */
export const LIVE_STREAMS_WAIT_MS = 1_500;
/** REST 补齐：每页帧数（服务端上限 5000）/ 一轮最多几页 / 失败后多久再试。 */
export const LIVE_BACKFILL_PAGE = 500;
export const LIVE_BACKFILL_MAX_PAGES = 20;
export const LIVE_BACKFILL_RETRY_MS = 3_000;
/** 后台多久之后降频（重连成 max_hz=1）。 */
export const LIVE_HIDDEN_SLOW_AFTER_MS = 60_000;
export const LIVE_SLOW_MAX_HZ = 1;
/** 光谱的历史一次最多要多少帧（= 服务端默认保留量）。 */
export const LIVE_HISTORY_MAX = 200;
/**
 * 最后一个视图走了之后，连接再留多久才关。切页面（光谱页 → 光学台页）时两边的视图
 * 前后脚卸载/挂载，立刻关会白白断一次、重放一次；React 开发模式下 StrictMode 的
 * 挂载—卸载—再挂载也会在握手中途把 socket 掐掉。
 */
export const LIVE_LINGER_MS = 3_000;

// ── 纯决策函数 ─────────────────────────────────────────────────────────────

/** 连接地址。同源：生产里 SPA 由同一个 FastAPI 进程提供，开发时 Vite 代理 /ws。 */
export function liveUrl(loc: { protocol: string; host: string }, maxHz?: number | null): string {
  const proto = loc.protocol === "https:" ? "wss" : "ws";
  const base = `${proto}://${loc.host}/ws/live`;
  return maxHz ? `${base}?max_hz=${maxHz}` : base;
}

/** REST 帧地址。since 总是显式给：缺省时服务端对 spectrum 只回最新一帧。 */
export function framesUrl(id: string, since: number, limit: number): string {
  const q = new URLSearchParams();
  q.set("since", String(Math.max(-1, Math.floor(since))));
  q.set("limit", String(Math.max(1, Math.floor(limit))));
  return `/api/live/streams/${streamPath(id)}?${q.toString()}`;
}

/**
 * 该不该跑 REST 轮询。只有 open 免；刚掉线的头 2 s 也免（多半马上重连上）；
 * 没有 WebSocket 的浏览器、连续失败之后立刻轮询。
 */
export function shouldPollLive(status: LiveStatus, msSinceNotOpen: number): boolean {
  if (status === "open" || status === "idle") return false;
  if (status === "unsupported" || status === "failed") return true;
  return msSinceNotOpen >= LIVE_POLL_START_AFTER_MS;
}

/** 重的流：回到前台时要补历史（光谱一帧一千多个点、20 Hz，后台降频时被合并掉的最多）。 */
export function isHeavyKind(kind: string): boolean {
  return kind === "spectrum";
}

// ── 可注入的环境（测试用） ─────────────────────────────────────────────────

interface SocketLike {
  readyState: number;
  onopen: ((ev?: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onerror: ((ev?: unknown) => void) | null;
  onclose: ((ev?: { code?: number }) => void) | null;
  send(data: string): void;
  close(): void;
}

type SocketCtor = new (url: string) => SocketLike;

interface FetchResponseLike {
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
}

type FetchLike = (url: string, init?: { credentials?: "same-origin" }) => Promise<FetchResponseLike>;

export interface LiveEnv {
  WebSocketImpl: SocketCtor | null;
  fetchImpl: FetchLike | null;
  location: { protocol: string; host: string } | null;
  isHidden: () => boolean;
  now: () => number;
}

function defaultEnv(): LiveEnv {
  const g = globalThis as unknown as {
    WebSocket?: SocketCtor;
    fetch?: FetchLike;
    location?: { protocol: string; host: string };
    document?: { visibilityState?: string };
  };
  return {
    WebSocketImpl: typeof g.WebSocket === "function" ? g.WebSocket : null,
    fetchImpl: typeof g.fetch === "function" ? g.fetch.bind(globalThis) : null,
    location: g.location ?? null,
    isHidden: () => g.document?.visibilityState === "hidden",
    now: () => Date.now(),
  };
}

let envOverride: Partial<LiveEnv> = {};

function env(): LiveEnv {
  return { ...defaultEnv(), ...envOverride };
}

/** 测试注入。传 {} 恢复默认。 */
export function configureLiveEnv(over: Partial<LiveEnv>): void {
  envOverride = { ...over };
}

// ════════════════════════════════════════════════════════════════════════════
// 单例连接
// ════════════════════════════════════════════════════════════════════════════

let store = new LiveStore();

export function getLiveStore(): LiveStore {
  return store;
}

let socket: SocketLike | null = null;
let attempt = 0;
let closingDeliberately = false;
/** 每开一条新连接 +1；旧连接迟到的回调按它丢弃。 */
let connGen = 0;
let retryTimer: ReturnType<typeof setTimeout> | undefined;
let idleTimer: ReturnType<typeof setTimeout> | undefined;
let streamsWaitTimer: ReturnType<typeof setTimeout> | undefined;
let pollStartTimer: ReturnType<typeof setTimeout> | undefined;
let pollTimer: ReturnType<typeof setTimeout> | undefined;
let hiddenTimer: ReturnType<typeof setTimeout> | undefined;
let lingerTimer: ReturnType<typeof setTimeout> | undefined;
/** 新连接上还没收到第一份流列表。 */
let awaitingStreams = false;
/** 当前连接上服务端已经知道我们要的流。 */
let subscribed = new Set<string>();
let syncQueued = false;
let notOpenSince: number | null = null;
let polling = false;
/** 每开一轮轮询 +1；旧一轮迟到的响应按它丢弃。 */
let pollGen = 0;
let lastListPoll = 0;
/** 后台降频中：连接带 max_hz=1。 */
let slowMode = false;
/** 降频开始时每个重流的游标：回到前台从这里往后补。 */
const slowCursor = new Map<string, number>();
/** 最近一次看到的服务端 LiveHub id（变了 = MAST 重启过）。 */
let lastHub: string | null = null;

const streamWatchers = new Map<string, number>();
let listWatchers = 0;
const backfilling = new Set<string>();
const backfillRetry = new Map<string, ReturnType<typeof setTimeout>>();
const historyDone = new Set<string>();

let conn: LiveConnState = { status: "idle", polling: false, slow: false, lastTrafficMs: null, problem: null };
const statusListeners = new Set<() => void>();

function setConn(patch: Partial<LiveConnState>): void {
  const next = { ...conn, ...patch };
  if (
    next.status === conn.status && next.polling === conn.polling && next.slow === conn.slow
    && next.problem === conn.problem && next.lastTrafficMs === conn.lastTrafficMs
  ) {
    return;
  }
  conn = next;
  for (const cb of statusListeners) {
    try {
      cb();
    } catch {
      /* 一个坏掉的监听者不许挡住别的 */
    }
  }
}

function setStatus(status: LiveStatus): void {
  if (status === "open" || status === "idle") notOpenSince = null;
  else if (notOpenSince === null) notOpenSince = env().now();
  setConn({ status });
  reconsiderPolling();
}

/** 流量戳只用来显示「最后收到」，按秒取整，免得每帧都通知一遍 React。 */
function noteTraffic(): void {
  const t = env().now();
  if (conn.lastTrafficMs === null || t - conn.lastTrafficMs >= 1000) setConn({ lastTrafficMs: t });
}

function totalWatchers(): number {
  let n = listWatchers;
  for (const c of streamWatchers.values()) n += c;
  return n;
}

function wantedStreams(): Set<string> {
  const out = new Set<string>();
  for (const [id, n] of streamWatchers) if (n > 0) out.add(id);
  return out;
}

function clearTimer(t: ReturnType<typeof setTimeout> | undefined): undefined {
  if (t !== undefined) clearTimeout(t);
  return undefined;
}

// ── 连接 ───────────────────────────────────────────────────────────────────

function armIdle(): void {
  idleTimer = clearTimer(idleTimer);
  idleTimer = setTimeout(() => forceReconnect(), LIVE_IDLE_MS);
}

function send(obj: unknown): boolean {
  const ws = socket;
  if (!ws || ws.readyState !== 1) return false;
  try {
    ws.send(JSON.stringify(obj));
    return true;
  } catch {
    return false;
  }
}

function open(): void {
  const e = env();
  if (!e.WebSocketImpl || !e.location) {
    setStatus("unsupported");
    return;
  }
  if (socket && (socket.readyState === 0 || socket.readyState === 1)) return;
  setStatus(attempt === 0 ? "connecting" : attempt >= LIVE_FAIL_AFTER_ATTEMPTS ? "failed" : "reconnecting");
  let ws: SocketLike;
  try {
    ws = new e.WebSocketImpl(liveUrl(e.location, slowMode ? LIVE_SLOW_MAX_HZ : null));
  } catch {
    scheduleReconnect();
    return;
  }
  socket = ws;
  const gen = ++connGen;
  subscribed = new Set();
  closingDeliberately = false;

  ws.onopen = () => {
    if (gen !== connGen) return;
    attempt = 0;
    awaitingStreams = true;
    streamsWaitTimer = clearTimer(streamsWaitTimer);
    // 服务端连上就先推一份目录；拿到它（判完重启）再订阅。万一它不推，等一会儿也照样
    // 订阅 —— 不能让一个缺失的目录把整条推送卡死。
    streamsWaitTimer = setTimeout(() => {
      if (gen !== connGen || !awaitingStreams) return;
      awaitingStreams = false;
      queueSync();
    }, LIVE_STREAMS_WAIT_MS);
    setConn({ problem: null });
    setStatus("open");
    armIdle();
  };

  ws.onmessage = (ev) => {
    if (gen !== connGen) return;
    armIdle();
    noteTraffic();
    if (typeof ev.data !== "string") return;
    handleMessage(ev.data);
  };

  ws.onerror = () => {
    /* 浏览器在这里不给细节；onclose 总会跟着来，重连从那里走 */
  };

  ws.onclose = (ev) => {
    if (gen !== connGen) return;
    socket = null;
    idleTimer = clearTimer(idleTimer);
    streamsWaitTimer = clearTimer(streamsWaitTimer);
    awaitingStreams = false;
    subscribed = new Set();
    if (closingDeliberately) {
      closingDeliberately = false;
      return;
    }
    const code = ev?.code;
    if (code === 4401 || code === 4403 || code === 1008) {
      setConn({ problem: tr("实时数据通道认证失败（会继续重试）", "Live data channel authentication failed (will keep retrying)") });
    }
    scheduleReconnect();
  };
}

function scheduleReconnect(): void {
  if (totalWatchers() === 0) {
    setStatus("idle");
    return;
  }
  attempt += 1;
  setStatus(attempt >= LIVE_FAIL_AFTER_ATTEMPTS ? "failed" : "reconnecting");
  if (attempt >= LIVE_FAIL_AFTER_ATTEMPTS && !conn.problem) {
    setConn({ problem: tr("实时推送连不上，已改用轮询（仍在重试）", "Live push cannot connect; falling back to polling (still retrying)") });
  }
  retryTimer = clearTimer(retryTimer);
  retryTimer = setTimeout(() => {
    retryTimer = undefined;
    open();
  }, backoffDelayMs(attempt - 1, { base: LIVE_BACKOFF_BASE_MS, cap: LIVE_BACKOFF_MAX_MS }));
}

function closeSocketOnly(): void {
  const ws = socket;
  socket = null;
  connGen += 1;
  if (ws) {
    closingDeliberately = true;
    try {
      ws.close();
    } catch {
      /* 已经在关 */
    }
    closingDeliberately = false;
  }
  idleTimer = clearTimer(idleTimer);
  streamsWaitTimer = clearTimer(streamsWaitTimer);
  awaitingStreams = false;
  subscribed = new Set();
}

function forceReconnect(): void {
  closeSocketOnly();
  retryTimer = clearTimer(retryTimer);
  if (totalWatchers() === 0) {
    setStatus("idle");
    return;
  }
  // 唤醒（合盖再开、网络回来了、换速率）应当立刻重连，而不是等完攒下来的退避
  attempt = 0;
  open();
}

function ensureRunning(): void {
  if (totalWatchers() === 0) {
    // 没人看了：先退订（立刻），连接本身留一会儿再关（LIVE_LINGER_MS）
    queueSync();
    if (lingerTimer === undefined && conn.status !== "idle") {
      lingerTimer = setTimeout(() => {
        lingerTimer = undefined;
        if (totalWatchers() === 0) shutdown();
      }, LIVE_LINGER_MS);
    }
    return;
  }
  lingerTimer = clearTimer(lingerTimer);
  if (!socket && retryTimer === undefined) {
    attempt = 0;
    open();
  }
  queueSync();
}

function shutdown(): void {
  closeSocketOnly();
  retryTimer = clearTimer(retryTimer);
  hiddenTimer = clearTimer(hiddenTimer);
  lingerTimer = clearTimer(lingerTimer);
  stopPolling();
  attempt = 0;
  if (slowMode) {
    slowMode = false;
    slowCursor.clear();
    setConn({ slow: false });
  }
  setStatus("idle");
}

// ── 消息 ───────────────────────────────────────────────────────────────────

function applyFrames(id: string, items: Array<{ seq: number; frame: unknown }>): void {
  for (const it of items) store.applyFrame(id, it.seq, it.frame);
  const st = store.get(id);
  if (st && !st.missing.isEmpty()) scheduleBackfill(id);
}

function handleMessage(raw: string): void {
  const msg = parseLiveMessage(raw);
  switch (msg.kind) {
    case "streams": {
      const first = awaitingStreams;
      if (first) {
        awaitingStreams = false;
        streamsWaitTimer = clearTimer(streamsWaitTimer);
      }
      // hub id 变了 = MAST 重启过：手里所有流的游标作废（契约 §7 扩展 2）
      const newGeneration = noteHub(msg.hub);
      // 服务端每次发的都是完整目录。平时判重启靠 hub id 与每个流的 uid；没有 uid 的流
      // 才退回看新连接上第一份目录里的 last_seq（它在因果上晚于我们之前收到的一切：旧连接
      // 已关、旧轮询的迟到响应按 pollGen 丢弃）。
      const reset = store.applyStreams(msg.items, {
        full: true,
        trustLastSeq: first && backfilling.size === 0,
        newGeneration,
      });
      for (const id of reset) historyDone.delete(id);
      resubscribeFresh(reset);
      queueSync();
      return;
    }
    case "axis":
      store.applyAxis(msg.stream, msg.ver, msg.x, msg.unit);
      return;
    case "frame":
      applyFrames(msg.stream, [{ seq: msg.seq, frame: msg.frame }]);
      return;
    case "frames":
      applyFrames(msg.stream, msg.items);
      return;
    case "dropped":
      if (store.applyDropped(msg.stream, msg.from, msg.to)) scheduleBackfill(msg.stream);
      return;
    case "error":
      // 这是我们自己发错了消息 —— 一个前端缺陷，不是网络问题。说出来，连接照常。
      setConn({
        problem: tr(
          `实时通道拒收了一条${msg.op ? ` ${msg.op} ` : ""}消息：${msg.message}`,
          `The live channel rejected a${msg.op ? ` ${msg.op} ` : " "}message: ${msg.message}`,
        ),
      });
      // eslint-disable-next-line no-console
      console.warn("[live] server rejected op", msg);
      return;
    case "ping":
    case "ignored":
      return;
  }
}

/** 记下服务端的 hub id；返回「它变了」（第一次见到不算变）。 */
function noteHub(hub: string | null): boolean {
  if (!hub) return false;
  const changed = lastHub !== null && hub !== lastHub;
  lastHub = hub;
  return changed;
}

/** 被清空重来的流：按冷启动重新订阅（不带 since），否则服务端会从旧游标往后找。 */
function resubscribeFresh(ids: string[]): void {
  const again = ids.filter((id) => subscribed.has(id));
  if (!again.length) return;
  send(unsubscribeOp(again));
  for (const id of again) subscribed.delete(id);
}

function queueSync(): void {
  if (syncQueued) return;
  syncQueued = true;
  // 一次渲染里会有好几个视图同时 watch/unwatch —— 攒成一条订阅消息再发。
  void Promise.resolve().then(() => {
    syncQueued = false;
    syncSubscriptions();
  });
}

function syncSubscriptions(): void {
  if (!socket || socket.readyState !== 1 || awaitingStreams) return;
  const wanted = wantedStreams();
  const add = [...wanted].filter((id) => !subscribed.has(id));
  const drop = [...subscribed].filter((id) => !wanted.has(id));
  if (drop.length && send(unsubscribeOp(drop))) for (const id of drop) subscribed.delete(id);
  if (add.length && send(subscribeOp(add, store.cursors(add)))) for (const id of add) subscribed.add(id);
}

// ── REST ───────────────────────────────────────────────────────────────────

type Fetched<T> = { ok: true; value: T } | { ok: false; status: number };

async function getJson(url: string): Promise<Fetched<unknown>> {
  const f = env().fetchImpl;
  if (!f) return { ok: false, status: 0 };
  try {
    const r = await f(url, { credentials: "same-origin" });
    if (!r.ok) return { ok: false, status: r.status };
    const body = await r.json();
    noteTraffic();
    return { ok: true, value: body };
  } catch {
    return { ok: false, status: 0 };
  }
}

async function fetchFrames(id: string, since: number, limit: number): Promise<Fetched<RestFrames>> {
  const r = await getJson(framesUrl(id, since, limit));
  return r.ok ? { ok: true, value: parseRestFrames(r.value) } : r;
}

async function fetchStreams(): Promise<{ hub: string | null; items: StreamMeta[] } | null> {
  const r = await getJson("/api/live/streams");
  if (!r.ok) {
    if (r.status === 404) setConn({ problem: tr("后端还没有 LiveHub 接口（/api/live 返回 404）", "The backend has no LiveHub endpoint yet (/api/live returned 404)") });
    return null;
  }
  return parseRestStreamList(r.value);
}

function applyRest(id: string, res: RestFrames): void {
  if (res.meta && !store.get(id)) store.applyStreams([res.meta], { full: false, trustLastSeq: false });
  // 旧版本的轴先进（帧可能指向它们），当前轴最后进 —— 它是最新版
  for (const ax of res.axes) store.applyAxis(id, ax.ver, ax.x, ax.unit);
  if (res.axis) store.applyAxis(id, res.axis.ver, res.axis.x, res.axis.unit);
  if (res.dropped) store.markLost(id, res.dropped.from, res.dropped.to);
  for (const f of res.frames) store.applyFrame(id, f.seq, f.frame);
}

// ── 补齐（增量流的缺口） ───────────────────────────────────────────────────

function scheduleBackfill(id: string): void {
  if (backfilling.has(id) || backfillRetry.has(id)) return;
  backfilling.add(id);
  void runBackfill(id).finally(() => backfilling.delete(id));
}

async function runBackfill(id: string): Promise<void> {
  for (let page = 0; page < LIVE_BACKFILL_MAX_PAGES; page++) {
    const st = store.get(id);
    if (!st) return;
    const first = st.missing.first();
    if (first === null) return;
    const r = await fetchFrames(id, first - 1, LIVE_BACKFILL_PAGE);
    if (!r.ok) {
      if (r.status === 404) {
        // 服务端已经没有这个流了：缺的这些永远补不回来
        for (const [a, b] of st.missing.list()) store.markLost(id, a, b);
        return;
      }
      // 网络/后端问题：过一会儿再来，不在这里打转。
      const t = setTimeout(() => {
        backfillRetry.delete(id);
        scheduleBackfill(id);
      }, LIVE_BACKFILL_RETRY_MS);
      backfillRetry.set(id, t);
      return;
    }
    const res = r.value;
    applyRest(id, res);
    if (!res.more) {
      // 服务端从 first 往后的全给了：这一段里还缺的，就是它也没有的。
      const last = res.frames.length ? res.frames[res.frames.length - 1]!.seq : st.cursor;
      store.markLost(id, first, Math.max(first, last));
      return;
    }
  }
}

/**
 * 只看最新的流（光谱）要历史：把服务端保留区里 `since` 之后的帧（最多 count 帧）取回来
 * 插进缓存。瀑布图第一次打开、后台降频结束回到前台时用。`since` 不给 = 最近 count 帧；
 * 每个流每次重置只自动取一次，`force` 绕过这个限制。
 */
export async function requestHistory(id: string, count: number, opts: { since?: number; force?: boolean } = {}): Promise<void> {
  if (!opts.force && historyDone.has(id)) return;
  historyDone.add(id);
  const st = store.get(id);
  const n = Math.max(1, Math.min(LIVE_HISTORY_MAX, Math.floor(count)));
  const last = st?.meta.last_seq ?? -1;
  const since = opts.since ?? (last > 0 ? Math.max(0, last - n) : 0);
  const r = await fetchFrames(id, since, n);
  if (r.ok) applyRest(id, r.value);
  else historyDone.delete(id);
}

// ── 轮询兜底 ───────────────────────────────────────────────────────────────

function reconsiderPolling(): void {
  const status = conn.status;
  if (status === "open" || status === "idle") {
    pollStartTimer = clearTimer(pollStartTimer);
    stopPolling();
    return;
  }
  const since = notOpenSince === null ? 0 : env().now() - notOpenSince;
  if (shouldPollLive(status, since)) {
    startPolling();
    return;
  }
  if (pollStartTimer === undefined && !polling) {
    pollStartTimer = setTimeout(() => {
      pollStartTimer = undefined;
      reconsiderPolling();
    }, Math.max(0, LIVE_POLL_START_AFTER_MS - since));
  }
}

function startPolling(): void {
  if (polling || totalWatchers() === 0) return;
  polling = true;
  pollGen += 1;
  lastListPoll = 0;
  setConn({ polling: true });
  void pollTick(pollGen);
}

function stopPolling(): void {
  if (!polling) return;
  polling = false;
  pollGen += 1;
  pollTimer = clearTimer(pollTimer);
  setConn({ polling: false });
}

async function pollTick(gen: number): Promise<void> {
  if (!polling || gen !== pollGen) return;
  const e = env();
  const slow = e.isHidden() ? LIVE_HIDDEN_SLOWDOWN : 1;
  if (e.now() - lastListPoll >= LIVE_POLL_LIST_MS * slow) {
    lastListPoll = e.now();
    const list = await fetchStreams();
    if (gen !== pollGen) return;
    if (list) {
      const reset = store.applyStreams(list.items, {
        full: true,
        trustLastSeq: backfilling.size === 0,
        newGeneration: noteHub(list.hub),
      });
      for (const id of reset) historyDone.delete(id);
    }
  }
  for (const id of wantedStreams()) {
    if (gen !== pollGen) return;
    const st = store.get(id);
    if (!st) continue;
    if (st.meta.kind === "spectrum") {
      // 光谱只要最新一帧（WS 上也是这样推的）；一秒追一千多个点的历史不值得
      const r = await fetchFrames(id, -1, 1);
      if (gen !== pollGen) return;
      if (r.ok) applyRest(id, r.value);
      continue;
    }
    // 其余：从游标往后一页一页追到最新（有上限）。轮询时标量也拿全，不只是最新。
    for (let page = 0; page < LIVE_BACKFILL_MAX_PAGES; page++) {
      const r = await fetchFrames(id, store.get(id)?.cursor ?? 0, LIVE_BACKFILL_PAGE);
      if (gen !== pollGen) return;
      if (!r.ok) break;
      applyRest(id, r.value);
      if (!r.value.more) break;
    }
  }
  if (gen !== pollGen) return;
  pollTimer = setTimeout(() => void pollTick(gen), LIVE_POLL_FRAMES_MS * slow);
}

// ── 后台标签页 ─────────────────────────────────────────────────────────────

function onVisibility(): void {
  const hidden = env().isHidden();
  if (hidden) {
    if (hiddenTimer === undefined && !slowMode) {
      hiddenTimer = setTimeout(() => {
        hiddenTimer = undefined;
        if (totalWatchers() === 0) return;
        slowMode = true;
        slowCursor.clear();
        for (const id of wantedStreams()) {
          const st = store.get(id);
          if (st && isHeavyKind(st.meta.kind)) slowCursor.set(id, st.cursor);
        }
        setConn({ slow: true });
        forceReconnect();
      }, LIVE_HIDDEN_SLOW_AFTER_MS);
    }
    return;
  }
  hiddenTimer = clearTimer(hiddenTimer);
  if (slowMode) {
    slowMode = false;
    setConn({ slow: false });
    forceReconnect();
    // 降频期间被合并掉的光谱帧：从降频开始时的游标往后，把服务端还保留的取回来
    for (const [id, cursor] of slowCursor) {
      if ((streamWatchers.get(id) ?? 0) > 0) void requestHistory(id, LIVE_HISTORY_MAX, { since: cursor, force: true });
    }
    slowCursor.clear();
    return;
  }
  if (totalWatchers() > 0 && conn.status !== "open" && conn.status !== "unsupported") forceReconnect();
}

let globalsBound = false;

function bindGlobals(): void {
  if (globalsBound) return;
  globalsBound = true;
  const g = globalThis as unknown as {
    addEventListener?: (t: string, cb: () => void) => void;
    document?: { addEventListener?: (t: string, cb: () => void) => void };
  };
  g.addEventListener?.("online", () => {
    if (totalWatchers() > 0 && conn.status !== "open") forceReconnect();
  });
  g.document?.addEventListener?.("visibilitychange", onVisibility);
}

// ── 对外：订阅 ─────────────────────────────────────────────────────────────

/** 要一个流的帧（引用计数）。返回取消函数；重复调用取消是安全的（StrictMode）。 */
export function watchStream(id: string): () => void {
  bindGlobals();
  streamWatchers.set(id, (streamWatchers.get(id) ?? 0) + 1);
  ensureRunning();
  let done = false;
  return () => {
    if (done) return;
    done = true;
    const n = (streamWatchers.get(id) ?? 1) - 1;
    if (n <= 0) streamWatchers.delete(id);
    else streamWatchers.set(id, n);
    ensureRunning();
  };
}

/** 只要流的列表（不要任何流的帧）——数据流列表页挂着它。 */
export function watchList(): () => void {
  bindGlobals();
  listWatchers += 1;
  ensureRunning();
  let done = false;
  return () => {
    if (done) return;
    done = true;
    listWatchers = Math.max(0, listWatchers - 1);
    ensureRunning();
  };
}

/** 后台标签页的可见性变化（测试里直接调；浏览器里由 visibilitychange 驱动）。 */
export function notifyVisibilityChanged(): void {
  onVisibility();
}

export function getLiveConnState(): LiveConnState {
  return conn;
}

export function subscribeLiveStatus(cb: () => void): () => void {
  statusListeners.add(cb);
  return () => {
    statusListeners.delete(cb);
  };
}

/** 当前连接上已订阅的流（给测试与诊断面板）。 */
export function getSubscribed(): string[] {
  return [...subscribed].sort();
}

/** 最近一次看到的服务端 LiveHub id。 */
export function getLiveHubId(): string | null {
  return lastHub;
}

/** 测试用：拆掉一切并换一个新的缓存。 */
export function resetLiveClientForTests(): void {
  shutdown();
  for (const t of backfillRetry.values()) clearTimeout(t);
  backfillRetry.clear();
  backfilling.clear();
  historyDone.clear();
  streamWatchers.clear();
  slowCursor.clear();
  listWatchers = 0;
  pollStartTimer = clearTimer(pollStartTimer);
  notOpenSince = null;
  syncQueued = false;
  slowMode = false;
  lastHub = null;
  store = new LiveStore();
  conn = { status: "idle", polling: false, slow: false, lastTrafficMs: null, problem: null };
}
