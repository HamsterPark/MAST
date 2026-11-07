import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Badge, DegradedNote, EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { Modal, useToast } from "@/components/controls";
import { tr } from "@/lib/i18n";
import { agentLabel } from "./registry";
import { InterruptCard, fromPollRow, type ResolveArgs } from "./InterruptCard";

// HITL interrupts viewer + resolver: the DANGEROUS / escalate modal, with operator
// approval of DANGEROUS ops.
//
// Reads GET /api/agents/{id}/interrupts (poll) and resolves via
// POST /api/agents/{id}/interrupts/{event_id}/resolve. The card body is the
// SHARED InterruptCard, so a new interrupt kind (an agent's ask_user question,
// a workflow route choice) renders here without a second implementation.
//
// LIVE-only: when the resolve response (or the list) reports degraded=true we
// surface a small note and never freeze — the buttons stay clickable and report
// an honest status.

export function InterruptsModal({
  open,
  agentId,
  onClose,
}: {
  open: boolean;
  agentId: string | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const { toast, node } = useToast();

  const q = useQuery({
    queryKey: ["agents", "interrupts", agentId],
    enabled: open && !!agentId,
    refetchInterval: open ? 4000 : false,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/agents/{agent_id}/interrupts", {
        params: { path: { agent_id: agentId as string } },
      });
      if (error) throw error;
      return data;
    },
  });

  const resolve = useMutation({
    mutationFn: async (vars: { agent_id: string; interrupt_id: string; args: ResolveArgs }) => {
      const { data, error } = await api.POST(
        "/api/agents/{agent_id}/interrupts/{interrupt_id}/resolve",
        {
          params: { path: { agent_id: vars.agent_id, interrupt_id: vars.interrupt_id } },
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
    onSuccess: (data, vars) => {
      const d = vars.args.decision;
      const label =
        d === "approve" ? tr("批准", "approved")
          : d === "reject" ? tr("拒绝", "rejected")
          : d === "edit" ? tr("编辑并批准", "edited and approved")
          : d === "answer" ? tr("回答", "answered") : tr(`选择「${d}」`, `chose "${d}"`);
      if (data?.ok && data?.applied) {
        toast(tr(`已${label}中断 ${vars.interrupt_id}`, `Interrupt ${vars.interrupt_id} ${label}`), "ok");
      } else if (data?.status === "answer_invalid" || data?.status === "route_not_allowed") {
        // The backend left the worker blocked on purpose so this can be fixed —
        // do NOT let it read as "resolved".
        toast(tr(`未提交：${data?.detail ?? "回答无效"}，请修改后重试`, `Not submitted: ${data?.detail ?? "invalid answer"}. Please edit and retry`), "err");
      } else {
        toast(tr(`中断未生效（${data?.status ?? "degraded"}）：${data?.detail ?? "后端降级或中断已处理"}`, `Interrupt not applied (${data?.status ?? "degraded"}): ${data?.detail ?? "backend degraded or interrupt already handled"}`), "err");
      }
      qc.invalidateQueries({ queryKey: ["agents", "interrupts", vars.agent_id] });
      qc.invalidateQueries({ queryKey: ["agents", "snapshot"] });
    },
    onError: (e) => toast(String((e as Error)?.message ?? e), "err"),
  });

  const interrupts = q.data?.interrupts ?? [];

  return (
    <Modal open={open} onClose={onClose} title={tr(`人机交互中断 · ${agentId ? agentLabel(agentId) : ""}`, `Human-in-the-loop interrupts · ${agentId ? agentLabel(agentId) : ""}`)} wide>
      {node}
      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} />}
      {q.data && q.data.degraded && <DegradedNote what={tr("中断队列", "Interrupt queue")} />}
      {q.data && !q.data.degraded && (
        <div className="mb-3 flex items-center gap-3 text-sm text-mast-muted">
          <span>{tr(`共 ${q.data.count} 个待处理中断`, `${q.data.count} pending interrupts`)}</span>
          {q.data.interrupt_gating && <Badge tone="WARN">{tr("门控开启", "Gating on")}</Badge>}
        </div>
      )}
      {q.data && !q.data.degraded && interrupts.length === 0 && (
        <EmptyNote label={tr("当前无待处理中断。", "No pending interrupts.")} />
      )}
      <div className="space-y-3">
        {interrupts.map((it) => {
          const agent = it.agent_id || (agentId as string);
          return (
            <InterruptCard
              key={it.event_id}
              it={fromPollRow(it, agent)}
              busy={resolve.isPending && resolve.variables?.interrupt_id === it.event_id}
              agentLabel={agentLabel}
              onResolve={(args) =>
                resolve.mutate({ agent_id: agent, interrupt_id: it.event_id, args })
              }
            />
          );
        })}
      </div>
    </Modal>
  );
}
