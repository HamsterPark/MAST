// 色标范围与刻度。
//
// 默认用 2%–98% 分位数而不是 min/max：TERS 图上一个宇宙线尖峰、map2d 上一个坏像素
// 就能把 min/max 撑到整幅图只剩两种颜色。但分位数**会把真实的极值夹掉** —— 所以
// 界面上同时给出真实的 min/max（不被色标影响），并且可以切回 min/max。
//
// 分位数在大图上按步长抽样（默认最多 8192 个点）：262144 像素每帧排序一次是浪费，
// 而色标范围本来就不需要那个精度。

export interface Range {
  min: number;
  max: number;
}

/** 有限值的 min/max；一个都没有返回 null。 */
export function finiteRange(arr: ArrayLike<number>): Range | null {
  let min = Infinity;
  let max = -Infinity;
  for (let i = 0; i < arr.length; i++) {
    const v = arr[i]!;
    if (!Number.isFinite(v)) continue;
    if (v < min) min = v;
    if (v > max) max = v;
  }
  return min <= max ? { min, max } : null;
}

/** 有限值的分位数范围（抽样）。退化（全相等）时向两边撑开一点，免得色标除以零。 */
export function percentileRange(
  arr: ArrayLike<number>,
  lo = 0.02,
  hi = 0.98,
  maxSamples = 8192,
): Range | null {
  const n = arr.length;
  if (!n) return null;
  const stride = Math.max(1, Math.floor(n / maxSamples));
  const sample: number[] = [];
  for (let i = 0; i < n; i += stride) {
    const v = arr[i]!;
    if (Number.isFinite(v)) sample.push(v);
  }
  // 抽样恰好全落在空像素上（逐行构建的图开头几行）：退回全量扫描
  if (!sample.length && stride > 1) {
    for (let i = 0; i < n; i++) {
      const v = arr[i]!;
      if (Number.isFinite(v)) sample.push(v);
    }
  }
  if (!sample.length) return null;
  sample.sort((a, b) => a - b);
  const pick = (q: number) => sample[Math.min(sample.length - 1, Math.max(0, Math.round(q * (sample.length - 1))))]!;
  return widen({ min: pick(lo), max: pick(hi) });
}

/** min == max 时撑开（色标需要一个非零宽度）。 */
export function widen(r: Range): Range {
  if (r.max > r.min) return r;
  const d = r.min === 0 ? 1 : Math.abs(r.min) * 0.01;
  return { min: r.min - d, max: r.max + d };
}

/** 关于 0 对称的范围（发散色表用，让 0 落在中间）。 */
export function symmetricRange(r: Range): Range {
  const m = Math.max(Math.abs(r.min), Math.abs(r.max)) || 1;
  return { min: -m, max: m };
}

/** 「好看的」刻度：1/2/5 × 10^k 步长，落在 [min, max] 内。 */
export function niceTicks(min: number, max: number, count = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || count < 1) return [];
  if (max < min) [min, max] = [max, min];
  if (max === min) return [min];
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const out: number[] = [];
  const start = Math.ceil(min / step) * step;
  for (let v = start; v <= max + step * 1e-9; v += step) {
    // 抹掉 0.30000000000000004 这类浮点尾巴
    out.push(Number.parseFloat(v.toPrecision(12)));
    if (out.length > 50) break;
  }
  return out;
}

/** 刻度文字：按步长定小数位，极大/极小用指数。 */
export function fmtTick(v: number, step: number): string {
  if (!Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  if (v !== 0 && (a >= 1e5 || a < 1e-3)) return v.toExponential(1);
  const decimals = step > 0 ? Math.max(0, Math.min(6, -Math.floor(Math.log10(step)))) : 2;
  return v.toFixed(decimals);
}

/** 一个读数的文字（不带单位前缀换算）：有效数字 4 位，极端值用指数。 */
export function fmtValue(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  if (v !== 0 && (a >= 1e6 || a < 1e-3)) return v.toExponential(3);
  return Number.parseFloat(v.toPrecision(5)).toString();
}
