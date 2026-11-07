import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { Card, DegradedNote, EmptyNote, ErrorNote, Section, Spinner } from "../ui";
import { tr } from "../../lib/i18n";
import { Field, JsonBlock, StatusPill, fmtTime } from "./shared";

type ActionDetail = components["schemas"]["ActionDetail"];

function useActionDetail(id: string) {
  return useQuery({
    queryKey: ["records", "action", id],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/records/actions/{action_id}", {
        params: { path: { action_id: id } },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function ActionDetailPane({
  actionId,
  onBack,
}: {
  actionId: string;
  onBack: () => void;
}) {
  const q = useActionDetail(actionId);

  return (
    <div className="space-y-6">
      <button onClick={onBack} className="text-sm text-mast-accent hover:underline">
        {tr("← 返回", "← Back")}
      </button>

      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} />}
      {q.data && <Body data={q.data} />}
    </div>
  );
}

function Body({ data }: { data: ActionDetail }) {
  if (data.degraded) return <DegradedNote what={tr("动作详情", "action details")} />;
  if (data.found === false) return <EmptyNote label={tr(`未找到动作 ${data.id}`, `Action ${data.id} not found`)} />;

  return (
    <Section title={`${tr("动作", "Action")} ${data.action_type || data.skill_name || data.id}`}>
      <Card className="space-y-1">
        <Field label="ID">{data.id}</Field>
        <Field label={tr("实验", "Experiment")}>{data.experiment_id ?? "—"}</Field>
        <Field label={tr("样品", "Sample")}>{data.sample_id ?? "—"}</Field>
        <Field label={tr("父动作", "Parent action")}>{data.parent_action_id ?? "—"}</Field>
        <Field label="Agent">{data.agent_id ?? "—"}</Field>
        <Field label={tr("动作类型", "Action type")}>{data.action_type ?? "—"}</Field>
        <Field label={tr("技能", "Skill")}>{data.skill_name ?? "—"}</Field>
        <Field label={tr("状态", "Status")}>
          <StatusPill status={data.status} />
        </Field>
        <Field label="HLC">{data.hlc ?? "—"}</Field>
        <Field label={tr("时间", "Time")}>{fmtTime(data.timestamp)}</Field>
        <Field label={tr("耗时(ms)", "Duration (ms)")}>{data.duration_ms ?? "—"}</Field>
        <Field label={tr("耗时(s)", "Duration (s)")}>{data.duration_s ?? "—"}</Field>
        {data.error && (
          <Field label={tr("错误", "Error")}>
            <span className="text-mast-danger">{data.error}</span>
          </Field>
        )}
        <Field label={tr("参数", "Parameters")}>
          <JsonBlock value={data.params} />
        </Field>
        <Field label={tr("状态变更", "State change")}>
          <JsonBlock value={data.state_delta} />
        </Field>
      </Card>
    </Section>
  );
}
