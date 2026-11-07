/** What the system injected into the model after a message was sent: matches a user
 *  message to a real request. Used for a collapsible "injected context" button in the chat,
 *  for debugging.
 *
 *  ## Why match by time instead of by id
 *
 *  Snapshots are captured in `on_chat_model_start` (`mast/prompts/capture.py`). That
 *  callback sees the message list but not which conversation turn it belongs to. Giving it
 *  that would mean threading conversation_id from the HTTP route all the way to the model
 *  factory through the LangGraph executor, where contextvars are not guaranteed to
 *  propagate in a thread pool. Requests that fail to propagate would silently carry no
 *  marker, and "does not match" would look the same as "was not recorded", which is what
 *  this feature must avoid.
 *
 *  So matching is by time: a user message carries `t` (epoch seconds stamped by
 *  `MessageClockMiddleware`) and a snapshot carries `ts`. The requests of a turn are those
 *  in [this user message, next user message). Same machine, same process, same clock, so
 *  there is no cross-machine offset.
 *
 *  ## A mismatch must say which kind of mismatch
 *
 *  "Not recorded" has five causes that call for different actions: capture is disabled
 *  (enable it), the process has not made a model call yet (normal), this turn is too old
 *  and was evicted (raise MAX_SNAPSHOTS or look sooner), this turn has not issued a request
 *  yet (wait), or the message has no timestamp at all (history from before a restart, which
 *  can never match). A single "no data" merges the five, and none of them is fixed by
 *  clicking again.
 */
import { tr } from "./i18n.ts";

/** Metadata of one snapshot (an item of `/api/admin/prompt-capture`). */
export type CaptureItem = {
  index: number;
  /** Stable id. `index` shifts on every model call, so do not use it to fetch details. */
  seq: number;
  ts: number;
  source: string;
  model_id: string;
  provider: string;
  message_count: number;
  total_chars: number;
  system_chars: number;
};

/** The part of the `/api/admin/prompt-capture` response this feature uses. */
export type CaptureList = {
  items: CaptureItem[];
  count: number;
  enabled: boolean;
  total_seen: number;
  capacity: number;
  degraded: boolean;
};

export type InjectedMatch =
  | {
      kind: "match";
      /** The FIRST model call of this turn: the injected context is the one it carried. */
      seq: number;
      ts: number;
      source: string;
      model_id: string;
      /** How many model calls this turn made in total (each tool-call round is one). */
      calls: number;
      /** Total characters of the system messages, taken from this call. */
      systemChars: number;
      totalChars: number;
      /** The matched request was not made by this agent: still shown, but flagged. */
      foreignSource: boolean;
    }
  | { kind: "none"; why: string };

/** A message without a timestamp can never match; that is not an error, there is simply nothing to match on. */
const NO_CLOCK = tr(
  "这条消息没有时间戳（多半是重启前就在存档里的历史），对不上任何一次请求。",
  "This message has no timestamp (probably history archived before a restart), so it cannot be matched to any request.",
);

function reasonWhenEmpty(list: CaptureList): string | null {
  if (list.degraded) {
    return tr("注入记录这个模块没能加载，读不到。", "The injection-record module failed to load, so nothing can be read.");
  }
  if (!list.enabled) {
    return tr(
      "快照记录已关闭，所以这里是空的 —— 不是没有发生过模型调用。",
      "Snapshot recording is off, so this is empty. That does not mean no model call happened.",
    );
  }
  if (list.count === 0) {
    return tr(
      "本进程还没有发生过模型调用。这里只记录真实请求，不做离线模拟渲染。",
      "This process has not made any model call yet. Only real requests are recorded; nothing is simulated offline.",
    );
  }
  return null;
}

/**
 * Match a user message to the first model request of its turn.
 *
 * @param t          epoch seconds of this user message (`ChatMsg.t`). null/undefined = none.
 * @param nextUserT  epoch seconds of the NEXT user message; pass null if there is none
 *                   (the turn is still in progress and the window is open-ended).
 * @param list       the `/api/admin/prompt-capture` response.
 * @param agentId    which agent this conversation belongs to (the snapshot `source`).
 */
export function matchInjectedContext(
  t: number | null | undefined,
  nextUserT: number | null,
  list: CaptureList,
  agentId: string,
): InjectedMatch {
  const empty = reasonWhenEmpty(list);
  if (empty) return { kind: "none", why: empty };
  if (typeof t !== "number" || !Number.isFinite(t)) {
    return { kind: "none", why: NO_CLOCK };
  }

  // The window is right-open: the request made at the time of the next user message belongs to the next turn. With no next message the window stays open.
  const hi = typeof nextUserT === "number" && Number.isFinite(nextUserT)
    ? nextUserT
    : Number.POSITIVE_INFINITY;
  const inWindow = list.items
    .filter((it) => it.ts >= t && it.ts < hi)
    // items are newest first; a turn's injection is read from its first call, so sort ascending by time.
    .sort((a, b) => a.ts - b.ts);

  if (inWindow.length === 0) {
    // Distinguish "evicted" from "not sent yet": both are empty, but one is in the past and the other in the future.
    const oldest = list.items.reduce(
      (m, it) => (it.ts < m ? it.ts : m),
      Number.POSITIVE_INFINITY,
    );
    if (Number.isFinite(oldest) && oldest > t) {
      return {
        kind: "none",
        why: tr(
          `这一轮的注入已经被后面的请求挤出记录了（只留最近 ${list.capacity} 次，` +
            `本进程至今 ${list.total_seen} 次）。`,
          `This turn's injection was evicted by later requests (only the last ${list.capacity} are kept; ` +
            `this process has made ${list.total_seen} so far).`,
        ),
      };
    }
    return {
      kind: "none",
      why: tr(
        "这一轮还没有发出模型请求（或者它没走会被记录的那条路）。",
        "This turn has not made a model request yet (or it did not go through the recorded path).",
      ),
    };
  }

  // The same window may contain calls from other agents or background tasks. Prefer this
  // conversation's agent; if there is none, take the earliest call and say it was not made
  // by this agent. Silently showing someone else's injection is worse than showing nothing.
  const own = inWindow.filter((it) => it.source === agentId);
  const pool = own.length > 0 ? own : inWindow;
  const picked = pool[0];
  // `inWindow.length === 0` returned above, so pool is never empty. The check is written
  // out for `noUncheckedIndexedAccess`, and it is right to be: a `pool[0]!` would hide this
  // invariant, and nothing would warn the next person who adds another filter.
  if (!picked) {
    return { kind: "none", why: tr("这一轮还没有发出模型请求。", "This turn has not made a model request yet.") };
  }
  return {
    kind: "match",
    seq: picked.seq,
    ts: picked.ts,
    source: picked.source,
    model_id: picked.model_id,
    calls: pool.length,
    systemChars: picked.system_chars,
    totalChars: picked.total_chars,
    foreignSource: picked.source !== agentId,
  };
}

/**
 * Start of every turn: the times of ALL user messages, ascending.
 *
 * Warning: compute it from the WHOLE transcript, never by index. `NarrationLane` splits
 * the messages into several segments handed to `ChatBubbles` separately (narration cards
 * sit between segments), and the last user message of a segment often has more messages
 * after it in the full transcript. Indexing would make that message's window open-ended,
 * and it would claim the requests of all later turns as its own.
 *
 * So the boundaries are a slice-independent set, queried with {@link nextTurnAfter}.
 */
export function userTurnTimes(
  msgs: { role: string; t?: number | null }[],
): number[] {
  return msgs
    .filter((m) => m.role === "user" && typeof m.t === "number"
      && Number.isFinite(m.t))
    .map((m) => m.t as number)
    .sort((a, b) => a - b);
}

/** Right boundary of a turn: the smallest value in `times` strictly greater than `t`; null if none. */
export function nextTurnAfter(
  times: number[],
  t: number | null | undefined,
): number | null {
  if (typeof t !== "number" || !Number.isFinite(t)) return null;
  let best: number | null = null;
  for (const x of times) {
    // Strictly greater: another user message within the same second must not close this turn early.
    if (x > t && (best === null || x < best)) best = x;
  }
  return best;
}

/** Injected blocks are the system-role messages. Conversation history does not count as injection, since it is visible on screen. */
export type CapturedMsg = {
  role: string;
  content: string;
  chars: number;
  truncated: boolean;
};

export function splitInjected(messages: CapturedMsg[]): {
  injected: CapturedMsg[];
  history: CapturedMsg[];
} {
  const injected = messages.filter((m) => m.role === "system");
  return { injected, history: messages.filter((m) => m.role !== "system") };
}
