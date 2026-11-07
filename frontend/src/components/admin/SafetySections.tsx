import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { api } from "@/api/client";
import type { components } from "@/api/schema";
import { Badge, Card, DegradedNote, EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { Button, Field, SelectField, TextField, useToast } from "@/components/controls";
import { DataTable } from "@/components/DataTable";
import { tr } from "@/lib/i18n";

/** Structured safety editors (no Dataframe). Each writes its override category
 *  via /api/admin/overrides/{category} (raw payload round-trip) but displays the
 *  effective values (code defaults + overrides), not the empty override layer. The
 *  big code-default lists must always be visible:
 *    limits        → AdminPage owns the limits form (kept there)
 *    checks        → full effective _GLOBAL_CHECKS list (default / modified / added)
 *                    from /api/safety/checks/effective, editable additions on top
 *    constraints   → full SC_LIST (safety_constraints, read-only) + full default
 *                    material bias-limit table, editable per-material override
 *    skill_levels  → all skills with their default safety_level (searchable),
 *                    per-skill override on top
 */

// ── helpers ──────────────────────────────────────────────────────────────────
function useOverride(category: string) {
  return useQuery({
    queryKey: ["override", category],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/admin/overrides/{category}", {
        params: { path: { category } },
      });
      if (error) throw error;
      return data;
    },
  });
}

function useSaveOverride(category: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      const { data, error } = await api.POST("/api/admin/overrides/{category}", {
        params: { path: { category } },
        body: { data: payload },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["override", category] });
      queryClient.invalidateQueries({ queryKey: ["override", category, "history"] });
      // Editors render the EFFECTIVE layer — refresh the read-side caches too.
      queryClient.invalidateQueries({ queryKey: ["safety", "checks", "effective"] });
      queryClient.invalidateQueries({ queryKey: ["knowledge", "reference"] });
      if (category === "safety_limits") {
        queryClient.invalidateQueries({ queryKey: ["safety", "limits"] });
      }
    },
  });
}

function useLimitFields(): string[] {
  const q = useQuery({
    queryKey: ["safety", "limits"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/safety/limits");
      if (error) throw error;
      return data;
    },
  });
  return useMemo(() => {
    if (!q.data) return [];
    return Object.keys(q.data).filter((k) => k !== "degraded");
  }, [q.data]);
}

// Origin badge (shared by checks + constraints): default / modified / added.
const ORIGIN_TONE: Record<string, string> = { default: "AUTO", modified: "WARN", addition: "INFO" };
const ORIGIN_LABEL: Record<string, string> = {
  default: tr("默认", "Default"),
  modified: tr("已改", "Modified"),
  addition: tr("新增", "Added"),
};
function OriginBadge({ origin }: { origin: string }) {
  return <Badge tone={ORIGIN_TONE[origin] ?? "INFO"}>{ORIGIN_LABEL[origin] ?? origin}</Badge>;
}

// ════════════════════════════════════════════════════════════════════════════
// Global check rules editor: shows the full effective rule list as the base
// ════════════════════════════════════════════════════════════════════════════
type CheckRule = { pattern: string; unit: string; min_attr: string; max_attr: string };
type EffectiveCheckRow = components["schemas"]["EffectiveCheckRow"];

export function ChecksEditor() {
  // Read side: the full code-default _GLOBAL_CHECKS merged with overrides,
  // row-tagged default / modified / addition.
  const effQ = useQuery({
    queryKey: ["safety", "checks", "effective"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/safety/checks/effective");
      if (error) throw error;
      return data;
    },
  });
  // Write side: the persisted override layer ({overrides, additions, removals}).
  const ovrQ = useOverride("checks");
  const save = useSaveOverride("checks");
  const limitFields = useLimitFields();
  const { toast, node } = useToast();

  const stored = (ovrQ.data?.data ?? {}) as {
    overrides?: CheckRule[];
    additions?: CheckRule[];
    removals?: string[];
  };

  // Editable rows = the override + addition layer (what the admin actually owns).
  const [rows, setRows] = useState<CheckRule[] | null>(null);
  const [removals, setRemovals] = useState<string>("");
  useEffect(() => {
    setRows(null);
    setRemovals((stored.removals ?? []).join(", "));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ovrQ.data]);

  const view: CheckRule[] = rows ?? [...(stored.overrides ?? []), ...(stored.additions ?? [])];

  const effColumns: ColumnDef<EffectiveCheckRow, any>[] = [
    { accessorKey: "pattern", header: tr("参数名模式", "Parameter name pattern"), cell: (c) => <span className="font-mono text-xs">{c.row.original.pattern}</span> },
    { accessorKey: "unit", header: tr("单位", "Unit"), cell: (c) => <span className="font-mono text-xs">{c.row.original.unit || "—"}</span> },
    { accessorKey: "min_attr", header: tr("最小值字段", "Min field"), cell: (c) => <span className="font-mono text-xs">{c.row.original.min_attr || "—"}</span> },
    { accessorKey: "max_attr", header: tr("最大值字段", "Max field"), cell: (c) => <span className="font-mono text-xs">{c.row.original.max_attr || "—"}</span> },
    { id: "origin", header: tr("来源", "Origin"), accessorFn: (r) => r.origin, cell: (c) => <OriginBadge origin={c.row.original.origin} /> },
  ];

  const persist = (nextRows: CheckRule[], nextRemovals: string) => {
    // The core diffs against code defaults; we cannot tell override-vs-addition
    // client-side, so we send everything as `additions` (the core's deep-merge
    // treats a same-pattern entry as an override of the default at load) plus the
    // explicit removals list. Empty everything ⇒ reset to defaults.
    const cleanRows = nextRows.filter((r) => r.pattern.trim() !== "");
    const rem = nextRemovals.split(",").map((s) => s.trim()).filter(Boolean);
    const payload: Record<string, unknown> = {};
    if (cleanRows.length) payload.additions = cleanRows;
    if (rem.length) payload.removals = rem;
    save.mutate(payload, {
      onSuccess: (res) => {
        if (res?.degraded) toast(tr("写入未生效（内核未接入）。", "Write not applied (core not attached)."), "err");
        else toast(tr("已保存全局检查规则。", "Global check rules saved."), "ok");
      },
      onError: (e) => toast(`${tr("保存失败：", "Save failed: ")}${String((e as Error)?.message ?? e)}`, "err"),
    });
  };

  return (
    <Card>
      <div className="mb-2 flex items-center gap-2">
        <span className="text-sm font-medium text-mast-text">{tr("全局检查规则", "Global check rules")}</span>
        {effQ.data?.has_override ? <Badge tone="INFO">{tr("含覆盖", "With overrides")}</Badge> : <Badge tone="AUTO">{tr("纯默认", "Defaults only")}</Badge>}
      </div>
      <p className="mb-3 text-xs text-mast-muted">
        {tr(
          "每条规则将参数名/单位模式映射到 SafetyLimits 字段。名称和单位同时匹配时触发范围检查。参数名和单位模式使用小写子串匹配。下表为代码默认 _GLOBAL_CHECKS 与覆盖层合并后的",
          "Each rule maps a parameter-name / unit pattern to a SafetyLimits field. A range check fires when both the name and the unit match; patterns are lower-case substring matches. The table below is the ",
        )}
        <strong>{tr("生效规则全集", "complete effective rule set")}</strong>
        {tr(
          "（SafetyGate 实际校验依据），每行标注来源。",
          " (code defaults _GLOBAL_CHECKS merged with the override layer, which is what SafetyGate enforces); each row shows its origin.",
        )}
      </p>

      {/* ── complete effective rule set (all code defaults included) ── */}
      {effQ.isPending && <Spinner />}
      {effQ.error && <ErrorNote error={effQ.error} />}
      {effQ.data?.degraded && <DegradedNote what={tr("全局检查规则", "Global check rules")} />}
      {effQ.data && !effQ.data.degraded && (
        <div className="mb-4">
          <div className="mb-2 text-xs text-mast-muted">
            {tr(
              `共 ${effQ.data.count ?? (effQ.data.rows?.length ?? 0)} 条生效规则`,
              `${effQ.data.count ?? (effQ.data.rows?.length ?? 0)} effective rules`,
            )}
            <span className="ml-2">
              <OriginBadge origin="default" /> {tr("代码默认", "code default")} · <OriginBadge origin="modified" /> {tr("已被覆盖", "overridden")} ·{" "}
              <OriginBadge origin="addition" /> {tr("新增", "added")}
            </span>
          </div>
          <DataTable data={effQ.data.rows ?? []} columns={effColumns} empty={tr("无检查规则", "No check rules")} />
        </div>
      )}

      {/* ── override layer editor (stacked on the defaults) ── */}
      {ovrQ.error && <ErrorNote error={ovrQ.error} />}
      {ovrQ.data?.degraded && <DegradedNote what={tr("全局检查规则覆盖层", "Global check rules override layer")} />}
      {ovrQ.data && !ovrQ.data.degraded && (
        <div className="space-y-2 rounded-md border border-mast-border bg-mast-bg/30 p-3">
          <div className="text-xs font-medium text-mast-text">{tr("覆盖 / 新增规则（叠加在默认之上）", "Override / added rules (stacked on the defaults)")}</div>
          <div className="grid grid-cols-[1.4fr_0.8fr_1.2fr_1.2fr_auto] gap-2 text-xs text-mast-muted">
            <span>{tr("参数名模式", "Parameter name pattern")}</span>
            <span>{tr("单位模式", "Unit pattern")}</span>
            <span>{tr("最小值字段", "Min field")}</span>
            <span>{tr("最大值字段", "Max field")}</span>
            <span />
          </div>
          {view.length === 0 && <EmptyNote label={tr("暂无覆盖/新增规则（当前全部为代码默认）。", "No override / added rules (everything is the code default).")} />}
          {view.map((r, i) => (
            <div key={i} className="grid grid-cols-[1.4fr_0.8fr_1.2fr_1.2fr_auto] items-center gap-2">
              <TextField value={r.pattern} mono onChange={(v) => {
                const next = [...view]; next[i] = { ...r, pattern: v }; setRows(next);
              }} />
              <TextField value={r.unit} onChange={(v) => {
                const next = [...view]; next[i] = { ...r, unit: v }; setRows(next);
              }} />
              <SelectField
                value={r.min_attr}
                onChange={(v) => { const next = [...view]; next[i] = { ...r, min_attr: v }; setRows(next); }}
                options={[{ value: "", label: "—" }, ...limitFields.map((f) => ({ value: f, label: f }))]}
              />
              <SelectField
                value={r.max_attr}
                onChange={(v) => { const next = [...view]; next[i] = { ...r, max_attr: v }; setRows(next); }}
                options={[{ value: "", label: "—" }, ...limitFields.map((f) => ({ value: f, label: f }))]}
              />
              <Button variant="danger" onClick={() => setRows(view.filter((_, j) => j !== i))}>{tr("删除", "Delete")}</Button>
            </div>
          ))}
          <Button variant="default" onClick={() => setRows([...view, { pattern: "", unit: "", min_attr: "", max_attr: "" }])}>
            {tr("+ 新增规则", "+ Add rule")}
          </Button>
          <Field label={tr("移除的代码默认规则（逗号分隔的参数名模式）", "Removed code-default rules (comma-separated name patterns)")}>
            <TextField value={removals} mono onChange={setRemovals} placeholder="bias_v, z_m" />
          </Field>
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <Button variant="primary" disabled={save.isPending} onClick={() => persist(view, removals)}>
              {save.isPending ? tr("保存中…", "Saving…") : tr("保存规则", "Save rules")}
            </Button>
            <Button variant="danger" disabled={save.isPending} onClick={() => persist([], "")}>
              {tr("重置为默认", "Reset to default")}
            </Button>
          </div>
        </div>
      )}
      {node}
    </Card>
  );
}

// ════════════════════════════════════════════════════════════════════════════
// Material bias-limit constraints editor: shows SC_LIST + the full default
// material bias table as the base, with an editable per-material override on top
// ════════════════════════════════════════════════════════════════════════════
type MatLimit = { max_abs_v: number; note: string };
type ConstraintRow = { id: string; category: string; description: string; rule: string; severity?: string; enforcement?: string };
type BiasRow = { id: string; max_abs_v: string; note: string; origin: string };

function useReference(kind: string) {
  return useQuery({
    queryKey: ["knowledge", "reference", kind],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/knowledge/reference/{kind}", {
        params: { path: { kind } },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function ConstraintsEditor() {
  // Read side A: SC_LIST, the advisory material-safety constraint list.
  const scQ = useReference("safety_constraints");
  // Read side B: the default MATERIAL_BIAS_LIMITS table the override merges into.
  const defBiasQ = useReference("material_bias_limits");
  // Write side: the material_bias_limits override.
  const ovrQ = useOverride("constraints");
  const save = useSaveOverride("constraints");
  const { toast, node } = useToast();

  const scList = (scQ.data?.data ?? []) as ConstraintRow[];
  const defBias = (defBiasQ.data?.data ?? {}) as Record<string, MatLimit>;
  const stored = (ovrQ.data?.data ?? {}) as { material_bias_limits?: Record<string, MatLimit> };
  const overrideBias = stored.material_bias_limits ?? {};

  // Editable rows = default bias table merged with overrides (full list visible),
  // each tagged default / modified / addition vs the code default.
  const [rows, setRows] = useState<BiasRow[] | null>(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { setRows(null); }, [ovrQ.data, defBiasQ.data]);

  const baseView: BiasRow[] = useMemo(() => {
    const ids = new Set<string>([...Object.keys(defBias), ...Object.keys(overrideBias)]);
    return [...ids].sort().map((id) => {
      const ovr = overrideBias[id];
      const def = defBias[id];
      const eff = ovr ?? def ?? { max_abs_v: 0, note: "" };
      const origin = !def ? "addition" : ovr ? "modified" : "default";
      return { id, max_abs_v: String(eff.max_abs_v ?? ""), note: eff.note ?? "", origin };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defBiasQ.data, ovrQ.data]);

  const view = rows ?? baseView;

  const scColumns: ColumnDef<ConstraintRow, any>[] = [
    { accessorKey: "id", header: "ID", cell: (c) => <span className="font-mono text-xs">{c.row.original.id}</span> },
    { accessorKey: "category", header: tr("类别", "Category"), cell: (c) => <span className="font-mono text-xs">{c.row.original.category}</span> },
    { accessorKey: "description", header: tr("描述", "Description") },
    { accessorKey: "rule", header: tr("规则", "Rule"), cell: (c) => <span className="font-mono text-xs">{c.row.original.rule}</span> },
    { accessorKey: "severity", header: tr("严重度", "Severity"), cell: (c) => <span className="text-xs">{c.row.original.severity ?? "—"}</span> },
  ];

  const persist = (next: BiasRow[]) => {
    // Only persist rows that DIFFER from the code default (or are net-new). Rows
    // identical to the default stay in the default layer — keeps the override
    // file minimal and lets "reset to default" work via an empty payload.
    const mat: Record<string, MatLimit> = {};
    for (const r of next) {
      const id = r.id.trim();
      if (!id) continue;
      const n = Number(r.max_abs_v);
      if (Number.isNaN(n)) continue;
      const note = r.note.trim();
      const def = defBias[id];
      if (def && def.max_abs_v === n && (def.note ?? "") === note) continue; // unchanged default
      mat[id] = { max_abs_v: n, note };
    }
    const payload = Object.keys(mat).length ? { material_bias_limits: mat } : {};
    save.mutate(payload, {
      onSuccess: (res) => res?.degraded ? toast(tr("写入未生效（内核未接入）。", "Write not applied (core not attached)."), "err") : toast(tr("已保存材料偏压限制。", "Material bias limits saved."), "ok"),
      onError: (e) => toast(`${tr("保存失败：", "Save failed: ")}${String((e as Error)?.message ?? e)}`, "err"),
    });
  };

  return (
    <Card>
      <div className="mb-2 flex items-center gap-2">
        <span className="text-sm font-medium text-mast-text">{tr("材料安全约束", "Material safety constraints")}</span>
        {ovrQ.data?.has_override ? <Badge tone="INFO">{tr("含覆盖", "With overrides")}</Badge> : <Badge tone="AUTO">{tr("纯默认", "Defaults only")}</Badge>}
      </div>

      {/* ── SC_LIST: complete material safety constraint reference (read-only) ── */}
      <p className="mb-2 text-xs text-mast-muted">
        {tr(
          "以下为代码内置的材料/样品安全约束参考全集（顾问性，不与覆盖合并）。",
          "The complete built-in reference of material / sample safety constraints (advisory, not merged with overrides).",
        )}
      </p>
      {scQ.isPending && <Spinner />}
      {scQ.error && <ErrorNote error={scQ.error} />}
      {scQ.data?.degraded && <DegradedNote what={tr("材料安全约束", "Material safety constraints")} />}
      {scQ.data && !scQ.data.degraded && (
        <div className="mb-4">
          <div className="mb-2 text-xs text-mast-muted">{tr(`共 ${scQ.data.count ?? scList.length} 条约束`, `${scQ.data.count ?? scList.length} constraints`)}</div>
          <DataTable data={scList} columns={scColumns} empty={tr("无安全约束", "No safety constraints")} />
        </div>
      )}

      {/* ── material bias limits (defaults + editable override) ── */}
      <p className="mb-2 text-xs text-mast-muted">
        {tr(
          "按样品类型设置最大允许偏压绝对值。下表为默认偏压限制全集，可在其上叠加覆盖（标注 默认 / 已改 / 新增）。",
          "Set the maximum allowed absolute bias per sample type. The table lists all default bias limits; overrides can be stacked on top (marked default / modified / added).",
        )}
      </p>
      {(defBiasQ.isPending || ovrQ.isPending) && <Spinner />}
      {defBiasQ.error && <ErrorNote error={defBiasQ.error} />}
      {ovrQ.error && <ErrorNote error={ovrQ.error} />}
      {ovrQ.data?.degraded && <DegradedNote what={tr("材料偏压限制覆盖层", "Material bias limits override layer")} />}
      {ovrQ.data && !ovrQ.data.degraded && (
        <div className="space-y-2 rounded-md border border-mast-border bg-mast-bg/30 p-3">
          <div className="grid grid-cols-[1.2fr_0.8fr_2fr_0.6fr_auto] gap-2 text-xs text-mast-muted">
            <span>{tr("样品类型", "Sample type")}</span><span>{tr("最大 |V|", "Max |V|")}</span><span>{tr("备注", "Note")}</span><span>{tr("来源", "Origin")}</span><span />
          </div>
          {view.length === 0 && <EmptyNote label={tr("无材料偏压限制。", "No material bias limits.")} />}
          {view.map((r, i) => (
            <div key={i} className="grid grid-cols-[1.2fr_0.8fr_2fr_0.6fr_auto] items-center gap-2">
              <TextField value={r.id} mono onChange={(v) => { const n = [...view]; n[i] = { ...r, id: v }; setRows(n); }} />
              <TextField value={r.max_abs_v} onChange={(v) => { const n = [...view]; n[i] = { ...r, max_abs_v: v }; setRows(n); }} />
              <TextField value={r.note} onChange={(v) => { const n = [...view]; n[i] = { ...r, note: v }; setRows(n); }} />
              <OriginBadge origin={r.origin} />
              <Button variant="danger" onClick={() => setRows(view.filter((_, j) => j !== i))}>{tr("删除", "Delete")}</Button>
            </div>
          ))}
          <Button variant="default" onClick={() => setRows([...view, { id: "", max_abs_v: "", note: "", origin: "addition" }])}>
            {tr("+ 新增材料", "+ Add material")}
          </Button>
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <Button variant="primary" disabled={save.isPending} onClick={() => persist(view)}>
              {save.isPending ? tr("保存中…", "Saving…") : tr("保存材料限制", "Save material limits")}
            </Button>
            <Button variant="danger" disabled={save.isPending} onClick={() => persist([])}>{tr("重置为默认", "Reset to default")}</Button>
          </div>
        </div>
      )}
      {node}
    </Card>
  );
}

// ════════════════════════════════════════════════════════════════════════════
// Per-skill safety-level editor: lists all skills with their default level
// ════════════════════════════════════════════════════════════════════════════
const LEVELS = ["auto", "confirm", "dangerous"] as const;
const LEVEL_TONE: Record<string, string> = { auto: "AUTO", confirm: "WARN", dangerous: "DANGEROUS" };

export function SkillLevelsEditor() {
  const ovrQ = useOverride("skill");
  const save = useSaveOverride("skill");
  const { toast, node } = useToast();
  const [filter, setFilter] = useState("");

  // Skill catalog: all skills with their code-default safety level.
  const catQ = useQuery({
    queryKey: ["skills", "catalog"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/skills/catalog");
      if (error) throw error;
      return data;
    },
  });

  const stored = (ovrQ.data?.data ?? {}) as Record<string, { safety_level?: string }>;

  // The full per-skill effective table: default level (from catalog) + override.
  type SkillLevelRow = { name: string; domain: string; defaultLevel: string; effectiveLevel: string; overridden: boolean };
  const fullView: SkillLevelRow[] = useMemo(() => {
    const idx = catQ.data?.index ?? [];
    return idx
      .map((s) => {
        const def = String(s.safety_level ?? "auto").toLowerCase();
        const ovr = stored[s.name]?.safety_level;
        const eff = ovr ? String(ovr).toLowerCase() : def;
        return { name: s.name, domain: s.domain ?? "", defaultLevel: def, effectiveLevel: eff, overridden: ovr != null };
      })
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [catQ.data, ovrQ.data]);

  const filtered = useMemo(() => {
    const f = filter.trim().toLowerCase();
    if (!f) return fullView;
    return fullView.filter((r) => r.name.toLowerCase().includes(f) || r.domain.toLowerCase().includes(f));
  }, [fullView, filter]);

  // Local edits keyed by skill name (only changed rows are tracked).
  const [edits, setEdits] = useState<Record<string, string>>({});
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { setEdits({}); }, [ovrQ.data]);

  const levelOf = (r: SkillLevelRow) => edits[r.name] ?? r.effectiveLevel;

  const persist = () => {
    // Start from the existing stored override (preserve non-safety_level keys),
    // then apply edits: an edit back to the default removes the override entry.
    const merged: Record<string, Record<string, unknown>> = {};
    for (const [name, v] of Object.entries(stored)) {
      const rest = { ...(v as Record<string, unknown>) };
      const sl = rest.safety_level;
      delete rest.safety_level;
      if (Object.keys(rest).length) merged[name] = rest;
      // keep an existing safety_level override unless an edit changes it below
      if (sl != null) merged[name] = { ...(merged[name] ?? {}), safety_level: sl };
    }
    const byName = new Map(fullView.map((r) => [r.name, r]));
    for (const [name, level] of Object.entries(edits)) {
      const def = byName.get(name)?.defaultLevel;
      if (def != null && level === def) {
        // reverting to default ⇒ drop the safety_level override (keep other keys)
        if (merged[name]) {
          delete merged[name].safety_level;
          if (!Object.keys(merged[name]).length) delete merged[name];
        }
      } else {
        merged[name] = { ...(merged[name] ?? {}), safety_level: level };
      }
    }
    save.mutate(merged, {
      onSuccess: (res) => res?.degraded ? toast(tr("写入未生效（内核未接入）。", "Write not applied (core not attached)."), "err") : toast(tr("已保存技能安全级。", "Skill safety levels saved."), "ok"),
      onError: (e) => toast(`${tr("保存失败：", "Save failed: ")}${String((e as Error)?.message ?? e)}`, "err"),
    });
  };

  const resetAll = () => {
    setEdits({});
    save.mutate({}, {
      onSuccess: (res) => res?.degraded ? toast(tr("写入未生效（内核未接入）。", "Write not applied (core not attached)."), "err") : toast(tr("已重置全部覆盖。", "All overrides reset."), "ok"),
      onError: (e) => toast(`${tr("保存失败：", "Save failed: ")}${String((e as Error)?.message ?? e)}`, "err"),
    });
  };

  const overrideCount = Object.keys(stored).filter((k) => stored[k]?.safety_level != null).length;
  const dirty = Object.keys(edits).length > 0;

  return (
    <Card>
      <div className="mb-2 flex items-center gap-2">
        <span className="text-sm font-medium text-mast-text">{tr("技能安全级别", "Skill safety levels")}</span>
        {overrideCount > 0 ? <Badge tone="INFO">{tr(`${overrideCount} 项覆盖`, `${overrideCount} override(s)`)}</Badge> : <Badge tone="AUTO">{tr("纯默认", "Defaults only")}</Badge>}
      </div>
      <p className="mb-3 text-xs text-mast-muted">
        {tr("下表列出", "The table lists ")}
        <strong>{tr("全部技能", "all skills")}</strong>
        {tr(
          "及其代码默认安全级别，可逐项覆盖。AUTO = 自动执行，CONFIRM = 需确认，DANGEROUS = 需人工审批。改回默认值即取消该技能的覆盖。",
          " with their code-default safety level; each can be overridden. AUTO = runs automatically, CONFIRM = needs confirmation, DANGEROUS = needs manual approval. Setting a skill back to its default removes the override.",
        )}
      </p>

      {(ovrQ.isPending || catQ.isPending) && <Spinner />}
      {ovrQ.error && <ErrorNote error={ovrQ.error} />}
      {catQ.error && <ErrorNote error={catQ.error} />}
      {ovrQ.data?.degraded && <DegradedNote what={tr("技能安全级", "Skill safety levels")} />}
      {catQ.data?.degraded && <DegradedNote what={tr("技能目录", "Skill catalog")} />}

      {ovrQ.data && !ovrQ.data.degraded && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <div className="grow">
              <TextField value={filter} onChange={setFilter} placeholder={tr("搜索技能名称或领域…", "Search skill name or domain…")} />
            </div>
            <span className="whitespace-nowrap text-xs text-mast-muted">
              {tr(`${filtered.length} / ${fullView.length} 个技能`, `${filtered.length} / ${fullView.length} skills`)}
            </span>
          </div>

          <div className="max-h-[60vh] overflow-auto rounded-md border border-mast-border">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-mast-panel text-mast-muted">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">{tr("技能名称", "Skill")}</th>
                  <th className="px-3 py-2 text-left font-medium">{tr("领域", "Domain")}</th>
                  <th className="px-3 py-2 text-left font-medium">{tr("默认级别", "Default level")}</th>
                  <th className="px-3 py-2 text-left font-medium">{tr("生效级别（可改）", "Effective level (editable)")}</th>
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 && (
                  <tr><td colSpan={4} className="px-3 py-3 text-mast-muted">{fullView.length ? tr("无匹配技能。", "No matching skills.") : tr("技能目录为空。", "The skill catalog is empty.")}</td></tr>
                )}
                {filtered.map((r) => {
                  const cur = levelOf(r);
                  const changed = cur !== r.effectiveLevel;
                  const isOverride = changed ? cur !== r.defaultLevel : r.overridden;
                  return (
                    <tr key={r.name} className="border-t border-mast-border hover:bg-mast-bg/40">
                      <td className="px-3 py-2 align-middle">
                        <span className="font-mono text-xs">{r.name}</span>
                        {isOverride && <span className="ml-2"><Badge tone="INFO">{tr("已改", "Modified")}</Badge></span>}
                      </td>
                      <td className="px-3 py-2 align-middle text-xs text-mast-muted">{r.domain || "—"}</td>
                      <td className="px-3 py-2 align-middle">
                        <Badge tone={LEVEL_TONE[r.defaultLevel] ?? "AUTO"}>{r.defaultLevel.toUpperCase()}</Badge>
                      </td>
                      <td className="px-3 py-2 align-middle">
                        <SelectField
                          value={cur}
                          onChange={(v) => setEdits((e) => ({ ...e, [r.name]: v }))}
                          options={LEVELS.map((l) => ({ value: l, label: l.toUpperCase() }))}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="flex flex-wrap items-center gap-2 pt-1">
            <Button variant="primary" disabled={save.isPending || !dirty} onClick={persist}>
              {save.isPending ? tr("保存中…", "Saving…") : dirty ? tr("保存覆盖", "Save override") : tr("无改动", "No changes")}
            </Button>
            <Button variant="danger" disabled={save.isPending} onClick={resetAll}>{tr("重置全部覆盖", "Reset all overrides")}</Button>
          </div>
        </div>
      )}
      {node}
    </Card>
  );
}
