import { Badge, Card } from "@/components/ui";
import { tr } from "@/lib/i18n";

/** One domain-agent card: human label + monogram, effective model alias, and an
 *  honest effective-thinking badge. Pure presentational; data comes from the
 *  /api/agents/models seam. */

export type AgentMeta = {
  label: string;
  monogram: string;
  blurb: string;
};

// Canonical roster (7 agents) — mirrors orchestrator + the inspector roster.
export const AGENT_META: Record<string, AgentMeta> = {
  instrument_control: { label: tr("仪器控制", "Instrument control"), monogram: "IC", blurb: tr("Nanonis 硬件技能执行", "Nanonis hardware skill execution") },
  literature: { label: tr("文献调研", "Literature"), monogram: "LIT", blurb: tr("检索 / 全文 / 知识库", "Search / full text / knowledge base") },
  experiment_design: { label: tr("实验设计", "Experiment design"), monogram: "EXP", blurb: tr("方案规划与技能编排", "Planning and skill orchestration") },
  data_processing: { label: tr("数据处理", "Data processing"), monogram: "DP", blurb: tr("图像分析 / 拼图 / FFT", "Image analysis / mosaic / FFT") },
  paper_writing: { label: tr("论文撰写", "Paper writing"), monogram: "PW", blurb: tr("结果整理与成稿", "Result collation and drafting") },
  paper_review: { label: tr("论文评审", "Paper review"), monogram: "PR", blurb: tr("稿件审阅与反馈", "Manuscript review and feedback") },
  buffer_summarizer: { label: tr("缓冲摘要", "Buffer summary"), monogram: "BUF", blurb: tr("尖端状态/分割摘要", "Tip state / segmentation summary") },
};

export function agentLabel(agentId: string): string {
  return AGENT_META[agentId]?.label ?? agentId;
}

/** Effective-thinking → tone + label. The string is whatever the model actually
 *  runs at (e.g. "high (固定)", "off", null when registry omits it). */
function thinkingTone(thinking?: string | null): { tone: string; text: string } {
  const t = (thinking ?? "").toLowerCase();
  if (!thinking) return { tone: "default", text: tr("未知", "Unknown") };
  if (t.includes("off") || t === "none" || t.includes("无")) return { tone: "INFO", text: thinking };
  if (t.includes("固定") || t.includes("fixed")) return { tone: "WARN", text: thinking };
  return { tone: "AUTO", text: thinking };
}

export function AgentCard({
  agentId,
  model,
  thinking,
  toolCount,
  active,
  onSelect,
}: {
  agentId: string;
  model?: string;
  thinking?: string | null;
  toolCount?: number;
  active?: boolean;
  onSelect?: () => void;
}) {
  const meta = AGENT_META[agentId];
  const th = thinkingTone(thinking);
  return (
    <Card
      className={
        "cursor-pointer transition-colors hover:border-mast-accent/60 " +
        (active ? "border-mast-accent ring-1 ring-mast-accent/40" : "")
      }
    >
      <button type="button" onClick={onSelect} className="w-full text-left">
        <div className="flex items-start gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded bg-mast-accent/15 text-xs font-bold text-mast-accent">
            {meta?.monogram ?? agentId.slice(0, 2).toUpperCase()}
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-2">
              <span className="truncate font-medium">{meta?.label ?? agentId}</span>
              {typeof toolCount === "number" && (
                <span className="shrink-0 text-xs text-mast-muted">{tr(`${toolCount} 工具`, `${toolCount} tools`)}</span>
              )}
            </div>
            <p className="mt-0.5 truncate text-xs text-mast-muted">{meta?.blurb ?? agentId}</p>
          </div>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="rounded bg-mast-bg px-2 py-0.5 font-mono text-xs text-mast-text">
            {model ?? "—"}
          </span>
          <span className="text-xs text-mast-muted">{tr("思考", "Thinking")}</span>
          <Badge tone={th.tone}>{th.text}</Badge>
        </div>
      </button>
    </Card>
  );
}
