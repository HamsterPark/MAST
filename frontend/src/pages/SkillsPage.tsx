import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { type ColumnDef } from "@tanstack/react-table";
import { api } from "@/api/client";
import { DataTable } from "@/components/DataTable";
import {
  Section,
  Badge,
  Spinner,
  ErrorNote,
  DegradedNote,
} from "@/components/ui";
import { SubTabs, Modal } from "@/components/controls";
import { SkillCardDetail } from "@/components/skills/SkillCardDetail";
import { SkillOverrideEditor } from "@/components/skills/SkillOverrideEditor";
import { CompositesManager } from "@/components/skills/CompositesManager";
import { EncyclopediaView } from "@/components/skills/EncyclopediaView";
import { CodexReference } from "@/components/skills/CodexReference";
import { SkillOverlayManager } from "@/components/skills/SkillOverlayManager";
import { MarketView } from "@/components/skills/MarketView";
import { useStickyTab } from "@/hooks/useStickyTab";
import { tr } from "@/lib/i18n";

// Skills page. Sub-tabs (flat React state, no freeze):
//   catalog    — catalog table + source/domain/safety filters + client-side search + row detail
//   editor     — edit params, pre/post-conditions, duration, rollback, safety level via admin overrides
//   composites — list + react-flow DAG + version history + clone/restore/validate
//   overlay    — override built-in skills with .py files in <data>/config/skill_overlay;
//                reload takes effect without restart; primarily answers "is it in effect now?"
//   encyclopedia — domain grouping + intent mapping / recipes
//   codex      — read-only IC workflows / decision trees / guides / advisors / experiment design /
//                hardware reference / intent mapping

type View =
  | "catalog"
  | "market"
  | "editor"
  | "composites"
  | "overlay"
  | "encyclopedia"
  | "codex";

// One list feeds both the tab bar and the valid names for useStickyTab.
const VIEW_TABS: { id: View; label: string }[] = [
  { id: "catalog", label: tr("技能目录", "Catalog") },
  // Market sits right after the catalog: the catalog answers "what exists", the market answers
  // "which ones the agent loads" (the subscription list).
  { id: "market", label: tr("市场", "Market") },
  { id: "editor", label: tr("技能详情编辑", "Skill editor") },
  { id: "composites", label: tr("复合技能", "Composites") },
  // Overlay edits skills without shipping a new version; it belongs with the editing tabs,
  // before the two reference tabs.
  { id: "overlay", label: tr("覆盖层", "Overlay") },
  { id: "encyclopedia", label: tr("百科 · 不完善", "Encyclopedia · incomplete") },
  { id: "codex", label: tr("参考/Codex · 不完善", "Codex reference · incomplete") },
];

const SAFETY_TONE: Record<string, string> = {
  AUTO: "AUTO",
  INFO: "INFO",
  WARN: "WARN",
  DANGEROUS: "DANGEROUS",
  auto: "AUTO",
  confirm: "WARN",
  dangerous: "DANGEROUS",
};

function useCatalog() {
  return useQuery({
    queryKey: ["skills", "catalog"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/skills/catalog");
      if (error) throw error;
      return data;
    },
  });
}

type SkillRow = {
  name: string;
  domain: string;
  source: string;
  safety_level: string;
  composition_level?: string | null;
  summary?: string | null;
};

function FilterSelect({
  label,
  value,
  setValue,
  options,
}: {
  label: string;
  value: string;
  setValue: (v: string) => void;
  options: string[];
}) {
  return (
    <label className="flex items-center gap-1 text-xs text-mast-muted">
      {label}
      <select
        value={value}
        onChange={(e) => setValue(e.target.value)}
        className="rounded border border-mast-border bg-mast-bg px-2 py-1 text-sm text-mast-text"
      >
        <option value="all">{tr("全部", "All")}</option>
        {options.map((o) => (
          <option key={o} value={o}>{o}</option>
        ))}
      </select>
    </label>
  );
}

// ── Catalog ──────────────────────────────────────────────────────────────────
function CatalogView() {
  const q = useCatalog();
  const [text, setText] = useState("");
  const [source, setSource] = useState("all");
  const [domain, setDomain] = useState("all");
  const [safety, setSafety] = useState("all");
  const [level, setLevel] = useState("all");
  const [selected, setSelected] = useState<string | null>(null);

  const rows = (q.data?.index ?? []) as SkillRow[];

  const sources = useMemo(() => Array.from(new Set(rows.map((r) => r.source))).sort(), [rows]);
  const domains = useMemo(() => Array.from(new Set(rows.map((r) => r.domain))).sort(), [rows]);
  const safeties = useMemo(() => Array.from(new Set(rows.map((r) => r.safety_level))).sort(), [rows]);
  const levels = useMemo(
    () => Array.from(new Set(rows.map((r) => r.composition_level).filter(Boolean) as string[])).sort(),
    [rows],
  );

  const filtered = useMemo(
    () =>
      rows.filter(
        (r) =>
          (source === "all" || r.source === source) &&
          (domain === "all" || r.domain === domain) &&
          (safety === "all" || r.safety_level === safety) &&
          (level === "all" || r.composition_level === level),
      ),
    [rows, source, domain, safety, level],
  );

  const columns = useMemo<ColumnDef<SkillRow, any>[]>(
    () => [
      {
        accessorKey: "name",
        header: tr("名称", "Name"),
        cell: (c) => <span className="font-medium text-mast-text">{c.getValue() as string}</span>,
      },
      { accessorKey: "domain", header: tr("领域", "Domain") },
      { accessorKey: "source", header: tr("来源", "Source") },
      {
        accessorKey: "safety_level",
        header: tr("安全等级", "Safety level"),
        cell: (c) => (
          <Badge tone={SAFETY_TONE[c.getValue() as string] ?? "default"}>{c.getValue() as string}</Badge>
        ),
      },
      { id: "composition_level", header: tr("层级", "Level"), accessorFn: (r) => r.composition_level ?? "—" },
      {
        accessorKey: "summary",
        header: tr("摘要", "Summary"),
        cell: (c) => <span className="text-mast-muted">{(c.getValue() as string) || "—"}</span>,
      },
      {
        id: "open",
        header: "",
        cell: (c) => (
          <button
            onClick={() => setSelected(c.row.original.name)}
            className="rounded border border-mast-border px-2 py-0.5 text-xs text-mast-accent hover:bg-mast-accent/10"
          >
            {tr("查看", "View")}
          </button>
        ),
      },
    ],
    [],
  );

  if (q.isPending) return <Spinner />;
  if (q.isError) return <ErrorNote error={q.error} />;
  if (q.data?.degraded) return <DegradedNote what={tr("技能目录", "Skill catalog")} />;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={tr("搜索技能名 / 领域 / 摘要…", "Search skill name / domain / summary…")}
          className="w-64 rounded border border-mast-border bg-mast-bg px-3 py-1.5 text-sm text-mast-text placeholder:text-mast-muted"
        />
        <FilterSelect label={tr("来源", "Source")} value={source} setValue={setSource} options={sources} />
        <FilterSelect label={tr("领域", "Domain")} value={domain} setValue={setDomain} options={domains} />
        <FilterSelect label={tr("安全", "Safety")} value={safety} setValue={setSafety} options={safeties} />
        {levels.length > 0 && (
          <FilterSelect label={tr("层级", "Level")} value={level} setValue={setLevel} options={levels} />
        )}
        <span className="text-xs text-mast-muted">{tr("共", "Total")} {filtered.length} / {rows.length}</span>
      </div>

      <DataTable data={filtered} columns={columns} globalFilter={text} empty={tr("无匹配技能", "No matching skills")} />

      {/* Skill detail opens in a modal rather than expanding inline below the table. Modal
          returns null while closed, so SkillCardDetail (and its on-demand query) only mounts
          when a row is selected. */}
      <Modal
        open={!!selected}
        onClose={() => setSelected(null)}
        title={selected ? tr(`技能详情：${selected}`, `Skill details: ${selected}`) : tr("技能详情", "Skill details")}
        wide
      >
        {selected && <SkillCardDetail name={selected} />}
      </Modal>
    </div>
  );
}

// ── Editor wrapper (provides the skill-name list from the catalog) ──────
function EditorView() {
  const q = useCatalog();
  const rows = (q.data?.index ?? []) as SkillRow[];
  const names = useMemo(() => rows.map((r) => r.name).sort(), [rows]);

  if (q.isError) return <ErrorNote error={q.error} />;
  if (q.data?.degraded) return <DegradedNote what={tr("技能目录", "Skill catalog")} />;

  return <SkillOverrideEditor names={names} loadingNames={q.isPending} />;
}

export default function SkillsPage() {
  // Remember the last sub-tab: the Codex reference and encyclopedia are long documents that
  // are read in several visits, and resetting to the catalog each time forces a re-search.
  const [view, setView] = useStickyTab<View>(
    "skills", VIEW_TABS.map((t) => t.id), "catalog");

  return (
    <div>
      <Section title={tr("技能", "Skills")}>
        <SubTabs tabs={VIEW_TABS} value={view} onChange={setView} />
        {view === "catalog" && <CatalogView />}
        {view === "market" && <MarketView />}
        {view === "editor" && <EditorView />}
        {view === "composites" && <CompositesManager />}
        {view === "overlay" && <SkillOverlayManager />}
        {view === "encyclopedia" && <EncyclopediaView />}
        {view === "codex" && <CodexReference />}
      </Section>
    </div>
  );
}
