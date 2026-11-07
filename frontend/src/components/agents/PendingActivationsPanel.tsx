import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Badge, Card, DegradedNote, EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { Button, Toggle, useToast } from "@/components/controls";
import { agentLabel } from "@/components/agents/registry";
import { tr } from "@/lib/i18n";
import { settingsWriteProblem } from "@/lib/settingsWrite";

// ════════════════════════════════════════════════════════════════════════════
// Pending activations: agents parked until their inputs exist.
//
// This panel is required, not optional. The scheduler may decline to dispatch an
// agent whose upstream products do not exist yet (reviewing a manuscript nobody
// has written can only produce an invented review). The main risk of that design
// is a silent dead end: an agent waiting forever while the system looks idle and
// healthy, indistinguishable from "nothing happened" or "it hung".
//
// So the backend and this panel share three properties: a parked agent is
// VISIBLE; its wait has a DEADLINE; when the deadline passes it surfaces to a
// human instead of resuming silently or vanishing. An expired row keeps
// demanding attention until someone acknowledges it; a one-shot toast is not a
// delivery when nobody is watching, which is the normal case for a mechanism
// meant to work while the operator is away.
//
//   GET  /api/agents/pending-activations                  — list (polled)
//   POST /api/agents/pending-activations/{id}/acknowledge — clear attention
// ════════════════════════════════════════════════════════════════════════════

type Park = {
  park_id: string;
  agent: string;
  experiment_id: string;
  waiting_for: string[];
  waiting_for_labels: string[];
  blocked_by: string;
  reason: string;
  instruction: string;
  hard: boolean;
  status: string;
  created_at: number;
  deadline_at: number;
  seconds_left: number;
  waited_human: string;
  declines: number;
  woken_run_id: string;
  needs_attention: boolean;
  campaign_id?: string;
  /** Latest goal-criterion evaluation. When `verdict==="unknown"`, `reason` says
   *  what could not be read; "cannot judge" must be visible, or it looks the same
   *  as "all fine". */
  goal_check?: { verdict?: string; reason?: string; checked_at?: number };
};

// These two tables must cover the full `STATUSES` set of `mast/core/park_board.py`
// (pinned by a Python parity test). A missing entry does not raise; the row shows
// a bare status code, and `done_by_goal` does not tell the user it is a good outcome.
const STATUS_TONE: Record<string, string> = {
  waiting: "INFO",
  woken: "AUTO",
  done: "AUTO",
  done_by_goal: "AUTO",
  expired: "DANGEROUS",
  cancelled: "INFO",
};
const STATUS_LABEL: Record<string, string> = {
  waiting: tr("等待中", "Waiting"),
  woken: tr("已唤醒", "Woken"),
  done: tr("已完成", "Done"),
  // Distinct from "done": that one means it woke, finished and its products came
  // back; this one means it never had to wake because the goal it serves is met.
  done_by_goal: tr("目标已达成", "Goal reached"),
  expired: tr("已超时", "Timed out"),
  cancelled: tr("已撤销", "Cancelled"),
};

function countdown(secondsLeft: number): string {
  if (!Number.isFinite(secondsLeft)) return "—";
  if (secondsLeft <= 0) return tr("已超时", "Timed out");
  const s = Math.round(secondsLeft);
  if (s < 3600) return tr(`还有 ${Math.max(1, Math.floor(s / 60))} 分钟`, `${Math.max(1, Math.floor(s / 60))} min left`);
  if (s < 172800) return tr(`还有 ${Math.floor(s / 3600)} 小时`, `${Math.floor(s / 3600)} h left`);
  return tr(`还有 ${Math.floor(s / 86400)} 天`, `${Math.floor(s / 86400)} d left`);
}

export function PendingActivationsPanel() {
  const qc = useQueryClient();
  const { toast, node } = useToast();

  const parksQuery = useQuery({
    queryKey: ["agents", "pending-activations"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/agents/pending-activations");
      if (error) throw error;
      return data;
    },
    // Slow poll: a park's timescale is hours. The backend sweeps deadlines on
    // read, so this poll is ALSO what makes a timeout fire while the system is
    // otherwise idle — but 20 s is ample for an hours-long wait.
    refetchInterval: 20000,
  });

  const parks = (parksQuery.data?.parks ?? []) as Park[];
  const attention = useMemo(() => parks.filter((p) => p.needs_attention), [parks]);

  // The gate that decides whether parking happens at all. Default OFF, and
  // surfaced HERE rather than buried in a settings page so the switch sits next
  // to the consequence of flipping it.
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/settings");
      if (error) throw error;
      return data;
    },
  });
  const gateOn = settings.data?.orchestrator_activation_gating ?? false;
  const saveGate = useMutation({
    mutationFn: async (v: boolean) => {
      const { data, error } = await api.POST("/api/settings", {
        body: { orchestrator_activation_gating: v },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d, v) => {
      const problem = settingsWriteProblem(d);
      if (problem) { toast(problem, "err"); return; }
      toast(
        v
          ? tr("已开启：缺上游资料的智能体会被搁置，并显示在这里", "Enabled: agents missing upstream material will be parked and listed here")
          : tr("已关闭：编排器照旧派发，不再搁置任何智能体", "Disabled: the orchestrator dispatches as usual and parks no agents"),
        "ok",
      );
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e) => toast(String((e as Error)?.message ?? e), "err"),
  });

  const ack = useMutation({
    mutationFn: async (parkId: string) => {
      const { data, error } = await api.POST(
        "/api/agents/pending-activations/{park_id}/acknowledge",
        { params: { path: { park_id: parkId } } },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      toast(tr("已确认（记录保留，不会自动继续）", "Acknowledged (the record is kept; it will not resume automatically)"), "ok");
      qc.invalidateQueries({ queryKey: ["agents", "pending-activations"] });
    },
    onError: (e) => toast(String((e as Error)?.message ?? e), "err"),
  });

  return (
    <div className="space-y-4">
      {node}

      {/* what this is + the gate that produces it */}
      <Card className="space-y-1 px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{tr("待唤醒 · 环境驱动搁置", "Pending activation · input-driven parking")}</span>
          {attention.length > 0 ? (
            <Badge tone="DANGEROUS">{tr(`${attention.length} 项超时待处理`, `${attention.length} timed out, need attention`)}</Badge>
          ) : (
            <Badge tone={parks.length > 0 ? "INFO" : "AUTO"}>
              {parks.length > 0 ? tr(`${parks.length} 个在等`, `${parks.length} waiting`) : tr("无搁置", "None parked")}
            </Badge>
          )}
        </div>
        <p className="text-xs text-mast-muted">
          {tr(
            "编排器不会把一个「缺上游资料」的智能体派去编内容 —— 例如还没有草稿就让评审去评。这类智能体会被记在这里等资料；资料到位后会重新问它要不要开工，它也可以选择继续等。每个等待都有上限，超时会浮到这里要你处理，",
            "The orchestrator will not send an agent that is missing upstream material off to make content up, for example asking for a review when no draft exists. Such agents are recorded here to wait; once the material arrives they are asked again whether to start, and may choose to keep waiting. Every wait has a limit, and a timeout surfaces here for you to handle, ",
          )}
          <strong>{tr("不会自动继续、也不会悄悄消失", "it neither resumes automatically nor disappears silently")}</strong>
          {tr("。", ".")}
        </p>
        <div className="flex items-center justify-between gap-3 border-t border-mast-border pt-2">
          <div className="min-w-0">
            <div className="text-xs font-medium text-mast-text">{tr("启用环境驱动搁置（实验性）", "Enable input-driven parking (experimental)")}</div>
            <div className="text-[11px] text-mast-muted">
              {tr(
                "默认关闭 —— 关着时编排器的调度与以前完全一样。开启后才会出现搁置。想强制某个智能体现在就干活？在对话里 @它 直接点名即可（定向永不被搁置）。",
                "Off by default; when off, orchestrator scheduling is unchanged and nothing is parked. To force an agent to work now, address it by name in the conversation (targeted requests are never parked).",
              )}
            </div>
          </div>
          <Toggle
            checked={!!gateOn}
            onChange={(v) => saveGate.mutate(v)}
            label={gateOn ? tr("已开启", "On") : tr("已关闭", "Off")}
          />
        </div>
      </Card>

      {parksQuery.isLoading ? <Spinner /> : null}
      {parksQuery.error ? <ErrorNote error={parksQuery.error} label={tr("读不到搁置记录", "Cannot read parked records")} /> : null}
      {parksQuery.data?.degraded ? <DegradedNote what={tr("待唤醒列表", "Pending activation list")} /> : null}

      {!parksQuery.isLoading && parks.length === 0 && !parksQuery.data?.degraded ? (
        <EmptyNote label={tr("目前没有被搁置的智能体。", "No agents are currently parked.")} />
      ) : null}

      {parks.map((p) => (
        <Card
          key={p.park_id}
          className={
            p.needs_attention
              ? "space-y-1.5 border-mast-danger-border px-4 py-3"
              : "space-y-1.5 px-4 py-3"
          }
        >
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{agentLabel(p.agent)}</span>
            <Badge tone={STATUS_TONE[p.status] ?? "INFO"}>
              {STATUS_LABEL[p.status] ?? p.status}
            </Badge>
            {p.hard ? <Badge tone="WARN">{tr("硬依赖缺失", "Hard dependency missing")}</Badge> : null}
          </div>

          <div className="text-xs text-mast-muted">
            {tr(`等：${p.waiting_for_labels.join("、") || "—"}`, `Waiting for: ${p.waiting_for_labels.join(", ") || "—"}`)}
            {p.blocked_by ? tr(`（由${agentLabel(p.blocked_by)}产出）`, ` (produced by ${agentLabel(p.blocked_by)})`) : ""}
          </div>

          <div className="text-[11px] text-mast-faint">
            {tr(`已等 ${p.waited_human || "—"}`, `Waited ${p.waited_human || "—"}`)} · {countdown(p.seconds_left)}
            {p.declines > 0 ? tr(` · 已拒绝 ${p.declines} 次`, ` · declined ${p.declines}×`) : ""}
            {p.woken_run_id ? tr(` · 运行 ${p.woken_run_id.slice(0, 8)}`, ` · run ${p.woken_run_id.slice(0, 8)}`) : ""}
          </div>

          {p.reason ? (
            <div className="text-[11px] text-mast-faint">{tr(`理由：${p.reason}`, `Reason: ${p.reason}`)}</div>
          ) : null}

          {/* Goal criterion. The backend evaluates it every tick and persists only
              when the verdict changes. "unknown" must be shown: being unable to
              judge differs from "not yet", and a hidden unknown looks exactly like
              "all fine". */}
          {p.goal_check?.verdict ? (
            <div
              className={
                "text-[11px] " +
                (p.goal_check.verdict === "unknown"
                  ? "text-mast-warn"
                  : "text-mast-faint")
              }
            >
              {tr("目标判据：", "Goal criterion: ")}
              {p.goal_check.verdict === "done"
                ? tr("已达成", "met")
                : p.goal_check.verdict === "unknown"
                  ? tr("读不到", "cannot read")
                  : tr("还没满足", "not yet met")}
              {p.goal_check.reason ? ` (${p.goal_check.reason})` : ""}
            </div>
          ) : null}

          {p.needs_attention ? (
            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-mast-danger-border pt-2">
              <span className="text-[11px] text-mast-danger">
                {tr("等待已超时。不会自动继续 —— 需要你决定：补上缺的资料，或在对话里 @它 直接让它开工。", "The wait has timed out. It will not resume automatically; you decide: supply the missing material, or address the agent by name in the conversation to make it start.")}
              </span>
              <Button variant="default" onClick={() => ack.mutate(p.park_id)} loading={ack.isPending}>
                {tr("我知道了", "Got it")}
              </Button>
            </div>
          ) : null}
        </Card>
      ))}
    </div>
  );
}
