import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import { tr } from "@/lib/i18n";

// Model + thinking quick-display: a read-only strip showing the global default
// model and the IC agent's model + thinking level, with a link to the settings
// page where they are actually changed.

const AGENT_ID = "instrument_control";

const THINKING_LABEL: Record<string, string> = {
  none: tr("无", "None"),
  fixed: tr("固定", "Fixed"),
  off: tr("关闭", "Off"),
  low: tr("低", "Low"),
  medium: tr("中", "Medium"),
  high: tr("高", "High"),
  max: tr("最高", "Max"),
};

function Pill({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-md border border-mast-border bg-mast-panel/60 px-2 py-1 text-xs">
      <span className="text-mast-muted">{k}</span>
      <span className={mono ? "font-mono text-mast-text" : "text-mast-text"}>{v}</span>
    </span>
  );
}

export function ModelStrip() {
  const models = useQuery({
    queryKey: ["chat", "config-models"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/config/models");
      if (error) throw error;
      return data;
    },
    staleTime: 60_000,
  });

  const agentModels = useQuery({
    queryKey: ["chat", "agents-models"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/agents/models");
      if (error) throw error;
      return data;
    },
    refetchInterval: 15_000,
  });

  const globalAlias = models.data?.default_alias ?? "—";
  const ic = (agentModels.data?.agents ?? []).find((a) => a.agent_id === AGENT_ID);
  const icModel = ic?.model ?? globalAlias;
  const icThinking = ic?.thinking ? (THINKING_LABEL[ic.thinking] ?? ic.thinking) : "—";

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Pill k={tr("全局模型", "Global model")} v={globalAlias} mono />
      <Pill k={tr("IC 模型", "IC model")} v={icModel} mono />
      <Pill k={tr("思考", "Thinking")} v={icThinking} />
      {agentModels.data?.degraded && (
        <span className="text-xs text-mast-warn">{tr("（模型信息降级）", "(model info degraded)")}</span>
      )}
      <Link
        to="/settings/general"
        className="ml-auto rounded-md border border-mast-border px-2 py-1 text-xs text-mast-accent hover:border-mast-accent"
      >
        {tr("在设置中修改 →", "Change in settings →")}
      </Link>
    </div>
  );
}
