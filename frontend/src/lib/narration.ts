// ════════════════════════════════════════════════════════════════════════════
// src/lib/narration.ts: where in the transcript a narration entry is inserted.
//
// Narration is the system explaining to the user what a long task is doing, e.g. "about to
// fire a 10 V / 500 ms pulse" or "scan at 50%". It is NOT something the assistant said:
// the agent cannot see it, and it does not enter the LangGraph message channel. Backend:
// mast/chat/narration.py.
//
// Why the ordering logic lives here and not in a component:
//
//   Each of the two lanes is ordered on its own; the hard part is merging them. Messages
//   now carry `t` (stamped by the backend `MessageClockMiddleware`) and narration always
//   has `t`, so time is the primary key. `anchor` (the number of messages rendered when the
//   narration was emitted) is the fallback, used only when one side has no time: it
//   records "which message index", and that coordinate system is rewritten when the
//   transcript is compacted (see the real-session example in mergeNarration).
//
//   Keep this comment in step with the code: a stale statement that timestamps are
//   unavailable would keep the code on the anchor-only path after the premise is fixed.
//
//   The merge rules are a pure function so they can be asserted. A `.filter()` buried in
//   JSX fails as "narration is occasionally placed oddly", which nobody investigates.
//
//     cd frontend && npm run test:unit
// ════════════════════════════════════════════════════════════════════════════

export interface NarrationItem {
  seq: number;
  t: number;
  text: string;
  nk?: string;
  /** Number of messages rendered when this entry was emitted; -1 = no active turn, so append at the end. */
  anchor?: number;
  tone?: string;
  has_image?: boolean;
  fold?: number;
  facts?: Record<string, unknown>;
}

export interface MessageLike {
  role: string;
  content: string;
  /** When this message happened (epoch seconds), stamped by the backend `MessageClockMiddleware`.
   *
   *  Absence is meaningful: `undefined` / `null` means "unknown when it was said" (history
   *  already in the checkpoint before a restart, or a span replaced by a compaction
   *  summary), not time zero. The merge treats it as undated and never as 1970. */
  t?: number | null;
}

/** Effective times of the messages: `eff[i]` is the latest time known up to message i.
 *
 *  - an unstamped message inherits the effective time of the previous one: it sits there,
 *    so it is contemporaneous with that spot;
 *  - a leading run with no stamps at all (compaction summary / pre-restart history) gives
 *    `null`, meaning "earlier than anything", so narration is never pushed above it;
 *  - `max` is used instead of plain assignment so `eff` is non-decreasing by construction,
 *    and "the first index later than T" can be found by a scan without assuming that the
 *    transcript itself is ordered.
 */
function effectiveTimes(messages: readonly MessageLike[]): (number | null)[] {
  const out: (number | null)[] = [];
  let carried: number | null = null;
  for (const m of messages) {
    const t = typeof m?.t === "number" && Number.isFinite(m.t) ? m.t : null;
    if (t !== null) carried = carried === null ? t : Math.max(carried, t);
    out.push(carried);
  }
  return out;
}

export type Lane<M> =
  | { kind: "message"; message: M; index: number }
  | { kind: "narration"; item: NarrationItem };

/**
 * Insert narration into the message sequence and return a flat render sequence.
 *
 * Rules, each guarding against a specific failure:
 *
 *  - `anchor = n` inserts AFTER message n (after `messages[n-1]`).
 *  - `anchor <= 0` inserts at the very front (0 means the transcript was genuinely empty then).
 *  - `anchor > messages.length` or `-1` appends at the END. The former is the normal state
 *    while a turn is running (messages not rendered yet); the latter means there is no
 *    active turn at all (scheduled wake-up, group run, manual trigger). Both land at the
 *    end but for different reasons, so -1 must not be treated as 0.
 *  - Several entries on the same anchor are ordered by `t` (event time), and only on equal
 *    `t` by `seq`.
 *
 *    Ordering used to be by `seq` only. `seq` is allocated atomically by SQLite at write
 *    time, so it records enqueue order, and enqueue order is not event order: the composite
 *    executor emits synchronously at step start (delay ~0), whereas the vision monitor
 *    must grab a frame and run the model first (0.5-1.5 s behind). Example:
 *
 *        seq 10  21:33:21.453  [bias_pulse]  about to fire a 10 V / 500 ms pulse
 *        seq 11  21:33:21.939  [scan_done ]  this image has finished scanning
 *
 *    "About to pulse" was sorted before "scan finished", although physically the scan must
 *    end before the executor can reach the next pulse.
 *
 *    The backend now lets emitters that know the true event time pass it in
 *    (`narrate(event_t=...)`), so the frontend must sort by that number, otherwise the
 *    backend half of the fix is wasted. Emitters that do not know it fall back to emission
 *    time, so the ordering for synchronous emitters is unchanged.
 *
 * ── Entries with timestamps on both sides merge by time; `anchor` is the fallback ──
 *
 * Messages have carried `t` since `MessageClockMiddleware` was added, but the merge used
 * to look at `anchor` only, which wasted the timestamps.
 *
 * The cost is measurable. In a long session (running over three hours and compacted
 * midway) with 15 messages and 36 narration entries, the anchors were 8 / 13 / 28 / 33:
 *
 *     narration 04:26 (anchor=8)  ->  drawn AFTER the 08:01 assistant message (3 h 34 min inverted)
 *     narration 05:40 (anchor=13) ->  drawn after the 08:47 assistant message (3 h 07 min inverted)
 *     narration 06:50 / 08:05 (anchor=28/33 > 15) -> all pushed to the very end
 *
 * `anchor` records the number of messages in state at emission time, but the transcript on
 * screen no longer has that shape: compaction replaced a long stretch of history with one
 * summary, dropping the count from thirty-odd to fifteen. A "which index" coordinate points
 * elsewhere once the coordinate system itself has been rewritten.
 *
 * So when both sides have `t`, merge by `t` (time is a coordinate that compaction does not
 * rewrite); whichever side lacks `t` falls back to `anchor`. This is decided per entry, not
 * per transcript, since "stamped recent messages plus unstamped old history" is the norm.
 *
 * Known approximation: a narration entry emitted in the middle of a tool execution has a
 * time earlier than the assistant message that is stamped after the tool call returns, so
 * it sorts before that tool's collapsed block. That is correct (the result had not
 * happened yet), it just is not aligned inside the collapsed block.
 */
export function mergeNarration<M extends MessageLike>(
  messages: readonly M[],
  items: readonly NarrationItem[],
): Lane<M>[] {
  const n = messages.length;
  const eff = effectiveTimes(messages);
  const dated = eff.some((t) => t !== null);
  // Buckets 0..n. Out-of-range values and -1 all go to n (the end).
  const buckets = new Map<number, NarrationItem[]>();
  for (const item of items ?? []) {
    const at = placeOne(item, n, eff, dated);
    const list = buckets.get(at);
    if (list) list.push(item);
    else buckets.set(at, [item]);
  }
  for (const list of buckets.values()) {
    list.sort(byEventTime);
  }

  const out: Lane<M>[] = [];
  for (const item of buckets.get(0) ?? []) out.push({ kind: "narration", item });
  for (let i = 0; i < n; i++) {
    out.push({ kind: "message", message: messages[i]!, index: i });
    for (const item of buckets.get(i + 1) ?? []) out.push({ kind: "narration", item });
  }
  return out;
}

/**
 * After which message this narration entry is inserted (returns 0..n).
 *
 * By time: find the first message whose effective time is later than the entry and insert
 * before it. `eff` is non-decreasing by construction (see {@link effectiveTimes}), so one
 * linear scan suffices, and the result does not depend on the transcript itself being
 * strictly ordered.
 *
 * Two cases fall back to `anchor`, and neither is abnormal:
 *  - the entry has no usable `t` of its own (old backend rows);
 *  - the whole transcript has no stamps (archive from before a restart, or the clock
 *    middleware turned off).
 * In both cases the behaviour is identical to the anchor-only logic.
 */
function placeOne(
  item: NarrationItem,
  n: number,
  eff: readonly (number | null)[],
  dated: boolean,
): number {
  const t = typeof item?.t === "number" && Number.isFinite(item.t) && item.t > 0
    ? item.t
    : null;
  if (t !== null && dated) {
    for (let i = 0; i < n; i++) {
      const e = eff[i];
      // `null` means this message (and those before it) have no known time. Treat it as
      // earlier than anything, so narration is never pushed above a compaction summary or
      // pre-restart history. `undefined` takes the same branch: under
      // `noUncheckedIndexedAccess` an out-of-range read yields it, and "cannot be read" and
      // "has no time" need the same action here: neither can be compared.
      if (typeof e === "number" && e > t) return i;
    }
    return n;
  }
  const raw = typeof item.anchor === "number" ? item.anchor : -1;
  return raw < 0 || raw > n ? n : raw;
}

/**
 * Incremental cursor: which seq to fetch after.
 *
 * It only moves forward, never back: going back would insert the same narration twice.
 * It must be reset when the conversation changes, since that is a different transcript
 * (reusing the seq would treat the first entries of the new conversation as already read
 * and never show them).
 */
export function nextCursor(current: number, latestSeq: number | undefined): number {
  const latest = typeof latestSeq === "number" && Number.isFinite(latestSeq) ? latestSeq : 0;
  return latest > current ? latest : current;
}

/**
 * Sort key: event time first, `seq` only for equal times.
 *
 * `seq` is kept as the tie-breaker rather than dropped: SQLite allocates it atomically, so
 * it is unique and stable, and two entries in the same millisecond do not jump around due
 * to unstable browser sorting.
 *
 * With `t` missing or invalid, mixing it into a comparison against `seq`-scale numbers
 * would be dangerous (it could be pushed to the front), so when `t` is missing only `seq`
 * is compared: an entry with unknown event time stays where it was stored.
 */
export function byEventTime(a: NarrationItem, b: NarrationItem): number {
  const ta = Number.isFinite(a?.t) ? a.t : null;
  const tb = Number.isFinite(b?.t) ? b.t : null;
  if (ta !== null && tb !== null && ta !== tb) return ta - tb;
  return (a?.seq ?? 0) - (b?.seq ?? 0);
}

/** Merge a newly fetched batch, de-duplicating by seq (WS push plus the polling fallback deliver the same entry). */
export function mergeBatch(
  existing: readonly NarrationItem[],
  incoming: readonly NarrationItem[],
): NarrationItem[] {
  if (!incoming?.length) return existing as NarrationItem[];
  // De-duplication still keys on `seq`, the identity of an entry (the same entry delivered
  // once by WS and once by polling must be recognised). `t` cannot be the identity: two
  // different entries can happen at the same time.
  const bySeq = new Map<number, NarrationItem>();
  for (const it of existing ?? []) bySeq.set(it.seq, it);
  for (const it of incoming) bySeq.set(it.seq, it);
  return [...bySeq.values()].sort(byEventTime);
}

/**
 * The time shown on a narration entry: `HH:MM:SS`, no milliseconds.
 *
 * The point of showing a time is not precise timing but making the order checkable: an
 * entry placed earlier that carries a later time reveals an ordering bug, which would
 * otherwise only be noticed as "the order feels off".
 *
 * An invalid `t` returns `""` (nothing shown), never 1970: a fake time is worse than none
 * because it gets reasoned about as if real.
 */
export function fmtClock(t: unknown): string {
  const v = typeof t === "number" && Number.isFinite(t) && t > 0 ? t : null;
  if (v === null) return "";
  // The backend stores epoch seconds (`time.time()`). Milliseconds would become year
  // 55000, so the magnitude is recognised here too: anything past 1971 in seconds is
  // treated as seconds.
  const ms = v > 1e11 ? v : v * 1000;
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** Does this `chat_narration` event belong to the conversation currently being viewed? */
export function isNarrationFor(
  data: { conversation_id?: unknown } | null | undefined,
  conversationId: string | null | undefined,
): boolean {
  if (!data || !conversationId) return false;
  return String((data as { conversation_id?: unknown }).conversation_id ?? "") === conversationId;
}

// Tokens must be ones that really exist in tailwind.config.ts. A wrong one raises no error
// and does not crash; it simply does nothing (the element keeps its inherited style), and
// the only symptom is text that is hard to read. The check is test/designTokens.test.ts,
// which scans by string, so even comments must not contain a token name that does not
// exist. "good" reuses auto (the green of the AUTO safety tier) instead of adding a new
// semantic colour, so no new token is introduced.
const TONE_CLASS: Record<string, string> = {
  info: "border-mast-border text-mast-muted",
  good: "border-mast-auto-border text-mast-auto",
  warn: "border-mast-warn-border text-mast-warn",
};

/** tone -> colours. Colours only, no judgement: an unknown tone falls back to neutral instead of guessing. */
export function toneClass(tone: string | undefined): string {
  return TONE_CLASS[String(tone ?? "info")] ?? TONE_CLASS.info!;
}

/**
 * Thumbnail URL. Returns null when `has_image` is falsy, and callers then do not render an
 * `<img>`.
 *
 * That way no request is made even if the image endpoint is not live yet (staged rollout):
 * no broken-image icon, no 404 noise.
 */
export function narrationImageUrl(
  item: NarrationItem,
  conversationId: string | null | undefined,
): string | null {
  if (!item?.has_image || !conversationId) return null;
  return (
    `/api/chat/narration-image/${item.seq}` +
    `?conversation_id=${encodeURIComponent(conversationId)}&px=240`
  );
}

// ── Retention across mounts ────────────────────────────────────────────────
//
// Symptom: after switching tabs and coming back, the narration was gone.
//
// There are two causes, and both must be fixed:
//
//   1. Entries live in `useState` of `NarrationLane` and the cursor in a `useRef`; leaving
//      the tab unmounts the component and loses both.
//   2. On return, react-query first feeds back the cache of the last INCREMENTAL request.
//      That request carried something like `after_seq=120` and its response held only the
//      last two entries; the `useEffect` accepted it as is and advanced the cursor to
//      `latest_seq`, so the hundred-odd earlier entries could never be fetched again.
//
// Keeping them in a module-level Map means entries and cursor survive leaving and
// returning, without refetching everything. Entries are bucketed per conversation, since a
// different conversation is a different transcript and must not mix.

export interface NarrationMemo<T> {
  items: T[];
  cursor: number;
}

const _memo = new Map<string, NarrationMemo<unknown>>();

/** What has accumulated for this conversation. If nothing, an empty one (not written into the table; a read must not have side effects). */
export function recallNarration<T>(conversationId: string | null | undefined): NarrationMemo<T> {
  const k = String(conversationId || "");
  const got = k ? _memo.get(k) : undefined;
  return got ? (got as NarrationMemo<T>) : { items: [], cursor: 0 };
}

/** Record what has accumulated for this conversation. */
export function rememberNarration<T>(
  conversationId: string | null | undefined,
  items: T[],
  cursor: number,
): void {
  const k = String(conversationId || "");
  if (!k) return;
  _memo.set(k, { items: items as unknown[], cursor });
  // Keep only the most recent few conversations: a long task produces hundreds of entries,
  // and without a cap memory would grow without bound. Delete the earliest inserted one:
  // Map preserves insertion order, so `keys().next()` is it.
  while (_memo.size > NARRATION_MEMO_MAX) {
    const oldest = _memo.keys().next();
    if (oldest.done) break;
    _memo.delete(oldest.value);
  }
}

/** How many conversations to keep. 3 = the main chat plus two recently viewed; more has no point. */
export const NARRATION_MEMO_MAX = 3;

/** For when everything must be forgotten (switching sample/experiment); tests also use it for isolation. */
export function forgetAllNarration(): void {
  _memo.clear();
}
