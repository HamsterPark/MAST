// 固定面板选哪个流（src/lib/live/pick.ts）。
//
//     cd frontend && npm run test:unit

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { normalizeStreamMeta } from "../src/lib/live/protocol.ts";
import { LiveStore } from "../src/lib/live/store.ts";
import {
  FOLLOW_FRESH_MS,
  STREAM_HINTS,
  candidates,
  followLatest,
  matching,
  resolvePick,
} from "../src/lib/live/pick.ts";

const GRID = { nx: 4, ny: 4 };

function store() {
  let t = 1000;
  const s = new LiveStore({ now: () => t });
  const add = (id: string, kind: string, ended = false) =>
    s.applyStreams([normalizeStreamMeta({ id, kind, ended, meta: kind === "map2d" ? GRID : {} })!], {
      full: false,
      trustLastSeq: false,
    });
  add("local/ters_map/run1", "map2d", true);
  add("local/ters_map/run2", "map2d");
  add("local/optics_scan/r9", "map2d");
  add("local/pump_probe/a", "xy");
  add("local/pump_probe/b", "map2d");
  add("sat1/ccd/spectrum", "spectrum");
  add("sat1/other/spec", "spectrum");
  add("sat1/pmt/counts", "scalar");
  // 让 optics_scan 最近有数据：它在活性上排第一，但名字不匹配 TERS 图
  t = 5000;
  assert.equal(s.applyFrame("local/optics_scan/r9", 1, { ix: 0, iy: 0, values: { v: 1 } }), "applied");
  return s;
}

describe("面板选流", () => {
  it("名字匹配用途的优先，其次进行中、最近有数据", () => {
    const s = store();
    const maps = candidates(s.states(), "map2d", STREAM_HINTS.tersMap).map((x) => x.meta.id);
    assert.deepEqual(maps, [
      "local/ters_map/run2",
      "local/ters_map/run1",
      "local/optics_scan/r9",
      "local/pump_probe/b",
    ]);
  });

  it("名字匹配是偏好不是过滤：换了命名的同类流照样能选", () => {
    const s = store();
    const specs = candidates(s.states(), "spectrum", STREAM_HINTS.spectrum).map((x) => x.meta.id);
    assert.deepEqual(specs, ["sat1/ccd/spectrum", "sat1/other/spec"]);
    assert.deepEqual(
      matching(s.states(), "map2d", STREAM_HINTS.raster).map((x) => x.meta.id),
      ["local/optics_scan/r9"],
      "OpticalStageScan 的流名是 optics_scan",
    );
  });

  it("一个面板可以收几种 kind：泵浦-探测单台是 xy、双台是 map2d", () => {
    const s = store();
    assert.deepEqual(
      matching(s.states(), ["xy", "map2d"], STREAM_HINTS.pumpProbe).map((x) => x.meta.id).sort(),
      ["local/pump_probe/a", "local/pump_probe/b"],
    );
  });

  it("用户选的流还在就用它；不在了退回跟随最新", () => {
    const s = store();
    const c = candidates(s.states(), "map2d", STREAM_HINTS.tersMap);
    assert.equal(resolvePick(c, "local/ters_map/run1"), "local/ters_map/run1");
    assert.equal(resolvePick(c, "gone/stream"), "local/ters_map/run2");
    assert.equal(resolvePick(c, null), "local/ters_map/run2");
    assert.equal(resolvePick([], null), null);
  });

  it("PMT", () => {
    const s = store();
    assert.deepEqual(matching(s.states(), "scalar", STREAM_HINTS.pmt).map((x) => x.meta.id), ["sat1/pmt/counts"]);
  });
});

describe("跟随最新：粘住正在显示的流", () => {
  const RAW = "sim1/ccd/spectrum"; // 卫星转发的每一帧（含人工采的）
  const AVG = "local/spectra/ccd"; // AcquireSpectrum 每次采集的平均谱

  function bench() {
    let t = 100_000;
    const s = new LiveStore({ now: () => t });
    const seq = new Map<string, number>();
    const add = (id: string, kind = "spectrum", ended = false, last_seq?: number) =>
      s.applyStreams([normalizeStreamMeta({ id, kind, ended, last_seq, meta: kind === "map2d" ? GRID : {} })!], {
        full: false,
        trustLastSeq: false,
      });
    const frame = (id: string, atMs: number) => {
      t = atMs;
      const n = (seq.get(id) ?? 0) + 1;
      seq.set(id, n);
      const kind = s.get(id)?.meta.kind;
      const f = kind === "map2d" ? { ix: 0, iy: 0, values: { v: n } } : { t: atMs / 1000, y: [1, 2, 3], axis_ver: 0 };
      assert.equal(s.applyFrame(id, n, f), "applied");
    };
    const spectrum = (cur: string | null, atMs = t) =>
      followLatest(candidates(s.states(), "spectrum", STREAM_HINTS.spectrum), cur, atMs, {
        hint: STREAM_HINTS.spectrum,
        prefer: STREAM_HINTS.acquired,
      });
    const map = (cur: string | null, atMs = t) =>
      followLatest(candidates(s.states(), "map2d", STREAM_HINTS.tersMap), cur, atMs, { hint: STREAM_HINTS.tersMap });
    return { s, add, frame, spectrum, map };
  }

  it("两个都在来数据：不跟着最近一帧来回换", () => {
    const b = bench();
    b.add(RAW);
    b.add("sim2/ccd/spectrum");
    b.frame(RAW, 100_000);
    b.frame("sim2/ccd/spectrum", 100_100);
    assert.equal(b.spectrum(null), "sim2/ccd/spectrum", "第一次：最近有数据的");
    b.frame(RAW, 100_200);
    assert.equal(b.spectrum("sim2/ccd/spectrum"), "sim2/ccd/spectrum", "对方的帧更晚也不换");
    assert.equal(b.spectrum(RAW), RAW, "反过来也粘住");
  });

  it("MAST 采的平均谱一来就接管面板，卫星的逐帧随后再来也不抢回去", () => {
    const b = bench();
    b.add(RAW);
    b.frame(RAW, 100_000);
    assert.equal(b.spectrum(null), RAW, "只有卫星的帧（人工采集、对准）");
    b.add(AVG); // AcquireSpectrum 第一次运行时声明，还没有平均谱
    assert.equal(b.spectrum(RAW), RAW, "声明了还没数据：不换");
    b.frame(AVG, 101_000);
    assert.equal(b.spectrum(RAW), AVG);
    b.frame(RAW, 101_500); // 下一个点的原始帧
    assert.equal(b.spectrum(AVG), AVG);
  });

  it("一打开页面：订阅时重放过来的旧平均谱不算正在来，不抢卫星的实时帧", () => {
    const b = bench();
    b.add(RAW);
    b.add(AVG, "spectrum", false, 4); // 目录：一小时前采过 4 次
    assert.equal(b.s.applyFrame(AVG, 4, { t: 1, y: [1, 2, 3], axis_ver: 0 }), "applied");
    b.frame(RAW, 100_000);
    assert.equal(b.spectrum(null), RAW);
    assert.equal(b.spectrum(RAW), RAW);
  });

  it("平均谱停了超过窗口、卫星的帧还在来：换回卫星的", () => {
    const b = bench();
    b.add(RAW);
    b.add(AVG);
    b.frame(AVG, 100_000);
    b.frame(RAW, 100_000 + FOLLOW_FRESH_MS - 1);
    assert.equal(b.spectrum(AVG, 100_000 + FOLLOW_FRESH_MS), AVG, "还在窗口里");
    b.frame(RAW, 100_000 + FOLLOW_FRESH_MS + 500);
    assert.equal(b.spectrum(AVG), RAW, "过了窗口");
  });

  it("都不在来数据：谁也不换", () => {
    const b = bench();
    b.add(RAW);
    b.add(AVG);
    b.frame(RAW, 100_000);
    b.frame(AVG, 100_500);
    const later = 100_500 + 10 * FOLLOW_FRESH_MS;
    assert.equal(b.spectrum(RAW, later), RAW);
    assert.equal(b.spectrum(null, later), AVG, "没有当前的：最近有数据的");
  });

  it("上一个 run 结束、新的 run 声明：立刻换过去，不等它的第一帧", () => {
    const b = bench();
    b.add("local/ters_map/run1", "map2d");
    b.frame("local/ters_map/run1", 100_000);
    assert.equal(b.map(null), "local/ters_map/run1");
    b.add("local/ters_map/run1", "map2d", true);
    b.add("local/ters_map/run2", "map2d");
    assert.equal(b.map("local/ters_map/run1"), "local/ters_map/run2");
  });

  it("名字对得上用途的流一出现，就不再显示同类的替补", () => {
    const b = bench();
    b.add("local/optics_scan/r9", "map2d");
    b.frame("local/optics_scan/r9", 100_000);
    assert.equal(b.map(null), "local/optics_scan/r9", "没有 TERS 图时用同类的");
    b.add("local/ters_map/run3", "map2d");
    assert.equal(b.map("local/optics_scan/r9"), "local/ters_map/run3");
    b.frame("local/optics_scan/r9", 100_500);
    assert.equal(b.map("local/ters_map/run3"), "local/ters_map/run3", "替补还在来数据也不回去");
  });

  it("当前的流从列表里没了 / 没有候选", () => {
    const b = bench();
    b.add(RAW);
    assert.equal(b.spectrum("gone/stream"), RAW);
    assert.equal(followLatest([], RAW, 0), null);
  });
});
