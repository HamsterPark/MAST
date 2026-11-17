// ════════════════════════════════════════════════════════════════════════════
// 数据图库的环境标签 — src/lib/gallery/env.ts（docs/v2/design/data_env_tags.md §6.3）
//
//     cd frontend && npm run test:unit
//
// 最要紧的三条：卡片按传感器自己的单位显示（kG 不悄悄换成 T）；过期值不进范围筛选；
// 3e-8 这样的真空度不被格式化成 0。
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ageText,
  envDetailUrl,
  envTagStatusLine,
  offsetText,
  parseMaxAge,
  windowChanged,
  envFacets,
  envLine,
  envMatches,
  envParts,
  envPresence,
  filterValue,
  fmt3,
  fmtExp,
  parseBound,
} from "../src/lib/gallery/env.ts";
import {
  DEFAULT_FILTERS,
  applyFilters,
  facets,
  parseStoredFilters,
  sanitizeFilters,
  type FilterContext,
  type Filters,
} from "../src/lib/gallery/filters.ts";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;
const mk = (id: string, env?: Any): Any => ({ id, k: "f", fn: id, d: "R/x", p: "", pf: "", ad: "", th: "", env });

const live = mk("live", {
  s: "r",
  T: { v: 4.312, u: "K", c: 4.312, s: "l" },
  B: { v: 20, u: "kG", c: 2, s: "l" },
  P: { v: 3.1e-8, u: "Pa", c: 3.1e-8, s: "l" },
  L: { v: 62, u: "%", c: 62, s: "h" },
});
const ramp = mk("ramp", { s: "r", B: { v: 20, u: "kG", c: 2, s: "l" }, wB: [0, 20], wr: ["B"] });
const hist = mk("hist", { s: "r", P: { v: 3.3e-8, u: "mbar", c: 3.3e-6, s: "h" } });
const stale = mk("stale", { s: "r", T: { v: 4.3, u: "K", c: 4.3, s: "o", a: 412 } });
const inf = mk("inf", { s: "i", T: { v: 77.4, u: "K", c: 77.4, s: "i" }, B: { v: -0.5, u: "T", c: -0.5, s: "i" } });
const none = mk("none");
const ALL = [live, ramp, hist, stale, inf, none];

describe("格式：原单位、有效数字、不出 0", () => {
  it("卡片一行：温度 · 磁场 · 真空（液位不上卡片）", () => {
    assert.equal(envLine(live), "4.31 K · 20.0 kG · 3.1e-8 Pa");
  });

  it("真空用科学计数，3e-8 不被格式化成 0", () => {
    assert.equal(fmtExp(3.1e-8), "3.1e-8");
    assert.equal(fmtExp(2.5e-10), "2.5e-10");
    assert.equal(fmtExp(0), "0");
  });

  it("三位有效数字、负号用 U+2212、大数不出科学计数", () => {
    assert.equal(fmt3(4.312), "4.31");
    assert.equal(fmt3(2), "2.00");
    assert.equal(fmt3(-0.5), "−0.500");
    assert.equal(fmt3(1234.4), "1234");
    assert.equal(fmt3(0), "0");
  });

  it("history 与推断的值带「≈」，过期值带早了多久", () => {
    assert.equal(envLine(hist), "≈3.3e-8 mbar");
    assert.equal(envLine(inf), "≈77.4 K · ≈−0.500 T");
    const [p] = envParts(stale);
    assert.equal(p.state, "o");
    assert.equal(p.stale, ageText(412));
    assert.match(p.title, /412|7/);
    assert.equal(envLine(stale), `4.30 K (${ageText(412)})`);
  });

  it("采集窗口里变了：显示 lo→hi（与值同一个单位）并标出来", () => {
    const [p] = envParts(ramp);
    assert.equal(p.text, "0→20.0 kG");
    assert.equal(p.changed, true);
    const flat = mk("flat", { s: "r", B: { v: 2, u: "T", c: 2, s: "l" }, wB: [1.9996, 2.0004], wr: ["B"] });
    assert.equal(envParts(flat)[0].changed, false);       // 显示精度下相同：不算变了
  });

  it("按 1 分钟桶算的窗口照样显示 lo→hi，但不标「采集期间变过」（桶比采集期间宽）", () => {
    const buckets = mk("buckets", { s: "r", B: { v: 20, u: "kG", c: 2, s: "h" }, wB: [0, 20] });
    const [p] = envParts(buckets);
    assert.equal(p.text, "≈0→20.0 kG");
    assert.equal(p.changed, false);
    assert.doesNotMatch(p.title, /采集期间变过|changed during/);
    const inferred = mk("inferred", { s: "i", T: { v: 4.4, u: "K", c: 4.4, s: "i" }, wT: [4.2, 4.6] });
    assert.equal(envParts(inferred)[0].changed, false);
  });

  it("温度、真空的起伏不当成「变了」：温度 > 2%、真空 > 2 倍才标", () => {
    assert.equal(windowChanged("T", 4.305, 4.315), false);
    assert.equal(windowChanged("T", 4.3, 4.6), true);
    assert.equal(windowChanged("P", 3.0e-8, 3.2e-8), false);
    assert.equal(windowChanged("P", 3.0e-8, 9.0e-8), true);
    assert.equal(windowChanged("B", 0, 0.001), true);      // 磁场按显示精度
    const noisy = mk("noisy", { s: "r", T: { v: 4.31, u: "K", c: 4.31, s: "l" }, wT: [4.305, 4.315],
                                P: { v: 3.1e-8, u: "Pa", c: 3.1e-8, s: "l" }, wP: [3.0e-8, 3.2e-8] });
    assert.equal(envLine(noisy), "4.31 K · 3.1e-8 Pa");
  });

  it("没有 env 就什么都不显示", () => {
    assert.deepEqual(envParts(none), []);
    assert.equal(envLine(none), "");
  });

  it("早了多久", () => {
    assert.equal(ageText(45), "45 秒");
    assert.equal(ageText(412), "7 分钟");
    assert.equal(ageText(7200), "2 小时");
    assert.equal(ageText(3 * 86400), "3 天");
  });
});

describe("筛选", () => {
  const ctx: FilterContext = { view: "all", mark: () => null, seriesOf: () => [] };
  const F = (o: Partial<Filters>): Filters => ({ ...DEFAULT_FILTERS, nodup: false, ...o });
  const ids = (xs: Any[]) => xs.map((x) => x.id);

  it("有无环境标签、保存时记录的、推断的", () => {
    assert.deepEqual(ids(applyFilters(ALL, F({ env: "any" }), ctx)), ["live", "ramp", "hist", "stale", "inf"]);
    assert.deepEqual(ids(applyFilters(ALL, F({ env: "rec" }), ctx)), ["live", "ramp", "hist", "stale"]);
    assert.deepEqual(ids(applyFilters(ALL, F({ env: "inf" }), ctx)), ["inf"]);
    assert.deepEqual(ids(applyFilters(ALL, F({ env: "none" }), ctx)), ["none"]);
    assert.equal(envPresence(none), "none");
  });

  it("温度范围按规范值 K；过期值不进范围筛选", () => {
    assert.deepEqual(ids(applyFilters(ALL, F({ tmin: 4, tmax: 5 }), ctx)), ["live"]);
    assert.equal(filterValue(stale, "T"), null);
    assert.deepEqual(ids(applyFilters(ALL, F({ tmin: 50 }), ctx)), ["inf"]);
  });

  it("磁场范围按规范值 T：显示 20.0 kG 的那张落在 1.5–2.5 T 里", () => {
    assert.deepEqual(ids(applyFilters(ALL, F({ bmin: 1.5, bmax: 2.5 }), ctx)), ["live", "ramp"]);
    assert.deepEqual(ids(applyFilters(ALL, F({ bmax: 0 }), ctx)), ["inf"]);   // 带符号
  });

  it("没设范围时，没有这个量的条目照样显示", () => {
    assert.equal(applyFilters(ALL, F({}), ctx).length, ALL.length);
    assert.ok(envMatches(none, F({})));
  });

  it("facets 决定控件出不出现；没有的量的范围被清掉", () => {
    assert.deepEqual(envFacets(ALL), { any: true, T: true, B: true });
    assert.deepEqual(envFacets([none, hist]), { any: true, T: false, B: false });
    const fac = facets([none]);
    const out = sanitizeFilters(F({ env: "rec", tmin: 4, bmax: 1 }), fac, []);
    assert.deepEqual([out.env, out.tmin, out.bmax], ["", null, null]);
    const keepNone = sanitizeFilters(F({ env: "none" }), fac, []);
    assert.equal(keepNone.env, "none");
  });

  it("读回来的环境筛选逐项校验", () => {
    const back = parseStoredFilters(JSON.stringify({ env: "inf", tmin: 4, tmax: "x", bmin: null, bmax: 2.5 }));
    assert.deepEqual([back.env, back.tmin, back.tmax, back.bmin, back.bmax], ["inf", 4, null, null, 2.5]);
    assert.equal(parseStoredFilters(JSON.stringify({ env: "zzz" })).env, "");
  });

  it("输入框里的数", () => {
    assert.equal(parseBound(" 4.2 "), 4.2);
    assert.equal(parseBound("−1"), -1);
    assert.equal(parseBound(""), null);
    assert.equal(parseBound("abc"), null);
  });
});

describe("详情", () => {
  it("URL 逐段编码、斜杠保留（后端是 {path:path}）", () => {
    assert.equal(
      envDetailUrl("SPM/2026/1112/Grid Spectroscopy#1.3ds"),
      "/api/gallery/env/SPM/2026/1112/Grid%20Spectroscopy%231.3ds",
    );
  });

  it("读数时刻相对保存：近的写秒，过期的写早了多久", () => {
    assert.equal(offsetText(-1.234), "−1.2 s");
    assert.equal(offsetText(0.5), "+0.5 s");
    assert.equal(offsetText(-412, false), `早于保存 ${ageText(412)}`);
    assert.equal(offsetText(null), "");
    assert.equal(offsetText(0, true, true), "保存时刻在桶内");
  });
});

describe("设置页：新鲜度阈值与计数行", () => {
  it("清空阈值是「不改」，不是 0；不是数就不保存；是数就夹到 5–600", () => {
    assert.deepEqual(parseMaxAge(""), { value: null });
    assert.deepEqual(parseMaxAge("   "), { value: null });
    assert.equal(parseMaxAge("abc"), null);
    assert.equal(parseMaxAge("30s"), null);
    assert.deepEqual(parseMaxAge("0"), { value: 5 });
    assert.deepEqual(parseMaxAge(" 90 "), { value: 90 });
    assert.deepEqual(parseMaxAge("1e4"), { value: 600 });
  });

  it("计数行里看得见丢弃和失败，有数时拆开", () => {
    const base = { written: 3, superseded: 1, reused: 2, same: 0, cooldown_skipped: 1, failed_write: 1,
                   empty: 0, pending: 4, cooling_dirs: 0 };
    assert.equal(
      envTagStatusLine({ ...base, dropped_full: 0, dropped_disabled: 0, central_failed: 0, errors: 0 }),
      "本次运行：写成 4 · 副本补写 2 · 已有不动 0 · 只写中央记录 2 · 当时什么都不知道 0 · 丢弃 0 · 失败 0 · 待办 4",
    );
    const line = envTagStatusLine({ ...base, dropped_full: 2, dropped_disabled: 1, central_failed: 1, errors: 1,
                                    cooling_dirs: 1 });
    assert.match(line, /丢弃 3（队列满 2 · 关着时 1）/);
    assert.match(line, /失败 2（中央记录 1 · 出错 1）/);
    assert.match(line, /写不进的目录 1$/);
    assert.match(envTagStatusLine({}), /写成 0 .* 丢弃 0 · 失败 0 · 待办 0$/);      // 缺字段按 0
  });
});
