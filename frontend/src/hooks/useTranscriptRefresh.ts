// ════════════════════════════════════════════════════════════════════════════
// useTranscriptRefresh: does this page see messages written by someone else?
//
// The message history was only invalidated once after **this browser** finished sending a message
// (SSE stream ends -> invalidateQueries), so anything written from another source never appeared:
// another machine, another tab of the same machine, a background agent run, or a message injected
// on the agent's behalf.
//
// The shape follows the working precedent in `components/agents/RunTaskPanel.tsx`: **events only
// advance a cursor, and the body is read from the authenticated read endpoint**. All three
// guards are required:
//
//   1. Conversation filter: events of other conversations must not refresh this page;
//   2. No refresh while streaming: while this browser is streaming, do not interfere, because the
//      stream invalidates once when it ends;
//   3. De-duplication: a burst of events collapses into one refresh, not one request per event.
//
// Why `updated_at` is watched as well as the WebSocket: when the WS drops (tunnel flaps, service
// restart) pushes stop, while the 6 s conversation-list poll is running anyway. Using a string it
// already fetched as the cursor makes the fallback free. These are not two mechanisms but two
// signal sources for one rule: when a cursor newer than the one in hand appears, read once.
// ════════════════════════════════════════════════════════════════════════════

import { useEffect, useRef } from "react";
import { useWsEvent } from "@/hooks/useWsEvents";
import {
  advanceCursor,
  isPrivateTurnFor,
  type Cursor,
  type TranscriptEventData,
} from "@/lib/transcriptRefresh";

/** One refresh request. The caller decides how to re-read (invalidate / refetch / hydrate). */
export type RefreshFn = () => void;

export interface TranscriptRefreshOptions {
  /** The conversation being viewed. `null` = nothing selected, no refresh. */
  conversationId: string | null | undefined;
  /**
   * Stamp of the conversation's most recent change (`Conversation.updated_at`).
   *
   * Take it from the conversation list that is **already being polled**; do not add a request for
   * it, which would turn this free fallback into an extra round trip every N seconds.
   */
  updatedAt?: string | null;
  /**
   * Whether this browser is currently streaming this conversation. True = no refresh.
   *
   * A premature refresh costs more than a request: while streaming, the page shows the SSE
   * incremental snapshot, and re-reading the whole history midway makes the half-drawn sentence
   * jump back to the end of the previous turn.
   */
  streaming?: boolean;
  /** What actually runs after de-duplication. */
  onRefresh: RefreshFn;
  /** Switches the whole subscription off (used when the component sits in a hidden sub-tab). */
  enabled?: boolean;
}

/**
 * Refreshes a transcript page when **someone else** writes into it.
 *
 * Two signal sources, one rule:
 * * the WS `experiment` event with `scope === "private_turn"` (a private-chat turn finished,
 *   published by `mast.chat.store.publish_private_turn_finished`): near real time;
 * * a change of `updatedAt`: still works when the WS is down, because the conversation list is
 *   polled anyway.
 */
export function useTranscriptRefresh({
  conversationId,
  updatedAt,
  streaming = false,
  onRefresh,
  enabled = true,
}: TranscriptRefreshOptions): void {
  // Keep the handler in a ref: callers usually pass an inline arrow, and putting it in the
  // dependencies would make useWsEvent resubscribe on every render, dropping the shared socket's
  // reference count to 0 and reopening it.
  const refreshRef = useRef(onRefresh);
  refreshRef.current = onRefresh;
  const streamingRef = useRef(streaming);
  streamingRef.current = streaming;

  // The cursor of the most recent refresh already performed. Cleared when the conversation
  // changes: otherwise it would keep the previous conversation's stamp, and stamps of two
  // conversations are not comparable.
  const seen = useRef<Cursor>(null);
  useEffect(() => {
    seen.current = null;
  }, [conversationId]);

  useWsEvent(
    "experiment",
    (event) => {
      const d = (event.data ?? {}) as TranscriptEventData;
      if (!isPrivateTurnFor(d, conversationId, streamingRef.current)) return;
      refreshRef.current();
    },
    enabled && !!conversationId,
  );

  useEffect(() => {
    if (!enabled || !conversationId || streamingRef.current) return;
    const next = advanceCursor(seen.current, updatedAt);
    seen.current = next.seen;
    if (next.refresh) refreshRef.current();
  }, [enabled, conversationId, updatedAt]);
}
