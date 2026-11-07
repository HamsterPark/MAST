/**
 * Criterion for skill-market writes + manifest parsing — one criterion, one place.
 *
 * ── Why `ok` alone is not enough ──
 * Changing a subscription only changes one holder. The agent's tool table is
 * **frozen when the graph is built**, so after a successful write the table the
 * model holds may still be the old one: a task is running ⇒ the rebuild is
 * queued; the rebuild was scheduled but failed; or there is no live runtime at
 * all (saved, takes effect at the next start). All three paths return `ok:true`.
 *
 * This extends the discipline of `settingsWrite.ts` by one layer: that file only
 * answers "was it saved", this one also answers "**is it in effect now**". The
 * criterion therefore has three states:
 *
 *   null                → saved, and the agent's table has caught up
 *   { kind: "pending" } → saved, not yet in effect (show as yellow, not green)
 *   { kind: "failed" }  → not saved
 *
 * `test/skillMarketWiring.test.ts` has a gate that requires every caller of the
 * market endpoints to use it.
 */

import { tr } from "./i18n.ts";

export type MarketWriteResult = {
  ok?: boolean;
  degraded?: boolean;
  reason?: string | null;
  rebuild_note?: string | null;
  /** The agent side has not caught up (true = not in effect yet); null = cannot tell. */
  agent_path_pending?: boolean | null;
  /** The agent's table equals the registry as it is now; **null ≠ false**. */
  fingerprint_matches?: boolean | null;
  skipped_mandatory?: string[] | null;
  unknown?: string[] | null;
};

export type MarketWriteProblem = {
  kind: "failed" | "pending" | "unsure";
  message: string;
};

/**
 * What the outcome of a write really is. Returns null when it was saved **and**
 * is in effect.
 *
 * The order is deliberate: first "no response", then degraded (kernel not
 * attached), then `ok !== true` (rejected), and only then the three-state
 * in-effect check. Dropping any layer would let one class of failure silently
 * turn green.
 */
export function marketWriteProblem(
  res: MarketWriteResult | null | undefined,
): MarketWriteProblem | null {
  if (!res) return { kind: "failed", message: tr("保存失败：没有收到内核的回应。", "Save failed: no response from the kernel.") };
  if (res.degraded) {
    return { kind: "failed", message: res.reason || tr("写入未生效（内核未接入）。", "The write did not take effect (kernel not attached).") };
  }
  if (res.ok !== true) {
    return { kind: "failed", message: res.reason || tr("保存被拒绝（内核未说明原因）。", "Save rejected (the kernel gave no reason).") };
  }
  // Pass the backend's human-readable sentence through verbatim — it says
  // "queued / rebuilt / takes effect at next start", and rewording it on the
  // frontend would collapse three cases into one.
  const note = (res.rebuild_note || "").trim();
  if (res.agent_path_pending === true) {
    return { kind: "pending", message: note || tr("已保存，但 agent 工具表尚未跟上。", "Saved, but the agent tool table has not caught up yet.") };
  }
  if (res.agent_path_pending === null || res.agent_path_pending === undefined) {
    return { kind: "unsure", message: note || tr("已保存；是否已生效判断不了。", "Saved; whether it is in effect cannot be determined.") };
  }
  if (res.fingerprint_matches === false) {
    return {
      kind: "pending",
      message: note || tr("已保存，但 agent 手上那张工具表还是旧的。", "Saved, but the agent is still holding the old tool table."),
    };
  }
  // Do not report pending when fingerprint_matches === null: the process has just
  // started and no tool table was built yet. That is "cannot tell", and
  // agent_path_pending has already said false (the rebuild finished).
  return null;
}

/** Three-state label for the in-effect status — null / false / true each speak for themselves and are not folded together. */
export function liveBadge(
  matches: boolean | null | undefined,
): { tone: "ok" | "warn" | "unknown"; text: string } {
  if (matches === true) return { tone: "ok", text: tr("已生效", "In effect") };
  if (matches === false) return { tone: "warn", text: tr("尚未跟上", "Not caught up") };
  return { tone: "unknown", text: tr("无法确认", "Cannot confirm") };
}

/** Mandatory entries are locked switches in the UI — same criterion as the backend (uses the `mandatory` field it returns). */
export function canUnsubscribe(row: { mandatory?: boolean }): boolean {
  return !row.mandatory;
}

/**
 * Whether the builder palette should hide this entry.
 *
 * **`=== false` rather than `!subscribed`**: when the field is absent (old
 * response, degraded catalogue, subscription subsystem unreadable) **do not
 * filter**. Written as `!e.subscribed`, a response missing the field would empty
 * the whole palette and the UI would only show "no matching skills" — a
 * perfectly plausible wrong answer. This is the "unknown folded into no" pattern.
 */
export function paletteHides(entry: { subscribed?: boolean }, subOnly: boolean): boolean {
  return subOnly && entry.subscribed === false;
}

/** Number of entries blocked by the subscription filter. **Show it** — a silent truncation would be read as "this machine does not have that skill". */
export function paletteHiddenCount(
  entries: { subscribed?: boolean }[],
  subOnly: boolean,
): number {
  return entries.filter((e) => paletteHides(e, subOnly)).length;
}

/**
 * Origins other than "shipped with the app" — i.e. skills that **appeared on
 * this machine**.
 *
 * The criterion uses the closed vocabulary of `classify_origin` (the backend is
 * the single source of truth). `builtin` / `composite` / `paper` / `agent_tool`
 * all ship with the app and arrive with upgrades; these three grow locally:
 * workflows composed by the user/agent, self-written .py files, overlay additions.
 */
export const LOCAL_ORIGINS = ["user_composite", "custom", "overlay"] as const;

/**
 * Skills that grew locally but are **not on the subscription surface**.
 *
 * This list fills a real gap: once subscriptions have been customised, a
 * composite skill newly created by the agent in the skill workshop enters the
 * market but not the tool surface — and **nothing tells the user it appeared**.
 * The workshop's wording is "next round it will show up in the tool table on its
 * own", which does not hold on a customised machine.
 *
 * Deliberately a **derived view rather than a gate**: a gate would turn the tool
 * surface back into a safety boundary (that was already dismantled once), and
 * what is missing here is **visibility**. So it has no state of its own, cannot
 * go stale, and needs no instrumentation on the agent side.
 */
export function locallyAuthoredUnsubscribed<
  T extends { name: string; source?: string; subscribed?: boolean },
>(rows: T[]): T[] {
  const local = new Set<string>(LOCAL_ORIGINS);
  return rows.filter((r) => r.subscribed === false && local.has(r.source ?? ""));
}

/** The badge counts only pending items; decided ones do not count. */
export function pendingBadgeCount(
  recs: { status?: string }[] | null | undefined,
): number {
  return (recs ?? []).filter((r) => (r.status ?? "pending") === "pending").length;
}

// ── manifest ────────────────────────────────────────────────────────────────

export type ManifestEntry = { name: string; source?: string; version?: string; spec?: unknown };
export type SubscriptionManifest = {
  kind: string;
  schema_version?: number;
  exported_at?: string;
  machine?: string;
  customised?: boolean;
  entries: ManifestEntry[];
};

export const MANIFEST_KIND = "mast-skill-subscription";

/**
 * Parse a subscription-list file someone sent.
 *
 * On failure it returns a reason string rather than throwing — the caller is a
 * file picker, and what it receives could be anything (an image, a conduct
 * export, a truncated download).
 */
export function parseSubscriptionManifest(
  text: string,
): { manifest: SubscriptionManifest } | { error: string } {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (e) {
    return { error: tr(`这不是一个 JSON 文件：${(e as Error).message}`, `This is not a JSON file: ${(e as Error).message}`) };
  }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { error: tr("文件内容不是一个对象。", "The file content is not an object.") };
  }
  const obj = raw as Record<string, unknown>;
  if (obj.kind !== MANIFEST_KIND) {
    return {
      error: tr(
        `这不是一份订阅列表（kind = ${JSON.stringify(obj.kind ?? null)}，应为 "${MANIFEST_KIND}"）。`,
        `This is not a subscription list (kind = ${JSON.stringify(obj.kind ?? null)}, expected "${MANIFEST_KIND}").`,
      ),
    };
  }
  if (!Array.isArray(obj.entries)) {
    return { error: tr("manifest 里没有 entries 列表。", "The manifest has no entries list.") };
  }
  const entries: ManifestEntry[] = [];
  for (const it of obj.entries) {
    if (it && typeof it === "object" && typeof (it as ManifestEntry).name === "string") {
      const e = it as ManifestEntry;
      if (e.name.trim()) entries.push(e);
    }
  }
  return {
    manifest: {
      kind: MANIFEST_KIND,
      schema_version: typeof obj.schema_version === "number" ? obj.schema_version : 1,
      exported_at: typeof obj.exported_at === "string" ? obj.exported_at : "",
      machine: typeof obj.machine === "string" ? obj.machine : "",
      customised: obj.customised === true,
      entries,
    },
  };
}

/**
 * Local preview before import: how many entries in this list this machine has
 * and how many it lacks.
 *
 * This is only a **preview** for the user. The real criterion is on the backend
 * (the dry_run of `POST /import`), because the answer to "do we have it" changes
 * once embedded composites are installed — the frontend's count is necessarily
 * conservative.
 */
export function diffManifestAgainstCatalog(
  manifest: SubscriptionManifest,
  known: Iterable<string>,
): { matched: string[]; missing: ManifestEntry[]; embedded: number } {
  const have = new Set(known);
  const matched: string[] = [];
  const missing: ManifestEntry[] = [];
  let embedded = 0;
  for (const e of manifest.entries) {
    if (e.spec) embedded += 1;
    if (have.has(e.name)) matched.push(e.name);
    else missing.push(e);
  }
  return { matched, missing, embedded };
}
