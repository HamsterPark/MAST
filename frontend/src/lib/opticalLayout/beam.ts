// Beam physics of the optical layout: power, polarisation (Stokes / Mueller) and Gaussian beams
// (the complex q parameter), docs/v2/design/optical_layout_v2.md sections 4 and 5.
//
// Two kinds of functions live here, all pure:
//   * the primitives the tracer (trace.ts) applies at every hit: the Mueller matrices of section
//     4.5, the axis projection, the q transforms of sections 4.3 and 4.6;
//   * what the editor reads off a trace: the polarisation text of section 5 (the same thresholds as
//     the server's summary), the polarisation ellipse, the beam size anywhere along a segment and
//     where a waist is, the beam profile along a branch for the inspector chart, and the beam checks
//     of section 5 (spot past the aperture, light extinguished, waveplate without retardance).
//
// Conventions (section 4.1): for a ray travelling along d in the table plane, e_h = z × d (in the
// table plane, to the LEFT of the direction of travel) and e_v = z (straight up). Every
// polarisation angle is measured from e_h towards e_v: 0° is in the table plane (p for an in-plane
// reflection), 90° is vertical (s). The Stokes vector is (S0, S1, S2, S3) with S0 the power
// relative to the source of the tree, S1 = I_h - I_v, S2 = I_+45 - I_-45; the sign of S3 is a
// convention only, and no handedness is named for it.
//
// Units: lengths mm, wavelengths nm (as the trace has them; λ in mm = nm × 1e-6), angles degrees in
// the API and radians inside.

import { tr } from "../i18n.ts";
import { pyRound } from "./geometry.ts";
import type { LayoutComponent, OpticalLayout, QParam, Segment, StokesNorm } from "./types.ts";
import type { DetailedBranch, DetailedEvent, DetailedSourceTrace, DetailedTrace } from "./trace.ts";
import { usesHeights } from "./heights.ts";

// ── thresholds (sections 4.6 and 5) ───────────────────────────────────────────

/** A relative power below this is drawn as "no light" (a thin grey dotted line, section 8.2). */
export const NO_LIGHT_P = 1e-6;
/** Section 5: an action after which S0 < 1e-6 while it arrived with S0 >= 1e-6 extinguishes the light. */
export const EXTINCTION_P = 1e-6;
/**
 * Darkness is exact (section 12, the server's EPS_DARK): an output of a polariser or of a PBS port whose power
 * k is not above EPS_DARK times the arriving S0 is exactly (0, 0, 0, 0). Crossed polarisers leave a residue of
 * about 1e-16 in floating point, sometimes negative, and its sign and size depend on the last bit of a cosine,
 * which Python and JavaScript do not compute alike; without this, s = S / S0 and a PBS's fractions behind it
 * would be noise divided by noise. With it, "no light" is S0 = 0 exactly: s is null and a PBS splits 50 / 50
 * (for the geometry only) when, and only when, S0 = 0.
 */
export const EPS_DARK = 1e-12;
/** Section 5: a degree of polarisation below this reads as unpolarised. */
export const UNPOLARIZED_DOP = 0.01;
/** Section 5: below this the degree of polarisation is written out. */
export const FULL_DOP = 0.99;
/** Section 5: |χ| below this (degrees) is linear polarisation. */
export const LINEAR_CHI_DEG = 1;
/** Section 5: |χ| above this (degrees) is circular polarisation. */
export const CIRCULAR_CHI_DEG = 44;
/** Section 5: linear within this many degrees of 0 / 180 is p, of 90 is s. */
export const PS_DEG = 1;
/** Section 5: no spot check at grazing incidence (degrees): the footprint means nothing there. */
export const GRAZING_DEG = 89;
/** Section 5: the aperture test of a spot has the tolerance of the hit test. */
export const SPOT_EPS = 1e-9;

const DEG = Math.PI / 180;

// ── Stokes vectors and Mueller matrices (section 4.5) ─────────────────────────

/** A Stokes vector (S0, S1, S2, S3), not normalised: S0 is the relative power. */
export type Stokes = [number, number, number, number];

/** Unpolarised light of relative power 1. */
export const UNPOLARIZED: Stokes = [1, 0, 0, 0];

/** Linear polarisation at `alphaDeg` (from e_h towards e_v), relative power 1: (1, cos 2α, sin 2α, 0). */
export function linearStokes(alphaDeg: number): Stokes {
  const a2 = 2 * (alphaDeg * DEG);
  return [1, Math.cos(a2), Math.sin(a2), 0];
}

export function scaleStokes(s: Stokes, k: number): Stokes {
  return [k * s[0], k * s[1], k * s[2], k * s[3]];
}

/** Any reflection in the table plane, an ideal conductor: diag(1, 1, -1, -1) (section 4.5 derives it). 0 - x, not -x: no -0. */
export function mirrorStokes(s: Stokes): Stokes {
  return [s[0], s[1], 0 - s[2], 0 - s[3]];
}

/** The ideal polariser P(θ) with its transmission axis at θ (radians) in the ray's own frame. */
export function polarizerStokes(s: Stokes, thetaRad: number): Stokes {
  return polarizerCS(s, Math.cos(2 * thetaRad), Math.sin(2 * thetaRad));
}

/**
 * P(θ) with c = cos 2θ and s = sin 2θ given: ½ [[1, c, s, 0], [c, c², cs, 0], [s, cs, s², 0], [0, 0, 0, 0]], that is
 * k (1, c, s, 0) with k = (S0 + c S1 + s S2) / 2; exactly dark when k <= EPS_DARK S0.
 */
export function polarizerCS(v: Stokes, c: number, s: number): Stokes {
  const k = (v[0] + c * v[1] + s * v[2]) / 2;
  if (k <= EPS_DARK * v[0]) return [0, 0, 0, 0];
  return [k, k * c, k * s, 0];
}

/** The polariser of a PBS's transmitted port: P(0), exact (c = 1, s = 0). */
export function pbsTransmitted(v: Stokes): Stokes {
  return polarizerCS(v, 1, 0);
}

/** The reflected port of a PBS: R_m · P(90°), exact (c = -1, s = 0). */
export function pbsReflected(v: Stokes): Stokes {
  return mirrorStokes(polarizerCS(v, -1, 0));
}

/** cos δ and sin δ of a waveplate, exact (section 4.5: never sin(π)). */
export interface Retardance {
  cd: number;
  sd: number;
}

export const HALF_WAVE: Retardance = { cd: -1, sd: 0 };
export const QUARTER_WAVE: Retardance = { cd: 0, sd: 1 };

/** The retardance of a waveplate's `retardance` prop; null when it has none (it then does nothing to the polarisation). */
export function retardanceOf(value: unknown): Retardance | null {
  return value === "half" ? HALF_WAVE : value === "quarter" ? QUARTER_WAVE : null;
}

/** The retarder W(θ, δ) with its fast axis at θ (radians) in the ray's own frame (section 4.5). */
export function retarderStokes(v: Stokes, thetaRad: number, r: Retardance): Stokes {
  const c = Math.cos(2 * thetaRad);
  const s = Math.sin(2 * thetaRad);
  const { cd, sd } = r;
  return [
    v[0],
    (c * c + s * s * cd) * v[1] + c * s * (1 - cd) * v[2] + s * sd * v[3],
    c * s * (1 - cd) * v[1] + (s * s + c * c * cd) * v[2] - c * sd * v[3],
    -s * sd * v[1] + c * sd * v[2] + cd * v[3],
  ];
}

/**
 * The angle (radians) at which a ray sees the axis of a waveplate or polariser (section 4.5): θ_eff = atan2(sin θ · dn, cos θ), dn = d · n of the hit. θ is measured in the component's face
 * from u (its local +y, in the table plane) towards z. An absorbing polariser passes the field that is
 * perpendicular to its absorption axis a = -sin θ u + cos θ z and to the ray, d × a = -(cos θ e_h + sin θ dn z)
 * (with d × u = dn z and d × z = -e_h); the ordinary ray of a uniaxial waveplate is d × c in the same way. So θ at
 * normal incidence, -θ (≡ 180° - θ) from the back, and at an angle the vertical part of the axis shrinks with dn,
 * not the horizontal one (not the projection of the axis itself, atan2(sin θ, cos θ · dn)).
 */
export function projectedAxisRad(thetaDeg: number, dn: number): number {
  const t = thetaDeg * DEG;
  return Math.atan2(Math.sin(t) * dn, Math.cos(t));
}

/** The polarisation part of a Stokes vector: (S1, S2, S3) / S0, or null when there is no light (S0 = 0, see EPS_DARK). */
export function normStokes(v: Stokes): StokesNorm | null {
  const s0 = v[0];
  if (!(s0 > 0)) return null;
  return [v[1] / s0, v[2] / s0, v[3] / s0];
}

// ── Gaussian beams (sections 4.2 to 4.4) ──────────────────────────────────────

/** A wavelength in nm as a length in mm. */
export function lambdaMm(wavelengthNm: number): number {
  return wavelengthNm * 1e-6;
}

/**
 * q at a waist of 1/e² diameter `diameterMm`: i·zR with zR = π w0² / (M² λ) (section 4.2); null when
 * the wavelength is not known or the numbers make no beam.
 */
export function waistQ(diameterMm: number, wavelengthNm: number | null, m2: number): QParam | null {
  if (wavelengthNm === null || !(wavelengthNm > 0) || !(diameterMm > 0) || !(m2 > 0)) return null;
  const w0 = diameterMm / 2;
  const zR = (Math.PI * w0 * w0) / (m2 * lambdaMm(wavelengthNm));
  return Number.isFinite(zR) && zR > 0 ? [0, zR] : null;
}

/** Free propagation over `lengthMm` (section 4.3): q + L. */
export function propagateQ(q: QParam, lengthMm: number): QParam {
  return [q[0] + lengthMm, q[1]];
}

/**
 * A thin lens of focal length f (section 4.6): 1/q' = 1/q - 1/f, in real numbers as the server writes it
 * (section 12): den = z² + zR², a = z/den - 1/f, b = -zR/den, den2 = a² + b², z' = a/den2, zR' = -b/den2.
 * null (no Gaussian beam from here on) when den2 is not above 0 or the result is not finite or zR' <= 0, which
 * props the model accepts never reach.
 */
export function qThroughLens(q: QParam, f: number): QParam | null {
  const [z, zR] = q;
  const den = z * z + zR * zR;
  const a = z / den - 1 / f;
  const b = -zR / den;
  const den2 = a * a + b * b;
  if (!(den2 > 0)) return null;
  const z2 = a / den2;
  const zR2 = -b / den2;
  return Number.isFinite(z2) && Number.isFinite(zR2) && zR2 > 0 ? [z2, zR2] : null;
}

/** The 1/e² diameter (mm) of a beam with parameter q (section 4.4): 2 sqrt(λ M² (z² + zR²) / (π zR)). */
export function diameterOf(q: QParam, wavelengthNm: number, m2: number): number {
  const [z, zR] = q;
  return 2 * Math.sqrt((lambdaMm(wavelengthNm) * m2 * (z * z + zR * zR)) / (Math.PI * zR));
}

/**
 * The 1/e² diameter (mm) where the beam has parameter q (section 4.4); null without a Gaussian beam (no q, no
 * known wavelength or no M², as a tree without one has).
 */
export function spotDiameterMm(q: QParam | null, wavelengthNm: number | null, m2: number | null): number | null {
  return q === null || wavelengthNm === null || m2 === null ? null : diameterOf(q, wavelengthNm, m2);
}

/** The 1/e² diameter (mm) at the waist of a beam with parameter q: 2 sqrt(λ M² zR / π). */
export function waistDiameterMm(q: QParam, wavelengthNm: number, m2: number): number {
  return 2 * Math.sqrt((lambdaMm(wavelengthNm) * m2 * q[1]) / Math.PI);
}

/** Section 12.3.3: a waist up to this far past the end of a segment is still that segment's (the resolution of the coordinates). */
export const WAIST_PAST_END_MM = 0.001;

/**
 * Where the waist is in a segment that starts with parameter q and is `lengthMm` long: its distance from the
 * start of the segment, or null when the segment has none. Section 4.4 with section 12.3.3: z < 0 at the start
 * (a waist at the start, z = 0, such as a source's, is not one) and z + L > -0.001 mm, so a sample put in the back
 * focal plane, the waist 2 nm behind it, still has its focus; the next segment then starts with z >= 0 and has
 * none. The distance can therefore pass `lengthMm` by up to WAIST_PAST_END_MM.
 */
export function waistInSegment(q: QParam | null, lengthMm: number): number | null {
  if (q === null) return null;
  const z = q[0];
  return z < 0 && z + lengthMm > -WAIST_PAST_END_MM ? -z : null;
}

// ── reading the polarisation (section 5) ──────────────────────────────────────

export type PolKind = "none" | "unpolarized" | "linear" | "circular" | "elliptical";

/**
 * The polarisation of a normalised Stokes vector, read the way section 5 reads it:
 *   * kind: s = null -> "none" (no light); DOP < 0.01 -> "unpolarized"; |χ| < 1° -> "linear"; |χ| > 44° ->
 *     "circular"; otherwise "elliptical";
 *   * dop = |s| (0 for no light);
 *   * psi_deg = ½ atan2(s2, s1) in [0, 180), from e_h (in the table plane, left of the direction of travel)
 *     towards e_v (up): the azimuth of the ellipse;
 *   * chi_deg = ½ asin(s3 / DOP), signed, in [-45, 45]: the ellipticity (tan χ = ± minor / major axis; its sign
 *     is a convention, no handedness is named);
 *   * ps: for linear light only, "p" within 1° of 0 or 180, "s" within 1° of 90, else null.
 * psi_deg and chi_deg are 0 when there is no light or the light is not polarised at all (DOP = 0).
 */
export interface PolReading {
  kind: PolKind;
  dop: number;
  psi_deg: number;
  chi_deg: number;
  ps: "p" | "s" | null;
}

export function readPolarization(s: StokesNorm | null): PolReading {
  if (s === null) return { kind: "none", dop: 0, psi_deg: 0, chi_deg: 0, ps: null };
  const [s1, s2, s3] = s;
  const dop = Math.sqrt(s1 * s1 + s2 * s2 + s3 * s3);
  if (dop === 0) return { kind: "unpolarized", dop: 0, psi_deg: 0, chi_deg: 0, ps: null };
  let psi = (0.5 * Math.atan2(s2, s1)) / DEG;
  if (psi < 0) psi += 180;
  if (psi >= 180) psi -= 180;
  // s3 / DOP can pass ±1 in the last bit; asin would give NaN
  const chi = (0.5 * Math.asin(Math.max(-1, Math.min(1, s3 / dop)))) / DEG;
  const kind: PolKind = dop < UNPOLARIZED_DOP ? "unpolarized" : Math.abs(chi) < LINEAR_CHI_DEG ? "linear" : Math.abs(chi) > CIRCULAR_CHI_DEG ? "circular" : "elliptical";
  let ps: "p" | "s" | null = null;
  if (kind === "linear") {
    if (psi < PS_DEG || psi > 180 - PS_DEG) ps = "p";
    else if (Math.abs(psi - 90) < PS_DEG) ps = "s";
  }
  return { kind, dop, psi_deg: psi + 0, chi_deg: chi + 0, ps };
}

/** One decimal, then without a trailing ".0" and without "-0" (section 12): 45 -> "45", 22.5 -> "22.5", 37.004502 -> "37". */
function fmtDeg(x: number): string {
  const t = x.toFixed(1).replace(/\.0$/, "");
  return t === "-0" ? "0" : t;
}

/** ψ formatted, and kept in [0, 180) after the rounding (179.96 -> "0"). */
function fmtPsi(psi: number): string {
  const v = Number(psi.toFixed(1));
  return fmtDeg(v >= 180 ? v - 180 : v);
}

/** χ with its sign always written out, the minus in ASCII (section 12): "+20", "-12.5", as a circular one's "+45" / "-45". */
function fmtChi(chi: number): string {
  const t = fmtDeg(chi);
  return t === "0" || t.startsWith("-") ? t : `+${t}`;
}

/** A text in both languages; the UI shows one with tr(), the tests read both. */
export interface Texts {
  zh: string;
  en: string;
}

/**
 * The polarisation in words, section 5 with the format of section 12 (the server's summary writes the same):
 * "无光 / no light", "非偏振 / unpolarised", "线偏 30°（p）/ linear 30° (p)", "圆偏（χ = +45°）/ circular (χ = +45°)",
 * "椭圆偏 30°、χ = +20° / elliptical 30°, χ = +20°", then "（偏振度 0.50）/ (degree of polarisation 0.50)" when the
 * degree of polarisation is below 0.99. Angles at 0.1° without a trailing ".0"; p / s are decided on the
 * unrounded ψ. Both languages; polarizationText() gives the one in use.
 */
export function polarizationTexts(s: StokesNorm | null): Texts {
  const r = readPolarization(s);
  if (r.kind === "none") return { zh: "无光", en: "no light" };
  if (r.kind === "unpolarized") return { zh: "非偏振", en: "unpolarised" };
  let t: Texts;
  if (r.kind === "linear") {
    const psi = fmtPsi(r.psi_deg);
    t = r.ps === null ? { zh: `线偏 ${psi}°`, en: `linear ${psi}°` } : { zh: `线偏 ${psi}°（${r.ps}）`, en: `linear ${psi}° (${r.ps})` };
  } else if (r.kind === "circular") {
    const sign = r.chi_deg < 0 ? "-" : "+";
    t = { zh: `圆偏（χ = ${sign}45°）`, en: `circular (χ = ${sign}45°)` };
  } else {
    const psi = fmtPsi(r.psi_deg);
    const chi = fmtChi(r.chi_deg);
    t = { zh: `椭圆偏 ${psi}°、χ = ${chi}°`, en: `elliptical ${psi}°, χ = ${chi}°` };
  }
  if (r.dop < FULL_DOP) {
    const dop = r.dop.toFixed(2);
    t = { zh: `${t.zh}（偏振度 ${dop}）`, en: `${t.en} (degree of polarisation ${dop})` };
  }
  return t;
}

/** polarizationTexts() in the language in use. */
export function polarizationText(s: StokesNorm | null): string {
  const t = polarizationTexts(s);
  return tr(t.zh, t.en);
}

// ── power and size in words ───────────────────────────────────────────────────

// The power is written with one algorithm on both sides (two separate implementations had differed,
// "1e+03 µW" against "1000 µW" and the last digit rounded either way). No toPrecision, no toFixed:
// three digits are found with a division by a power of ten written as a literal, and the text is put together from
// the integer and the exponent. The shared cases are tests/fixtures/optical_layout/power_text_cases.json.

/** 10^e as the literal "1e<e>" reads, the same double on both sides. */
const pow10 = (e: number): number => Number(`1e${e}`);

/**
 * Three significant digits of x > 0 as an integer n (100 to 999) and an exponent e with x ≈ n · 10^(e-2): e is the
 * largest in [-15, 15] with 10^e <= x (-15 when there is none), n = floor(x / 10^(e-2) + 0.5), and n = 1000 becomes
 * 100 with e + 1.
 */
export function sig3(x: number): { n: number; e: number } {
  let e = -15;
  for (let k = 15; k >= -15; k--) {
    if (pow10(k) <= x) {
      e = k;
      break;
    }
  }
  let n = Math.floor(x / pow10(e - 2) + 0.5);
  if (n >= 1000) {
    n = 100;
    e += 1;
  }
  return { n, e };
}

/** n · 10^k written out from its digits, without trailing zeros after the point: (123, -1) -> "12.3", (150, -2) -> "1.5". */
export function decimalText(n: number, k: number): string {
  const digits = String(n);
  if (k >= 0) return digits + "0".repeat(k);
  const padded = digits.padStart(1 - k, "0");
  const cut = padded.length + k;
  const frac = padded.slice(cut).replace(/0+$/, "");
  return frac === "" ? padded.slice(0, cut) : `${padded.slice(0, cut)}.${frac}`;
}

/** The units of an absolute power: the exponent of the unit in mW. */
const POWER_UNITS: ReadonlyArray<[number, string]> = [
  [3, "W"],
  [0, "mW"],
  [-3, "µW"],
  [-6, "nW"],
  [-9, "pW"],
];

/**
 * An absolute power in mW as the shared algorithm writes it: "0 mW", "12.3 mW", "1.5 W", "17 µW", "0.001 pW", and
 * "< 0.001 pW" below 1e-12 mW, as the share of the source says "< 0.001 %" (so sig3 runs only with e >= -12, and
 * 10^(e-2) is always one of the literals; power_mw <= 1e7 keeps e <= 7).
 */
export function mwText(mw: number): string {
  if (!(mw > 0)) return "0 mW";
  if (mw < 1e-12) return "< 0.001 pW";
  const { n, e } = sig3(mw);
  const [u, unit] = POWER_UNITS.find(([x]) => x <= e) ?? POWER_UNITS[POWER_UNITS.length - 1]!;
  return `${decimalText(n, e - 2 - u)} ${unit}`;
}

/** The share of the source in percent, X = p × 100: "0", "< 0.001", or three digits ("24.6", "100", "0.001"). */
export function percentText(p: number): string {
  const x = p * 100;
  if (!(x > 0)) return "0";
  if (x < 0.001) return "< 0.001";
  const { n, e } = sig3(x);
  return decimalText(n, e - 2);
}

/**
 * The power as section 12.3.2 writes it (the server's summary writes the same): with the absolute power in the
 * unit a person would use, three significant digits (mwText); without it the share of the source, "相对 24.6 %" /
 * "relative 24.6 %" (percentText).
 */
export function powerTexts(p: number, mw: number | null): Texts {
  if (mw !== null) {
    const text = mwText(mw);
    return { zh: text, en: text };
  }
  const x = percentText(p);
  return { zh: `相对 ${x} %`, en: `relative ${x} %` };
}

/** powerTexts() in the language in use. */
export function powerText(p: number, mw: number | null): string {
  const t = powerTexts(p, mw);
  return tr(t.zh, t.en);
}

/** "Ø1.20 mm", in µm below 0.1 mm (section 5); "—" when the size is not known. */
export function spotText(dMm: number | null): string {
  if (dMm === null) return "—";
  return dMm < 0.1 ? `Ø${(dMm * 1000).toFixed(2)} µm` : `Ø${dMm.toFixed(2)} mm`;
}

// ── along a branch ────────────────────────────────────────────────────────────

/** The branches from the root of a tree to `branchId`, ancestors first; empty when there is no such branch. */
export function branchPath(tree: DetailedSourceTrace, branchId: string): DetailedBranch[] {
  const byId = new Map(tree.branches.map((b) => [b.branch, b]));
  const out: DetailedBranch[] = [];
  let b = byId.get(branchId);
  while (b) {
    out.push(b);
    b = b.parent === null ? undefined : byId.get(b.parent);
  }
  return out.reverse();
}

/** What a beam is `dMm` along a segment (the hover tooltip of section 8.4); null when the trace has no beam data. */
export function beamAt(tree: DetailedSourceTrace, seg: Segment, dMm: number): { p: number; mw: number | null; d_mm: number | null; s: StokesNorm | null } | null {
  const b = seg.beam;
  if (!b) return null;
  const mw = tree.beam?.source_mw ?? null;
  const d = spotDiameterMm(b.q === null ? null : propagateQ(b.q, dMm), tree.wavelength_nm, tree.beam?.m2 ?? null);
  return { p: b.p, mw: mw === null ? null : mw * b.p, d_mm: d, s: b.s };
}

export interface ProfilePoint {
  /** optical path from the source, mm */
  pathMm: number;
  /** 1/e² diameter, mm */
  dMm: number;
}

export interface BeamProfile {
  /** the diameter along the path, sampled on every segment (and exactly at every waist and segment end) */
  points: ProfilePoint[];
  /** every component the path meets: where, what it is, what it did */
  marks: Array<{ pathMm: number; componentId: string; action: string }>;
  /** every waist on the path */
  waists: ProfilePoint[];
}

const segLength = (g: Segment): number => Math.hypot(g.x1 - g.x0, g.y1 - g.y0);

/**
 * The vertical run a periscope puts before segment `i` of a branch (docs/v2/design/optical_layout_3d.md section 3.4):
 * how far the height of the segment is from that of the beam arriving at the event before it, mm (0 for the first
 * segment, after any other component, and in a trace without heights). It is path: the event's path_mm is where the
 * run starts, the segment's start is that plus the run.
 */
export function verticalRunBefore(br: Pick<DetailedBranch, "segments" | "events">, i: number): number {
  if (i <= 0) return 0;
  const z0 = br.events[i - 1]?.z_mm;
  const z1 = br.segments[i]?.z_mm;
  return z0 === undefined || z1 === undefined ? 0 : Math.abs(z1 - z0);
}

/** The optical path at the start of segment `i` of a branch: where it starts, or the event before it plus a periscope's vertical run. */
export function segmentStartPath(br: Pick<DetailedBranch, "segments" | "events" | "startPath">, i: number): number {
  const ev = i > 0 ? br.events[i - 1] : undefined;
  return ev ? ev.path_mm + verticalRunBefore(br, i) : br.startPath;
}

/**
 * The beam size along the light path from the source to the end of `branchId` (the chart of section
 * 8.4): x = optical path (mm), y = 1/e² diameter (mm). null when the tree has no Gaussian beam or no
 * beam data, or there is no such branch. A periscope's vertical run (3D section 3.4) is part of the path,
 * and the curve runs through it.
 */
export function beamProfile(tree: DetailedSourceTrace, branchId: string, samplesPerSegment = 32): BeamProfile | null {
  const m2 = tree.beam?.m2 ?? null;
  const wl = tree.wavelength_nm;
  if (m2 === null || wl === null) return null;
  const path = branchPath(tree, branchId);
  if (path.length === 0) return null;
  const n = Math.max(1, Math.floor(samplesPerSegment));
  const points: ProfilePoint[] = [];
  const marks: BeamProfile["marks"] = [];
  const waists: ProfilePoint[] = [];
  for (const br of path) {
    let at = br.startPath;
    br.segments.forEach((g, i) => {
      const len = segLength(g);
      const q = g.beam?.q ?? null;
      if (q !== null) {
        const local: number[] = [];
        // the vertical run before it, if a periscope put one there: its q is this segment's, run backwards
        const run = verticalRunBefore(br, i);
        if (run > 0) for (let k = 0; k < n; k++) local.push(-run + (run * k) / n);
        for (let k = 0; k <= n; k++) local.push((len * k) / n);
        // a waist in the segment or in the vertical run before it (the server's summary looks at both)
        const w0 = waistInSegment(run > 0 ? propagateQ(q, -run) : q, len + run);
        if (w0 !== null) {
          const w = w0 - run;
          // a waist up to 0.001 mm past the end (section 12.3.3) is drawn at the end, so the curve stays in order
          local.push(Math.min(w, len));
          waists.push({ pathMm: at + w, dMm: waistDiameterMm(q, wl, m2) });
        }
        local.sort((a, b) => a - b);
        for (const d of local) points.push({ pathMm: at + d, dMm: diameterOf(propagateQ(q, d), wl, m2) });
      }
      const ev = br.events[i];
      if (ev) marks.push({ pathMm: ev.path_mm, componentId: ev.component, action: ev.action });
      at = ev ? ev.path_mm + verticalRunBefore(br, i + 1) : at + len;
    });
  }
  if (points.length === 0) return null;
  return { points, marks, waists };
}

// ── checks (section 5) ────────────────────────────────────────────────────────

/** Types the spot check leaves out (section 5): the beam is meant to end on them, whatever its size. */
const NO_SPOT_CHECK = new Set(["sample", "beam_dump", "blocker"]);
/** Actions the spot check looks at (section 12): not a hit on the back, not a beam that is blocked there. */
const SPOT_ACTIONS = new Set(["reflect", "transmit", "split", "stop"]);
/** Actions after which light can be extinguished (section 12): the ray goes on, unsplit. */
const EXTINCTION_ACTIONS = new Set(["reflect", "transmit"]);

/** Half the width the beam may use on a component's face: half its aperture, or half the opening of an iris that has one. */
export function usableHalfWidth(c: LayoutComponent): number {
  const open = c.props["open_mm"];
  if (c.type === "iris" && typeof open === "number" && Number.isFinite(open) && open > 0) return open / 2;
  return c.aperture_mm / 2;
}

/**
 * How far (mm) the 1/e² footprint of the arriving beam reaches past the usable aperture of `c` (section 5):
 * |offset| + r / cos(incidence) - A with r = d/2 and A half the aperture (half the opening of an iris).
 * null when it fits, and wherever the rule of section 12 does not look: a beam size not known, an arriving
 * power below 1e-6 (no light to clip), a hit on the back or a beam blocked there (only reflect, transmit, split
 * and stop count), grazing incidence (> 89°), and the types the check leaves out (sample, beam dump, blocker).
 * The server's summary and the check panel use this one rule; the canvas can tint a hit with it too.
 */
export function spotOverflowMm(c: LayoutComponent, ev: Pick<DetailedEvent, "action" | "offset_mm" | "incidence_deg" | "beam">): number | null {
  if (NO_SPOT_CHECK.has(c.type) || !SPOT_ACTIONS.has(ev.action)) return null;
  const dRaw = ev.beam?.d_mm ?? null;
  // section 12.3.6: the numbers as the trace writes them out (p and the diameter at 1e-9, the offset and the
  // incidence at 0.001), as the server judges them, so that a case at an edge is judged alike in the summary and here
  if (dRaw === null || !(pyRound(ev.beam!.p, 9) >= NO_LIGHT_P)) return null;
  const offset = pyRound(ev.offset_mm, 3);
  const inc = pyRound(ev.incidence_deg, 3);
  const d = pyRound(dRaw, 9);
  if (!(inc <= GRAZING_DEG)) return null;
  const over = Math.abs(offset) + d / 2 / Math.cos(inc * DEG) - usableHalfWidth(c);
  return over > SPOT_EPS ? over : null;
}

/**
 * How far (mm) the 1/e² spot of the arriving beam reaches past the usable aperture in height
 * (docs/v2/design/optical_layout_3d.md section 6.3): |dz| + d/2 - A with A half the aperture (half the opening of an
 * iris) - whatever the incidence, the footprint in height does not depend on it. null when it fits and where the rule
 * does not look: as spotOverflowMm (no beam size, no light, not reflect / transmit / split / stop, a sample, beam dump
 * or blocker), and an event without a height (a server without heights). On the numbers the trace writes out (dz at
 * 0.001, d and p at 1e-9), as the server judges them. Only asked for a layout that uses heights.
 */
export function spotHeightOverflowMm(c: LayoutComponent, ev: Pick<DetailedEvent, "action" | "dz_mm" | "beam">): number | null {
  if (NO_SPOT_CHECK.has(c.type) || !SPOT_ACTIONS.has(ev.action) || ev.dz_mm === undefined) return null;
  const dRaw = ev.beam?.d_mm ?? null;
  if (dRaw === null || !(pyRound(ev.beam!.p, 9) >= NO_LIGHT_P)) return null;
  const over = Math.abs(pyRound(ev.dz_mm, 3)) + pyRound(dRaw, 9) / 2 - usableHalfWidth(c);
  return over > SPOT_EPS ? over : null;
}

export type BeamCheckKind = "spot_overflow" | "spot_height" | "extinguished" | "no_retardance";

export interface BeamCheck {
  kind: BeamCheckKind;
  level: "warn" | "info";
  componentId: string;
  message: string;
  /** the beam the check is about (the worst one when several beams have the same problem on the component) */
  source: string;
  wavelength_nm: number | null;
  branch: string;
  /** spot_overflow: by how much (mm) the footprint reaches past the aperture */
  byMm?: number;
  /** the power (relative to its source) arriving in the instance reported */
  p: number;
  /** how many hits have this problem on this component (written as "N beams" when more than one) */
  beams: number;
}

const wlText = (wl: number | null): string => (wl === null ? tr("未知波长", "unknown wavelength") : `${wl} nm`);

/** Why light is gone after a component, for the "extinguished" message. */
function extinctionReason(type: string): string {
  if (type === "polarizer") return tr("偏振", "polarization");
  if (type === "nd") return "ND";
  return tr("通过率", "throughput");
}

/**
 * The beam checks of section 5 with the rules of section 12.3.6 (the server's summary uses the same; the numbers are
 * those the trace writes out, p at 1e-9):
 *   * spot past the aperture: spotOverflowMm on every hit;
 *   * extinguished: a reflection or transmission with p >= 1e-6 arriving and p < 1e-6 at the start of the next
 *     segment of the branch (none after the 64th event, which is not judged); the reason by type: a polariser
 *     "polarization", ND "ND", the rest "throughput";
 *   * waveplate without retardance: any waveplate a beam hits (geometrically: light of p = 0 too), one per component.
 * The first two are one per (kind, component): the worst hit is reported (the largest overflow, the largest arriving
 * relative power p; a tie keeps the earlier one in the trace) and, when more than one hit has the problem, the
 * message ends with "（共 N 束光）" / " (N beams in all)", N the number of hits. Kinds come in the order of
 * CHECK_ORDER; within a kind, in the order of their first hit in the trace (trees in order, branches depth first,
 * events in turn). Read from a detailed trace that carries beam data; a trace without it gives none.
 */
export function beamChecks(layout: OpticalLayout, trace: DetailedTrace): BeamCheck[] {
  const byId = new Map(layout.components.map((c) => [c.id, c]));
  // the 3D contract, section 6.3: the spot in height, only where the layout uses heights (elsewhere a spot too large
  // all round is the spot past the aperture above, and nothing else is to be said)
  const heights = usesHeights(layout);
  const found = new Map<string, BeamCheck>();
  const note = (key: string, make: () => BeamCheck, worse: (old: BeamCheck) => boolean): void => {
    const old = found.get(key);
    if (!old) {
      found.set(key, make());
      return;
    }
    const beams = old.beams + 1;
    if (worse(old)) found.set(key, { ...make(), beams });
    else old.beams = beams;
  };
  for (const tree of trace) {
    for (const br of tree.branches) {
      br.events.forEach((ev, i) => {
        const c = byId.get(ev.component);
        if (!c || !ev.beam) return;
        const label = c.label || c.id;
        const p = pyRound(ev.beam.p, 9);
        const by = spotOverflowMm(c, ev);
        if (by !== null) {
          const d = ev.beam.d_mm ?? 0;
          note(
            `spot|${c.id}`,
            () => ({
              kind: "spot_overflow",
              level: "warn",
              componentId: c.id,
              message: tr(
                `${label}：光斑超出口径 ${by.toFixed(2)} mm（${tree.source}，${wlText(tree.wavelength_nm)}，${spotText(d)}）`,
                `${label}: the spot reaches ${by.toFixed(2)} mm past the aperture (${tree.source}, ${wlText(tree.wavelength_nm)}, ${spotText(d)})`,
              ),
              source: tree.source,
              wavelength_nm: tree.wavelength_nm,
              branch: br.branch,
              byMm: by,
              p,
              beams: 1,
            }),
            (old) => (old.byMm ?? 0) < by,
          );
        }
        const byZ = heights ? spotHeightOverflowMm(c, ev) : null;
        if (byZ !== null) {
          const d = ev.beam.d_mm ?? 0;
          const dz = pyRound(ev.dz_mm ?? 0, 3);
          const where = dz > 0 ? tr("上沿", "upper edge") : dz < 0 ? tr("下沿", "lower edge") : tr("上下沿", "both edges");
          note(
            `spotz|${c.id}`,
            () => ({
              kind: "spot_height",
              level: "warn",
              componentId: c.id,
              message: tr(
                `${label}：光斑在高度方向超出口径${where} ${byZ.toFixed(2)} mm（${tree.source}，${wlText(tree.wavelength_nm)}，${spotText(d)}，高度偏离 ${dz.toFixed(1)} mm）`,
                `${label}: the spot reaches ${byZ.toFixed(2)} mm past the ${where} of the aperture in height (${tree.source}, ${wlText(tree.wavelength_nm)}, ${spotText(d)}, ${dz.toFixed(1)} mm off its axis)`,
              ),
              source: tree.source,
              wavelength_nm: tree.wavelength_nm,
              branch: br.branch,
              byMm: byZ,
              p,
              beams: 1,
            }),
            (old) => (old.byMm ?? 0) < byZ,
          );
        }
        // the state after the action is the start of the next segment of the branch
        const next = br.segments[i + 1]?.beam;
        if (next && EXTINCTION_ACTIONS.has(ev.action) && p >= EXTINCTION_P && pyRound(next.p, 9) < EXTINCTION_P) {
          note(
            `ext|${c.id}`,
            () => ({
              kind: "extinguished",
              level: "warn",
              componentId: c.id,
              message: tr(`${label} 之后几乎没有光（${extinctionReason(c.type)}）`, `Almost no light after ${label} (${extinctionReason(c.type)})`),
              source: tree.source,
              wavelength_nm: tree.wavelength_nm,
              branch: br.branch,
              p,
              beams: 1,
            }),
            (old) => old.p < p,
          );
        }
        if (c.type === "waveplate" && retardanceOf(c.props["retardance"]) === null) {
          note(
            `wp|${c.id}`,
            () => ({
              kind: "no_retardance",
              level: "info",
              componentId: c.id,
              message: tr(`${label}：波片没填相位延迟，按透明处理`, `${label}: the waveplate has no retardance and is treated as clear glass`),
              source: tree.source,
              wavelength_nm: tree.wavelength_nm,
              branch: br.branch,
              p,
              beams: 1,
            }),
            (old) => old.p < p,
          );
        }
      });
    }
  }
  // the number of hits on the two grouped kinds; within a kind the Map keeps the order of the first hit
  const counted = (c: BeamCheck): BeamCheck =>
    c.beams > 1 && c.kind !== "no_retardance" ? { ...c, message: c.message + tr(`（共 ${c.beams} 束光）`, ` (${c.beams} beams in all)`) } : c;
  return orderChecks([...found.values()].map(counted));
}

/**
 * The order of the problems in the check panel, the order of the server's summary (section 12.3.6): a cut trace, hits on the back, off centre by more than 20 %, near misses, spots past the aperture, light
 * extinguished, beams that leave the table, components no beam reaches, 64 interactions, fibres, waveplates without
 * retardance. The kinds are the alignment diagnostics' (indicators.ts) and beamChecks'; a beam blocked on purpose
 * (filter, iris, shutter) has no place in the summary and comes right after the off-centre hits; a kind this list
 * does not know goes just before the waveplate notes.
 */
export const CHECK_ORDER: readonly string[] = [
  "trace_cut",
  "back_hit",
  "off_center",
  // the 3D contract, section 6.3: off the axis in height comes right after off centre, missed in height after the
  // near misses, the spot in height after the spot in the plane
  "off_height",
  "blocked",
  "miss",
  "height_miss",
  "spot_overflow",
  "spot_height",
  "extinguished",
  "left_table",
  "unlit",
  "max_events",
  "fibre",
  "no_retardance",
];

/** Where a kind of problem goes in CHECK_ORDER (an unknown kind just before the waveplate notes). */
export function checkRank(kind: string): number {
  const i = CHECK_ORDER.indexOf(kind);
  return i >= 0 ? i : CHECK_ORDER.indexOf("no_retardance") - 0.5;
}

/** Problems of either source in the order of CHECK_ORDER; within a kind the order they came in (a stable sort). */
export function orderChecks<T extends { kind: string }>(items: readonly T[]): T[] {
  return [...items].sort((a, b) => checkRank(a.kind) - checkRank(b.kind));
}
