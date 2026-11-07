// 光谱横轴：nm ↔ 拉曼位移 cm⁻¹。
//
// 卫星推来的波长轴是绝对波长（nm）。拉曼位移是相对于激发线的波数差：
//
//     Δν̃ [cm⁻¹] = 10⁷/λ₀[nm] − 10⁷/λ[nm]
//
// λ > λ₀（斯托克斯侧）为正，λ < λ₀（反斯托克斯侧）为负；λ 增大时 Δν̃ 单调增大，所以
// 一条升序的 nm 轴换算后仍是升序，uPlot 不需要重排。
//
// 激发线从哪来：契约 §5.4 的 status 和 wavelength_axis 都带 laser_line_nm；到了
// LiveHub 这一层它在流的 meta（declare 时给）或帧的 meta 里。**两处都没有就不提供
// cm⁻¹** —— 拿一个默认的 633 去换算，会让一张 532 nm 激发的谱整体平移几千个波数，
// 而且看起来完全正常。

import { tr } from "../i18n.ts";

export type XUnit = "nm" | "cm-1";

export function ramanShiftCm1(lambdaNm: number, laserNm: number): number {
  return 1e7 / laserNm - 1e7 / lambdaNm;
}

export function wavelengthFromShift(shiftCm1: number, laserNm: number): number {
  return 1e7 / (1e7 / laserNm - shiftCm1);
}

/** 合理的激发线（nm）。不合理（非数、≤0、超出 100–20000 nm）一律当没有。 */
export function validLaserNm(v: unknown): number | null {
  const n = typeof v === "number" ? v : typeof v === "string" ? Number.parseFloat(v) : Number.NaN;
  return Number.isFinite(n) && n >= 100 && n <= 20_000 ? n : null;
}

/** 帧 meta 优先（它跟着这一帧走），其次流的 meta。 */
export function laserLineOf(
  frameMeta: Record<string, unknown> | null | undefined,
  streamMeta: Record<string, unknown> | null | undefined,
): number | null {
  return validLaserNm(frameMeta?.laser_line_nm) ?? validLaserNm(streamMeta?.laser_line_nm);
}

/** 这个轴单位是不是绝对波长（只有它才能换算成拉曼位移）。 */
export function isNmUnit(unit: string | null | undefined): boolean {
  const u = (unit ?? "").trim().toLowerCase();
  return u === "nm" || u === "nanometer" || u === "nanometers";
}

/** nm 轴 → 目标单位。换不了（没有激发线、轴不是 nm）返回 null。 */
export function convertAxis(xNm: Float64Array, to: XUnit, laserNm: number | null): Float64Array | null {
  if (to === "nm") return xNm;
  if (laserNm === null) return null;
  const out = new Float64Array(xNm.length);
  for (let i = 0; i < xNm.length; i++) out[i] = ramanShiftCm1(xNm[i]!, laserNm);
  return out;
}

/**
 * uPlot 要求 x 升序。有的光谱仪给的是降序轴：把 x 和每条 y 一起反过来。
 * 已经升序时原样返回（不复制）。
 */
export function ascending(
  x: Float64Array,
  ys: Float64Array[],
): { x: Float64Array; ys: Float64Array[]; reversed: boolean } {
  if (x.length < 2 || x[0]! <= x[x.length - 1]!) return { x, ys, reversed: false };
  const rev = (a: Float64Array) => {
    const o = new Float64Array(a.length);
    for (let i = 0; i < a.length; i++) o[i] = a[a.length - 1 - i]!;
    return o;
  };
  return { x: rev(x), ys: ys.map(rev), reversed: true };
}

export function unitLabel(u: XUnit): string {
  return u === "nm" ? tr("波长 (nm)", "Wavelength (nm)") : tr("拉曼位移 (cm⁻¹)", "Raman shift (cm⁻¹)");
}
