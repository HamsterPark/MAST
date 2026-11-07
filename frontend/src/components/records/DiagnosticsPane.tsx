import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Card, Badge, Spinner, ErrorNote, DegradedNote, EmptyNote } from "@/components/ui";
import { tr } from "@/lib/i18n";

// Diagnostics: the refusal ledger, i.e. why nothing happened.
//
// Successful actions are recorded elsewhere; this ledger records what was
// refused or skipped (precondition, safety gate, mode gate, abort, HITL reject,
// step skip/fail/abort, stalls), so "the action did nothing" can be traced to the
// layer that stopped it. It is a window onto core/diagnostics.py. Empty means
// nothing has been refused, not that the panel is broken.

const KIND_STYLE: Record<
  string,
  { label: string; tone: React.ComponentProps<typeof Badge>["tone"] }
> = {
  precondition_block: { label: tr("前置条件", "Precondition"), tone: "WARN" },
  safety_block: { label: tr("安全门", "Safety gate"), tone: "DANGEROUS" },
  mode_block: { label: tr("模式门", "Mode gate"), tone: "WARN" },
  abort_block: { label: tr("中止拦截", "Abort block"), tone: "DANGEROUS" },
  hitl_reject: { label: tr("人工拒绝", "Operator rejected"), tone: "WARN" },
  step_skip: { label: tr("跳过", "Skipped"), tone: "INFO" },
  step_fail: { label: tr("步骤失败", "Step failed"), tone: "DANGEROUS" },
  step_abort: { label: tr("步骤中止", "Step aborted"), tone: "DANGEROUS" },
  stall: { label: tr("空转", "Stalled"), tone: "DANGEROUS" },
  note: { label: tr("记录", "Note"), tone: "INFO" },
  // Events that would previously have interrupted work and now only notify
  // (critical buffer events no longer prompt, DANGEROUS skills no longer wait for
  // approval). INFO rather than WARN: the entry records that the action was
  // allowed through, and an alarm colour would make the panel look like a pile of
  // problems.
  notice_only: { label: tr("只通知", "Notify only"), tone: "INFO" },
};

const GROUPS = [
  { id: "", label: tr("全部", "All") },
  { id: "refusals", label: tr("拒绝（前置/安全/模式/中止）", "Refusals (precondition/safety/mode/abort)") },
  { id: "steps", label: tr("步骤（跳过/失败/中止）", "Steps (skipped/failed/aborted)") },
  { id: "stall", label: tr("空转", "Stalled") },
  // "How often would this have interrupted me" should be one click, not a log search.
  { id: "notices", label: tr("只通知（本来会打断）", "Notify only (would have interrupted)") },
] as const;

function fmtTs(t: number): string {
  const d = new Date(t * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function DiagnosticsPane() {
  const [group, setGroup] = useState<string>("");
  const [subject, setSubject] = useState("");

  const q = useQuery({
    queryKey: ["diagnostics", group, subject],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/diagnostics", {
        params: { query: { group, subject, limit: 200 } },
      });
      if (error) throw error;
      return data;
    },
    refetchInterval: 5000,
  });

  if (q.isPending) return <Spinner />;
  if (q.isError) return <ErrorNote error={q.error} />;
  const data = q.data!;
  if (data.degraded) return <DegradedNote what={tr("诊断台账", "diagnostics ledger")} />;

  const top = data.summary?.top_refusals ?? [];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-3">
        <h2 className="text-lg font-semibold tracking-tight">{tr("诊断", "Diagnostics")}</h2>
        <span className="text-xs text-mast-muted">
          {tr("每一次", "Every action that was ")}
          <strong className="text-mast-text">{tr("被拒绝", "refused")}</strong>
          {tr("或", " or ")}
          <strong className="text-mast-text">{tr("被跳过", "skipped")}</strong>
          {tr(
            "的动作都记在这里——「为什么什么都没发生」的答案。",
            " is recorded here: the answer to \"why did nothing happen\".",
          )}
        </span>
      </div>

      {/* Shape before detail: one subject dominating the counts is the stall itself. */}
      {top.length > 0 && (
        <Card className="space-y-2">
          <div className="text-xs font-medium text-mast-muted">{tr("最常被拒绝的动作", "Most frequently refused actions")}</div>
          <div className="flex flex-wrap gap-2">
            {top.slice(0, 6).map((r) => {
              const what = String(r.what ?? "");
              const [kind, subj] = what.split(":");
              const st = KIND_STYLE[kind ?? ""] ?? { label: kind ?? "?", tone: "INFO" as const };
              return (
                <button
                  key={what}
                  type="button"
                  onClick={() => setSubject(subj ?? "")}
                  className="flex items-center gap-1.5 rounded border border-mast-border px-2 py-1 text-xs hover:border-mast-accent"
                >
                  <Badge tone={st.tone}>{st.label}</Badge>
                  <span className="font-mono text-mast-text">{subj}</span>
                  <span className="font-mono tabular-nums text-mast-muted">×{String(r.count ?? "")}</span>
                </button>
              );
            })}
          </div>
        </Card>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {GROUPS.map((g) => (
          <button
            key={g.id}
            type="button"
            onClick={() => setGroup(g.id)}
            className={
              "rounded border px-2.5 py-1 text-xs " +
              (group === g.id
                ? "border-mast-accent bg-mast-accent/10 text-mast-accent"
                : "border-mast-border text-mast-muted hover:text-mast-text")
            }
          >
            {g.label}
          </button>
        ))}
        <input
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          placeholder={tr("按技能 / 步骤名过滤", "Filter by skill / step name")}
          className="ml-auto min-w-0 flex-1 rounded border border-mast-border bg-mast-bg px-3 py-1 text-xs text-mast-text placeholder:text-mast-muted focus:border-mast-accent focus:outline-none sm:max-w-xs"
        />
      </div>

      {data.count === 0 ? (
        <EmptyNote label={tr("尚无拒绝记录——没有任何动作被拦下或跳过。", "No refusals recorded: no action has been blocked or skipped.")} />
      ) : (
        <div className="space-y-1.5">
          {(data.entries ?? []).map((e) => {
            const st = KIND_STYLE[e.kind] ?? { label: e.kind, tone: "INFO" as const };
            const extra = Object.entries(e.fields ?? {}).filter(
              ([, v]) => v !== null && v !== "" && !(Array.isArray(v) && v.length === 0),
            );
            return (
              <Card key={e.seq} className="space-y-1 py-2">
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="font-mono tabular-nums text-mast-muted">{fmtTs(e.t)}</span>
                  <Badge tone={st.tone}>{st.label}</Badge>
                  <span className="font-mono text-sm text-mast-text">{e.subject}</span>
                  {e.run_id && (
                    <span className="ml-auto font-mono text-[11px] text-mast-muted">
                      run {e.run_id}
                    </span>
                  )}
                </div>
                <p className="whitespace-pre-wrap text-sm text-mast-text">{e.reason}</p>
                {extra.length > 0 && (
                  <div className="flex flex-wrap gap-x-4 gap-y-0.5 font-mono text-[11px] text-mast-muted">
                    {extra.map(([k, v]) => (
                      <span key={k}>
                        {k}={typeof v === "object" ? JSON.stringify(v) : String(v)}
                      </span>
                    ))}
                  </div>
                )}
              </Card>
            );
          })}
        </div>
      )}

      {data.summary?.log_path && (
        <p className="text-[11px] text-mast-muted">
          {tr("完整历史（跨重启）：", "Full history (across restarts):")}
          <code className="ml-1 select-all font-mono text-mast-text">
            {data.summary.log_path}
          </code>
        </p>
      )}
    </div>
  );
}
