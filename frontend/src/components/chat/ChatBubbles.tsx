import { useRef } from "react";
import clsx from "clsx";
import { AgentIcon, agentClasses, agentColorVar, agentDef } from "@/components/agents/registry";
import { useStickToBottom } from "@/hooks/useStickToBottom";
import { InjectedContext } from "@/components/chat/InjectedContext";
import { fmtClock } from "@/lib/narration";
import { userTurnTimes } from "@/lib/injectedContext";
import { tr } from "@/lib/i18n";

/** Rendered chat history. Backend `content` is PRE-RENDERED, SANITIZED HTML
 *  (markdown → HTML, thinking wrapped in <details>; see mast.chat.render +
 *  chat.py _esc_llm). We therefore render it with dangerouslySetInnerHTML inside
 *  a styled wrapper — never re-escape, never re-parse. */

/** `t` is the epoch second at which the message was produced, stamped by the
 *  backend `MessageClockMiddleware`.
 *
 *  Optional on purpose: a missing `t` means "unknown" (history that was already in
 *  the checkpoint before a restart), not "time zero". No default is supplied and
 *  `fmtClock` returns an empty string for invalid values, so 1970 is never shown.
 *
 *  `null` must be part of the type: the backend `ChatMessage.t` is `float | None`
 *  and FastAPI serialises it as `"t": null` rather than omitting the key. */
export type ChatMsg = { role: string; content: string; t?: number | null };

const ROLE_LABEL: Record<string, string> = {
  user: tr("你", "You"),
  assistant: tr("助手", "Assistant"),
  system: tr("系统", "System"),
  tool: tr("工具", "Tool"),
};

// Tailwind arbitrary-variant restyle of the pre-rendered HTML body. We cannot
// touch the inner markup (it is sanitized backend HTML), so we reach into it:
//   • the <details>…</details> thinking / CoT block → code-bg card, muted text
//   • inline numeric values (bias/setpoint, wrapped in <code> by the renderer)
//     → mono + accent so params read like the canvas
// Class strings stay complete literals so Tailwind never purges them.
const CHAT_HTML_CLASS = clsx(
  "mast-chat-html text-sm leading-relaxed text-mast-text [overflow-wrap:anywhere]",
  // thinking / chain-of-thought (<details>) → bg-mast-code-bg rounded-mast-ctl text-mast-muted
  "[&_details]:my-2 [&_details]:rounded-mast-ctl [&_details]:bg-mast-code-bg [&_details]:px-3 [&_details]:py-2",
  "[&_details]:text-xs [&_details]:text-mast-muted [&_summary]:cursor-pointer [&_summary]:select-none",
  "[&_summary]:text-mast-dream [&_summary]:font-medium",
  // inline numeric values / params → font-mono text-mast-accent (tabular)
  "[&_code]:font-mono [&_code]:tabular-nums [&_code]:text-mast-accent",
  "[&_pre]:rounded-mast-ctl [&_pre]:bg-mast-code-bg [&_pre]:p-3 [&_pre]:text-xs [&_pre]:text-mast-muted",
  "[&_pre_code]:text-mast-text",
);

/** Small agent chip: 18px rounded-mast-badge filled with the agent hue, mono
 *  initials (matches the canvas assistant-message header chip). */
function AgentChip({ agentId }: { agentId: string }) {
  const a = agentDef(agentId);
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="inline-flex h-[18px] w-[18px] items-center justify-center rounded-mast-badge font-mono text-[9px] font-bold text-white"
        style={{ background: agentColorVar(agentId) }}
      >
        {a.short}
      </span>
      <span className={clsx("inline-flex items-center", agentClasses(agentId).text)}>
        <AgentIcon id={agentId} size={12} />
      </span>
      <span className="text-[11.5px] font-medium text-mast-text">{tr(`${a.cn} 助手`, `${a.cn} assistant`)}</span>
    </span>
  );
}

function Bubble({
  msg,
  agentId,
  turnTimes,
}: {
  msg: ChatMsg;
  agentId: string;
  /** Start time of every turn in the whole transcript (ascending). See `lib/injectedContext.ts::userTurnTimes`. */
  turnTimes: number[];
}) {
  const isUser = msg.role === "user";
  // Not rendered when unavailable. The timestamp makes the ordering checkable:
  // narration and messages share one timeline, so an entry placed earlier but
  // timestamped later is immediately visible.
  const clock = fmtClock(msg.t);

  // USER → accent-aligned (right), accent-soft fill — unchanged identity.
  if (isUser) {
    return (
      <div className="flex w-full justify-end">
        <div className="max-w-[80%] rounded-mast-card rounded-br-[3px] border border-mast-accent/40 bg-mast-accent-soft px-3.5 py-2.5">
          {clock && (
            <div className="mb-1 text-right font-mono text-[10px] tabular-nums text-mast-faint">
              {clock}
            </div>
          )}
          <div className="text-sm leading-relaxed text-mast-text [overflow-wrap:anywhere]">
            <div
              className="mast-chat-html"
              dangerouslySetInnerHTML={{ __html: msg.content }}
            />
          </div>
          {/* What the system injected into the model after this message was sent.
              Collapsed by default and issues no request while collapsed. Attached to
              the user bubble because the injection happens after the message and
              before the answer. */}
          <InjectedContext t={msg.t} turnTimes={turnTimes} agentId={agentId} />
        </div>
      </div>
    );
  }

  // SYSTEM / TOOL → neutral panel (no agent identity).
  const isAgentMsg = msg.role === "assistant";
  const cls = agentClasses(agentId);

  return (
    <div className="flex w-full justify-start">
      <div
        className={clsx(
          "max-w-[80%] overflow-hidden rounded-mast-card border border-mast-border bg-mast-panel shadow-mast",
          isAgentMsg && [cls.borderL, "rounded-l-none"],
        )}
      >
        {/* header strip with the agent chip (assistant) or plain role label */}
        <div className="flex items-center gap-2 border-b border-mast-border bg-mast-panel-2 px-3.5 py-2">
          {isAgentMsg ? (
            <AgentChip agentId={agentId} />
          ) : (
            <span className="text-[11.5px] font-medium text-mast-muted">
              {ROLE_LABEL[msg.role] ?? msg.role}
            </span>
          )}
          {clock && (
            <span className="ml-auto font-mono text-[10px] tabular-nums text-mast-faint">
              {clock}
            </span>
          )}
        </div>
        <div className="px-3.5 py-3">
          <div
            className={CHAT_HTML_CLASS}
            dangerouslySetInnerHTML={{ __html: msg.content }}
          />
        </div>
      </div>
    </div>
  );
}

export function ChatBubbles({
  messages,
  pending,
  agentId = "instrument_control",
  autoScroll = true,
  allMessages,
}: {
  messages: ChatMsg[];
  pending?: boolean;
  // Which agent owns this transcript (drives the chip + left-border color).
  // The main console chat is the IC private chat, so IC is the default.
  agentId?: string;
  /**
   * Scroll this block's end into view on every update. Default true (the whole
   * transcript is one block). NarrationLane renders SEVERAL blocks with
   * narration cards between them, and every block scrolling itself into view
   * means N competing smooth-scrolls per render — the view lands wherever the
   * race ends. Only the last block sets this.
   */
  autoScroll?: boolean;
  /**
   * The WHOLE transcript (`messages` may be only one segment of it). Used only to
   * compute where each turn starts, which bounds the injected-context block.
   *
   * It must be the whole transcript: `NarrationLane` splits messages into several
   * segments at narration cards, and the last user message of a segment usually has
   * later turns in the full transcript. Looking at one segment alone would leave that
   * message's time window open-ended and attribute all later requests to it.
   *
   * Falls back to `messages` when omitted (unsegmented callers).
   */
  allMessages?: ChatMsg[];
}) {
  const endRef = useRef<HTMLDivElement>(null);
  const turnTimes = userTurnTimes(allMessages ?? messages);

  // An unconditional `scrollIntoView({behavior:"smooth"})` had two problems:
  //   1. it ignores where the user is looking, so scrolling up through history gets
  //      undone by the next message;
  //   2. it scrolls EVERY scrollable ancestor, so both the chat and the page scroll.
  // Instead, scroll only the nearest container, and only when the user is already
  // at the bottom.
  useStickToBottom(endRef, [messages, pending], autoScroll);

  const cls = agentClasses(agentId);

  return (
    <div className="flex flex-col gap-3.5">
      {messages.map((m, i) => (
        <Bubble key={`${i}-${m.role}`} msg={m} agentId={agentId}
                turnTimes={turnTimes} />
      ))}
      {pending && (
        <div className="flex justify-start">
          <div
            className={clsx(
              "rounded-mast-card rounded-l-none border border-mast-border bg-mast-panel px-3.5 py-2.5 text-sm text-mast-muted shadow-mast",
              cls.borderL,
            )}
          >
            <span className="inline-flex items-center gap-2">
              <span className={clsx("inline-flex", cls.text)}>
                <AgentIcon id={agentId} size={13} />
              </span>
              {tr(`${agentDef(agentId).cn}思考中…`, `${agentDef(agentId).cn} is thinking…`)}
            </span>
          </div>
        </div>
      )}
      <div ref={endRef} />
    </div>
  );
}
