import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Card, Badge, Spinner, ErrorNote, DegradedNote, EmptyNote } from "@/components/ui";
import { Field, TextField, Button } from "@/components/controls";
import { tr } from "@/lib/i18n";

// Conversation phase summaries (sharding output) — mirrors cognition_panel.render_phase_summaries_html
// over PhaseManager.list_phases via GET /api/cognition/phases.
//
// Long conversations are auto/manually sharded; each shard yields a compressed
// summary. Optionally scope to one experiment_id (read-only; the query only runs
// on an explicit "Query" click so a stray keystroke never refetches).

export function PhaseSummaries() {
  const [draft, setDraft] = useState("");
  const [experimentId, setExperimentId] = useState("");

  const q = useQuery({
    queryKey: ["cognition", "phases", experimentId],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/cognition/phases", {
        params: { query: { experiment_id: experimentId.trim() || null } },
      });
      if (error) throw error;
      return data;
    },
  });

  const phases = q.data?.phases ?? [];

  return (
    <Card>
      <div className="mb-3 flex items-end gap-2">
        <div className="flex-1">
          <Field label={tr("实验 ID（可选）", "Experiment ID (optional)")} hint={tr("留空 = 全部实验的对话阶段", "Leave empty = conversation phases of all experiments")}>
            <TextField
              value={draft}
              onChange={setDraft}
              placeholder={tr("例如：exp-2026-06-...", "e.g. exp-2026-06-...")}
            />
          </Field>
        </div>
        <Button variant="primary" onClick={() => setExperimentId(draft)}>
          {tr("查询", "Query")}
        </Button>
        {experimentId && (
          <Button
            variant="ghost"
            onClick={() => {
              setDraft("");
              setExperimentId("");
            }}
          >
            {tr("清除筛选", "Clear filter")}
          </Button>
        )}
      </div>

      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} />}
      {q.data?.degraded && <DegradedNote what={tr("分片管理", "Phase sharding")} />}
      {q.data && !q.data.degraded && phases.length === 0 && (
        <EmptyNote label={tr("尚无对话阶段。长对话会自动/手动分片，每段产出压缩摘要。", "No conversation phases yet. Long conversations are sharded automatically or manually, and each shard yields a compressed summary.")} />
      )}

      {q.data && !q.data.degraded && phases.length > 0 && (
        <div className="space-y-1">
          <p className="px-1 text-xs text-mast-muted">
            {tr(`${q.data.count} 段`, `${q.data.count} ${q.data.count === 1 ? "phase" : "phases"}`)}
            {experimentId && (
              <>
                {" "}
                · {tr("实验", "Experiment")} <code>{experimentId}</code>
              </>
            )}
          </p>
          <div className="divide-y divide-mast-border overflow-hidden rounded border border-mast-border">
            {phases.map((p, i) => (
              <div key={`${p.phase_index ?? i}`} className="px-3 py-2 text-sm">
                <div className="flex items-center gap-2">
                  <span className="font-semibold tabular-nums">
                    #{p.phase_index ?? i}
                  </span>
                  <span>{p.title}</span>
                  {p.open && <Badge tone="AUTO">{tr("进行中", "In progress")}</Badge>}
                </div>
                <div className="mt-1 whitespace-pre-wrap text-mast-muted">
                  {p.summary}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}
