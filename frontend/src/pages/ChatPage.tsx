import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { tr } from "@/lib/i18n";
import { Section, Card, Spinner, ErrorNote, DegradedNote, EmptyNote } from "@/components/ui";
import { SubTabs } from "@/components/controls";
import { ConfirmDialog } from "@/components/scope/ConfirmDialog";
import { useCurrentScope } from "@/api/scope";
import { type ChatMsg } from "@/components/chat/ChatBubbles";
// Bubbles are still drawn by ChatBubbles; NarrationLane only inserts narration
// cards between them and, with narration off, forwards to ChatBubbles unchanged
// (it is not a second render path).
import { NarrationLane } from "@/components/chat/NarrationLane";
import { DataStrip } from "@/components/chat/DataStrip";
import { VisionRibbon } from "@/components/chat/VisionRibbon";
import { QuickPrompts } from "@/components/chat/QuickPrompts";
import { useVoiceOutput } from "@/components/chat/VoiceControls";
import { VoiceDock } from "@/components/chat/VoiceDock";
import {
  PendingInterrupts,
  useInterrupts,
  type PendingInterruptsHandle,
} from "@/components/chat/PendingInterrupts";
import { useUiStore } from "@/store";
import { useDraft } from "@/hooks/useDraft";
import { useTranscriptRefresh } from "@/hooks/useTranscriptRefresh";
import { transcriptSource } from "@/lib/transcriptRefresh";
import { useStickyTab } from "@/hooks/useStickyTab";
import { readSseStream, sseBroke, sseEndMessage } from "@/lib/sse";
import { ScanMapPanel } from "@/components/vision/ScanMapPanel";
import { CoarseMapPanel } from "@/components/vision/CoarseMapPanel";
import { useCoarseMap } from "@/hooks/useCoarseMap";
import { PulseRibbon } from "@/components/vision/PulseRibbon";
import { VisionBufferPanel } from "@/components/vision/VisionBufferPanel";
import { RecentFrameThumb } from "@/components/vision/RecentFrameThumb";
import { useRunTaskStore } from "@/components/agents/runTaskStore";
import { PendingRecommendations } from "@/components/skills/PendingRecommendations";

// ════════════════════════════════════════════════════════════════════════
//  Chat page: the private chat with the instrument-control (IC) agent.
//
//  Surfaces:
//    · LATEST DATA thumbnail strip (recent .sxm scans + open data browser) -> DataStrip
//    · conversation picker (dropdown + new + rename + delete)
//    · quick-prompt buttons                                               -> QuickPrompts
//    · full-duplex voice (streaming ASR/TTS, three modes, barge-in)       -> VoiceDock
//    · voice read-aloud (/api/voice/synthesize + autoplay)                -> useVoiceOutput
//    · conversation feedback widget (draggable, rating + comment)         -> FeedbackFloat
//    · vision pulse ribbon (/api/vision/pulse)                            -> VisionRibbon
//    · inline human-in-the-loop panel (/api/agents/{id}/interrupts)       -> PendingInterrupts
//
//  Endpoints consumed:
//    GET  /api/agents/{id}/conversations | /messages | /interrupts
//    POST /api/agents/{id}/conversations | PATCH/DELETE | /chat (SSE) | /chat/abort
//    GET  /api/chat/quick-prompts | /api/hardware/live-readings | /api/vision/pulse
//    GET  /api/config/models | /api/agents/models
//    POST /api/voice/transcribe | /api/voice/synthesize | /api/feedback
//
//  The page has two in-page sub-tabs, Chat | Vision Buffer:
//    · Chat          = the chat surface (ChatSurface).
//    · Vision Buffer = scan map + vision pulse + recent frames + buffer events
//                      (/api/scan-map, /api/vision/pulse, /api/vision/recent,
//                       /api/vision/buffer).
//  The "Open Vision Buffer" link of the pulse ribbon on the Chat sub-tab switches
//  to the Vision Buffer sub-tab (no route change).
// ════════════════════════════════════════════════════════════════════════

const AGENT_ID = "instrument_control";

type Conversation = {
  conversation_id: string;
  title: string;
  last_message_preview: string;
  updated_at?: string | null;
};

function useConversations() {
  return useQuery({
    queryKey: ["chat", "conversations", AGENT_ID],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/agents/{agent_id}/conversations", {
        params: { path: { agent_id: AGENT_ID } },
      });
      if (error) throw error;
      return data;
    },
    refetchInterval: 5000,
  });
}

function useMessages(conversationId: string | null) {
  return useQuery({
    queryKey: ["chat", "messages", AGENT_ID, conversationId],
    enabled: !!conversationId,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/agents/{agent_id}/messages", {
        params: {
          path: { agent_id: AGENT_ID },
          query: { conversation_id: conversationId as string },
        },
      });
      if (error) throw error;
      return data;
    },
  });
}

// ────────────────────────────────────────────────────────────────────────
//  IC group-activity strip — real-time mirror of what instrument_control is
//  doing in a RUNNING group chat (multi-agent orchestrator run). This page is the
//  IC PRIVATE chat, a separate stream from the group run, so without this strip
//  nothing of IC's team-run work would be visible here.
//  Subscribes to the module-scoped run-task store (live, no poll). Renders
//  nothing when there's no group activity — never occupies space / never freezes.
// ────────────────────────────────────────────────────────────────────────
function IcGroupActivityStrip() {
  // Subscribe to the STABLE store-owned `entries` ref, derive in useMemo. A
  // `useShallow` selector that `.map`s into new object literals returns a fresh
  // array every call → unstable getSnapshot → React #185 infinite render.
  const entries = useRunTaskStore((s) => s.entries);
  const running = useRunTaskStore((s) => s.running);
  const ic = useMemo(
    () =>
      entries
        .filter((e) => e.kind === "message" && e.agent === AGENT_ID)
        .map((e) => (e.kind === "message" ? { role: e.role as string, text: e.text } : null))
        .filter((x): x is { role: string; text: string } => x != null),
    [entries],
  );
  if (ic.length === 0) return null;
  const recent = ic.slice(-6);
  return (
    <div className="rounded-lg border border-mast-accent/30 bg-mast-accent/5 px-3 py-2">
      <div className="mb-1 flex items-center gap-2 text-xs font-medium text-mast-text">
        <span>{tr("仪器控制 · 群聊中的实时活动", "Instrument control · live activity in group chat")}</span>
        {running && (
          <span className="inline-flex items-center gap-1 text-mast-accent">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-mast-accent" />
            {tr("进行中", "Running")}
          </span>
        )}
        <span className="ml-auto font-mono text-[10px] text-mast-muted">{tr(`${ic.length} 条`, `${ic.length} items`)}</span>
      </div>
      <div className="max-h-28 space-y-1 overflow-auto">
        {recent.map((e, i) => (
          <div key={i} className="flex items-start gap-2 text-xs">
            {e.role === "tool" ? (
              <span className="shrink-0 rounded bg-mast-bg px-1 font-mono text-mast-muted">{tr("工具", "Tool")}</span>
            ) : (
              <span className="shrink-0 rounded bg-mast-accent/10 px-1 text-mast-accent">{tr("发言", "Message")}</span>
            )}
            <span
              className={
                "min-w-0 flex-1 truncate " +
                (e.role === "tool" ? "font-mono text-mast-muted" : "text-mast-text")
              }
              title={e.text}
            >
              {e.text}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────
//  Page wrapper: in-page SubTabs Chat | Vision Buffer.
// ────────────────────────────────────────────────────────────────────────
type ChatSubTab = "chat" | "vision_buffer";

// Remember the last-selected sub-tab across navigation. The route element
// remounts every time the user returns to this page (AppLayout renders it through
// <Outlet/>), so a plain useState would reset the tab each time. The shared
// useStickyTab is used so the stored id is validated on read (ids can go stale
// when sub-pages are renamed or merged; the symptom of a stale id is a blank page).
const CHAT_SUB_TABS: { id: ChatSubTab; label: string }[] = [
  { id: "chat", label: tr("对话", "Chat") },
  { id: "vision_buffer", label: "Vision Buffer" },
];

export default function ChatPage() {
  const [tab, setTab] = useStickyTab<ChatSubTab>(
    "chat", CHAT_SUB_TABS.map((t) => t.id), "chat");
  return (
    <div>
      <SubTabs<ChatSubTab> value={tab} onChange={setTab} tabs={CHAT_SUB_TABS} />
      {/* Keep both mounted — never unmount the chat surface (live SSE stream
          + draggable feedback widget must survive a sub-tab switch). Hide the
          inactive one instead. */}
      <div className={tab === "chat" ? undefined : "hidden"}>
        <ChatSurface onOpenBuffer={() => setTab("vision_buffer")} />
      </div>
      <div className={tab === "vision_buffer" ? undefined : "hidden"}>
        <VisionBufferSurface />
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────
//  Chat: the chat surface.
// ────────────────────────────────────────────────────────────────────────
function ChatSurface({ onOpenBuffer }: { onOpenBuffer: () => void }) {
  const qc = useQueryClient();
  const convos = useConversations();
  const [activeId, setActiveId] = useState<string | null>(null);
  // Native confirm() blocks the main thread and gives no feedback on failure;
  // the shared ConfirmDialog keeps the dialog open and shows the error inline.
  const [confirmDelete, setConfirmDelete] = useState(false);

  // Auto-select the first conversation once the list loads.
  useEffect(() => {
    const list = convos.data?.conversations ?? [];
    if (!activeId && list[0]) setActiveId(list[0].conversation_id);
  }, [convos.data, activeId]);

  // Publish the active conversation to the UI store so the global feedback widget
  // (mounted once in AppLayout → visible on every tab) attaches feedback to it.
  // Clear on unmount so feedback given from other tabs isn't tied to a stale chat.
  const setActiveConversation = useUiStore((s) => s.setActiveConversation);
  useEffect(() => {
    setActiveConversation(activeId);
  }, [activeId, setActiveConversation]);
  useEffect(() => () => setActiveConversation(null), [setActiveConversation]);

  const history = useMessages(activeId);

  // Live message buffer driven by the SSE stream. While streaming this overrides
  // the fetched history; otherwise the fetched history is the source of truth.
  const [streamMsgs, setStreamMsgs] = useState<ChatMsg[] | null>(null);
  // The "otherwise" case: the snapshot is only cleared on a conversation switch,
  // so without this stamp it would stay in front of the re-fetched history. The
  // stamp turns the criterion from "who has a value first" into "who is newer".
  const [streamEndedAt, setStreamEndedAt] = useState(0);
  const [streaming, setStreaming] = useState(false);
  const [streamErr, setStreamErr] = useState<string | null>(null);
  const abortCtrl = useRef<AbortController | null>(null);

  // Which transcript to draw: the criterion lives in lib/transcriptRefresh.ts and
  // is shared with the agent chat. It is defined before `send` on purpose: the
  // optimistic append on send must go after what is ON SCREEN, not after the
  // previous turn's snapshot.
  const shownMessages: ChatMsg[] =
    transcriptSource({
      hasSnapshot: streamMsgs !== null,
      streaming,
      streamEndedAt,
      historyUpdatedAt: history.dataUpdatedAt,
    }) === "history"
      ? (history.data?.messages ?? [])
      : (streamMsgs ?? []);

  // Draft survives tab switches (sessionStorage-backed).
  const [input, setInput] = useDraft("chat-input");
  // "Show narration": running commentary during long tasks ("about to fire a
  // pulse", "scan at 50%"). Stored in sessionStorage (same mechanism as useDraft)
  // rather than as a setting: adding an editable settings_store KNOWN_KEYS entry
  // requires changing two places in lockstep. It can move into settings once the
  // toggle is stable.
  const [showNarration, setShowNarration] = useDraft("chat-narration");
  const narrationOn = showNarration !== "off";
  const [voiceMsg, setVoiceMsg] = useState<string | null>(null);

  // HITL interrupts — poll continuously, badge the toolbar button, render the
  // waiting items INLINE (see the note below).
  const interrupts = useInterrupts(AGENT_ID);
  const hitlRef = useRef<PendingInterruptsHandle | null>(null);
  const interruptCount = interrupts.data?.count ?? 0;
  // An agent asking the operator to DECIDE something is not an approval, so the
  // button label must not always read "Approval": it would promise a yes/no
  // about an action the agent already chose.
  const interruptLabel = (interrupts.data?.interrupts ?? []).some(
    (i) => i.kind === "ask_user",
  )
    ? tr("智能体在问你", "Agent is asking you")
    : tr("人工审批", "Approval needed");

  // Human-in-the-loop interrupts are rendered inline, not as a full-screen modal.
  // A modal (`fixed inset-0 bg-black/60 backdrop-blur-sm`) would hide the bias,
  // current, Z, scan progress and alerts at exactly the moment the operator has to
  // judge whether to approve, and that judgement depends on those readings.
  // The original reason for auto-popping stays valid: the turn that raised the
  // interrupt is blocked, and hiding it behind a badge would mean a silent
  // conversation followed by an unnoticed timeout. So "cannot be missed" is kept
  // and "covers the screen" is dropped: an inline card pushes content aside
  // instead of covering it. A collapsed panel still leaves a one-line
  // "N items pending" summary; see PendingInterrupts.

  // Voice output (TTS) for the latest assistant reply.
  const voiceOut = useVoiceOutput((m) => setVoiceMsg(m));

  // ── conversation mutations ───────────────────────────────────────────
  const createConvo = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/agents/{agent_id}/conversations", {
        params: { path: { agent_id: AGENT_ID } },
        body: { kind: "private", title: null, experiment_id: null },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["chat", "conversations", AGENT_ID] });
      const cid = data?.conversation?.conversation_id;
      if (cid) {
        setActiveId(cid);
        setStreamMsgs(null);
        setStreamEndedAt(0);
      }
    },
  });

  const renameConvo = useMutation({
    mutationFn: async (vars: { conversation_id: string; title: string }) => {
      const { data, error } = await api.PATCH(
        "/api/agents/{agent_id}/conversations/{conversation_id}",
        {
          params: { path: { agent_id: AGENT_ID, conversation_id: vars.conversation_id } },
          body: { title: vars.title },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["chat", "conversations", AGENT_ID] }),
  });

  const deleteConvo = useMutation({
    mutationFn: async (conversation_id: string) => {
      const { data, error } = await api.DELETE(
        "/api/agents/{agent_id}/conversations/{conversation_id}",
        { params: { path: { agent_id: AGENT_ID, conversation_id } } },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: (_data, conversation_id) => {
      qc.invalidateQueries({ queryKey: ["chat", "conversations", AGENT_ID] });
      if (activeId === conversation_id) {
        setActiveId(null);
        setStreamMsgs(null);
        setStreamEndedAt(0);
      }
    },
  });

  // ── send (SSE stream) ────────────────────────────────────────────────
  async function send(textArg?: string) {
    const text = (textArg ?? input).trim();
    if (!text || streaming || !activeId) return;

    setInput("");
    setStreamErr(null);
    setStreaming(true);
    setStreamEndedAt(0);

    // yield-first: show the user's message immediately on top of what is ON
    // SCREEN — which stops being the last snapshot as soon as anyone else has
    // written to this conversation.
    // `t` (message time) must be carried over. `ChatMsg.t` is optional, so
    // dropping it is not a type error; the only symptom would be bubbles that
    // sometimes lack a time.
    const base: ChatMsg[] = shownMessages.map((m) => ({
      role: m.role,
      content: m.content,
      t: m.t,
    }));
    // The optimistic message gets a local timestamp: it does happen now, and the
    // next snapshot replaces it with the server's stamp. Without it the user's own
    // message would have no time until the reply arrives.
    setStreamMsgs([
      ...base,
      { role: "user", content: escapeHtml(text), t: Date.now() / 1000 },
    ]);

    const ctrl = new AbortController();
    abortCtrl.current = ctrl;

    try {
      const resp = await fetch(`/api/agents/${AGENT_ID}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: activeId, user_text: text }),
        signal: ctrl.signal,
      });
      if (!resp.ok || !resp.body) {
        throw new Error(`HTTP ${resp.status}`);
      }

      const end = await readSseStream(resp, {
        controller: ctrl,
        onFrame: (frame: { kind?: string; messages?: ChatMsg[]; message?: string }) => {
          if (frame.kind === "snapshot" && Array.isArray(frame.messages)) {
            // REPLACE the whole list on each snapshot — the backend yields the
            // full rendered history each frame.
            // Carry `t` over too: this is the path the user watches during a long
            // task, and narration is ordered by `t`. Dropping it would put bubbles
            // and narration on different timelines and make the order look off.
            setStreamMsgs(
              frame.messages.map((m) => ({ role: m.role, content: m.content, t: m.t })),
            );
          } else if (frame.kind === "error") {
            setStreamErr(frame.message ?? tr("对话出错", "Chat error"));
          }
        },
      });
      // A body that ends without the server's `done` frame, or 75 s of silence
      // through a 15 s keep-alive, is a cut link, not a finished turn. Treating
      // them alike would make a network change look like the model simply stopped
      // talking. The turn itself keeps running server-side and lands in the
      // checkpointer, which the invalidate below re-reads.
      const cause = sseEndMessage(end);
      if (sseBroke(end) && cause) {
        setStreamErr(tr(`${cause}本轮回答仍在后端继续，已自动重新载入历史；若还没出现，稍后再点一次该会话即可。`, `${cause} The reply is still running on the backend and the history was reloaded automatically; if it has not appeared, click the conversation again shortly.`));
      }
    } catch (err) {
      if ((err as Error)?.name !== "AbortError") {
        setStreamErr(tr(`连接出错：${String((err as Error)?.message ?? err)}`, `Connection error: ${String((err as Error)?.message ?? err)}`));
      }
    } finally {
      setStreaming(false);
      // Hand this turn back to the server history: the re-fetch below is only
      // effective if the drawn transcript switches from the snapshot to history.
      setStreamEndedAt(Date.now());
      abortCtrl.current = null;
      // Re-sync the canonical rendered history from the checkpointer.
      qc.invalidateQueries({ queryKey: ["chat", "messages", AGENT_ID, activeId] });
      qc.invalidateQueries({ queryKey: ["chat", "conversations", AGENT_ID] });
    }
  }

  async function stop() {
    // Signal the backend to abort the active turn, then drop the local reader.
    try {
      await api.POST("/api/agents/{agent_id}/chat/abort", {
        params: { path: { agent_id: AGENT_ID } },
      });
    } catch {
      /* best-effort */
    }
    abortCtrl.current?.abort();
    setStreaming(false);
  }

  const conversations: Conversation[] = (convos.data?.conversations ?? []) as Conversation[];

  // The latest assistant reply (plain-ish text) for the read-aloud (TTS) action.
  const latestAssistant = [...shownMessages].reverse().find((m) => m.role === "assistant");
  function speakLatest() {
    if (!latestAssistant) return;
    // strip HTML tags from the rendered content before sending to TTS.
    const txt = latestAssistant.content.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
    voiceOut.speak(txt);
  }

  const activeConvo = conversations.find((c) => c.conversation_id === activeId) ?? null;
  const [renameDraft, setRenameDraft] = useState("");

  // Same as the agent chat: message history is otherwise invalidated only once,
  // after this browser finishes sending. It is easy to miss here because the
  // sender is usually the one watching, so it looks live until the page is opened
  // on another machine or a background turn finishes the reply.
  useTranscriptRefresh({
    conversationId: activeId,
    updatedAt: activeConvo?.updated_at ?? null,
    streaming,
    onRefresh: () => {
      void qc.invalidateQueries({
        queryKey: ["chat", "messages", AGENT_ID, activeId],
      });
    },
  });

  return (
    <Section
      title={tr("对话", "Chat")}
      actions={
        interruptCount > 0 ? (
          <button
            // Scrolls to the inline card and expands it. With a long transcript this
            // badge is the only anchor; it takes you there rather than covering
            // what you are looking at.
            onClick={() => hitlRef.current?.reveal()}
            className="rounded border border-mast-warn-border bg-mast-warn-bg px-3 py-1 text-sm text-mast-warn"
          >
            {interruptLabel}
            <span className="ml-1.5 rounded bg-mast-warn-border px-1.5 text-xs">{interruptCount}</span>
          </button>
        ) : undefined
      }
    >
      {/* ── Lab console strips above the transcript (data strip, then vision pulse) ── */}
      <div className="mb-3 flex flex-col gap-2">
        <DataStrip />
        <VisionRibbon onOpenBuffer={onOpenBuffer} />
        {/* IC's live activity in a running group chat (multi-agent run). This
            private chat and the group run are separate streams, so without this
            the operator could not see what IC is doing in the team run. Real-time
            via the run-task store; hidden entirely when there is no group activity. */}
        <IcGroupActivityStrip />
      </div>

      {/* Items waiting for a human, placed directly under the data strip: judging
          whether to approve depends on the readings above (bias / current / Z /
          scan state), which a full-screen modal would cover. Not rendered when
          nothing is pending. */}
      <div className="mb-3">
        <PendingInterrupts ref={hitlRef} data={interrupts.data} agentId={AGENT_ID} />
      </div>

      {/* Scope badge: whether this conversation belongs to the experiment level or
          the sample level. A conversation opened with no sample selected is
          experiment-level (planning discussion); one opened after selecting a
          sample is sample-level. The backend derives this (sample_id NULL means
          experiment-level); the badge only shows where the discussion will appear
          in the experiment folder. */}
      <ConversationScopeBadge
        convo={activeConvo as ConvScope | null}
      />

      {/* ── conversation picker bar: dropdown + new + rename + delete, a FLAT row, never nested tabs ── */}
      <div className="mb-3 flex items-center gap-2">
        <select
          value={activeId ?? ""}
          onChange={(e) => {
            setActiveId(e.target.value || null);
            setStreamMsgs(null);
            setStreamEndedAt(0);
            setStreamErr(null);
          }}
          className="min-w-0 flex-1 rounded border border-mast-border bg-mast-bg px-2 py-1.5 text-sm text-mast-text outline-none focus:border-mast-accent"
        >
          {conversations.length === 0 && <option value="">{tr("新对话", "New chat")}</option>}
          {conversations.map((c) => (
            <option key={c.conversation_id} value={c.conversation_id}>
              {c.title || tr("新对话", "New chat")}
            </option>
          ))}
        </select>
        <button
          onClick={() => createConvo.mutate()}
          disabled={createConvo.isPending}
          className="shrink-0 rounded border border-mast-border bg-mast-panel px-3 py-1.5 text-sm hover:border-mast-accent disabled:opacity-50"
        >
          {tr("➕ 新对话", "➕ New chat")}
        </button>
        <input
          value={renameDraft}
          onChange={(e) => setRenameDraft(e.target.value)}
          placeholder={tr("重命名…", "Rename…")}
          className="w-40 shrink-0 rounded border border-mast-border bg-mast-bg px-2 py-1.5 text-sm outline-none focus:border-mast-accent"
        />
        <button
          onClick={() => {
            if (activeId && renameDraft.trim()) {
              renameConvo.mutate({ conversation_id: activeId, title: renameDraft.trim() });
              setRenameDraft("");
            }
          }}
          disabled={!activeId || !renameDraft.trim()}
          title={tr("重命名当前对话", "Rename current chat")}
          className="shrink-0 rounded border border-mast-border bg-mast-panel px-3 py-1.5 text-sm hover:border-mast-accent disabled:opacity-40"
        >
          ✎
        </button>
        <button
          onClick={() => setConfirmDelete(true)}
          disabled={!activeId}
          title={tr("删除当前对话", "Delete current chat")}
          className="shrink-0 rounded border border-mast-danger-border bg-mast-danger-bg px-3 py-1.5 text-sm text-mast-danger hover:bg-mast-danger-border disabled:opacity-40"
        >
          🗑
        </button>
        <ConfirmDialog
          open={confirmDelete}
          onClose={() => setConfirmDelete(false)}
          title={tr("删除对话", "Delete chat")}
          tone="danger"
          confirmLabel={tr("删除", "Delete")}
          busy={deleteConvo.isPending}
          busyLabel={tr("删除中…", "Deleting…")}
          onConfirm={() => {
            if (activeId) deleteConvo.mutate(activeId);
            setConfirmDelete(false);
          }}
        >
          <p className="text-sm text-mast-text">
            {tr(
              `删除会话「${activeConvo?.title || "新对话"}」？`,
              `Delete chat "${activeConvo?.title || "New chat"}"?`,
            )}
          </p>
          <p className="text-xs text-mast-muted">
            {tr(
              "这条对话的转录会一并删除。若它已被导出到实验文件夹，文件夹里的那份不受影响。",
              "The transcript of this chat is deleted too. If it was exported to the experiment folder, that copy is not affected.",
            )}
          </p>
        </ConfirmDialog>
      </div>

      {/* ── chatbot transcript ──────────────────────────────────────── */}
      <div
        className="flex flex-col overflow-hidden rounded-lg border border-mast-border bg-mast-panel"
        style={{ height: "calc(100vh - 420px)" }}
      >
        <div className="flex items-center justify-between border-b border-mast-border px-3 py-1.5 text-xs text-mast-muted">
          <span>MAST Assistant</span>
          <label className="flex cursor-pointer select-none items-center gap-1.5">
            <input
              type="checkbox"
              checked={narrationOn}
              onChange={(e) => setShowNarration(e.target.checked ? "" : "off")}
              className="h-3 w-3 accent-[var(--mast-accent)]"
            />
            <span title={tr("长任务跑的时候，持续解说正在做什么（这些话智能体看不到）", "During long tasks, keep narrating what is being done (the agent does not see this)")}>
              {tr("显示旁白", "Show narration")}
            </span>
          </label>
        </div>
        <div className="flex-1 overflow-y-auto p-4">
          {convos.data?.degraded && <DegradedNote what={tr("对话列表", "chat list")} />}
          {!activeId && conversations.length === 0 && (
            <EmptyNote label={tr("点击「➕ 新对话」开始与仪器控制智能体私聊", "Click \"➕ New chat\" to start a private chat with the instrument-control agent")} />
          )}
          {activeId && history.isPending && !streamMsgs && <Spinner />}
          {activeId && history.isError && !streamMsgs && <ErrorNote error={history.error} />}
          {activeId && history.data?.degraded && !streamMsgs && <DegradedNote what={tr("对话引擎", "chat engine")} />}
          {activeId && (shownMessages.length > 0 || streaming) && (
            <NarrationLane
              messages={shownMessages}
              conversationId={activeId}
              pending={streaming}
              show={narrationOn}
            />
          )}
          {activeId &&
            !streaming &&
            !history.isPending &&
            !history.data?.degraded &&
            shownMessages.length === 0 && (
              <EmptyNote label={tr("还没有消息，在下方输入发送第一条", "No messages yet. Type below to send the first one")} />
            )}
          {streamErr && <p className="mt-3 text-sm text-mast-danger">{tr("流式出错：", "Stream error: ")}{streamErr}</p>}
        </div>
      </div>

      {/* Confirm the agent's subscription recommendations in place, without leaving
          the chat (that step is the one most likely to be dropped). Placed above
          the input because it responds to the turn just finished, not a footer
          notice. */}
      <div className="mt-3">
        <PendingRecommendations compact />
      </div>

      {/* ── input bar: textbox + Send + Stop + read-aloud ── */}
      <div className="mt-3 flex items-center gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder={tr("向 MAST 提问…", "Ask MAST anything...")}
          disabled={!activeId || streaming}
          className="min-w-0 flex-1 rounded border border-mast-border bg-mast-bg px-3 py-2 text-sm text-mast-text outline-none focus:border-mast-accent disabled:opacity-50"
        />
        {streaming ? (
          <button
            onClick={() => void stop()}
            className="shrink-0 rounded border border-mast-danger-border bg-mast-danger-bg px-5 py-2 text-sm text-mast-danger hover:bg-mast-danger-border"
          >
            {tr("停止", "Stop")}
          </button>
        ) : (
          <button
            onClick={() => void send()}
            disabled={!activeId || !input.trim()}
            className="shrink-0 rounded border border-mast-accent bg-mast-accent px-5 py-2 text-sm font-medium text-mast-accent-ink hover:opacity-90 disabled:opacity-40"
          >
            {tr("发送", "Send")}
          </button>
        )}
        <button
          onClick={speakLatest}
          disabled={!latestAssistant || voiceOut.speaking}
          title={tr("朗读最新回复", "Read latest reply aloud")}
          className="shrink-0 rounded border border-mast-border px-3 py-2 text-sm text-mast-muted hover:border-mast-accent hover:text-mast-accent disabled:opacity-40"
        >
          {voiceOut.speaking ? "…" : "⟳"}
        </button>
      </div>

      {/* ── voice dock (full-duplex voice: continuous listen/speak, streaming, barge-in) ── */}
      <div className="mt-2">
        <VoiceDock conversationId={activeId} />
        {voiceMsg && <p className="mt-1 text-xs italic text-mast-muted">{voiceMsg}</p>}
      </div>

      {/* ── quick-prompt row (single flex-wrap line, below input) ── */}
      <div className="mt-3">
        <QuickPrompts onPick={(p) => setInput(input ? input + " " + p : p)} />
      </div>

      {/* Human-in-the-loop is inline under the data strip, not a footer modal. */}
      {/* The conversation feedback widget lives in AppLayout (visible on every tab). */}
    </Section>
  );
}

// ────────────────────────────────────────────────────────────────────────
//  Vision Buffer: scan map + vision pulse + recent frames + buffer events.
//  Reuses the same components + endpoints as the standalone VisionPage so the
//  operator can watch vision without leaving Chat.
// ────────────────────────────────────────────────────────────────────────
function fmtTime(epochS?: number | null): string {
  if (!epochS) return "—";
  try {
    return new Date(epochS * 1000).toLocaleTimeString();
  } catch {
    return "—";
  }
}

function usePulse() {
  return useQuery({
    queryKey: ["vision", "pulse"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/vision/pulse");
      if (error) throw error;
      return data;
    },
    refetchInterval: 2000,
  });
}
function useRecent() {
  return useQuery({
    queryKey: ["vision", "recent"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/vision/recent");
      if (error) throw error;
      return data;
    },
    refetchInterval: 5000,
  });
}

function VisionBufferSurface() {
  return (
    <div className="space-y-4">
      {/* `/vision` has no top-bar entry (`nav.ts` INTENTIONALLY_UNLISTED), and this
          surface embeds only four of its seven sub-pages, so signal capture + FFT,
          long-term monitoring and mosaic would have no entry at all. This link is
          the rendered entry point to the full vision page; test/scanMapPairing.test.ts
          pins that some rendered link points to `/vision`. */}
      <div className="text-right text-xs">
        <Link to="/vision" className="text-mast-accent hover:underline">
          {tr("完整视觉页（信号捕获+FFT · 长期监控 · 拼图）→", "Full vision page (signal capture + FFT · long-term monitoring · mosaic) →")}
        </Link>
      </div>
      <BufferScanMapSection />
      <BufferPulseSection />
      <BufferRecentFramesSection />
      <Section title={tr("视觉缓冲", "Vision buffer")}>
        <VisionBufferPanel />
      </Section>
    </div>
  );
}

function BufferPulseSection() {
  const q = usePulse();
  return (
    <Section title={tr("视觉脉冲", "Vision pulse")}>
      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} />}
      {q.data &&
        (q.data.degraded ? <DegradedNote what={tr("视觉脉冲", "vision pulse")} /> : <PulseRibbon pulse={q.data} />)}
    </Section>
  );
}

function BufferScanMapSection() {
  const coarseQ = useCoarseMap();
  return (
    <Section title={tr("扫描地图", "Scan map")}>
      <p className="mb-3 text-xs text-mast-muted">
        {tr(
          "实验助手的可视化核心：实时扫描框 + 针尖位置 + 所有带位置的历史操作（扫图 / STS / 电脉冲 / 修针尖 / 移动 / 手动），以及计划路线与候选序列。每 3 秒刷新；图层与显示范围与视觉页共用一套设置。",
          "Core visualisation of the experiment assistant: live scan frame + tip position + all past position-tagged operations (scans / STS / electrical pulses / tip conditioning / moves / manual), plus the planned route and candidate sequence. Refreshes every 3 s; layers and display range share settings with the vision page.",
        )}
      </p>
      <ScanMapPanel compact />

      {/* Second map at sample-stage scale. It has always existed but was only drawn
          on /vision, which has no top-bar entry. The map above is the +/-1.5 um
          piezo range (metres, current generation only); this one covers the whole
          sample (coarse-motor steps, all generations). They are drawn separately
          but must be drawn TOGETHER: the difference in scale is itself the evidence
          that changing region cannot be done with the piezo, and with only one map
          that evidence disappears. */}
      <div className="mt-4">
        {/* Compact by default on the chat page, where it competes with the
            transcript for space; on `/vision` it is the main element. The
            difference is made at the call site, not in the component. With zoom
            available a small size is still legible, and the panel has an
            "expand" control to return to the large view. */}
        <CoarseMapPanel
          data={coarseQ.data}
          isPending={coarseQ.isPending}
          isError={coarseQ.isError}
          error={coarseQ.error}
          isFetching={coarseQ.isFetching}
          onRefresh={() => void coarseQ.refetch()}
          defaultCompact
        />
      </div>
    </Section>
  );
}

function BufferRecentFramesSection() {
  const q = useRecent();
  const data = q.data;
  const frames = data?.frames ?? [];
  return (
    <Section title={tr("近期标注帧", "Recent annotated frames")}>
      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} />}
      {data && data.degraded && <DegradedNote what={tr("近期标注帧", "recent annotated frames")} />}
      {data && !data.degraded && frames.length === 0 && <EmptyNote label={tr("暂无近期视觉事件。", "No recent vision events.")} />}
      {data && !data.degraded && frames.length > 0 && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          {frames.map((f) => (
            <Card key={f.seqno} className="space-y-2 p-3">
              <RecentFrameThumb frame={f} alt={f.summary || f.kind} />
              <div className="space-y-1 text-xs">
                <div className="flex items-center justify-between">
                  <span className="font-medium">{f.kind || tr("事件", "Event")}</span>
                  <span
                    className={
                      f.severity?.toLowerCase() === "critical"
                        ? "text-mast-danger"
                        : f.severity?.toLowerCase() === "warn" ||
                            f.severity?.toLowerCase() === "warning"
                          ? "text-mast-warn"
                          : "text-mast-muted"
                    }
                  >
                    {f.severity || "info"}
                  </span>
                </div>
                <div className="text-mast-muted">
                  {fmtTime(f.t_wall)} · #{f.seqno}
                </div>
                {f.summary && <div className="line-clamp-2 text-mast-text">{f.summary}</div>}
                {f.file_path && (
                  <div className="truncate text-mast-muted" title={f.file_path}>
                    {f.file_path}
                  </div>
                )}
              </div>
            </Card>
          ))}
        </div>
      )}
    </Section>
  );
}

/** The scope fields may not yet appear in schema.d.ts (same approach as
 *  GroupChatHistoryPane), so they are declared locally as optional. */
type ConvScope = { experiment_id?: string | null; sample_id?: string | null };

/** Scope badge of a conversation: experiment-level / sample-level / unassigned. */
function ConversationScopeBadge({ convo }: { convo: ConvScope | null }) {
  const scope = useCurrentScope();
  if (!convo) return null;
  const expName =
    convo.experiment_id && convo.experiment_id === scope.data?.experiment?.id
      ? scope.data?.experiment?.name
      : null;
  const smpName =
    convo.sample_id && convo.sample_id === scope.data?.sample?.id
      ? scope.data?.sample?.name
      : null;

  if (!convo.experiment_id) {
    return (
      <div className="mb-2 text-[11px] text-mast-faint">
        {tr("未归属实验 —— 这段对话不会被导出到任何实验文件夹。", "Not assigned to an experiment: this chat will not be exported to any experiment folder.")}
      </div>
    );
  }
  return (
    <div className="mb-2 flex items-center gap-1.5 text-[11px]">
      <span className="rounded-mast-badge border border-mast-border px-1.5 py-px text-mast-muted">
        {convo.sample_id ? tr("样品级", "Sample level") : tr("实验级", "Experiment level")}
      </span>
      <span className="truncate text-mast-faint">
        {smpName ?? expName ?? tr("已归属", "Assigned")}
      </span>
    </div>
  );
}


// ── helpers ───────────────────────────────────────────────────────────

/** Minimal HTML escape for the optimistic user bubble only (the backend will
 *  re-render the canonical version on the next snapshot). */
function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\n/g, "<br/>");
}
