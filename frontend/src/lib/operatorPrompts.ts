// operatorPrompts.ts — pure logic for the structured operator prompts.
//
// Contract: docs/v2/design/optical_station_witec.md §6.2 / D8 and
// MASTv2/mast/operator_hitl/prompts.py. An agent posts a prompt (confirm / choice /
// number / text / attest) and waits; only a person at the instrument may answer it,
// from this page or from the launcher's desktop popup — whoever answers first counts.
//
// Everything here is plain data in, plain data out (no React, no DOM), so the request
// bodies, the validation, the "which prompts are new" bookkeeping and the nav badge are
// testable under `node --test`. The `/api/operator` endpoints are not in the generated
// schema yet, so the shapes below are written by hand against that contract; every
// parser is defensive about what it accepts.
//
// Identity strings (kind, status, severity, option ids, slot ids) are compared as they
// arrive and are never passed through tr(); only the text shown to the operator is.

import { tr } from "./i18n.ts";

export const PROMPT_KINDS = ["confirm", "choice", "number", "text", "attest"] as const;
export const SEVERITIES = ["info", "action", "safety"] as const;
export type Severity = (typeof SEVERITIES)[number];

/** What this client reports as `source` when it answers. */
export const OPERATOR_SOURCE = "web";
export const OPERATOR_NAME_MAX = 80;
export const NOTE_MAX = 2000;
export const TEXT_MAX = 4000;

export interface PromptOption {
  id: string;
  label: string;
}

export interface OperatorAnswer {
  prompt_id: string;
  /** answered | cancelled | timeout | dismissed */
  status: string;
  choice: string | null;
  value: unknown;
  note: string;
  answered_by: string;
  /** web | launcher | test | system */
  source: string;
  answered_at: string;
}

export interface OperatorPrompt {
  id: string;
  /** confirm | choice | number | text | attest — kept as a string so a newer service's
   *  extra kind reaches the UI (which then says "answer it elsewhere") instead of vanishing. */
  kind: string;
  title: string;
  message: string;
  options: PromptOption[];
  number_min: number | null;
  number_max: number | null;
  unit: string;
  attest_slots: string[];
  severity: Severity;
  job_id: string | null;
  skill: string | null;
  agent_id: string | null;
  timeout_s: number | null;
  created_at: string;
  /** open | answered | cancelled | timeout | dismissed */
  status: string;
  answer: OperatorAnswer | null;
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

function normalizeAnswer(raw: unknown): OperatorAnswer | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  const status = str(o.status);
  if (!status) return null;
  return {
    prompt_id: str(o.prompt_id),
    status,
    choice: o.choice === null || o.choice === undefined ? null : str(o.choice),
    value: o.value === undefined ? null : o.value,
    note: str(o.note),
    answered_by: str(o.answered_by),
    source: str(o.source),
    answered_at: str(o.answered_at),
  };
}

/** One prompt as the service sends it, or null when it cannot be told apart from another. */
export function normalizePrompt(raw: unknown): OperatorPrompt | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  const id = str(o.id).trim();
  if (!id) return null;
  const options: PromptOption[] = [];
  if (Array.isArray(o.options)) {
    for (const x of o.options) {
      if (x && typeof x === "object") {
        const oid = str((x as Record<string, unknown>).id);
        if (oid !== "") options.push({ id: oid, label: str((x as Record<string, unknown>).label) || oid });
      }
    }
  }
  const sev = str(o.severity);
  return {
    id,
    kind: str(o.kind),
    title: str(o.title).trim() || id,
    message: str(o.message),
    options,
    number_min: num(o.number_min),
    number_max: num(o.number_max),
    unit: str(o.unit),
    attest_slots: Array.isArray(o.attest_slots) ? o.attest_slots.map(str).filter(Boolean) : [],
    severity: (SEVERITIES as readonly string[]).includes(sev) ? (sev as Severity) : "action",
    job_id: o.job_id ? str(o.job_id) : null,
    skill: o.skill ? str(o.skill) : null,
    agent_id: o.agent_id ? str(o.agent_id) : null,
    timeout_s: num(o.timeout_s),
    created_at: str(o.created_at),
    status: str(o.status) || "open",
    answer: normalizeAnswer(o.answer),
  };
}

export interface PromptsPage {
  prompts: OperatorPrompt[];
  seq: number | null;
  /** the service could not save its prompt list (it keeps working from memory, but a restart would lose what is open) */
  persistError: string | null;
}

/** `{prompts: [...], seq?, persist_error?}` (or a bare list) → normalised prompts; rows without an id are dropped. */
export function parsePrompts(raw: unknown): PromptsPage {
  const box = raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as Record<string, unknown>) : null;
  const items = Array.isArray(raw) ? raw : box && Array.isArray(box.prompts) ? box.prompts : [];
  const prompts: OperatorPrompt[] = [];
  for (const it of items) {
    const p = normalizePrompt(it);
    if (p) prompts.push(p);
  }
  return { prompts, seq: box ? num(box.seq) : null, persistError: box && box.persist_error ? str(box.persist_error) : null };
}

// ── what is open, in what order ─────────────────────────────────────────────

export function isOpen(p: OperatorPrompt): boolean {
  return p.status === "open";
}

export function openPrompts(list: readonly OperatorPrompt[]): OperatorPrompt[] {
  return list.filter(isOpen);
}

const RANK: Record<string, number> = { safety: 0, action: 1, info: 2 };

/** Safety before action before info; oldest first within a severity (the one that has waited longest). */
export function sortForAnswering(list: readonly OperatorPrompt[]): OperatorPrompt[] {
  return [...list].sort(
    (a, b) =>
      (RANK[a.severity] ?? 1) - (RANK[b.severity] ?? 1) ||
      a.created_at.localeCompare(b.created_at) ||
      a.id.localeCompare(b.id),
  );
}

/** When the prompt was closed (answer time), else when it was posted. */
export function closedAtMs(p: OperatorPrompt): number {
  return parseIsoMs(p.answer?.answered_at) ?? parseIsoMs(p.created_at) ?? 0;
}

/** Closed prompts, most recently handled first. */
export function sortHistory(list: readonly OperatorPrompt[]): OperatorPrompt[] {
  return list.filter((p) => !isOpen(p)).sort((a, b) => closedAtMs(b) - closedAtMs(a) || b.id.localeCompare(a.id));
}

export function hasSafetyPrompt(list: readonly OperatorPrompt[]): boolean {
  return list.some((p) => isOpen(p) && p.severity === "safety");
}

// ── the feed the app shell reads ────────────────────────────────────────────

/** The open prompts as the nav badge, the alert host and the board read them. */
export interface OpenPromptFeed {
  /** This station has operator prompts at all (the optical profile). When false the
   *  rest is empty and nothing under /api/operator is being asked for. */
  enabled: boolean;
  /** open prompts in answering order (safety first, then oldest first) */
  prompts: readonly OperatorPrompt[];
  openCount: number;
  hasSafety: boolean;
  /** the first answer has arrived (so "nothing yet" is not mistaken for "nothing") */
  loaded: boolean;
  /** this service has no operator API (an older build) */
  unsupported: boolean;
  /** the service could not save its prompt list (see PromptsPage.persistError) */
  persistError: string | null;
}

/**
 * What the shell sees on a station without operator prompts: an STM station, or one that
 * has not said what it is yet. It is always the same frozen object, so an effect that
 * depends on it never re-runs, and nothing in it can raise a badge, a toast or a sound.
 */
export const NO_PROMPT_FEED: OpenPromptFeed = Object.freeze({
  enabled: false,
  prompts: Object.freeze([]) as readonly OperatorPrompt[],
  openCount: 0,
  hasSafety: false,
  loaded: false,
  unsupported: false,
  persistError: null,
});

/**
 * The feed for one answer of `GET /api/operator/prompts?status=open`. Only an optical
 * station has operator prompts: on any other profile (or before the profile is known) the
 * answer is ignored, even when an earlier one is still sitting in the cache, so the badge
 * never shows a count and the alert host never has anything new to announce there.
 */
export function openPromptFeed(input: {
  optical: boolean;
  list: readonly OperatorPrompt[] | undefined;
  loaded: boolean;
  /** HTTP status of the last failed request, if there was one */
  errorStatus: unknown;
  persistError: string | null;
}): OpenPromptFeed {
  if (!input.optical) return NO_PROMPT_FEED;
  const prompts = sortForAnswering(openPrompts(input.list ?? []));
  return {
    enabled: true,
    prompts,
    openCount: prompts.length,
    hasSafety: hasSafetyPrompt(prompts),
    loaded: input.loaded,
    unsupported: input.errorStatus === 404,
    persistError: input.persistError,
  };
}

// ── "which prompts are new" (toast, sound, desktop notification) ────────────

/** Open prompts whose ids have not been seen yet, in answering order. */
export function freshPrompts(seen: ReadonlySet<string>, open: readonly OperatorPrompt[]): OperatorPrompt[] {
  return sortForAnswering(open.filter((p) => isOpen(p) && !seen.has(p.id)));
}

/** *seen* plus every open id. Bounded so a week-long session does not grow it for ever. */
export function rememberSeen(seen: ReadonlySet<string>, open: readonly OperatorPrompt[], cap = 500): Set<string> {
  const next = new Set(seen);
  for (const p of open) next.add(p.id);
  if (next.size > cap) {
    for (const id of next) {
      if (next.size <= cap) break;
      next.delete(id); // insertion order: the oldest go first
    }
  }
  return next;
}

// ── the nav badge ───────────────────────────────────────────────────────────

/** What waits for the operator on the Operator Requests tab: the free-text agent
 *  requests (polled slowly, as before) plus the structured prompts (polled fast). */
export function wishlistBadge(openPromptCount: number, agentPending: number): number {
  const a = Number.isFinite(openPromptCount) && openPromptCount > 0 ? Math.floor(openPromptCount) : 0;
  const b = Number.isFinite(agentPending) && agentPending > 0 ? Math.floor(agentPending) : 0;
  return a + b;
}

export function wishlistBadgeLabel(openPromptCount: number, agentPending: number, safety: boolean): string {
  const total = wishlistBadge(openPromptCount, agentPending);
  const base = tr(`${total} 条请求等待你处理`, `${total} requests waiting for you`);
  return safety ? tr(`${base}（含安全相关）`, `${base} (including a safety one)`) : base;
}

export function titleWithCount(base: string, n: number): string {
  return n > 0 ? `(${n > 99 ? "99+" : n}) ${base}` : base;
}

// ── what to draw and what to send, per kind ─────────────────────────────────

/** The few things a slot definition adds to an attestation with nothing to choose or type. */
export interface SlotHint {
  kind?: string;
  unit?: string;
  min?: number | null;
  max?: number | null;
  options?: { id: string; label?: string }[];
}

export type PromptFields =
  | { mode: "confirm" }
  | { mode: "options"; options: PromptOption[]; send: "choice" | "value" }
  | { mode: "number"; min: number | null; max: number | null; unit: string }
  | { mode: "text" }
  | { mode: "flag" }
  | { mode: "unsupported" };

/**
 * Which control the card shows for a prompt, mirroring `prompts.check_answer`:
 * confirm → Yes/No; choice (or attest with options) → one option; number (or attest with
 * a unit) → a number in range; text → text; any other attest sets its slots to a bare
 * `value`, whose kind is the slot's (`slots` maps slot id → definition; without it the
 * common reading is a signed flag).
 */
export function promptFields(p: OperatorPrompt, slots?: Readonly<Record<string, SlotHint>>): PromptFields {
  switch (p.kind) {
    case "confirm":
      return { mode: "confirm" };
    case "choice":
      return { mode: "options", options: p.options, send: "choice" };
    case "number":
      return { mode: "number", min: p.number_min, max: p.number_max, unit: p.unit };
    case "text":
      return { mode: "text" };
    case "attest": {
      if (p.options.length) return { mode: "options", options: p.options, send: "choice" };
      if (p.unit) return { mode: "number", min: p.number_min, max: p.number_max, unit: p.unit };
      const slot = slots?.[p.attest_slots[0] ?? ""];
      if (slot?.kind === "text") return { mode: "text" };
      if (slot?.kind === "number") {
        return { mode: "number", min: num(slot.min), max: num(slot.max), unit: str(slot.unit) };
      }
      if (slot?.kind === "choice" && slot.options?.length) {
        return {
          mode: "options",
          send: "value",
          options: slot.options.map((o) => ({ id: str(o.id), label: str(o.label) || str(o.id) })),
        };
      }
      return { mode: "flag" };
    }
    default:
      return { mode: "unsupported" };
  }
}

export type Parsed<T> = { ok: true; value: T } | { ok: false; error: string };

function fail<T>(error: string): Parsed<T> {
  return { ok: false, error };
}

const NUMBER_RE = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;

/** A number as typed ('12.5', '12,5', '-3e-2'); never NaN, infinity or "12 mW". */
export function parseNumberInput(text: unknown): Parsed<number> {
  let s = str(text).trim().replace(/−/g, "-");
  if (!s) return fail(tr("请输入一个数值。", "Enter a number."));
  if (s.includes(",") && !s.includes(".") && s.split(",").length === 2) s = s.replace(",", ".");
  if (!NUMBER_RE.test(s)) return fail(tr(`“${s.slice(0, 40)}”不是数值。`, `"${s.slice(0, 40)}" is not a number.`));
  const v = Number(s);
  if (!Number.isFinite(v)) return fail(tr(`“${s.slice(0, 40)}”不是有效的数值。`, `"${s.slice(0, 40)}" is not a valid number.`));
  return { ok: true, value: v };
}

function fmtNum(v: number): string {
  return String(Number(v.toPrecision(12)));
}

/** A number checked against optional bounds, the way the service checks it. */
export function checkRange(v: number, min: number | null, max: number | null, unit = ""): Parsed<number> {
  const u = unit ? ` ${unit}` : "";
  if (min !== null && v < min) {
    return fail(tr(`${fmtNum(v)}${u} 低于下限 ${fmtNum(min)}${u}。`, `${fmtNum(v)}${u} is below the minimum ${fmtNum(min)}${u}.`));
  }
  if (max !== null && v > max) {
    return fail(tr(`${fmtNum(v)}${u} 高于上限 ${fmtNum(max)}${u}。`, `${fmtNum(v)}${u} is above the maximum ${fmtNum(max)}${u}.`));
  }
  return { ok: true, value: v };
}

/** "Allowed range: 0 – 20 mW" for the line under a number box ('' when nothing is known). */
export function rangeHint(min: number | null, max: number | null, unit = ""): string {
  const u = unit ? ` ${unit}` : "";
  if (min !== null && max !== null) return tr(`允许范围：${fmtNum(min)} – ${fmtNum(max)}${u}`, `Allowed range: ${fmtNum(min)} – ${fmtNum(max)}${u}`);
  if (min !== null) return tr(`下限：${fmtNum(min)}${u}`, `Minimum: ${fmtNum(min)}${u}`);
  if (max !== null) return tr(`上限：${fmtNum(max)}${u}`, `Maximum: ${fmtNum(max)}${u}`);
  return unit ? tr(`单位：${unit}`, `Unit: ${unit}`) : "";
}

export function cleanOperatorName(name: unknown): string {
  return str(name).split(/\s+/).filter(Boolean).join(" ").slice(0, OPERATOR_NAME_MAX);
}

export interface AnswerDraft {
  /** clicked Yes/No, or the selected option id */
  choice?: string | null;
  /** the number or text field */
  text?: string;
  /** the attestation box */
  confirmed?: boolean;
  note?: string;
}

export interface AnswerBody {
  by: string;
  source: typeof OPERATOR_SOURCE;
  choice?: string;
  value?: number | string | boolean;
  note?: string;
}

/**
 * The `POST /api/operator/prompts/{id}/answer` body for what the operator filled in, or
 * the reason it cannot be sent. Mirrors `check_answer` so a bad number or option is
 * refused here, before a request is made; the service validates again.
 */
export function buildAnswerBody(
  p: OperatorPrompt,
  draft: AnswerDraft,
  by: unknown,
  slots?: Readonly<Record<string, SlotHint>>,
): Parsed<AnswerBody> {
  const name = cleanOperatorName(by);
  if (!name) return fail(tr("请先填写你的姓名。", "Enter your name first."));
  const f = promptFields(p, slots);
  const body: AnswerBody = { by: name, source: OPERATOR_SOURCE };
  const note = str(draft.note).trim().slice(0, NOTE_MAX);
  if (note) body.note = note;
  switch (f.mode) {
    case "unsupported":
      return fail(tr("这类提示本页还不会回答。", "This page cannot answer this kind of prompt."));
    case "confirm":
      if (draft.choice !== "yes" && draft.choice !== "no") return fail(tr("请选择“是”或“否”。", "Choose Yes or No."));
      body.choice = draft.choice;
      break;
    case "options": {
      const c = draft.choice ?? "";
      if (!c) return fail(tr("请先选一项。", "Choose an option first."));
      if (!f.options.some((o) => o.id === c)) return fail(tr("这不是可选项之一。", "That is not one of the options."));
      if (f.send === "value") body.value = c;
      else body.choice = c;
      break;
    }
    case "number": {
      const n = parseNumberInput(draft.text);
      if (!n.ok) return n;
      const r = checkRange(n.value, f.min, f.max, f.unit);
      if (!r.ok) return r;
      body.value = n.value;
      break;
    }
    case "text": {
      const t = str(draft.text).trim();
      if (!t) return fail(tr("请先输入内容。", "Type your answer first."));
      body.value = t.slice(0, TEXT_MAX);
      break;
    }
    case "flag":
      if (!draft.confirmed) return fail(tr("请先勾选确认框。", "Tick the box to confirm first."));
      body.value = true;
      break;
  }
  return { ok: true, value: body };
}

export interface DismissBody {
  by: string;
  source: typeof OPERATOR_SOURCE;
  reason?: string;
}

/** The `POST .../dismiss` body ("I cannot / will not do this"). `source` is sent explicitly; the service's default is the same. */
export function buildDismissBody(by: unknown, reason: unknown = ""): Parsed<DismissBody> {
  const name = cleanOperatorName(by);
  if (!name) return fail(tr("请先填写你的姓名。", "Enter your name first."));
  const why = str(reason).trim().slice(0, NOTE_MAX);
  return { ok: true, value: why ? { by: name, source: OPERATOR_SOURCE, reason: why } : { by: name, source: OPERATOR_SOURCE } };
}

// ── words for the screen ────────────────────────────────────────────────────

export function statusLabel(status: string): string {
  switch (status) {
    case "open":
      return tr("待处理", "Waiting");
    case "answered":
      return tr("已回答", "Answered");
    case "dismissed":
      return tr("已拒绝", "Declined");
    case "cancelled":
      return tr("已取消", "Cancelled");
    case "timeout":
      return tr("已超时", "Timed out");
    default:
      return status;
  }
}

export function severityLabel(sev: Severity): string {
  switch (sev) {
    case "safety":
      return tr("安全相关", "Safety");
    case "info":
      return tr("供知悉", "FYI");
    default:
      return tr("需要操作", "Action");
  }
}

function clip(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

/** One line saying what the closed prompt ended up as ("Yes", "12.5 mW", "Declined", …). */
export function answerSummary(p: OperatorPrompt): string {
  const a = p.answer;
  if (!a) return statusLabel(p.status);
  if (a.status === "dismissed") return tr("已拒绝（无法完成）", "Declined (cannot do it)");
  if (a.status === "cancelled") return tr("被 agent 取消", "Cancelled by the agent");
  if (a.status === "timeout") return tr("无人应答，已超时", "Nobody answered in time");
  if (a.status !== "answered") return statusLabel(a.status);
  if (p.kind === "confirm") return a.choice === "yes" ? tr("是", "Yes") : a.choice === "no" ? tr("否", "No") : str(a.choice);
  if (a.choice) {
    const opt = p.options.find((o) => o.id === a.choice);
    return opt ? opt.label : a.choice;
  }
  if (typeof a.value === "number") return p.unit ? `${fmtNum(a.value)} ${p.unit}` : fmtNum(a.value);
  if (a.value === true) return tr("已签署", "Signed");
  return a.value === null || a.value === undefined ? "—" : clip(str(a.value), 120);
}

/** Where the answer came from: web / launcher popup / system. */
export function sourceLabel(source: string): string {
  if (source === "web") return tr("网页", "web");
  if (source === "launcher") return tr("启动器弹窗", "launcher popup");
  if (source === "system") return tr("系统", "system");
  if (source.startsWith("prompt:")) return tr(`提示 ${source.slice(7)}`, `prompt ${source.slice(7)}`);
  return source;
}

// ── time ────────────────────────────────────────────────────────────────────

export function parseIsoMs(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

/** 45 s · 3 min 10 s · 2 h 5 min (coarse on purpose: nobody needs seconds after an hour). */
export function formatDuration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return tr(`${s} 秒`, `${s} s`);
  const m = Math.floor(s / 60);
  if (m < 60) {
    const rest = s % 60;
    return rest && m < 10 ? tr(`${m} 分 ${rest} 秒`, `${m} min ${rest} s`) : tr(`${m} 分`, `${m} min`);
  }
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm ? tr(`${h} 小时 ${mm} 分`, `${h} h ${mm} min`) : tr(`${h} 小时`, `${h} h`);
}

/** How long a prompt has been waiting; null when its post time is unknown. */
export function waitingFor(p: OperatorPrompt, nowMs: number): string | null {
  const t = parseIsoMs(p.created_at);
  return t === null ? null : formatDuration(Math.max(0, nowMs - t));
}

/** Milliseconds until the agent stops waiting (≤ 0: it already has); null when it waits for ever. */
export function deadlineLeftMs(p: OperatorPrompt, nowMs: number): number | null {
  const t = parseIsoMs(p.created_at);
  if (t === null || !p.timeout_s || p.timeout_s <= 0) return null;
  return t + p.timeout_s * 1000 - nowMs;
}

/** "agent claude-code · job j-12 · skill AskOperator · waiting 2 min · the agent waits another 8 min at most". */
export function metaLine(p: OperatorPrompt, nowMs: number): string {
  const bits: string[] = [];
  if (p.agent_id) bits.push(tr(`智能体 ${p.agent_id}`, `agent ${p.agent_id}`));
  if (p.job_id) bits.push(tr(`作业 ${p.job_id}`, `job ${p.job_id}`));
  if (p.skill) bits.push(tr(`技能 ${p.skill}`, `skill ${p.skill}`));
  const w = waitingFor(p, nowMs);
  if (w) bits.push(tr(`已等待 ${w}`, `waiting ${w}`));
  const left = deadlineLeftMs(p, nowMs);
  if (left !== null) {
    bits.push(
      left > 0
        ? tr(`智能体最多再等 ${formatDuration(left)}`, `the agent waits another ${formatDuration(left)} at most`)
        : tr("智能体已不再等待", "the agent has stopped waiting"),
    );
  }
  return bits.join(" · ");
}

/** "07-03 10:20" (this year) in local time; the raw text when it is not a date. */
export function fmtClock(iso: string | null | undefined): string {
  const t = parseIsoMs(iso);
  if (t === null) return iso ? String(iso).slice(0, 16) : "";
  const d = new Date(t);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ── the alert: toast lifetime, sound, desktop notification ──────────────────

export interface BeepStep {
  freq: number;
  ms: number;
  gapMs: number;
}

/** Urgent for safety, a double beep for an action, one soft beep for information. */
export function beepPlan(sev: Severity): BeepStep[] {
  if (sev === "safety") {
    return [880, 660, 880, 660, 880].map((freq) => ({ freq, ms: 170, gapMs: 60 }));
  }
  if (sev === "info") return [{ freq: 660, ms: 140, gapMs: 0 }];
  return [
    { freq: 784, ms: 150, gapMs: 60 },
    { freq: 587, ms: 150, gapMs: 0 },
  ];
}

/** How long a toast stays; null = until the prompt is handled (a safety prompt must not time out on screen). */
export function toastLifetimeMs(sev: Severity): number | null {
  return sev === "safety" ? null : sev === "action" ? 30_000 : 12_000;
}

/** A desktop notification is for someone who is not looking at this page. */
export function shouldDesktopNotify(env: { supported: boolean; permission: string; hidden: boolean; hasFocus: boolean }): boolean {
  return env.supported && env.permission === "granted" && (env.hidden || !env.hasFocus);
}

export function notificationText(p: OperatorPrompt): { title: string; body: string } {
  const head = p.severity === "safety" ? tr("安全相关：", "Safety: ") : "";
  return { title: `${head}${p.title}`, body: clip(p.message.replace(/\s+/g, " ").trim(), 160) || tr("MAST 需要你操作", "MAST needs you") };
}

// ── who is answering ────────────────────────────────────────────────────────

export const OPERATOR_NAME_KEY = "mast.operatorName";

function defaultStorage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null; // storage disabled (private mode, blocked cookies)
  }
}

/** The name the operator last typed on this browser ('' when there is none or storage is off). */
export function readOperatorName(storage: Pick<Storage, "getItem"> | null = defaultStorage()): string {
  try {
    return cleanOperatorName(storage?.getItem(OPERATOR_NAME_KEY) ?? "");
  } catch {
    return "";
  }
}

export function writeOperatorName(name: string, storage: Pick<Storage, "setItem"> | null = defaultStorage()): string {
  const clean = cleanOperatorName(name);
  try {
    storage?.setItem(OPERATOR_NAME_KEY, clean);
  } catch {
    /* the name still works for this page load */
  }
  return clean;
}

// ── calls to /api/operator ──────────────────────────────────────────────────

/** The reason in an error body: FastAPI's `detail` (text, or a list of `{msg}`), else `error` / `message`. */
export function errorDetail(raw: unknown): string {
  if (!raw || typeof raw !== "object") return "";
  const o = raw as Record<string, unknown>;
  const d = o.detail ?? o.error ?? o.message;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    return d
      .map((x) => (x && typeof x === "object" ? str((x as Record<string, unknown>).msg) : str(x)))
      .filter(Boolean)
      .join("; ");
  }
  return "";
}

/**
 * How long to wait before asking again. A service that is down is not hammered (10 s),
 * and one without the operator API at all (404, an older build) is asked once a minute.
 */
export function pollDelayMs(error: unknown, baseMs = 2500): number {
  if (!error) return baseMs;
  return (error as { status?: unknown }).status === 404 ? 60_000 : 10_000;
}

/** Whether a failed answer means "somebody else got there first" (the prompt is no longer open). */
export function isAlreadyHandled(status: unknown): boolean {
  return status === 404 || status === 409 || status === 410;
}

/**
 * The line under a card when sending failed. For a prompt, 404 / 409 / 410 mean somebody
 * else got there first; for a slot write a 404 means the slot does not exist, so only the
 * service's own words are shown.
 */
export function failureText(err: unknown, scope: "prompt" | "slot" = "prompt"): string {
  const e = err as { message?: unknown; status?: unknown } | null;
  if (scope === "prompt" && isAlreadyHandled(e?.status)) {
    return tr("这条提示已经在别处处理了（刷新后会消失）。", "This prompt was already handled elsewhere; it disappears on refresh.");
  }
  if (e?.status === 0) return tr("无法联系主服务，请稍后再试。", "Could not reach the main service; try again.");
  return e && typeof e.message === "string" && e.message ? e.message : String(err);
}
