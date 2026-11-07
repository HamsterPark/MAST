import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Badge, DegradedNote, EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { Button } from "@/components/controls";
import { ConfirmDialog } from "@/components/scope/ConfirmDialog";
import { tr } from "@/lib/i18n";
import { readSseStream, sseBroke, sseEndMessage } from "@/lib/sse";
import { useTranscriptRefresh } from "@/hooks/useTranscriptRefresh";
import { transcriptSource } from "@/lib/transcriptRefresh";
import { fmtClock } from "@/lib/narration";
import { AGENTS, AgentIcon, Avatar, SUPERVISOR, agentClasses, agentColorVar, agentDef } from "./registry";
import { InterruptCard, fromPollRow, type ResolveArgs } from "./InterruptCard";

// @-mention roster: the 8 agents (SUP + 7) the operator can @-target.
// Selecting a row inserts "@<agent_id> " at the caret. It is a cosmetic targeting
// hint; there is no backend routing.
const MENTIONABLE = [SUPERVISOR, ...AGENTS];

// Per-agent private chat (per-agent threads):
// session list (create / rename / delete) + a live SSE transcript. The SSE
// frame protocol matches ChatPage: POST /api/agents/{id}/chat → text/event-stream
// of {kind:"snapshot",messages:[...]} | {kind:"error"} | {kind:"done"}.

/** `t` = epoch seconds when the message happened (stamped by the backend
 *  `MessageClockMiddleware`).
 *
 *  Optional, and its absence is meaningful: no `t` means "unknown when it was
 *  said" (history already in the checkpoint before a restart), not "time zero".
 *  `null` belongs in the type: the backend field is `float | None`, which FastAPI
 *  serializes as `"t": null`. */
type Msg = { role: string; content: string; t?: number | null };

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function AgentChatPanel({ agentId }: { agentId: string }) {
  const qc = useQueryClient();
  const a = agentDef(agentId);
  const cls = agentClasses(agentId);
  const hue = agentColorVar(agentId);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [interruptNote, setInterruptNote] = useState<string | null>(null);
  const [streamMsgs, setStreamMsgs] = useState<Msg[] | null>(null);
  // When this browser's last stream ended. History newer than this means the
  // server was read again after that turn, and that copy is the one to draw
  // (rule in lib/transcriptRefresh.ts).
  const [streamEndedAt, setStreamEndedAt] = useState(0);
  const [streamErr, setStreamErr] = useState<string | null>(null);
  // Native window.confirm blocks the main thread and gives no failure feedback; use the shared dialog.
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const abortCtrl = useRef<AbortController | null>(null);

  // @-mention autocomplete: when the operator types "@" we track the
  // partial query after it and offer the 8-agent roster; selecting inserts
  // "@<agent_id> ". Closed on space, Escape, or selection.
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const [mentionQuery, setMentionQuery] = useState<string | null>(null);
  const [mentionIdx, setMentionIdx] = useState(0);

  // current "@query" fragment immediately before the caret (null = popup closed)
  function detectMention(value: string, caret: number): string | null {
    const upto = value.slice(0, caret);
    const at = upto.lastIndexOf("@");
    if (at === -1) return null;
    // "@" must start the token (preceded by start/whitespace) and have no space after
    const before = at === 0 ? "" : upto[at - 1];
    if (before && !/\s/.test(before)) return null;
    const frag = upto.slice(at + 1);
    if (/\s/.test(frag)) return null;
    return frag;
  }

  const mentionMatches = useMemo(() => {
    if (mentionQuery == null) return [];
    const q = mentionQuery.toLowerCase();
    return MENTIONABLE.filter(
      (m) =>
        !q ||
        m.id.toLowerCase().includes(q) ||
        m.short.toLowerCase().includes(q) ||
        m.cn.includes(mentionQuery),
    );
  }, [mentionQuery]);

  function onInputChange(value: string, caret: number) {
    setInput(value);
    const m = detectMention(value, caret);
    setMentionQuery(m);
    setMentionIdx(0);
  }

  // insert "@<agent_id> " replacing the active "@query" fragment at the caret
  function applyMention(agentId: string) {
    const el = inputRef.current;
    const caret = el ? el.selectionStart : input.length;
    const upto = input.slice(0, caret);
    const at = upto.lastIndexOf("@");
    if (at === -1) return;
    const next = input.slice(0, at) + `@${agentId} ` + input.slice(caret);
    setInput(next);
    setMentionQuery(null);
    setMentionIdx(0);
    // restore focus + place caret after the inserted mention
    requestAnimationFrame(() => {
      const node = inputRef.current;
      if (node) {
        const pos = at + agentId.length + 2;
        node.focus();
        node.setSelectionRange(pos, pos);
      }
    });
  }

  const convos = useQuery({
    queryKey: ["agent-chat", "conversations", agentId],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/agents/{agent_id}/conversations", {
        params: { path: { agent_id: agentId } },
      });
      if (error) throw error;
      return data;
    },
    refetchInterval: 6000,
  });

  const list = convos.data?.conversations ?? [];

  // Auto-pick the first conversation when none is selected.
  useEffect(() => {
    if (!activeId && list[0]) setActiveId(list[0].conversation_id);
    if (activeId && !list.some((c) => c.conversation_id === activeId)) {
      setActiveId(list[0]?.conversation_id ?? null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list, activeId]);

  // Reset transcript when switching agent or conversation.
  useEffect(() => {
    setStreamMsgs(null);
    setStreamEndedAt(0);
    setStreamErr(null);
  }, [agentId, activeId]);

  const history = useQuery({
    queryKey: ["agent-chat", "messages", agentId, activeId],
    enabled: !!activeId,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/agents/{agent_id}/messages", {
        params: { path: { agent_id: agentId }, query: { conversation_id: activeId as string } },
      });
      if (error) throw error;
      return data;
    },
  });

  // Auto-refresh. Invalidating only when this browser finishes its own SSE stream
  // would miss everything written from elsewhere: another machine, another tab,
  // a background agent, someone else's interjection. There is deliberately no
  // refetchInterval: refetching the whole history every N seconds is real traffic
  // over the tunnel, and almost every fetch returns what is already shown. The
  // rule is "read only when updated": the cursor is pushed over WS, with the 6 s
  // conversation-list poll as a fallback.
  useTranscriptRefresh({
    conversationId: activeId,
    updatedAt: list.find((c) => c.conversation_id === activeId)?.updated_at ?? null,
    streaming,
    onRefresh: () => {
      void qc.invalidateQueries({
        queryKey: ["agent-chat", "messages", agentId, activeId],
      });
    },
  });

  // Which copy to draw after a refetch. Writing `streamMsgs ?? history.data?.messages`
  // here would keep this browser's last SSE snapshot, which is only cleared on
  // switching conversation, in front of the refetched history, so new messages
  // would not show until the component remounts and the snapshot resets to null.
  const source = transcriptSource({
    hasSnapshot: streamMsgs !== null,
    streaming,
    streamEndedAt,
    historyUpdatedAt: history.dataUpdatedAt,
  });
  const shown: Msg[] = source === "history" ? (history.data?.messages ?? []) : (streamMsgs ?? []);

  // What this agent is currently blocked on (approval to grant, or a question
  // it asked). Polled rather than streamed: the chat SSE cannot emit while the
  // turn is blocked waiting for exactly this answer.
  const interrupts = useQuery({
    queryKey: ["agent-chat", "interrupts", agentId],
    refetchInterval: 3000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/agents/{agent_id}/interrupts", {
        params: { path: { agent_id: agentId } },
      });
      if (error) throw error;
      return data;
    },
  });
  const pendingInterrupts = interrupts.data?.interrupts ?? [];

  const resolveInterrupt = useMutation({
    mutationFn: async (vars: { interrupt_id: string; args: ResolveArgs }) => {
      const { data, error } = await api.POST(
        "/api/agents/{agent_id}/interrupts/{interrupt_id}/resolve",
        {
          params: { path: { agent_id: agentId, interrupt_id: vars.interrupt_id } },
          body: {
            decision: vars.args.decision,
            edited_args: vars.args.editedArgs ?? null,
            comment: vars.args.comment ?? null,
            selected: vars.args.selected ?? null,
            custom_text: vars.args.customText ?? null,
          },
        },
      );
      if (error) throw error;
      return data;
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["agent-chat", "interrupts", agentId] });
    },
    onSuccess: (data) => {
      if (!data?.applied) {
        // Never silent. The backend keeps the worker blocked on an
        // invalid answer precisely so it can be corrected — say what was wrong.
        setInterruptNote(data?.detail ?? tr("未生效：后端降级或该中断已处理", "Not applied: backend degraded or interrupt already handled"));
      } else {
        setInterruptNote(null);
      }
    },
    onError: (e) => setInterruptNote(String((e as Error)?.message ?? e)),
  });

  const createConvo = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/agents/{agent_id}/conversations", {
        params: { path: { agent_id: agentId } },
        body: { title: null, kind: "private", experiment_id: null },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["agent-chat", "conversations", agentId] });
      const cid = data?.conversation?.conversation_id;
      if (cid) setActiveId(cid);
    },
  });

  const renameConvo = useMutation({
    mutationFn: async (vars: { conversation_id: string; title: string }) => {
      const { data, error } = await api.PATCH(
        "/api/agents/{agent_id}/conversations/{conversation_id}",
        {
          params: { path: { agent_id: agentId, conversation_id: vars.conversation_id } },
          body: { title: vars.title },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agent-chat", "conversations", agentId] }),
  });

  const deleteConvo = useMutation({
    mutationFn: async (conversation_id: string) => {
      const { data, error } = await api.DELETE(
        "/api/agents/{agent_id}/conversations/{conversation_id}",
        { params: { path: { agent_id: agentId, conversation_id } } },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: (_d, cid) => {
      qc.invalidateQueries({ queryKey: ["agent-chat", "conversations", agentId] });
      if (activeId === cid) setActiveId(null);
    },
  });

  async function send() {
    const text = input.trim();
    if (!text || streaming || !activeId) return;
    setInput("");
    setMentionQuery(null);
    setStreamErr(null);
    setStreaming(true);
    setStreamEndedAt(0);
    // Append to the copy currently on screen, not to the previous turn's snapshot:
    // once someone else has written to this conversation the two differ.
    // `t` must be carried along: dropping it raises no tsc error (optional field)
    // and only shows as timestamps occasionally missing. Guarded by the structural
    // gate in test/chatMsgFields.test.ts.
    const base: Msg[] = shown.map((m) => ({ role: m.role, content: m.content, t: m.t }));
    setStreamMsgs([
      ...base,
      { role: "user", content: escapeHtml(text), t: Date.now() / 1000 },
    ]);
    const ctrl = new AbortController();
    abortCtrl.current = ctrl;
    try {
      const resp = await fetch(`/api/agents/${agentId}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: activeId, user_text: text }),
        signal: ctrl.signal,
      });
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
      const end = await readSseStream(resp, {
        controller: ctrl,
        onFrame: (frame: { kind?: string; messages?: Msg[]; message?: string }) => {
          if (frame.kind === "snapshot" && Array.isArray(frame.messages)) {
            setStreamMsgs(
              frame.messages.map((m) => ({ role: m.role, content: m.content, t: m.t })),
            );
          } else if (frame.kind === "error") {
            setStreamErr(frame.message ?? tr("对话出错", "Chat error"));
          }
        },
      });
      // EOF without the server's `done` frame (or 75 s of silence past the 15 s
      // keep-alive) means the link was cut, not that the turn finished; treating
      // them alike would silently show a truncated answer as a complete one.
      const cause = sseEndMessage(end);
      if (sseBroke(end) && cause) {
        setStreamErr(tr(`${cause}本轮回答仍在后端继续，已自动重新载入历史。`, `${cause} The answer for this turn continues on the backend; history was reloaded automatically.`));
      }
    } catch (err) {
      if ((err as Error)?.name !== "AbortError")
        setStreamErr(tr(`连接出错：${String((err as Error)?.message ?? err)}`, `Connection error: ${String((err as Error)?.message ?? err)}`));
    } finally {
      setStreaming(false);
      // Hand this turn back to the server history: from now on any refetch newer than this overrides the snapshot.
      setStreamEndedAt(Date.now());
      abortCtrl.current = null;
      qc.invalidateQueries({ queryKey: ["agent-chat", "messages", agentId, activeId] });
      qc.invalidateQueries({ queryKey: ["agent-chat", "conversations", agentId] });
    }
  }

  async function stop() {
    try {
      await api.POST("/api/agents/{agent_id}/chat/abort", { params: { path: { agent_id: agentId } } });
    } catch {
      /* best-effort */
    }
    abortCtrl.current?.abort();
    setStreaming(false);
  }

  const degraded = convos.data?.degraded;

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[260px_1fr]">
      {/* session list */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span
            className={
              "inline-flex items-center gap-2 rounded-mast-ctl border bg-mast-panel px-2.5 py-1 text-sm font-medium text-mast-text " +
              cls.border
            }
            style={{ borderLeftWidth: 3, borderLeftColor: hue }}
          >
            <Avatar id={agentId} size={20} />
            <span className={"inline-flex " + cls.text}>
              <AgentIcon id={agentId} size={13} />
            </span>
            {tr(`${a.cn} 会话`, `${a.cn} sessions`)}
          </span>
          <Button variant="primary" onClick={() => createConvo.mutate()}>
            {tr("＋ 新建", "+ New")}
          </Button>
        </div>
        {convos.isPending && <Spinner />}
        {convos.isError && <ErrorNote error={convos.error} />}
        {convos.data && degraded && <DegradedNote what={tr("代理对话", "Agent conversations")} />}
        {convos.data && !degraded && list.length === 0 && <EmptyNote label={tr("暂无会话，点「新建」开始。", "No sessions yet. Click \"New\" to start.")} />}
        <div className="max-h-[460px] space-y-1 overflow-auto">
          {list.map((c) => (
            <div
              key={c.conversation_id}
              className={
                "group flex items-center gap-1 rounded-mast-ctl border px-2 py-1.5 text-sm " +
                (c.conversation_id === activeId
                  ? cls.border + " bg-mast-panel-2"
                  : "border-mast-border hover:bg-mast-panel-2/40")
              }
              style={
                c.conversation_id === activeId
                  ? { borderLeftWidth: 3, borderLeftColor: hue }
                  : undefined
              }
            >
              <button
                className="min-w-0 flex-1 truncate text-left"
                onClick={() => setActiveId(c.conversation_id)}
                title={c.last_message_preview}
              >
                {c.title || tr("新对话", "New conversation")}
              </button>
              <button
                className="text-mast-muted opacity-0 hover:text-mast-text group-hover:opacity-100"
                title={tr("重命名", "Rename")}
                onClick={() => {
                  const t = window.prompt(tr("重命名会话", "Rename session"), c.title);
                  if (t != null && t.trim()) renameConvo.mutate({ conversation_id: c.conversation_id, title: t.trim() });
                }}
              >
                ✎
              </button>
              <button
                className="text-mast-danger opacity-0 group-hover:opacity-100"
                title={tr("删除", "Delete")}
                onClick={() => setPendingDelete(c.conversation_id)}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      </div>

      {/* transcript */}
      <div
        className={"flex min-h-[460px] flex-col rounded-mast-card border border-mast-border bg-mast-panel shadow-mast " + cls.border}
        style={{ borderLeftWidth: 3, borderLeftColor: hue }}
      >
        <div className="flex-1 space-y-3 overflow-auto p-4">
          {/* Anything this agent is blocked on, INLINE. A private chat that hits an
              approval, or asks the operator a question, needs somewhere to show it;
              otherwise the turn just stops. */}
          {pendingInterrupts.map((it) => (
            <InterruptCard
              key={it.event_id}
              it={fromPollRow(it, agentId)}
              busy={resolveInterrupt.isPending && resolveInterrupt.variables?.interrupt_id === it.event_id}
              compact
              onResolve={(args) => resolveInterrupt.mutate({ interrupt_id: it.event_id, args })}
            />
          ))}
          {interruptNote && (
            <p className="rounded-md border border-mast-danger-border bg-mast-danger-bg px-3 py-2 text-xs text-mast-danger">
              {interruptNote}
            </p>
          )}
          {!activeId && <EmptyNote label={tr("选择或新建一个会话以开始私聊。", "Select or create a session to start a private chat.")} />}
          {activeId && history.isPending && !streamMsgs && <Spinner />}
          {activeId && shown.length === 0 && !history.isPending && (
            <EmptyNote label={tr("该会话暂无消息。", "No messages in this session yet.")} />
          )}
          {shown.map((m, i) => {
            const isUser = m.role === "user";
            const isAgentMsg = m.role === "assistant";
            // If unavailable, render nothing (`fmtClock` returns an empty string for invalid values); never show 1970.
            const clock = fmtClock(m.t);
            return (
              <div key={i} className={"flex " + (isUser ? "justify-end" : "justify-start")}>
                <div
                  className={
                    "max-w-[85%] text-sm text-mast-text " +
                    (isUser
                      ? "rounded-mast-card rounded-br-[3px] border border-mast-accent/40 bg-mast-accent-soft px-3.5 py-2.5"
                      : "overflow-hidden rounded-mast-card border border-mast-border bg-mast-panel shadow-mast " +
                        (isAgentMsg ? "rounded-l-none " + cls.borderL : ""))
                  }
                >
                  {isUser && clock && (
                    <div className="mb-1 text-right font-mono text-[10px] tabular-nums text-mast-faint">
                      {clock}
                    </div>
                  )}
                  {!isUser && (
                    <div className="mb-1 flex items-center gap-2 border-b border-mast-border bg-mast-panel-2 px-3 py-1.5">
                      {isAgentMsg ? (
                        <>
                          <span
                            className="inline-flex h-[18px] w-[18px] items-center justify-center rounded-mast-badge font-mono text-[9px] font-bold text-white"
                            style={{ background: hue }}
                          >
                            {a.short}
                          </span>
                          <span className={"inline-flex " + cls.text}>
                            <AgentIcon id={agentId} size={12} />
                          </span>
                          <span className="text-[11.5px] font-medium text-mast-text">{a.cn}</span>
                        </>
                      ) : (
                        <Badge tone="INFO">{m.role}</Badge>
                      )}
                    </div>
                  )}
                  <div
                    className={
                      (isUser ? "" : "px-3.5 py-2.5 ") +
                      "prose-sm whitespace-pre-wrap break-words " +
                      "[&_code]:font-mono [&_code]:tabular-nums [&_code]:text-mast-accent " +
                      "[&_details]:my-1.5 [&_details]:rounded-mast-ctl [&_details]:bg-mast-code-bg [&_details]:px-3 [&_details]:py-2 [&_details]:text-xs [&_details]:text-mast-muted [&_summary]:cursor-pointer [&_summary]:text-mast-dream"
                    }
                    dangerouslySetInnerHTML={{ __html: m.content }}
                  />
                </div>
              </div>
            );
          })}
          {streamErr && <ErrorNote error={streamErr} label={tr("运行出错", "Run error")} />}
        </div>
        <div className="relative flex items-end gap-2 border-t border-mast-border p-3">
          {/* @-mention autocomplete popup */}
          {mentionQuery != null && mentionMatches.length > 0 && (
            <div className="absolute bottom-full left-3 z-20 mb-1 w-64 overflow-hidden rounded-mast-ctl border border-mast-border bg-mast-panel shadow-mast">
              <div className="border-b border-mast-border bg-mast-panel-2 px-2.5 py-1 text-[11px] text-mast-muted">
                {tr("@ 指向代理", "@ address an agent")}
              </div>
              {mentionMatches.map((m, i) => {
                const mc = agentClasses(m.id);
                return (
                  <button
                    key={m.id}
                    type="button"
                    onMouseDown={(e) => {
                      e.preventDefault();
                      applyMention(m.id);
                    }}
                    className={
                      "flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-sm " +
                      (i === mentionIdx
                        ? "bg-mast-panel-2 text-mast-text"
                        : "text-mast-muted hover:bg-mast-panel-2/50")
                    }
                    style={i === mentionIdx ? { borderLeft: `3px solid ${agentColorVar(m.id)}` } : { borderLeft: "3px solid transparent" }}
                  >
                    <Avatar id={m.id} size={16} />
                    <span className={"font-mono text-xs font-semibold " + mc.text}>{m.short}</span>
                    <span className="text-xs">{m.cn}</span>
                    <span className="ml-auto font-mono text-[10px] text-mast-faint">@{m.id}</span>
                  </button>
                );
              })}
            </div>
          )}
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => onInputChange(e.target.value, e.target.selectionStart)}
            onKeyUp={(e) => {
              const el = e.currentTarget;
              setMentionQuery(detectMention(el.value, el.selectionStart));
            }}
            onKeyDown={(e) => {
              // when the @-popup is open, arrows/enter/tab drive selection
              if (mentionQuery != null && mentionMatches.length > 0) {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setMentionIdx((n) => (n + 1) % mentionMatches.length);
                  return;
                }
                if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setMentionIdx((n) => (n - 1 + mentionMatches.length) % mentionMatches.length);
                  return;
                }
                if (e.key === "Enter" || e.key === "Tab") {
                  e.preventDefault();
                  applyMention(mentionMatches[mentionIdx]!.id);
                  return;
                }
                if (e.key === "Escape") {
                  e.preventDefault();
                  setMentionQuery(null);
                  return;
                }
              }
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder={activeId ? tr(`私聊 ${a.cn}…（@ 指向代理 · Enter 发送，Shift+Enter 换行）`, `Chat privately with ${a.cn}… (@ address an agent · Enter to send, Shift+Enter for newline)`) : tr("请先选择会话", "Select a session first")}
            disabled={!activeId || streaming}
            rows={2}
            className="flex-1 resize-none rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-3 py-2 text-sm text-mast-text outline-none focus:border-mast-accent disabled:opacity-50"
          />
          {streaming ? (
            <Button variant="danger" onClick={stop}>
              {tr("停止", "Stop")}
            </Button>
          ) : (
            <Button variant="primary" onClick={send} disabled={!activeId || !input.trim()}>
              {tr("发送", "Send")}
            </Button>
          )}
        </div>
      </div>
      <ConfirmDialog
        open={!!pendingDelete}
        onClose={() => setPendingDelete(null)}
        title={tr("删除会话", "Delete session")}
        tone="danger"
        confirmLabel={tr("删除", "Delete")}
        busy={deleteConvo.isPending}
        busyLabel={tr("删除中…", "Deleting…")}
        onConfirm={() => {
          if (pendingDelete) deleteConvo.mutate(pendingDelete);
          setPendingDelete(null);
        }}
      >
        <p className="text-sm text-mast-text">{tr("删除该会话及其转录？", "Delete this session and its transcript?")}</p>
        <p className="text-xs text-mast-muted">
          {tr("若它已被导出到实验文件夹，文件夹里的那份不受影响。", "If it was exported to the experiment folder, the copy there is unaffected.")}
        </p>
      </ConfirmDialog>
    </div>
  );
}
