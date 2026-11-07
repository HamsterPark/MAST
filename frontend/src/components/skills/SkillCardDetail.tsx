import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import {
  Card,
  Badge,
  Spinner,
  ErrorNote,
  DegradedNote,
  EmptyNote,
} from "../ui";
import { getLang, tr } from "../../lib/i18n";

const SAFETY_TONE: Record<string, string> = {
  AUTO: "AUTO",
  INFO: "INFO",
  WARN: "WARN",
  DANGEROUS: "DANGEROUS",
  auto: "AUTO",
  confirm: "WARN",
  dangerous: "DANGEROUS",
};

/** Full skill card detail, fetched on demand from GET /api/skills/{name}. */
export function SkillCardDetail({ name }: { name: string }) {
  const q = useQuery({
    queryKey: ["skill", name],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/skills/{name}", {
        params: { path: { name } },
      });
      if (error) throw error;
      return data;
    },
  });

  if (q.isPending) return <Card><Spinner /></Card>;
  if (q.isError) return <Card><ErrorNote error={q.error} /></Card>;
  const card = q.data;
  if (!card) return <Card><EmptyNote /></Card>;
  if (card.degraded) return <DegradedNote what={tr("技能详情", "Skill details")} />;
  if (!card.found) {
    return (
      <Card>
        <EmptyNote label={tr(`未找到技能：${name}`, `Skill not found: ${name}`)} />
      </Card>
    );
  }

  return (
    <Card className="space-y-4">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-base font-semibold text-mast-text">{card.name}</h3>
          {getLang() === "zh" && card.zh && <span className="text-sm text-mast-muted">{card.zh}</span>}
          <Badge tone={SAFETY_TONE[card.safety] ?? "default"}>{card.safety || "—"}</Badge>
          <Badge tone="INFO">L{card.level}</Badge>
          {card.version && <Badge>v{card.version}</Badge>}
        </div>
        <div className="mt-1 flex flex-wrap gap-3 text-xs text-mast-muted">
          <span>{tr("领域：", "Domain: ")}{card.domain}</span>
          <span>{tr("来源：", "Source: ")}{getLang() === "zh" ? card.source_zh || card.source : card.source || card.source_zh}</span>
          {card.category && <span>{tr("分类：", "Category: ")}{card.category}</span>}
          {card.estimated_duration_s != null && (
            <span>{tr("预计耗时：", "Estimated duration: ")}{card.estimated_duration_s}s</span>
          )}
          {card.rollback_skill && <span>{tr("回滚：", "Rollback: ")}{card.rollback_skill}</span>}
        </div>
      </div>

      {(card.description_zh || card.description) && (
        <p className="text-sm text-mast-text/90">
          {getLang() === "zh"
            ? card.description_zh || card.description
            : card.description || card.description_zh}
        </p>
      )}

      {!!card.tags?.length && (
        <div className="flex flex-wrap gap-1.5">
          {card.tags.map((t) => (
            <span
              key={t}
              className="rounded bg-mast-bg px-1.5 py-0.5 text-xs text-mast-muted"
            >
              {t}
            </span>
          ))}
        </div>
      )}

      {!!card.parameters?.length && (
        <div>
          <h4 className="mb-2 text-sm font-medium text-mast-text">{tr("参数", "Parameters")}</h4>
          <div className="overflow-auto rounded border border-mast-border">
            <table className="w-full text-xs">
              <thead className="bg-mast-bg text-mast-muted">
                <tr>
                  <th className="px-2 py-1.5 text-left">{tr("名称", "Name")}</th>
                  <th className="px-2 py-1.5 text-left">{tr("类型", "Type")}</th>
                  <th className="px-2 py-1.5 text-left">{tr("单位", "Unit")}</th>
                  <th className="px-2 py-1.5 text-left">{tr("必填", "Required")}</th>
                  <th className="px-2 py-1.5 text-left">{tr("默认", "Default")}</th>
                  <th className="px-2 py-1.5 text-left">{tr("范围", "Range")}</th>
                  <th className="px-2 py-1.5 text-left">{tr("说明", "Description")}</th>
                </tr>
              </thead>
              <tbody>
                {card.parameters.map((p) => (
                  <tr key={p.name} className="border-t border-mast-border">
                    <td className="px-2 py-1.5 font-medium">{p.name}</td>
                    <td className="px-2 py-1.5 text-mast-muted">{p.type}</td>
                    <td className="px-2 py-1.5 text-mast-muted">{p.unit ?? "—"}</td>
                    <td className="px-2 py-1.5">{p.required ? tr("是", "Yes") : tr("否", "No")}</td>
                    <td className="px-2 py-1.5 text-mast-muted">
                      {p.default == null ? "—" : String(p.default)}
                    </td>
                    <td className="px-2 py-1.5 text-mast-muted">
                      {p.allowed_values?.length
                        ? p.allowed_values.map((v) => String(v)).join(" / ")
                        : p.min != null || p.max != null
                          ? `${p.min ?? "−∞"} ~ ${p.max ?? "+∞"}`
                          : "—"}
                    </td>
                    <td className="px-2 py-1.5 text-mast-muted">{p.description || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2">
        {!!card.preconditions?.length && (
          <div>
            <h4 className="mb-1 text-sm font-medium text-mast-text">{tr("前置条件", "Preconditions")}</h4>
            <ul className="list-disc space-y-0.5 pl-5 text-xs text-mast-muted">
              {card.preconditions.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          </div>
        )}
        {!!card.postconditions?.length && (
          <div>
            <h4 className="mb-1 text-sm font-medium text-mast-text">{tr("后置条件", "Postconditions")}</h4>
            <ul className="list-disc space-y-0.5 pl-5 text-xs text-mast-muted">
              {card.postconditions.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {!!card.outputs?.length && (
        <div>
          <h4 className="mb-1 text-sm font-medium text-mast-text">{tr("输出", "Outputs")}</h4>
          <pre className="overflow-auto rounded border border-mast-border bg-mast-bg p-2 text-xs text-mast-muted">
            {JSON.stringify(card.outputs, null, 2)}
          </pre>
        </div>
      )}
    </Card>
  );
}
