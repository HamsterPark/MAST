import { useMemo, useState } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import type { NodeKind } from "./builderSpec";
import { paletteHiddenCount, paletteHides } from "@/lib/skillMarket";

// Control-flow nodes are first-class in the palette (not buried in a modal) so
// it is obvious the builder supports branching, loops, variables and decisions,
// not just skills. Each is click-to-add (at end) AND draggable into any container
// slot (dataTransfer "text/nodekind", mirrored by BuilderCanvas' drop handlers).
const CONTROL_FLOW: { kind: NodeKind; label: string; hint: string }[] = [
  {
    kind: "if",
    label: tr("⑂ if 分支", "⑂ if branch"),
    hint: tr("条件为真/假走不同分支（then / else）", "Take a different branch when the condition is true / false (then / else)"),
  },
  {
    kind: "loop",
    label: tr("↻ loop 循环", "↻ loop"),
    hint: tr("重复 N 次 / 遍历序列 / while 条件循环（body）", "Repeat N times / iterate a sequence / while-condition loop (body)"),
  },
  {
    kind: "set",
    label: tr("≔ set 变量", "≔ set variable"),
    hint: tr("设置/更新一个变量，供后续表达式引用", "Set or update a variable for later expressions to reference"),
  },
  {
    kind: "llm",
    label: tr("🤖 llm 决策", "🤖 llm decision"),
    hint: tr("受限路由 / 结构化输出的 LLM 决策节点", "LLM decision node with constrained routing / structured output"),
  },
  {
    kind: "human",
    label: tr("✋ human 人工", "✋ human"),
    hint: tr("暂停等待用户决策", "Pause and wait for an operator decision"),
  },
  {
    kind: "agent",
    label: tr("🤝 agent 委托", "🤝 agent delegate"),
    hint: tr("委托给文献/分析/写作等子 agent", "Delegate to a sub-agent (literature / analysis / writing, etc.)"),
  },
];

// Skill catalog palette for the builder: fuzzy search · category/source facet
// chips · grouped-by-domain collapsible list · favorites (★, local-only) ·
// staging basket (🧺, per-draft) · double-click / drag to add. Backed by
// GET /api/builder/catalog (richer than /api/skills/catalog: carries zh /
// category / level / tags; search is fuzzy over name / zh / domain / tags).

export interface CatalogEntry {
  name: string;
  zh?: string;
  domain: string;
  source: string;
  // builder/catalog uses `safety`; skills/catalog used `safety_level`. Accept both.
  safety?: string;
  safety_level?: string;
  category?: string;
  level?: number;
  tags?: string[];
  composition_level?: string | null;
  summary?: string | null;
  /** Whether the skill is on the user subscription surface. When the field is
   *  absent nothing is filtered (fail-open): an old or degraded response must
   *  not empty the palette. */
  subscribed?: boolean;
  /** Mandatory skill (cannot be unsubscribed). Display only; the backend is the source of truth. */
  mandatory?: boolean;
}

function safetyOf(e: CatalogEntry): string {
  return String(e.safety ?? e.safety_level ?? "");
}

const SAFETY_DOT: Record<string, string> = {
  AUTO: "var(--mast-auto)",
  auto: "var(--mast-auto)",
  WARN: "var(--mast-warn)",
  confirm: "var(--mast-warn)",
  CONFIRM: "var(--mast-warn)",
  DANGEROUS: "var(--mast-danger)",
  dangerous: "var(--mast-danger)",
};

// Facet rows: category (read / write / composite / analysis), then source
// (built-in / paper / user composite). There is no safety facet row.
const CATEGORY_FACETS: { val: string; label: string }[] = [
  { val: "READ", label: tr("读", "Read") },
  { val: "WRITE", label: tr("写", "Write") },
  { val: "COMPOSITE", label: tr("组合", "Composite") },
  { val: "ANALYSIS", label: tr("分析", "Analysis") },
];

const SOURCE_FACETS: { val: string; label: string }[] = [
  { val: "builtin", label: tr("内置", "Built-in") },
  { val: "paper", label: tr("论文", "Paper") },
  { val: "user_composite", label: tr("用户组合", "User composite") },
];

export function BuilderPalette({
  catalog,
  favorites,
  onToggleFav,
  staging,
  setStaging,
  onAddSkill,
  onAddAtEnd,
  onAddKind,
  onHover,
  loading,
}: {
  catalog: CatalogEntry[];
  favorites: string[];
  onToggleFav: (name: string) => void;
  staging: string[];
  setStaging: (next: string[]) => void;
  onAddSkill: (name: string) => void;
  onAddAtEnd?: () => void;
  onAddKind?: (kind: NodeKind) => void;
  onHover: (name: string | null, ev?: React.MouseEvent) => void;
  loading: boolean;
}) {
  const [q, setQ] = useState("");
  const [cat, setCat] = useState("");
  const [src, setSrc] = useState("");
  const [open, setOpen] = useState<Record<string, boolean>>({});
  // Default to subscribed skills only: the palette shows what the user works with,
  // the market shows everything installed. In the factory state every entry is
  // subscribed, so this default changes nothing on a fresh machine.
  const [subOnly, setSubOnly] = useState(true);

  /** Number of entries hidden by the subscription filter. It must be shown: a
   *  silent cut-off reads as "this is all there is" and invites writing a
   *  duplicate of a skill that is already installed. */
  const hiddenBySub = useMemo(
    () => paletteHiddenCount(catalog, subOnly),
    [catalog, subOnly],
  );

  const filtered = useMemo(() => {
    const ql = q.trim().toLowerCase();
    const qt = q.trim();
    return catalog.filter((e) => {
      // The predicate lives in lib/skillMarket (covered by node --test), not re-implemented here.
      if (paletteHides(e, subOnly)) return false;
      if (cat && e.category !== cat) return false;
      if (src && e.source !== src) return false;
      if (!ql) return true;
      return (
        e.name.toLowerCase().includes(ql) ||
        (e.zh || "").includes(qt) ||
        (e.domain || "").includes(qt) ||
        (e.tags || []).some((t) => t.toLowerCase().includes(ql)) ||
        (e.summary || "").toLowerCase().includes(ql)
      );
    });
  }, [catalog, q, cat, src, subOnly]);

  const groups = useMemo(() => {
    const g: Record<string, CatalogEntry[]> = {};
    for (const e of filtered) (g[e.domain] = g[e.domain] || []).push(e);
    return Object.entries(g).sort((a, b) => b[1].length - a[1].length);
  }, [filtered]);

  const favEntries = catalog.filter((e) => favorites.includes(e.name));

  const Row = (e: CatalogEntry) => (
    <div
      key={e.name}
      draggable
      onDragStart={(ev) => ev.dataTransfer.setData("text/skill", e.name)}
      onDoubleClick={() => onAddSkill(e.name)}
      onMouseEnter={(ev) => onHover(e.name, ev)}
      onMouseLeave={() => onHover(null)}
      title={tr("双击添加到工作流末尾；拖到画布同效", "Double-click to append to the workflow; dragging onto the canvas does the same")}
      className="group flex cursor-grab items-center gap-2 rounded px-2 py-1 text-sm hover:bg-mast-bg"
    >
      <span
        className="inline-block h-2 w-2 shrink-0 rounded-full"
        style={{ background: SAFETY_DOT[safetyOf(e)] ?? "var(--mast-ag-sup)" }}
      />
      <span className="flex-1 truncate text-xs">
        <span className={clsx("font-mono", e.subscribed === false
          ? "text-mast-muted" : "text-mast-text")}>{e.name}</span>
        {e.zh ? <span className="ml-1 text-mast-muted">{e.zh}</span> : null}
      </span>
      {/* Unsubscribed skills remain usable in workflows (composite sub-steps run
          through ExecutionContext, which ignores subscriptions), so this is a
          label, not a disabled state. */}
      {e.subscribed === false && (
        <span
          className="shrink-0 rounded border border-mast-border px-1 text-[10px] text-mast-muted"
          title={tr("不在你的订阅面上（agent 看不见它，但工作流仍可调用）", "Not on your subscription list (agents cannot see it, but workflows can still call it)")}
        >
          {tr("未订阅", "Not subscribed")}
        </span>
      )}
      <span className="shrink-0 text-[10px] text-mast-muted">L{e.level ?? e.composition_level ?? 0}</span>
      <button
        title={tr("加入本草稿候选", "Add to this draft staging basket")}
        onClick={(ev) => {
          ev.stopPropagation();
          if (!staging.includes(e.name)) setStaging([...staging, e.name]);
        }}
        className="shrink-0 text-xs opacity-0 group-hover:opacity-100"
      >
        🧺
      </button>
      <button
        onClick={(ev) => {
          ev.stopPropagation();
          onToggleFav(e.name);
        }}
        className={clsx(
          "shrink-0 text-xs",
          favorites.includes(e.name) ? "text-mast-warn" : "text-mast-muted opacity-0 group-hover:opacity-100",
        )}
        title={favorites.includes(e.name) ? tr("取消收藏", "Remove from favorites") : tr("收藏", "Add to favorites")}
      >
        ★
      </button>
    </div>
  );

  const Chip = (
    label: string,
    val: string,
    cur: string,
    set: (v: string) => void,
  ) => (
    <button
      key={label + val}
      onClick={() => set(cur === val ? "" : val)}
      className={clsx(
        "rounded-full border px-2 py-0.5 text-xs",
        cur === val
          ? "border-mast-accent bg-mast-accent/15 text-mast-accent"
          : "border-mast-border text-mast-muted hover:text-mast-text",
      )}
    >
      {label}
    </button>
  );

  return (
    <div className="flex h-full flex-col gap-2">
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder={tr(
          `搜索 ${catalog.length} 个技能…（名称/中文/标签/域）`,
          `Search ${catalog.length} skills… (name / Chinese name / tag / domain)`,
        )}
        className="rounded-md border border-mast-border bg-mast-bg px-2.5 py-1.5 text-sm text-mast-text outline-none focus:border-mast-accent"
      />
      {onAddAtEnd && (
        <button
          onClick={onAddAtEnd}
          className="rounded-md border border-mast-accent/60 bg-mast-accent/10 px-2.5 py-1.5 text-sm font-medium text-mast-accent hover:bg-mast-accent/20"
        >
          {tr("＋ 添加节点到末尾", "＋ Add node at end")}
        </button>
      )}

      {/* Control-flow nodes — first-class, so branching and loops are obviously available */}
      {onAddKind && (
        <div className="rounded-md border border-mast-border bg-mast-bg p-2">
          <div className="mb-1.5 text-[11px] font-medium text-mast-muted">
            {tr("控制流 · 判断 / 循环 / 变量（点击加到末尾，或拖入分支槽）", "Control flow · branch / loop / variable (click to append, or drag into a branch slot)")}
          </div>
          <div className="flex flex-wrap gap-1">
            {CONTROL_FLOW.map((c) => (
              <button
                key={c.kind}
                draggable
                onDragStart={(ev) => ev.dataTransfer.setData("text/nodekind", c.kind)}
                onClick={() => onAddKind(c.kind)}
                title={c.hint + tr("（双击/拖拽同效）", " (double-click / drag works too)")}
                className="cursor-grab rounded border border-mast-border-strong bg-mast-panel px-2 py-1 text-xs text-mast-text hover:border-mast-accent hover:text-mast-accent"
              >
                {c.label}
              </button>
            ))}
          </div>
        </div>
      )}
      <div className="flex flex-wrap gap-1">
        {CATEGORY_FACETS.map((f) => Chip(f.label, f.val, cat, setCat))}
      </div>
      <div className="flex flex-wrap gap-1">
        {SOURCE_FACETS.map((f) => Chip(f.label, f.val, src, setSrc))}
      </div>

      {/* Subscribed vs whole market. The hidden count must be stated, otherwise a
          missing skill looks uninstalled. Clicking it switches to the whole market. */}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <button
          type="button"
          onClick={() => setSubOnly(!subOnly)}
          className={clsx(
            "rounded border px-1.5 py-0.5",
            subOnly
              ? "border-mast-border-strong bg-mast-panel text-mast-text"
              : "border-mast-border text-mast-muted",
          )}
        >
          {subOnly ? tr("只看订阅", "Subscribed only") : tr("全市场", "All skills")}
        </button>
        {subOnly && hiddenBySub > 0 && (
          <button
            type="button"
            onClick={() => setSubOnly(false)}
            className="text-mast-accent underline decoration-dotted"
            title={tr("订阅列表在「技能 → 市场」维护", "The subscription list is managed under Skills → Market")}
          >
            {tr(`另有 ${hiddenBySub} 个未订阅（点此显示）`, `${hiddenBySub} more not subscribed (click to show)`)}
          </button>
        )}
      </div>

      {staging.length > 0 && (
        <div className="rounded border border-mast-border bg-mast-bg p-2">
          <div className="mb-1 text-xs text-mast-muted">{tr("🧺 本草稿候选（点击添加）", "🧺 Staging basket (click to add)")}</div>
          <div className="flex flex-wrap gap-1">
            {staging.map((s) => (
              <span
                key={s}
                onClick={() => onAddSkill(s)}
                className="flex cursor-pointer items-center gap-1 rounded bg-mast-panel px-1.5 py-0.5 text-xs text-mast-text"
              >
                {s}
                <span
                  onClick={(e) => {
                    e.stopPropagation();
                    setStaging(staging.filter((x) => x !== s));
                  }}
                  className="text-mast-muted hover:text-mast-danger"
                >
                  ✕
                </span>
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-auto pr-1">
        {loading && <p className="px-2 py-1 text-xs text-mast-muted">{tr("加载技能目录…", "Loading skill catalog…")}</p>}
        {!loading && !catalog.length && (
          <p className="px-2 py-1 text-xs text-mast-muted">{tr("技能目录为空（内核未连接？）。", "Skill catalog is empty (kernel not connected?).")}</p>
        )}
        {favEntries.length > 0 && (
          <div className="mb-2">
            <div className="px-2 py-1 text-xs font-medium text-mast-warn">
              {tr("★ 收藏", "★ Favorites")} <span className="text-mast-muted">{favEntries.length}</span>
            </div>
            {favEntries.map(Row)}
          </div>
        )}
        {groups.map(([dom, list]) => {
          const collapsed = open[dom] === false;
          return (
            <div key={dom} className="mb-1.5">
              <button
                onClick={() => setOpen({ ...open, [dom]: !collapsed ? false : true })}
                className="flex w-full items-center gap-1 px-2 py-1 text-left text-xs font-medium text-mast-text hover:text-mast-accent"
              >
                <span>{collapsed ? "▸" : "▾"}</span>
                <span className="flex-1 truncate">{dom}</span>
                <span className="text-mast-muted">{list.length}</span>
              </button>
              {!collapsed && list.map(Row)}
            </div>
          );
        })}
      </div>
      <p className="px-1 text-[11px] text-mast-muted">{tr("双击/拖拽添加 · ★收藏 · 🧺存入草稿候选", "Double-click / drag to add · ★ favorite · 🧺 stage for this draft")}</p>
    </div>
  );
}
