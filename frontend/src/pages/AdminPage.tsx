import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { api } from "@/api/client";
import type { components } from "@/api/schema";
import { useUiStore } from "@/store";
import {
  Badge,
  Card,
  DegradedNote,
  ErrorNote,
  Section,
  Spinner,
} from "@/components/ui";
import { DataTable } from "@/components/DataTable";
import { CapabilityGates } from "@/components/admin/CapabilityGates";
import {
  useToast, RadioGroup, SubTabs } from "@/components/controls";
import { PinGate } from "@/components/admin/PinGate";
import { ChecksEditor, ConstraintsEditor, SkillLevelsEditor } from "@/components/admin/SafetySections";
import { SkillsManager } from "@/components/admin/SkillsManager";
import { GuidanceEditor } from "@/components/admin/GuidanceEditor";
import { QuickPromptsEditor } from "@/components/admin/QuickPromptsEditor";
import { HardwareManager } from "@/components/admin/HardwareManager";
import { OverrideHistory } from "@/components/admin/OverrideHistory";
import { JsonSectionEditor } from "@/components/admin/JsonSectionEditor";
import { ReferenceTables } from "@/components/admin/ReferenceTables";
import { LightAgentPanel } from "@/components/admin/LightAgentPanel";
import { PromptInspector } from "@/components/admin/PromptInspector";
import { useStickyTab } from "@/hooks/useStickyTab";
import { useLegacyFeatures } from "@/hooks/useLegacyFeatures";
import { IncompleteBadge } from "@/components/shell/LegacyOnly";
import { tr } from "@/lib/i18n";

// Advanced administration, grouped per agent. The top level is a selector:
//   - Instrument control (IC): the full governance panel (safety system, skill
//     management, knowledge base, skill guidance, encyclopedia config, reference/rules).
//   - LIT / XD / DP / PW / PR: a light read-only panel (see LightAgentPanel).
//   - System / global: surfaces that are not per-agent (capability gates, hardware
//     connection, system check, override history, quick prompts, context injection).
// With legacy features off, the built-in-agent parts (non-IC agents, knowledge base,
// skill guidance, quick prompts, context injection) are hidden.
// A PIN gate sits in front (shared zustand flag). Everything is forms and tables;
// every read renders loading / error / degraded / empty states.

type SafetyLimits = components["schemas"]["SafetyLimits"];
type SystemCheckItem = components["schemas"]["SystemCheckItem"];

// ── Safety limits (read + edit as a FORM, never a Dataframe) ──────────────────
// 5-column grid: 4 paired rows (bias / Z / XY / current setpoint), each split into
// min / max (8 fields). scan_size_* is intentionally not shown.
const LIMIT_ROWS: {
  label: string;
  unit: string;
  minKey: keyof SafetyLimits;
  maxKey: keyof SafetyLimits;
}[] = [
  { label: tr("偏压 (Bias)", "Bias"), unit: "V", minKey: "bias_min_v", maxKey: "bias_max_v" },
  { label: tr("Z 位置", "Z position"), unit: "m", minKey: "z_min_m", maxKey: "z_max_m" },
  { label: tr("XY 位置", "XY position"), unit: "m", minKey: "xy_min_m", maxKey: "xy_max_m" },
  { label: tr("电流设定值 (Setpoint)", "Current setpoint"), unit: "A", minKey: "setpoint_min_a", maxKey: "setpoint_max_a" },
];
const LIMIT_FIELDS: { key: keyof SafetyLimits; label: string; unit: string }[] =
  LIMIT_ROWS.flatMap((r) => [
    { key: r.minKey, label: `${r.label} · ${tr("最小", "min")}`, unit: r.unit },
    { key: r.maxKey, label: `${r.label} · ${tr("最大", "max")}`, unit: r.unit },
  ]);

// SI-prefix formatter for the DEFAULT column.
function fmtSci(value: number, unit: string): string {
  if (!Number.isFinite(value)) return `${value} ${unit}`;
  const absVal = Math.abs(value);
  if (absVal === 0) return `0 ${unit}`;
  const prefixes: [number, string][] = [
    [1e-15, "f"], [1e-12, "p"], [1e-9, "n"], [1e-6, "μ"],
    [1e-3, "m"], [1, ""], [1e3, "k"], [1e6, "M"],
  ];
  for (const [scale, prefix] of prefixes) {
    if (absVal < scale * 1000) {
      const scaled = value / scale;
      if (scaled === Math.trunc(scaled)) return `${Math.trunc(scaled)} ${prefix}${unit}`;
      return `${scaled} ${prefix}${unit}`;
    }
  }
  return `${value} ${unit}`;
}

// SafetyLimits code defaults (from the schema @default annotations), shown in the
// DEFAULT column in SI-prefixed form.
const LIMIT_DEFAULTS: Record<string, number> = {
  bias_min_v: -10, bias_max_v: 10,
  z_min_m: 0, z_max_m: 1.5e-6,
  xy_min_m: -1.5e-6, xy_max_m: 1.5e-6,
  setpoint_min_a: 1e-12, setpoint_max_a: 1e-7,
};

function SafetyLimitsSection() {
  const queryClient = useQueryClient();
  const { data, isPending, error } = useQuery({
    queryKey: ["safety", "limits"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/safety/limits");
      if (error) throw error;
      return data;
    },
  });
  // Override layer — drives the per-field STATUS pill (override / default).
  const ovrQ = useQuery({
    queryKey: ["override", "safety_limits"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/admin/overrides/{category}", {
        params: { path: { category: "safety_limits" } },
      });
      if (error) throw error;
      return data;
    },
  });
  const overrideMap = (ovrQ.data?.data ?? {}) as Record<string, unknown>;

  const [draft, setDraft] = useState<Record<string, string> | null>(null);
  const view = useMemo<Record<string, string> | null>(() => {
    if (!data) return null;
    if (draft) return draft;
    const m: Record<string, string> = {};
    for (const f of LIMIT_FIELDS) m[f.key as string] = String(data[f.key]);
    return m;
  }, [data, draft]);

  const save = useMutation({
    mutationFn: async (payload: Record<string, number>) => {
      const { data, error } = await api.POST("/api/admin/overrides/{category}", {
        params: { path: { category: "safety_limits" } },
        body: { data: payload },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      setDraft(null);
      queryClient.invalidateQueries({ queryKey: ["safety", "limits"] });
      queryClient.invalidateQueries({ queryKey: ["override", "safety_limits"] });
    },
  });

  const isOverridden = (key: string): boolean =>
    overrideMap[key] != null && overrideMap[key] !== "";

  return (
    <Section title={tr("全局安全限制", "Global safety limits")}>
      {isPending && <Spinner />}
      {error && <ErrorNote error={error} />}
      {data && view && (
        <Card>
          <p className="mb-3 text-xs text-mast-muted">
            {tr(
              "硬件参数的最小/最大边界。空值 = 使用代码默认。保存后立即生效，SafetyGuard 随下次操作拾取新值。",
              "Minimum / maximum bounds for hardware parameters. Empty = use the code default. Saved values take effect immediately; SafetyGuard picks them up on the next action.",
            )}
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const payload: Record<string, number> = {};
              for (const f of LIMIT_FIELDS) {
                const raw = (view[f.key as string] ?? "").trim();
                if (raw === "") continue;
                const n = Number(raw);
                if (!Number.isNaN(n)) payload[f.key as string] = n;
              }
              save.mutate(payload);
            }}
          >
            {/* 5-column grid header */}
            <div className="grid grid-cols-[1.6fr_1.2fr_0.5fr_1fr_0.8fr] items-center gap-2 border-b border-mast-border px-1 pb-1 text-[0.7rem] font-medium uppercase tracking-wide text-mast-muted">
              <div>{tr("限值", "Limit")}</div>
              <div>{tr("覆盖值", "Override")}</div>
              <div>{tr("单位", "Unit")}</div>
              <div>{tr("默认", "Default")}</div>
              <div>{tr("状态", "Status")}</div>
            </div>
            {LIMIT_FIELDS.map((f) => {
              const key = f.key as string;
              const ovr = isOverridden(key);
              return (
                <div
                  key={key}
                  className="grid grid-cols-[1.6fr_1.2fr_0.5fr_1fr_0.8fr] items-center gap-2 border-b border-mast-border/40 px-1 py-1.5"
                >
                  <div className="leading-tight">
                    <div className="text-sm text-mast-text">{f.label}</div>
                    <div className="font-mono text-[0.7rem] text-mast-muted">{key}</div>
                  </div>
                  <input
                    type="text"
                    inputMode="decimal"
                    value={view[key] ?? ""}
                    onChange={(e) =>
                      setDraft({ ...(view as Record<string, string>), [key]: e.target.value })
                    }
                    className="rounded-md border border-mast-border bg-mast-bg px-2 py-1.5 font-mono text-sm text-mast-text outline-none focus:border-mast-accent"
                  />
                  <div className="text-xs text-mast-muted">{f.unit}</div>
                  <div className="font-mono text-xs text-mast-muted">
                    {fmtSci(LIMIT_DEFAULTS[key] ?? 0, f.unit)}
                  </div>
                  <div>
                    <Badge tone={ovr ? "WARN" : "AUTO"}>{ovr ? tr("已覆盖", "override") : tr("默认", "default")}</Badge>
                  </div>
                </div>
              );
            })}
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <button
                type="submit"
                disabled={save.isPending}
                className="rounded-md bg-mast-accent/20 px-4 py-2 text-sm font-medium text-mast-accent hover:bg-mast-accent/30 disabled:opacity-50"
              >
                {save.isPending ? tr("保存中…", "Saving…") : tr("保存覆盖", "Save override")}
              </button>
              <button
                type="button"
                onClick={() => { setDraft(null); save.mutate({}); }}
                className="rounded-md border border-mast-border px-4 py-2 text-sm text-mast-muted hover:text-mast-text"
              >
                {tr("恢复默认", "Reset to defaults")}
              </button>
              {save.isError && <ErrorNote error={save.error} />}
              {save.data?.degraded && (
                <span className="text-sm text-mast-warn">{tr("写入未生效（内核未接入）。", "Write not applied (core not attached).")}</span>
              )}
              {save.data?.ok && !save.data?.degraded && (
                <span className="text-sm text-mast-auto">
                  {save.data.reloaded
                    ? tr("已保存并热重载。", "Saved and hot-reloaded.")
                    : tr("已保存。", "Saved.")}
                </span>
              )}
            </div>
          </form>
        </Card>
      )}
    </Section>
  );
}

// ── System self-check ────────────────────────────────────────────────────────
const CHECK_TONE: Record<string, string> = {
  ok: "AUTO",
  warning: "WARN",
  error: "DANGEROUS",
  unavailable: "INFO",
};
const CHECK_LABEL: Record<string, string> = {
  ok: tr("正常", "OK"),
  warning: tr("警告", "Warning"),
  error: tr("错误", "Error"),
  unavailable: tr("不可用", "Unavailable"),
};

function SystemCheckSection() {
  const [enabled, setEnabled] = useState(false);
  const checkQ = useQuery({
    queryKey: ["system", "check"],
    enabled,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/system/check");
      if (error) throw error;
      return data;
    },
  });

  const columns: ColumnDef<SystemCheckItem, any>[] = [
    { accessorKey: "name", header: tr("检查项", "Check") },
    {
      id: "status",
      header: tr("状态", "Status"),
      accessorFn: (r) => r.status,
      cell: (c) => (
        <Badge tone={CHECK_TONE[c.row.original.status] ?? "INFO"}>
          {CHECK_LABEL[c.row.original.status] ?? c.row.original.status}
        </Badge>
      ),
    },
    { accessorKey: "detail", header: tr("详情", "Details") },
  ];

  return (
    <Section
      title={tr("系统自检", "System check")}
      actions={
        <button
          onClick={() => {
            setEnabled(true);
            checkQ.refetch();
          }}
          disabled={checkQ.isFetching}
          className="rounded-md bg-mast-accent/20 px-3 py-1.5 text-sm font-medium text-mast-accent hover:bg-mast-accent/30 disabled:opacity-50"
        >
          {checkQ.isFetching ? tr("检查中…", "Checking…") : tr("运行自检", "Run check")}
        </button>
      }
    >
      {!enabled && (
        <p className="text-sm text-mast-muted">
          {tr(
            "点击「运行自检」检查 Nanonis / 存储 / LLM / 传感器。",
            "Click “Run check” to test Nanonis, storage, the LLM and sensors.",
          )}
        </p>
      )}
      {enabled && checkQ.isPending && <Spinner label={tr("检查中…", "Checking…")} />}
      {enabled && checkQ.error && <ErrorNote error={checkQ.error} />}
      {enabled && checkQ.data?.degraded && <DegradedNote what={tr("系统自检", "System check")} />}
      {enabled && checkQ.data && !checkQ.data.degraded && (
        <DataTable data={checkQ.data.items ?? []} columns={columns} empty={tr("无检查结果", "No check results")} />
      )}
    </Section>
  );
}

// ── Safety system sub-tab: 4 sections ──────────────────────────────────────────
type SafetySub = "limits" | "checks" | "constraints" | "skill_levels";
const SAFETY_SUBS: { id: SafetySub; label: string }[] = [
  { id: "limits", label: tr("全局安全限制", "Global safety limits") },
  { id: "checks", label: tr("全局检查规则", "Global check rules") },
  { id: "constraints", label: tr("材料安全约束", "Material safety constraints") },
  { id: "skill_levels", label: tr("技能安全级别", "Skill safety levels") },
];

function SafetyTab() {
  const [sub, setSub] = useStickyTab<SafetySub>(
    "admin.ic.safety", SAFETY_SUBS.map((t) => t.id), "limits");
  return (
    <div className="space-y-4">
      <SubTabs tabs={SAFETY_SUBS} value={sub} onChange={setSub} />
      {sub === "limits" && <SafetyLimitsSection />}
      {sub === "checks" && <ChecksEditor />}
      {sub === "constraints" && <ConstraintsEditor />}
      {sub === "skill_levels" && <SkillLevelsEditor />}
    </div>
  );
}

// ── Knowledge base sub-tab: 5 ktype editors ────────────────────────────────────
const KNOWLEDGE_TYPES: { id: string; label: string; desc: string }[] = [
  {
    id: "experiment_design",
    label: tr("实验设计", "Experiment design"),
    desc: tr("测量策略 / 参考实验 / 异常响应协议（experiment_design）。", "Measurement strategies, reference experiments and anomaly-response protocols (experiment_design)."),
  },
  {
    id: "fault_diagnosis",
    label: tr("故障诊断", "Fault diagnosis"),
    desc: tr("故障分类与诊断条目（fault_diagnosis）。", "Fault categories and diagnosis entries (fault_diagnosis)."),
  },
  {
    id: "hardware_profile",
    label: tr("硬件画像", "Hardware profile"),
    desc: tr("仪器硬件画像与能力（hardware_profile）。", "Instrument hardware profile and capabilities (hardware_profile)."),
  },
  {
    id: "image_databases",
    label: tr("图像数据库", "Image databases"),
    desc: tr("参考图像数据库（image_databases）。", "Reference image databases (image_databases)."),
  },
  {
    id: "workflows",
    label: tr("工作流", "Workflows"),
    desc: tr("工作流分类与配方（workflows）。", "Workflow categories and recipes (workflows)."),
  },
];

function KnowledgeTab() {
  const [ktype, setKtype] = useState(KNOWLEDGE_TYPES[0]!.id);
  const cur = KNOWLEDGE_TYPES.find((k) => k.id === ktype)!;
  return (
    <div>
      <div className="mb-3 flex flex-wrap gap-2">
        {KNOWLEDGE_TYPES.map((k) => (
          <button
            key={k.id}
            onClick={() => setKtype(k.id)}
            className={
              "rounded-md border px-3 py-1.5 text-sm " +
              (ktype === k.id
                ? "border-mast-accent bg-mast-accent/15 text-mast-accent"
                : "border-mast-border text-mast-muted hover:text-mast-text")
            }
          >
            {k.label}
          </button>
        ))}
      </div>
      <JsonSectionEditor
        key={ktype}
        title={`${tr("知识库", "Knowledge base")} · ${cur.label}`}
        description={cur.desc}
        getPath="/api/admin/knowledge/{ktype}"
        postPath="/api/admin/knowledge/{ktype}"
        pathParam={{ ktype }}
        queryKey={["knowledge", ktype]}
      />
    </div>
  );
}

// ── Encyclopedia config sub-tab: domains / intents / hierarchy / verification ──
const ENC_SECTIONS: { id: string; label: string; desc: string }[] = [
  {
    id: "domains",
    label: tr("领域 (domains)", "Domains"),
    desc: tr("技能领域分组定义（DOMAINS）。", "Skill domain grouping (DOMAINS)."),
  },
  {
    id: "intents",
    label: tr("意图映射 (intents)", "Intent mapping"),
    desc: tr("意图到技能/agent 的路由映射（INTENT_MAPPING）。", "Routing from intents to skills / agents (INTENT_MAPPING)."),
  },
  {
    id: "hierarchy",
    label: tr("复合层级 (hierarchy)", "Composite hierarchy"),
    desc: tr("复合技能层级（COMPOSITE_HIERARCHY）。", "Composite skill hierarchy (COMPOSITE_HIERARCHY)."),
  },
  {
    id: "verification",
    label: tr("验证 (verification)", "Verification"),
    desc: tr("技能验证规则（SKILL_VERIFICATION）。", "Skill verification rules (SKILL_VERIFICATION)."),
  },
];

function EncyclopediaTab() {
  const [section, setSection] = useState(ENC_SECTIONS[0]!.id);
  const cur = ENC_SECTIONS.find((s) => s.id === section)!;
  return (
    <div>
      <div className="mb-3 flex flex-wrap gap-2">
        {ENC_SECTIONS.map((s) => (
          <button
            key={s.id}
            onClick={() => setSection(s.id)}
            className={
              "rounded-md border px-3 py-1.5 text-sm " +
              (section === s.id
                ? "border-mast-accent bg-mast-accent/15 text-mast-accent"
                : "border-mast-border text-mast-muted hover:text-mast-text")
            }
          >
            {s.label}
          </button>
        ))}
      </div>
      <JsonSectionEditor
        key={section}
        title={`${tr("百科配置", "Encyclopedia config")} · ${cur.label}`}
        description={cur.desc}
        getPath="/api/encyclopedia/config/{section}"
        postPath="/api/encyclopedia/config/{section}"
        pathParam={{ section }}
        queryKey={["encyclopedia", section]}
      />
    </div>
  );
}

// ── IC (instrument control) full governance ──────────────────────────────────────
// Reference/rules and encyclopedia config are marked incomplete. Complex skills live
// in the dedicated skill-builder tab (composite version / clone / rollback), so they
// are not duplicated here.
type IcSub = "safety" | "skills" | "knowledge" | "guidance" | "encyclopedia" | "reference";
const incomplete = () => tr("不完善", "incomplete");
const IC_SUB_TABS: { id: IcSub; label: string; legacy?: boolean }[] = [
  { id: "safety", label: tr("安全系统", "Safety system") },
  { id: "skills", label: tr("技能管理", "Skill management") },
  { id: "knowledge", label: tr("知识库", "Knowledge base"), legacy: true },
  { id: "guidance", label: tr("技能指导", "Skill guidance"), legacy: true },
  { id: "encyclopedia", label: `${tr("百科配置", "Encyclopedia config")} · ${incomplete()}` },
  { id: "reference", label: `${tr("参考/规则", "Reference & rules")} · ${incomplete()}` },
];

function IcGovernanceTabs({ legacyEnabled }: { legacyEnabled: boolean }) {
  const tabs = IC_SUB_TABS.filter((t) => legacyEnabled || !t.legacy);
  const [sub, setSub] = useStickyTab<IcSub>(
    "admin.ic", tabs.map((t) => t.id), "safety");
  return (
    <div className="space-y-3">
      <p className="text-xs text-mast-muted">
        <strong className="text-mast-text">{tr("仪器控制 IC", "Instrument control (IC)")}</strong>
        {tr(
          " — 拥有全部 ~250 个 Nanonis V5e 硬件技能，以下为完整治理面板（安全限值 / 技能元数据",
          " — owns all ~250 Nanonis V5e hardware skills. The panels below are the full governance surface (safety limits / skill metadata",
        )}
        {legacyEnabled
          ? tr(" / 知识库 / 技能指导 / 百科配置）。", " / knowledge base / skill guidance / encyclopedia config).")
          : tr(" / 百科配置）。", " / encyclopedia config).")}
        {" "}⚠️{" "}
        <strong className="text-mast-text">
          {tr("改动会影响真实运行的 agent", "Changes affect agents that are running")}
        </strong>
        {tr(
          "（经 ConfigOverrideRegistry 持久化并热重载）。",
          " (persisted through ConfigOverrideRegistry and hot-reloaded).",
        )}
      </p>
      <SubTabs tabs={tabs} value={sub} onChange={setSub} />
      {sub === "safety" && <SafetyTab />}
      {sub === "skills" && (
        <Section title={tr("技能管理", "Skill management")}>
          <SkillsManager />
        </Section>
      )}
      {sub === "knowledge" && legacyEnabled && (
        <Section title={tr("知识库编辑", "Knowledge base editor")}>
          <KnowledgeTab />
        </Section>
      )}
      {sub === "guidance" && legacyEnabled && (
        <Section title={tr("技能指导（专家标注）", "Skill guidance (expert annotations)")}>
          <GuidanceEditor />
        </Section>
      )}
      {sub === "encyclopedia" && (
        <Section title={tr("百科配置", "Encyclopedia config")} actions={<IncompleteBadge />}>
          <EncyclopediaTab />
        </Section>
      )}
      {sub === "reference" && (
        <Section title={tr("参考/规则（只读）", "Reference & rules (read-only)")} actions={<IncompleteBadge />}>
          <ReferenceTables />
        </Section>
      )}
    </div>
  );
}

// ── System / global ──────────────────────────────────────────────────────────────
// Surfaces that are not per-agent. Quick prompts and context injection belong to the
// built-in agent, so they are legacy-only. Context injection is not the default
// sub-tab: most users do not need it.
type SysSub = "capabilities" | "hardware" | "system" | "history" | "prompts" | "context";
const SYS_SUB_TABS: { id: SysSub; label: string; legacy?: boolean }[] = [
  { id: "capabilities", label: tr("能力开关", "Capability gates") },
  { id: "hardware", label: tr("硬件连接", "Hardware connection") },
  { id: "system", label: tr("系统自检", "System check") },
  { id: "history", label: tr("覆盖历史", "Override history") },
  { id: "prompts", label: tr("快速提示", "Quick prompts"), legacy: true },
  { id: "context", label: tr("上下文注入", "Context injection"), legacy: true },
];

function SystemGlobalTabs({ legacyEnabled }: { legacyEnabled: boolean }) {
  const tabs = SYS_SUB_TABS.filter((t) => legacyEnabled || !t.legacy);
  const [sub, setSub] = useStickyTab<SysSub>(
    "admin.system", tabs.map((t) => t.id), "capabilities");
  const { toast, node: toastNode } = useToast();
  return (
    <div className="space-y-3">
      {toastNode}
      <p className="text-xs text-mast-muted">
        <strong className="text-mast-text">{tr("系统 / 全局", "System / global")}</strong>
        {tr(
          " — 非按-agent 的全局管理面（能力开关 / 硬件连接 / 系统自检 / 覆盖历史",
          " — global administration that is not per-agent (capability gates / hardware connection / system check / override history",
        )}
        {legacyEnabled ? tr(" / 快速提示）。", " / quick prompts).") : tr("）。", ").")}
      </p>
      <SubTabs tabs={tabs} value={sub} onChange={setSub} />
      {sub === "capabilities" && (
        <Section
          title={tr(
            "能力开关 — 授予 agent 默认没有的能力（需管理 PIN）",
            "Capability gates — grant agents capabilities they do not have by default (admin PIN required)",
          )}
        >
          <CapabilityGates toast={toast} />
        </Section>
      )}
      {sub === "hardware" && (
        <Section title={tr("硬件连接", "Hardware connection")}>
          <HardwareManager />
        </Section>
      )}
      {sub === "system" && <SystemCheckSection />}
      {sub === "history" && (
        <Section title={tr("覆盖历史 · 查看 + 恢复", "Override history · view and restore")}>
          <OverrideHistory />
        </Section>
      )}
      {sub === "prompts" && legacyEnabled && (
        <Section title={tr("快速提示编辑", "Quick prompt editor")}>
          <QuickPromptsEditor />
        </Section>
      )}
      {sub === "context" && legacyEnabled && <PromptInspector />}
    </div>
  );
}

// ── per-agent selector ─────────────────────────────────────────────────────────
// 6 core agents in scientific-workflow order, then a final system / global entry.
// Only IC and system / global remain when legacy features are off.
type AgentSel =
  | "literature"
  | "experiment_design"
  | "instrument_control"
  | "data_processing"
  | "paper_writing"
  | "paper_review"
  | "__system__";

const AGENT_SELECTOR: { value: AgentSel; label: string; legacy?: boolean }[] = [
  { value: "literature", label: tr("文献 LIT", "Literature (LIT)"), legacy: true },
  { value: "experiment_design", label: tr("实验设计 XD", "Experiment design (XD)"), legacy: true },
  { value: "instrument_control", label: tr("仪器控制 IC", "Instrument control (IC)") },
  { value: "data_processing", label: tr("数据处理 DP", "Data processing (DP)"), legacy: true },
  { value: "paper_writing", label: tr("论文写作 PW", "Paper writing (PW)"), legacy: true },
  { value: "paper_review", label: tr("论文审稿 PR", "Paper review (PR)"), legacy: true },
  { value: "__system__", label: tr("系统 / 全局", "System / global") },
];

export default function AdminPage() {
  const pinUnlocked = useUiStore((s) => s.pinUnlocked);
  const setPinUnlocked = useUiStore((s) => s.setPinUnlocked);
  const { enabled: legacyEnabled } = useLegacyFeatures();
  const agents = AGENT_SELECTOR.filter((a) => legacyEnabled || !a.legacy);
  // Default to IC: its panel holds the most content. The last-visited sub-tab is
  // remembered per level so returning to a half-edited panel is not a chain of clicks.
  const [agent, setAgent] = useStickyTab<AgentSel>(
    "admin.agent", agents.map((a) => a.value), "instrument_control");

  if (!pinUnlocked) return <PinGate />;

  const cur = agents.find((a) => a.value === agent) ?? agents[0]!;

  return (
    <div className="space-y-3">
      <div className="mb-2 flex items-center justify-between">
        <h1 className="text-lg font-semibold text-mast-text">{tr("高级管理", "Advanced")}</h1>
        <button
          onClick={() => setPinUnlocked(false)}
          className="rounded-md border border-mast-border px-3 py-1.5 text-sm text-mast-muted hover:text-mast-text"
        >
          {tr("锁定", "Lock")}
        </button>
      </div>
      {legacyEnabled && (
        <p className="mb-2 text-xs text-mast-muted">
          {tr("按 agent 分组的系统配置。", "System configuration grouped by agent. ")}
          <strong className="text-mast-text">{tr("仪器控制 IC", "Instrument control (IC)")}</strong>
          {tr(
            " 拥有完整治理面板；其余 5 个工具型 agent 为只读（工具均 AUTO 级，无安全门控）；模型 / thinking 在「设置」调整。",
            " has the full governance panel; the other 5 tool agents are read-only (all tools are AUTO level, no safety gating). Model and thinking are set under Settings.",
          )}
        </p>
      )}

      {agents.length > 1 && <RadioGroup value={agent} onChange={setAgent} options={agents} />}

      <div className="pt-1">
        {cur.value === "instrument_control" && <IcGovernanceTabs legacyEnabled={legacyEnabled} />}
        {cur.value === "__system__" && <SystemGlobalTabs legacyEnabled={legacyEnabled} />}
        {cur.value !== "instrument_control" && cur.value !== "__system__" && (
          <LightAgentPanel agentId={cur.value} label={cur.label} />
        )}
      </div>
    </div>
  );
}
