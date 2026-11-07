import { forwardRef, useImperativeHandle, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, useToast } from "@/components/controls";
import { DegradedNote } from "@/components/ui";
import { agentLabel } from "@/components/agents/registry";
import { interruptPanelView } from "@/lib/interruptPanel";
import { tr, getLang } from "@/lib/i18n";
import {
  InterruptCard,
  fromPollRow,
  type ResolveArgs,
} from "@/components/agents/InterruptCard";

// Human-in-the-loop surface of the private chat: pending interrupts polled from
// GET /api/agents/{id}/interrupts, resolved via
// POST /api/agents/{id}/interrupts/{event_id}/resolve.
//
// It covers two directions: the user approving a DANGEROUS skill an agent wants
// to run, and the user answering a question the agent asked (ask_user). The card
// itself is shared with the group-chat surface, so both behave identically.
//
// Why this is inline and not a modal: a modal (`fixed inset-0 bg-black/60
// backdrop-blur-sm`) darkens and blurs the whole UI, so at the very moment the
// operator must judge "approve this step?" the bias, current, Z, scan progress
// and alerts are hidden behind the approval dialog. The auto-popup existed for a
// real reason, though: the turn that raised the interrupt is blocked, and hiding
// it behind a badge would mean a silent conversation followed by an unnoticed
// timeout. So "cannot be missed" is kept and "covers the screen" is dropped: an
// inline card sits in the document flow and pushes content aside instead of
// covering it. The group-chat surface (RunTaskPanel) is already inline.
//
// When collapsed, a one-line "N items pending" summary stays: dismissible must
// not mean invisible, or the silent-timeout failure mode returns.

export function useInterrupts(agentId: string) {
  return useQuery({
    queryKey: ["chat", "interrupts", agentId],
    queryFn: async () => {
      const { api } = await import("@/api/client");
      const { data, error } = await api.GET("/api/agents/{agent_id}/interrupts", {
        params: { path: { agent_id: agentId } },
      });
      if (error) throw error;
      return data;
    },
    refetchInterval: 2500,
  });
}

/**
 * Submit a decision.
 *
 * Extracted as a hook because there are three outcomes, not two: success, the
 * backend deliberately rejecting it (answer_invalid / route_not_allowed: the
 * worker stays blocked until the user fixes and resubmits), and degraded without
 * effect. The second keeps the card interactive; a copy would eventually miss it.
 */
export function useResolveInterrupt(onDone?: () => void) {
  const qc = useQueryClient();
  const { toast, node } = useToast();

  const resolve = useMutation({
    mutationFn: async (vars: { agent_id: string; interrupt_id: string; args: ResolveArgs }) => {
      const { api } = await import("@/api/client");
      const { data: res, error } = await api.POST(
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
      return res;
    },
    onSuccess: (res, vars) => {
      const d = vars.args.decision;
      const label =
        d === "approve" ? tr("批准", "approved")
          : d === "reject" ? tr("拒绝", "rejected")
          : d === "edit" ? tr("编辑并批准", "edited and approved")
          : d === "answer" ? tr("提交回答", "answer submitted") : tr(`选择「${d}」`, `selected "${d}"`);
      if (res?.ok && res?.applied) {
        toast(tr(`已${label}`, `Done: ${label}`), "ok");
      } else if (res?.status === "answer_invalid" || res?.status === "route_not_allowed") {
        toast(
          tr(
            `未提交：${res?.detail ?? "回答无效"}，请修改后重试`,
            `Not submitted: ${res?.detail ?? "invalid answer"}. Please edit and retry`,
          ),
          "err",
        );
      } else {
        toast(
          tr(
            `未生效（${res?.status ?? "degraded"}）：${res?.detail ?? "后端降级或已处理"}`,
            `Not applied (${res?.status ?? "degraded"}): ${res?.detail ?? "backend degraded or already handled"}`,
          ),
          "err",
        );
      }
      qc.invalidateQueries({ queryKey: ["chat", "interrupts", vars.agent_id] });
      onDone?.();
    },
    // A failed submission must say so; silence is the one outcome this surface
    // must never have.
    onError: (e) => toast(String((e as Error)?.message ?? e), "err"),
  });

  return { resolve, toastNode: node };
}

export interface PendingInterruptsHandle {
  /** Scroll to the panel and expand it. Used by the badge button, the only anchor when the transcript is long. */
  reveal: () => void;
}

export const PendingInterrupts = forwardRef<
  PendingInterruptsHandle,
  {
    data: ReturnType<typeof useInterrupts>["data"];
    agentId: string;
  }
>(function PendingInterrupts({ data, agentId }, ref) {
  const [collapsed, setCollapsed] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const { resolve, toastNode } = useResolveInterrupt();

  useImperativeHandle(ref, () => ({
    reveal: () => {
      setCollapsed(false);
      // The worst case is "nothing happens"; never throw. An unattached ref or a
      // browser without smooth scrolling must not turn the click into a console error.
      try {
        boxRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
      } catch {
        /* Scrolling failed; the panel is already expanded, which is what matters. */
      }
    },
  }), []);

  const interrupts = data?.interrupts ?? [];
  // The display decision lives in lib/interruptPanel.ts: "collapsing hides only
  // the cards, the summary line always stays" is a property that cannot be
  // guarded when written inline in JSX.
  const view = interruptPanelView(data, collapsed);
  if (!view.render) return null;

  return (
    <div
      ref={boxRef}
      className="rounded-mast-card border border-mast-warn-border bg-mast-warn-bg p-3"
    >
      {toastNode}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-mast-warn">
          {view.headline}
          {view.count > 0 && <span className="ml-1.5 tabular-nums">（{view.count}）</span>}
        </span>
        <span className="text-xs text-mast-muted">
          {getLang() === "zh" ? (
            <>这一轮<b className="font-medium">停在这里</b>，处理完才会继续。</>
          ) : (
            <>This turn is <b className="font-medium">paused here</b> and continues once handled.</>
          )}
        </span>
        <div className="ml-auto">
          <Button variant="ghost" onClick={() => setCollapsed((c) => !c)}>
            {collapsed ? tr("展开", "Expand") : tr("收起", "Collapse")}
          </Button>
        </div>
      </div>

      {data?.degraded && <DegradedNote what={tr("中断队列", "interrupt queue")} />}

      {/* Collapsing hides only the cards. The summary line above always stays: a
          notice that can be dismissed entirely is no notice, and this turn is
          genuinely blocked. */}
      {view.showCards && (
        <div className="mt-3 flex flex-col gap-3">
          <p className="text-xs text-mast-muted">
            {tr(
              "危险技能需要你批准 / 拒绝 / 改参数后批准（改过的参数会由核心 SafetyGate 重新校验）；智能体的提问需要你作答后它才会继续。",
              "Dangerous skills need your approval, rejection, or approval with edited parameters (edited parameters are re-validated by the core SafetyGate); the agent continues after you answer its question.",
            )}
          </p>
          {interrupts.map((it) => {
            const agent = it.agent_id || agentId;
            return (
              <InterruptCard
                key={it.event_id}
                it={fromPollRow(it, agent)}
                busy={resolve.isPending && resolve.variables?.interrupt_id === it.event_id}
                agentLabel={agentLabel}
                compact
                onResolve={(args) =>
                  resolve.mutate({ agent_id: agent, interrupt_id: it.event_id, args })
                }
              />
            );
          })}
        </div>
      )}
    </div>
  );
});
