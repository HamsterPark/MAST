// ════════════════════════════════════════════════════════════════════════════
// LiveHub 线上格式（src/lib/live/protocol.ts + seqRanges.ts）。
//
//     cd frontend && npm run test:unit
//
// 契约 §7 的消息与帧在这里逐条解一遍。重点不在「格式对的能解」，在「格式不对的
// 不会把连接掐断、不会被当成数据画出来」：一条认不出的消息是要忽略的事实。
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  decodeMap2dFrame,
  decodeScalarFrame,
  decodeSpectrumFrame,
  decodeStatusFrame,
  decodeXyFrame,
  epochSeconds,
  isIncrementalKind,
  map2dGeometry,
  normalizeStreamMeta,
  parseLiveMessage,
  parseRestFrames,
  parseRestStreams,
  streamPath,
  subscribeOp,
  unsubscribeOp,
  type StreamMeta,
} from "../src/lib/live/protocol.ts";
import { SeqRanges } from "../src/lib/live/seqRanges.ts";

function meta(over: Partial<StreamMeta> & { id: string; kind: string }): StreamMeta {
  return normalizeStreamMeta({ title: over.id, source: "test", ...over })!;
}

describe("parseLiveMessage", () => {
  it("解开契约里的五种消息", () => {
    const s = parseLiveMessage(
      JSON.stringify({ kind: "streams", items: [{ id: "sat1/ccd/spectrum", kind: "spectrum", title: "CCD", last_seq: 7 }] }),
    );
    assert.equal(s.kind, "streams");
    if (s.kind === "streams") {
      assert.equal(s.items.length, 1);
      assert.equal(s.items[0]!.last_seq, 7);
      assert.equal(s.items[0]!.ended, false);
    }

    const a = parseLiveMessage('{"kind":"axis","stream":"s","ver":3,"x":[500,500.5,null],"unit":"nm"}');
    assert.equal(a.kind, "axis");
    if (a.kind === "axis") {
      assert.equal(a.ver, 3);
      assert.equal(a.x.length, 3);
      assert.ok(Number.isNaN(a.x[2]!), "null 在轴里是 NaN，不是 0");
    }

    const f = parseLiveMessage('{"kind":"frame","stream":"s","seq":12,"frame":{"t":1,"y":[1,2]}}');
    assert.deepEqual(f.kind === "frame" && [f.stream, f.seq], ["s", 12]);

    assert.equal(parseLiveMessage('{"kind":"ping"}').kind, "ping");

    const d = parseLiveMessage('{"kind":"dropped","stream":"s","from":5,"to":9}');
    assert.deepEqual(d.kind === "dropped" && [d.from, d.to], [5, 9]);
  });

  it("合批 frames、目录的 hub id、服务端的 error", () => {
    const f = parseLiveMessage('{"kind":"frames","stream":"s","items":[{"seq":1,"frame":{}},{"seq":"x"},{"seq":2,"frame":{"t":1}}]}');
    assert.equal(f.kind, "frames");
    if (f.kind === "frames") assert.deepEqual(f.items.map((i) => i.seq), [1, 2], "坏项丢掉，其余照收");
    const s = parseLiveMessage('{"kind":"streams","hub":"abc123","items":[]}');
    assert.equal(s.kind === "streams" && s.hub, "abc123");
    const e = parseLiveMessage('{"kind":"error","op":"subscribe","message":"bad"}');
    assert.deepEqual(e, { kind: "error", op: "subscribe", message: "bad" });
  });

  it("REST 帧响应：较旧的轴、翻页标记、补不回来的那一段", () => {
    const r = parseRestFrames({
      hub: "h",
      meta: { id: "s", kind: "spectrum", uid: "u1", last_seq: 9 },
      axis: { ver: 3, unit: "nm", n: 2, x: [1, 2] },
      axes: [{ ver: 2, unit: "nm", n: 2, x: [0, 1] }, { ver: "bad" }],
      frames: [],
      more: true,
      dropped: { from: 4, to: 6 },
    });
    assert.equal(r.axis?.ver, 3);
    assert.deepEqual(r.axes.map((a) => a.ver), [2]);
    assert.equal(r.more, true);
    assert.deepEqual(r.dropped, { from: 4, to: 6 });
    assert.equal(r.meta?.epoch, "u1");
  });

  it("认不出的一律 ignored，不抛", () => {
    for (const raw of [
      "",
      "not json",
      "null",
      "[1,2]",
      '{"kind":"frame","stream":"s"}', // 没有 seq
      '{"kind":"frame","stream":"s","seq":-1}',
      '{"kind":"frame","stream":"s","seq":1.5}',
      '{"kind":"dropped","stream":"s","from":9,"to":5}', // 倒着的区间
      '{"kind":"axis","stream":"s","ver":1}', // 没有 x
      '{"kind":"streams"}',
      '{"kind":"something_from_2027"}',
      // Python json.dumps 默认会写出的裸 NaN —— 不是合法 JSON
      '{"kind":"frame","stream":"s","seq":1,"frame":{"t":1,"values":{"a":NaN}}}',
    ]) {
      assert.equal(parseLiveMessage(raw).kind, "ignored", raw);
    }
  });

  it("流列表里缺 id 或 kind 的项被丢掉，其余照收", () => {
    const m = parseLiveMessage(
      JSON.stringify({ kind: "streams", items: [{ kind: "xy" }, { id: "a/b", kind: "xy" }, 5, null] }),
    );
    assert.equal(m.kind === "streams" && m.items.length, 1);
  });
});

describe("normalizeStreamMeta", () => {
  it("认 stream_id / stream 两种键名，缺省值不是 undefined", () => {
    const a = normalizeStreamMeta({ stream_id: "x/y/z", kind: "scalar" })!;
    assert.equal(a.id, "x/y/z");
    assert.equal(a.title, "x/y/z");
    assert.equal(a.last_seq, -1);
    assert.deepEqual(a.meta, {});
    assert.equal(a.epoch, null);
    assert.equal(normalizeStreamMeta({ stream: "q", kind: "status" })!.id, "q");
  });

  it("代次标记是服务端的 uid（流被重建时换一个）；字符串或数字都认", () => {
    assert.equal(normalizeStreamMeta({ id: "a", kind: "xy", uid: "9f3c" })!.epoch, "9f3c");
    assert.equal(normalizeStreamMeta({ id: "a", kind: "xy", epoch: 3 })!.epoch, "3");
  });
});

describe("map2d 几何", () => {
  it("完整的 meta 解出几何", () => {
    const g = map2dGeometry(meta({ id: "m", kind: "map2d", meta: { nx: 4, ny: 3, x0: 1, y0: 2, dx: 0.5, dy: -0.5, x_unit: "nm", y_unit: "nm" } }));
    assert.deepEqual(g, { nx: 4, ny: 3, x0: 1, y0: 2, dx: 0.5, dy: -0.5, x_unit: "nm", y_unit: "nm" });
  });

  it("缺 nx/ny、步长为零、像素过多都拒绝 —— 不拿默认值硬画", () => {
    assert.equal(map2dGeometry(meta({ id: "m", kind: "map2d", meta: { ny: 3 } })), null);
    assert.equal(map2dGeometry(meta({ id: "m", kind: "map2d", meta: { nx: 3, ny: 3, dx: 0 } })), null);
    assert.equal(map2dGeometry(meta({ id: "m", kind: "map2d", meta: { nx: 2.5, ny: 3 } })), null);
    assert.equal(map2dGeometry(meta({ id: "m", kind: "map2d", meta: { nx: 4096, ny: 4096 } })), null);
  });
});

describe("帧解码", () => {
  it("spectrum：y 变成 Float64Array，null → NaN", () => {
    const f = decodeSpectrumFrame({ t: 10, y: [1, null, 3], axis_ver: 2, meta: { exposure_ms: 100 } })!;
    assert.equal(f.y.length, 3);
    assert.ok(Number.isNaN(f.y[1]!));
    assert.equal(f.axis_ver, 2);
    assert.equal(decodeSpectrumFrame({ t: 1 }), null);
  });

  it("scalar / xy / map2d 的 null 读数保留为 null（不是 0）", () => {
    assert.deepEqual(decodeScalarFrame({ t: 1, values: { a: 1, b: null } })!.values, { a: 1, b: null });
    assert.deepEqual(decodeXyFrame({ t: 1, x: 2, values: { a: null } })!.values, { a: null });
    assert.equal(decodeXyFrame({ t: 1, values: {} }), null, "xy 没有 x 不收");
    assert.equal(decodeMap2dFrame({ t: 1, ix: 1.5, iy: 0, values: {} }), null, "像素下标必须是整数");
    assert.deepEqual(decodeMap2dFrame({ t: 1, ix: 1, iy: 2, values: { c: 3 } }), { t: 1, ix: 1, iy: 2, values: { c: 3 } });
  });

  it("status 缺 level 时按 info", () => {
    assert.equal(decodeStatusFrame({ t: 1, text: "hi" })!.level, "info");
    assert.equal(decodeStatusFrame({ t: 1 }), null);
  });

  it("帧时间：秒原样，毫秒折算", () => {
    assert.equal(epochSeconds(1_760_000_000.5), 1_760_000_000.5);
    assert.equal(epochSeconds(1_760_000_000_500), 1_760_000_000.5);
    assert.equal(epochSeconds(null), null);
  });
});

describe("订阅消息", () => {
  it("since 只带游标 > 0 的流", () => {
    assert.deepEqual(subscribeOp(["a", "b"], { a: 5, b: 0 }), { op: "subscribe", streams: ["a", "b"], since: { a: 5 } });
    assert.deepEqual(subscribeOp(["a"], {}), { op: "subscribe", streams: ["a"] });
    assert.deepEqual(unsubscribeOp(["a"]), { op: "unsubscribe", streams: ["a"] });
  });

  it("流 id 拼进 URL 时斜杠保留、每段转义", () => {
    assert.equal(streamPath("local/ters_map/run 1"), "local/ters_map/run%201");
    assert.equal(streamPath("a/b?c"), "a/b%3Fc");
  });
});

describe("REST 响应", () => {
  it("帧按 seq 排好，坏项丢掉", () => {
    const r = parseRestFrames({
      meta: { id: "s", kind: "xy", last_seq: 9 },
      axis: null,
      frames: [{ seq: 9, frame: {} }, { seq: 7, frame: {} }, { frame: {} }, "x"],
    });
    assert.deepEqual(r.frames.map((f) => f.seq), [7, 9]);
    assert.equal(r.meta?.last_seq, 9);
  });

  it("列表认 {items} 和裸数组", () => {
    assert.equal(parseRestStreams({ items: [{ id: "a", kind: "xy" }] })!.length, 1);
    assert.equal(parseRestStreams([{ id: "a", kind: "xy" }])!.length, 1);
    assert.equal(parseRestStreams({ nope: 1 }), null);
  });
});

describe("kind 的分类", () => {
  it("增量流与只看最新的流", () => {
    assert.equal(isIncrementalKind("xy"), true);
    assert.equal(isIncrementalKind("map2d"), true);
    assert.equal(isIncrementalKind("status"), true);
    assert.equal(isIncrementalKind("spectrum"), false);
    assert.equal(isIncrementalKind("scalar"), false);
  });
});

describe("SeqRanges", () => {
  it("重叠与相邻的区间合并", () => {
    const r = new SeqRanges();
    r.add(5, 7);
    r.add(9, 10);
    assert.deepEqual(r.list(), [[5, 7], [9, 10]]);
    r.add(8, 8); // 相邻：三段并成一段
    assert.deepEqual(r.list(), [[5, 10]]);
    r.add(1, 2);
    r.add(12, 15);
    r.add(2, 13);
    assert.deepEqual(r.list(), [[1, 15]]);
    assert.equal(r.total(), 15);
  });

  it("删掉中间一个点会把区间拆开", () => {
    const r = new SeqRanges();
    r.add(1, 5);
    r.remove(3);
    assert.deepEqual(r.list(), [[1, 2], [4, 5]]);
    assert.equal(r.has(3), false);
    assert.equal(r.has(4), true);
    r.removeRange(0, 4);
    assert.deepEqual(r.list(), [[5, 5]]);
    assert.equal(r.first(), 5);
    r.remove(5);
    assert.equal(r.isEmpty(), true);
    assert.equal(r.first(), null);
  });

  it("非法区间忽略", () => {
    const r = new SeqRanges();
    r.add(5, 4);
    r.add(Number.NaN, 3);
    assert.equal(r.isEmpty(), true);
  });
});
