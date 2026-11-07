import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useStickyTab } from "@/hooks/useStickyTab";
import type { ColumnDef } from "@tanstack/react-table";
import { api } from "@/api/client";
import {
  Badge,
  Card,
  DegradedNote,
  EmptyNote,
  ErrorNote,
  Section,
  Spinner,
} from "@/components/ui";
import {
  Button,
  Field,
  SelectField,
  SubTabs,
  useToast,
} from "@/components/controls";
import { DataTable } from "@/components/DataTable";
import {
  AGENTS,
  Avatar,
  PIPELINE,
  SUP_ID,
  SUPERVISOR,
  THINKING_LEVELS,
  agentDef,
  agentLabel,
  safetyTone,
} from "@/components/agents/registry";
import {
  ALL_VIEW as CONTEXT_ALL,
  ContextInjectionView,
} from "@/components/agents/ContextInjectionView";
import { TopologyGraph, type AgentLive } from "@/components/agents/TopologyGraph";
import { AgentChatPanel } from "@/components/agents/AgentChatPanel";
import { GroupActivityFeed } from "@/components/agents/GroupActivityFeed";
import { InterruptsModal } from "@/components/agents/InterruptsModal";
import { RunTaskPanel } from "@/components/agents/RunTaskPanel";
import { BackgroundRunsPanel } from "@/components/agents/BackgroundRunsPanel";
import { PendingActivationsPanel } from "@/components/agents/PendingActivationsPanel";
import { ArtifactEditor } from "@/components/agents/ArtifactEditor";
// Replaces ArtifactConnectionGraph, which drew a single-writer bipartite graph that did
// not match the real pipeline.
import { ArtifactFlowGraph } from "@/components/agents/ArtifactFlowGraph";
import { langTag, tr } from "@/lib/i18n";

// ════════════════════════════════════════════════════════════════════════════
// Agents inspector — full parity rebuild of the old Gradio agents surface.
//
// Sub-tabs (flat React state via <SubTabs>, never nested gr.Tabs, so nothing freezes):
//   Chat      — default/primary. Interactive multi-agent conversation (POST
//               /api/agents/run-task SSE + live POST /api/agents/{target}/interject):
//               one scrolling thread with a persistent bottom input; the first message
//               starts the task, later messages steer the run live; inline handoffs,
//               HITL approve/reject/edit and abort; the legacy per-agent supervisor
//               private chat is kept as a secondary mode
//   Topology  — supervisor + 7-agent topology graph (live model/active/threads)
//   Agent chat — per-agent private chat threads
//   Inspector — per-agent model+thinking picker + tool catalog DataTable (IC ~224)
//   Objects   — artifact list + R/W permission matrix + per-artifact operator editor
//               (save/revert/history/diff/export via /api/artifacts/{id}/edit…)
//
// Plus a global HITL interrupts modal (badge-gated) reachable from the header.
// All reads render loading / error / degraded / empty. All writes toast.
// ════════════════════════════════════════════════════════════════════════════

const ROSTER = [SUP_ID, ...AGENTS.map((a) => a.id)];

// ── shared read hooks ───────────────────────────────────────────────────────

function useSnapshot() {
  return useQuery({
    queryKey: ["agents", "snapshot"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/agents/snapshot");
      if (error) throw error;
      return data;
    },
    refetchInterval: 4000,
  });
}

function useAgentModels() {
  return useQuery({
    queryKey: ["agents", "models"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/agents/models");
      if (error) throw error;
      return data;
    },
  });
}

function useAgentTools() {
  return useQuery({
    queryKey: ["agents", "tools"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/agents/tools");
      if (error) throw error;
      return data;
    },
    refetchInterval: (q) => (q.state.data && !q.state.data.degraded ? false : 4000),
  });
}

// ── live overlay (model/thinking/active/held/threads) keyed by agent id ──────

function useLiveByAgent(): {
  live: Record<string, AgentLive>;
  snapshot: ReturnType<typeof useSnapshot>;
} {
  const snapshot = useSnapshot();
  const models = useAgentModels();
  const live = useMemo(() => {
    const m: Record<string, AgentLive> = {};
    // model seam (near-static defaults)
    for (const a of models.data?.agents ?? []) {
      m[a.agent_id] = { model: a.model, thinking: a.thinking };
    }
    // snapshot maps override with live values
    const snap = snapshot.data;
    if (snap) {
      for (const [id, model] of Object.entries(snap.models ?? {})) {
        m[id] = { ...(m[id] ?? {}), model };
      }
      for (const [id, th] of Object.entries(snap.thinking ?? {})) {
        m[id] = { ...(m[id] ?? {}), thinking: th };
      }
      for (const [id, held] of Object.entries(snap.holds ?? {})) {
        m[id] = { ...(m[id] ?? {}), held };
      }
      for (const [id, n] of Object.entries(snap.threads_index ?? {})) {
        m[id] = { ...(m[id] ?? {}), threads: n };
      }
      for (const node of snap.agents ?? []) {
        m[node.id] = {
          ...(m[node.id] ?? {}),
          model: node.model ?? m[node.id]?.model,
          thinking: node.thinking ?? m[node.id]?.thinking,
          active: node.active,
          held: node.held,
          threads: node.thread_count,
        };
      }
      if (snap.active_agent_id) {
        m[snap.active_agent_id] = { ...(m[snap.active_agent_id] ?? {}), active: true };
      }
    }
    return m;
  }, [models.data, snapshot.data]);
  return { live, snapshot };
}

type SubTab =
  | "topology" | "chat" | "background" | "waking"
  | "agentchat" | "inspector" | "context" | "workspace" | "editor";

// The sub-page table is a single piece of data because it serves two purposes: the
// {id,label} list that renders the tab bar, and the list of valid ids for useStickyTab.
// A second hand-copied list would drift from the tab bar, and the symptom (one sub-page
// not being remembered) is easy to miss.
const SUB_TABS: { id: SubTab; label: string }[] = [
  { id: "chat", label: tr("对话", "Chat") },
  { id: "background", label: tr("后台", "Background") },
  // A parked agent that nobody can see is indistinguishable from a hung system, so the
  // scheduler's parking mechanism is not usable without this tab.
  { id: "waking", label: tr("待唤醒", "Parked") },
  { id: "topology", label: tr("拓扑", "Topology") },
  { id: "agentchat", label: tr("代理对话", "Agent chat") },
  { id: "inspector", label: "Inspector" },
  // Context injection: read-only. Answers "is injection role-specific?" and "what makes up
  // this call?". Editing stays under Advanced → Context injection (behind the PIN gate).
  { id: "context", label: tr("上下文注入", "Context injection") },
  { id: "workspace", label: tr("对象总览", "Workspace") },
  { id: "editor", label: tr("对象编辑", "Object editor") },
];

export default function AgentsPage() {
  // Remember the sub-tab across navigation. The route element is rendered by AppLayout's
  // <Outlet/> and is freshly mounted on every return, so plain useState("chat") would reset
  // the user to the chat tab each time.
  const [tab, setTab] = useStickyTab<SubTab>(
    "agents", SUB_TABS.map((t) => t.id), "chat");
  const [selected, setSelected] = useState<string>("instrument_control");
  // Which role the context-injection sub-page shows. The list is derived from ROSTER; a
  // hand-copied list elsewhere (AdminPage) once missed research_director.
  const CONTEXT_PICKS = useMemo(
    () => [{ id: CONTEXT_ALL, label: tr("全部", "All") },
           ...ROSTER.map((id) => ({ id, label: agentDef(id).cn }))],
    []);
  const [ctxPick, setCtxPick] = useStickyTab<string>(
    "agents.context.pick", CONTEXT_PICKS.map((p) => p.id), CONTEXT_ALL);
  const [interruptAgent, setInterruptAgent] = useState<string | null>(null);
  // Shared between the workspace and editor sub-tabs (the workspace card's "Open editor"
  // button jumps to the editor sub-tab focused on the selected artifact).
  const [editArtifact, setEditArtifact] = useState<string | null>(null);

  const { live, snapshot } = useLiveByAgent();
  const pending = snapshot.data?.pending_interrupt_count ?? 0;

  return (
    <div className="space-y-6">
      <Section
        title={tr("智能体", "Agents")}
        actions={
          <div className="flex items-center gap-2">
            {snapshot.data && (
              <Badge tone={snapshot.data.session_active ? "AUTO" : "INFO"}>
                {snapshot.data.session_active ? tr("会话进行中", "Session active") : tr("空闲", "Idle")}
              </Badge>
            )}
            <Button
              variant={pending > 0 ? "danger" : "default"}
              onClick={() => setInterruptAgent(selected || SUP_ID)}
            >
              {tr("人机交互", "Human-in-the-loop")}{pending > 0 ? ` (${pending})` : ""}
            </Button>
          </div>
        }
      >
        <SubTabs<SubTab> tabs={SUB_TABS} value={tab} onChange={setTab} />

        {tab === "chat" && <SupervisorChatView snapshot={snapshot} onOpenInterrupts={() => setInterruptAgent(SUP_ID)} />}
        {tab === "background" && <BackgroundRunsPanel />}
        {tab === "context" && (
          <ContextInjectionView picks={CONTEXT_PICKS} pick={ctxPick} onPick={setCtxPick} />
        )}
        {tab === "waking" && <PendingActivationsPanel />}
        {tab === "topology" && (
          <TopologyView live={live} snapshot={snapshot} selected={selected} onSelect={setSelected} />
        )}
        {tab === "agentchat" && (
          <AgentChatView selected={selected} onSelect={setSelected} />
        )}
        {tab === "inspector" && (
          <InspectorView live={live} selected={selected} onSelect={setSelected} />
        )}
        {tab === "workspace" && (
          <WorkspaceView
            onEdit={(id) => {
              setEditArtifact(id);
              setTab("editor");
            }}
          />
        )}
        {tab === "editor" && (
          <EditorView selected={editArtifact} onSelect={setEditArtifact} />
        )}
      </Section>

      <InterruptsModal
        open={interruptAgent != null}
        agentId={interruptAgent}
        onClose={() => setInterruptAgent(null)}
      />
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════════
// Topology graph + per-agent live status strip
// ════════════════════════════════════════════════════════════════════════════

function TopologyView({
  live,
  snapshot,
  selected,
  onSelect,
}: {
  live: Record<string, AgentLive>;
  snapshot: ReturnType<typeof useSnapshot>;
  selected: string;
  onSelect: (id: string) => void;
}) {
  const qc = useQueryClient();
  const { toast, node } = useToast();
  const snap = snapshot.data;

  // hold/release per agent — LIVE-only orchestrator control, degrade-safe.
  const hold = useMutation({
    mutationFn: async (vars: { agent_id: string; held: boolean }) => {
      const opts = { params: { path: { agent_id: vars.agent_id } } };
      const { data, error } = vars.held
        ? await api.POST("/api/agents/{agent_id}/hold", opts)
        : await api.POST("/api/agents/{agent_id}/release", opts);
      if (error) throw error;
      return data;
    },
    onSuccess: (data, vars) => {
      const who = agentLabel(vars.agent_id);
      const verb = vars.held ? tr("暂停", "Pause") : tr("恢复", "Resume");
      if (data?.ok && !data.degraded) {
        toast(vars.held ? tr(`已暂停 ${who}`, `Paused ${who}`) : tr(`已恢复 ${who}`, `Resumed ${who}`), "ok");
      } else {
        const why = data?.detail ?? tr("后端降级", "backend degraded");
        toast(tr(`${verb}未生效（${why}）`, `${verb} had no effect (${why})`), "err");
      }
      qc.invalidateQueries({ queryKey: ["agents", "snapshot"] });
    },
    onError: (e) => toast(String((e as Error)?.message ?? e), "err"),
  });

  return (
    <div className="space-y-5">
      {node}
      {/* header — multi-agent topology (mirrors the old AGTopologyView title row) */}
      <div className="flex flex-wrap items-baseline gap-3">
        <h2 className="text-lg font-semibold tracking-tight">{tr("多智能体拓扑", "Multi-agent topology")}</h2>
        <span className="text-xs text-mast-muted">
          {tr("用户 → 编排协调 → 六 agent 流水线 · BUF 侧通道", "User → supervisor → six-agent pipeline · BUF side channel")}
        </span>
      </div>
      {snapshot.isPending && <Spinner />}
      {snapshot.isError && <ErrorNote error={snapshot.error} />}
      {snap && snap.degraded && <DegradedNote what={tr("智能体拓扑快照", "Agent topology snapshot")} />}

      <TopologyGraph
        selected={selected}
        live={live}
        onPick={onSelect}
        onToggleHold={(id, held) => hold.mutate({ agent_id: id, held })}
      />

      {snap && (
        <div className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-3 lg:grid-cols-6">
          <StatCell label={tr("后端", "Backend")} value={snap.backend || "—"} />
          <StatCell label={tr("会话", "Session")} value={snap.session_active ? tr("活跃", "Active") : tr("空闲", "Idle")} />
          <StatCell label={tr("活动智能体", "Active agent")} value={snap.active_agent_id ? agentLabel(snap.active_agent_id) : "—"} />
          <StatCell label={tr("制品数", "Artifacts")} value={String(snap.artifacts_count ?? 0)} />
          <StatCell label={tr("待中断", "Pending interrupts")} value={String(snap.pending_interrupt_count ?? 0)} />
          <StatCell label={tr("插话次数", "Interjections")} value={String(snap.interject_count ?? 0)} />
        </div>
      )}

      {/* per-agent live status strip */}
      <div>
        <h4 className="mb-2 text-sm font-medium text-mast-muted">{tr("逐智能体实时状态", "Live status per agent")}</h4>
        <div className="overflow-x-auto rounded-lg border border-mast-border">
          <table className="w-full text-sm">
            <thead className="border-b border-mast-border text-left text-xs text-mast-muted">
              <tr>
                <th className="px-3 py-2">{tr("智能体", "Agent")}</th>
                <th className="px-3 py-2">{tr("模型", "Model")}</th>
                <th className="px-3 py-2">{tr("思考", "Thinking")}</th>
                <th className="px-3 py-2">{tr("状态", "Status")}</th>
                <th className="px-3 py-2 text-right tabular-nums">{tr("线程深度", "Thread depth")}</th>
                <th className="px-3 py-2 text-right">{tr("操作", "Actions")}</th>
              </tr>
            </thead>
            <tbody>
              {ROSTER.map((id) => {
                const lv = live[id] ?? {};
                return (
                  <tr
                    key={id}
                    className={
                      "cursor-pointer border-b border-mast-border/50 hover:bg-mast-bg/60 " +
                      (selected === id ? "bg-mast-accent/5" : "")
                    }
                    onClick={() => onSelect(id)}
                  >
                    <td className="px-3 py-2">
                      <span className="flex items-center gap-2">
                        <Avatar id={id} size={18} active={lv.active} />
                        {agentLabel(id)}
                      </span>
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">{lv.model ?? "—"}</td>
                    <td className="px-3 py-2">
                      <Badge tone={lv.thinking ? "AUTO" : "default"}>{lv.thinking ?? "—"}</Badge>
                    </td>
                    <td className="px-3 py-2">
                      {lv.active ? (
                        <Badge tone="AUTO">{tr("运行中", "Running")}</Badge>
                      ) : lv.held ? (
                        <Badge tone="WARN">{tr("已暂留", "Held")}</Badge>
                      ) : (
                        <span className="text-xs text-mast-muted">{tr("空闲", "Idle")}</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-right font-mono tabular-nums">{lv.threads ?? 0}</td>
                    <td className="px-3 py-2 text-right" onClick={(e) => e.stopPropagation()}>
                      <button
                        disabled={hold.isPending && hold.variables?.agent_id === id}
                        onClick={() => hold.mutate({ agent_id: id, held: !lv.held })}
                        className={
                          "rounded-md border px-2.5 py-1 text-xs font-medium disabled:opacity-50 " +
                          (lv.held
                            ? "border-mast-auto-border text-mast-auto hover:bg-mast-auto-bg"
                            : "border-mast-warn-border text-mast-warn hover:bg-mast-warn-bg")
                        }
                      >
                        {hold.isPending && hold.variables?.agent_id === id
                          ? "…"
                          : lv.held
                            ? tr("恢复", "Resume")
                            : tr("暂停", "Pause")}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function StatCell({ label, value }: { label: string; value: string }) {
  return (
    <Card className="px-3 py-2">
      <div className="text-xs text-mast-muted">{label}</div>
      <div className="mt-0.5 truncate font-mono text-sm tabular-nums">{value}</div>
    </Card>
  );
}

// ════════════════════════════════════════════════════════════════════════════
// Chat — interactive multi-agent conversation (run-task SSE + live interject) with a
//   persistent bottom input + handoff timeline; legacy supervisor private chat as a
//   secondary mode.
// ════════════════════════════════════════════════════════════════════════════

type ChatMode = "runtask" | "supchat";

const CHAT_MODES: { id: ChatMode; label: string }[] = [
  { id: "runtask", label: tr("多智能体对话", "Multi-agent chat") },
  { id: "supchat", label: tr("编排器私聊", "Supervisor chat") },
];

// Per-event-kind icon + dot colour + badge tone for the handoff event timeline (a wall of
// identical dots was unscannable).
const TIMELINE_STYLE: Record<
  string,
  { icon: string; label?: string; dot: string; tone: React.ComponentProps<typeof Badge>["tone"] }
> = {
  start: { icon: "🚀", label: tr("开始", "Start"), dot: "bg-sky-400", tone: "INFO" },
  handoff: { icon: "🔀", label: tr("交接", "Handoff"), dot: "bg-violet-400", tone: "AUTO" },
  // A parallel fan-out is the one event the operator most needs to spot; without its own
  // style it would land on the anonymous fallback bullet.
  dispatch: { icon: "‖", label: tr("并行下发", "Parallel dispatch"), dot: "bg-cyan-400", tone: "INFO" },
  held: { icon: "⏸️", label: tr("暂停", "Paused"), dot: "bg-amber-400", tone: "WARN" },
  resumed: { icon: "▶️", label: tr("继续", "Resumed"), dot: "bg-emerald-400", tone: "AUTO" },
  interrupt: { icon: "🛡️", label: tr("待批准", "Awaiting approval"), dot: "bg-rose-400", tone: "DANGEROUS" },
  dangerous: { icon: "🛡️", label: tr("待批准", "Awaiting approval"), dot: "bg-rose-400", tone: "DANGEROUS" },
  buffer_hitl: { icon: "🛡️", label: tr("待批准", "Awaiting approval"), dot: "bg-rose-400", tone: "DANGEROUS" },
  workflow_human: { icon: "🙋", label: tr("待人工", "Awaiting operator"), dot: "bg-rose-300", tone: "WARN" },
  done: { icon: "✅", label: tr("完成", "Done"), dot: "bg-emerald-500", tone: "AUTO" },
  aborted: { icon: "🛑", label: tr("已中止", "Aborted"), dot: "bg-rose-500", tone: "DANGEROUS" },
  error: { icon: "⚠️", label: tr("出错", "Error"), dot: "bg-rose-500", tone: "DANGEROUS" },
};
const TIMELINE_FALLBACK: {
  icon: string;
  label?: string;
  dot: string;
  tone: React.ComponentProps<typeof Badge>["tone"];
} = {
  icon: "•",
  dot: "bg-mast-accent",
  tone: "INFO",
};

function SupervisorChatView({
  snapshot,
  onOpenInterrupts,
}: {
  snapshot: ReturnType<typeof useSnapshot>;
  onOpenInterrupts: () => void;
}) {
  const { node } = useToast();
  const snap = snapshot.data;
  const task = snap?.active_task ?? null;
  // Primary action = the REAL multi-agent orchestrator runner (run-task SSE).
  // The legacy per-agent SUP private chat is kept reachable as a secondary mode.
  // Remembered one level deeper too: someone parked on "Supervisor chat" would otherwise be
  // bounced back to "Multi-agent chat" after leaving once.
  const [mode, setMode] = useStickyTab<ChatMode>(
    "agents.chat.mode", CHAT_MODES.map((m) => m.id), "runtask");

  const handoffs = snap?.handoff_events ?? [];

  return (
    <div className="space-y-4">
      {node}
      {/* mode selector + status bar */}
      <Card className="space-y-2 px-4 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <span className="flex items-center gap-2 text-sm font-medium">
            <Avatar id={SUP_ID} size={20} /> {tr("编排与协调", "Orchestration")}
          </span>
          {task?.active ? (
            <Badge tone="AUTO">{tr("任务进行中", "Task running")}</Badge>
          ) : (
            <Badge tone="INFO">{tr("无活动任务", "No active task")}</Badge>
          )}
          <div className="ml-auto">
            <Button variant="default" onClick={onOpenInterrupts}>
              {tr("人机交互", "Human-in-the-loop")}
            </Button>
          </div>
        </div>
        <SubTabs<ChatMode> tabs={CHAT_MODES} value={mode} onChange={setMode} />
        {mode === "runtask" ? (
          <p className="text-xs text-mast-muted">
            {tr(
              "与 6 智能体编排器持续对话（POST /api/agents/run-task）：第一条消息下发任务，运行期间继续输入即可插话引导（POST /api/agents/{target}/interject），逐智能体进展、交接与 HITL 批准内联呈现；与「编排器私聊」（单代理对话）不同。",
              "Ongoing conversation with the 6-agent orchestrator (POST /api/agents/run-task): the first message dispatches the task; keep typing while it runs to steer it (POST /api/agents/{target}/interject). Per-agent progress, handoffs and HITL approvals appear inline. This differs from \"Supervisor chat\" (single-agent conversation).",
            )}
          </p>
        ) : (
          <p className="text-xs text-mast-muted">
            {tr("与编排智能体的单代理私聊线程（不触发多智能体编排）。", "Single-agent private chat thread with the supervisor (does not trigger multi-agent orchestration).")}
          </p>
        )}
      </Card>

      {/* PRIMARY: real multi-agent orchestrator runner */}
      {mode === "runtask" && (
        <RunTaskPanel conversationId={null} />
      )}

      {/* SECONDARY: legacy per-agent supervisor private chat (kept as-is) */}
      {mode === "supchat" && <AgentChatPanel agentId={SUP_ID} />}

      {/* Handoff event timeline (from snapshot) — per-kind icon + colour so the
          operator can scan it at a glance. */}
      <div>
        <h4 className="mb-2 text-sm font-medium text-mast-muted">{tr("交接事件时间线", "Handoff event timeline")}</h4>
        {handoffs.length === 0 ? (
          <EmptyNote label={tr("暂无交接事件（编排器空闲或未接入）。", "No handoff events (orchestrator idle or not connected).")} />
        ) : (
          <ol className="space-y-1.5 border-l border-mast-border pl-4">
            {handoffs.map((ev, i) => {
              const st = TIMELINE_STYLE[ev.kind ?? ""] ?? TIMELINE_FALLBACK;
              return (
                <li key={i} className="relative text-sm">
                  <span
                    className={`absolute -left-[21px] top-1.5 h-2 w-2 rounded-full ${st.dot}`}
                  />
                  <span className="mr-2 font-mono text-xs text-mast-muted">
                    {ev.t != null ? new Date(ev.t * 1000).toLocaleTimeString(langTag()) : "—"}
                  </span>
                  {ev.kind && (
                    <Badge tone={st.tone}>
                      {st.icon} {st.label ?? ev.kind}
                      {/* the fan-out width: the event carries targets[], and the
                          count is the whole point of a parallel dispatch */}
                      {(ev.targets?.length ?? 0) > 1 && ` ×${ev.targets!.length}`}
                    </Badge>
                  )}
                  <span className="ml-2 text-mast-text">{ev.text}</span>
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════════
// Agent chat — per-agent private chat threads
// ════════════════════════════════════════════════════════════════════════════

function AgentChatView({ selected, onSelect }: { selected: string; onSelect: (id: string) => void }) {
  // The supervisor (_supervisor) is selectable here too so its group-chat contributions are
  // viewable per agent like everyone else (agent_activity("_supervisor") returns its group
  // messages). Its 1:1 private chat lives under Chat → Supervisor chat, so the private-chat
  // panel below is shown only for the pipeline agents.
  const isSup = selected === SUP_ID;
  const { toast, node } = useToast();
  const [interjectText, setInterjectText] = useState("");

  // Interject — queue an operator interjection for the running supervisor; it is delivered
  // as an operator message on the supervisor's next super-step.
  // LIVE-only; degrade-safe (typed no-op when no live app / not running).
  const interject = useMutation({
    mutationFn: async (text: string) => {
      const { data, error } = await api.POST("/api/agents/{agent_id}/interject", {
        params: { path: { agent_id: SUP_ID } },
        body: { text },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      if (data?.ok && !data.degraded) {
        toast(tr("已插话，将于编排器下一步送达", "Interjection sent; it will be delivered at the supervisor's next step"), "ok");
        setInterjectText("");
      } else {
        const why = data?.detail ?? tr("编排器未在运行 / 后端降级", "supervisor not running / backend degraded");
        toast(tr(`插话未生效（${why}）`, `Interjection had no effect (${why})`), "err");
      }
    },
    onError: (e) => toast(String((e as Error)?.message ?? e), "err"),
  });

  function submitInterject() {
    const t = interjectText.trim();
    if (!t || interject.isPending) return;
    interject.mutate(t);
  }

  return (
    <div className="space-y-4">
      {node}
      <div className="flex flex-wrap gap-2">
        {[SUPERVISOR, ...AGENTS].map((a) => (
          <button
            key={a.id}
            onClick={() => onSelect(a.id)}
            className={
              "flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm " +
              (selected === a.id
                ? "border-mast-accent bg-mast-accent/10 text-mast-accent"
                : "border-mast-border text-mast-muted hover:text-mast-text")
            }
          >
            <Avatar id={a.id} size={16} /> {a.cn}
          </button>
        ))}
      </div>

      {/* Interject — operator interjection to the running supervisor */}
      <Card className="space-y-2 px-4 py-3">
        <Field
          label={tr("插话", "Interject")}
          hint={tr("向正在运行的编排智能体注入一条用户消息（不打断当前回合，于下一步送达）。", "Inject a user message into the running supervisor (does not interrupt the current turn; delivered at the next step).")}
        >
          <div className="flex items-end gap-2">
            <textarea
              value={interjectText}
              onChange={(e) => setInterjectText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  submitInterject();
                }
              }}
              placeholder={tr("例：优先处理 Au(111)，跳过当前样品的剩余扫描", "e.g. prioritise Au(111) and skip the remaining scans of the current sample")}
              rows={2}
              disabled={interject.isPending}
              className="flex-1 resize-none rounded-md border border-mast-border bg-mast-bg px-3 py-2 text-sm text-mast-text outline-none focus:border-mast-accent disabled:opacity-50"
            />
            <Button
              variant="primary"
              onClick={submitInterject}
              disabled={!interjectText.trim() || interject.isPending}
            >
              {interject.isPending ? tr("插话中…", "Sending…") : tr("插话", "Interject")}
            </Button>
          </div>
        </Field>
      </Card>

      {/* Group-chat activity — what this agent said/did in multi-agent runs, the per-agent
          view of the team conversation. Covers the supervisor too: its routing/decision turns
          are persisted with agent_id="_supervisor" and surface here. */}
      <GroupActivityFeed agentId={selected} />

      {isSup ? (
        <Card className="px-4 py-3 text-sm text-mast-muted">
          {tr("编排器的 1:1 私聊在「对话 → 编排器私聊」标签。此处展示的是它在多智能体群聊中的发言。", "The supervisor's 1:1 private chat is under Chat → Supervisor chat. This view shows what it said in multi-agent group chats.")}
        </Card>
      ) : (
        <AgentChatPanel agentId={selected} />
      )}
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════════
// Inspector — per-agent model+thinking picker + tool catalog DataTable
// ════════════════════════════════════════════════════════════════════════════

type ToolRow = {
  name: string;
  safety_level: string;
  composition_level?: string | null;
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const TOOL_COLUMNS: ColumnDef<ToolRow, any>[] = [
  {
    accessorKey: "name",
    header: tr("工具名", "Tool name"),
    cell: (c) => <span className="font-mono text-xs">{c.getValue() as string}</span>,
  },
  {
    accessorKey: "safety_level",
    header: tr("安全等级", "Safety level"),
    cell: (c) => {
      const sl = (c.getValue() as string) || "AUTO";
      return <Badge tone={safetyTone(sl)}>{sl}</Badge>;
    },
  },
  {
    accessorKey: "composition_level",
    header: tr("组合层级", "Composition level"),
    cell: (c) => {
      const lvl = c.getValue() as string | null | undefined;
      return lvl ? (
        <span className="font-mono text-xs text-mast-muted">{lvl}</span>
      ) : (
        <span className="text-xs text-mast-muted">—</span>
      );
    },
  },
];

function InspectorView({
  live,
  selected,
  onSelect,
}: {
  live: Record<string, AgentLive>;
  selected: string;
  onSelect: (id: string) => void;
}) {
  const qc = useQueryClient();
  const { toast, node } = useToast();
  const tools = useAgentTools();
  const models = useQuery({
    queryKey: ["config", "models"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/config/models");
      if (error) throw error;
      return data;
    },
  });
  const [filter, setFilter] = useState("");

  const toolsByAgent = useMemo(() => {
    const m = new Map<string, ToolRow[]>();
    for (const cat of tools.data?.agents ?? []) {
      m.set(
        cat.agent_id,
        (cat.tools ?? []).map((t) => ({
          name: t.name,
          safety_level: t.safety_level,
          composition_level: t.composition_level,
        })),
      );
    }
    return m;
  }, [tools.data]);

  const override = useMutation({
    mutationFn: async (vars: { agent_id: string; model?: string; thinking?: string }) => {
      const { data, error } = await api.POST("/api/agents/{agent_id}/model-override", {
        params: { path: { agent_id: vars.agent_id } },
        body: { model: vars.model ?? null, thinking: vars.thinking ?? null },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      if (data?.ok && !data.degraded) toast(tr("已保存并将于下个任务生效", "Saved; takes effect on the next task"), "ok");
      else toast(tr("后端未接入 / 降级，未生效", "Backend not connected / degraded; no effect"), "err");
      qc.invalidateQueries({ queryKey: ["agents", "snapshot"] });
      qc.invalidateQueries({ queryKey: ["agents", "models"] });
    },
    onError: (e) => toast(String((e as Error)?.message ?? e), "err"),
  });

  const a = agentDef(selected);
  const lv = live[selected] ?? {};
  const selectedTools = toolsByAgent.get(selected) ?? [];

  // model options from /api/config/models (fall back to current live model only)
  const modelOptions = useMemo(() => {
    const opts: { value: string; label: string }[] = [];
    const seen = new Set<string>();
    for (const m of models.data?.models ?? []) {
      const id = m.alias;
      if (!id || seen.has(id)) continue;
      seen.add(id);
      opts.push({ value: id, label: m.description ? `${id} · ${m.description}` : id });
    }
    if (lv.model && !seen.has(String(lv.model))) {
      opts.unshift({ value: String(lv.model), label: String(lv.model) });
    }
    if (opts.length === 0 && lv.model) opts.push({ value: String(lv.model), label: String(lv.model) });
    return opts;
  }, [models.data, lv.model]);

  return (
    <div className="space-y-5">
      {node}
      {/* agent picker chips */}
      <div className="flex flex-wrap gap-2">
        {ROSTER.map((id) => (
          <button
            key={id}
            onClick={() => onSelect(id)}
            className={
              "flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm " +
              (selected === id
                ? "border-mast-accent bg-mast-accent/10 text-mast-accent"
                : "border-mast-border text-mast-muted hover:text-mast-text")
            }
          >
            <Avatar id={id} size={16} active={lv && live[id]?.active} /> {agentDef(id).cn}
          </button>
        ))}
      </div>

      {/* model + thinking picker */}
      <Card className="space-y-3 px-4 py-4">
        <div className="flex items-center gap-3">
          <Avatar id={selected} size={26} active={lv.active} />
          <div>
            <div className="font-mono text-sm font-semibold">{selected}</div>
            <div className="text-xs text-mast-muted">{a.role}</div>
          </div>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field
            label={tr("模型", "Model")}
            hint={tr("保存后重建编排器，下个任务生效（不影响进行中的会话）", "Rebuilds the orchestrator on save; takes effect on the next task (does not affect a running session)")}
          >
            <SelectField
              value={String(lv.model ?? "")}
              onChange={(v) => override.mutate({ agent_id: selected, model: v })}
              options={
                modelOptions.length
                  ? modelOptions
                  : [{ value: String(lv.model ?? ""), label: String(lv.model ?? tr("（未知）", "(unknown)")) }]
              }
            />
          </Field>
          <Field
            label={tr("思考强度", "Thinking effort")}
            hint={tr("对 Claude 映射为扩展思考预算；Kimi/DeepSeek 等推理模型自带推理、固定最高强度。", "Mapped to an extended-thinking budget for Claude; reasoning models such as Kimi/DeepSeek reason natively at a fixed maximum effort.")}
          >
            <div className="flex gap-1">
              {THINKING_LEVELS.map((tl) => (
                <button
                  key={tl}
                  onClick={() => override.mutate({ agent_id: selected, thinking: tl })}
                  className={
                    "rounded-md px-3 py-1.5 font-mono text-xs " +
                    ((lv.thinking ?? "").toLowerCase() === tl
                      ? "bg-mast-accent/25 text-mast-accent"
                      : "border border-mast-border text-mast-muted hover:text-mast-text")
                  }
                >
                  {tl}
                </button>
              ))}
            </div>
          </Field>
        </div>
      </Card>

      {/* tool catalog */}
      <div>
        <div className="mb-3 flex items-center justify-between gap-3">
          <h4 className="text-sm font-medium">
            {tr("工具目录", "Tool catalog")} · {a.cn}
            <span className="ml-2 text-xs text-mast-muted">{tr(`共 ${selectedTools.length} 个`, `${selectedTools.length} total`)}</span>
          </h4>
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder={tr("搜索工具名 / 安全等级 / 层级…", "Search tool name / safety level / level…")}
            className="w-64 rounded border border-mast-border bg-mast-bg px-3 py-1.5 text-sm text-mast-text outline-none focus:border-mast-accent"
          />
        </div>
        {tools.isPending && <Spinner />}
        {tools.isError && <ErrorNote error={tools.error} />}
        {tools.data && tools.data.degraded && <DegradedNote what={tr("工具目录", "Tool catalog")} />}
        {tools.data && !tools.data.degraded && (
          <DataTable
            data={selectedTools}
            columns={TOOL_COLUMNS}
            globalFilter={filter}
            empty={tr("该智能体暂无工具（或目录尚未预热）。", "This agent has no tools (or the catalog is not warmed up yet).")}
          />
        )}
      </div>
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════════
// Workspace overview — shared objects: R/W permission matrix + artifact list + the
//   selected-artifact detail card (read-only overview). Mirrors the old
//   AGWorkspaceView: access matrix, per-artifact detail with an "Open editor" button
//   that jumps to the editor sub-tab.
// ════════════════════════════════════════════════════════════════════════════

// Permission-matrix columns = the 6 pipeline agents. The old table also carried
// operator / SUP / BUF columns, but in the REAL flow model none of them read or
// write a pipeline artifact (the supervisor routes; BUF narrates the buffer;
// the operator edits through the editor, not through an agent edge) — so those
// columns were three permanently-empty stripes pretending to carry information.
const WS_COLS: string[] = [...PIPELINE];

function WorkspaceView({ onEdit }: { onEdit: (id: string) => void }) {
  const artifacts = useQuery({
    queryKey: ["artifacts"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/artifacts");
      if (error) throw error;
      return data;
    },
    refetchInterval: 6000,
  });
  const perms = useQuery({
    queryKey: ["artifacts", "permissions"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/artifacts/permissions");
      if (error) throw error;
      return data;
    },
  });

  const entries = artifacts.data?.artifacts ?? [];
  const matrix = perms.data?.permissions ?? [];
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({});

  // Artifact grouping.
  //
  // Which classes are still "pending" must not be computed in the browser by subtracting
  // produced ids from permission rows: a produced id is per-FILE ("draft:Au111_v001", always
  // contains a colon) while a permission row id is the CLASS ("draft"), so such a filter
  // removes nothing and every class would show as pending on every load. The class state is
  // therefore not derived here at all. `groups` comes from GET /api/artifacts, which asks
  // each class its own store; this is the only way the non-file classes (literature library,
  // experiment records, long-term memory, vision buffer) can report themselves, since
  // nothing walks a directory to find them.
  const sessionActive = !!artifacts.data?.session_active;
  const groups = useMemo(() => {
    const byClass = new Map<string, typeof entries>();
    for (const e of entries) {
      const k = e.kind ?? "";
      const bucket = byClass.get(k);
      if (bucket) bucket.push(e);
      else byClass.set(k, [e]);
    }
    return (artifacts.data?.groups ?? []).map((g) => ({
      ...g,
      items: byClass.get(g.artifact_id) ?? [],
      producer:
        (matrix.find((r) => r.artifact_id === g.artifact_id)?.writers ?? []).find(
          (w) => w !== "operator",
        ) ?? null,
    }));
  }, [entries, matrix, artifacts.data?.groups]);

  // A group starts open when it holds a manageable number of files. `scan_files` is flagged
  // high_volume by the backend (one row per SaveScan; a night's run is hundreds), which would
  // make the list unreadable. Classes with no enumerable files (the DB/registry ones) have
  // nothing to expand.
  function isOpen(g: { artifact_id: string; high_volume: boolean; items: unknown[] }) {
    const explicit = openGroups[g.artifact_id];
    if (explicit !== undefined) return explicit;
    return g.items.length > 0 && !g.high_volume && g.items.length <= 6;
  }
  const pendingCount = groups.filter((g) => !g.produced).length;

  // (ArtifactFlowGraph consumes the permission rows directly, since those carry the
  // label / kind / store / multi_writer the picture needs.)

  // Permission lookup for the selected-artifact detail card. The selection is a per-FILE id
  // ("draft:Au111_v001") while permissions are keyed by CLASS ("draft"), so resolve through
  // the entry's kind; matching the file id against the class id directly never hits and the
  // access panel would show "no permission info" for every produced artifact.
  const selEntry = entries.find((e) => e.id === selectedId);
  const selRow = matrix.find((r) => r.artifact_id === (selEntry?.kind ?? selectedId));
  const writers = new Set(selRow?.writers ?? []);
  const readers = new Set([...(selRow?.readers ?? []), ...(selRow?.writers ?? [])]);

  return (
    <div className="space-y-5">
      {/* header — shared objects · Workspace */}
      <div className="flex flex-wrap items-baseline gap-3">
        <h2 className="text-lg font-semibold tracking-tight">{tr("共享对象 · Workspace", "Shared objects · Workspace")}</h2>
        <span className="text-xs text-mast-muted">
          {tr("本次运行的客体 · agent 和用户均可直接读写 · 写权限收紧", "Objects of this run · readable and writable by agents and the user · write access restricted")}
        </span>
        {artifacts.data && (
          <span className="ml-auto font-mono text-xs text-mast-muted">
            {tr(`${artifacts.data.count} 对象`, `${artifacts.data.count} ${artifacts.data.count === 1 ? "object" : "objects"}`)}
          </span>
        )}
      </div>

      {/* Data-flow graph: the real directed flow. Production is the skeleton (solid, in the
          writer's hue), consumption the detail (dashed, on hover). Multi-writer artifacts are
          called out; the two artifacts everyone touches are drawn as a shared bus / side rail
          rather than as nodes with a dozen edges each. */}
      <div>
        <h4 className="mb-2 text-sm font-medium">
          {tr("数据流 · 谁写 → 谁读", "Data flow · who writes → who reads")}
          <span className="ml-2 text-xs font-normal text-mast-muted">
            {tr("· 悬停聚焦 · 单击选中 · 双击打开编辑器", "· hover to focus · click to select · double-click to open the editor")}
          </span>
        </h4>
        {perms.isPending && <Spinner />}
        {perms.isError && <ErrorNote error={perms.error} />}
        {perms.data && perms.data.degraded && <DegradedNote what={tr("数据流图", "Data-flow graph")} />}
        {perms.data && !perms.data.degraded && matrix.length === 0 && (
          <EmptyNote label={tr("无产物定义。", "No artifact definitions.")} />
        )}
        {matrix.length > 0 && (
          <ArtifactFlowGraph
            permissions={matrix}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onOpen={onEdit}
          />
        )}
      </div>

      {/* R/W matrix — the precise lookup that complements the flow graph.
          A grid is still the fastest way to answer "may agent X write artifact
          Y?"; the graph answers "where does the data go". Both read from the
          same (now truthful) flow model. */}
      <div>
        <h4 className="mb-2 text-sm font-medium">
          {tr("读写矩阵", "Read/write matrix")}
          <span className="ml-2 text-xs font-normal text-mast-muted">
            {tr("· W=写入 · R=读取 · 点击行聚焦", "· W=write · R=read · click a row to focus")}
          </span>
        </h4>
        {perms.isPending && <Spinner />}
        {perms.isError && <ErrorNote error={perms.error} />}
        {perms.data && perms.data.degraded && <DegradedNote what={tr("读写矩阵", "Read/write matrix")} />}
        {perms.data && !perms.data.degraded && matrix.length === 0 && (
          <EmptyNote label={tr("无产物定义。", "No artifact definitions.")} />
        )}
        {matrix.length > 0 && (
          <div className="overflow-x-auto rounded-lg border border-mast-border">
            <table className="w-full text-sm">
              <thead className="border-b border-mast-border text-left text-xs text-mast-muted">
                <tr>
                  <th className="px-3 py-2">{tr("产物", "Artifact")}</th>
                  {WS_COLS.map((id) => (
                    <th key={id} className="px-2 py-2 text-center">
                      {agentDef(id).short}
                    </th>
                  ))}
                  <th className="px-3 py-2 text-left">{tr("存储位置", "Storage")}</th>
                </tr>
              </thead>
              <tbody>
                {matrix.map((row) => {
                  const w = new Set(row.writers ?? []);
                  const r = new Set(row.readers ?? []);
                  const isSel = row.artifact_id === selectedId;
                  return (
                    <tr
                      key={row.artifact_id}
                      onClick={() => setSelectedId(row.artifact_id)}
                      className={
                        "cursor-pointer border-b border-mast-border/50 hover:bg-mast-bg/60 " +
                        (isSel ? "bg-mast-accent-soft" : "")
                      }
                    >
                      <td className="px-3 py-2">
                        <div className="flex items-center gap-1.5">
                          <span className="text-mast-text">{row.label || row.artifact_id}</span>
                          {row.multi_writer && (
                            <span
                              className="rounded border border-mast-accent px-1 font-mono text-[9px] text-mast-accent"
                              title={tr(
                                `多个智能体都写入：${(row.writers ?? []).map((x) => agentDef(x).cn).join("、")}`,
                                `Written by several agents: ${(row.writers ?? []).map((x) => agentDef(x).cn).join(", ")}`,
                              )}
                            >
                              {tr("多写", "multi")}
                            </span>
                          )}
                        </div>
                        <div className="font-mono text-[10px] text-mast-muted">
                          {row.artifact_id}
                        </div>
                      </td>
                      {WS_COLS.map((id) => {
                        const canW = w.has(id);
                        const canR = r.has(id);
                        return (
                          <td key={id} className="px-2 py-2 text-center">
                            {canW && canR ? (
                              <span className="font-mono text-xs text-mast-warn" title={tr("读 + 写", "Read + write")}>
                                W·R
                              </span>
                            ) : canW ? (
                              <span className="font-mono text-xs text-mast-warn" title={tr("写入", "Write")}>
                                W
                              </span>
                            ) : canR ? (
                              <span className="font-mono text-xs text-mast-info" title={tr("只读", "Read-only")}>
                                R
                              </span>
                            ) : (
                              <span className="text-mast-muted/40">·</span>
                            )}
                          </td>
                        );
                      })}
                      <td className="px-3 py-2 font-mono text-[10px] text-mast-muted">
                        {row.store || "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="px-3 py-2 text-xs text-mast-muted/80">
              <span className="text-mast-warn">W</span> {tr("读 + 写", "read + write")} ·{" "}
              <span className="text-mast-info">R</span> {tr("只读", "read-only")}
            </p>
          </div>
        )}
      </div>

      {/* artifact list */}
      <div>
        <div className="mb-2 flex items-center gap-3">
          <h4 className="text-sm font-medium">{tr("工作区制品", "Workspace artifacts")}</h4>
          {artifacts.data && (
            <Badge tone={artifacts.data.session_active ? "AUTO" : "INFO"}>
              {artifacts.data.session_active ? tr("会话活跃", "Session active") : tr("空闲", "Idle")} ·{" "}
              {tr(`${artifacts.data.count} 项`, `${artifacts.data.count} ${artifacts.data.count === 1 ? "item" : "items"}`)}
            </Badge>
          )}
          {pendingCount > 0 && (
            <Badge tone="INFO">{tr(`${pendingCount} 类尚未产出`, `${pendingCount} not produced yet`)}</Badge>
          )}
        </div>
        {artifacts.isPending && <Spinner />}
        {artifacts.isError && <ErrorNote error={artifacts.error} />}
        {artifacts.data && artifacts.data.degraded && <DegradedNote what={tr("制品列表", "Artifact list")} />}
        {artifacts.data && !artifacts.data.degraded && groups.length === 0 && (
          <EmptyNote label={tr("无产物定义。", "No artifact definitions.")} />
        )}
        <p className="mb-2 text-xs text-mast-muted">
          {tr("按类型分组 · 点组标题展开/折叠 · 计数来自各自的存储（不是猜的）", "Grouped by type · click a group title to expand/collapse · counts come from each store (not guessed)")}
          {sessionActive && tr(" · 实验进行中", " · experiment running")}
        </p>
        {/* Grouped by class instead of one flat grid. A run saves one row per scan, so the
            .sxm pile would otherwise bury the drafts, figures and plans underneath it. */}
        <div className="space-y-2">
          {groups.map((g) => {
            const open = isOpen(g);
            const expandable = g.items.length > 0;
            return (
              <div
                key={g.artifact_id}
                className={
                  "overflow-hidden rounded-lg border " +
                  (g.produced ? "border-mast-border" : "border-dashed border-mast-border/60")
                }
              >
                <button
                  type="button"
                  disabled={!expandable}
                  onClick={() =>
                    setOpenGroups((s) => ({ ...s, [g.artifact_id]: !open }))
                  }
                  title={g.store}
                  className={
                    "flex w-full items-center gap-2 px-3 py-2 text-left " +
                    (expandable ? "hover:bg-mast-bg/60" : "cursor-default")
                  }
                >
                  <span className="w-3 shrink-0 font-mono text-xs text-mast-muted">
                    {expandable ? (open ? "▾" : "▸") : ""}
                  </span>
                  <span className="text-sm text-mast-text">{g.label || g.artifact_id}</span>
                  <span className="font-mono text-[10px] text-mast-muted">
                    {g.artifact_id}
                  </span>
                  {/* Honest three-way state. "Pending" is shown only when the store really is
                      empty; "unreadable" covers a store we could not open, which must never be
                      rendered as "0 / pending". */}
                  {!g.known ? (
                    <Badge tone="WARN">{tr("无法读取", "Unreadable")}</Badge>
                  ) : g.produced ? (
                    <Badge tone="AUTO">{tr("已产出", "Produced")} · {g.count}</Badge>
                  ) : (
                    <Badge tone="INFO">{tr("待产出", "Pending")}</Badge>
                  )}
                  {g.high_volume && g.items.length > 0 && (
                    <span className="text-[10px] text-mast-muted/70">{tr("量大 · 默认折叠", "large · collapsed by default")}</span>
                  )}
                  <span className="ml-auto truncate pl-2 text-xs text-mast-muted">
                    {g.detail}
                  </span>
                </button>

                {/* Classes whose contents are not files (experiment records / long-term memory /
                    vision buffer / literature library) have a real count but nothing to list
                    here; say where they live instead of showing a fake empty folder. They get
                    no edit button either: POST /api/artifacts/<class>/edit is always rejected
                    for a bare class id, and a button that cannot work is worse than none. */}
                {!expandable && (
                  <div className="border-t border-mast-border/50 px-3 py-2 text-xs text-mast-muted/80">
                    {g.produced
                      ? tr(`内容存于 ${g.store}（此处不逐条列出）`, `Content is stored in ${g.store} (not listed individually here)`)
                      : tr(
                          `尚未产出 · 由 ${g.producer ? agentLabel(g.producer) : "系统"} 写入 ${g.store}`,
                          `Not produced yet · written to ${g.store} by ${g.producer ? agentLabel(g.producer) : "the system"}`,
                        )}
                  </div>
                )}

                {expandable && open && (
                  <div className="grid grid-cols-1 gap-2 border-t border-mast-border/50 p-2 sm:grid-cols-2 lg:grid-cols-3">
                    {g.items.map((e) => (
                      <div
                        key={e.id}
                        onClick={() => setSelectedId(e.id)}
                        className={
                          "cursor-pointer space-y-1 rounded-md border border-mast-border bg-mast-panel px-3 py-2 " +
                          (e.id === selectedId ? "ring-1 ring-mast-accent" : "")
                        }
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="truncate font-mono text-xs" title={e.path || undefined}>
                            {e.preview || e.id}
                          </span>
                          {e.edited && <Badge tone="WARN">{tr("已编辑", "Edited")}</Badge>}
                        </div>
                        {e.producer && (
                          <div className="text-xs text-mast-muted">
                            {tr("产出", "Produced by")} · {agentLabel(e.producer)}
                          </div>
                        )}
                        <div className="pt-1" onClick={(ev) => ev.stopPropagation()}>
                          {/* Only file-backed editable artifacts get a button. Read-only rows
                              (e.g. .sxm scans) are refused by the backend, so a button there
                              could only open a degraded editor. */}
                          {e.editable ? (
                            <Button variant="default" onClick={() => onEdit(e.id)}>
                              {tr("打开编辑器", "Open editor")}
                            </Button>
                          ) : (
                            <Badge tone="INFO">{tr("只读", "Read-only")}</Badge>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* selected-artifact detail card with access-permission panel + Open editor */}
      {selectedId && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_320px]">
          <Card className="space-y-3 border-l-4 border-l-mast-accent px-5 py-4">
            <div className="flex items-baseline gap-2">
              <span className="font-mono text-base font-semibold">{selectedId}</span>
              {selEntry?.kind && (
                <span className="font-mono text-xs text-mast-muted">· {selEntry.kind}</span>
              )}
              {selEntry?.producer && (
                <span className="text-xs text-mast-muted">· {tr("产出", "produced by")} {agentLabel(selEntry.producer)}</span>
              )}
            </div>
            {selEntry?.preview ? (
              <pre className="max-h-60 overflow-auto whitespace-pre-wrap rounded-md border border-mast-border bg-mast-bg/60 p-3 font-mono text-xs leading-relaxed">
                {selEntry.preview}
              </pre>
            ) : (
              <EmptyNote label={tr("无预览正文。", "No preview text.")} />
            )}
            <div className="flex flex-wrap items-center gap-2">
              {selEntry?.editable ? (
                <Button variant="primary" onClick={() => onEdit(selectedId)}>
                  {tr("打开编辑器", "Open editor")}
                </Button>
              ) : (
                <span className="text-xs text-mast-muted">
                  {tr("只读", "Read-only")} · {selEntry?.path || tr("该产物不可手工编辑", "This artifact cannot be edited by hand")}
                </span>
              )}
            </div>
          </Card>

          <Card className="space-y-0 px-0 py-0">
            <div className="border-b border-mast-border px-4 py-2">
              <div className="text-xs font-semibold uppercase tracking-wide text-mast-muted">
                {tr("访问权限", "Access permissions")}
              </div>
              <div className="mt-0.5 text-xs text-mast-muted">
                {tr(`${writers.size} 写 · ${readers.size} 读`, `${writers.size} write · ${readers.size} read`)}
              </div>
            </div>
            {selRow
              ? WS_COLS.filter((id) => readers.has(id)).map((id) => {
                  const canWrite = writers.has(id);
                  const def = id === "operator" ? null : agentDef(id);
                  return (
                    <div
                      key={id}
                      className="flex items-center gap-3 border-b border-mast-border/50 px-4 py-2 last:border-b-0"
                    >
                      {def ? (
                        <Avatar id={id} size={18} />
                      ) : (
                        <span className="inline-flex h-[18px] w-[18px] items-center justify-center rounded-full bg-mast-muted/40 font-mono text-[8px] font-bold text-mast-text">
                          OP
                        </span>
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="font-mono text-xs font-semibold">
                          {def ? def.short : "OP"}
                        </div>
                        <div className="text-xs text-mast-muted">
                          {def ? def.cn : tr("用户", "User")}
                        </div>
                      </div>
                      <span
                        className={
                          "rounded px-2 py-0.5 font-mono text-xs font-semibold " +
                          (canWrite
                            ? "bg-mast-warn-bg text-mast-warn"
                            : "bg-mast-bg text-mast-muted")
                        }
                      >
                        {canWrite ? tr("读 + 写", "Read + write") : tr("只读", "Read-only")}
                      </span>
                    </div>
                  );
                })
              : (
                <div className="px-4 py-3">
                  <EmptyNote label={tr("无权限信息。", "No permission info.")} />
                </div>
              )}
          </Card>
        </div>
      )}
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════════
// Object editor — inline per-artifact editor (left artifact list + center editor).
//   Mirrors the old AGWorkspaceEditorView 3-column layout: left = artifact roster, center =
//   the operator editor (edit / diff / history). The center editor reuses the fully wired
//   ArtifactEditor logic, rendered inline (not a modal) so it lives on its own sub-tab.
// ════════════════════════════════════════════════════════════════════════════

function EditorView({
  selected,
  onSelect,
}: {
  selected: string | null;
  onSelect: (id: string | null) => void;
}) {
  const artifacts = useQuery({
    queryKey: ["artifacts"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/artifacts");
      if (error) throw error;
      return data;
    },
    refetchInterval: 6000,
  });
  // Artifact classes that nothing has produced yet are shown as informational rows, not as
  // editable slots: a manuscript cannot be brought into existence by typing into a textarea
  // (paper_writing creates it with save_draft), and the experiment DB or a Nanonis .sxm cannot
  // be hand-edited at all. Showing them still says what the system can produce and where it
  // lands, so they stay as rows you read rather than buttons that lie.
  const perms = useQuery({
    queryKey: ["artifacts", "permissions"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/artifacts/permissions");
      if (error) throw error;
      return data;
    },
  });
  const entries = artifacts.data?.artifacts ?? [];
  const cur = entries.find((e) => e.id === selected) ?? null;
  const [showReadonly, setShowReadonly] = useState(false);
  // Only file-backed editable artifacts (drafts / reviews) are clickable edit targets.
  // Produced-but-read-only artifacts (scan .sxm / figures) are listed as informational rows:
  // the backend refuses to edit them, so a click would only open a degraded editor. They are
  // usually the newest files (the list is mtime-sorted) and would otherwise sit at the top.
  const editableEntries = entries.filter((e) => e.editable);
  // Collapsed behind one summary row instead of N flat rows: this rail is 260px wide and a
  // run's scans would push everything else off the screen.
  const readonlyEntries = entries.filter((e) => !e.editable);
  // "Pending" comes from the backend's per-class status, not from subtracting the produced
  // files: the literature library / experiment records / long-term memory / vision buffer have
  // no files to enumerate, so subtracting would mark them pending permanently. Sharing
  // `groups` also keeps this tab and the workspace overview from disagreeing about a class.
  const permById = new Map(
    (perms.data?.permissions ?? []).map((r) => [r.artifact_id, r]),
  );
  const rosterSlots = (artifacts.data?.groups ?? [])
    .filter((g) => !g.produced)
    .map((g) => ({
      id: g.artifact_id,
      label: g.label || g.artifact_id,
      store: g.store ?? "",
      known: g.known,
      producer:
        (permById.get(g.artifact_id)?.writers ?? []).find((w) => w !== "operator") ??
        null,
    }));

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-3">
        <h2 className="text-lg font-semibold tracking-tight">{tr("对象编辑", "Object editor")}</h2>
        <span className="text-xs text-mast-muted">
          {tr("直接编辑智能体产出的文档（论文草稿 / 评审报告）。保存 = 存为新版本，下一个打开该文档的智能体会读到你的修改。", "Edit documents produced by the agents (paper drafts / review reports) directly. Saving creates a new version; the next agent that opens the document will see your changes.")}
        </span>
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[260px_1fr]">
        {/* left: artifact roster */}
        <div className="space-y-1">
          {artifacts.isPending && <Spinner />}
          {artifacts.isError && <ErrorNote error={artifacts.error} />}
          {artifacts.data && artifacts.data.degraded && <DegradedNote what={tr("制品列表", "Artifact list")} />}
          {artifacts.data && !artifacts.data.degraded && entries.length === 0 && rosterSlots.length === 0 && (
            <EmptyNote label={tr("当前会话尚无制品。", "No artifacts in the current session yet.")} />
          )}
          {editableEntries.map((e) => (
            <button
              key={e.id}
              onClick={() => onSelect(e.id)}
              className={
                "flex w-full items-center gap-2 rounded-md border px-3 py-2 text-left text-sm " +
                (e.id === selected
                  ? "border-mast-accent bg-mast-accent/10 text-mast-accent"
                  : "border-mast-border text-mast-muted hover:text-mast-text")
              }
            >
              <span className="min-w-0 flex-1 truncate font-mono text-xs">{e.id}</span>
              {e.edited && <Badge tone="WARN">{tr("已编辑", "Edited")}</Badge>}
            </button>
          ))}
          {/* Produced but read-only (scan .sxm / figures): shown so the operator knows they
              exist and where (title=path), but not clickable since the backend cannot edit
              them. Behind one collapsed summary row so they do not push the editable drafts
              off the rail. */}
          {readonlyEntries.length > 0 && (
            <button
              type="button"
              onClick={() => setShowReadonly((s) => !s)}
              className="flex w-full items-center gap-2 rounded-md border border-dashed border-mast-border/50 px-3 py-2 text-left text-sm text-mast-muted/70 hover:text-mast-text"
            >
              <span className="w-3 shrink-0 font-mono text-xs">
                {showReadonly ? "▾" : "▸"}
              </span>
              <span className="min-w-0 flex-1 truncate text-xs">
                {tr(`只读产物 · ${readonlyEntries.length} 项`, `Read-only artifacts · ${readonlyEntries.length}`)}
              </span>
            </button>
          )}
          {showReadonly &&
            readonlyEntries.map((e) => (
              <div
                key={e.id}
                title={e.path || undefined}
                className="flex w-full items-center gap-2 rounded-md border border-dashed border-mast-border/50 py-2 pl-6 pr-3 text-left text-sm text-mast-muted/70"
              >
                <span className="min-w-0 flex-1 truncate font-mono text-xs">{e.id}</span>
                <Badge tone="INFO">{tr("只读", "Read-only")}</Badge>
              </div>
            ))}
          {/* Not-yet-produced artifact CLASSES — informational, not clickable.
              A dashed row that opens an editor over a file that does not exist
              is a button that lies; these say what will be produced and where. */}
          {rosterSlots.map((slot) => (
            <div
              key={slot.id}
              title={slot.store}
              className="flex w-full items-center gap-2 rounded-md border border-dashed border-mast-border/50 px-3 py-2 text-left text-sm text-mast-muted/70"
            >
              <span className="min-w-0 flex-1 truncate font-mono text-xs">{slot.id}</span>
              <Badge tone={slot.known ? "INFO" : "WARN"}>
                {slot.known ? tr("待产出", "Pending") : tr("无法读取", "Unreadable")}
              </Badge>
            </div>
          ))}
        </div>

        {/* center: inline editor */}
        <div>
          {selected ? (
            <ArtifactEditor
              inline
              artifactId={selected}
              producer={cur?.producer ?? rosterSlots.find((s) => s.id === selected)?.producer ?? null}
            />
          ) : (
            <EmptyNote label={tr("从左侧选择一个对象进行编辑。", "Select an object on the left to edit it.")} />
          )}
        </div>
      </div>
    </div>
  );
}
