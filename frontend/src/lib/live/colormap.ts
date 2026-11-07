// 伪彩色查找表（瀑布图与 map2d 热图用）。
//
// 画在 <canvas> 上，没有任何依赖，所以表在这里：每张表是几个控制点，线性插值成
// 256 级。viridis / inferno 的控制点取自 matplotlib 同名色表的 9 等分采样；感知上
// 单调、灰度打印也单调 —— 用彩虹表（jet）画强度会在本来平滑的地方造出边界。
//
// 发散表 rdbu 给有正负号的量（锁相 X、差分图）：0 落在中间的白色上，前提是色标范围
// 关于 0 对称 —— 那一步在调用方（symmetricRange）。

import { tr } from "../i18n.ts";

export const COLORMAPS = ["viridis", "inferno", "gray", "rdbu"] as const;
export type ColormapName = (typeof COLORMAPS)[number];

const STOPS: Record<ColormapName, string[]> = {
  viridis: ["#440154", "#472d7b", "#3b528b", "#2c728e", "#21908c", "#27ad81", "#5dc863", "#aadc32", "#fde725"],
  inferno: ["#000004", "#1f0c48", "#550f6d", "#88226a", "#ba3655", "#e35932", "#f98c0a", "#f9c932", "#fcffa4"],
  gray: ["#000000", "#ffffff"],
  rdbu: ["#2166ac", "#67a9cf", "#f7f7f7", "#ef8a62", "#b2182b"],
};

export const COLORMAP_LABELS: Record<ColormapName, string> = {
  viridis: "viridis",
  inferno: "inferno",
  gray: tr("灰度", "Grayscale"),
  rdbu: tr("红蓝（有正负）", "Red-blue (signed)"),
};

function hexRgb(h: string): [number, number, number] {
  const n = Number.parseInt(h.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

const cache = new Map<ColormapName, Uint8ClampedArray>();

/** 256 级 RGB 表：lut[3*i .. 3*i+2]。 */
export function colormapLut(name: ColormapName): Uint8ClampedArray {
  const hit = cache.get(name);
  if (hit) return hit;
  const stops = (STOPS[name] ?? STOPS.viridis).map(hexRgb);
  const lut = new Uint8ClampedArray(256 * 3);
  const segs = stops.length - 1;
  for (let i = 0; i < 256; i++) {
    const p = (i / 255) * segs;
    const k = Math.min(segs - 1, Math.floor(p));
    const f = p - k;
    const a = stops[k]!;
    const b = stops[k + 1]!;
    lut[3 * i] = Math.round(a[0] + (b[0] - a[0]) * f);
    lut[3 * i + 1] = Math.round(a[1] + (b[1] - a[1]) * f);
    lut[3 * i + 2] = Math.round(a[2] + (b[2] - a[2]) * f);
  }
  cache.set(name, lut);
  return lut;
}

/** 值 → 查找表下标（0..255）。超出范围的夹到两端；非有限值返回 -1（「没有数据」）。 */
export function lutIndex(v: number, min: number, max: number): number {
  if (!Number.isFinite(v)) return -1;
  if (!(max > min)) return 128;
  const f = (v - min) / (max - min);
  if (f <= 0) return 0;
  if (f >= 1) return 255;
  return Math.round(f * 255);
}

/** CSS 渐变（色标条用）。 */
export function colormapGradient(name: ColormapName, direction = "to top"): string {
  const stops = STOPS[name] ?? STOPS.viridis;
  return `linear-gradient(${direction}, ${stops.join(", ")})`;
}

export function isColormap(v: string): v is ColormapName {
  return (COLORMAPS as readonly string[]).includes(v);
}
