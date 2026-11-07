// opticalState.ts — pure logic for the manual optical-state panel.
//
// Contract: docs/v2/design/optical_station_witec.md §6.2 / D9 and
// MASTv2/mast/operator_hitl/optical_state.py. A slot records something no software can
// read back (which λ/2 is in the beam, the manual polarizer angle, the objective, the
// laser-area-safe attestation); only the operator writes it, every write is kept in a
// history, and each slot value says who set it, when, from where and until when it holds.
//
// Plain data in / out like operatorPrompts.ts. Expiry is decided here in one place:
// the service's own `valid` flag wins when it says "not valid", and the local clock can
// only make a value expire EARLIER than the last poll said (a value must never look
// valid on screen after its time is up just because the next poll has not arrived).
//
// Slot ids and kinds are identity strings and are never translated.

import { tr } from "./i18n.ts";
import { normalizeProfile } from "./stationProfile.ts";
import {
  NOTE_MAX,
  OPERATOR_NAME_MAX,
  OPERATOR_SOURCE,
  checkRange,
  cleanOperatorName,
  fmtClock,
  formatDuration,
  parseIsoMs,
  parseNumberInput,
  rangeHint,
  sourceLabel,
  type Parsed,
} from "./operatorPrompts.ts";

export const SLOT_KINDS = ["choice", "number", "text", "flag"] as const;
export type SlotKind = (typeof SLOT_KINDS)[number];

/**
 * Whether the service runs as an optical station. The value is `GET /api/health`
 * `station_profile`, the profile the running process IS (not the stored setting, which
 * can differ until the next restart). Same reading as the station profile helper:
 * only "optical" (case and surrounding space ignored) counts; anything unreadable is an
 * STM station, so a machine that predates profiles behaves as it always did.
 */
export function isOpticalProfile(profile: unknown): boolean {
  // one reading of the profile for the whole UI: the station profile helper's
  return normalizeProfile(profile) === "optical";
}

/** The attestation software needs before it may switch laser emission on. */
export const LASER_AREA_SAFE = "laser_area_safe";
export const TEXT_SLOT_MAX = 500;

export interface SlotOption {
  id: string;
  label: string;
  meta: Record<string, unknown>;
}

export interface OpticalSlot {
  id: string;
  label: string;
  kind: string;
  options: SlotOption[];
  unit: string;
  min: number | null;
  max: number | null;
  /** null = valid until changed */
  valid_for_s: number | null;
  description: string;
}

export interface SlotValue {
  value: unknown;
  set_by: string;
  set_at: string;
  source: string;
  valid_until: string | null;
  /** the service's verdict when it answered; the clock may only turn it to false (see isExpired) */
  valid: boolean;
  note: string;
}

export interface HistoryEntry {
  slot: string;
  value: unknown;
  set_by: string;
  set_at: string;
  source: string;
  valid_until: string | null;
  note: string;
}

export interface OpticalStateSnapshot {
  slots: OpticalSlot[];
  values: Record<string, SlotValue>;
}

// ── parsing ─────────────────────────────────────────────────────────────────

function str(v: unknown): string {
  return v === null || v === undefined ? "" : String(v);
}

function num(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function normalizeSlot(raw: unknown): OpticalSlot | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  const id = str(o.id).trim();
  if (!id) return null;
  const options: SlotOption[] = [];
  if (Array.isArray(o.options)) {
    for (const x of o.options) {
      if (!x || typeof x !== "object") continue;
      const xo = x as Record<string, unknown>;
      const oid = str(xo.id);
      if (oid === "") continue;
      options.push({
        id: oid,
        label: str(xo.label) || oid,
        meta: xo.meta && typeof xo.meta === "object" ? (xo.meta as Record<string, unknown>) : {},
      });
    }
  }
  return {
    id,
    label: str(o.label) || id,
    kind: str(o.kind) || "choice",
    options,
    unit: str(o.unit),
    min: num(o.min),
    max: num(o.max),
    valid_for_s: num(o.valid_for_s),
    description: str(o.description),
  };
}

/** A value as the service reports it; null for "not set" (absent, or the snapshot-style `{value: null}`). */
export function normalizeValue(raw: unknown): SlotValue | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  if (o.value === null || o.value === undefined) return null;
  return {
    value: o.value,
    set_by: str(o.set_by),
    set_at: str(o.set_at),
    source: str(o.source),
    valid_until: o.valid_until ? str(o.valid_until) : null,
    valid: o.valid === undefined ? true : Boolean(o.valid),
    note: str(o.note),
  };
}

/** `GET /api/operator/optical-state` → slots in the order the service defines them, set values by slot id. */
export function parseOpticalState(raw: unknown): OpticalStateSnapshot {
  const o = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const slots: OpticalSlot[] = [];
  if (Array.isArray(o.slots)) {
    for (const s of o.slots) {
      const slot = normalizeSlot(s);
      if (slot) slots.push(slot);
    }
  }
  const values: Record<string, SlotValue> = {};
  if (o.values && typeof o.values === "object") {
    for (const [id, v] of Object.entries(o.values as Record<string, unknown>)) {
      const sv = normalizeValue(v);
      if (sv) values[id] = sv;
    }
  }
  return { slots, values };
}

/** `GET .../history` → entries, newest first. The service sends `{records: [...]}` oldest first;
 *  `{history: []}`, `{entries: []}` and a bare list are accepted too. */
export function parseHistory(raw: unknown): HistoryEntry[] {
  const o = raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as Record<string, unknown>) : null;
  const pick = (k: string) => (o && Array.isArray(o[k]) ? (o[k] as unknown[]) : null);
  const items = Array.isArray(raw) ? raw : (pick("records") ?? pick("history") ?? pick("entries") ?? []);
  const out: HistoryEntry[] = [];
  for (const it of items) {
    if (!it || typeof it !== "object") continue;
    const e = it as Record<string, unknown>;
    const slot = str(e.slot);
    if (!slot) continue;
    out.push({
      slot,
      value: e.value === undefined ? null : e.value,
      set_by: str(e.set_by),
      set_at: str(e.set_at),
      source: str(e.source),
      valid_until: e.valid_until ? str(e.valid_until) : null,
      note: str(e.note),
    });
  }
  return out.sort((a, b) => (parseIsoMs(b.set_at) ?? 0) - (parseIsoMs(a.set_at) ?? 0));
}

// ── expiry ──────────────────────────────────────────────────────────────────

/** Whether a set value has run out. The service's `valid: false` always counts; the
 *  clock adds the moment a value expires between two polls. */
export function isExpired(sv: SlotValue | null | undefined, nowMs: number): boolean {
  if (!sv) return false;
  if (sv.valid === false) return true;
  const until = parseIsoMs(sv.valid_until);
  return until !== null && until <= nowMs;
}

/** Milliseconds until the value expires (negative: it already has); null = until changed. */
export function remainingMs(sv: SlotValue | null | undefined, nowMs: number): number | null {
  const until = parseIsoMs(sv?.valid_until);
  return until === null ? null : until - nowMs;
}

export type SlotStatus = "unset" | "valid" | "expired";

/**
 * `unset` — nothing recorded (a flag that is not `true` counts: "not signed");
 * `expired` — recorded but out of date (shown in red); `valid` — recorded and in date.
 */
export function slotStatus(slot: OpticalSlot, sv: SlotValue | null | undefined, nowMs: number): SlotStatus {
  if (!sv) return "unset";
  if (slot.kind === "flag" && sv.value !== true) return "unset";
  return isExpired(sv, nowMs) ? "expired" : "valid";
}

/** "7 h 12 min left" / "expired 5 min ago" / "until changed". */
export function formatRemaining(ms: number | null): string {
  if (ms === null) return tr("直到被改动", "until changed");
  if (ms > 0) {
    const d = formatDuration(ms);
    return tr(`还剩 ${d}`, `${d} left`);
  }
  const d = formatDuration(-ms);
  return tr(`已过期 ${d}`, `expired ${d} ago`);
}

/** What the header of the panel counts: values in date, out of date, never set. */
export function summarize(snapshot: OpticalStateSnapshot, nowMs: number): Record<SlotStatus, number> {
  const out: Record<SlotStatus, number> = { unset: 0, valid: 0, expired: 0 };
  for (const slot of snapshot.slots) out[slotStatus(slot, snapshot.values[slot.id], nowMs)] += 1;
  return out;
}

export function hasAnyValue(snapshot: OpticalStateSnapshot): boolean {
  return Object.keys(snapshot.values).length > 0;
}

// ── showing a value ─────────────────────────────────────────────────────────

/** "λ/2 @532 nm", "45 deg", "Signed", "Nikon 40x" — the value as a person reads it. */
export function displayValue(slot: OpticalSlot, sv: SlotValue | null | undefined): string {
  if (!sv) return "—";
  const v = sv.value;
  switch (slot.kind) {
    case "choice":
      return slot.options.find((o) => o.id === str(v))?.label ?? str(v);
    case "number": {
      const n = typeof v === "number" ? v : Number(v);
      if (!Number.isFinite(n)) return str(v);
      const body = String(Number(n.toPrecision(12)));
      return slot.unit ? `${body} ${slot.unit}` : body;
    }
    case "flag":
      return v === true ? tr("已签署", "Signed") : v === false ? tr("否 / 未签署", "No / not signed") : str(v);
    default:
      return str(v);
  }
}

/** "500–580 nm" for an option that carries `meta.valid_nm` (a waveplate's band); '' otherwise. */
export function optionBand(o: SlotOption): string {
  const r = o.meta.valid_nm;
  if (Array.isArray(r) && r.length === 2 && r.every((x) => typeof x === "number" && Number.isFinite(x))) {
    return `${r[0]}–${r[1]} nm`;
  }
  return "";
}

/** "set by Ana · 07-03 10:20 · launcher popup" */
export function setByLine(sv: SlotValue): string {
  const bits = [sv.set_by && tr(`${sv.set_by} 设置`, `set by ${sv.set_by}`), fmtClock(sv.set_at), sv.source && sourceLabel(sv.source)];
  return bits.filter(Boolean).join(" · ");
}

/** What signing the laser-area-safe flag means, in the words the operator should have in mind. */
export function laserSafeMeaning(): string[] {
  return [
    tr("你已确认：护目镜已佩戴、激光光路已封闭、光路中没有人。", "You have checked that laser goggles are worn, the beam path is enclosed and nobody is in the beam path."),
    tr("签署后，软件可以在有效期内（默认 8 小时）打开激光出光；关出光不受影响，任何时候都允许。", "While signed (8 h by default) software may switch laser emission on; switching it off is always allowed."),
    tr("你的姓名与时间会记入历史。要收回签署，点「撤销签署」。", "Your name and the time go into the history. To take it back, click Revoke."),
  ];
}

// ── writing a slot ──────────────────────────────────────────────────────────

export interface SlotInput {
  choice?: string | null;
  text?: string;
  /** the flag's tick box */
  flag?: boolean;
}

/** The value to PUT for what the operator entered, or the reason it cannot be sent. */
export function validateSlotInput(slot: OpticalSlot, input: SlotInput): Parsed<string | number | boolean> {
  switch (slot.kind) {
    case "choice": {
      const c = input.choice ?? "";
      if (!c) return { ok: false, error: tr("请先选一项。", "Choose an option first.") };
      if (!slot.options.some((o) => o.id === c)) return { ok: false, error: tr("这不是可选项之一。", "That is not one of the options.") };
      return { ok: true, value: c };
    }
    case "number": {
      const n = parseNumberInput(input.text);
      if (!n.ok) return n;
      return checkRange(n.value, slot.min, slot.max, slot.unit);
    }
    case "flag":
      if (input.flag !== true) return { ok: false, error: tr("请先勾选确认框。", "Tick the box to confirm first.") };
      return { ok: true, value: true };
    default: {
      const t = str(input.text).trim();
      if (!t) return { ok: false, error: tr("请先输入内容。", "Type something first.") };
      if (t.length > TEXT_SLOT_MAX) return { ok: false, error: tr(`太长了（最多 ${TEXT_SLOT_MAX} 个字符）。`, `Too long (at most ${TEXT_SLOT_MAX} characters).`) };
      return { ok: true, value: t };
    }
  }
}

export interface SlotPutBody {
  by: string;
  source: typeof OPERATOR_SOURCE;
  value: string | number | boolean;
  note?: string;
}

/** The `PUT /api/operator/optical-state/{slot}` body: the operator's name is required on every write. */
export function buildSlotPutBody(by: unknown, value: string | number | boolean, note: unknown = ""): Parsed<SlotPutBody> {
  const name = cleanOperatorName(by);
  if (!name) return { ok: false, error: tr("请先填写你的姓名。", "Enter your name first.") };
  const n = str(note).trim().slice(0, NOTE_MAX);
  return { ok: true, value: n ? { by: name, source: OPERATOR_SOURCE, value, note: n } : { by: name, source: OPERATOR_SOURCE, value } };
}

/** The `POST .../clear` body. */
export function buildSlotClearBody(by: unknown): Parsed<{ by: string; source: typeof OPERATOR_SOURCE }> {
  const name = cleanOperatorName(by);
  if (!name) return { ok: false, error: tr("请先填写你的姓名。", "Enter your name first.") };
  return { ok: true, value: { by: name.slice(0, OPERATOR_NAME_MAX), source: OPERATOR_SOURCE } };
}

/** The input hint under a number slot's box. */
export function slotRangeHint(slot: OpticalSlot): string {
  return rangeHint(slot.min, slot.max, slot.unit);
}
