import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Badge, Card } from "@/components/ui";
import { useLegacyFeatures } from "@/hooks/useLegacyFeatures";
import { tr } from "@/lib/i18n";

/** Shown in place of a legacy page (or panel) while legacy features are off. */
export function LegacyNotice({ compact = false }: { compact?: boolean }) {
  const body = (
    <>
      <div className="flex items-center gap-2">
        <Badge tone="WARN">{tr("旧版功能", "Legacy feature")}</Badge>
        <span className="text-[13px] font-semibold text-mast-text">
          {tr("此功能已默认隐藏", "This feature is hidden by default")}
        </span>
      </div>
      <p className="mt-2 text-[12.5px] text-mast-muted">
        {tr(
          "MAST 现在推荐用 Claude Code 通过 MAST 插件控制仪器。内置 agent、多天 conduct、记忆、视觉模型等属于旧版功能。如需使用，请在「设置 → 常规设置 → 旧版功能」中打开。",
          "MAST now recommends controlling the instrument from Claude Code through the MAST plugin. The built-in agents, multi-day conduct, memory and the vision model are legacy features. To use them, turn them on under Settings → General → Legacy features.",
        )}
      </p>
      <Link
        to="/settings/general#legacy-features"
        className="mt-2 inline-block text-[12.5px] text-mast-accent underline"
      >
        {tr("前往设置 →", "Open settings →")}
      </Link>
    </>
  );
  return compact ? (
    <div className="rounded-mast-ctl border border-mast-border p-3">{body}</div>
  ) : (
    <Card className="mx-auto mt-6 max-w-[640px] p-5">{body}</Card>
  );
}

/** Render children only when legacy features are on; otherwise explain why not. */
export function LegacyOnly({ children, compact }: { children: ReactNode; compact?: boolean }) {
  const { enabled, loaded } = useLegacyFeatures();
  if (!loaded) return null;
  return enabled ? <>{children}</> : <LegacyNotice compact={compact} />;
}

/** Small inline marker for features that are present but unfinished. */
export function IncompleteBadge() {
  return (
    <span
      className="ml-1.5 inline-flex items-center rounded-mast-badge border border-mast-warn px-1.5 py-[1px] text-[10.5px] font-medium text-mast-warn"
      title={tr("该功能尚不完善，仍在开发中", "This feature is incomplete and still being developed")}
    >
      {tr("不完善", "incomplete")}
    </span>
  );
}
