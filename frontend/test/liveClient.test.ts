// ════════════════════════════════════════════════════════════════════════════
// /ws/live 传输层（src/lib/liveClient.ts）：订阅、since 续传、退避重连、dropped 补齐、
// REST 轮询兜底、后台降频。
//
//     cd frontend && npm run test:unit
//
// 和 ws.reconnect.test.ts 同一个做法：不起浏览器，换一个假的 WebSocket 和假的 fetch，
// 直接驱动真的单例。覆盖不到的是 React 绑定和画图 —— 那一半靠页面截图核对。
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it, mock } from "node:test";

import {
  LIVE_BACKOFF_MAX_MS,
  LIVE_FAIL_AFTER_ATTEMPTS,
  LIVE_HIDDEN_SLOW_AFTER_MS,
  LIVE_IDLE_MS,
  LIVE_LINGER_MS,
  LIVE_POLL_START_AFTER_MS,
  LIVE_STREAMS_WAIT_MS,
  configureLiveEnv,
  framesUrl,
  getLiveConnState,
  getLiveStore,
  getSubscribed,
  liveUrl,
  notifyVisibilityChanged,
  resetLiveClientForTests,
  shouldPollLive,
  watchList,
  watchStream,
} from "../src/lib/liveClient.ts";

// ── 一个能驱动的 WebSocket ─────────────────────────────────────────────────

class FakeSocket {
  static instances: FakeSocket[] = [];
  readyState = 0;
  url: string;
  sent: Array<Record<string, unknown>> = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((ev?: { code?: number }) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeSocket.instances.push(this);
  }
  send(data: string): void {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }
  close(): void {
    this.readyState = 3;
  }
  // —— 由测试驱动 ——
  accept(): void {
    this.readyState = 1;
    this.onopen?.();
  }
  deliver(obj: unknown): void {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
  drop(code = 1006): void {
    this.readyState = 3;
    this.onclose?.({ code });
  }
  ops(op: string): Array<Record<string, unknown>> {
    return this.sent.filter((m) => m.op === op);
  }
}

function live(): FakeSocket {
  const s = FakeSocket.instances.at(-1);
  assert.ok(s, "应当已经开了一个 socket");
  return s;
}

// ── 一个能驱动的后端 ───────────────────────────────────────────────────────

type Route = (url: URL) => unknown;
let route: Route = () => undefined;
const fetched: string[] = [];

async function fakeFetch(url: string) {
  fetched.push(url);
  const body = route(new URL(url, "http://lab"));
  if (body === undefined) return { ok: false, status: 404, json: async () => ({}) };
  return { ok: true, status: 200, json: async () => body };
}

let hidden = false;

/** 把排队的微任务和 fetch 的 promise 链跑完。 */
async function settle(): Promise<void> {
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
}

const XY = (id: string, last_seq = -1) => ({ id, kind: "xy", title: id, source: "t", last_seq });
const SP = (id: string, last_seq = -1) => ({ id, kind: "spectrum", title: id, source: "t", last_seq });
const xyFrame = (x: number) => ({ t: 100 + x, x, values: { s: x } });

const offs: Array<() => void> = [];
function keep(off: () => void): () => void {
  offs.push(off);
  return off;
}

beforeEach(() => {
  mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1_000_000 });
  while (offs.length) offs.pop()?.();
  resetLiveClientForTests();
  FakeSocket.instances = [];
  fetched.length = 0;
  route = () => undefined;
  hidden = false;
  configureLiveEnv({
    WebSocketImpl: FakeSocket as unknown as never,
    fetchImpl: fakeFetch,
    location: { protocol: "http:", host: "lab:7862" },
    isHidden: () => hidden,
    now: () => Date.now(),
  });
});

afterEach(() => {
  while (offs.length) offs.pop()?.();
  resetLiveClientForTests();
  configureLiveEnv({});
  mock.timers.reset();
});

/** 连上并交出第一份流列表。 */
async function connect(items: unknown[]): Promise<FakeSocket> {
  const s = live();
  s.accept();
  s.deliver({ kind: "streams", items });
  await settle();
  return s;
}

// ════════════════════════════════════════════════════════════════════════════

describe("纯决策", () => {
  it("地址同源，跟着页面走 wss；降频时带 max_hz", () => {
    assert.equal(liveUrl({ protocol: "http:", host: "lab:7862" }), "ws://lab:7862/ws/live");
    assert.equal(liveUrl({ protocol: "https:", host: "box.ts.net" }), "wss://box.ts.net/ws/live");
    assert.equal(liveUrl({ protocol: "http:", host: "lab" }, 1), "ws://lab/ws/live?max_hz=1");
  });

  it("REST 帧地址：since 总是显式给（缺省时服务端对光谱只回最新一帧）", () => {
    assert.equal(framesUrl("local/pump_probe/r1", 0, 10), "/api/live/streams/local/pump_probe/r1?since=0&limit=10");
    assert.equal(framesUrl("a/b", 12, 500), "/api/live/streams/a/b?since=12&limit=500");
    assert.equal(framesUrl("a/b", -1, 1), "/api/live/streams/a/b?since=-1&limit=1");
  });

  it("轮询：open 不轮询；刚掉线 2 s 内不轮询；failed / 不支持立刻轮询", () => {
    assert.equal(shouldPollLive("open", 1e9), false);
    assert.equal(shouldPollLive("idle", 1e9), false);
    assert.equal(shouldPollLive("reconnecting", 500), false);
    assert.equal(shouldPollLive("reconnecting", LIVE_POLL_START_AFTER_MS), true);
    assert.equal(shouldPollLive("failed", 0), true);
    assert.equal(shouldPollLive("unsupported", 0), true);
  });
});

describe("连接与订阅", () => {
  it("多个观察者共用一条连接；最后一个走了，留一会儿再关", async () => {
    const a = keep(watchStream("a"));
    const b = keep(watchList());
    assert.equal(FakeSocket.instances.length, 1, "一条连接，不是一个视图一条");
    assert.equal(getLiveConnState().status, "connecting");
    await connect([XY("a")]);
    assert.equal(getLiveConnState().status, "open");
    a();
    a(); // StrictMode 会调两次
    assert.equal(getLiveConnState().status, "open", "还有列表的观察者");
    b();
    assert.equal(getLiveConnState().status, "open", "切页面时前后脚卸载/挂载，不立刻断");
    mock.timers.tick(LIVE_LINGER_MS + 1);
    assert.equal(getLiveConnState().status, "idle");
  });

  it("留着的那几秒里又有视图挂上来：沿用同一条连接", async () => {
    const off = keep(watchStream("a"));
    await connect([XY("a")]);
    off();
    mock.timers.tick(LIVE_LINGER_MS - 100);
    keep(watchStream("a"));
    mock.timers.tick(LIVE_LINGER_MS);
    assert.equal(FakeSocket.instances.length, 1);
    assert.equal(getLiveConnState().status, "open");
  });

  it("先等第一份流列表再订阅；订阅带上已有的 since 游标", async () => {
    const store = getLiveStore();
    store.applyStreams([{ ...XY("p"), ended: false, x_label: null, x_unit: null, y_label: null, y_unit: null, meta: {}, retain: null, epoch: null }], { full: false, trustLastSeq: false });
    for (let k = 1; k <= 7; k++) store.applyFrame("p", k, xyFrame(k));
    keep(watchStream("p"));
    const s = live();
    s.accept();
    await settle();
    assert.equal(s.ops("subscribe").length, 0, "列表没到之前不订阅（要先判重启）");
    s.deliver({ kind: "streams", items: [XY("p", 9)] });
    await settle();
    assert.deepEqual(s.ops("subscribe"), [{ op: "subscribe", streams: ["p"], since: { p: 7 } }]);
    assert.deepEqual(getSubscribed(), ["p"]);
  });

  it("列表迟迟不来也照样订阅 —— 一份缺失的列表不许把推送卡死", async () => {
    keep(watchStream("p"));
    const s = live();
    s.accept();
    mock.timers.tick(LIVE_STREAMS_WAIT_MS + 1);
    await settle();
    assert.deepEqual(s.ops("subscribe"), [{ op: "subscribe", streams: ["p"] }]);
  });

  it("同一渲染里的多次 watch 攒成一条订阅", async () => {
    keep(watchStream("a"));
    keep(watchStream("b"));
    keep(watchStream("a"));
    const sock = await connect([XY("a"), XY("b")]);
    assert.equal(sock.ops("subscribe").length, 1);
    assert.deepEqual((sock.ops("subscribe")[0]!.streams as string[]).sort(), ["a", "b"]);
  });

  it("不看了就退订", async () => {
    const off = keep(watchStream("a"));
    keep(watchStream("b"));
    const s = await connect([XY("a"), XY("b")]);
    off();
    await settle();
    assert.deepEqual(s.ops("unsubscribe"), [{ op: "unsubscribe", streams: ["a"] }]);
    assert.deepEqual(getSubscribed(), ["b"]);
  });
});

describe("帧、缺口与补齐", () => {
  it("合批的 frames 消息逐帧进缓存", async () => {
    keep(watchStream("p"));
    const s = await connect([XY("p")]);
    s.deliver({ kind: "frames", stream: "p", items: [1, 2, 3].map((k) => ({ seq: k, frame: xyFrame(k) })) });
    const st = getLiveStore().get("p")!;
    assert.equal(st.cursor, 3);
    assert.equal(st.received, 3);
  });

  it("服务端拒收了我们的消息：说出来，连接照常", async () => {
    keep(watchStream("p"));
    const s = await connect([XY("p")]);
    const warn = console.warn;
    console.warn = () => {};
    try {
      s.deliver({ kind: "error", op: "subscribe", message: "streams 必须是 1…512 个字符串" });
    } finally {
      console.warn = warn;
    }
    assert.match(getLiveConnState().problem ?? "", /拒收/);
    assert.equal(getLiveConnState().status, "open");
  });

  it("帧进缓存；WS 与 REST 重叠的帧不会重复", async () => {
    keep(watchStream("p"));
    const s = await connect([XY("p")]);
    for (let k = 1; k <= 3; k++) s.deliver({ kind: "frame", stream: "p", seq: k, frame: xyFrame(k) });
    route = (u) =>
      u.pathname === "/api/live/streams/p"
        ? { meta: XY("p", 5), axis: null, frames: [2, 3, 4, 5].map((k) => ({ seq: k, frame: xyFrame(k) })) }
        : undefined;
    // 跳到 6：缺 4、5 → REST 从 since=3 补
    s.deliver({ kind: "frame", stream: "p", seq: 6, frame: xyFrame(6) });
    await settle();
    assert.ok(fetched.includes("/api/live/streams/p?since=3&limit=500"), fetched.join("\n"));
    const st = getLiveStore().get("p")!;
    assert.equal(st.missing.isEmpty(), true);
    assert.ok(st.buf.kind === "xy");
    if (st.buf.kind === "xy") assert.deepEqual(st.buf.points.map((p) => p.seq), [1, 2, 3, 4, 5, 6]);
  });

  it("dropped → REST 补；服务端已经不保留的那几帧记 lost", async () => {
    keep(watchStream("p"));
    const s = await connect([XY("p")]);
    s.deliver({ kind: "frame", stream: "p", seq: 1, frame: xyFrame(1) });
    s.deliver({ kind: "frame", stream: "p", seq: 2, frame: xyFrame(2) });
    route = (u) =>
      u.pathname === "/api/live/streams/p"
        ? { meta: XY("p", 8), axis: null, frames: [5, 6, 7, 8].map((k) => ({ seq: k, frame: xyFrame(k) })) }
        : undefined;
    s.deliver({ kind: "dropped", stream: "p", from: 3, to: 8 });
    await settle();
    const st = getLiveStore().get("p")!;
    assert.equal(st.missing.isEmpty(), true);
    assert.equal(st.lost, 2, "3、4 已经不在服务端的保留里");
    assert.equal(st.cursor, 8);
  });

  it("补齐的 REST 失败了：过一会儿再试，不在原地打转", async () => {
    keep(watchStream("p"));
    const s = await connect([XY("p")]);
    s.deliver({ kind: "frame", stream: "p", seq: 1, frame: xyFrame(1) });
    s.deliver({ kind: "frame", stream: "p", seq: 4, frame: xyFrame(4) });
    await settle();
    const tries = fetched.filter((u) => u.startsWith("/api/live/streams/p")).length;
    assert.equal(tries, 1, "一次失败只请求一次");
    route = (u) =>
      u.pathname === "/api/live/streams/p"
        ? { meta: XY("p", 4), axis: null, frames: [2, 3, 4].map((k) => ({ seq: k, frame: xyFrame(k) })) }
        : undefined;
    mock.timers.tick(3_001);
    await settle();
    assert.equal(getLiveStore().get("p")!.missing.isEmpty(), true);
  });
});

describe("断线与重启", () => {
  it("掉线后退避重连，重连时按游标续传", async () => {
    keep(watchStream("p"));
    const s = await connect([XY("p")]);
    s.deliver({ kind: "frame", stream: "p", seq: 4, frame: xyFrame(4) });
    s.drop();
    assert.equal(getLiveConnState().status, "reconnecting");
    assert.equal(FakeSocket.instances.length, 1, "先退避，不立刻重连");
    mock.timers.tick(LIVE_BACKOFF_MAX_MS + 1);
    assert.equal(FakeSocket.instances.length, 2);
    const s2 = await connect([XY("p", 4)]);
    assert.deepEqual(s2.ops("subscribe"), [{ op: "subscribe", streams: ["p"], since: { p: 4 } }]);
  });

  it("服务端重启（新连接上的 last_seq 比游标小）：清空这个流，按冷启动订阅", async () => {
    keep(watchStream("p"));
    const s = await connect([XY("p")]);
    for (let k = 1; k <= 30; k++) s.deliver({ kind: "frame", stream: "p", seq: k, frame: xyFrame(k) });
    s.drop();
    mock.timers.tick(LIVE_BACKOFF_MAX_MS + 1);
    const s2 = await connect([XY("p", 2)]);
    assert.deepEqual(s2.ops("subscribe"), [{ op: "subscribe", streams: ["p"] }], "不带 since —— 带了 30 就什么都收不到");
    assert.equal(getLiveStore().get("p")!.resets, 1);
    s2.deliver({ kind: "frame", stream: "p", seq: 1, frame: xyFrame(1) });
    assert.equal(getLiveStore().get("p")!.cursor, 1, "重启后的第 1 帧被收下了");
  });

  it("目录里的 hub id 变了 = MAST 重启过：全部清空，按冷启动重订（即使 uid 碰巧相同）", async () => {
    keep(watchStream("p"));
    const s = live();
    s.accept();
    s.deliver({ kind: "streams", hub: "h1", items: [{ ...XY("p"), uid: "same" }] });
    await settle();
    for (let k = 1; k <= 20; k++) s.deliver({ kind: "frame", stream: "p", seq: k, frame: xyFrame(k) });
    s.drop();
    mock.timers.tick(LIVE_BACKOFF_MAX_MS + 1);
    const s2 = live();
    s2.accept();
    s2.deliver({ kind: "streams", hub: "h2", items: [{ ...XY("p", 25), uid: "same" }] });
    await settle();
    assert.deepEqual(s2.ops("subscribe"), [{ op: "subscribe", streams: ["p"] }], "游标作废，不带 since");
    assert.equal(getLiveStore().get("p")!.resets, 1);
  });

  it("只开不送的半死连接，45 s 后强制重连；心跳能续命", async () => {
    keep(watchStream("p"));
    const s = await connect([XY("p")]);
    for (let i = 0; i < 3; i++) {
      mock.timers.tick(15_000);
      s.deliver({ kind: "ping" });
    }
    assert.equal(FakeSocket.instances.length, 1, "有心跳就不重连");
    mock.timers.tick(LIVE_IDLE_MS + 1);
    assert.equal(FakeSocket.instances.length, 2);
  });

  it("认证失败的关闭码：说出来，并且继续重试", async () => {
    keep(watchStream("p"));
    const s = live();
    s.drop(4401);
    assert.match(getLiveConnState().problem ?? "", /认证/);
    mock.timers.tick(LIVE_BACKOFF_MAX_MS + 1);
    assert.equal(FakeSocket.instances.length, 2);
  });
});

describe("轮询兜底", () => {
  it("连续失败升级为 failed 并开始轮询；WS 回来后停止轮询", async () => {
    route = (u) => (u.pathname === "/api/live/streams" ? { items: [XY("p", 0)] } : undefined);
    keep(watchStream("p"));
    for (let i = 0; i < LIVE_FAIL_AFTER_ATTEMPTS; i++) {
      live().drop();
      mock.timers.tick(LIVE_BACKOFF_MAX_MS + 1);
      await settle();
    }
    assert.equal(getLiveConnState().status, "failed");
    assert.equal(getLiveConnState().polling, true);
    assert.ok(fetched.includes("/api/live/streams"), "轮询要拉列表");
    live().accept();
    assert.equal(getLiveConnState().status, "open");
    assert.equal(getLiveConnState().polling, false);
  });

  it("浏览器没有 WebSocket：直接轮询列表与帧", async () => {
    configureLiveEnv({
      WebSocketImpl: null,
      fetchImpl: fakeFetch,
      location: { protocol: "http:", host: "lab" },
      isHidden: () => hidden,
      now: () => Date.now(),
    });
    route = (u) => {
      if (u.pathname === "/api/live/streams") return { items: [XY("p", 2)] };
      if (u.pathname === "/api/live/streams/p") {
        return { meta: XY("p", 2), axis: null, frames: [1, 2].map((k) => ({ seq: k, frame: xyFrame(k) })) };
      }
      return undefined;
    };
    keep(watchStream("p"));
    await settle();
    assert.equal(getLiveConnState().status, "unsupported");
    assert.equal(getLiveConnState().polling, true);
    assert.equal(getLiveStore().get("p")!.cursor, 2);
  });

  it("后端没有 /api/live（404）：说出来，而不是一片空白", async () => {
    configureLiveEnv({
      WebSocketImpl: null,
      fetchImpl: fakeFetch,
      location: { protocol: "http:", host: "lab" },
      isHidden: () => hidden,
      now: () => Date.now(),
    });
    keep(watchList());
    await settle();
    assert.match(getLiveConnState().problem ?? "", /404/);
  });
});

describe("后台标签页", () => {
  it("后台超过 60 s 重连成 max_hz=1；回到前台恢复全速，并用 REST 把被合并掉的光谱补回来", async () => {
    keep(watchStream("sp"));
    keep(watchStream("p"));
    const s = await connect([SP("sp"), XY("p")]);
    s.deliver({ kind: "frame", stream: "sp", seq: 3, frame: { t: 1, y: [1, 2] } });
    hidden = true;
    notifyVisibilityChanged();
    for (let t = 0; t < LIVE_HIDDEN_SLOW_AFTER_MS; t += 15_000) {
      mock.timers.tick(15_000);
      s.deliver({ kind: "ping" });
    }
    mock.timers.tick(1);
    assert.equal(getLiveConnState().slow, true);
    assert.match(live().url, /\?max_hz=1$/, "后台降频靠服务端限速，不是退订 —— 退订会在瀑布图里留一整段空白");
    const slowSock = await connect([SP("sp", 40), XY("p")]);
    assert.deepEqual(
      (slowSock.ops("subscribe")[0]!.streams as string[]).sort(),
      ["p", "sp"],
      "降频期间照样订着每一个流",
    );
    slowSock.deliver({ kind: "frame", stream: "sp", seq: 40, frame: { t: 40, y: [40, 40] } });

    route = (u) =>
      u.pathname === "/api/live/streams/sp"
        ? { meta: SP("sp", 41), axis: null, frames: [7, 8, 9].map((k) => ({ seq: k, frame: { t: k, y: [k, k] } })) }
        : undefined;
    hidden = false;
    notifyVisibilityChanged();
    await settle();
    assert.equal(getLiveConnState().slow, false);
    assert.doesNotMatch(live().url, /max_hz/, "回到前台恢复全速");
    assert.ok(fetched.includes("/api/live/streams/sp?since=3&limit=200"), fetched.join("\n"));
    const b = getLiveStore().get("sp")!.buf;
    assert.ok(b.kind === "spectrum");
    if (b.kind === "spectrum") assert.deepEqual(b.entries.map((e) => e.seq), [3, 7, 8, 9, 40]);
  });

  it("后台不到 60 s 就回来：什么都不换", async () => {
    keep(watchStream("sp"));
    const s = await connect([SP("sp")]);
    hidden = true;
    notifyVisibilityChanged();
    mock.timers.tick(10_000);
    hidden = false;
    notifyVisibilityChanged();
    // 心跳照常（不然 45 s 的空闲看门狗会重连 —— 那是另一件事）
    for (let t = 0; t < LIVE_HIDDEN_SLOW_AFTER_MS; t += 15_000) {
      mock.timers.tick(15_000);
      s.deliver({ kind: "ping" });
    }
    assert.equal(FakeSocket.instances.length, 1);
    assert.equal(getLiveConnState().slow, false);
  });
});
