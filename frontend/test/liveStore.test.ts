// ════════════════════════════════════════════════════════════════════════════
// LiveStore（src/lib/live/store.ts）—— 浏览器侧的流缓存。
//
//     cd frontend && npm run test:unit
//
// 这里钉的是「画出来的是不是全的、是不是新的」：
//   · 增量流跳号记缺、补到划掉、补不回记 lost；
//   · 只看最新的流跳号不算缺，历史帧按 seq 插回；
//   · 服务端重启（seq 从头来）能认出来并清空重来 —— 认不出的后果是此后每一帧都被
//     当成重复丢掉，图冻在重启前那一刻，而界面上没有任何异常。
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { normalizeStreamMeta, type StreamMeta } from "../src/lib/live/protocol.ts";
import { LIST_EVIDENCE_GAP_MS, LiveStore, MAP2D_LOG_CAPACITY, map2dChangesSince } from "../src/lib/live/store.ts";

function meta(id: string, kind: string, extra: Record<string, unknown> = {}): StreamMeta {
  return normalizeStreamMeta({ id, kind, title: id, source: "t", ...extra })!;
}

function storeWith(...metas: StreamMeta[]): LiveStore {
  const s = new LiveStore({ now: () => 1000 });
  s.applyStreams(metas, { full: true, trustLastSeq: false });
  return s;
}

const xy = (x: number, v: number | null = x) => ({ t: 100 + x, x, values: { s: v } });
const MAP_META = { meta: { nx: 4, ny: 3, x0: 0, y0: 0, dx: 1, dy: 1 } };

describe("增量流：缺口与补齐", () => {
  it("冷启动的第一帧前面不记缺（补不回来，也不知道有没有）", () => {
    const s = storeWith(meta("p", "xy"));
    assert.equal(s.applyFrame("p", 50, xy(1)), "applied");
    assert.equal(s.get("p")!.missing.isEmpty(), true);
    assert.equal(s.get("p")!.cursor, 50);
  });

  it("跳号记缺，补到的帧收、重复的帧不收", () => {
    const s = storeWith(meta("p", "xy"));
    s.applyFrame("p", 1, xy(1));
    assert.equal(s.applyFrame("p", 5, xy(5)), "gap");
    assert.deepEqual(s.get("p")!.missing.list(), [[2, 4]]);
    assert.equal(s.applyFrame("p", 3, xy(3)), "applied", "缺口里的旧帧要收");
    assert.deepEqual(s.get("p")!.missing.list(), [[2, 2], [4, 4]]);
    assert.equal(s.applyFrame("p", 3, xy(3)), "duplicate", "同一帧第二次是重复");
    assert.equal(s.applyFrame("p", 5, xy(5)), "duplicate");
    const pts = s.get("p")!.buf;
    assert.equal(pts.kind, "xy");
    if (pts.kind === "xy") assert.deepEqual(pts.points.map((p) => p.seq), [1, 3, 5], "按采集顺序插回原位");
  });

  it("dropped 只把游标之后的部分记成缺；之后真到的帧照常划掉", () => {
    const s = storeWith(meta("p", "xy"));
    s.applyFrame("p", 1, xy(1));
    s.applyFrame("p", 2, xy(2));
    assert.equal(s.applyDropped("p", 1, 6), true);
    assert.deepEqual(s.get("p")!.missing.list(), [[3, 6]], "已经收到的 1、2 不算缺");
    // REST 补到的 3..6 在游标之后 —— 也要从缺里划掉，否则补齐会一直重要
    for (let k = 3; k <= 6; k++) s.applyFrame("p", k, xy(k));
    assert.equal(s.get("p")!.missing.isEmpty(), true);
    assert.equal(s.get("p")!.cursor, 6);
  });

  it("只看最新的流不理 dropped", () => {
    const s = storeWith(meta("sp", "spectrum"));
    s.applyFrame("sp", 1, { t: 1, y: [1] });
    assert.equal(s.applyDropped("sp", 2, 9), false);
    assert.equal(s.get("sp")!.missing.isEmpty(), true);
  });

  it("markLost 只数真缺的那些", () => {
    const s = storeWith(meta("p", "xy"));
    s.applyFrame("p", 1, xy(1));
    s.applyFrame("p", 10, xy(10)); // 缺 2..9
    s.applyFrame("p", 4, xy(4)); // 补到 4
    s.markLost("p", 1, 6); // 2,3,5,6 补不回来
    assert.equal(s.get("p")!.lost, 4);
    assert.deepEqual(s.get("p")!.missing.list(), [[7, 9]]);
  });

  it("解不出来的帧计入 invalid，也推进游标（不被当成缺一遍遍去要）", () => {
    const s = storeWith(meta("p", "xy"));
    s.applyFrame("p", 1, xy(1));
    assert.equal(s.applyFrame("p", 2, { t: 1, values: {} }), "invalid");
    assert.equal(s.get("p")!.cursor, 2);
    assert.equal(s.get("p")!.invalid, 1);
    assert.equal(s.get("p")!.missing.isEmpty(), true);
  });
});

describe("只看最新的流", () => {
  it("跳号是常态，不记缺", () => {
    const s = storeWith(meta("sp", "spectrum"));
    s.applyFrame("sp", 1, { t: 1, y: [1, 2] });
    assert.equal(s.applyFrame("sp", 40, { t: 2, y: [3, 4] }), "applied");
    assert.equal(s.get("sp")!.missing.isEmpty(), true);
  });

  it("历史帧按 seq 插回，重复的不收，超过上限砍最旧的", () => {
    const s = new LiveStore({ caps: { spectrum: 3 } });
    s.applyStreams([meta("sp", "spectrum")], { full: true, trustLastSeq: false });
    s.applyFrame("sp", 10, { t: 10, y: [10] });
    assert.equal(s.applyFrame("sp", 7, { t: 7, y: [7] }), "applied");
    assert.equal(s.applyFrame("sp", 7, { t: 7, y: [7] }), "duplicate");
    s.applyFrame("sp", 8, { t: 8, y: [8] });
    s.applyFrame("sp", 11, { t: 11, y: [11] });
    const b = s.get("sp")!.buf;
    assert.ok(b.kind === "spectrum");
    if (b.kind === "spectrum") assert.deepEqual(b.entries.map((e) => e.seq), [8, 10, 11]);
    assert.equal(s.get("sp")!.lastT, 11, "lastT 跟着最新那一帧，不跟着最后到的历史帧");
  });

  it("scalar：中途冒出来的通道，前面补 null；超过上限一次多砍一截", () => {
    const s = new LiveStore({ caps: { scalar: 10 } });
    s.applyStreams([meta("pol", "scalar")], { full: true, trustLastSeq: false });
    s.applyFrame("pol", 1, { t: 1, values: { a: 1 } });
    s.applyFrame("pol", 2, { t: 2, values: { a: 2, b: 20 } });
    const b = s.get("pol")!.buf;
    assert.ok(b.kind === "scalar");
    if (b.kind !== "scalar") return;
    assert.deepEqual(b.channels, ["a", "b"]);
    assert.deepEqual(b.values.get("b"), [null, 20]);
    for (let k = 3; k <= 11; k++) s.applyFrame("pol", k, { t: k, values: { a: k } });
    // 第 11 帧溢出：一次砍掉溢出的 1 帧再加上限的 10%（1 帧），剩 9 —— 不是剩 10。
    assert.equal(b.seqs.length, 9, "满了之后一次多砍一截，免得之后每帧都 splice");
    s.applyFrame("pol", 12, { t: 12, values: { a: 12 } });
    assert.deepEqual(b.seqs.slice(0, 2), [3, 4], "砍掉的是最旧的");
    assert.equal(b.seqs.length, b.t.length);
    assert.equal(b.values.get("a")!.length, b.seqs.length);
    assert.equal(b.values.get("b")!.length, b.seqs.length);
    assert.equal(b.seqs.at(-1), 12);
  });
});

describe("服务端重启", () => {
  it("新连接上第一份列表的 last_seq 比游标小 → 清空重来", () => {
    const s = storeWith(meta("p", "xy"));
    for (let k = 1; k <= 30; k++) s.applyFrame("p", k, xy(k));
    const reset = s.applyStreams([meta("p", "xy", { last_seq: 3 })], { full: true, trustLastSeq: true });
    assert.deepEqual(reset, ["p"]);
    const st = s.get("p")!;
    assert.equal(st.cursor, 0);
    assert.equal(st.resets, 1);
    // 重启后的第 1 帧要收 —— 不清空的话它会被当成重复
    assert.equal(s.applyFrame("p", 1, xy(1)), "applied");
  });

  it("不可信的列表（同一连接上的后续列表）不拿 last_seq 判重启", () => {
    const s = storeWith(meta("p", "xy"));
    for (let k = 1; k <= 30; k++) s.applyFrame("p", k, xy(k));
    const reset = s.applyStreams([meta("p", "xy", { last_seq: 28 })], { full: false, trustLastSeq: false });
    assert.deepEqual(reset, []);
    assert.equal(s.get("p")!.cursor, 30);
  });

  it("有 uid 的流：uid 没变就不拿 last_seq 判重启（同一进程里 seq 永不回退）", () => {
    const s = storeWith(meta("p", "xy", { uid: "u1" }));
    for (let k = 1; k <= 30; k++) s.applyFrame("p", k, xy(k));
    const reset = s.applyStreams([meta("p", "xy", { uid: "u1", last_seq: 3 })], { full: true, trustLastSeq: true });
    assert.deepEqual(reset, []);
    assert.equal(s.get("p")!.cursor, 30);
  });

  it("hub id 变了（newGeneration）→ 全部清空，不论 uid", () => {
    const s = storeWith(meta("p", "xy", { uid: "u1" }), meta("q", "scalar", { uid: "u2" }));
    s.applyFrame("p", 5, xy(5));
    s.applyFrame("q", 5, { t: 5, values: { a: 1 } });
    const reset = s.applyStreams([meta("p", "xy", { uid: "u1" }), meta("q", "scalar", { uid: "u2" })], {
      full: true,
      trustLastSeq: false,
      newGeneration: true,
    });
    assert.deepEqual(reset.sort(), ["p", "q"]);
    assert.equal(s.get("p")!.cursor, 0);
  });

  it("代次标记变了 → 不论 last_seq 都清空", () => {
    const s = storeWith(meta("p", "xy", { epoch: "a" }));
    s.applyFrame("p", 5, xy(5));
    assert.deepEqual(s.applyStreams([meta("p", "xy", { epoch: "b", last_seq: 9 })], { full: false, trustLastSeq: false }), ["p"]);
  });

  it("kind 变了、map2d 几何变了也清空", () => {
    const s = storeWith(meta("q", "xy"), meta("m", "map2d", MAP_META));
    s.applyFrame("q", 1, xy(1));
    s.applyFrame("m", 1, { t: 1, ix: 0, iy: 0, values: { c: 1 } });
    const reset = s.applyStreams(
      [meta("q", "scalar"), meta("m", "map2d", { meta: { nx: 8, ny: 3, dx: 1, dy: 1 } })],
      { full: false, trustLastSeq: false },
    );
    assert.deepEqual(reset.sort(), ["m", "q"]);
    assert.equal(s.get("q")!.buf.kind, "scalar");
  });

  it("完整列表里没有的流标成 gone，再出现时取消", () => {
    const s = storeWith(meta("a", "xy"), meta("b", "xy"));
    s.applyStreams([meta("a", "xy")], { full: true, trustLastSeq: false });
    assert.equal(s.get("b")!.gone, true);
    assert.equal(s.get("a")!.gone, false);
    s.applyStreams([meta("b", "xy")], { full: false, trustLastSeq: false });
    assert.equal(s.get("b")!.gone, false);
  });
});

describe("列表还没到时的帧", () => {
  it("先暂存，列表到了按 seq 重放", () => {
    const s = new LiveStore();
    assert.equal(s.applyFrame("late", 2, xy(2)), "pending");
    assert.equal(s.applyFrame("late", 1, xy(1)), "pending");
    s.applyStreams([meta("late", "xy")], { full: false, trustLastSeq: false });
    const b = s.get("late")!.buf;
    assert.ok(b.kind === "xy");
    if (b.kind === "xy") assert.deepEqual(b.points.map((p) => p.seq), [1, 2]);
    assert.equal(s.get("late")!.missing.isEmpty(), true);
  });
});

describe("map2d", () => {
  it("同一像素：新的 seq 赢；旧帧晚到不覆盖", () => {
    const s = storeWith(meta("m", "map2d", MAP_META));
    s.applyFrame("m", 2, { t: 1, ix: 1, iy: 2, values: { c: 5 } });
    s.applyFrame("m", 9, { t: 2, ix: 1, iy: 2, values: { c: 7 } });
    s.applyFrame("m", 4, { t: 3, ix: 3, iy: 0, values: { c: 1 } }); // 补缺
    const b = s.get("m")!.buf;
    assert.ok(b.kind === "map2d");
    if (b.kind !== "map2d") return;
    const idx = 2 * 4 + 1;
    assert.equal(b.values.get("c")![idx], 7);
    assert.equal(b.filled, 2);
    // 让 seq 3 作为缺口里的旧帧晚到：它写的是同一个像素，不该盖掉 seq 9 的 7
    s.get("m")!.missing.add(3, 3);
    s.applyFrame("m", 3, { t: 4, ix: 1, iy: 2, values: { c: -1 } });
    assert.equal(b.values.get("c")![idx], 7);
  });

  it("越界像素拒收并计数；没采到的像素是 NaN 不是 0", () => {
    const s = storeWith(meta("m", "map2d", MAP_META));
    assert.equal(s.applyFrame("m", 1, { t: 1, ix: 4, iy: 0, values: { c: 1 } }), "invalid");
    s.applyFrame("m", 2, { t: 1, ix: 0, iy: 0, values: { c: 0 } });
    const b = s.get("m")!.buf;
    if (b.kind !== "map2d") throw new Error("kind");
    assert.equal(b.outOfRange, 1);
    assert.equal(b.values.get("c")![0], 0);
    assert.ok(Number.isNaN(b.values.get("c")![1]!));
  });

  it("几何缺失的 map2d 流：帧解不出来，但流还在（界面要说清楚为什么画不了）", () => {
    const s = storeWith(meta("m", "map2d"));
    assert.equal(s.applyFrame("m", 1, { t: 1, ix: 0, iy: 0, values: { c: 1 } }), "invalid");
    const b = s.get("m")!.buf;
    assert.ok(b.kind === "map2d" && b.geom === null);
  });

  it("两个视图各读各的增量，谁也不吃掉谁的", () => {
    const s = storeWith(meta("m", "map2d", MAP_META));
    const b = s.get("m")!.buf;
    if (b.kind !== "map2d") throw new Error("kind");
    s.applyFrame("m", 1, { t: 1, ix: 0, iy: 0, values: { c: 1 } });
    const a1 = map2dChangesSince(b, 0);
    assert.equal(a1.full, true, "第一次画整幅");
    s.applyFrame("m", 2, { t: 1, ix: 1, iy: 0, values: { c: 1 } });
    s.applyFrame("m", 3, { t: 1, ix: 2, iy: 0, values: { c: 1 } });
    const a2 = map2dChangesSince(b, a1.next);
    assert.deepEqual(a2, { full: false, idx: [1, 2], next: 3 });
    // 第二个视图从 1 开始读，照样看得到 1、2 两个像素
    const v2 = map2dChangesSince(b, 1);
    assert.deepEqual(v2.idx, [1, 2]);
  });

  it("落后超过日志容量就整幅重画", () => {
    const s = storeWith(meta("m", "map2d", { meta: { nx: 512, ny: 512, dx: 1, dy: 1 } }));
    const b = s.get("m")!.buf;
    if (b.kind !== "map2d") throw new Error("kind");
    for (let k = 0; k < MAP2D_LOG_CAPACITY + 10; k++) {
      s.applyFrame("m", k + 1, { t: 1, ix: k % 512, iy: Math.floor(k / 512), values: { c: k } });
    }
    assert.equal(map2dChangesSince(b, 5).full, true);
    assert.equal(map2dChangesSince(b, b.logCount - 3).idx.length, 3);
  });
});

describe("status 与未知 kind", () => {
  it("status 按 seq 排，未知 kind 原样留帧", () => {
    const s = storeWith(meta("log", "status"), meta("new", "hologram"));
    s.applyFrame("log", 2, { t: 2, text: "b", level: "warn" });
    s.applyFrame("log", 1, { t: 1, text: "a" }); // 冷启动后游标 2，1 不在缺里 → 重复
    const b = s.get("log")!.buf;
    if (b.kind !== "status") throw new Error("kind");
    assert.deepEqual(b.lines.map((l) => l.text), ["b"]);
    assert.equal(s.applyFrame("new", 1, { anything: [1, 2] }), "applied");
    assert.equal(s.get("new")!.buf.kind, "raw");
  });
});

describe("变化通知", () => {
  it("写入时同步通知流 id，列表变化单独通知", () => {
    const s = storeWith(meta("p", "xy"));
    const seen: string[] = [];
    let lists = 0;
    s.onChange((id) => seen.push(id));
    s.onList(() => (lists += 1));
    s.applyFrame("p", 1, xy(1));
    s.applyStreams([meta("q", "xy")], { full: false, trustLastSeq: false });
    assert.deepEqual(seen, ["p"]);
    assert.equal(lists, 1);
  });

  it("一个监听者抛错不挡住别的", () => {
    const s = storeWith(meta("p", "xy"));
    let ok = 0;
    s.onChange(() => {
      throw new Error("boom");
    });
    s.onChange(() => (ok += 1));
    s.applyFrame("p", 1, xy(1));
    assert.equal(ok, 1);
  });
});

// 「跟随最新」靠 lastLiveMs 判断流是不是正在来数据。一小时前的平均谱，订阅时被服务端重放过来，
// 不能看起来像刚采的 —— 否则一打开页面面板就切到它上面。
describe("正在产出（lastLiveMs）", () => {
  function clocked() {
    let t = 1000;
    const s = new LiveStore({ now: () => t });
    return { s, at: (ms: number) => (t = ms) };
  }

  it("订阅时重放的最新一帧不算，比目录里 last_seq 新的才算", () => {
    const { s, at } = clocked();
    s.applyStreams([meta("sp", "spectrum", { last_seq: 7 })], { full: true, trustLastSeq: false });
    at(2000);
    s.applyFrame("sp", 7, { t: 1, y: [1] });
    assert.equal(s.get("sp")!.lastRecvMs, 2000);
    assert.equal(s.get("sp")!.lastLiveMs, null, "重放");
    at(3000);
    s.applyFrame("sp", 8, { t: 2, y: [2] });
    assert.equal(s.get("sp")!.lastLiveMs, 3000);
  });

  it("补齐的旧帧不算", () => {
    const { s, at } = clocked();
    s.applyStreams([meta("p", "xy")], { full: true, trustLastSeq: false });
    s.applyFrame("p", 1, xy(1));
    s.applyFrame("p", 5, xy(5));
    const live = s.get("p")!.lastLiveMs;
    at(9000);
    assert.equal(s.applyFrame("p", 3, xy(3)), "applied");
    assert.equal(s.get("p")!.lastLiveMs, live);
  });

  it("没订阅的流：相隔不久的两份目录之间 last_seq 涨了才算，隔太久不算", () => {
    const { s, at } = clocked();
    s.applyStreams([meta("q", "spectrum", { last_seq: 3 })], { full: true, trustLastSeq: false });
    at(4000);
    s.applyStreams([meta("q", "spectrum", { last_seq: 3 })], { full: true, trustLastSeq: false });
    assert.equal(s.get("q")!.lastLiveMs, null, "没涨");
    at(7000);
    s.applyStreams([meta("q", "spectrum", { last_seq: 5 })], { full: true, trustLastSeq: false });
    assert.equal(s.get("q")!.lastLiveMs, 7000);
    at(7000 + LIST_EVIDENCE_GAP_MS + 1);
    s.applyStreams([meta("q", "spectrum", { last_seq: 9 })], { full: true, trustLastSeq: false });
    assert.equal(s.get("q")!.lastLiveMs, 7000, "隔太久：不知道那几帧是什么时候来的");
  });
});
