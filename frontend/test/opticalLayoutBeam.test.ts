// ════════════════════════════════════════════════════════════════════════════
// Beam physics: src/lib/opticalLayout/beam.ts and the beam state the trace carries
// (docs/v2/design/optical_layout_v2.md sections 4 and 5).
//
//     cd frontend && npm run test:unit
//
// The expected numbers are the anchors of section 7, worked out with an independent
// script from the formulas of section 4, and textbook results (Malus's law, the 45° mirror that turns
// +45° into -45°, the quarter-wave isolator, the Kepler telescope, the focal spot λf/(πw)), so a
// failure here says which piece of physics broke; the shared fixtures (opticalLayoutFixtures.test.ts)
// say whether the TypeScript and the Python traces agree.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import {
  EXTINCTION_P,
  HALF_WAVE,
  QUARTER_WAVE,
  CHECK_ORDER,
  EPS_DARK,
  WAIST_PAST_END_MM,
  beamAt,
  beamChecks,
  beamProfile,
  branchPath,
  decimalText,
  diameterOf,
  linearStokes,
  mirrorStokes,
  mwText,
  normStokes,
  orderChecks,
  pbsReflected,
  pbsTransmitted,
  percentText,
  polarizationText,
  polarizationTexts,
  polarizerStokes,
  powerTexts,
  projectedAxisRad,
  propagateQ,
  qThroughLens,
  readPolarization,
  retardanceOf,
  retarderStokes,
  sig3,
  spotDiameterMm,
  spotOverflowMm,
  spotText,
  waistDiameterMm,
  waistInSegment,
  waistQ,
  type Stokes,
} from "../src/lib/opticalLayout/beam.ts";
import { traceLayout, traceLayoutDetailed } from "../src/lib/opticalLayout/trace.ts";
import { demo } from "../src/lib/opticalLayout/devMock.ts";
import type { OpticalLayout, StokesNorm } from "../src/lib/opticalLayout/types.ts";
import { comp, layoutOf, segGeom } from "./helpers/opticalLayout.ts";

const DEG = Math.PI / 180;
const near = (a: number, b: number, tol = 1e-9, what = "") => assert.ok(Math.abs(a - b) <= tol, `${what} ${a} is not within ${tol} of ${b}`);
const nearVec = (a: readonly number[] | null | undefined, b: readonly number[], tol = 1e-9, what = "") => {
  assert.ok(a, `${what}: missing`);
  assert.equal(a.length, b.length, what);
  a.forEach((x, i) => near(x, b[i]!, tol, `${what}[${i}]`));
};
const H: Stokes = [1, 1, 0, 0];
/** An axis angle in degrees, folded into [0, 180): an axis and its opposite are one axis. */
const axisDeg = (rad: number) => ((((rad / DEG) % 180) + 180) % 180) + 0;
const psiOf = (s: StokesNorm | null) => readPolarization(s).psi_deg;
const diameterAt = (q: [number, number], d: number, wl: number, m2: number) => diameterOf(propagateQ(q, d), wl, m2);

// ── the Mueller matrices (section 4.5) ─────────────────────────────────────────

describe("Stokes vectors and Mueller matrices", () => {
  it("a linear source at α is (1, cos 2α, sin 2α, 0)", () => {
    nearVec(linearStokes(0), [1, 1, 0, 0]);
    nearVec(linearStokes(45), [1, 0, 1, 0]);
    nearVec(linearStokes(90), [1, -1, 0, 0]);
    nearVec(linearStokes(-45), [1, 0, -1, 0]);
  });

  it("A: Malus's law, H through polarisers at 30° then 90°: 0.75 at 30°, then 0.1875 vertical", () => {
    const a = polarizerStokes(H, 30 * DEG);
    near(a[0], 0.75);
    near(psiOf(normStokes(a)), 30, 1e-9, "ψ");
    const b = polarizerStokes(a, 90 * DEG);
    near(b[0], 0.1875);
    nearVec(normStokes(b), [-1, 0, 0], 1e-12, "V");
  });

  it("B: λ/2 with its fast axis at 22.5° turns H into +45° without loss", () => {
    const s = retarderStokes(H, 22.5 * DEG, HALF_WAVE);
    near(s[0], 1);
    nearVec(normStokes(s), [0, 1, 0], 1e-12);
  });

  it("C: λ/4 at 45° makes H circular with χ = -45° (s = (0, 0, -1))", () => {
    const s = retarderStokes(H, 45 * DEG, QUARTER_WAVE);
    nearVec(normStokes(s), [0, 0, -1], 1e-12);
    near(readPolarization(normStokes(s)).chi_deg, -45, 1e-6);
  });

  it("C2: λ/4 at 45°, a mirror at normal incidence, the same λ/4 from the back (seen at 135°): V, the isolator", () => {
    const out = retarderStokes(H, 45 * DEG, QUARTER_WAVE);
    const back = mirrorStokes(out);
    const seen = projectedAxisRad(45, -1);
    near(axisDeg(seen), 135, 1e-12, "the axis seen from the back");
    nearVec(normStokes(retarderStokes(back, seen, QUARTER_WAVE)), [-1, 0, 0], 1e-12);
  });

  it("D: a mirror turns +45° into -45° (ψ = 135°): the reflection flips the handedness of the frame", () => {
    const s = mirrorStokes(linearStokes(45));
    nearVec(normStokes(s), [0, -1, 0], 1e-12);
    near(psiOf(normStokes(s)), 135, 1e-9);
  });

  it("E: +45° into a PBS: half transmitted as H, half reflected as V", () => {
    const t = pbsTransmitted(linearStokes(45));
    const r = pbsReflected(linearStokes(45));
    near(t[0], 0.5, 1e-12);
    near(r[0], 0.5, 1e-12);
    nearVec(normStokes(t), [1, 0, 0], 1e-12);
    nearVec(normStokes(r), [-1, 0, 0], 1e-12);
  });

  it("E3: unpolarised light through any polariser: half the power, linear along the axis", () => {
    for (const axis of [0, 17, 60, 90, 133]) {
      const s = polarizerStokes([1, 0, 0, 0], axis * DEG);
      near(s[0], 0.5, 1e-12, `axis ${axis}`);
      near(psiOf(normStokes(s)), axis, 1e-9, `axis ${axis}`);
    }
  });

  it("the axis a ray sees (section 4.5): θ at normal incidence, 180° - θ from the back, atan2(sin θ dn, cos θ) at an angle", () => {
    near(axisDeg(projectedAxisRad(30, 1)), 30, 1e-12);
    near(axisDeg(projectedAxisRad(30, -1)), 150, 1e-12);
    near(axisDeg(projectedAxisRad(30, Math.cos(40 * DEG))), 23.858655, 1e-6);
  });

  it("the axis a ray sees is the field an absorbing polariser passes: perpendicular to its absorption axis and to the ray", () => {
    // worked out with vectors, not with the formula: component normal n, u = z × n along its face, the axis
    // f = cos θ u + sin θ z, the absorption axis a = -sin θ u + cos θ z; the field that passes is d × a, read in the
    // ray's frame e_h = z × d, e_v = z
    const cross = (p: number[], q: number[]) => [p[1]! * q[2]! - p[2]! * q[1]!, p[2]! * q[0]! - p[0]! * q[2]!, p[0]! * q[1]! - p[1]! * q[0]!];
    const dot = (p: number[], q: number[]) => p[0]! * q[0]! + p[1]! * q[1]! + p[2]! * q[2]!;
    const z = [0, 0, 1];
    for (const [thetaDeg, normalDeg, rayDeg] of [[30, 40, 0], [30, 0, 0], [30, 180, 0], [70, 25, 100], [-20, 300, 210], [90, 10, 30]] as const) {
      const n = [Math.cos(normalDeg * DEG), Math.sin(normalDeg * DEG), 0];
      const d = [Math.cos(rayDeg * DEG), Math.sin(rayDeg * DEG), 0];
      const u = cross(z, n);
      const t = thetaDeg * DEG;
      const a = [-Math.sin(t) * u[0]!, -Math.sin(t) * u[1]!, Math.cos(t)];
      const e = cross(d, a);
      const eh = cross(z, d);
      const passed = Math.atan2(dot(e, z), dot(e, eh));
      near(axisDeg(projectedAxisRad(thetaDeg, dot(d, n))), axisDeg(passed), 1e-9, `θ ${thetaDeg}, normal ${normalDeg}, ray ${rayDeg}`);
    }
  });

  it("the retardance of a waveplate: exact constants, and none without the prop", () => {
    assert.deepEqual(retardanceOf("half"), { cd: -1, sd: 0 });
    assert.deepEqual(retardanceOf("quarter"), { cd: 0, sd: 1 });
    assert.equal(retardanceOf(undefined), null);
    assert.equal(retardanceOf("eighth"), null);
  });

  it("darkness is exact: a polariser or PBS output at or below 1e-12 of the arriving power is (0, 0, 0, 0)", () => {
    // +45° made by a half-wave plate at 22.5° and then V from another at 45°: right only to the last bit or so
    const d45 = retarderStokes(H, 22.5 * DEG, HALF_WAVE);
    assert.deepEqual(polarizerStokes(d45, 135 * DEG), [0, 0, 0, 0], "crossed: no residue of either sign");
    const v = retarderStokes(d45, 67.5 * DEG, HALF_WAVE);
    assert.deepEqual(polarizerStokes(v, 0), [0, 0, 0, 0]);
    assert.deepEqual(pbsTransmitted(v), [0, 0, 0, 0]);
    assert.deepEqual(pbsReflected(H), [0, 0, 0, 0]);
    assert.equal(normStokes([0, 0, 0, 0]), null, "no light has no polarisation");
    // weak light is not dark: what counts is the share of the arriving power
    const weak: Stokes = [1e-15, 1e-15, 0, 0];
    near(polarizerStokes(weak, 0)[0], 1e-15, 1e-30);
    nearVec(normStokes(polarizerStokes(weak, 0)), [1, 0, 0], 1e-12, "a polarisation of its own");
    const nearlyCrossed = polarizerStokes(H, (90 - 1e-4) * DEG);
    assert.ok(nearlyCrossed[0] > EPS_DARK, "3e-12 of the power is above the threshold");
    nearVec(normStokes(nearlyCrossed), [Math.cos(2 * (90 - 1e-4) * DEG), Math.sin(2 * (90 - 1e-4) * DEG), 0], 1e-12, "the polarisation is the axis, not noise");
    assert.equal(EPS_DARK, 1e-12);
  });
});

// ── Gaussian beams (sections 4.2 to 4.4) ──────────────────────────────────────

describe("Gaussian beams", () => {
  it("F: 633 nm, Ø1 mm, M² 1: zR 1240.755392 mm, Ø1.284357 mm after 1000 mm", () => {
    const q = waistQ(1, 633, 1)!;
    assert.equal(q[0], 0);
    near(q[1], 1240.755392, 1e-6);
    near(diameterOf(q, 633, 1), 1, 1e-12, "the waist");
    near(diameterAt(q, 1000, 633, 1), 1.284357, 1e-6);
    near(diameterAt(q, -1000, 633, 1), 1.284357, 1e-6, "symmetric about the waist");
  });

  it("M² makes zR shorter and the beam spread faster by M²", () => {
    const q = waistQ(1, 633, 2)!;
    near(q[1], 1240.755392 / 2, 1e-6);
    near(waistDiameterMm(q, 633, 2), 1, 1e-12);
  });

  it("no wavelength or no diameter: no beam; spotDiameterMm is null without q, wavelength or M²", () => {
    assert.equal(waistQ(1, null, 1), null);
    assert.equal(waistQ(0, 633, 1), null);
    const q = waistQ(1, 633, 1)!;
    near(spotDiameterMm(q, 633, 1)!, 1, 1e-12);
    assert.equal(spotDiameterMm(null, 633, 1), null);
    assert.equal(spotDiameterMm(q, null, 1), null);
    assert.equal(spotDiameterMm(q, 633, null), null);
  });

  it("G: a Kepler telescope f 50 / 150 makes 532 nm Ø1 mm three times wider", () => {
    let q = waistQ(1, 532, 1)!;
    q = propagateQ(q, 100);
    near(diameterOf(q, 532, 1), 1.002291, 1e-6, "at the first lens");
    q = propagateQ(qThroughLens(q, 50)!, 200);
    near(diameterOf(q, 532, 1), 3.000765, 1e-6, "at the second lens");
    q = propagateQ(qThroughLens(q, 150)!, 1000);
    near(diameterOf(q, 532, 1), 3.014325, 1e-6, "1000 mm on");
  });

  it("H: 633 nm Ø4 mm into an objective of f 4 mm: the waist 4.000002 mm behind it, Ø0.805958 µm (λf/(πw): 0.805961 µm)", () => {
    const q = qThroughLens(propagateQ(waistQ(4, 633, 1)!, 50), 4)!;
    near(waistInSegment(q, 10)!, 4.000002, 1e-6);
    near(waistDiameterMm(q, 633, 1) * 1000, 0.805958, 1e-6);
    near((2 * 633e-6 * 4) / (Math.PI * 2) * 1000, 0.805961, 1e-6, "the textbook estimate");
    assert.equal(waistInSegment(q, 3), null, "a segment that ends before the waist has none");
    assert.equal(waistInSegment(propagateQ(q, 5), 10), null, "nor one that starts after it");
    assert.equal(waistInSegment(null, 10), null, "no beam, no waist");
    // section 12.3.3: a waist up to 0.001 mm past the end is still the segment's (a sample put in the focal plane)
    near(waistInSegment(q, 4)!, 4.000002, 1e-6, "2 nm past the end of a 4 mm segment");
    assert.equal(waistInSegment(q, 4.000002 - 2 * WAIST_PAST_END_MM), null, "more than 0.001 mm short of it is not");
    assert.equal(waistInSegment([0, 5], 10), null, "a waist at the start (a source's) is no focus");
  });

  it("a lens that would leave no beam gives none (the server's guard): a focal length at the waist of a zero-size beam cannot occur, a NaN can", () => {
    assert.equal(qThroughLens([0, 1], Number.NaN), null);
    const q = qThroughLens([0, 1], 1e-300);
    assert.ok(q === null || q[1] > 0);
  });
});

// ── the words of section 5 ────────────────────────────────────────────────────

describe("the polarisation in words (section 5)", () => {
  const t = (s: StokesNorm | null) => polarizationTexts(s);

  it("no light, unpolarised below a degree of polarisation of 0.01", () => {
    assert.deepEqual(t(null), { zh: "无光", en: "no light" });
    assert.deepEqual(t([0, 0, 0]), { zh: "非偏振", en: "unpolarised" });
    assert.deepEqual(t([0.0099, 0, 0]), { zh: "非偏振", en: "unpolarised" });
    assert.equal(readPolarization([0.0101, 0, 0]).kind, "linear");
    assert.deepEqual(readPolarization(null), { kind: "none", dop: 0, psi_deg: 0, chi_deg: 0, ps: null });
    assert.deepEqual(readPolarization([0, 0, 0]), { kind: "unpolarized", dop: 0, psi_deg: 0, chi_deg: 0, ps: null });
    assert.equal(polarizationText(null), "无光", "tr() answers in Chinese under node");
  });

  it("linear ψ° at 0.1° without a trailing .0, with (p) within 1° of 0 or 180 and (s) within 1° of 90 (section 12)", () => {
    const lin = (deg: number): StokesNorm => [Math.cos(2 * deg * DEG), Math.sin(2 * deg * DEG), 0];
    assert.deepEqual(t(lin(0)), { zh: "线偏 0°（p）", en: "linear 0° (p)" });
    assert.deepEqual(t(lin(179.5)), { zh: "线偏 179.5°（p）", en: "linear 179.5° (p)" });
    assert.deepEqual(t(lin(90.9)), { zh: "线偏 90.9°（s）", en: "linear 90.9° (s)" });
    assert.deepEqual(t(lin(30)), { zh: "线偏 30°", en: "linear 30°" });
    assert.deepEqual(t(lin(22.5)), { zh: "线偏 22.5°", en: "linear 22.5°" });
    assert.deepEqual(t(lin(37.004502)), { zh: "线偏 37°", en: "linear 37°" });
    assert.deepEqual(t(lin(-45)), { zh: "线偏 135°", en: "linear 135°" }, "ψ is in [0, 180)");
    assert.deepEqual(t(lin(179.96)), { zh: "线偏 0°（p）", en: "linear 0° (p)" }, "179.96° rounds to the angle it is, 0°, not 180°");
    assert.deepEqual(t(lin(1.04)), { zh: "线偏 1°", en: "linear 1°" }, "p / s are decided on the unrounded ψ: 1.04° is not p, though it reads 1°");
    assert.equal(readPolarization(lin(1.1)).ps, null);
    assert.equal(readPolarization(lin(0.9)).ps, "p");
    assert.equal(readPolarization(lin(89.1)).ps, "s");
    assert.equal(readPolarization([0, 0, 1]).ps, null, "only linear light is p or s");
  });

  it("circular beyond |χ| = 44°, with the sign of χ and no name for the handedness", () => {
    assert.deepEqual(t([0, 0, -1]), { zh: "圆偏（χ = -45°）", en: "circular (χ = -45°)" }, "the minus in ASCII, as in the narrative");
    assert.deepEqual(t([0, 0, 1]), { zh: "圆偏（χ = +45°）", en: "circular (χ = +45°)" });
  });

  it("the server's table of section 12.3.1 (tests/v2/unit/optical_layout/test_layout_summary_beam.py), row by row", () => {
    // s = DOP (cos 2χ cos 2ψ, cos 2χ sin 2ψ, sin 2χ), as the server's test builds it
    const stokes = (psi: number, chi: number, dop = 1): StokesNorm => [
      dop * Math.cos(2 * chi * DEG) * Math.cos(2 * psi * DEG),
      dop * Math.cos(2 * chi * DEG) * Math.sin(2 * psi * DEG),
      dop * Math.sin(2 * chi * DEG),
    ];
    const rows: Array<[StokesNorm | null, string, string]> = [
      [null, "no light", "无光"],
      [[0, 0, 0], "unpolarised", "非偏振"],
      [[0.005, 0, 0], "unpolarised", "非偏振"],
      [[1, 0, 0], "linear 0° (p)", "线偏 0°（p）"],
      [[-1, 0, 0], "linear 90° (s)", "线偏 90°（s）"],
      [[0, 1, 0], "linear 45°", "线偏 45°"],
      [[0, -1, 0], "linear 135°", "线偏 135°"],
      [stokes(179.4, 0), "linear 179.4° (p)", "线偏 179.4°（p）"],
      [stokes(0.6, 0), "linear 0.6° (p)", "线偏 0.6°（p）"],
      [stokes(90.8, 0), "linear 90.8° (s)", "线偏 90.8°（s）"],
      [stokes(91.2, 0), "linear 91.2°", "线偏 91.2°"],
      [stokes(30, 0.6), "linear 30°", "线偏 30°"],
      [[0, 0, -1], "circular (χ = -45°)", "圆偏（χ = -45°）"],
      [[0, 0, 1], "circular (χ = +45°)", "圆偏（χ = +45°）"],
      [stokes(10, 44.5), "circular (χ = +45°)", "圆偏（χ = +45°）"],
      [stokes(30, 20), "elliptical 30°, χ = +20°", "椭圆偏 30°、χ = +20°"], // the sign is always written out
      [stokes(120, -12.5), "elliptical 120°, χ = -12.5°", "椭圆偏 120°、χ = -12.5°"],
      [stokes(0, 0, 0.5), "linear 0° (p) (degree of polarisation 0.50)", "线偏 0°（p）（偏振度 0.50）"],
    ];
    for (const [s, en, zh] of rows) assert.deepEqual(t(s), { zh, en }, JSON.stringify(s));
  });

  it("elliptical in between, and the degree of polarisation when it is below 0.99", () => {
    // ψ = 20°, χ = 10°: s = (cos 2χ cos 2ψ, cos 2χ sin 2ψ, sin 2χ)
    const e: StokesNorm = [Math.cos(20 * DEG) * Math.cos(40 * DEG), Math.cos(20 * DEG) * Math.sin(40 * DEG), Math.sin(20 * DEG)];
    assert.deepEqual(t(e), { zh: "椭圆偏 20°、χ = +10°", en: "elliptical 20°, χ = +10°" });
    const half = e.map((x) => x * 0.5) as StokesNorm;
    assert.deepEqual(t(half), { zh: "椭圆偏 20°、χ = +10°（偏振度 0.50）", en: "elliptical 20°, χ = +10° (degree of polarisation 0.50)" });
    const r = readPolarization(half);
    near(r.dop, 0.5, 1e-12);
    near(r.chi_deg, 10, 1e-9, "χ is taken from s3 / DOP");
    const neg: StokesNorm = [Math.cos(45 * DEG) * Math.cos(250 * DEG), Math.cos(45 * DEG) * Math.sin(250 * DEG), -Math.sin(45 * DEG)];
    assert.deepEqual(t(neg), { zh: "椭圆偏 125°、χ = -22.5°", en: "elliptical 125°, χ = -22.5°" });
    assert.match(polarizationTexts([0, 0, -1]).zh, /χ = -45°/, "an ASCII minus, as in the rest of the narrative");
  });

  it("χ survives s3 / DOP rounding past ±1", () => {
    near(readPolarization([0, 0, -1.0000000000000002]).chi_deg, -45, 1e-9);
  });

  it("power the way section 12.3.2 writes it: the server's table, and the share of the source at three digits", () => {
    const table: Array<[number, string]> = [
      [0, "0 mW"],
      [12.345, "12.3 mW"],
      [100, "100 mW"],
      [1500, "1.5 W"],
      [1.875, "1.88 mW"],
      [0.017, "17 µW"],
      [2.5e-5, "25 nW"],
      [1e-12, "0.001 pW"],
    ];
    for (const [mw, text] of table) assert.deepEqual(powerTexts(1, mw), { zh: text, en: text }, String(mw));
    assert.deepEqual(powerTexts(0.246, null), { zh: "相对 24.6 %", en: "relative 24.6 %" });
    assert.deepEqual(powerTexts(1, null), { zh: "相对 100 %", en: "relative 100 %" });
    assert.equal(powerTexts(0.1875, null).en, "relative 18.8 %");
    assert.equal(powerTexts(0, null).en, "relative 0 %");
    assert.equal(powerTexts(-1e-17, null).en, "relative 0 %", "rounding noise is nothing");
    assert.equal(powerTexts(2e-6, null).en, "relative < 0.001 %", "below 0.001 % the share is not written out");
    assert.equal(powerTexts(1e-5, null).en, "relative 0.001 %");
  });

  it("the shared cases of the power text (tests/fixtures/optical_layout/power_text_cases.json), the same file the server's test reads", () => {
    let dir = dirname(fileURLToPath(import.meta.url));
    let file = "";
    for (let i = 0; i < 8 && !file; i++) {
      const candidate = join(dir, "tests", "fixtures", "optical_layout", "power_text_cases.json");
      if (existsSync(candidate)) file = candidate;
      dir = dirname(dir);
    }
    assert.ok(file, "power_text_cases.json was not found above the frontend directory");
    const cases = (JSON.parse(readFileSync(file, "utf8")) as { cases: Array<{ mw?: number; p?: number; zh: string; en: string }> }).cases;
    assert.ok(cases.length >= 40, `only ${cases.length} cases`);
    const wrong: string[] = [];
    for (const c of cases) {
      // an absolute power: "<number> <unit>" in both languages (the narrative puts "入射" / "arriving" before it);
      // a share of the source: the whole phrase
      const got = c.mw !== undefined ? powerTexts(1, c.mw) : powerTexts(c.p!, null);
      if (got.zh !== c.zh || got.en !== c.en) wrong.push(`${JSON.stringify(c)} -> ${JSON.stringify(got)}`);
    }
    assert.deepEqual(wrong, []);
  });

  it("the power algorithm: sig3 finds three digits with a literal power of ten, the text is put together from them", () => {
    assert.deepEqual(sig3(12.345), { n: 123, e: 1 });
    assert.deepEqual(sig3(1.875), { n: 188, e: 0 }, "floor(187.5 + 0.5)");
    assert.deepEqual(sig3(0.99996), { n: 100, e: 0 }, "1000 becomes 100 one decade up");
    assert.deepEqual(sig3(999.5), { n: 100, e: 3 });
    assert.deepEqual(sig3(1e-12), { n: 100, e: -12 });
    assert.deepEqual([decimalText(123, -1), decimalText(150, -2), decimalText(100, -5), decimalText(170, -1), decimalText(100, 2), decimalText(250, 0)], ["12.3", "1.5", "0.001", "17", "10000", "250"]);
    const mw: Array<[number, string]> = [
      [0, "0 mW"],
      [12.345, "12.3 mW"],
      [100, "100 mW"],
      [1500, "1.5 W"],
      [1.875, "1.88 mW"],
      [0.017, "17 µW"],
      [2.5e-5, "25 nW"],
      [1e-12, "0.001 pW"],
      [0.99996, "1 mW"],
      [999.5, "1 W"],
      [0.0009995, "1 µW"],
      [1e7, "10000 W"],
      [25.725, "25.7 mW"],
      [9.99e-13, "< 0.001 pW"],
      [1e-16, "< 0.001 pW"],
      [5e-20, "< 0.001 pW"],
    ];
    for (const [x, text] of mw) assert.equal(mwText(x), text, String(x));
    const pct: Array<[number, string]> = [
      [0, "0"],
      [1, "100"],
      [0.246, "24.6"],
      [0.75, "75"],
      [0.123456, "12.3"],
      [1e-5, "0.001"],
      [9.99e-6, "< 0.001"],
    ];
    for (const [p, text] of pct) assert.equal(percentText(p), text, String(p));
  });

  it("a spot the way section 12.3.2 writes it: mm at 0.01, µm below 0.1 mm, a dash when not known", () => {
    assert.deepEqual([spotText(1.2), spotText(0.1), spotText(0.0999), spotText(0.000805961)], ["Ø1.20 mm", "Ø0.10 mm", "Ø99.90 µm", "Ø0.81 µm"]);
    assert.equal(spotText(null), "—");
  });
});

// ── the trace carries the beam (section 4.7) ──────────────────────────────────

const laser = (props: Record<string, unknown>, x = 0, y = 100, a = 0) => comp("laser", "las1", x, y, a, { props: { wavelength_nm: 532, ...props } });
const det = (id: string, x: number, y: number, a: number) => comp("detector", id, x, y, a);
const lastBeam = (l: OpticalLayout, branch = "0") => {
  const b = traceLayout(l)[0]!.branches.find((x) => x.branch === branch)!;
  return b.events[b.events.length - 1]!.beam!;
};

describe("the beam along a trace", () => {
  it("A through the tracer: p 0.75 at 30°, then 0.1875 vertical", () => {
    const l = layoutOf([laser({ polarization: "linear" }), comp("polarizer", "p1", 100, 100, 0, { props: { axis_deg: 30 } }), comp("polarizer", "p2", 200, 100, 0, { props: { axis_deg: 90 } }), det("d", 300, 100, 180)]);
    const ev = traceLayout(l)[0]!.branches[0]!.events;
    assert.equal(ev[1]!.beam!.p, 0.75);
    near(psiOf(ev[1]!.beam!.s), 30, 1e-6);
    assert.equal(ev[2]!.beam!.p, 0.1875);
    assert.deepEqual(ev[2]!.beam!.s, [-1, 0, 0]);
  });

  it("C2 through the tracer: with a PBS in front, the light that comes back leaves by the reflected port, all of it", () => {
    const l = layoutOf([
      comp("laser", "las1", 20, 200, 0, { props: { wavelength_nm: 532, polarization: "linear" } }),
      comp("pbs", "pbs1", 100, 200, 135),
      comp("waveplate", "qwp", 200, 200, 0, { props: { retardance: "quarter", fast_axis_deg: 45 } }),
      comp("mirror", "m", 300, 200, 180),
      det("d", 100, 100, 90),
    ]);
    const t = traceLayout(l)[0]!;
    assert.deepEqual(
      t.branches.map((b) => [b.branch, b.fraction, b.end]),
      [
        ["0", 1, "split"],
        ["0.t", 1, "split"],
        ["0.t.r", 1, "stop"],
      ],
      "p goes through on the way in, s is reflected on the way back; the empty ports are not made",
    );
    const back = t.branches[1]!.events;
    assert.deepEqual(back.map((e) => e.component), ["qwp", "m", "qwp", "pbs1"]);
    assert.deepEqual(back[3]!.beam!.s, [-1, 0, 0], "vertical when it comes back");
    assert.deepEqual(lastBeam(l, "0.t.r"), { p: 1, mw: null, d_mm: null, s: [-1, 0, 0] });
  });

  it("D through the tracer: +45° off a fold mirror is -45°", () => {
    const l = layoutOf([laser({ polarization: "linear", pol_angle_deg: 45 }), comp("mirror", "m", 100, 100, 135), det("d", 100, 300, 270)]);
    assert.deepEqual(lastBeam(l).s, [0, -1, 0]);
  });

  it("E through the tracer: the PBS's fractions are the shares of the arriving power", () => {
    const l = layoutOf([laser({ polarization: "linear", pol_angle_deg: 30 }), comp("pbs", "pbs1", 100, 100, 135), det("dr", 100, 300, 270), det("dt", 300, 100, 180)]);
    const t = traceLayout(l)[0]!;
    assert.deepEqual(t.branches.map((b) => [b.branch, b.fraction]), [["0", 1], ["0.r", 0.25], ["0.t", 0.75]], "cos² 30° through, sin² 30° reflected");
    assert.equal(lastBeam(l, "0.r").p, 0.25);
    assert.equal(lastBeam(l, "0.t").p, 0.75);
  });

  it("p polarisation into a PBS: only the transmitted branch is made", () => {
    const l = layoutOf([laser({ polarization: "linear" }), comp("pbs", "pbs1", 100, 100, 135), det("dr", 100, 300, 270), det("dt", 300, 100, 180)]);
    assert.deepEqual(traceLayout(l)[0]!.branches.map((b) => b.branch), ["0", "0.t"]);
  });

  it("a beam splitter keeps the polarisation of v1's fractions: R R_m S and (1 - R) S", () => {
    const l = layoutOf([laser({ polarization: "linear", pol_angle_deg: 45 }), comp("beam_splitter", "bs", 100, 100, 135, { props: { reflect_fraction: 0.3 } }), det("dr", 100, 300, 270), det("dt", 300, 100, 180)]);
    const t = traceLayout(l)[0]!;
    assert.deepEqual(t.branches.map((b) => [b.branch, b.fraction]), [["0", 1], ["0.r", 0.3], ["0.t", 0.7]]);
    assert.deepEqual(lastBeam(l, "0.r"), { p: 0.3, mw: null, d_mm: null, s: [0, -1, 0] });
    assert.deepEqual(lastBeam(l, "0.t"), { p: 0.7, mw: null, d_mm: null, s: [0, 1, 0] });
  });

  it("crossed polarisers: no light, and the geometry goes on (a PBS behind them splits 50 / 50 for the geometry)", () => {
    const l = layoutOf([laser({ polarization: "linear" }), comp("polarizer", "p1", 100, 100, 0, { props: { axis_deg: 90 } }), comp("pbs", "pbs1", 200, 100, 135), det("dr", 200, 300, 270), det("dt", 400, 100, 180)]);
    const t = traceLayout(l)[0]!;
    assert.deepEqual(t.branches.map((b) => [b.branch, b.fraction, b.end]), [["0", 1, "split"], ["0.r", 0.5, "stop"], ["0.t", 0.5, "stop"]]);
    assert.deepEqual(t.branches[0]!.segments[1]!.beam, { p: 0, s: null, q: null });
    assert.deepEqual(lastBeam(l, "0.t"), { p: 0, mw: null, d_mm: null, s: null });
  });

  it("an unknown polarisation is unpolarised: one polariser halves the power of an old layout", () => {
    const l = layoutOf([laser({ power_mw: 10 }), comp("polarizer", "p1", 100, 100, 0), det("d", 300, 100, 180)]);
    assert.deepEqual(lastBeam(l), { p: 0.5, mw: 5, d_mm: null, s: [1, 0, 0] });
  });

  it("power: ND multiplies by 10^-OD, throughput by itself, a dichroic, an iris and a shutter take nothing", () => {
    const l = layoutOf([
      laser({ power_mw: 100 }),
      comp("nd", "nd1", 50, 100, 0, { props: { od: 1 } }),
      comp("window", "w", 100, 100, 0, { props: { throughput: 0.9 } }),
      comp("iris", "ir", 150, 100, 0, { props: { open_mm: 10 } }),
      comp("shutter", "sh", 200, 100, 0, { props: { closed: false } }),
      comp("dichroic", "dm", 250, 100, 135, { props: { edge_nm: 600, reflects: "short" } }),
      comp("mirror", "m", 250, 300, 315, { props: { throughput: 0.5 } }),
      det("d", 500, 300, 180),
    ]);
    const ev = traceLayout(l)[0]!.branches[0]!.events;
    assert.deepEqual(ev.map((e) => [e.component, e.beam!.p, e.beam!.mw]), [
      ["nd1", 1, 100],
      ["w", 0.1, 10],
      ["ir", 0.09, 9],
      ["sh", 0.09, 9],
      ["dm", 0.09, 9],
      ["m", 0.09, 9],
      ["d", 0.045, 4.5],
    ]);
  });

  it("a waveplate without retardance does nothing to the polarisation but its throughput", () => {
    const l = layoutOf([laser({ polarization: "linear", pol_angle_deg: 20 }), comp("waveplate", "w", 100, 100, 0, { props: { fast_axis_deg: 10, throughput: 0.8 } }), det("d", 300, 100, 180)]);
    const b = lastBeam(l);
    assert.equal(b.p, 0.8);
    near(psiOf(b.s), 20, 1e-6);
  });

  it("a fibre continuation: power is what arrived times the coupling, polarisation and size are the fibre output's own", () => {
    const l = layoutOf([
      laser({ power_mw: 100 }),
      comp("nd", "nd1", 50, 100, 0, { props: { od: 1 } }),
      comp("mirror", "m", 100, 100, 135, { props: { throughput: 0.9 } }),
      comp("fiber_in", "fi", 100, 200, 270, { props: { continues_at: "fo", coupling: 0.6 } }),
      comp("fiber_out", "fo", 0, 400, 0, { props: { wavelength_nm: 1064, power_mw: 999, polarization: "linear", pol_angle_deg: 90, beam_diameter_mm: 0.5 } }),
      det("d", 200, 400, 180),
    ]);
    const t = traceLayout(l);
    assert.deepEqual(t.map((x) => [x.source, x.wavelength_nm, x.beam]), [
      ["las1", 532, { source_mw: 100, m2: null }],
      ["fo", 532, { source_mw: 5.4, m2: 1 }],
    ], "100 mW x 0.1 x 0.9 x 0.6; the fibre output's own power and wavelength are not used");
    const seg = t[1]!.branches[0]!.segments[0]!.beam!;
    assert.equal(seg.p, 1, "the continued tree starts again at 1");
    assert.deepEqual(seg.s, [-1, 0, 0], "its own linear 90°");
    near(seg.q![1], (Math.PI * 0.25 * 0.25) / 532e-6, 1e-6, "its own waist at the arriving wavelength");
  });

  it("only the first arrival at a fibre input feeds the continuation", () => {
    const l = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532, power_mw: 10 } }),
      comp("fiber_in", "fi", 100, 100, 180, { props: { continues_at: "fo" } }),
      comp("laser", "las2", 0, 50, 0, { props: { wavelength_nm: 532, power_mw: 40 } }),
      comp("mirror", "m", 100, 50, 135),
      comp("fiber_out", "fo", 0, 400, 0, { props: { wavelength_nm: 532 } }),
      det("d", 200, 400, 180),
    ]);
    // las2 is folded up into the back of fi (no stop), las1 arrives first on its front
    const t = traceLayout(l);
    assert.deepEqual(t.map((x) => [x.source, x.beam?.source_mw]), [["las1", 10], ["las2", 40], ["fo", 10]]);
  });

  it("lamps and LEDs: unpolarised, no beam size, no power", () => {
    const l = layoutOf([comp("lamp", "lamp1", 0, 100, 0, { props: { wavelength_nm: 600 } }), det("d", 100, 100, 180)]);
    const t = traceLayout(l)[0]!;
    assert.deepEqual(t.beam, { source_mw: null, m2: null });
    assert.deepEqual(t.branches[0]!.events[0]!.beam, { p: 1, mw: null, d_mm: null, s: [0, 0, 0] });
  });

  it("F through the tracer: q at the source and the diameter where the beam arrives", () => {
    const l = layoutOf([comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 633, beam_diameter_mm: 1 } }), det("d", 1000, 100, 180)], { width_mm: 3000 });
    const t = traceLayout(l)[0]!;
    assert.deepEqual(t.beam, { source_mw: null, m2: 1 });
    nearVec(t.branches[0]!.segments[0]!.beam!.q, [0, 1240.755392], 5e-6); // q at 9 significant digits: 1240.75539
    near(t.branches[0]!.events[0]!.beam!.d_mm!, 1.284357, 1e-6);
  });

  it("G through the tracer: the Kepler telescope", () => {
    const l = layoutOf(
      [
        comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532, beam_diameter_mm: 1 } }),
        comp("lens", "l1", 100, 100, 0, { props: { focal_mm: 50 } }),
        comp("lens", "l2", 300, 100, 0, { props: { focal_mm: 150 } }),
        det("d", 1300, 100, 180),
      ],
      { width_mm: 3000 },
    );
    const ev = traceLayout(l)[0]!.branches[0]!.events;
    near(ev[0]!.beam!.d_mm!, 1.002291, 1e-6);
    near(ev[1]!.beam!.d_mm!, 3.000765, 1e-6);
    near(ev[2]!.beam!.d_mm!, 3.014325, 1e-6);
  });

  it("H through the tracer: the waist behind the objective, on a sample put in the focus", () => {
    const l = layoutOf([comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 633, beam_diameter_mm: 4 } }), comp("objective", "o", 50, 100, 0, { props: { focal_mm: 4 } }), comp("sample", "s", 54, 100, 180)]);
    const tree = traceLayoutDetailed(l)[0]!;
    const seg = tree.branches[0]!.segments[1]!;
    near(waistInSegment(seg.beam!.q, 4.5)!, 4.000002, 1e-6);
    near(waistDiameterMm(seg.beam!.q!, 633, 1) * 1000, 0.805958, 1e-6);
  });

  it("a lens of focal length 0 or without one leaves the beam as it is", () => {
    for (const props of [{}, { focal_mm: 0 }]) {
      const l = layoutOf([comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 633, beam_diameter_mm: 1 } }), comp("lens", "l", 100, 100, 0, { props }), det("d", 1000, 100, 180)], { width_mm: 3000 });
      near(traceLayout(l)[0]!.branches[0]!.events[1]!.beam!.d_mm!, 1.284357, 1e-6, JSON.stringify(props));
    }
  });

  it("the beam keys add nothing to the geometry: the same layout without beam props traces the same", () => {
    const beamy = layoutOf([laser({ polarization: "linear", pol_angle_deg: 30, power_mw: 5, beam_diameter_mm: 1 }), comp("beam_splitter", "bs", 100, 100, 135, { props: { reflect_fraction: 0.4 } }), comp("mirror", "m", 300, 100, 90, { props: { throughput: 0.5 } }), det("dr", 100, 300, 270)]);
    const plain = { ...beamy, components: beamy.components.map((c) => ({ ...c, props: Object.fromEntries(Object.entries(c.props).filter(([k]) => !["polarization", "pol_angle_deg", "power_mw", "beam_diameter_mm", "throughput"].includes(k))) })) };
    const a = traceLayout(beamy);
    const b = traceLayout(plain);
    assert.deepEqual(
      a.map((t) => t.branches.map((x) => [x.branch, x.fraction, x.end, segGeom(x.segments), x.events.map((e) => [e.component, e.action, e.x, e.y, e.offset_mm, e.incidence_deg, e.path_mm])])),
      b.map((t) => t.branches.map((x) => [x.branch, x.fraction, x.end, segGeom(x.segments), x.events.map((e) => [e.component, e.action, e.x, e.y, e.offset_mm, e.incidence_deg, e.path_mm])])),
    );
  });

  it("segment beams are the state at the start, event beams the state on arrival", () => {
    const l = layoutOf([comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 633, beam_diameter_mm: 1, power_mw: 2 } }), comp("mirror", "m", 400, 100, 135, { props: { throughput: 0.5 } }), det("d", 400, 600, 270)]);
    const b = traceLayout(l)[0]!.branches[0]!;
    nearVec(b.segments[0]!.beam!.q, [0, 1240.755392], 5e-6, "the waist at the source (9 significant digits)");
    nearVec(b.segments[1]!.beam!.q, [400, 1240.755392], 5e-6, "after the mirror: 400 mm on");
    assert.equal(b.segments[1]!.beam!.p, 0.5, "after the mirror's reflectance");
    assert.equal(b.events[0]!.beam!.p, 1, "the mirror sees the full power arrive");
    assert.equal(b.events[0]!.beam!.mw, 2);
    assert.equal(b.events[1]!.beam!.mw, 1);
    near(b.events[1]!.beam!.d_mm!, diameterAt([0, 1240.755392413], 900, 633, 1), 1e-6);
  });
});

// ── what the editor reads off a detailed trace ────────────────────────────────

describe("reading a detailed trace", () => {
  const kepler = () =>
    layoutOf(
      [
        comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 532, beam_diameter_mm: 1, power_mw: 10 } }),
        comp("beam_splitter", "bs", 50, 100, 135, { props: { reflect_fraction: 0.5 } }),
        comp("lens", "l1", 100, 100, 0, { props: { focal_mm: 50 } }),
        comp("lens", "l2", 300, 100, 0, { props: { focal_mm: 150 } }),
        det("d", 1300, 100, 180),
        det("dr", 50, 400, 270),
      ],
      { width_mm: 3000 },
    );

  it("branchPath: from the root to the branch, ancestors first", () => {
    const tree = traceLayoutDetailed(kepler())[0]!;
    assert.deepEqual(branchPath(tree, "0.t").map((b) => b.branch), ["0", "0.t"]);
    assert.deepEqual(branchPath(tree, "0").map((b) => b.branch), ["0"]);
    assert.deepEqual(branchPath(tree, "9.9"), []);
  });

  it("beamProfile: the diameter along the path from the source, the components, the waist inside the telescope", () => {
    const tree = traceLayoutDetailed(kepler())[0]!;
    const prof = beamProfile(tree, "0.t", 16)!;
    assert.deepEqual(prof.marks.map((m) => [m.componentId, m.action]), [["bs", "split"], ["l1", "transmit"], ["l2", "transmit"], ["d", "stop"]]);
    assert.deepEqual(prof.marks.map((m) => Math.round(m.pathMm * 1000) / 1000), [50, 100, 300, 1300]);
    const xs = prof.points.map((p) => p.pathMm);
    assert.deepEqual(xs, [...xs].sort((a, b) => a - b), "in order along the path");
    near(xs[0]!, 0, 1e-12);
    near(xs[xs.length - 1]!, 1300, 1e-9);
    near(prof.points[0]!.dMm, 1, 1e-9, "the waist at the source");
    near(prof.points[prof.points.length - 1]!.dMm, 3.014325, 1e-6, "three times wider at the end");
    assert.equal(prof.waists.length, 1, "the focus between the lenses");
    near(prof.waists[0]!.pathMm, 150, 0.1);
    assert.ok(prof.points.some((p) => p.pathMm === prof.waists[0]!.pathMm && p.dMm === prof.waists[0]!.dMm), "the waist itself is a point of the curve");
    const smallest = Math.min(...prof.points.map((p) => p.dMm));
    near(smallest, prof.waists[0]!.dMm, 1e-15, "nothing on the curve is narrower than the waist");
  });

  it("beamProfile is null without a Gaussian beam", () => {
    const l = layoutOf([laser({}), det("d", 300, 100, 180)]);
    assert.equal(beamProfile(traceLayoutDetailed(l)[0]!, "0"), null);
  });

  it("beamAt: power, size and polarisation anywhere along a segment", () => {
    const tree = traceLayoutDetailed(kepler())[0]!;
    const seg = branchPath(tree, "0.t")[1]!.segments[2]!; // l2 -> d
    const at = beamAt(tree, seg, 1000)!;
    assert.equal(at.p, 0.5);
    near(at.mw!, 5, 1e-12);
    near(at.d_mm!, 3.014325, 1e-6);
    assert.deepEqual(at.s, [0, 0, 0]);
  });
});

// ── the checks of section 5 ───────────────────────────────────────────────────

describe("beam checks (section 5, with the rules of section 12)", () => {
  const ev = (offset: number, inc: number, d: number | null, action = "reflect", p = 1) => ({ action, offset_mm: offset, incidence_deg: inc, beam: { p, mw: null, d_mm: d, s: null } });

  it("a spot past the aperture: |offset| + r / cos(incidence) beyond half the aperture", () => {
    const m = comp("mirror", "m", 0, 0, 135, { aperture: 10 });
    assert.equal(spotOverflowMm(m, ev(0, 0, 10)), null, "exactly filling it is fine");
    near(spotOverflowMm(m, ev(0, 0, 10.2))!, 0.1, 1e-12);
    near(spotOverflowMm(m, ev(-4, 0, 2.5))!, 0.25, 1e-12, "off centre");
    near(spotOverflowMm(m, ev(0, 60, 6))!, 1, 1e-9, "the footprint at 60° is twice as wide");
    assert.equal(spotOverflowMm(m, ev(4.9, 89.5, 1)), null, "grazing: no check");
    near(spotOverflowMm(m, ev(4.9, 89, 0.01))!, 4.9 + 0.005 / Math.cos(89 * DEG) - 5, 1e-9, "89° itself is still judged");
    assert.equal(spotOverflowMm(m, ev(0, 0, null)), null, "no beam size: no check");
  });

  it("an iris with an opening is measured by its opening; samples, dumps and blockers are not checked", () => {
    const iris = comp("iris", "ir", 0, 0, 0, { aperture: 25.4, props: { open_mm: 4 } });
    near(spotOverflowMm(iris, ev(0, 0, 5))!, 0.5, 1e-12);
    for (const type of ["sample", "beam_dump", "blocker"] as const) assert.equal(spotOverflowMm(comp(type, "x", 0, 0, 0, { aperture: 1 }), ev(0, 0, 50)), null, type);
  });

  it("only reflect, transmit, split and stop are judged, and only light of at least 1e-6 of its source", () => {
    const m = comp("mirror", "m", 0, 0, 135, { aperture: 10 });
    for (const action of ["reflect", "transmit", "split", "stop"]) near(spotOverflowMm(m, ev(0, 0, 12, action))!, 1, 1e-12, action);
    for (const action of ["back_hit", "blocked"]) assert.equal(spotOverflowMm(m, ev(0, 0, 12, action)), null, action);
    near(spotOverflowMm(m, ev(0, 0, 12, "reflect", 1e-6))!, 1, 1e-12, "1e-6 is still light");
    assert.equal(spotOverflowMm(m, ev(0, 0, 12, "reflect", 9.9e-7)), null, "below it there is no light to clip");
    assert.equal(spotOverflowMm(m, ev(0, 0, 12, "reflect", 0)), null);
  });

  it("one per kind and component: the largest overflow, or the largest arriving power, with the number of hits", () => {
    // two lasers into one small mirror: the wider beam is the one reported
    const l = layoutOf(
      [
        comp("laser", "a", 0, 100, 0, { props: { wavelength_nm: 633, beam_diameter_mm: 11 } }),
        comp("laser", "b", 0, 100, 0, { props: { wavelength_nm: 633, beam_diameter_mm: 14 } }),
        comp("mirror", "m", 100, 100, 135, { aperture: 10 }),
        comp("detector", "d", 100, 300, 270, { aperture: 50 }),
      ],
      { width_mm: 500 },
    );
    const spot = beamChecks(l, traceLayoutDetailed(l));
    assert.deepEqual(spot.map((c) => [c.kind, c.componentId, c.source, c.beams]), [["spot_overflow", "m", "b", 2]]);
    assert.match(spot[0]!.message, /（共 2 束光）$/, "N hits, in the words of the summary");
    // two beams extinguished at one polariser: the stronger one is reported
    const x = layoutOf([
      comp("laser", "a", 0, 100, 0, { props: { wavelength_nm: 532, polarization: "linear", pol_angle_deg: 90 } }),
      comp("nd", "nd", 50, 100, 0, { props: { od: 1 } }),
      comp("laser", "b", 60, 100, 0, { props: { wavelength_nm: 532, polarization: "linear", pol_angle_deg: 90 } }),
      comp("polarizer", "pol", 100, 100, 0, { props: { axis_deg: 0 } }),
      det("d", 300, 100, 180),
    ]);
    const ext = beamChecks(x, traceLayoutDetailed(x));
    assert.deepEqual(ext.map((c) => [c.kind, c.componentId, c.source, c.p, c.beams]), [["extinguished", "pol", "b", 1, 2]]);
    assert.match(ext[0]!.message, /（共 2 束光）$/);
  });

  it("a wide beam on the back of a mirror or on a closed shutter is no spot problem; on the open shutter it is", () => {
    const layout = (closed: boolean) =>
      layoutOf(
        [
          comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 633, beam_diameter_mm: 20 } }),
          comp("mirror", "m", 150, 100, 315, { aperture: 10 }),
          comp("laser", "las2", 0, 300, 0, { props: { wavelength_nm: 633, beam_diameter_mm: 20 } }),
          comp("shutter", "sh", 150, 300, 0, { props: { closed } }),
        ],
        { width_mm: 500 },
      );
    const shut = layout(true);
    assert.deepEqual(traceLayoutDetailed(shut).map((t) => t.branches[0]!.events[0]!.action), ["back_hit", "blocked"]);
    assert.deepEqual(beamChecks(shut, traceLayoutDetailed(shut)), []);
    const open = layout(false);
    assert.deepEqual(beamChecks(open, traceLayoutDetailed(open)).map((c) => [c.kind, c.componentId]), [["spot_overflow", "sh"]]);
  });

  it("extinction is judged after a reflection too, and only for light that arrived with at least 1e-6", () => {
    const dark = layoutOf([laser({ power_mw: 1 }), comp("mirror", "m", 100, 100, 135, { props: { throughput: 0 } }), det("d", 100, 300, 270)]);
    assert.deepEqual(beamChecks(dark, traceLayoutDetailed(dark)).map((c) => [c.kind, c.componentId]), [["extinguished", "m"]]);
    const weak = (od: number) =>
      layoutOf([laser({ polarization: "linear" }), comp("nd", "nd", 50, 100, 0, { props: { od } }), comp("polarizer", "p", 100, 100, 0, { props: { axis_deg: 90 } }), det("d", 300, 100, 180)]);
    const kinds = (l: OpticalLayout) => beamChecks(l, traceLayoutDetailed(l)).map((c) => [c.kind, c.componentId]);
    assert.deepEqual(kinds(weak(5.9)), [["extinguished", "p"]], "1.3e-6 arrives: extinguished at the polariser");
    assert.deepEqual(kinds(weak(6.1)), [["extinguished", "nd"]], "OD 6.1 already leaves 7.9e-7: the ND is where the light went, not the polariser");
  });

  it("within a kind the order of the first hit in the trace, not the size of the problem", () => {
    // two small mirrors on one beam: the first overruns its aperture less than the second
    const l = layoutOf(
      [
        comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 633, beam_diameter_mm: 12 } }),
        comp("mirror", "m1", 100, 100, 135, { aperture: 16 }),
        comp("mirror", "m2", 100, 300, 315, { aperture: 10 }),
        comp("detector", "d", 400, 300, 180, { aperture: 50 }),
      ],
      { width_mm: 600 },
    );
    const checks = beamChecks(l, traceLayoutDetailed(l));
    assert.deepEqual(checks.map((c) => c.componentId), ["m1", "m2"]);
    assert.ok(checks[0]!.byMm! < checks[1]!.byMm!, "m1 first though it is the smaller problem");
  });

  it("the numbers as the trace writes them: light that rounds to 1e-6 is light, an overflow of 1e-9 or less is none", () => {
    const m = comp("mirror", "m", 0, 0, 135, { aperture: 10 });
    near(spotOverflowMm(m, ev(0, 0, 12, "reflect", 9.9999999996e-7))!, 1, 1e-12, "rounds to 1e-6");
    assert.equal(spotOverflowMm(m, ev(0, 0, 12, "reflect", 9.994e-7)), null, "rounds to 9.99e-7");
    assert.equal(spotOverflowMm(m, ev(0.0004, 0, 10, "reflect")), null, "an offset of 0.0004 mm is written 0.0");
    near(spotOverflowMm(m, ev(0.0006, 0, 10, "reflect"))!, 0.001, 1e-12, "and 0.0006 mm is written 0.001");
  });

  it("two beams over by half a mm and two mm give one line with the two (the server has a test of the same example)", () => {
    const l = layoutOf(
      [
        comp("laser", "la", 100, 100, 0, { props: { wavelength_nm: 633, beam_diameter_mm: 26.4 } }),
        comp("laser", "lb", 100, 101.5, 0, { props: { wavelength_nm: 633, beam_diameter_mm: 26.4 } }),
        comp("laser", "lc", 500, 100, 180, { props: { wavelength_nm: 633, beam_diameter_mm: 40 } }),
        comp("laser", "ld", 250, 150, 315, { props: { wavelength_nm: 633, beam_diameter_mm: 30, polarization: "linear" } }),
        comp("polarizer", "pd", 275, 125, 315, { aperture: 40, props: { axis_deg: 90 } }),
        comp("mirror", "m1", 300, 100, 180),
      ],
      { width_mm: 1200, height_mm: 900 },
    );
    const trace = traceLayoutDetailed(l);
    // the four hits on M1: la over by 0.5, lb by 2.0 (1.5 off centre), lc on the back, ld with no light left
    const onM1 = trace.flatMap((t) => t.branches.flatMap((b) => b.events.filter((e) => e.component === "m1").map((e) => [t.source, e.action, Math.round(e.offset_mm * 10) / 10 + 0, e.beam!.p])));
    assert.deepEqual(onM1, [["la", "reflect", 0, 1], ["lb", "reflect", -1.5, 1], ["lc", "back_hit", 0, 1], ["ld", "reflect", 0, 0]]);
    const m1 = l.components.find((c) => c.id === "m1")!;
    const over = trace.flatMap((t) => t.branches.flatMap((b) => b.events.filter((e) => e.component === "m1").map((e) => spotOverflowMm(m1, e))));
    assert.deepEqual(over.map((x) => (x === null ? null : Math.round(x * 100) / 100)), [0.5, 2, null, null], "lc's back and ld's dark hit are not judged");
    const checks = beamChecks(l, trace);
    assert.deepEqual(checks.map((c) => [c.kind, c.componentId, c.source, c.beams, Math.round((c.byMm ?? 0) * 100) / 100]), [
      ["spot_overflow", "m1", "lb", 2, 2],
      ["extinguished", "pd", "ld", 1, 0],
    ]);
    assert.equal(checks[0]!.message, "M1：光斑超出口径 2.00 mm（lb，633 nm，Ø26.40 mm）（共 2 束光）");
    assert.equal(checks[1]!.message, "PD 之后几乎没有光（偏振）");
  });

  it("a waveplate without retardance that no beam hits has no note", () => {
    const l = layoutOf([laser({}), comp("waveplate", "w", 100, 300, 0, { props: {} }), det("d", 300, 100, 180)]);
    assert.deepEqual(beamChecks(l, traceLayoutDetailed(l)), []);
  });

  it("a hit after the light is gone is not judged", () => {
    const l = layoutOf([
      comp("laser", "las1", 0, 100, 0, { props: { wavelength_nm: 633, beam_diameter_mm: 20, polarization: "linear" } }),
      comp("polarizer", "p1", 50, 100, 0, { aperture: 50, props: { axis_deg: 90 } }),
      comp("window", "w", 150, 100, 0, { aperture: 10 }),
    ]);
    const checks = beamChecks(l, traceLayoutDetailed(l));
    assert.deepEqual(checks.map((c) => [c.kind, c.componentId]), [["extinguished", "p1"]], "the window's spot would overrun it, but no light arrives");
  });

  it("the demo shows each: the probe's spot past the off-centre objective, light extinguished by the crossed analyzer", () => {
    const l = demo();
    const checks = beamChecks(l, traceLayoutDetailed(l));
    assert.deepEqual(checks.map((c) => [c.kind, c.componentId]), [["spot_overflow", "obj1"], ["extinguished", "pol1"]]);
    assert.equal(checks[0]!.source, "las2", "the wide probe, not the pump");
    assert.ok(checks[0]!.byMm! > 0.1 && checks[0]!.byMm! < 0.5, String(checks[0]!.byMm));
    assert.match(checks[1]!.message, /几乎没有光（偏振）/);
    const dark = layoutOf([laser({ power_mw: 1 }), comp("window", "w", 100, 100, 0, { props: { throughput: 0 } }), det("d", 300, 100, 180)]);
    assert.match(beamChecks(dark, traceLayoutDetailed(dark))[0]!.message, /几乎没有光（通过率）/, "the server's word for throughput");
  });

  it("a waveplate without retardance is a note, once per waveplate", () => {
    const l = layoutOf([laser({ wavelengths_nm: [532, 633] }), comp("waveplate", "w", 100, 100, 0, { props: {} }), det("d", 300, 100, 180)]);
    delete l.components[0]!.props["wavelength_nm"];
    const checks = beamChecks(l, traceLayoutDetailed(l));
    assert.deepEqual(checks.map((c) => [c.kind, c.componentId, c.level, c.beams]), [["no_retardance", "w", "info", 2]]);
    assert.doesNotMatch(checks[0]!.message, /共/, "the waveplate note is one line per component, without a count");
  });

  it("extinction is a drop below 1e-6 at one action, not light that was already gone", () => {
    const l = layoutOf([laser({ polarization: "linear" }), comp("polarizer", "p1", 100, 100, 0, { props: { axis_deg: 90 } }), comp("polarizer", "p2", 200, 100, 0, { props: { axis_deg: 0 } }), det("d", 300, 100, 180)]);
    const checks = beamChecks(l, traceLayoutDetailed(l));
    assert.deepEqual(checks.map((c) => [c.kind, c.componentId]), [["extinguished", "p1"]]);
    assert.ok(EXTINCTION_P === 1e-6);
  });

  it("the order of the summary (section 12.3.6): cut, back, off centre, (blocked), near miss, spot, extinguished, left the table, unlit, 64, fibre, waveplate notes", () => {
    // the 3D contract, section 6.3, adds off_height, height_miss and spot_height (only those three)
    assert.deepEqual([...CHECK_ORDER], ["trace_cut", "back_hit", "off_center", "off_height", "blocked", "miss", "height_miss", "spot_overflow", "spot_height", "extinguished", "left_table", "unlit", "max_events", "fibre", "no_retardance"]);
    const items = ["no_retardance", "unlit", "spot_overflow", "left_table", "something_new", "miss", "extinguished", "back_hit", "max_events", "off_center", "trace_cut"].map((kind, i) => ({ kind, i }));
    assert.deepEqual(orderChecks(items).map((x) => x.kind), ["trace_cut", "back_hit", "off_center", "miss", "spot_overflow", "extinguished", "left_table", "unlit", "max_events", "something_new", "no_retardance"]);
    const same = [{ kind: "unlit", i: 0 }, { kind: "back_hit", i: 1 }, { kind: "unlit", i: 2 }];
    assert.deepEqual(orderChecks(same).map((x) => x.i), [1, 0, 2], "within a kind the order they came in");
  });

  it("a trace without beam data (an older server) gives no checks", () => {
    const l = demo();
    const stripped = traceLayoutDetailed(l).map((t) => ({ ...t, branches: t.branches.map((b) => ({ ...b, segments: segGeom(b.segments), events: b.events.map(({ beam: _b, ...e }) => e) })) }));
    assert.deepEqual(beamChecks(l, stripped), []);
  });
});

// ── the demo of the dev mock ──────────────────────────────────────────────────

describe("the dev mock's demo layout shows the new physics", () => {
  const trees = () => traceLayout(demo());
  const tree = (source: string) => trees().find((t) => t.source === source)!;

  it("the pump: λ/2 at 15° turns p to 30°, BS1 picks off 30 %, the PBS dumps the s quarter, M2 and the objective take 2 % and 20 %", () => {
    const t = tree("las1");
    assert.deepEqual(t.beam, { source_mw: 50, m2: 1 });
    const byBranch = new Map(t.branches.map((b) => [b.branch, b]));
    const pbsHit = byBranch.get("0.t")!.events[0]!;
    assert.equal(pbsHit.component, "pbs1");
    near(pbsHit.beam!.mw!, 35, 1e-6, "50 mW x 0.7");
    near(psiOf(pbsHit.beam!.s), 30, 1e-6);
    const dump = byBranch.get("0.t.r")!;
    assert.equal(dump.events.at(-1)!.component, "bd1");
    near(dump.events.at(-1)!.beam!.mw!, 8.75, 1e-6, "35 mW x sin² 30°");
    const on = byBranch.get("0.t.t")!;
    const atSample = on.events.at(-1)!;
    assert.equal(atSample.component, "smp1");
    near(atSample.beam!.mw!, 26.25 * 0.98 * 0.8, 1e-6);
    assert.deepEqual(on.segments[0]!.beam!.s, [1, 0, 0], "p after the PBS");
  });

  it("the pickoff: BS1 reflects the 30° light as 150°, the λ/4 at 0° makes it elliptical, ψ 0°, χ -30°", () => {
    const pick = tree("las1").branches.find((b) => b.branch === "0.r")!;
    assert.deepEqual(pick.events.map((e) => e.component), ["qwp1", "det1"]);
    near(psiOf(pick.events[0]!.beam!.s), 150, 1e-6);
    const r = readPolarization(pick.events[1]!.beam!.s);
    assert.equal(r.kind, "elliptical");
    near(r.psi_deg, 0, 1e-6);
    near(r.chi_deg, -30, 1e-6);
    assert.deepEqual(polarizationTexts(pick.events[1]!.beam!.s), { zh: "椭圆偏 0°、χ = -30°", en: "elliptical 0°, χ = -30°" });
  });

  it("the sample sits in the focus of the objective: both beams have a waist in that segment and a spot of µm on the sample", () => {
    const l = demo();
    const smp = l.components.find((c) => c.id === "smp1")!;
    const obj = l.components.find((c) => c.id === "obj1")!;
    assert.equal(obj.props["focal_mm"], 20);
    for (const t of traceLayoutDetailed(l).filter((x) => x.source === "las1" || x.source === "las2")) {
      const b = t.branches.find((x) => x.events.some((e) => e.component === "smp1"))!;
      const i = b.events.findIndex((e) => e.component === "obj1");
      const seg = b.segments[i + 1]!;
      const len = Math.hypot(seg.x1 - seg.x0, seg.y1 - seg.y0);
      near(len, smp.x_mm - obj.x_mm, 1e-9, "from the objective straight to the sample");
      const w = waistInSegment(seg.beam!.q, len);
      assert.ok(w !== null && w <= len, `${t.source}: the waist is inside the segment (a focus line in the narrative)`);
      const atSample = b.events[i + 1]!;
      assert.equal(atSample.component, "smp1");
      assert.ok(atSample.beam!.d_mm! < 0.02, `${t.source}: ${atSample.beam!.d_mm} mm on the sample`);
    }
  });

  it("the pump's Galilean telescope (afocal, 2x) makes the waist twice as wide: zR four times as long", () => {
    const t = tree("las1");
    const on = t.branches.find((b) => b.branch === "0.t.t")!;
    const i = on.events.findIndex((e) => e.component === "len3");
    const zR0 = t.branches[0]!.segments[0]!.beam!.q![1];
    const zR1 = on.segments[i + 1]!.beam!.q![1];
    near(zR1 / zR0, 4, 1e-6);
  });

  it("the fibre: about Ø17 mm at the collimator, focused into the fibre input, 60 % coupled, then crossed", () => {
    const fo1 = tree("fo1").branches[0]!;
    const atL1 = fo1.events.find((e) => e.component === "len1")!.beam!.d_mm!;
    near(atL1, 16.93, 0.05);
    assert.ok(fo1.events.at(-1)!.beam!.d_mm! < 0.01, "a focus at the fibre input");
    const fo2 = tree("fo2");
    near(fo2.beam!.source_mw!, 3, 1e-9, "5 mW x 0.6");
    const cam = fo2.branches[0]!.events.at(-1)!;
    assert.equal(cam.component, "cam1");
    assert.equal(cam.beam!.p, 0);
    assert.equal(cam.beam!.s, null);
  });
});
