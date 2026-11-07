import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { api } from "@/api/client";
import { tr } from "@/lib/i18n";
import { useWsEvent } from "@/hooks/useWsEvents";
import { ChatBubbles, type ChatMsg } from "@/components/chat/ChatBubbles";
import {
  fmtClock,
  isNarrationFor,
  mergeBatch,
  recallNarration,
  rememberNarration,
  mergeNarration,
  narrationImageUrl,
  nextCursor,
  toneClass,
  type NarrationItem,
} from "@/lib/narration";

// ════════════════════════════════════════════════════════════════════════
//  Narration: running commentary for the user while a long task executes.
//
//  A long task (e.g. tip forging) only returns a final result from the agent,
//  which leaves the user without feedback. This lane fills the gap with short
//  statements such as "about to fire a pulse", "scan at 50%", "this spot is no
//  good, moving elsewhere".
//
//  It deliberately looks different from an assistant bubble: no agent avatar,
//  no left colour bar, smaller type, and a small "Narration" tag. Drawing it as
//  assistant speech would suggest the agent is talking to itself, but the
//  narration is system commentary the agent never sees (backend
//  mast/chat/narration.py).
//
//  Ordering is a pure function (src/lib/narration.ts + test/narration.test.ts):
//  a filter buried in JSX fails as "narration occasionally lands in an odd place",
//  which nobody would investigate.
// ════════════════════════════════════════════════════════════════════════

function NarrationCard({
  item,
  conversationId,
}: {
  item: NarrationItem;
  conversationId: string | null;
}) {
  const src = narrationImageUrl(item, conversationId);
  const [imgFailed, setImgFailed] = useState(false);
  const clock = fmtClock(item.t);

  return (
    <div className="flex w-full justify-start">
      <div
        className={clsx(
          "max-w-[80%] rounded-mast-ctl border border-dashed bg-mast-panel-2/50 px-3 py-1.5",
          toneClass(item.tone),
        )}
      >
        <div className="flex items-baseline gap-2">
          <span className="shrink-0 rounded-mast-badge bg-mast-code-bg px-1.5 py-0.5 text-[10px] text-mast-faint">
            {tr("旁白", "Narration")}
          </span>
          {/* Time the event HAPPENED (not when it was announced). It makes the
              ordering checkable: an earlier-placed card with a later time means
              the sort is wrong. Not rendered when unavailable (`fmtClock` returns
              an empty string), so 1970 is never shown. */}
          {clock && (
            <span
              className="shrink-0 font-mono text-[10px] tabular-nums text-mast-faint"
              title={tr("这件事发生的时刻", "When this happened")}
            >
              {clock}
            </span>
          )}
          <span className="text-[12.5px] leading-relaxed [overflow-wrap:anywhere]">
            {item.text}
          </span>
        </div>
        {src && !imgFailed && (
          <img
            src={src}
            loading="lazy"
            alt=""
            onError={() => setImgFailed(true)}
            className="mt-1.5 max-h-40 rounded border border-mast-border"
          />
        )}
        {src && imgFailed && (
          // "Image unavailable" is not "no image": say which, instead of an empty box.
          <p className="mt-1 text-[11px] text-mast-faint">{tr("这一帧的画面取不到了。", "The image for this frame is no longer available.")}</p>
        )}
      </div>
    </div>
  );
}

/**
 * Merged view of transcript + narration. With `show=false` it is exactly the plain
 * `<ChatBubbles>` and issues no request: "off" really is off, not merely hidden.
 */
export function NarrationLane({
  messages,
  conversationId,
  pending,
  show,
}: {
  messages: ChatMsg[];
  conversationId: string | null;
  pending?: boolean;
  show: boolean;
}) {
  // Items and cursor live in a module-level memo, not in component state.
  // Switching tabs unmounts the component and would drop useState/useRef; on
  // return react-query first replays the cached INCREMENTAL response (only the
  // last two items), the cursor jumps to the end and the earlier hundreds can
  // never be refetched. See recallNarration in lib/narration.
  const [items, setItems] = useState<NarrationItem[]>(
    () => recallNarration<NarrationItem>(conversationId).items);
  const cursor = useRef(recallNarration(conversationId).cursor);

  // A new conversation is a new transcript: switch the cursor and items to that
  // conversation's own record, otherwise its first items would count as already
  // read and never be shown.
  useEffect(() => {
    const memo = recallNarration<NarrationItem>(conversationId);
    cursor.current = memo.cursor;
    setItems(memo.items);
  }, [conversationId]);

  const enabled = show && !!conversationId;
  const q = useQuery({
    queryKey: ["chat", "narration", conversationId],
    enabled,
    // Polling is the fallback: if the WebSocket is down (e.g. a proxy strips the
    // Upgrade header) narration still appears, a few seconds later.
    refetchInterval: 4000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/chat/narration", {
        params: {
          query: { conversation_id: conversationId!, after_seq: cursor.current },
        },
      });
      if (error) throw error;
      return data;
    },
  });

  useEffect(() => {
    const data = q.data;
    if (!data) return;
    cursor.current = nextCursor(cursor.current, data.latest_seq);
    if (data.items?.length) {
      setItems((prev) => {
        const next = mergeBatch(prev, data.items as NarrationItem[]);
        rememberNarration(conversationId, next, cursor.current);
        return next;
      });
    } else {
      // Remember empty batches too: the cursor may have advanced (`latest_seq`
      // skips non-narration rows).
      rememberNarration(conversationId, items, cursor.current);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q.data, conversationId]);

  // Live push: the event carries only a pointer (conversation + seq); the body is
  // read through the authenticated endpoint above. The bus replays only the last
  // 100 events and a long task emits hundreds of narration items, so putting the
  // body on the bus would crowd out everything else.
  useWsEvent(
    "chat_narration",
    (event) => {
      if (isNarrationFor(event.data as { conversation_id?: unknown }, conversationId)) {
        void q.refetch();
      }
    },
    enabled,
  );

  if (!show || items.length === 0) {
    return <ChatBubbles messages={messages} pending={pending} />;
  }

  const lanes = mergeNarration(messages, items);
  // Consecutive messages are rendered by ChatBubbles as one segment (it owns the
  // bubble style and scroll-to-bottom); narration cards go between segments.
  // Bubbles are not redrawn here, which would create a second render path.
  const blocks: Array<{ msgs: ChatMsg[]; after: NarrationItem[] }> = [];
  let current: { msgs: ChatMsg[]; after: NarrationItem[] } = { msgs: [], after: [] };
  for (const lane of lanes) {
    if (lane.kind === "message") {
      if (current.after.length) {
        blocks.push(current);
        current = { msgs: [], after: [] };
      }
      current.msgs.push(lane.message);
    } else {
      current.after.push(lane.item);
    }
  }
  blocks.push(current);

  return (
    <div className="flex flex-col gap-3.5">
      {blocks.map((block, i) => (
        <div key={i} className="flex flex-col gap-3.5">
          {(block.msgs.length > 0 || (pending && i === blocks.length - 1)) && (
            <ChatBubbles
              messages={block.msgs}
              pending={pending && i === blocks.length - 1}
              autoScroll={i === blocks.length - 1}
              // Rendered in segments, but where each turn starts must be computed
              // from the WHOLE transcript; see allMessages in ChatBubbles.
              allMessages={messages}
            />
          )}
          {block.after.map((item) => (
            <NarrationCard key={item.seq} item={item} conversationId={conversationId} />
          ))}
        </div>
      ))}
    </div>
  );
}
