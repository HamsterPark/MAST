import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { api } from "@/api/client";
import { tr, getLang } from "@/lib/i18n";
import {
  matchInjectedContext,
  nextTurnAfter,
  splitInjected,
  type CaptureList,
  type CapturedMsg,
} from "@/lib/injectedContext";

/** What the system injected into the model after a user message was sent: a
 *  collapsible block attached under every user message, for debugging.
 *
 *  ## No request until someone opens it
 *
 *  Before expansion this component queries nothing (both queries are
 *  `enabled: open`). A transcript holds dozens of user messages, and a poll per
 *  message would be dozens of unwatched requests. Collapsed, it is just a button.
 *
 *  ## It shows the real request, not a replay
 *
 *  The data comes from the ring buffer in `mast/prompts/capture.py`: the message
 *  list the model ACTUALLY received. An offline replay cannot reproduce that
 *  turn's hardware readings, conversation history or middleware order, so it
 *  would show something that looks like the injection rather than the injection.
 *
 *  The cost is that it can fail to match (process restarted, the turn was pushed
 *  out of the buffer, capture is disabled). Each mismatch case is reported
 *  separately, see `lib/injectedContext.ts`: one generic "no data" would make
 *  all of them look like "try again and it may appear".
 */

function fmtChars(n: number): string {
  if (n >= 1000) return tr(`${(n / 1000).toFixed(1)}k 字符`, `${(n / 1000).toFixed(1)}k chars`);
  return tr(`${n} 字符`, `${n} chars`);
}

/** Expanded body: the full message list of that request, fetched by seq. */
function CaptureBody({ seq }: { seq: number }) {
  const q = useQuery({
    // Fetch by seq, not by index: the index shifts on every model call, and this
    // panel is typically opened while the agent is still running.
    queryKey: ["prompt-capture", "by-seq", seq],
    queryFn: async () => {
      const { data, error } = await api.GET(
        "/api/admin/prompt-capture/by-seq/{seq}",
        { params: { path: { seq } } },
      );
      if (error) throw error;
      return data;
    },
    staleTime: Infinity,   // the content of a request never changes
  });

  if (q.isPending) {
    return <p className="px-3 py-2 text-[11px] text-mast-muted">{tr("读取快照…", "Loading snapshot…")}</p>;
  }
  if (q.error || !q.data) {
    return (
      <p className="px-3 py-2 text-[11px] text-mast-warn">
        {tr(`读不到这份快照（${String(q.error ?? "无回包")}）。`, `Could not read this snapshot (${String(q.error ?? "no response")}).`)}
      </p>
    );
  }
  const d = q.data;
  if (!d.found) {
    return (
      <p className="px-3 py-2 text-[11px] text-mast-warn">
        {tr("这一份已经被后面的请求挤出记录了 —— 不是没有注入过。", "This snapshot has been pushed out of the buffer by later requests; injection did take place.")}
      </p>
    );
  }

  const messages = (d.messages ?? []) as CapturedMsg[];
  const { injected, history } = splitInjected(messages);

  return (
    <div className="space-y-2 px-3 pb-3 pt-2">
      <p className="text-[11px] leading-relaxed text-mast-faint">
        {new Date((d.ts ?? 0) * 1000).toLocaleString()} · {d.source} ·{" "}
        {d.model_id}
        {(d.dropped_messages ?? 0) > 0 && (
          <span className="text-mast-warn">
            {" "}
            {tr(`· 另有 ${d.dropped_messages} 条消息因体积上限未记录`, `· ${d.dropped_messages} more messages not recorded (size limit)`)}
          </span>
        )}
      </p>
      {/* State the boundary: someone who assumes this is the whole payload will
          draw wrong conclusions about token cost and what the model could see.
          Tool definitions are bound to the model and do not travel in the message
          list. Emphasis uses tags: `**bold**` in JSX would show literal asterisks. */}
      <p className="text-[11px] leading-relaxed text-mast-faint">
        {getLang() === "zh" ? (
          <>
            这里只有<strong className="text-mast-muted">消息列表</strong>
            。那几百个仪器控制工具的定义是绑在模型上的，不走消息，所以不在这份记录里
            （要看去 Agents → 工具）。
          </>
        ) : (
          <>
            This shows only the <strong className="text-mast-muted">message list</strong>.
            The hundreds of instrument-control tool definitions are bound to the model and
            do not travel as messages, so they are not in this record (see Agents → Tools).
          </>
        )}
      </p>

      {injected.length === 0 ? (
        <p className="text-[11px] text-mast-warn">
          {getLang() === "zh" ? (
            <>
              这次请求里一条 system 消息都没有 —— 也就是说这一轮
              <strong>没有系统注入</strong>。
            </>
          ) : (
            <>
              This request contains no system message, i.e. this turn had{" "}
              <strong>no system injection</strong>.
            </>
          )}
        </p>
      ) : (
        injected.map((m, i) => (
          <div
            key={`sys-${i}`}
            className="rounded-mast-ctl border border-mast-border bg-mast-bg"
          >
            <div className="flex items-center justify-between border-b border-mast-border px-2.5 py-1 text-[10px]">
              <span className="font-mono text-mast-accent">{m.role}</span>
              <span className="font-mono text-mast-muted">
                {fmtChars(m.chars ?? 0)}
                {m.truncated && (
                  <span className="ml-2 text-mast-warn">{tr("已截断显示", "Display truncated")}</span>
                )}
              </span>
            </div>
            <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words p-2.5 font-mono text-[11px] leading-relaxed text-mast-text">
              {m.content}
            </pre>
          </div>
        ))
      )}

      {history.length > 0 && (
        <details className="rounded-mast-ctl border border-mast-border bg-mast-bg">
          <summary className="cursor-pointer select-none px-2.5 py-1.5 text-[11px] text-mast-muted">
            {tr(`同时带过去的对话历史 ${history.length} 条（不是注入，屏幕上就看得见）`, `${history.length} conversation history messages sent along (not injected; visible on screen)`)}
          </summary>
          <div className="space-y-1.5 px-2.5 pb-2.5">
            {history.map((m, i) => (
              <div key={`hist-${i}`}>
                <div className="font-mono text-[10px] text-mast-faint">
                  {m.role} · {fmtChars(m.chars ?? 0)}
                </div>
                <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-mast-muted">
                  {m.content}
                </pre>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

export function InjectedContext({
  t,
  turnTimes,
  agentId,
}: {
  /** Epoch seconds of this user message. Without it no match is possible, and that is reported too. */
  t?: number | null;
  /** Times of all user messages in the WHOLE transcript (ascending); see `userTurnTimes`. */
  turnTimes: number[];
  agentId: string;
}) {
  const [open, setOpen] = useState(false);

  const listQ = useQuery({
    queryKey: ["prompt-capture", "list"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/admin/prompt-capture");
      if (error) throw error;
      return data as CaptureList;
    },
    enabled: open,        // no request while collapsed
    staleTime: 2000,
  });

  const body = () => {
    if (listQ.isPending) {
      return <p className="px-3 py-2 text-[11px] text-mast-muted">{tr("对齐这一轮…", "Matching this turn…")}</p>;
    }
    if (listQ.error || !listQ.data) {
      return (
        <p className="px-3 py-2 text-[11px] text-mast-warn">
          {tr(`读不到注入记录（${String(listQ.error ?? "无回包")}）。`, `Could not read the injection record (${String(listQ.error ?? "no response")}).`)}
        </p>
      );
    }
    const m = matchInjectedContext(
      t, nextTurnAfter(turnTimes, t), listQ.data, agentId);
    if (m.kind === "none") {
      return (
        <p className="px-3 py-2 text-[11px] leading-relaxed text-mast-muted">
          {m.why}
        </p>
      );
    }
    return (
      <>
        <p className="px-3 pt-2 text-[11px] leading-relaxed text-mast-faint">
          {getLang() === "zh" ? (
            <>
              这一轮发出 {m.calls} 次模型调用；下面是
              <strong className="text-mast-muted">第一次</strong>带过去的那份
              （system {fmtChars(m.systemChars)}，整个请求 {fmtChars(m.totalChars)}）。
            </>
          ) : (
            <>
              This turn made {m.calls} model call(s); below is the{" "}
              <strong className="text-mast-muted">first</strong> request sent
              (system {fmtChars(m.systemChars)}, whole request {fmtChars(m.totalChars)}).
            </>
          )}
          {m.foreignSource && (
            <span className="text-mast-warn">
              {" "}
              {tr(
                `⚠️ 这一次是 ${m.source} 发的，不是本对话的 ${agentId} —— 时间窗里没有后者的请求，展示的是同一时刻别人的那次。`,
                `⚠️ This request was sent by ${m.source}, not by this conversation's ${agentId}: the time window holds no request from the latter, so another sender's request at the same moment is shown.`,
              )}
            </span>
          )}
        </p>
        <CaptureBody seq={m.seq} />
      </>
    );
  };

  return (
    <div className="mt-1.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={clsx(
          "inline-flex items-center gap-1 rounded-mast-badge px-1.5 py-0.5",
          "font-mono text-[10px] text-mast-faint transition-colors",
          "hover:bg-mast-panel-2 hover:text-mast-muted",
        )}
        aria-expanded={open}
      >
        <span className={clsx("transition-transform", open && "rotate-90")}>
          ▸
        </span>
        {tr("注入的上下文", "Injected context")}
      </button>
      {open && (
        <div className="mt-1 rounded-mast-ctl border border-mast-border bg-mast-panel text-left">
          {body()}
        </div>
      )}
    </div>
  );
}
