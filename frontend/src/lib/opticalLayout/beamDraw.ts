// What the canvas draws of the light (docs/v2/design/optical_layout_v2.md sections 8.2 to 8.4), as data in
// screen pixels: the beam lines (width and opacity by power, "no light" dotted grey), the 1/e² envelope
// band at its real size, the tick of every waist, a few direction arrows, the polarisation marks, and which
// beam is under the pointer.
//
// No physics is written here. The beam size comes from beam.ts (diameterOf, waistInSegment,
// waistDiameterMm: the formulas of section 4.4, the same ones the tracer and the inspector use), the
// polarisation from readPolarization (the rules of section 5), and the look of a power from colors.ts.
// This file only decides where things go on the screen. Pure, so it is tested without a browser
// (test/opticalLayoutBeamDraw.test.ts).

import type { BranchEnd, QParam, Segment, StokesNorm, Vec } from "./types.ts";
import type { DetailedBranch, DetailedTrace } from "./trace.ts";
import { diameterOf, powerTexts, readPolarization, waistDiameterMm, waistInSegment, type PolReading } from "./beam.ts";
import { DARK_P, beamLook, type BeamLook, type ThemeName } from "./colors.ts";
import type { View } from "./view.ts";

/** A segment of a trace: tree index, branch id, index of the segment in the branch. */
export interface SegRef {
  tree: number;
  branch: string;
  segment: number;
}

export const segKey = (r: SegRef): string => `${r.tree}|${r.branch}|${r.segment}`;

/** Consecutive segments of one branch that look the same: one path. */
export interface CoreRun {
  key: string;
  tree: number;
  branch: string;
  /** SVG path data, screen px */
  d: string;
  look: BeamLook;
}

/**
 * The 1/e² envelope of one segment: a closed polygon in TABLE millimetres (y up). The canvas draws the bands in a
 * group that carries the view's transform, so a pan or a zoom does not rebuild them, and a segment that did not
 * change between two frames of a drag gives the very same string (bandPolygonMm keeps them): only the paths of
 * what moved are redrawn.
 */
export interface Band {
  key: string;
  d: string;
  color: string;
  opacity: number;
}

/** Where a segment has its waist: a tick across the band. */
export interface WaistTick {
  key: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  color: string;
  /** the waist's 1/e² diameter, mm */
  dMm: number;
  /** the waist on the screen */
  at: Vec;
}

export interface Arrow {
  key: string;
  x: number;
  y: number;
  /** direction of travel on the screen, degrees (screen y down) */
  deg: number;
  color: string;
  opacity: number;
}

/**
 * A polarisation mark on a segment (section 8.3), at `x, y` on the beam. "p": the field lies in the table plane, a
 * double arrow across the beam; "s": the field is vertical, dots on the beam; "unpolarized": both; "badge": any
 * other state, a small disc beside the beam at `bx, by` with the ellipse seen against the light (right = e_h, the
 * left of the direction of travel; up = up).
 */
export interface PolMark {
  key: string;
  kind: "p" | "s" | "unpolarized" | "badge";
  x: number;
  y: number;
  /** direction of travel on the screen, degrees */
  deg: number;
  bx: number;
  by: number;
  reading: PolReading;
}

/** Where a branch ends, for the end markers. */
export interface BranchEndMark {
  key: string;
  end: BranchEnd;
  at: Vec;
  look: BeamLook;
}

export interface BeamScene {
  cores: CoreRun[];
  bands: Band[];
  waists: WaistTick[];
  arrows: Arrow[];
  pols: PolMark[];
  ends: BranchEndMark[];
}

export interface SceneOptions {
  envelope: boolean;
  polarization: boolean;
  /** screen rectangle worth drawing marks in (bands, ticks, arrows, polarisation); everything when absent */
  clip?: { x0: number; y0: number; x1: number; y1: number };
}

/** A band narrower than this (full width, px) is not drawn: the beam line says as much. */
export const BAND_MIN_PX = 1.5;
/** A direction arrow on every segment at least this long on the screen. */
export const ARROW_MIN_PX = 90;
/** A polarisation mark on every segment at least this long on the screen, this far along it. */
export const POL_MIN_PX = 64;
export const POL_AT = 0.28;
/** How far beside the beam a polarisation badge sits (centre, px). */
export const BADGE_OFFSET_PX = 15;
/** The opacity of an envelope band, times the opacity of its beam. */
export const BAND_OPACITY = 0.2;
/** Samples of a band between its ends, at most (they are spread evenly in the Gouy angle, so they gather at a waist). */
export const BAND_MAX_SAMPLES = 48;

const fmt = (v: number) => (Math.round(v * 100) / 100).toString();

function sameLook(a: BeamLook, b: BeamLook): boolean {
  return a.color === b.color && a.dash === b.dash && a.width === b.width && a.opacity === b.opacity && a.dark === b.dark;
}

const segLen = (g: Segment) => Math.hypot(g.x1 - g.x0, g.y1 - g.y0);

/** The power at the start of a segment: its beam's, or (a trace without beam data) its branch's share. */
export function segmentPower(br: DetailedBranch, g: Segment): number {
  return g.beam ? g.beam.p : br.fraction;
}

/**
 * The distances (mm from the start of a segment of length `L` whose beam starts with parameter q) where a band
 * is sampled: spread evenly in θ = atan(z / zR), so they are dense where the size changes fast (at a waist) and
 * sparse where it does not; both ends always, and the waist itself when the segment has one.
 */
export function bandSamples(q: QParam, L: number, maxSamples = BAND_MAX_SAMPLES): number[] {
  const [z0, zR] = q;
  const t0 = Math.atan(z0 / zR);
  const t1 = Math.atan((z0 + L) / zR);
  const n = Math.max(2, Math.min(maxSamples, Math.ceil(Math.abs(t1 - t0) / 0.05) + 2));
  const out: number[] = [];
  for (let i = 0; i <= n; i++) {
    const s = i === 0 ? 0 : i === n ? L : zR * Math.tan(t0 + ((t1 - t0) * i) / n) - z0;
    out.push(Math.min(L, Math.max(0, s)));
  }
  // beam.ts also reports a waist up to 0.001 mm past the end of the segment (contract section 12): keep it inside
  const w = waistInSegment(q, L);
  if (w !== null) out.push(Math.min(L, Math.max(0, w)));
  return [...new Set(out)].sort((a, b) => a - b);
}

/** Millimetres to 0.1 µm, enough for a focus of a few µm at the deepest zoom. */
const fmtMm = (v: number) => (Math.round(v * 10000) / 10000).toString();

// Bands already worked out, by everything they depend on (the segment, q, the wavelength, M²), each with those eight
// numbers: a drag re-traces the whole layout every frame, but only the beams of what moved come out different.
// Two generations: the bands the scene being made has asked for, and those the scene before asked for; a band
// asked for again moves to the new generation, the rest goes with the old one (beamScene turns them over). So the
// cache holds what the last picture drew, however long the drag, and a band is found again by its numbers bit for
// bit (a hash of their bytes, then compared exactly), without writing eight numbers into a string for a key.
type BandOut = { d: string; maxHalfMm: number } | null;
interface BandEntry {
  v: Float64Array;
  out: BandOut;
}
let bandsNow = new Map<number, BandEntry[]>();
let bandsBefore = new Map<number, BandEntry[]>();
const KEY = new Float64Array(8);
const KEY_WORDS = new Uint32Array(KEY.buffer);

function keyHash(): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < KEY_WORDS.length; i++) h = Math.imul(h ^ KEY_WORDS[i]!, 0x01000193);
  return h >>> 0;
}
function find(m: Map<number, BandEntry[]>, h: number): BandEntry | undefined {
  const list = m.get(h);
  if (!list) return undefined;
  for (const e of list) {
    let same = true;
    for (let i = 0; i < 8; i++) {
      if (!Object.is(e.v[i], KEY[i])) {
        same = false;
        break;
      }
    }
    if (same) return e;
  }
  return undefined;
}
function keep(h: number, e: BandEntry) {
  const list = bandsNow.get(h);
  if (list) list.push(e);
  else bandsNow.set(h, [e]);
}

/** A new picture: the bands the last one asked for become the old generation (see above). */
export function turnBandCache(): void {
  bandsBefore = bandsNow;
  bandsNow = new Map();
}

/**
 * The band of one segment in table millimetres (y up): the closed polygon between the two 1/e² edges (the left one
 * forward, the right one back), and its largest half width; null when the beam has no finite size here. Kept (see
 * above): the same segment with the same beam gives the very same string, whatever the view.
 */
export function bandPolygonMm(g: Segment, q: QParam, wl: number, m2: number): BandOut {
  KEY[0] = g.x0;
  KEY[1] = g.y0;
  KEY[2] = g.x1;
  KEY[3] = g.y1;
  KEY[4] = q[0];
  KEY[5] = q[1];
  KEY[6] = wl;
  KEY[7] = m2;
  const h = keyHash();
  const now = find(bandsNow, h);
  if (now) return now.out;
  const before = find(bandsBefore, h);
  if (before) {
    keep(h, before);
    return before.out;
  }
  const e = { v: KEY.slice(), out: bandPolygonMmUncached(g, q, wl, m2) };
  keep(h, e);
  return e.out;
}

function bandPolygonMmUncached(g: Segment, q: QParam, wl: number, m2: number): { d: string; maxHalfMm: number } | null {
  const L = segLen(g);
  if (!(L > 0)) return null;
  const ux = (g.x1 - g.x0) / L;
  const uy = (g.y1 - g.y0) / L;
  // the left of the direction of travel, in table coordinates (y up)
  const nx = -uy;
  const ny = ux;
  const left: string[] = [];
  const right: string[] = [];
  let maxHalf = 0;
  for (const s of bandSamples(q, L)) {
    const half = diameterOf([q[0] + s, q[1]], wl, m2) / 2;
    if (!Number.isFinite(half)) return null;
    maxHalf = Math.max(maxHalf, half);
    const cx = g.x0 + ux * s;
    const cy = g.y0 + uy * s;
    left.push(`${fmtMm(cx + nx * half)} ${fmtMm(cy + ny * half)}`);
    right.push(`${fmtMm(cx - nx * half)} ${fmtMm(cy - ny * half)}`);
  }
  return { d: `M${left.join("L")}L${right.reverse().join("L")}Z`, maxHalfMm: maxHalf };
}

/**
 * The side a badge goes to: the normal of the beam that points down the screen (left, for an upright beam), away
 * from the alignment read-outs, which sit above and to the right of a hit.
 */
export function badgeSide(ux: number, uy: number): { nx: number; ny: number } {
  let nx = -uy;
  let ny = ux;
  if (Math.abs(ny) < 1e-6 ? nx > 0 : ny < 0) {
    nx = -nx;
    ny = -ny;
  }
  return { nx: nx + 0, ny: ny + 0 };
}

/** Which mark a polarisation gets (section 8.3); null for no light. */
export function polMarkKind(r: PolReading): PolMark["kind"] | null {
  if (r.kind === "none") return null;
  if (r.kind === "unpolarized") return "unpolarized";
  if (r.kind === "linear" && r.ps === "p") return "p";
  if (r.kind === "linear" && r.ps === "s") return "s";
  return "badge";
}

function inside(clip: SceneOptions["clip"], x: number, y: number, margin: number): boolean {
  if (!clip) return true;
  return x >= clip.x0 - margin && x <= clip.x1 + margin && y >= clip.y0 - margin && y <= clip.y1 + margin;
}

/**
 * Everything the canvas draws of the light, for one view. It runs on every frame of a drag (the trace changes), so
 * it allocates only what it hands out: no point objects, the keys only of the marks that are made.
 */
export function beamScene(trace: DetailedTrace, view: View, theme: ThemeName, opts: SceneOptions): BeamScene {
  const scene: BeamScene = { cores: [], bands: [], waists: [], arrows: [], pols: [], ends: [] };
  const { k, ox, oy } = view;
  if (opts.envelope) turnBandCache();
  for (let ti = 0; ti < trace.length; ti++) {
    const tree = trace[ti]!;
    const wl = tree.wavelength_nm;
    const m2 = tree.beam?.m2 ?? null;
    const looks = new Map<number, BeamLook>();
    const lookOf = (p: number): BeamLook => {
      let l = looks.get(p);
      if (!l) {
        l = beamLook(wl, p, theme);
        looks.set(p, l);
      }
      return l;
    };
    for (const br of tree.branches) {
      const segs = br.segments;
      if (segs.length === 0) continue;
      let runLook: BeamLook | null = null;
      let runParts: string[] = [];
      let runFirst = 0;
      const flush = () => {
        if (runLook) scene.cores.push({ key: `${ti}|${br.branch}|${runFirst}`, tree: ti, branch: br.branch, d: runParts.join(""), look: runLook });
      };
      for (let si = 0; si < segs.length; si++) {
        const g = segs[si]!;
        const p = segmentPower(br, g);
        const look = lookOf(p);
        const ax = ox + g.x0 * k;
        const ay = oy - g.y0 * k;
        const bx = ox + g.x1 * k;
        const by = oy - g.y1 * k;
        if (runLook !== null && (runLook === look || sameLook(runLook, look))) runParts.push(`L${fmt(bx)} ${fmt(by)}`);
        else {
          flush();
          runLook = look;
          runParts = [`M${fmt(ax)} ${fmt(ay)}L${fmt(bx)} ${fmt(by)}`];
          runFirst = si;
        }
        const lenPx = Math.hypot(bx - ax, by - ay);
        if (!(lenPx > 0)) continue;
        const midX = (ax + bx) / 2;
        const midY = (ay + by) / 2;
        if (!inside(opts.clip, midX, midY, lenPx / 2 + 40)) continue;
        const q = g.beam?.q ?? null;
        const wantBand = opts.envelope && q !== null && wl !== null && m2 !== null && !look.dark;
        const wantArrow = !look.dark && lenPx >= ARROW_MIN_PX;
        const wantPol = !look.dark && opts.polarization && lenPx >= POL_MIN_PX && g.beam !== undefined && g.beam.s !== null && p >= DARK_P;
        if (!wantBand && !wantArrow && !wantPol) continue;
        const ux = (bx - ax) / lenPx;
        const uy = (by - ay) / lenPx;
        const key = `${ti}|${br.branch}|${si}`;
        // envelope and waist
        if (wantBand && q !== null && wl !== null && m2 !== null) {
          const band = bandPolygonMm(g, q, wl, m2);
          if (band && band.maxHalfMm * 2 * k >= BAND_MIN_PX) scene.bands.push({ key, d: band.d, color: look.color, opacity: Math.round(BAND_OPACITY * look.opacity * 1000) / 1000 });
          const L = segLen(g);
          const w0 = waistInSegment(q, L);
          // a waist reported just past the end of the segment (up to 0.001 mm, section 12) is drawn at its end
          const w = w0 === null ? null : Math.min(L, Math.max(0, w0));
          if (w !== null && lenPx >= 6) {
            const dMm = waistDiameterMm(q, wl, m2);
            const cx = ax + ux * w * k;
            const cy = ay + uy * w * k;
            const half = Math.max((dMm / 2) * k + 5, 6);
            scene.waists.push({ key, x1: cx - uy * half, y1: cy + ux * half, x2: cx + uy * half, y2: cy - ux * half, color: look.color, dMm, at: { x: cx, y: cy } });
          }
        }
        if (wantArrow) scene.arrows.push({ key, x: midX, y: midY, deg: (Math.atan2(uy, ux) * 180) / Math.PI, color: look.color, opacity: look.opacity });
        // polarisation
        if (wantPol && g.beam && g.beam.s !== null) {
          const reading = readPolarization(g.beam.s as StokesNorm);
          const kind = polMarkKind(reading);
          if (kind) {
            const x = ax + ux * lenPx * POL_AT;
            const y = ay + uy * lenPx * POL_AT;
            const side = badgeSide(ux, uy);
            scene.pols.push({ key, kind, x, y, deg: (Math.atan2(uy, ux) * 180) / Math.PI, bx: x + side.nx * BADGE_OFFSET_PX, by: y + side.ny * BADGE_OFFSET_PX, reading });
          }
        }
      }
      flush();
      const last = segs[segs.length - 1]!;
      scene.ends.push({ key: `${ti}|${br.branch}`, end: br.end, at: { x: ox + last.x1 * k, y: oy - last.y1 * k }, look: lookOf(segmentPower(br, last)) });
    }
  }
  return scene;
}

// ── where the beams run, for the labels to keep clear of ─────────────────────

/**
 * Screen rectangles that cover every beam (padded by `pad` px): one per level or upright segment, small squares
 * along an oblique one. At most `max` of them (the rest of the beams are then not avoided).
 */
export function beamObstacles(trace: DetailedTrace, view: View, pad = 3, max = 3000): Array<{ x: number; y: number; w: number; h: number }> {
  const out: Array<{ x: number; y: number; w: number; h: number }> = [];
  const { k, ox, oy } = view;
  for (const tree of trace) {
    for (const br of tree.branches) {
      for (const g of br.segments) {
        if (out.length >= max) return out;
        const ax = ox + g.x0 * k;
        const ay = oy - g.y0 * k;
        const bx = ox + g.x1 * k;
        const by = oy - g.y1 * k;
        if (Math.abs(ax - bx) < 1 || Math.abs(ay - by) < 1) {
          out.push({ x: Math.min(ax, bx) - pad, y: Math.min(ay, by) - pad, w: Math.abs(bx - ax) + 2 * pad, h: Math.abs(by - ay) + 2 * pad });
          continue;
        }
        const len = Math.hypot(bx - ax, by - ay);
        // n + 1 squares, the last one at the end of the segment, never past the cap
        const n = Math.min(max - out.length - 1, Math.ceil(len / (2 * pad + 2)));
        if (n < 0) return out;
        for (let i = 0; i <= n; i++) {
          const x = ax + ((bx - ax) * i) / Math.max(1, n);
          const y = ay + ((by - ay) * i) / Math.max(1, n);
          out.push({ x: x - pad, y: y - pad, w: 2 * pad, h: 2 * pad });
        }
      }
    }
  }
  return out;
}

// ── power where the light ends ────────────────────────────────────────────────

/** The power arriving where a beam ends (section 8.5, "power labels"): one tag per beam per component. */
export interface PowerTag {
  key: string;
  componentId: string;
  /** the hit point, mm */
  at: Vec;
  /** "37.5 mW" or "24.6 % relative" (beam.ts powerTexts) */
  zh: string;
  en: string;
  color: string;
  /** 0 for the first beam ending on this component, 1 for the next, ...: they stack */
  slot: number;
}

/**
 * The power of every beam that ends on the front of a component (a branch whose end is "stop": detectors,
 * samples, dumps, fibre inputs), as the beam arrives there. A trace without beam data has none.
 */
export function powerTags(trace: DetailedTrace, theme: ThemeName): PowerTag[] {
  const out: PowerTag[] = [];
  const slots = new Map<string, number>();
  trace.forEach((tree, ti) => {
    const color = beamLook(tree.wavelength_nm, 1, theme).color;
    for (const br of tree.branches) {
      if (br.end !== "stop") continue;
      const ev = br.events[br.events.length - 1];
      if (!ev || !ev.beam) continue;
      const slot = slots.get(ev.component) ?? 0;
      slots.set(ev.component, slot + 1);
      const t = powerTexts(ev.beam.p, ev.beam.mw);
      out.push({ key: `${ti}|${br.branch}`, componentId: ev.component, at: { x: ev.pos.x, y: ev.pos.y }, zh: t.zh, en: t.en, color, slot });
    }
  });
  return out;
}

// ── which beam is under the pointer ───────────────────────────────────────────

export interface SegmentHit extends SegRef {
  /** distance from the start of the segment to the point nearest the pointer, mm */
  along: number;
  /** distance from the pointer to the segment, mm */
  dist: number;
  /** the point on the segment, mm */
  at: Vec;
}

/**
 * The segment nearest to `p` within `maxMm` (null if none). Of segments at the same distance (beams that share a
 * path), the one with the most light wins, then the earlier one; light that is not there loses to light that is.
 */
export function nearestSegment(trace: DetailedTrace, p: Vec, maxMm: number): SegmentHit | null {
  let best: SegmentHit | null = null;
  let bestP = -1;
  trace.forEach((tree, ti) => {
    for (const br of tree.branches) {
      br.segments.forEach((g, si) => {
        const dx = g.x1 - g.x0;
        const dy = g.y1 - g.y0;
        const L2 = dx * dx + dy * dy;
        let t = L2 > 0 ? ((p.x - g.x0) * dx + (p.y - g.y0) * dy) / L2 : 0;
        t = Math.min(1, Math.max(0, t));
        const x = g.x0 + dx * t;
        const y = g.y0 + dy * t;
        const dist = Math.hypot(p.x - x, p.y - y);
        if (dist > maxMm) return;
        const power = segmentPower(br, g);
        const closer = best === null || dist < best.dist - 1e-9;
        const tie = best !== null && Math.abs(dist - best.dist) <= 1e-9;
        if (closer || (tie && power > bestP)) {
          best = { tree: ti, branch: br.branch, segment: si, along: Math.hypot(dx, dy) * t, dist, at: { x, y } };
          bestP = power;
        }
      });
    }
  });
  return best;
}

/** The segment a reference names in a trace, if the trace still has it. */
export function segmentOf(trace: DetailedTrace, r: SegRef): { tree: DetailedTrace[number]; branch: DetailedBranch; segment: Segment } | null {
  const tree = trace[r.tree];
  const branch = tree?.branches.find((b) => b.branch === r.branch);
  const segment = branch?.segments[r.segment];
  return tree && branch && segment ? { tree, branch, segment } : null;
}

/** A segment named by what does not change when a component moves a little: the source, the wavelength and the branch. */
export interface BeamPick {
  source: string;
  wavelength_nm: number | null;
  branch: string;
  segment: number;
}

export function pickOf(trace: DetailedTrace, r: SegRef): BeamPick | null {
  const t = trace[r.tree];
  return t ? { source: t.source, wavelength_nm: t.wavelength_nm, branch: r.branch, segment: r.segment } : null;
}

/** Where a pick is in a (new) trace: same source, wavelength and branch, the segment if it still exists (else the last). */
export function refOf(trace: DetailedTrace, pick: BeamPick): SegRef | null {
  const ti = trace.findIndex((t) => t.source === pick.source && t.wavelength_nm === pick.wavelength_nm);
  if (ti < 0) return null;
  const br = trace[ti]!.branches.find((b) => b.branch === pick.branch);
  if (!br || br.segments.length === 0) return null;
  return { tree: ti, branch: pick.branch, segment: Math.min(pick.segment, br.segments.length - 1) };
}
