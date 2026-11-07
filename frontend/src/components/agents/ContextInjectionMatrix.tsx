import { useMemo } from "react";

import { Avatar, agentDef } from "@/components/agents/registry";
import { ShareBar } from "@/components/agents/ShareBar";
import { Badge, Card } from "@/components/ui";
import { tr } from "@/lib/i18n";
import {
  type InjectionMatrix,
  agentFootprint,
  buildMatrix,
  fmtChars,
  positionLabel,
  toUiAgentId,
  whenLabel,
} from "@/lib/contextInjection";

// "All" view. The table exists to answer one question: do different roles get
// targeted injections?
//
// It therefore has to show differences: if every cell read "yes" it would answer
// nothing. Blocks scoped to specific agents must not be rendered as global.

export function ContextInjectionMatrix({
  matrix,
  onPick,
}: {
  matrix: InjectionMatrix;
  onPick: (uiId: string) => void;
}) {
  // These are optional in the schema (the backend has defaults); under strict
  // typing the frontend must fall back explicitly. A `!` would turn a missing
  // field into a runtime blank screen.
  const agents = useMemo(() => matrix.agents ?? [], [matrix.agents]);
  const columns = useMemo(() => agents.map((a) => a.id), [agents]);
  const rows = useMemo(() => buildMatrix(matrix, columns), [matrix, columns]);

  const exclusiveCount = rows.filter((r) => r.exclusive).length;
  const sharedCount = rows.filter((r) => r.shared).length;

  return (
    <div className="space-y-4">
      <Card>
        <h3 className="mb-1 text-sm font-semibold text-mast-text">
          {tr(`注入矩阵：${rows.length} 块 × ${columns.length} 个角色`, `Injection matrix: ${rows.length} blocks × ${columns.length} roles`)}
        </h3>
        <p className="mb-3 text-xs text-mast-muted">
          {tr(`${sharedCount} 块发给全员，${exclusiveCount} 块只发给一个角色。`, `${sharedCount} blocks go to all roles, ${exclusiveCount} to a single role only.`)}
          {matrix.note && <span className="ml-1 text-mast-faint">{matrix.note}</span>}
        </p>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] border-collapse text-xs">
            <thead>
              <tr className="border-b border-mast-border">
                <th className="sticky left-0 bg-mast-panel px-2 py-1.5 text-left font-medium text-mast-muted">
                  {tr("注入块", "Injection block")}
                </th>
                <th className="px-2 py-1.5 text-left font-medium text-mast-muted">{tr("时机", "When")}</th>
                <th className="px-2 py-1.5 text-left font-medium text-mast-muted">{tr("落点", "Position")}</th>
                {agents.map((a) => (
                  <th key={a.id} className="px-1 py-1.5 text-center font-medium">
                    <button
                      type="button"
                      onClick={() => onPick(toUiAgentId(a.id))}
                      className="inline-flex flex-col items-center gap-0.5 text-mast-muted hover:text-mast-text"
                      title={a.label ?? a.id}
                    >
                      <Avatar id={toUiAgentId(a.id)} size={18} />
                      <span className="text-[10px]">
                        {agentDef(toUiAgentId(a.id)).short}
                      </span>
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.block.id} className="border-b border-mast-border last:border-b-0">
                  <td className="sticky left-0 bg-mast-panel px-2 py-1.5">
                    <span className="text-mast-text">{row.block.label}</span>
                    {row.block.overridden && (
                      <span className="ml-1.5"><Badge tone="WARN">{tr("已覆写", "Overridden")}</Badge></span>
                    )}
                    <span className="block font-mono text-[10px] text-mast-faint">
                      {row.block.id}
                    </span>
                  </td>
                  <td className="px-2 py-1.5 text-mast-muted">
                    {whenLabel(row.block.when ?? "always")}
                  </td>
                  <td className="px-2 py-1.5 text-mast-muted">
                    {positionLabel(row.block.position ?? "system")}
                  </td>
                  {row.cells.map((on, i) => (
                    <td key={i} className="px-1 py-1.5 text-center">
                      {on ? (
                        <span
                          className={row.shared ? "text-mast-faint" : "text-mast-auto"}
                          title={row.shared ? tr("全员", "All roles") : tr("定向", "Targeted")}
                        >
                          {row.shared ? "○" : "●"}
                        </span>
                      ) : (
                        <span className="text-mast-border-strong">·</span>
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-xs text-mast-faint">
          {tr("● = 定向给这个角色 · ○ = 全员都收 · · = 不发给它", "● = targeted at this role · ○ = sent to all roles · · = not sent to it")}
        </p>
      </Card>

      <Card>
        <h3 className="mb-1 text-sm font-semibold text-mast-text">{tr("每个角色各背多少", "Footprint per role")}</h3>
        <p className="mb-3 text-xs text-mast-muted">
          {tr("静态提示词与工具面来自这个进程真的建过的那次图；没建过的显示「—」，这里不做静态估算。", "Static prompt and tool surface come from a graph this process actually built; roles never built show \"—\". No static estimate is made here.")}
        </p>
        <ul className="space-y-3">
          {agents.map((a) => {
            const uiId = toUiAgentId(a.id);
            const fp = agentFootprint(matrix, a.id);
            const shares = [];
            if (a.system_chars) {
              shares.push({ key: "static" as const, label: tr("静态系统提示", "Static system prompt"),
                            chars: a.system_chars, pct: 0, estimated: false });
            }
            if (fp.knownChars > 0) {
              shares.push({ key: "block" as const, label: tr("中间件注入块", "Middleware injection blocks"),
                            chars: fp.knownChars, pct: 0, estimated: false });
            }
            if (a.tool_chars) {
              shares.push({ key: "tools" as const, label: tr("工具面（估算）", "Tool surface (estimated)"),
                            chars: a.tool_chars, pct: 0, estimated: true });
            }
            const total = shares.reduce((s, x) => s + x.chars, 0);
            const withPct = shares.map((s) => ({
              ...s, pct: total > 0 ? Math.round((s.chars * 100) / total) : 0,
            }));
            return (
              <li key={a.id}>
                <div className="mb-1 flex flex-wrap items-center gap-2 text-xs">
                  <button type="button" onClick={() => onPick(uiId)}
                          className="flex items-center gap-1.5 text-mast-text hover:underline">
                    <Avatar id={uiId} size={16} />
                    {a.label ?? a.id}
                  </button>
                  <span className="text-mast-muted">
                    {tr(`${fp.sharedCount} 块全员 · ${fp.exclusiveCount} 块专属`, `${fp.sharedCount} shared · ${fp.exclusiveCount} exclusive`)}
                    {fp.unknownCount > 0 && tr(` · ${fp.unknownCount} 块只在真实请求里才有内容`, ` · ${fp.unknownCount} filled only in real requests`)}
                  </span>
                  <span className="text-mast-faint">
                    {tr(`工具 ${a.tool_count ?? "—"} 个 · 提示 ${fmtChars(a.system_chars)}`, `${a.tool_count ?? "—"} tools · prompt ${fmtChars(a.system_chars)}`)}
                  </span>
                </div>
                {withPct.length > 0
                  ? <ShareBar shares={withPct} agentId={uiId} />
                  : <p className="text-xs text-mast-faint">
                      {tr("这个进程还没建过它的图，量不到。", "This process has not built its graph yet, so it cannot be measured.")}
                    </p>}
              </li>
            );
          })}
        </ul>
      </Card>
    </div>
  );
}
