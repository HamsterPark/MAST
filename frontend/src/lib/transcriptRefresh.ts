// ════════════════════════════════════════════════════════════════════════════
// transcriptRefresh — the **pure** decision "is what just happened worth
// re-reading the transcript for".
//
// Relation to `hooks/useTranscriptRefresh.ts` mirrors `lib/ws.ts` to
// `hooks/useWsEvents.ts`: there is no React import here, so every decision can
// run directly under node --test (`frontend/test/transcriptRefresh.test.ts`).
// The hook keeps only the subscription and refs.
//
// Why it is extracted: these decisions are all about when **not** to refresh, and
// missing one shows up as an occasional visual jump — it does not crash, a
// screenshot will not show it, and the type checker cannot see it. Only tests can
// guard it.
// ════════════════════════════════════════════════════════════════════════════

/** The fields of a WS `experiment` event payload that we care about. */
export interface TranscriptEventData {
  scope?: unknown;
  conversation_id?: unknown;
  agent_id?: unknown;
  t?: unknown;
  seq?: unknown;
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/**
 * Whether this event means "**the private chat I am looking at** just finished a
 * turn".
 *
 * `scope` must be strictly equal to `"private_turn"`: group-chat transcripts use
 * `"transcript"`, and the two payload shapes differ (that one carries seq, this
 * one does not). Mixing them makes one side de-duplicate on the other's fields —
 * and on the side missing the field `Number(undefined ?? 0) === 0`, so the
 * de-dup condition is always true and nothing ever refreshes.
 *
 * Always false while `streaming`: during a stream the page shows the SSE
 * incremental snapshot, and re-reading the whole history midway would make the
 * half-drawn sentence jump back to the end of the previous turn. It invalidates
 * itself when the stream ends.
 */
export function isPrivateTurnFor(
  data: TranscriptEventData | null | undefined,
  conversationId: string | null | undefined,
  streaming = false,
): boolean {
  if (!data || streaming) return false;
  if (str(data.scope) !== "private_turn") return false;
  if (!conversationId) return false;
  return str(data.conversation_id) === conversationId;
}

/**
 * Whether this event means "**the agent I am looking at** spoke in the group
 * chat", and its time cursor.
 *
 * `t = 0` means "the event carries no usable cursor". Callers **refresh anyway**
 * in that case — comparing 0 with the cursor already seen is always false
 * (`0 <= seen`), so an event without t would never refresh anything. "No cursor"
 * and "an old cursor" must be handled differently.
 */
export function transcriptCursorFor(
  data: TranscriptEventData | null | undefined,
  agentId: string,
): { ok: boolean; t: number } {
  if (!data || !agentId) return { ok: false, t: 0 };
  if (str(data.scope) !== "transcript") return { ok: false, t: 0 };
  if (str(data.agent_id) !== agentId) return { ok: false, t: 0 };
  const raw = data.t;
  const t = typeof raw === "number" && Number.isFinite(raw) && raw > 0 ? raw : 0;
  return { ok: true, t };
}

// ── which copy to draw after a refetch ───────────────────────────────────────
//
// Delivery alone is not enough: the notification can be sent, the event received
// and `invalidateQueries` can refetch — and the screen still does not change,
// because **the drawing code did not use the new data**. Both pages used to say
//
//     const shown = streamMsgs ?? history.data?.messages ?? [];
//
// `streamMsgs` is this browser's own final snapshot from the previous SSE run and
// is only cleared when switching conversation / agent. Once the user has sent a
// message in this conversation it stays non-null and keeps shadowing the
// refetched history. Navigating away and back "fixes" it only because the
// component unmounts and remounts, resetting `streamMsgs` to null.
//
// So the criterion cannot be "whichever has a value first"; it must be "whichever
// is newer".
export type TranscriptSource = "stream" | "history";

/**
 * Draw the in-stream snapshot, or the server history.
 *
 * There is one rule: **once the server has been read again after this stream
 * ended, it wins.** The two share one source of truth (`/messages` and the SSE
 * snapshot both come from the checkpointer's `render_history`), so switching to
 * history does not lose the turn that was just spoken.
 *
 * Three guards, each pointing a different way:
 * * while `streaming`, always stay on the snapshot — switching to the whole
 *   history midway would make the half-drawn sentence jump back to the end of
 *   the previous turn;
 * * when `historyUpdatedAt === 0` (history never arrived), stay on the snapshot
 *   — otherwise a failed refetch would blank the screen;
 * * when `streamEndedAt === 0` (this stream has not ended yet), stay on the
 *   snapshot.
 */
export function transcriptSource({
  hasSnapshot,
  streaming,
  streamEndedAt,
  historyUpdatedAt,
}: {
  /** Whether an SSE snapshot is held. */
  hasSnapshot: boolean;
  /** Whether this browser is streaming this conversation right now. */
  streaming: boolean;
  /** When this browser's stream ended (`Date.now()`). 0 = never streamed. */
  streamEndedAt: number;
  /** When the server history was last fetched (react-query `dataUpdatedAt`). 0 = never. */
  historyUpdatedAt: number;
}): TranscriptSource {
  if (!hasSnapshot) return "history";
  if (streaming) return "stream";
  if (streamEndedAt <= 0 || historyUpdatedAt <= 0) return "stream";
  return historyUpdatedAt > streamEndedAt ? "history" : "stream";
}

/** The cursor already refreshed on. `null` = no cursor seen yet. */
export type Cursor = string | null;

/**
 * Whether the `updated_at` cursor advanced — re-read once when it does.
 *
 * **First sight is recorded, not refreshed**: the `updated_at` at mount time
 * describes the very history the page just fetched, and treating it as "updated"
 * would waste one read on every mount (once per sub-tab switch, since this page
 * is conditionally rendered and unmounts when switched away).
 *
 * An empty value does not advance the cursor: a missing `updated_at` means "this
 * row did not carry the field", not "went back in time". Using it to reset `seen`
 * to null would make the next real update look like a first sight and be
 * swallowed.
 */
export function advanceCursor(
  seen: Cursor,
  updatedAt: string | null | undefined,
): { seen: Cursor; refresh: boolean } {
  if (!updatedAt) return { seen, refresh: false };
  if (seen === null) return { seen: updatedAt, refresh: false };
  if (seen === updatedAt) return { seen, refresh: false };
  return { seen: updatedAt, refresh: true };
}
