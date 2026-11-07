// ════════════════════════════════════════════════════════════════════════════
// conduct.ts: pure decisions for the conduct panel.
//
// Same relationship to `pages/ConductPage.tsx` as `lib/envPanel.ts` has to the right
// panel: no React import, so every decision runs directly under node --test.
//
// No `@/...` imports, and that must stay. `node --test` loads this .ts directly (no
// bundler, no path aliases), so an `@/...` specifier would make the whole test file
// fail on load. Relative imports with an explicit `.ts` extension are fine.
//
// ── Why this is extracted ─────────────────────────────────────────────────
//
// Nearly every decision on the panel is "when must a control NOT be pressable" or
// "when is a number not what it looks like":
//
//   - a "pause" button that is still enabled in PAUSED: pressing it makes the backend
//     record an op_rejected while nothing visibly happens;
//   - an empty budget bar: readers take it to mean "nothing spent" when the truth is
//     "nobody looked";
//   - a wait card that does not show the waive marker: the gate was released from the
//     command line, which the report shows but the screen does not;
//   - a stall alarm that reports a normal long step: alarms that fire too often get
//     ignored, including when a real stall happens.
//
// None of these failures crashes, is caught by typecheck, or is visible in a screenshot.
//
// ── The status vocabulary is closed, and its source of truth is in Python ──
//
// `STATUSES` / `OPS` in `mast/conduct/store.py` are the single source of truth. The copy
// here is a mirror, reconciled by `frontend/test/conduct.test.ts`, which reads that .py
// file directly: not via openapi (the response fields are `str`, so the enum never
// reaches the schema) and not by relying on people to edit both sides together.
//
// An unknown status is not an error: when the backend adds a new one, an older frontend
// shows it verbatim in a neutral colour instead of rendering blank. A blank status bar
// looks like "not running".
// ════════════════════════════════════════════════════════════════════════════

import { langTag, tr } from "./i18n.ts";

/** Top-level statuses. Mirrors `store.STATUSES`, in the same order. */
export const CONDUCT_STATUSES = [
  "draft",
  "approved",
  "running",
  "waiting_operator",
  "waiting_condition",
  "yielding",
  "paused",
  "halted_estop",
  "recovery_pending",
  "completed",
  "aborted",
] as const;

export type ConductStatus = (typeof CONDUCT_STATUSES)[number];

/** Terminal statuses. Mirrors `store.TERMINAL_STATUSES`. */
export const TERMINAL_STATUSES: readonly string[] = ["completed", "aborted"];

/** User intents. Mirrors `store.OPS`. */
export const CONDUCT_OPS = [
  "pause",
  "resume",
  "abort",
  "takeover",
  "ack",
  "set_attended",
  "waive_condition",
  "override_decision",
] as const;

export type ConductOp = (typeof CONDUCT_OPS)[number];

/**
 * Panel colour tiers.
 *
 * Deliberately semantic names rather than the Badge vocabulary of `components/ui.tsx`:
 * that vocabulary expresses safety levels (AUTO/DANGEROUS), whereas here we describe the
 * state of a conduct. The mapping is written in one place, :func:`badgeTone`, and a test
 * pins it against the real vocabulary in ui.tsx: a misspelt tone name makes a badge
 * silently fall back to the default colour, which looks perfectly normal.
 */
export type Tone = "ok" | "warn" | "crit" | "info" | "neutral";

/** Semantic tier -> Badge tone of `components/ui.tsx`. */
export function badgeTone(tone: Tone): string {
  switch (tone) {
    case "ok":
      return "AUTO";
    case "warn":
      return "WARN";
    case "crit":
      return "DANGEROUS";
    case "info":
      return "INFO";
    default:
      return "default";
  }
}

const STATUS_LABEL: Record<ConductStatus, string> = {
  draft: tr("草稿", "Draft"),
  approved: tr("已批准", "Approved"),
  running: tr("执行中", "Running"),
  waiting_operator: tr("等人", "Waiting for operator"),
  waiting_condition: tr("等条件", "Waiting for condition"),
  yielding: tr("让路中", "Yielding"),
  paused: tr("已暂停", "Paused"),
  halted_estop: tr("急停闩挂着", "E-stop latched"),
  recovery_pending: tr("恢复自检中", "Recovery self-check"),
  completed: tr("已完成", "Completed"),
  aborted: tr("已中止", "Aborted"),
};

const STATUS_TONE: Record<ConductStatus, Tone> = {
  draft: "neutral",
  approved: "info",
  running: "ok",
  waiting_operator: "warn",
  waiting_condition: "warn",
  yielding: "warn",
  paused: "info",
  halted_estop: "crit",
  recovery_pending: "warn",
  completed: "ok",
  aborted: "neutral",
};

function known(status: string): status is ConductStatus {
  return (CONDUCT_STATUSES as readonly string[]).includes(status);
}

/**
 * Human-readable status name.
 *
 * An unknown status is returned verbatim, never as an empty string: when the backend adds
 * a status this frontend does not know yet, the user should see `some_new_state` rather
 * than a blank, which looks like "not running".
 */
export function statusLabel(status: string): string {
  if (!status) return tr("（无状态）", "(no status)");
  return known(status) ? STATUS_LABEL[status] : status;
}

export function statusTone(status: string): Tone {
  return known(status) ? STATUS_TONE[status] : "neutral";
}

export function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.includes(status);
}

// ── Whether a button can be pressed ──────────────────────────────────────────
//
// Mirrors `director.OP_VALID_STATUSES`. A status not in the table means the backend
// explicitly rejects the op and records op_rejected (not a silent no-op), but the user
// still sees "I pressed it and nothing happened". So the button must be greyed out on
// this side, and must be able to say why.

/** Statuses in which the Director is actively driving the conduct. */
const DRIVEN: readonly string[] = [
  "running",
  "waiting_operator",
  "waiting_condition",
  "yielding",
  "recovery_pending",
];

/** Every non-terminal status. Abort must be reachable from any of them (a stop must always be possible). */
const NON_TERMINAL: readonly string[] = CONDUCT_STATUSES.filter(
  (s) => !TERMINAL_STATUSES.includes(s),
);

export const OP_VALID_STATUSES: Record<ConductOp, readonly string[]> = {
  pause: DRIVEN,
  takeover: DRIVEN,
  resume: ["paused"],
  abort: NON_TERMINAL,
  set_attended: NON_TERMINAL,
  ack: ["waiting_operator", "waiting_condition"],
  waive_condition: ["waiting_operator", "waiting_condition"],
  // The audited unlock path after a gate decision has stopped the run. Only meaningful
  // in waiting_operator.
  //
  // The status alone is not enough: the same waiting_operator has two sources (a wait
  // step, or a verdict handed to a human), and only the latter has a releasable
  // decision. Final availability also needs a non-empty `pending_decision`: see
  // `overrideEnabled`.
  override_decision: ["waiting_operator"],
};

const OP_LABEL: Record<ConductOp, string> = {
  pause: tr("暂停", "Pause"),
  resume: tr("继续", "Resume"),
  abort: tr("中止", "Abort"),
  takeover: tr("人工接管", "Take over manually"),
  ack: tr("我已确认", "I confirm"),
  set_attended: tr("值守模式", "Attended mode"),
  waive_condition: tr("由我提供证据", "I provide evidence"),
  override_decision: tr("我看过了,继续", "I have reviewed it, continue"),
};

export function opLabel(op: ConductOp): string {
  return OP_LABEL[op];
}

/**
 * Whether the "I have reviewed it, continue" button can be pressed; asks one more
 * question than `opEnabled`.
 *
 * Status `waiting_operator` is not enough: the same status has two sources, a `wait` step
 * (waiting for an ack plus a physical condition, where the right button is "I confirm")
 * and a verdict handed to a human (waiting for human judgement). Only the latter has a
 * releasable decision, and the backend accepts only that one.
 *
 * A greyed-out button must say why: without a reason it looks identical to a broken one.
 */
export function overrideEnabled(
  status: string,
  pending: { decision_id: number; gate_id: string } | null | undefined,
): { enabled: boolean; why: string } {
  const base = opEnabled("override_decision", status);
  if (!base.enabled) return base;
  if (!pending || !pending.decision_id) {
    return {
      enabled: false,
      why: tr(
        "现在停的不是一次闸门判定 —— 等待步用「我已确认」,别的停法用中止或接管",
        "It is not stopped on a gate decision: for a wait step use \"I confirm\"; otherwise abort or take over",
      ),
    };
  }
  return { enabled: true, why: "" };
}

/**
 * Whether this button can be pressed now and, if not, the sentence explaining why.
 *
 * A greyed-out button must carry its reason. Without one it looks the same as a broken
 * button, and the right user reaction differs: "oh, not now" versus "file a bug".
 */
export function opEnabled(
  op: ConductOp,
  status: string,
): { enabled: boolean; why: string } {
  const allowed = OP_VALID_STATUSES[op];
  if (allowed.includes(status)) return { enabled: true, why: "" };
  if (isTerminal(status)) {
    return {
      enabled: false,
      why: tr(
        `这份 conduct 已经${statusLabel(status)} —— 终态是终态,要接着做请新建一份`,
        `This conduct is already ${statusLabel(status).toLowerCase()}: a terminal state is final; create a new one to continue`,
      ),
    };
  }
  return {
    enabled: false,
    why: tr(
      `${opLabel(op)}在「${statusLabel(status)}」下没有意义（可用于：${allowed
        .map(statusLabel)
        .join(" / ")}）`,
      `"${opLabel(op)}" does not apply in "${statusLabel(status)}" (available in: ${allowed
        .map(statusLabel)
        .join(" / ")})`,
    ),
  };
}

// ── Waiting: which gate is missing ───────────────────────────────────────────

export interface ConditionLike {
  desc?: string | null;
  threshold?: number | null;
  stale_after_s?: number | null;
  current_value?: number | null;
  met?: boolean | null;
  met_since?: number | null;
  stale?: boolean | null;
  reading_age_s?: number | null;
  waived?: boolean | null;
  waived_by?: string | null;
  waive_reason?: string | null;
}

export interface WaitLike {
  wait_id?: string | null;
  kind?: string | null;
  message?: string | null;
  ack?: { required?: boolean | null; at?: number | null; by?: string | null } | null;
  condition?: ConditionLike | null;
  lacking?: string[] | null;
}

/** How one gate appears on the panel. */
export interface GateRow {
  /** "Human confirmation" / "Physical condition". */
  label: string;
  /** Whether this gate has passed. */
  ok: boolean;
  /** Cannot be judged (unreadable): different from "not passed", with different colour and wording. */
  unreadable: boolean;
  /** One human-readable sentence. */
  detail: string;
}

/**
 * State of each of the two gates.
 *
 * The two pieces of evidence answer two different questions and do not substitute for each
 * other: a person confirming does not mean the temperature has been reached, and reaching
 * temperature does not mean the sample has been swapped. So this always returns two rows
 * (when there is a condition gate) rather than a "1 item still missing" count: a count
 * cannot say which one is missing, and that is the user's next action.
 */
export function waitGateRows(wait: WaitLike | null | undefined): GateRow[] {
  if (!wait) return [];
  const rows: GateRow[] = [];
  const ack = wait.ack ?? {};
  if (ack.required) {
    const done = ack.at != null;
    rows.push({
      label: tr("人的确认", "Human confirmation"),
      ok: done,
      unreadable: false,
      detail: done
        ? tr(`${ack.by || "某人"} 已确认`, `Confirmed by ${ack.by || "someone"}`)
        : tr("等一句「我已确认」——按下面那个按钮", "Waiting for \"I confirm\": press the button below"),
    });
  }
  const cond = wait.condition;
  if (cond) {
    if (cond.waived) {
      rows.push({
        label: tr("物理条件", "Physical condition"),
        ok: true,
        unreadable: false,
        // The waive marker stays visible permanently, not just at the moment of the ack:
        // the gate was released by a person from the command line, the report carries
        // that, and the screen must keep carrying it too.
        detail: tr(
          `已由 ${cond.waived_by || "某人"} 提供证据放行${
            cond.waive_reason ? `：${cond.waive_reason}` : ""
          }`,
          `Released by ${cond.waived_by || "someone"} providing evidence${
            cond.waive_reason ? `: ${cond.waive_reason}` : ""
          }`,
        ),
      });
    } else if (cond.stale) {
      // stale = cannot be read, which is not the same as not reached. Waiting
      // indefinitely is wrong; a person has to look.
      rows.push({
        label: tr("物理条件", "Physical condition"),
        ok: false,
        unreadable: true,
        detail: tr(
          `读不到${
            cond.reading_age_s != null ? `（读数已 ${fmtDuration(cond.reading_age_s)}）` : ""
          } —— 读不到不等于没到，请人来看（温度采集程序还在跑吗）`,
          `Cannot be read${
            cond.reading_age_s != null ? ` (reading is ${fmtDuration(cond.reading_age_s)} old)` : ""
          }. Unreadable does not mean not reached; ask a person to check (is the temperature logger still running?)`,
        ),
      });
    } else {
      rows.push({
        label: tr("物理条件", "Physical condition"),
        ok: Boolean(cond.met),
        unreadable: false,
        detail: conditionText(cond),
      });
    }
  }
  return rows;
}

/** Body of the condition row: target, current reading, how long it has been in place. */
export function conditionText(cond: ConditionLike | null | undefined): string {
  if (!cond) return "";
  const target =
    cond.desc || (cond.threshold != null ? tr(`目标 ${cond.threshold}`, `Target ${cond.threshold}`) : "");
  const now =
    cond.current_value != null
      ? tr(`当前 ${trimNum(cond.current_value)}`, `Current ${trimNum(cond.current_value)}`)
      : tr("当前读不到", "Current value unreadable");
  const held = cond.met && cond.met_since != null ? tr("，已到位", ", reached") : "";
  return [target, now].filter(Boolean).join(tr("；", "; ")) + held;
}

/** Which gates are still missing (for the title of the wait card). An empty array means both gates are satisfied. */
export function lackingLabels(wait: WaitLike | null | undefined): string[] {
  return waitGateRows(wait)
    .filter((r) => !r.ok)
    .map((r) => r.label);
}

// ── Heartbeat alarm banner ───────────────────────────────────────────────────

export interface HeartbeatLike {
  at?: number | null;
  age_s?: number | null;
  stalled?: boolean | null;
  threshold_s?: number | null;
  in_step?: boolean | null;
  step_elapsed_s?: number | null;
  reason?: string | null;
}

export interface Banner {
  tone: Tone;
  title: string;
  detail: string;
}

/**
 * Stall alarm banner. Returns `null` when it should not fire: alarms that fire too often
 * get ignored, including when a real stall happens.
 *
 * The backend already separates a normal long step from a dead loop (design section 6-1);
 * this function does not re-judge, it only draws the backend's verdict and reason. A
 * second judgement in the frontend would mean a second set of thresholds, and two
 * thresholds eventually give two answers.
 */
export function heartbeatBanner(
  hb: HeartbeatLike | null | undefined,
  status: string,
): Banner | null {
  if (!hb || !hb.stalled) return null;
  if (isTerminal(status)) return null;
  const detail = hb.reason || "";
  if (hb.in_step) {
    return {
      tone: "warn",
      title: tr("这一步停太久了", "This step has been stuck too long"),
      detail:
        (hb.step_elapsed_s != null
          ? tr(
              `当前步已经跑了 ${fmtDuration(hb.step_elapsed_s)}`,
              `The current step has been running for ${fmtDuration(hb.step_elapsed_s)}`,
            )
          : tr("当前步已经超时", "The current step has timed out")) +
        (hb.threshold_s != null
          ? tr(`（阈值 ${fmtDuration(hb.threshold_s)}）`, ` (threshold ${fmtDuration(hb.threshold_s)})`)
          : "") +
        (detail ? tr(`。${detail}`, `. ${detail}`) : "") +
        tr(
          "。指挥线程**不会**去杀这一步：卡死的 TCP 事务杀不得，" +
            "强杀会永久损坏 Nanonis 端口。护针的是看门狗。",
          ". The director thread will **not** kill this step: a hung TCP transaction must not be killed, " +
            "and force-killing it permanently damages the Nanonis port. The watchdog protects the tip.",
        ),
    };
  }
  return {
    tone: "crit",
    title: tr("指挥线程可能停了", "The director thread may have stopped"),
    detail:
      (hb.age_s != null
        ? tr(`心跳已经 ${fmtDuration(hb.age_s)} 没更新`, `Heartbeat not updated for ${fmtDuration(hb.age_s)}`)
        : tr("读不到心跳", "Heartbeat unreadable")) +
      (hb.threshold_s != null
        ? tr(`（阈值 ${fmtDuration(hb.threshold_s)}）`, ` (threshold ${fmtDuration(hb.threshold_s)})`)
        : "") +
      (detail ? tr(`。${detail}`, `. ${detail}`) : ""),
  };
}

// ── Budget ───────────────────────────────────────────────────────────────────

export interface BudgetLike {
  spent_usd?: number | null;
  cap_usd?: number | null;
  reason?: string | null;
  enforceable?: boolean | null;
  not_enforceable_why?: string | null;
  measured_by_currency?: Record<string, number> | null;
}

/**
 * Body text of the budget bar.
 *
 * Unreadable is not the same as zero spent. An empty bar, or one reading `$0.00`, would
 * be taken as "nothing spent" when the truth may be "nobody is keeping the books". So when
 * unreadable it says so and gives a reason.
 *
 * "Cannot be enforced" is a third case, different from both. When the cap cannot be
 * enforced, even the "cap $20.00" half must not be printed as is: "unreadable / cap
 * $20.00" still reads as a cap existing with only the current spend missing. The truth
 * is that the cap does not exist: the ledger measures in each provider's native currency
 * while the cap is in USD, merging them would need an exchange rate, and this repo does
 * not invent rates.
 */
export function budgetText(b: BudgetLike | null | undefined): {
  text: string;
  unreadable: boolean;
  hint: string;
} {
  const cap = b?.cap_usd ?? 0;
  const enforceable = b?.enforceable !== false; // older responses lack this field: keep the old behaviour
  const capText = cap > 0 ? tr(`上限 $${cap.toFixed(2)}`, `cap $${cap.toFixed(2)}`) : tr("未设上限", "no cap set");
  // Measured per currency: show it when present, and never fold into one number (that
  // would need an exchange rate).
  const measured = Object.entries(b?.measured_by_currency ?? {});
  const measuredText = measured.length
    ? measured.map(([cur, v]) => `${cur} ${v.toFixed(2)}`).join(" + ")
    : "";

  if (!enforceable) {
    return {
      text: measuredText
        ? tr(`${measuredText}(上限不生效)`, `${measuredText} (cap not enforced)`)
        : tr(`上限已声明但不生效`, `Cap declared but not enforced`),
      unreadable: true,
      hint:
        b?.not_enforceable_why ||
        b?.reason ||
        tr(
          `声明了 ${capText},但没有任何东西会因为它停下来`,
          `A ${capText} is declared, but nothing will stop because of it`,
        ),
    };
  }
  if (!b || b.spent_usd == null) {
    return {
      text: tr(`读不到 / ${capText}`, `unreadable / ${capText}`),
      unreadable: true,
      hint: b?.reason || tr("花销读不到——读不到不是 0", "Spend is unreadable. Unreadable is not zero."),
    };
  }
  return {
    text: `$${b.spent_usd.toFixed(2)} / ${capText}`,
    unreadable: false,
    hint: b.reason || "",
  };
}

// ── The two reasons approve can be refused ───────────────────────────────────

export interface ApproveLike {
  ok?: boolean | null;
  validation_ok?: boolean | null;
  validation_complete?: boolean | null;
}

/**
 * The sentence explaining why approval is blocked. An empty string means not blocked.
 *
 * The backend only looks at `approvable = ok AND complete`, but the two failure causes call
 * for completely different actions:
 *
 * * errors found (ok=false): the template itself is wrong, so fix the template;
 * * some check never ran (complete=false): e.g. the registry could not be read, so rule 3
 *   did not execute at all. `ok` is still true, and "not checked" would look exactly like
 *   "check passed".
 *
 * Merging both into one "validation failed" message would erase that distinction, which is
 * the whole reason the validator reports three states (ok / complete / approvable).
 */
export function approveBlockReason(o: ApproveLike | null | undefined): string {
  if (!o || o.ok) return "";
  if (o.validation_ok && o.validation_complete === false) {
    return tr(
      "不是发现了错误，是**有检查根本没跑**（比如技能注册表读不到）——「没检查」不许长得像「检查通过」",
      "No error was found; **some checks did not run at all** (e.g. the skill registry could not be read). \"Not checked\" must not look like \"check passed\".",
    );
  }
  if (o.validation_ok === false) {
    return tr("模板本身有问题，见下面的发现", "The template itself has problems; see the findings below");
  }
  return tr("批不下去，见下面的原因", "Cannot be approved; see the reasons below");
}

// ── WS frames: triggers only ─────────────────────────────────────────────────

/** The three frame names, matching `EventType` in `core/events.py` exactly. */
export const CONDUCT_FRAMES = [
  "conduct_status",
  "conduct_gate",
  "conduct_alert",
] as const;

/**
 * What to do when a frame arrives. There are only two answers: refetch, or ignore.
 *
 * This function deliberately reads no state from the frame, and the return value carries
 * no payload field. By design frames are triggers only, never an incremental state source:
 * the bus replays only the last 100 events, so a conduct running for three days will
 * inevitably miss frames, and a client that accumulates state from frames would keep
 * displaying a wrong state, which is much worse than having no live push. The panel's only
 * data source is `GET /api/conducts/{id}`, where one response is one point in time.
 *
 * Frames with a different `conductId` are ignored: the same bus carries frames of other
 * conducts (and replays from before a restart), and accepting them all would make the
 * panel fetch a conduct nobody is looking at.
 */
export function frameAction(
  type: string,
  data: unknown,
  conductId: string,
): "refetch" | "ignore" {
  if (!(CONDUCT_FRAMES as readonly string[]).includes(type)) return "ignore";
  if (!conductId) return "ignore";
  const d = data && typeof data === "object" ? (data as { conduct_id?: unknown }) : null;
  const id = d && typeof d.conduct_id === "string" ? d.conduct_id : "";
  // No conduct_id in the frame (older backend or malformed frame): refetch once to be
  // safe. One extra GET costs far less than missing one "waiting for your confirmation".
  if (!id) return "refetch";
  return id === conductId ? "refetch" : "ignore";
}

// ── Wishlist <-> panel ───────────────────────────────────────────────────────

/** Prefix of a conduct's agent_id in the wishlist. Mirrors `adapters.WISHLIST_AGENT_PREFIX`. */
export const WISHLIST_AGENT_PREFIX = "conduct:";

/**
 * Which conduct a wishlist request came from. Returns an empty string if it did not come
 * from a conduct.
 *
 * Two places light up when a conduct is waiting for a person: the wait card on the conduct
 * panel and a `conduct:<id>` request in the wishlist. The latter is often what the user
 * sees first (they are already looking at the to-do list), and without this link the
 * request was text with no way out: to act on it you had to know that the conduct panel is
 * under "Records".
 */
export function conductIdFromAgentId(agentId: string | null | undefined): string {
  const s = String(agentId ?? "");
  if (!s.startsWith(WISHLIST_AGENT_PREFIX)) return "";
  return s.slice(WISHLIST_AGENT_PREFIX.length).trim();
}

/** Deep link to the panel. With an id it can be bookmarked and passed to the next shift. */
export function conductPanelHref(conductId?: string | null): string {
  const id = String(conductId ?? "").trim();
  return id ? `/records/conduct?id=${encodeURIComponent(id)}` : "/records/conduct";
}

// ── Small helpers ────────────────────────────────────────────────────────────

/** Seconds -> human-readable duration. The panel shows durations everywhere; two formatters would drift into two styles. */
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return tr(`${s} 秒`, `${s} s`);
  if (s < 3600) return tr(`${Math.floor(s / 60)} 分`, `${Math.floor(s / 60)} min`);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return m ? tr(`${h} 小时 ${m} 分`, `${h} h ${m} min`) : tr(`${h} 小时`, `${h} h`);
}

/** Epoch seconds -> local time. Empty values give "—" rather than 1970. */
export function fmtClock(epoch: number | null | undefined): string {
  if (epoch == null || !Number.isFinite(epoch) || epoch <= 0) return "—";
  return new Date(epoch * 1000).toLocaleString(langTag());
}

function trimNum(v: number): string {
  if (!Number.isFinite(v)) return "—";
  return Math.abs(v) >= 100 ? v.toFixed(0) : String(Number(v.toFixed(3)));
}

/** Progress text for one timeline row (`3/7 steps`). */
export function timelineProgress(done: number, total: number): string {
  const d = Math.max(0, Math.min(done, total));
  return tr(`${d}/${total} 步`, `${d}/${total} steps`);
}

// ── Text shared by the settings page and the conduct page ────────────────────

/**
 * Title of the section on the settings page. Both pages reference this same constant.
 *
 * The conduct page's "open it in settings" hint used to point at "Settings -> General",
 * which does not exist (SettingsPage has no `conduct` section there). Users following it
 * could not find the switch. Sharing one constant guarantees the two sides agree.
 */
export const CONDUCT_SETTINGS_TITLE = tr("conduct 指挥线程 Conduct director", "Conduct director");

/**
 * "Saved" and "actually running" are two different things; say which one applies.
 *
 * After `cd_enabled` is written: if a runtime handle is available the thread starts or
 * stops immediately; without one (standalone) the change takes effect at the next start.
 * The old "takes effect after restart" text described both cases as the latter.
 *
 * All four states need a message, including the combination that should not occur:
 * disabled but still running means something is really wrong and must not look the same as
 * "off".
 */
export function directorStateNote(
  enabled: boolean,
  running: boolean,
): { text: string; tone: Tone } {
  if (enabled && running) {
    return { text: tr("指挥线程正在运行。", "The director thread is running."), tone: "ok" };
  }
  if (enabled && !running)
    return {
      text: tr(
        "已保存为启用，但线程还没起来：本次写入若没有运行中的服务句柄，则下次启动生效；" +
          "否则请查日志里的「ConductDirector 没能启动」。",
        "Saved as enabled, but the thread is not up yet: if this write had no running service handle, " +
          "it takes effect at the next start; otherwise check the log for \"ConductDirector failed to start\".",
      ),
      tone: "warn",
    };
  if (!enabled && running)
    return {
      text: tr(
        "已保存为关闭，但线程仍在运行 —— 这不该发生，请查日志并重启服务。",
        "Saved as disabled, but the thread is still running. This should not happen; check the log and restart the service.",
      ),
      tone: "crit",
    };
  return {
    text: tr(
      "关着。读端点照常可用：已有 conduct 的状态仍然读得到，中止也仍然按得下。",
      "Off. Read endpoints still work: the state of existing conducts can still be read, and abort can still be pressed.",
    ),
    tone: "neutral",
  };
}

/**
 * The line for the supervised undo window. Window not open gives an empty string (render
 * nothing, not an empty box).
 *
 * The whole point of this tier is "nobody present, but there is still time to change your
 * mind". If the window is open and the panel says nothing, the tier reverts to what it
 * was before it was wired up, behaving like autonomous while the 403 text still
 * recommended it.
 */
export function ignitionText(
  ig: { by?: string; remaining_s?: number; delay_s?: number } | null | undefined,
): string {
  const left = Math.ceil(Number(ig?.remaining_s ?? 0));
  if (!ig || !Number.isFinite(left) || left <= 0) return "";
  const who = ig.by || "agent";
  const mins = Math.floor(left / 60);
  const when =
    mins >= 1
      ? tr(`${mins} 分 ${left % 60} 秒`, `${mins} min ${left % 60} s`)
      : tr(`${left} 秒`, `${left} s`);
  return tr(
    `${who} 已批准，${when}后点火 —— 这段时间里 abort 能把它撤回。`,
    `Approved by ${who}; ignition in ${when}. Abort can revoke it during this time.`,
  );
}

/**
 * How a campaign's goal criterion is shown in the list.
 *
 * Each of the three states gets its own text, and unknown must never be shown as `0/N`:
 * "none satisfied" and "unreadable" are different things; the former means keep going,
 * the latter means find out why it cannot be read. Folded into one number, a database
 * fault would be read as "still early".
 */
export function goalProgressText(
  g:
    | { verdict?: string; satisfied?: number; total?: number; reason?: string }
    | null
    | undefined,
): { text: string; tone: Tone } {
  const v = String(g?.verdict ?? "");
  const sat = Number(g?.satisfied ?? 0);
  const total = Number(g?.total ?? 0);
  if (v === "done") return { text: tr(`已达成 ${sat}/${total}`, `Achieved ${sat}/${total}`), tone: "ok" };
  if (v === "not_done") return { text: `${sat}/${total}`, tone: "info" };
  if (v === "unknown") {
    // The backend distinguishes three kinds of unknown (no criterion written / criterion
    // not understood / evidence unreadable), and each drives a different next step: the
    // first needs someone to write the criterion, the second to fix it, the third to
    // inspect the database. Folding all into "unreadable" would merge the three again.
    // The substrings below match backend reason text and must stay in Chinese.
    const why = String(g?.reason ?? "");
    if (why.includes("没写")) return { text: tr("未设判据", "No criterion set"), tone: "neutral" };
    if (why.includes("读不懂")) return { text: tr("判据有误", "Criterion invalid"), tone: "warn" };
    return { text: tr("读不到", "Unreadable"), tone: "neutral" };
  }
  return { text: "—", tone: "neutral" };
}
