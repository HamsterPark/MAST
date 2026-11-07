// ════════════════════════════════════════════════════════════════════════════
// conductForm.ts: pure decisions for the form that creates a new conduct.
//
// Division of labour with `lib/conduct.ts`: that file covers a conduct that ALREADY exists
// (status, gates, stalls); this one covers the one that does not exist yet: which template
// to pick, what goes in each field, and how to report a bad entry.
//
// ── Why this file exists ───────────────────────────────────────────────────
//
// Before it, creating a conduct was possible only through `curl -XPOST /api/conducts`, and
// the empty state said exactly that. This has the same shape as approve: the design lets
// approve accept only UI-originated requests, so without a button approve effectively did
// not exist; the create backend was always complete, so without a form creation did not
// exist either. An action that cannot be performed looks the same on screen as an action
// that does not exist.
//
// ── Four hard rules, each from a real incident class ─────────────────────────
//
// 1. No physical quantity is ever pre-filled with a default. The template's
//    `ParamSpec.default` is `None` everywhere, deliberately: if it cannot be filled in, the
//    working point of this experiment is not decided yet, which is exactly when to stop.
//    So the `ParamSpecRow` contract has no `default` field at all (the backend omits it on
//    purpose); the renderer cannot pre-fill what it never receives. This removes the
//    incentive rather than relying on discipline. `sts_condition` is the clearest case:
//    it ships with a single `default` group that is deliberately uncalibrated, and
//    pre-filling "default" would suggest the working point is settled.
//
// 2. Out-of-envelope values are rejected, never clamped. On a real rig someone intended
//    1.5 nA and typed 1.5 A. Clamping to the boundary turns an out-of-range entry into a
//    normal-looking run: the person thinks 1.5 nA was set while the machine runs 100 nA,
//    and no log ever reconciles the two. So the form only rejects and explains.
//
// 3. "Unreadable" is not a value. When the template list cannot be fetched, the message is
//    "could not fetch the template list", not "no templates". The former is a fault, the
//    latter a fact.
//
// 4. Server-side validation is the source of truth; this is advance notice only. So local
//    checks never disable the submit button: a form that can no longer be submitted after a
//    local false positive is a stop with no release. Locally found problems are written in
//    red under the field, submission proceeds, and the backend findings are laid over the
//    matching fields when they return.
//
// ── How numbers come in ──────────────────────────────────────────────────────
//
// Input is always text, so a parser is unavoidable; the question is how honest it is.
// `parseFloat("1.5nA")` returns `1.5`, which reproduces the incident above one to one.
// Hence strict whole-string matching: unparseable input is an error, never truncated.
// Nanonis panel notation (`1.5n`, `5nm`) is accepted too, because that is what the
// operator's machine shows; the prefix table is reconciled by tests against
// `SI_PREFIXES` in `mast/core/si_quantity.py`.
//
// Prefixes are NOT mandatory here, unlike the backend agent channel: the `si_quantity`
// docstring explains that mandatory prefixes are for model-supplied quantities, and the
// user's own UI should not pay that cost. A different safeguard applies instead: the number
// the system actually received is echoed back (`1.50 nA (= 1.5e-9 A)`), so a wrong order
// of magnitude is visible immediately.
// ════════════════════════════════════════════════════════════════════════════

import { fmtSI } from "./units.ts";
import { tr } from "./i18n.ts";

// ── SI prefixes ──────────────────────────────────────────────────────────────

/**
 * Prefix -> decimal exponent. Mirrors `SI_PREFIXES` in `mast/core/si_quantity.py`;
 * `frontend/test/conductForm.test.ts` reads that .py file directly to reconcile them.
 *
 * Case is significant: `m` is milli, `M` is mega. A case-insensitive parser would turn
 * 3 milli into 3 mega, which is why this table exists.
 */
export const SI_PREFIX_EXP: Readonly<Record<string, number>> = {
  a: -18,
  f: -15,
  p: -12,
  n: -9,
  u: -6,
  "µ": -6, // U+00B5 MICRO SIGN
  "μ": -6, // U+03BC GREEK SMALL LETTER MU (the one most keyboards type)
  m: -3,
  k: 3,
  M: 6,
  G: 9,
};

/** Prefix -> factor. For the parity test (the Python side stores factors, not exponents). */
export const SI_PREFIX_FACTORS: Readonly<Record<string, number>> =
  Object.fromEntries(
    Object.entries(SI_PREFIX_EXP).map(([k, e]) => [k, Number(`1e${e}`)]),
  );

const PREFIX_CHARS = Object.keys(SI_PREFIX_EXP).join("");

/**
 * Match a numeric value over the WHOLE string, optionally with one SI prefix.
 *
 * Whole-string matching is the point: a `parseFloat`-style "read as much as possible"
 * would read `1.5nA` as `1.5`, and `1.5` is a perfectly valid ampere value, which is the
 * shape of the incident this guards against.
 */
const NUM_RE = new RegExp(
  `^([+-]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:[eE][+-]?\\d+)?)\\s*([${PREFIX_CHARS}])?$`,
);

// ── Contract (mirrors `ParamSpecRow`, deliberately without default: see header item 1) ─

export interface ParamSpecLike {
  name: string;
  /** `float` / `int` / `str` / `bool`. */
  type?: string | null;
  unit?: string | null;
  min_value?: number | null;
  max_value?: number | null;
  help?: string | null;
  choices?: unknown[] | null;
  /** `ParamSpec.default is None` means required. */
  required?: boolean | null;
}

export type ParamValue = number | string | boolean;

export interface FieldParse {
  /** Not filled in yet. Not an error: whether it is required is decided by `buildCreateParams` at submit time. */
  empty: boolean;
  /** The value to send. `null` means there is nothing to send. */
  value: ParamValue | null;
  /** One human-readable error. Empty string means fine. */
  error: string;
  /** The number the system actually received, echoed back. Empty string means nothing to echo. */
  echo: string;
}

const EMPTY: FieldParse = { empty: true, value: null, error: "", echo: "" };

// ── Numeric parsing ──────────────────────────────────────────────────────────

/** Render a number as a human-readable quantity (`1.50 nA`); without a unit it is the number itself. */
function qty(value: number, unit: string): string {
  return unit ? fmtSI(value, unit) : String(value);
}

/**
 * Echo: the human-readable quantity plus the number that actually goes on the wire; both
 * are written out.
 *
 * With only `1.50 nA` a wrong order of magnitude is visible but cannot be reconciled; with
 * only `1.5e-9` the user has to do exponent arithmetic with the tip engaged, which is
 * exactly why `units.ts` was factored out. Writing both makes "what I wanted" and "what it
 * received" comparable at a glance.
 */
export function fieldEcho(value: number, unit: string): string {
  if (!Number.isFinite(value)) return "";
  if (!unit) return String(value);
  return tr(
    `${fmtSI(value, unit)}（= ${String(value)} ${unit}）`,
    `${fmtSI(value, unit)} (= ${String(value)} ${unit})`,
  );
}

/**
 * A piece of text -> a number in the base unit.
 *
 * Accepts `1.5`, `-2`, `5e-3`, `1.5n`, `5nm` (a trailing unit is stripped). Rejects
 * anything that does not match as a whole string; it never truncates.
 */
export function parseQuantity(
  raw: string,
  unit: string,
): { value: number | null; error: string } {
  const s = String(raw ?? "").trim();
  if (!s) return { value: null, error: "" };

  // A trailing unit is stripped verbatim (case-sensitive), so both `5nm` and `1.5 nA` work.
  let head = s;
  let strippedUnit = false;
  if (unit && s.length > unit.length && s.endsWith(unit)) {
    head = s.slice(0, s.length - unit.length).trim();
    strippedUnit = true;
  }

  // Collision when the unit itself is a prefix letter: `5m` in a "metre" field can be read
  // as 5 metres or 5 millimetres, a factor of 1000 apart. Do not guess: nothing would
  // reveal a wrong guess, which is the "fallback so plausible nobody notices it fired"
  // class of failure.
  if (strippedUnit && unit.length === 1 && unit in SI_PREFIX_EXP
      && /[\d.]$/.test(head)) {
    return {
      value: null,
      error: tr(
        `「${s}」有歧义：末尾的 ${unit} 既是单位「${unit}」，也是 SI 前缀` +
          `（${unit} = 1e${SI_PREFIX_EXP[unit]}），两种读法差 1000 倍。` +
          `要 ${head} ${unit} 就写 ${head}；要 ${head} ${unit}${unit} 就写 ` +
          `${head}${unit}${unit} 或 ${head}e${SI_PREFIX_EXP[unit]}。`,
        `"${s}" is ambiguous: the trailing ${unit} is both the unit "${unit}" and an SI prefix ` +
          `(${unit} = 1e${SI_PREFIX_EXP[unit]}), and the two readings differ by a factor of 1000. ` +
          `For ${head} ${unit} write ${head}; for ${head} ${unit}${unit} write ` +
          `${head}${unit}${unit} or ${head}e${SI_PREFIX_EXP[unit]}.`,
      ),
    };
  }

  const m = NUM_RE.exec(head);
  if (!m) {
    return {
      value: null,
      error: tr(
        `「${s}」看不懂。写一个数就行（${unit ? `单位 ${unit}；` : ""}` +
          `可以带 SI 前缀，如 1.5n${unit}、5e-9）。` +
          `**这里不会「读到哪算哪」** —— 一个被读成 1.5 的 1.5n 是一次量级事故。`,
        `"${s}" is not understood. Enter a number (${unit ? `unit ${unit}; ` : ""}` +
          `an SI prefix is allowed, e.g. 1.5n${unit}, 5e-9). ` +
          `**Input is never partially parsed**: reading 1.5n as 1.5 would be an order-of-magnitude error.`,
      ),
    };
  }
  const mant = m[1] ?? "";
  const pfx = m[2] ?? "";
  let value: number;
  if (!pfx) {
    value = Number(mant);
  } else if (!/[eE]/.test(mant)) {
    // Build the exponent through a string rather than multiplying: `1.5 * 1e-9` introduces
    // a binary rounding tail, and `1.5000000000000002e-9` in the echo box would look like
    // the system changed your number.
    value = Number(`${mant}e${SI_PREFIX_EXP[pfx]}`);
  } else {
    value = Number(mant) * Number(`1e${SI_PREFIX_EXP[pfx]}`);
  }
  if (!Number.isFinite(value)) {
    return { value: null, error: tr(`「${s}」不是一个有限的数`, `"${s}" is not a finite number`) };
  }
  return { value, error: "" };
}

// ── Per field ────────────────────────────────────────────────────────────────

function choiceList(spec: ParamSpecLike): string[] {
  return (spec.choices ?? []).map((c) => String(c));
}

/**
 * The text in one field -> the value to send (or an error).
 *
 * The order of checks follows `mast/conduct/validator.check_params` one to one: type, then
 * choices (min/max do not apply when choices is non-empty), then the envelope. A different
 * order would make the same input produce different errors on the two sides, and the user
 * would think they were fixing different things.
 */
export function parseParamInput(raw: string, spec: ParamSpecLike): FieldParse {
  const type = String(spec.type || "str");
  const unit = String(spec.unit || "");
  const s = String(raw ?? "").trim();
  if (!s) return EMPTY;

  if (type === "bool") {
    if (s === "true") return { empty: false, value: true, error: "", echo: tr("是", "Yes") };
    if (s === "false") return { empty: false, value: false, error: "", echo: tr("否", "No") };
    return {
      empty: false,
      value: null,
      error: tr(`「${s}」不是 true/false`, `"${s}" is not true/false`),
      echo: "",
    };
  }

  const choices = choiceList(spec);

  if (type === "str") {
    if (choices.length && !choices.includes(s)) {
      return {
        empty: false,
        value: null,
        error: tr(
          `「${s}」不在允许集里：${choices.join(" / ")}`,
          `"${s}" is not in the allowed set: ${choices.join(" / ")}`,
        ),
        echo: "",
      };
    }
    // String parameters are not numerically parsed: `bias_series_v` is a comma-separated
    // list of biases, and reading it as a number would stop at the first comma. Its shape
    // is validated by the backend skill.
    return { empty: false, value: s, error: "", echo: "" };
  }

  const { value, error } = parseQuantity(s, unit);
  if (error) return { empty: false, value: null, error, echo: "" };
  if (value == null) return EMPTY;

  if (type === "int" && !Number.isInteger(value)) {
    return {
      empty: false,
      value: null,
      error: tr(`${spec.name} 要一个整数，${value} 不是`, `${spec.name} must be an integer; ${value} is not`),
      echo: "",
    };
  }

  if (choices.length) {
    // With non-empty choices, min/max do not take part: same rule as check_params.
    if (!choices.includes(String(value))) {
      return {
        empty: false,
        value: null,
        error: tr(
          `${value} 不在允许集里：${choices.join(" / ")}`,
          `${value} is not in the allowed set: ${choices.join(" / ")}`,
        ),
        echo: "",
      };
    }
    return { empty: false, value, error: "", echo: fieldEcho(value, unit) };
  }

  // ── Envelope: reject, never clamp ───────────────────────────────────────
  //
  // Clamping to the boundary would turn an out-of-range entry into a normal-looking run.
  // Comparison uses `<` / `>` (equal to the boundary is valid), matching check_params
  // exactly: if one side used `<` and the other `<=`, a value on the boundary would pass
  // here and be rejected there.
  const lo = spec.min_value;
  const hi = spec.max_value;
  if (lo != null && value < lo) {
    return {
      empty: false,
      value,
      error: tr(
        `${qty(value, unit)} 低于下限 ${qty(lo, unit)} —— 拒绝，不夹紧` +
          `（夹到边界的话，你以为设了 ${qty(value, unit)}，机器跑的是 ` +
          `${qty(lo, unit)}，两个数不会在任何日志里对上）`,
        `${qty(value, unit)} is below the lower limit ${qty(lo, unit)}: rejected, not clamped ` +
          `(if clamped to the boundary you would think ${qty(value, unit)} was set while the ` +
          `machine ran ${qty(lo, unit)}, and no log would reconcile the two)`,
      ),
      echo: fieldEcho(value, unit),
    };
  }
  if (hi != null && value > hi) {
    return {
      empty: false,
      value,
      error: tr(
        `${qty(value, unit)} 高于上限 ${qty(hi, unit)} —— 拒绝，不夹紧` +
          `（量级填错一位就是这个样子：1.5 nA 与 1.5 A 差十亿倍）`,
        `${qty(value, unit)} is above the upper limit ${qty(hi, unit)}: rejected, not clamped ` +
          `(this is what a wrong prefix looks like: 1.5 nA and 1.5 A differ by a factor of a billion)`,
      ),
      echo: fieldEcho(value, unit),
    };
  }

  return { empty: false, value, error: "", echo: fieldEcho(value, unit) };
}

/** The envelope hint line. With no envelope there is no sentence (no invented "unlimited"). */
export function envelopeHint(spec: ParamSpecLike): string {
  const unit = String(spec.unit || "");
  const lo = spec.min_value;
  const hi = spec.max_value;
  const choices = choiceList(spec);
  if (choices.length) return tr(`只能是：${choices.join(" / ")}`, `Must be one of: ${choices.join(" / ")}`);
  if (lo != null && hi != null) {
    return tr(
      `允许 ${qty(lo, unit)} – ${qty(hi, unit)}（超出即拒绝，不夹紧）`,
      `Allowed ${qty(lo, unit)} – ${qty(hi, unit)} (outside is rejected, not clamped)`,
    );
  }
  if (lo != null) {
    return tr(
      `不得低于 ${qty(lo, unit)}（超出即拒绝，不夹紧）`,
      `Must not be below ${qty(lo, unit)} (rejected, not clamped)`,
    );
  }
  if (hi != null) {
    return tr(
      `不得高于 ${qty(hi, unit)}（超出即拒绝，不夹紧）`,
      `Must not be above ${qty(hi, unit)} (rejected, not clamped)`,
    );
  }
  return "";
}

// ── Whole form ───────────────────────────────────────────────────────────────

export interface BuildResult {
  /** The `params` for `POST /api/conducts`. */
  params: Record<string, ParamValue>;
  /** Field name -> one error (found locally). */
  fieldErrors: Record<string, string>;
  /** Names of required fields left empty. */
  missing: string[];
  /** No problem found locally. This does not mean creation will succeed: the source of truth is the server. */
  ok: boolean;
}

/**
 * Initial values of the form when the template changes: every field is empty, without
 * exception.
 *
 * It exists so that "no physical quantity is pre-filled with a default" is a rule that can
 * be tested rather than a self-discipline written in a comment. The backend deliberately
 * does not return `default`, so even a wish to pre-fill would have nothing to fill with:
 * one layer cannot get it, the other does not try.
 */
export function initialValues(specs: ParamSpecLike[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const spec of specs ?? []) out[String(spec.name)] = "";
  return out;
}

/**
 * Whole form -> request body.
 *
 * Warning: `ok=false` must not be used to disable the submit button. Local checks are
 * advance notice only: a form that cannot be submitted after a local false positive is a
 * stop with no release, and this side will sooner or later differ slightly from
 * `check_params` (for example `<` versus `<=` on a boundary). So the button is always
 * enabled, whatever parses is sent, and the backend has the last word.
 */
export function buildCreateParams(
  specs: ParamSpecLike[],
  raw: Record<string, string>,
): BuildResult {
  const params: Record<string, ParamValue> = {};
  const fieldErrors: Record<string, string> = {};
  const missing: string[] = [];
  for (const spec of specs ?? []) {
    const p = parseParamInput(raw?.[spec.name] ?? "", spec);
    if (p.empty) {
      // A field with `required === false` left empty means "use the template default".
      // No such parameter exists today (`default` is None everywhere), but the contract
      // allows it, so no assumption is made here.
      if (spec.required !== false) missing.push(spec.name);
      continue;
    }
    if (p.error) fieldErrors[spec.name] = p.error;
    // Send any value that parsed, including an out-of-envelope one. The backend says the
    // rejection, so a local check that is stricter than the backend cannot lock the user
    // out on this side.
    if (p.value != null) params[spec.name] = p.value;
  }
  return {
    params,
    fieldErrors,
    missing,
    ok: missing.length === 0 && Object.keys(fieldErrors).length === 0,
  };
}

/** The note shown before submit. Empty when `ok`: say nothing if there is nothing to say. */
export function preSubmitNote(r: BuildResult): string {
  const bits: string[] = [];
  if (r.missing.length) {
    bits.push(tr(`${r.missing.length} 个必填格子还空着`, `${r.missing.length} required field(s) still empty`));
  }
  const bad = Object.keys(r.fieldErrors).length;
  if (bad) {
    bits.push(tr(`${bad} 个格子填的东西这一侧就看得出有问题`, `${bad} field(s) with entries that already look wrong here`));
  }
  if (!bits.length) return "";
  return tr(
    `${bits.join("，")}。还是会提交 —— 最后那句话由后端说；红字见下面各格。`,
    `${bits.join("; ")}. It will still be submitted: the backend has the last word; see the red text under each field.`,
  );
}

// ── After the backend responds ───────────────────────────────────────────────

export interface ParamEchoLike {
  name?: string | null;
  value?: unknown;
  unit?: string | null;
  ok?: boolean | null;
  error?: string | null;
}

export interface CreateOutcomeLike {
  ok?: boolean | null;
  conduct_id?: string | null;
  status?: string | null;
  params_echo?: ParamEchoLike[] | null;
  errors?: string[] | null;
  degraded?: boolean | null;
}

/**
 * String shape of a finding: `[code] where: message` (`validator.Finding.__str__`).
 * `where` may contain dots (step ids), so the middle part cannot match only `\w`.
 */
const FINDING_RE = /^\[([a-z_]+)\]\s*([^:]*):\s*([\s\S]*)$/;

/**
 * The backend's per-field echo -> field name -> its error.
 *
 * The source of truth is `params_echo` (grouped by `Finding.where` on the backend), not a
 * second parse of the strings in `errors`.
 */
export function serverFieldErrors(
  out: CreateOutcomeLike | null | undefined,
): Record<string, string> {
  const map: Record<string, string> = {};
  for (const row of out?.params_echo ?? []) {
    const name = String(row?.name ?? "");
    if (!name) continue;
    if (row?.ok === false) map[name] = String(row?.error || tr("后端拒绝了这个值", "The backend rejected this value"));
  }
  return map;
}

/**
 * Errors that could not be attached to any field.
 *
 * This guards against treating every entry of `errors` as per-field, in which case errors
 * without a field name, such as `ActiveConductExists` (the single-active invariant), "no
 * such template" or "conduct layer unavailable", would never appear on screen: the user
 * presses create, nothing happens, and nothing explains why. Per-field echo is good, but
 * it must not become a funnel that swallows everything else.
 */
export function unassignedErrors(
  out: CreateOutcomeLike | null | undefined,
): string[] {
  const shown = new Set(Object.keys(serverFieldErrors(out)));
  return (out?.errors ?? [])
    .map((e) => String(e ?? ""))
    .filter((e) => e.length > 0)
    .filter((e) => {
      const m = FINDING_RE.exec(e);
      // Not finding-shaped (a plain exception message): always show it.
      if (!m) return true;
      const where = (m[2] ?? "").trim();
      // Also show it when `where` cannot be extracted: "I did not recognise who this
      // belongs to" does not mean "this need not be shown".
      return !where || !shown.has(where);
    });
}

/**
 * A non-2xx response body -> a displayable result.
 *
 * `ConductCreateResponse` is only ONE of the possible bodies: when the request itself is
 * invalid FastAPI returns `{detail: [...]}`, with no `errors` at all. Treating it as a
 * CreateOutcome would make `unassignedErrors` return an empty array and the screen would
 * say "the backend gave no reason" although it did, in a different field. That would fold
 * "unreadable" into a concrete value yet again.
 */
export function normalizeCreateError(
  body: unknown,
  status: number,
): CreateOutcomeLike {
  const b = (body && typeof body === "object" ? body : {}) as Record<string, unknown>;
  if (Array.isArray(b.errors) || Array.isArray(b.params_echo)) {
    return b as CreateOutcomeLike;
  }
  const detail = b.detail;
  if (typeof detail === "string" && detail) {
    return { ok: false, errors: [`HTTP ${status}${tr("：", ": ")}${detail}`] };
  }
  if (Array.isArray(detail) && detail.length) {
    // Per-item pydantic 422. The tail of `loc` is the field name; include it, otherwise
    // people have to guess which field.
    return {
      ok: false,
      errors: detail.map((d) => {
        const one = (d && typeof d === "object" ? d : {}) as Record<string, unknown>;
        const loc = Array.isArray(one.loc) ? one.loc.map(String).join(".") : "";
        const msg = String(one.msg ?? one.type ?? tr("请求体不合法", "Invalid request body"));
        return `HTTP ${status}${tr("：", ": ")}${loc ? `${loc} ` : ""}${msg}`;
      }),
    };
  }
  return {
    ok: false,
    errors: [
      tr(
        `HTTP ${status}：后端回了一个这一侧看不懂的响应体（不是「没有原因」）`,
        `HTTP ${status}: the backend returned a response body this side cannot interpret (not "no reason given")`,
      ),
    ],
  };
}

/** The toast after submission. */
export function createOutcomeMessage(
  out: CreateOutcomeLike | null | undefined,
  ok: boolean,
): { text: string; tone: "ok" | "err" } {
  if (ok && out?.conduct_id) {
    return {
      text: tr(
        `草稿建好了（${out.conduct_id}）。它还**没有**开始跑 —— 下一步是批准。`,
        `Draft created (${out.conduct_id}). It has **not** started running: the next step is approval.`,
      ),
      tone: "ok",
    };
  }
  const first = unassignedErrors(out)[0] || "";
  const nField = Object.keys(serverFieldErrors(out)).length;
  if (nField) {
    return {
      text: tr(
        `没建成：${nField} 个参数被拒绝${first ? `；${first}` : ""}（见下面各格）`,
        `Not created: ${nField} parameter(s) rejected${first ? `; ${first}` : ""} (see the fields below)`,
      ),
      tone: "err",
    };
  }
  return {
    text: tr(
      `没建成：${first || "后端没说为什么"}`,
      `Not created: ${first || "the backend gave no reason"}`,
    ),
    tone: "err",
  };
}

// ── Template dropdown ────────────────────────────────────────────────────────

export interface TemplateRowLike {
  spec_id?: string | null;
  spec_version?: number | null;
  title?: string | null;
  stages?: string[] | null;
  approvable?: boolean | null;
  findings?: string[] | null;
  checks_skipped?: string[] | null;
  params_schema?: ParamSpecLike[] | null;
}

export type TemplateListState =
  | { kind: "loading"; message: string }
  | { kind: "unreadable"; message: string }
  | { kind: "empty"; message: string }
  | { kind: "ready"; message: string };

/**
 * The current situation of the template list.
 *
 * "Unreadable" and "none" are different statements. When the fetch fails, say "could not
 * fetch the template list" and why; when there really are no templates, say something
 * else. Showing the former as an empty dropdown would make the user think this machine
 * simply has no templates: a fault read as a fact, the "unreadable folded into a concrete
 * value" failure mode.
 */
export function templateListState(q: {
  isPending?: boolean | null;
  isError?: boolean | null;
  error?: unknown;
  data?: { templates?: unknown[] | null; degraded?: boolean | null; reason?: string | null } | null;
}): TemplateListState {
  if (q?.isPending) return { kind: "loading", message: tr("正在取模板列表…", "Loading templates…") };
  if (q?.isError) {
    const msg = String((q.error as Error)?.message ?? q.error ?? "");
    return {
      kind: "unreadable",
      message: tr(
        `拉不到模板列表${msg ? `：${msg}` : ""} —— 这是读不到，不是「没有模板」`,
        `Could not fetch the template list${msg ? `: ${msg}` : ""}. This is unreadable, not "no templates"`,
      ),
    };
  }
  const d = q?.data;
  if (!d) {
    return {
      kind: "unreadable",
      message: tr(
        "拉不到模板列表（没有响应体）—— 这是读不到，不是「没有模板」",
        "Could not fetch the template list (no response body). This is unreadable, not \"no templates\"",
      ),
    };
  }
  if (d.degraded) {
    return {
      kind: "unreadable",
      message: tr(
        `拉不到模板列表：${d.reason || "后端说它降级了，但没说为什么"}`,
        `Could not fetch the template list: ${d.reason || "the backend reports degraded mode without saying why"}`,
      ),
    };
  }
  if (!(d.templates ?? []).length) {
    return {
      kind: "empty",
      message: tr(
        "这台机器上一个 conduct 模板都没有（模板在 mast/conduct/templates/ 里注册）",
        "There are no conduct templates on this machine (templates are registered in mast/conduct/templates/)",
      ),
    };
  }
  return { kind: "ready", message: "" };
}

/** Text of one dropdown row. A template that cannot be approved must be recognisable at a glance. */
export function templateOptionLabel(row: TemplateRowLike): string {
  const id = String(row?.spec_id ?? "");
  const title = String(row?.title ?? "") || id;
  const ver = row?.spec_version ? ` v${row.spec_version}` : "";
  return row?.approvable === false
    ? tr(`${title}${ver}（这台机器上批不下去）`, `${title}${ver} (cannot be approved on this machine)`)
    : `${title}${ver}`;
}

/**
 * Whether the selected template can be approved, and the fact that this does not prevent
 * creating a draft first.
 *
 * The distinction matters: `POST /conduct` only runs `check_params`, so a template that
 * cannot be approved still produces a draft; what is blocked is the approve step. Calling
 * it "this template cannot be used" would send people looking for the wrong remedy
 * (switching template) when the right one is supplying the missing skill.
 */
export function templateApprovalNote(
  row: TemplateRowLike | null | undefined,
): { blocked: boolean; text: string } {
  if (!row || row.approvable !== false) return { blocked: false, text: "" };
  const skipped = (row.checks_skipped ?? []).length;
  return {
    blocked: true,
    text:
      tr(
        "这个模板**现在批不下去**：草稿建得出来，但下一步的「批准」会被挡住。",
        "This template **cannot be approved right now**: a draft can be created, but the next step, approval, will be blocked.",
      ) +
      (skipped
        ? tr(
            "注意其中有**根本没跑的检查**（下面「没能跑的检查」那一栏）——「没检查」不等于「检查通过」。",
            " Note that some checks **did not run at all** (see \"Checks that could not run\" below): \"not checked\" is not \"check passed\".",
          )
        : tr("缺什么见下面。", " See below for what is missing.")),
  };
}

/** The selected template. Returns `null` if the selection is not in the list (no silent substitute). */
export function pickTemplate(
  rows: TemplateRowLike[] | null | undefined,
  specId: string,
): TemplateRowLike | null {
  if (!specId) return null;
  return (rows ?? []).find((r) => String(r?.spec_id ?? "") === specId) ?? null;
}

// ── Experiment ownership ─────────────────────────────────────────────────────

export type ExperimentPickState =
  | { kind: "list"; message: string }
  | { kind: "manual"; message: string };

/**
 * Whether the experiment dropdown is usable. When it is not, leave a way to type the id by
 * hand: `experiment_id` is required (a conduct with no experiment has nowhere to put its
 * artefacts), and if a failed dropdown blocked creation entirely that would be a stop with
 * no release.
 */
export function experimentPickState(q: {
  isPending?: boolean | null;
  isError?: boolean | null;
  data?: { experiments?: unknown[] | null; degraded?: boolean | null } | null;
}): ExperimentPickState {
  if (q?.isPending) {
    return {
      kind: "manual",
      message: tr("正在取实验列表…（也可以直接手填 ID）", "Loading experiments… (you can also type an ID directly)"),
    };
  }
  if (q?.isError || !q?.data || q.data.degraded) {
    return {
      kind: "manual",
      message: tr(
        "读不到实验列表（不是「没有实验」）—— 手填一个实验 ID 也能建",
        "Could not read the experiment list (not \"no experiments\"). You can still create one by typing an experiment ID",
      ),
    };
  }
  if (!(q.data.experiments ?? []).length) {
    return {
      kind: "manual",
      message: tr(
        "还没有实验 —— 手填一个 ID，或先去实验记录里建一个",
        "No experiments yet. Type an ID, or create one under Records first",
      ),
    };
  }
  return { kind: "list", message: "" };
}
