// ════════════════════════════════════════════════════════════════════════════
// 缓存 → 画图数组（src/lib/live/views.ts）以及它用到的换算、色表、色标。
//
//     cd frontend && npm run test:unit
//
// 钉的是「画出来的是不是测到的」：轴版本对不上不能硬用、没有激发线不能换算 cm⁻¹、
// null 不能变成 0、map2d 的上下左右跟着物理坐标走。
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { normalizeStreamMeta, map2dGeometry } from "../src/lib/live/protocol.ts";
import { LiveStore } from "../src/lib/live/store.ts";
import {
  canvasCellToPixel,
  magnitudeSpread,
  mapExtent,
  pixelToCanvasCell,
  scalarView,
  seriesLatest,
  spectrumView,
  valueUnit,
  waterfallRows,
  xyView,
} from "../src/lib/live/views.ts";
import {
  ascending,
  convertAxis,
  laserLineOf,
  ramanShiftCm1,
  validLaserNm,
  wavelengthFromShift,
} from "../src/lib/live/spectrumAxis.ts";
import { colormapLut, lutIndex } from "../src/lib/live/colormap.ts";
import {
  finiteRange,
  fmtTick,
  fmtValue,
  niceTicks,
  percentileRange,
  symmetricRange,
  widen,
} from "../src/lib/live/scale.ts";

function spectrumStore(laser?: number) {
  const s = new LiveStore();
  s.applyStreams(
    [normalizeStreamMeta({ id: "sp", kind: "spectrum", meta: laser ? { laser_line_nm: laser } : {} })!],
    { full: true, trustLastSeq: false },
  );
  return s;
}

const opts = { unit: "nm" as const, overlay: 0, uptoSeq: null };

describe("光谱横轴换算", () => {
  it("Si 520.7 cm⁻¹ 在 532 nm 激发下约 547.16 nm，来回换算一致", () => {
    const lam = wavelengthFromShift(520.7, 532);
    assert.ok(Math.abs(lam - 547.157) < 0.01, `got ${lam}`);
    assert.ok(Math.abs(ramanShiftCm1(lam, 532) - 520.7) < 1e-9);
    assert.ok(ramanShiftCm1(520, 532) < 0, "反斯托克斯侧为负");
  });

  it("不合理的激发线当没有；帧 meta 优先于流 meta", () => {
    assert.equal(validLaserNm(0), null);
    assert.equal(validLaserNm("632.8"), 632.8);
    assert.equal(validLaserNm(Number.NaN), null);
    assert.equal(laserLineOf({ laser_line_nm: 532 }, { laser_line_nm: 633 }), 532);
    assert.equal(laserLineOf(null, { laser_line_nm: 633 }), 633);
    assert.equal(laserLineOf(null, {}), null);
  });

  it("没有激发线就不换算", () => {
    assert.equal(convertAxis(new Float64Array([600]), "cm-1", null), null);
  });

  it("降序轴连同 y 一起翻成升序", () => {
    const r = ascending(new Float64Array([3, 2, 1]), [new Float64Array([30, 20, 10])]);
    assert.equal(r.reversed, true);
    assert.deepEqual([...r.x], [1, 2, 3]);
    assert.deepEqual([...r.ys[0]!], [10, 20, 30]);
  });
});

describe("spectrumView", () => {
  it("轴到了且版本对得上：按 nm 画；可以换成 cm⁻¹", () => {
    const s = spectrumStore(633);
    s.applyAxis("sp", 1, new Float64Array([640, 650, 660]), "nm");
    s.applyFrame("sp", 1, { t: 1, y: [1, 2, 3], axis_ver: 1 });
    const v = spectrumView(s.get("sp")!, opts)!;
    assert.equal(v.xUnit, "nm");
    assert.equal(v.axisNote, null);
    const c = spectrumView(s.get("sp")!, { ...opts, unit: "cm-1" })!;
    assert.equal(c.xUnit, "cm-1");
    assert.ok(Math.abs(c.x[0]! - ramanShiftCm1(640, 633)) < 1e-9);
  });

  it("要 cm⁻¹ 但没有激发线：仍按 nm，并且说出来", () => {
    const s = spectrumStore();
    s.applyAxis("sp", 1, new Float64Array([640, 650]), "nm");
    s.applyFrame("sp", 1, { t: 1, y: [1, 2], axis_ver: 1 });
    const v = spectrumView(s.get("sp")!, { ...opts, unit: "cm-1" })!;
    assert.equal(v.xUnit, "nm");
    assert.match(v.axisNote ?? "", /激发线/);
  });

  it("帧指向的轴版本不在手里：按像素号画并说明 —— 绝不拿别的版本的轴硬套", () => {
    const s = spectrumStore(633);
    s.applyAxis("sp", 1, new Float64Array([640, 650, 660]), "nm");
    s.applyFrame("sp", 1, { t: 1, y: [1, 2, 3], axis_ver: 2 });
    const v = spectrumView(s.get("sp")!, opts)!;
    assert.equal(v.xUnit, "px");
    assert.deepEqual([...v.x], [0, 1, 2]);
    assert.match(v.axisNote ?? "", /版本 2/);
  });

  it("axis_ver = 0 是「没有轴」：之后才到的轴不能拿来套它", () => {
    const s = spectrumStore(633);
    s.applyFrame("sp", 1, { t: 1, y: [1, 2, 3], axis_ver: 0 });
    s.applyAxis("sp", 1, new Float64Array([640, 650, 660]), "nm");
    const v = spectrumView(s.get("sp")!, opts)!;
    assert.equal(v.xUnit, "px");
    assert.match(v.axisNote ?? "", /没有波长轴/);
  });

  it("历史叠加只叠同一版轴、同长度的谱；暂停冻结在给定 seq", () => {
    const s = spectrumStore(633);
    s.applyAxis("sp", 1, new Float64Array([1, 2]), "nm");
    s.applyAxis("sp", 2, new Float64Array([3, 4]), "nm");
    s.applyFrame("sp", 1, { t: 1, y: [1, 1], axis_ver: 1 });
    s.applyFrame("sp", 2, { t: 2, y: [2, 2], axis_ver: 2 });
    s.applyFrame("sp", 3, { t: 3, y: [3, 3], axis_ver: 2 });
    s.applyFrame("sp", 4, { t: 4, y: [4, 4, 4], axis_ver: 2 });
    const v = spectrumView(s.get("sp")!, { ...opts, overlay: 5, uptoSeq: 3 })!;
    assert.deepEqual(v.seqs, [3, 2], "seq 1 换过轴、seq 4 在暂停点之后");
    assert.equal(v.latest.seq, 3);
    assert.deepEqual(waterfallRows(s.get("sp")!, 10, 3).map((r) => r.seq), [3, 2]);
  });
});

describe("scalarView / xyView", () => {
  it("时间窗只留最近的；没有时间戳的点跳过并计数", () => {
    const s = new LiveStore();
    s.applyStreams([normalizeStreamMeta({ id: "a", kind: "scalar" })!], { full: true, trustLastSeq: false });
    s.applyFrame("a", 1, { t: 100, values: { v: 1 } });
    s.applyFrame("a", 2, { t: 150, values: { v: null } });
    s.applyFrame("a", 3, { values: { v: 3 } });
    s.applyFrame("a", 4, { t: 160, values: { v: 4 } });
    const b = s.get("a")!.buf;
    if (b.kind !== "scalar") throw new Error("kind");
    const v = scalarView(b, 30);
    assert.deepEqual(v.data[0], [150, 160]);
    assert.deepEqual(v.data[1], [null, 4], "null 保留为 null，不是 0");
    assert.equal(v.skipped, 1);
    assert.equal(scalarView(b, null).data[0]!.length, 3);
  });

  it("xy 按 x 升序，x 相同按采集顺序", () => {
    const s = new LiveStore();
    s.applyStreams([normalizeStreamMeta({ id: "p", kind: "xy" })!], { full: true, trustLastSeq: false });
    s.applyFrame("p", 1, { t: 1, x: 5, values: { a: 50 } });
    s.applyFrame("p", 2, { t: 2, x: 1, values: { a: 10, b: 1 } });
    s.applyFrame("p", 3, { t: 3, x: 5, values: { a: 51 } });
    const b = s.get("p")!.buf;
    if (b.kind !== "xy") throw new Error("kind");
    const v = xyView(b);
    assert.deepEqual(v.data[0], [1, 5, 5]);
    assert.deepEqual(v.data[1], [10, 50, 51]);
    assert.deepEqual(v.data[2], [1, null, null], "没有这个通道的点是 null");
  });
});

describe("多通道：最新读数与量级差", () => {
  it("泵浦-探测：100 pA 的电流和几个 fA 的锁相差上万倍 —— 该分开画", () => {
    const s = new LiveStore();
    s.applyStreams([normalizeStreamMeta({ id: "pp", kind: "xy" })!], { full: true, trustLastSeq: false });
    s.applyFrame("pp", 1, { t: 1, x: 0, values: { current_a: 1e-10, lockin_x: 3e-15 } });
    s.applyFrame("pp", 2, { t: 2, x: 1, values: { current_a: 1.02e-10, lockin_x: -5e-15 } });
    const b = s.get("pp")!.buf;
    if (b.kind !== "xy") throw new Error("kind");
    assert.ok(magnitudeSpread(b, ["current_a", "lockin_x"]) > 1e4);
    assert.equal(magnitudeSpread(b, ["current_a"]), 1, "一个通道无所谓分不分开");
    assert.deepEqual(seriesLatest(b), { n: 2, x: 1, values: { current_a: 1.02e-10, lockin_x: -5e-15 } });
  });

  it("XYZ 三个坐标量级相近 —— 合并画", () => {
    const s = new LiveStore();
    s.applyStreams([normalizeStreamMeta({ id: "xyz", kind: "scalar" })!], { full: true, trustLastSeq: false });
    s.applyFrame("xyz", 1, { t: 1, values: { x_mm: 5, y_mm: 2.5, z_mm: 4.8 } });
    const b = s.get("xyz")!.buf;
    if (b.kind !== "scalar") throw new Error("kind");
    assert.ok(magnitudeSpread(b, b.channels) < 3);
    assert.deepEqual(seriesLatest(b).values, { x_mm: 5, y_mm: 2.5, z_mm: 4.8 });
  });

  it("值的单位：通道单位 > value_unit > y_unit；map2d 绝不拿 y 轴的单位当值的单位", () => {
    const xy = normalizeStreamMeta({ id: "a", kind: "xy", y_unit: "A", meta: { units: { lockin_v: "V" } } })!;
    assert.equal(valueUnit(xy, "lockin_v"), "V");
    // devices 的扫描流：units 只列单位固定的列，sig<N>_mean 的单位看信号槽接了什么 —— 不知道就不写
    assert.equal(valueUnit(xy, "sig3_mean"), null, "给了 units 表、表里没有：不退到 y_unit");
    const sat = normalizeStreamMeta({ id: "s", kind: "scalar", y_unit: "deg" })!;
    assert.equal(valueUnit(sat, "position_deg"), "deg", "没有 units 表（卫星转发的轴）：用 y_unit");
    const map = normalizeStreamMeta({ id: "m", kind: "map2d", y_unit: "nm", meta: { nx: 1, ny: 1 } })!;
    assert.equal(valueUnit(map, "intensity"), null, "y_unit 是 y 轴的单位");
    const map2 = normalizeStreamMeta({ id: "m", kind: "map2d", y_unit: "nm", meta: { value_unit: "counts" } })!;
    assert.equal(valueUnit(map2, "intensity"), "counts");
  });

  it("空缓冲：0 点，读数是 null", () => {
    const s = new LiveStore();
    s.applyStreams([normalizeStreamMeta({ id: "e", kind: "scalar" })!], { full: true, trustLastSeq: false });
    const b = s.get("e")!.buf;
    if (b.kind !== "scalar") throw new Error("kind");
    assert.deepEqual(seriesLatest(b), { n: 0, x: null, values: {} });
  });
});

describe("map2d 方向", () => {
  const g = (dx: number, dy: number) =>
    map2dGeometry(normalizeStreamMeta({ id: "m", kind: "map2d", meta: { nx: 3, ny: 2, x0: 0, y0: 0, dx, dy } })!)!;

  it("dy > 0：iy = 0 在最下面（y 向上增大）", () => {
    assert.deepEqual(pixelToCanvasCell(g(1, 1), 0, 0), { cx: 0, cy: 1 });
    assert.deepEqual(pixelToCanvasCell(g(1, 1), 2, 1), { cx: 2, cy: 0 });
  });

  it("dy < 0：iy = 0 在最上面；dx < 0：ix = 0 在最右边", () => {
    assert.deepEqual(pixelToCanvasCell(g(1, -1), 0, 0), { cx: 0, cy: 0 });
    assert.deepEqual(pixelToCanvasCell(g(-1, 1), 0, 0), { cx: 2, cy: 1 });
  });

  it("画布格 → 像素是逆运算", () => {
    for (const geom of [g(1, 1), g(-1, 1), g(1, -1), g(-1, -1)]) {
      for (let ix = 0; ix < 3; ix++) {
        for (let iy = 0; iy < 2; iy++) {
          const c = pixelToCanvasCell(geom, ix, iy);
          assert.deepEqual(canvasCellToPixel(geom, c.cx, c.cy), { ix, iy });
        }
      }
    }
  });

  it("物理范围按像素边缘算", () => {
    assert.deepEqual(mapExtent(g(1, -1)), { xMin: -0.5, xMax: 2.5, yMin: -1.5, yMax: 0.5 });
  });
});

describe("色表与色标", () => {
  it("查找表两端就是首尾控制点；NaN 是「没有数据」", () => {
    const lut = colormapLut("viridis");
    assert.deepEqual([...lut.slice(0, 3)], [0x44, 0x01, 0x54]);
    assert.deepEqual([...lut.slice(765, 768)], [0xfd, 0xe7, 0x25]);
    assert.equal(lutIndex(Number.NaN, 0, 1), -1);
    assert.equal(lutIndex(-5, 0, 1), 0);
    assert.equal(lutIndex(5, 0, 1), 255);
    assert.equal(lutIndex(0.5, 0, 1), 128);
  });

  it("分位数色标不被一个尖峰撑开；min/max 照实报", () => {
    const a = new Float64Array(1000);
    for (let i = 0; i < a.length; i++) a[i] = i % 10;
    a[500] = 1e6; // 宇宙线
    a[501] = Number.NaN;
    const p = percentileRange(a)!;
    assert.ok(p.max < 100, `分位数上界 ${p.max} 被尖峰带跑了`);
    assert.equal(finiteRange(a)!.max, 1e6);
  });

  it("逐行构建的图：抽样全落在空像素上时退回全量扫描", () => {
    const a = new Float64Array(100_000).fill(Number.NaN);
    a[3] = 2;
    a[7] = 4;
    const p = percentileRange(a, 0, 1, 10)!;
    assert.deepEqual(p, { min: 2, max: 4 });
  });

  it("退化范围撑开，对称范围以 0 为中心", () => {
    assert.deepEqual(widen({ min: 5, max: 5 }), { min: 4.95, max: 5.05 });
    assert.deepEqual(widen({ min: 0, max: 0 }), { min: -1, max: 1 });
    assert.deepEqual(symmetricRange({ min: -2, max: 5 }), { min: -5, max: 5 });
  });

  it("刻度与数字格式", () => {
    assert.deepEqual(niceTicks(0, 1, 5), [0, 0.2, 0.4, 0.6, 0.8, 1]);
    assert.deepEqual(niceTicks(-3, 7, 2), [0, 5]);
    assert.equal(fmtTick(0.2, 0.2), "0.2");
    assert.equal(fmtTick(1234567, 1e6), "1.2e+6");
    assert.equal(fmtValue(null), "—");
    assert.equal(fmtValue(1.234567), "1.2346");
    assert.equal(fmtValue(1e-9), "1.000e-9");
  });
});
