import { Badge } from "../ui";
import { tr } from "@/lib/i18n";

// Read-only model-capability table (Settings -> Models -> model_info): every
// MODEL_PRESETS row plus derived limits.

const THINKING_BADGE: Record<string, { label: string; tone: string }> = {
  none: { label: tr("无", "None"), tone: "WARN" },
  fixed: { label: tr("固定", "Fixed"), tone: "INFO" },
  tunable: { label: tr("可调", "Tunable"), tone: "AUTO" },
};

export type ModelRow = {
  alias: string;
  provider: string;
  model_id: string;
  description: string;
  thinking_mode: string;
  output_limit: number;
  input_context: number;
  default_max_tokens: number;
  is_default: boolean;
};

export function ModelCapabilityTable({ models }: { models: ModelRow[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-mast-border">
      <table className="w-full text-sm">
        <thead className="bg-mast-bg/60 text-mast-muted">
          <tr>
            <th className="px-3 py-2 text-left">{tr("别名", "Alias")}</th>
            <th className="px-3 py-2 text-left">Provider</th>
            <th className="px-3 py-2 text-left">{tr("模型 ID", "Model ID")}</th>
            <th className="px-3 py-2 text-left">{tr("思考", "Thinking")}</th>
            <th className="px-3 py-2 text-right">{tr("输出上限", "Output limit")}</th>
            <th className="px-3 py-2 text-right">{tr("输入上下文", "Input context")}</th>
            <th className="px-3 py-2 text-left">{tr("说明", "Description")}</th>
          </tr>
        </thead>
        <tbody>
          {models.map((m) => {
            const tb = THINKING_BADGE[m.thinking_mode] ?? { label: m.thinking_mode, tone: "default" };
            return (
              <tr key={m.alias} className="border-t border-mast-border align-top">
                <td className="px-3 py-2 font-medium">
                  {m.alias}
                  {m.is_default && (
                    <span className="ml-2 rounded bg-mast-accent/20 px-1.5 py-0.5 text-xs text-mast-accent">
                      {tr("默认", "Default")}
                    </span>
                  )}
                </td>
                <td className="px-3 py-2 text-mast-muted">{m.provider}</td>
                <td className="px-3 py-2 font-mono text-xs text-mast-muted">{m.model_id}</td>
                <td className="px-3 py-2">
                  <Badge tone={tb.tone}>{tb.label}</Badge>
                </td>
                <td className="px-3 py-2 text-right tabular-nums">{m.output_limit.toLocaleString()}</td>
                <td className="px-3 py-2 text-right tabular-nums">{m.input_context.toLocaleString()}</td>
                <td className="px-3 py-2 text-mast-muted">{m.description}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
