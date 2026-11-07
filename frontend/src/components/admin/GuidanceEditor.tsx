import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Badge, Card, DegradedNote, EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { Button, Field, SubTabs, TextField, useToast } from "@/components/controls";
import { JsonSectionEditor } from "@/components/admin/JsonSectionEditor";
import { tr } from "@/lib/i18n";

/** Skill guidance: per-skill expert annotations (SKILL_EXTRA): when_use / when_not /
 *  related / nanonis / example. Read+write via GET/POST /api/admin/guidance/{skill}.
 *  The skill list comes from /api/skills/catalog. Empty payload ⇒ reset to code default.
 *
 *  Plus the three whole-section guidance kinds: decision_trees / recipes / templates,
 *  via GET/POST
 *  /api/admin/guidance-extra/{kind} (effective default⊕override + override JSON
 *  editor, reusing JsonSectionEditor). A top mode-switch picks between the
 *  per-skill annotations and the section editors. */

type GuidanceFields = {
  when_use: string;
  when_not: string;
  related: string;
  nanonis: string;
  example: string;
};

type GuidanceMode = "annotations" | "extra";

const GUIDANCE_MODES: { id: GuidanceMode; label: string }[] = [
  { id: "annotations", label: tr("逐技能标注", "Per-skill annotations") },
  { id: "extra", label: tr("决策树 / 配方 / 模板", "Decision trees / recipes / templates") },
];

const EXTRA_KINDS: { id: string; label: string; desc: string }[] = [
  {
    id: "decision_trees",
    label: tr("决策树 (decision_trees)", "Decision trees"),
    desc: tr(
      "分诊决策树（DECISION_TREES）——按 kind 整段覆盖；留空=恢复代码默认。",
      "Triage decision trees (DECISION_TREES). The whole kind is overridden at once; empty = restore the code default.",
    ),
  },
  {
    id: "recipes",
    label: tr("配方 (recipes)", "Recipes"),
    desc: tr(
      "实验配方列表（RECIPES）——按 kind 整段覆盖；留空=恢复代码默认。",
      "Experiment recipes (RECIPES). The whole kind is overridden at once; empty = restore the code default.",
    ),
  },
  {
    id: "templates",
    label: tr("模板 (templates)", "Templates"),
    desc: tr(
      "指导模板（TEMPLATES）——按 kind 整段覆盖；留空=恢复代码默认。",
      "Guidance templates (TEMPLATES). The whole kind is overridden at once; empty = restore the code default.",
    ),
  },
];

function TextArea({ value, onChange, rows = 3, mono }: { value: string; onChange: (v: string) => void; rows?: number; mono?: boolean }) {
  return (
    <textarea
      value={value}
      rows={rows}
      spellCheck={false}
      onChange={(e) => onChange(e.target.value)}
      className={
        "w-full resize-y rounded-md border border-mast-border bg-mast-bg px-2.5 py-1.5 text-sm text-mast-text outline-none focus:border-mast-accent " +
        (mono ? "font-mono text-xs" : "")
      }
    />
  );
}

export function GuidanceEditor() {
  const [mode, setMode] = useState<GuidanceMode>("annotations");
  return (
    <div className="space-y-4">
      <SubTabs tabs={GUIDANCE_MODES} value={mode} onChange={setMode} />
      {mode === "annotations" && <AnnotationsEditor />}
      {mode === "extra" && <GuidanceExtraEditor />}
    </div>
  );
}

// ── Decision trees / recipes / templates: whole-section override editors ──────
function GuidanceExtraEditor() {
  const [kind, setKind] = useState(EXTRA_KINDS[0]!.id);
  const cur = EXTRA_KINDS.find((k) => k.id === kind)!;
  return (
    <div>
      <div className="mb-3 flex flex-wrap gap-2">
        {EXTRA_KINDS.map((k) => (
          <button
            key={k.id}
            onClick={() => setKind(k.id)}
            className={
              "rounded-md border px-3 py-1.5 text-sm " +
              (kind === k.id
                ? "border-mast-accent bg-mast-accent/15 text-mast-accent"
                : "border-mast-border text-mast-muted hover:text-mast-text")
            }
          >
            {k.label}
          </button>
        ))}
      </div>
      <JsonSectionEditor
        key={kind}
        title={`${tr("技能指导", "Skill guidance")} · ${cur.label}`}
        description={cur.desc}
        getPath="/api/admin/guidance-extra/{kind}"
        postPath="/api/admin/guidance-extra/{kind}"
        pathParam={{ kind }}
        queryKey={["guidance-extra", kind]}
      />
    </div>
  );
}

// ── Per-skill expert annotations (SKILL_EXTRA) ────────────────────────────────
function AnnotationsEditor() {
  const queryClient = useQueryClient();
  const { toast, node } = useToast();
  const [filter, setFilter] = useState("");
  const [skill, setSkill] = useState<string | null>(null);

  const catQ = useQuery({
    queryKey: ["skills", "catalog"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/skills/catalog");
      if (error) throw error;
      return data;
    },
  });

  const guidQ = useQuery({
    queryKey: ["guidance", skill],
    enabled: !!skill,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/admin/guidance/{skill}", {
        params: { path: { skill: skill! } },
      });
      if (error) throw error;
      return data;
    },
  });

  const names = useMemo(() => {
    const f = filter.trim().toLowerCase();
    const all = (catQ.data?.index ?? []).map((s) => s.name).sort();
    return f ? all.filter((n) => n.toLowerCase().includes(f)) : all;
  }, [catQ.data, filter]);

  const effective = (guidQ.data?.data ?? {}) as Partial<GuidanceFields>;
  const [draft, setDraft] = useState<GuidanceFields | null>(null);
  useEffect(() => { setDraft(null); }, [guidQ.data, skill]);

  const view: GuidanceFields = draft ?? {
    when_use: effective.when_use ?? "",
    when_not: effective.when_not ?? "",
    related: effective.related ?? "",
    nanonis: effective.nanonis ?? "",
    example: effective.example ?? "",
  };

  const save = useMutation({
    mutationFn: async (payload: GuidanceFields | Record<string, never>) => {
      const { data, error } = await api.POST("/api/admin/guidance/{skill}", {
        params: { path: { skill: skill! } },
        body: { data: payload } as never,
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      setDraft(null);
      queryClient.invalidateQueries({ queryKey: ["guidance", skill] });
      res?.degraded ? toast(tr("写入未生效（内核未接入）。", "Write not applied (core not attached)."), "err") : toast(tr("已保存技能指导。", "Skill guidance saved."), "ok");
    },
    onError: (e) => toast(`${tr("保存失败：", "Save failed: ")}${String((e as Error)?.message ?? e)}`, "err"),
  });

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[280px_1fr]">
      <Card>
        <div className="mb-2 text-sm font-medium text-mast-text">{tr("选择技能", "Select a skill")}</div>
        <TextField value={filter} onChange={setFilter} placeholder={tr("搜索技能…", "Search skills…")} />
        {catQ.isPending && <Spinner />}
        {catQ.error && <ErrorNote error={catQ.error} />}
        {catQ.data?.degraded && <DegradedNote what={tr("技能目录", "Skill catalog")} />}
        {catQ.data && !catQ.data.degraded && (
          names.length === 0 ? <EmptyNote label={tr("无匹配技能", "No matching skills")} /> : (
            <ul className="mt-2 max-h-[60vh] space-y-0.5 overflow-auto pr-1">
              {names.map((n) => (
                <li key={n}>
                  <button
                    onClick={() => setSkill(n)}
                    className={
                      "w-full truncate rounded-md px-2 py-1.5 text-left font-mono text-xs " +
                      (skill === n ? "bg-mast-accent/15 text-mast-accent" : "text-mast-text hover:bg-mast-bg/60")
                    }
                  >
                    {n}
                  </button>
                </li>
              ))}
            </ul>
          )
        )}
      </Card>

      <Card>
        {!skill && <EmptyNote label={tr("从左侧选择一个技能编辑其指导信息。", "Select a skill on the left to edit its guidance.")} />}
        {skill && guidQ.isPending && <Spinner />}
        {skill && guidQ.error && <ErrorNote error={guidQ.error} />}
        {skill && guidQ.data?.degraded && <DegradedNote what={tr("技能指导", "Skill guidance")} />}
        {skill && guidQ.data && !guidQ.data.degraded && (
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <h3 className="font-mono text-sm font-semibold text-mast-text">{skill}</h3>
              {guidQ.data.has_override ? <Badge tone="INFO">{tr("已覆盖", "Overridden")}</Badge> : <Badge tone="AUTO">{tr("默认", "Default")}</Badge>}
            </div>
            <Field label={tr("何时使用 (when_use)", "When to use (when_use)")}>
              <TextArea value={view.when_use} onChange={(v) => setDraft({ ...view, when_use: v })} />
            </Field>
            <Field label={tr("何时不使用 (when_not)", "When not to use (when_not)")}>
              <TextArea value={view.when_not} onChange={(v) => setDraft({ ...view, when_not: v })} />
            </Field>
            <Field label={tr("关联技能 (related)", "Related skills (related)")}>
              <TextField value={view.related} onChange={(v) => setDraft({ ...view, related: v })} />
            </Field>
            <Field label={tr("底层 Nanonis TCP 方法 (nanonis)", "Underlying Nanonis TCP methods (nanonis)")}>
              <TextField value={view.nanonis} mono onChange={(v) => setDraft({ ...view, nanonis: v })} />
            </Field>
            <Field label={tr("示例 JSON 调用 (example)", "Example JSON call (example)")}>
              <TextArea value={view.example} rows={5} mono onChange={(v) => setDraft({ ...view, example: v })} />
            </Field>
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate(view)}>
                {save.isPending ? tr("保存中…", "Saving…") : tr("保存", "Save")}
              </Button>
              {draft && <Button variant="ghost" onClick={() => setDraft(null)}>{tr("撤销编辑", "Discard edits")}</Button>}
              <Button variant="danger" disabled={save.isPending} onClick={() => save.mutate({})}>
                {tr("重置为默认值", "Reset to default")}
              </Button>
            </div>
          </div>
        )}
      </Card>
      {node}
    </div>
  );
}
