// ════════════════════════════════════════════════════════════════════════════
// Scan-map time filter — which recorded operations and saved images fall inside
// the time window the operator picked.
//
// Pure, for the same reason as scanMapView.ts: every way this can go wrong is
// silent. A window that drops undated markers makes work vanish; a window
// anchored the wrong way hides the newest action; a subtraction that goes
// negative after a clock step empties the map. None of those throw.
//
// Timestamps arrive in two dialects:
//   • marker `timestamp` — the instrument PC's wall clock WITH the UTC offset
//     in force on that date ("2026-07-01T12:00:00.123456+02:00"): the backend
//     turns storage's naive `datetime.now().isoformat()` into that per stamp,
//     so a marker from the other half of a daylight-saving year carries its own
//     offset. An older backend sends the naive stamp, read
//     here as the browser's local time — right whenever the two share a zone;
//   • image `mtime` — ISO 8601 UTC with an explicit "Z".
// Both become epoch milliseconds here. A stamp that does not parse is UNDATED:
// always drawn, and counted so the time bar can say how many there are.
//
// No imports from ../api/schema, so `node --test` runs this without a build.
// ════════════════════════════════════════════════════════════════════════════

import { tr } from "./i18n.ts";

/** What the operator picked. */
export type TimeFilter =
  | { kind: "all" }
  | { kind: "recent"; hours: number } // the last N hours of the wall clock
  | { kind: "today" } // since local midnight
  | { kind: "range"; from: number | null; to: number | null }; // epoch ms; null = open end

/** A resolved window in epoch ms. `null` on either side means unbounded. */
export interface TimeWindow {
  from: number | null;
  to: number | null;
}

export const ALL_TIME: TimeWindow = { from: null, to: null };

export interface TimePreset {
  id: "all" | "1h" | "3h" | "today";
  label: string;
  filter: TimeFilter;
}

export const TIME_PRESETS: TimePreset[] = [
  { id: "all", label: tr("全部", "All"), filter: { kind: "all" } },
  { id: "1h", label: tr("最近 1 h", "Last 1 h"), filter: { kind: "recent", hours: 1 } },
  { id: "3h", label: tr("3 h", "3 h"), filter: { kind: "recent", hours: 3 } },
  { id: "today", label: tr("今天", "Today"), filter: { kind: "today" } },
];

/** The preset a filter IS, or null for a hand-set range. */
export function presetOf(f: TimeFilter): TimePreset["id"] | null {
  if (f.kind === "all") return "all";
  if (f.kind === "today") return "today";
  if (f.kind === "recent") return f.hours === 1 ? "1h" : f.hours === 3 ? "3h" : null;
  // A range with both ends open is "all" in effect, and should light that chip.
  if (f.from == null && f.to == null) return "all";
  return null;
}

// ── parsing ────────────────────────────────────────────────────────────────

const STAMP_RE =
  /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$/i;

/**
 * Epoch milliseconds for an ISO-8601-ish timestamp, or `null` when there is no
 * usable time in it.
 *
 * Parsed by hand rather than by `Date.parse`, whose handling of microseconds,
 * a space separator and impossible dates differs between engines — and an
 * engine that rolls "2026-02-31" over to 3 March would file a corrupt stamp as
 * a real one. Anything that does not round-trip is undated.
 *
 * A stamp without a zone is read as browser-local: no offset is applied from
 * outside. One "current offset" for every stamp is exactly what is wrong in a
 * zone with daylight saving; the backend sends each marker with its own.
 */
export function parseStamp(s: unknown): number | null {
  if (typeof s !== "string") return null;
  const m = STAMP_RE.exec(s.trim());
  if (!m) return null;
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const d = Number(m[3]);
  const hh = Number(m[4]);
  const mi = Number(m[5]);
  const ss = Number(m[6] ?? "0");
  const ms = m[7] ? Math.floor(Number(`0.${m[7]}`) * 1000) : 0;
  const tz = m[8];
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || hh > 23 || mi > 59 || ss > 59) return null;

  let t: number;
  let back: Date;
  if (tz) {
    t = Date.UTC(y, mo - 1, d, hh, mi, ss, ms);
    back = new Date(Date.UTC(y, mo - 1, d));
    if (back.getUTCFullYear() !== y || back.getUTCMonth() !== mo - 1 || back.getUTCDate() !== d) {
      return null;
    }
    const off = zoneMinutes(tz);
    if (off == null) return null;
    t -= off * 60_000;
  } else {
    back = new Date(y, mo - 1, d, hh, mi, ss, ms);
    if (back.getFullYear() !== y || back.getMonth() !== mo - 1 || back.getDate() !== d) return null;
    t = back.getTime();
  }
  return Number.isFinite(t) ? t : null;
}

/** "Z" → 0, "+05:30" / "+0530" / "+05" → 330, "-08:00" → -480; null if impossible. */
function zoneMinutes(tz: string): number | null {
  if (tz.toUpperCase() === "Z") return 0;
  const sign = tz[0] === "-" ? -1 : 1;
  const digits = tz.slice(1).replace(":", "");
  const offH = Number(digits.slice(0, 2));
  const offM = Number(digits.slice(2) || "0");
  if (offH > 23 || offM > 59) return null;
  return sign * (offH * 60 + offM);
}

/**
 * The UTC offset a stamp names itself, in minutes east — the offset that was in
 * force on the instrument PC on that date — or `null` for a stamp without one
 * (or no stamp).
 */
export function stampOffsetMin(s: unknown): number | null {
  if (typeof s !== "string") return null;
  const m = STAMP_RE.exec(s.trim());
  return m && m[8] ? zoneMinutes(m[8]) : null;
}

/** "2026-07-01 12:00:00": the wall-clock part of a stamp as written, no fraction, no zone. */
export function wallClockOf(s: unknown): string | null {
  if (typeof s !== "string") return null;
  const m = STAMP_RE.exec(s.trim());
  if (!m) return null;
  return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6] ?? "00"}`;
}

// ── windows ────────────────────────────────────────────────────────────────

function finiteOrNull(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/**
 * The window a filter selects at wall-clock time `nowMs`.
 *
 * "Recent" and "today" have an OPEN upper end. A marker stamped after `now` is
 * real: the clock was stepped back (an NTP correction, an operator fixing the
 * system time) or the backend's clock runs ahead of this browser's. Closing the
 * window at `now` would file exactly the newest work as "the future" and hide
 * it. Open, the worst a clock step can do is show a little more than asked.
 *
 * A range whose ends were dragged past each other is read the right way round
 * rather than as an empty window.
 */
export function resolveWindow(f: TimeFilter, nowMs: number): TimeWindow {
  if (!f || f.kind === "all") return ALL_TIME;
  if (f.kind === "range") {
    let from = finiteOrNull(f.from);
    let to = finiteOrNull(f.to);
    if (from != null && to != null && from > to) [from, to] = [to, from];
    return { from, to };
  }
  if (!Number.isFinite(nowMs)) return ALL_TIME;
  if (f.kind === "today") {
    const d = new Date(nowMs);
    d.setHours(0, 0, 0, 0);
    return { from: d.getTime(), to: null };
  }
  const h = Number(f.hours);
  if (!Number.isFinite(h) || h <= 0) return ALL_TIME;
  return { from: nowMs - h * 3_600_000, to: null };
}

/** Whether a time is inside the window. Undated (`null`) is ALWAYS inside. */
export function inWindow(t: number | null | undefined, w: TimeWindow): boolean {
  if (t == null || !Number.isFinite(t)) return true;
  if (w.from != null && t < w.from) return false;
  if (w.to != null && t > w.to) return false;
  return true;
}

/** True when the window excludes nothing. */
export function isAllTime(w: TimeWindow): boolean {
  return w.from == null && w.to == null;
}

// ── the slider ─────────────────────────────────────────────────────────────

export interface TimeDomain {
  min: number;
  max: number;
}

/**
 * Earliest and latest of the given times, by VALUE — never "first and last",
 * because after a clock step the record is not in time order. `null` when
 * there is no usable time at all.
 */
export function timeDomain(times: Iterable<number | null | undefined>): TimeDomain | null {
  let min = Infinity;
  let max = -Infinity;
  for (const t of times) {
    if (t == null || !Number.isFinite(t)) continue;
    if (t < min) min = t;
    if (t > max) max = t;
  }
  return min <= max ? { min, max } : null;
}

/** Slider resolution: positions 0…SLIDER_STEPS map linearly onto the domain. */
export const SLIDER_STEPS = 1000;

export function sliderToTime(v: number, d: TimeDomain): number {
  const f = Math.min(1, Math.max(0, (Number(v) || 0) / SLIDER_STEPS));
  return d.min + (d.max - d.min) * f;
}

/** Slider position of a window end. An open end sits at its extreme. */
export function timeToSlider(t: number | null, d: TimeDomain, end: "from" | "to"): number {
  if (t == null || !Number.isFinite(t)) return end === "from" ? 0 : SLIDER_STEPS;
  const span = d.max - d.min;
  if (!(span > 0)) return end === "from" ? 0 : SLIDER_STEPS;
  const v = Math.round(((t - d.min) / span) * SLIDER_STEPS);
  return Math.min(SLIDER_STEPS, Math.max(0, v));
}

/**
 * The range a pair of slider positions selects. A handle resting at its end of
 * the track means "unbounded on that side": with the right handle at the end,
 * operations recorded after the operator set the range still appear — the map
 * keeps updating every 3 s, and a range that silently froze at the moment it
 * was set would hide exactly the newest work.
 */
export function rangeFromSlider(lo: number, hi: number, d: TimeDomain): TimeFilter {
  let a = Math.round(Number(lo) || 0);
  let b = Math.round(Number(hi) || 0);
  if (a > b) [a, b] = [b, a];
  return {
    kind: "range",
    from: a <= 0 ? null : sliderToTime(a, d),
    to: b >= SLIDER_STEPS ? null : sliderToTime(b, d),
  };
}

// ── what the bar reports ──────────────────────────────────────────────────

export interface TimeSummary {
  /** Items considered (executed markers that can be drawn). */
  total: number;
  /** Of those, inside the window — undated ones included. */
  shown: number;
  /** Of those, with no usable time. */
  undated: number;
}

export function summarize(times: (number | null)[], w: TimeWindow): TimeSummary {
  let shown = 0;
  let undated = 0;
  for (const t of times) {
    if (t == null || !Number.isFinite(t)) undated++;
    if (inWindow(t, w)) shown++;
  }
  return { total: times.length, shown, undated };
}

/**
 * Index of the most recent operation that has a position, or -1.
 *
 * Decided by RECORD ORDER, not by the largest timestamp: markers come back
 * oldest-first by row id (the table is append-only), and after a clock step the
 * newest row can carry an earlier stamp than the rows before it. The largest
 * stamp would then ring an operation that is not the latest one.
 */
export function latestActionIndex(
  markers: { status?: string | null; x_m?: number | null; y_m?: number | null }[] | null | undefined,
): number {
  const list = markers ?? [];
  for (let i = list.length - 1; i >= 0; i--) {
    const m = list[i];
    if (!m || (m.status ?? "") === "planned") continue;
    if (m.x_m == null || m.y_m == null || !Number.isFinite(m.x_m) || !Number.isFinite(m.y_m)) continue;
    return i;
  }
  return -1;
}

// ── formatting ─────────────────────────────────────────────────────────────

const pad2 = (n: number) => String(n).padStart(2, "0");

/** "HH:MM" — or "MM-DD HH:MM" when the day differs from `refMs`'s. */
export function fmtClock(ms: number, refMs?: number | null): string {
  if (!Number.isFinite(ms)) return "";
  const d = new Date(ms);
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  if (refMs != null && Number.isFinite(refMs)) {
    const r = new Date(refMs);
    if (r.getFullYear() === d.getFullYear() && r.getMonth() === d.getMonth() && r.getDate() === d.getDate()) {
      return hm;
    }
    return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${hm}`;
  }
  return hm;
}

/** "2026-11-12 13:19:33" in local time. */
export function fmtDateTime(ms: number): string {
  if (!Number.isFinite(ms)) return "";
  const d = new Date(ms);
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
    `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
  );
}

/** "UTC+8", "UTC+5:30", "UTC−5" for an offset in minutes east of UTC. */
export function fmtUtcOffset(min: number): string {
  if (!Number.isFinite(min)) return "";
  const m = Math.round(min);
  const a = Math.abs(m);
  const rest = a % 60;
  return `UTC${m < 0 ? "−" : "+"}${Math.floor(a / 60)}${rest ? `:${pad2(rest)}` : ""}`;
}

/** Minutes east of UTC this browser's clock reads at instant `ms`. */
export function browserOffsetMin(ms: number): number {
  return -new Date(ms).getTimezoneOffset();
}

/**
 * How long ago, in words — or `null` when that cannot honestly be said: a time
 * in the future (the clocks disagree) has no "ago".
 */
export function fmtAgo(thenMs: number, nowMs: number): string | null {
  const dt = nowMs - thenMs;
  if (!Number.isFinite(dt) || dt < 0) return null;
  const s = dt / 1000;
  if (s < 60) return tr("刚刚", "just now");
  const m = Math.floor(s / 60);
  if (m < 60) return tr(`${m} 分钟前`, `${m} min ago`);
  const h = Math.floor(m / 60);
  if (h < 48) return tr(`${h} 小时前`, `${h} h ago`);
  const days = Math.floor(h / 24);
  return tr(`${days} 天前`, `${days} days ago`);
}

/**
 * The window as the bar shows it: "10:05 – now", "10:05 – 11:40". An open start
 * shows the earliest recorded time; an open end shows "now", because that end
 * keeps moving as new work is recorded.
 */
export function windowLabel(w: TimeWindow, d: TimeDomain | null, nowMs: number): string {
  if (isAllTime(w)) return tr("全部时间", "All time");
  const ref = Number.isFinite(nowMs) ? nowMs : null;
  const from = w.from ?? d?.min ?? null;
  const left = from == null ? "…" : fmtClock(from, ref);
  const right = w.to == null ? tr("现在", "now") : fmtClock(w.to, ref);
  return `${left} – ${right}`;
}
