import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { api } from "@/api/client";
import {
  Section,
  Card,
  Badge,
  Spinner,
  ErrorNote,
  DegradedNote,
} from "@/components/ui";
import { Button, Modal, useToast } from "@/components/controls";
import { CompositeDag } from "@/components/skills/CompositeDag";
import { BuilderPalette, type CatalogEntry } from "@/components/builder/BuilderPalette";
import { BuilderCanvas } from "@/components/builder/BuilderCanvas";
import { BuilderInspector, type SkillCard, type ValReport } from "@/components/builder/BuilderInspector";
import {
  type Spec,
  type Addr,
  type NodeKind,
  type Draft,
  emptySpec,
  findIn,
  newNode,
  specInsert,
  specRemove,
  specMove,
  listDrafts,
  writeDraft,
  removeDraft,
  draftKey,
} from "@/components/builder/builderSpec";
import { fuzzyScoreFields } from "@/lib/fuzzy";
import { getLang, tr } from "@/lib/i18n";

// Composite skill builder.
//
// Document format = backend CompositeSpec JSON. Reproduces: skill palette
// (catalog search/facets/favorites/staging) · structured node-tree canvas
// (step/if/loop/set/llm/human/agent) · per-node inspector with typed param forms
// · read-only DAG preview (CompositeDag) · design-time validate · open stored
// composite · version history + restore · clone-as-blueprint · local drafts.
//
// API surface: open / validate / publish / save (new version) / delete / clone /
// restore all go through /api/composites (versioned via _history, with optimistic
// concurrency). AI generation (NL -> spec) and sharing are not exposed here.
// Edits can also be kept as local drafts (localStorage, per-browser). Clone
// (server) is the supported way to fork an existing composite into a new blueprint.

const ADD_KINDS: { kind: NodeKind; label: string }[] = [
  { kind: "if", label: tr("⑂ if 分支", "⑂ if branch") },
  { kind: "loop", label: tr("↻ loop 循环", "↻ loop") },
  { kind: "try", label: tr("⛨ try/finally（清理必跑）", "⛨ try/finally (cleanup always runs)") },
  { kind: "break", label: tr("⤓ break 跳出循环", "⤓ break (exit loop)") },
  { kind: "continue", label: tr("⥁ continue 下一轮", "⥁ continue (next iteration)") },
  { kind: "succeed", label: tr("✓ succeed 提前成功", "✓ succeed (finish early as success)") },
  { kind: "fail", label: tr("✗ fail 提前失败", "✗ fail (finish early as failure)") },
  { kind: "llm", label: tr("🤖 llm 决策（受限路由/结构化输出）", "🤖 llm decision (constrained routing / structured output)") },
  { kind: "human", label: tr("✋ human 人工决策（暂停等用户）", "✋ human decision (pause for the operator)") },
  { kind: "agent", label: tr("🤝 agent 委托（文献/分析/写作）", "🤝 agent delegate (literature / analysis / writing)") },
  { kind: "set", label: tr("≔ set 变量", "≔ set variable") },
];

const FAV_KEY = "mast_builder_favorites";

function loadFavs(): string[] {
  try {
    const d = JSON.parse(localStorage.getItem(FAV_KEY) || "[]");
    return Array.isArray(d) ? d.map(String) : [];
  } catch {
    return [];
  }
}

export default function BuilderPage() {
  const qc = useQueryClient();
  const { toast, node: toastNode } = useToast();

  // ── catalog (builder palette index — richer than /api/skills/catalog: carries
  //    zh / category / level / tags) ──
  const catalogQ = useQuery({
    queryKey: ["builder", "catalog"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/builder/catalog");
      if (error) throw error;
      return data;
    },
  });
  const catalog = (catalogQ.data?.skills ?? []) as CatalogEntry[];

  // ── stored composites (open picker) ──
  const compositesQ = useQuery({
    queryKey: ["composites"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/composites");
      if (error) throw error;
      return data;
    },
  });
  const workflows = compositesQ.data?.composites ?? [];

  // ── editor state ──
  const [spec, setSpecRaw] = useState<Spec>(emptySpec(""));
  const [baseVersion, setBaseVersion] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [selId, setSelId] = useState<string | null>(null);
  const [report, setReport] = useState<ValReport | null>(null);
  const [versions, setVersions] = useState<{ version: number; saved_at: string; n_nodes: number; description: string }[]>([]);
  const [staging, setStaging] = useState<string[]>([]);
  const [favorites, setFavorites] = useState<string[]>(loadFavs);

  // ── fullscreen ── the dense node-tree builder needs room; toggle an
  //    in-app fixed inset-0 overlay (no Fullscreen API → keeps app chrome/toasts,
  //    and never freezes if the browser denies the request). Esc exits.
  const [fullscreen, setFullscreen] = useState(false);
  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFullscreen(false);
    };
    document.addEventListener("keydown", onKey);
    // lock body scroll while the overlay owns the viewport
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [fullscreen]);

  const specRef = useRef(spec);
  specRef.current = spec;
  const draftTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // skill-card cache (fetched on demand for the inspector / hover)
  const cardsRef = useRef<Record<string, SkillCard>>({});
  const [, forceCards] = useState(0);
  const cardOf = useCallback((name: string) => cardsRef.current[name] || null, []);
  const ensureCard = useCallback(async (name: string) => {
    if (!name || cardsRef.current[name]) return cardsRef.current[name] || null;
    try {
      const { data } = await api.GET("/api/skills/{name}", { params: { path: { name } } });
      if (data && (data as SkillCard).name) {
        cardsRef.current[name] = data as SkillCard;
        forceCards((x) => x + 1);
        return data as SkillCard;
      }
    } catch {
      /* degrade silently */
    }
    return null;
  }, []);

  // fetch card for the selected step
  useEffect(() => {
    if (!selId) return;
    const hit = findIn(spec.nodes, selId, null);
    if (hit && hit.node.type === "step" && hit.node.skill) ensureCard(String(hit.node.skill));
  }, [selId, spec.nodes, ensureCard]);

  const setSpec = useCallback(
    (s: Spec) => {
      setSpecRaw(s);
      setDirty(true);
      setReport(null);
      if (draftTimer.current) clearTimeout(draftTimer.current);
      draftTimer.current = setTimeout(() => {
        writeDraft(s, baseVersion, staging);
        setDrafts(listDrafts());
      }, 600);
    },
    [baseVersion, staging],
  );

  // ── local drafts ──
  const [drafts, setDrafts] = useState<Draft[]>(() => listDrafts());

  // ── add-node menu ──
  const [menu, setMenu] = useState<{ addr: Addr; index: number | null } | null>(null);

  // ── hover card popup ──
  const [hover, setHover] = useState<{ name: string; x: number; y: number; card: SkillCard } | null>(null);
  const onHover = useCallback(
    (name: string | null, ev?: React.MouseEvent) => {
      if (!name || !ev) return setHover(null);
      const x = Math.min(ev.clientX + 16, window.innerWidth - 340);
      const y = Math.min(ev.clientY + 8, window.innerHeight - 260);
      ensureCard(name).then((card) => card && setHover({ name, x, y, card }));
    },
    [ensureCard],
  );

  // errors keyed by node id (from validate report)
  const errorsById = useMemo(() => {
    const m: Record<string, { errors: string[]; warnings: string[] }> = {};
    for (const s of report?.steps || []) m[s.id] = { errors: s.errors, warnings: s.warnings };
    return m;
  }, [report]);

  // ── node operations ──
  const addNode = useCallback(
    (kind: NodeKind, skillName: string | undefined, addr: Addr, index: number | null) => {
      if (kind === "step" && skillName) ensureCard(skillName);
      setSpec(specInsert(specRef.current, addr, index, newNode(specRef.current, kind, skillName)));
      setMenu(null);
    },
    [setSpec, ensureCard],
  );
  const onAddSkill = useCallback((name: string) => addNode("step", name, { containerId: null, slot: "root" }, null), [addNode]);
  const onDropSkill = useCallback((name: string, addr: Addr) => addNode("step", name, addr, null), [addNode]);
  // control-flow nodes (if/loop/set/llm/human/agent) added from the palette —
  // click adds at root end; drag drops into a specific slot/addr.
  const onAddKind = useCallback((kind: NodeKind) => addNode(kind, undefined, { containerId: null, slot: "root" }, null), [addNode]);
  const onDropKind = useCallback((kind: NodeKind, addr: Addr) => addNode(kind, undefined, addr, null), [addNode]);
  const onMove = useCallback((id: string, d: number) => setSpec(specMove(specRef.current, id, d)), [setSpec]);
  const onRemove = useCallback(
    (id: string) => {
      setSpec(specRemove(specRef.current, id));
      if (selId === id) setSelId(null);
    },
    [setSpec, selId],
  );
  const onAddInto = useCallback((addr: Addr) => setMenu({ addr, index: null }), []);
  const onAddAfter = useCallback((id: string) => {
    const hit = findIn(specRef.current.nodes, id, null);
    if (!hit) return;
    const addr: Addr = hit.owner ? { containerId: hit.owner.node.id as string, slot: hit.owner.slot } : { containerId: null, slot: "root" };
    setMenu({ addr, index: hit.index + 1 });
  }, []);

  // ── favorites (local-only; no /api endpoint) ──
  const toggleFav = useCallback((name: string) => {
    setFavorites((f) => {
      const next = f.includes(name) ? f.filter((x) => x !== name) : [...f, name];
      try {
        localStorage.setItem(FAV_KEY, JSON.stringify(next));
      } catch {
        /* quota */
      }
      return next;
    });
  }, []);

  // ── open / new ──
  const openWorkflow = useCallback(
    async (name: string) => {
      try {
        const { data, error } = await api.GET("/api/composites/{name}", { params: { path: { name } } });
        if (error) throw error;
        if (!data?.found || !data.spec) {
          toast(tr(`未找到工作流：${name}`, `Workflow not found: ${name}`), "err");
          return;
        }
        const s = { ...emptySpec(name), ...(data.spec as Spec) };
        setSpecRaw(s);
        setBaseVersion(Number((data.spec as Spec).version || 0));
        setVersions((data.versions || []) as typeof versions);
        setDirty(false);
        setReport(null);
        setSelId(null);
        setWfOpen(false);
      } catch (e) {
        toast(tr(`打开失败：${e instanceof Error ? e.message : e}`, `Open failed: ${e instanceof Error ? e.message : e}`), "err");
      }
    },
    [toast],
  );

  const newWorkflow = useCallback(() => {
    setSpecRaw(emptySpec(""));
    setBaseVersion(0);
    setVersions([]);
    setDirty(false);
    setReport(null);
    setSelId(null);
    setStaging([]);
  }, []);

  // ── drafts ops ──
  const saveDraft = useCallback(() => {
    const s = specRef.current;
    if (draftTimer.current) clearTimeout(draftTimer.current);
    writeDraft(s, baseVersion, staging);
    setDrafts(listDrafts());
    toast(
      tr(
        `已存为本地草稿${s.name ? `「${s.name}」` : "（未命名）"}——仅本机、未注册为技能。`,
        `Saved as local draft${s.name ? ` "${s.name}"` : " (untitled)"} — this machine only, not registered as a skill.`,
      ),
      "ok",
    );
  }, [baseVersion, staging, toast]);

  const openDraft = useCallback(
    (d: Draft) => {
      setSpecRaw(d.spec);
      setBaseVersion(d.baseVersion || 0);
      setStaging(d.staging || []);
      setVersions([]);
      setDirty(true);
      setReport(null);
      setSelId(null);
      setDfOpen(false);
      toast(
        tr(
          `已载入本地草稿${d.name && d.name !== "__new__" ? `「${d.name}」` : "（未命名）"}。`,
          `Loaded local draft${d.name && d.name !== "__new__" ? ` "${d.name}"` : " (untitled)"}.`,
        ),
        "ok",
      );
    },
    [toast],
  );

  const clearCurrentDraft = useCallback(() => {
    const s = specRef.current;
    const hasContent = (s.nodes && s.nodes.length) || s.name;
    if (hasContent && !window.confirm(tr("清空当前画布并删除其本地草稿？（已发布的技能版本不受影响）", "Clear the current canvas and delete its local draft? (Published skill versions are not affected.)"))) return;
    if (draftTimer.current) clearTimeout(draftTimer.current);
    removeDraft(draftKey(s.name));
    removeDraft(draftKey(""));
    newWorkflow();
    setDrafts(listDrafts());
    toast(tr("已清空当前草稿。", "Current draft cleared."), "ok");
  }, [newWorkflow, toast]);

  // ── validate (server) ──
  const validateMut = useMutation({
    mutationFn: async () => {
      const name = specRef.current.name || "__draft__";
      const { data, error } = await api.POST("/api/composites/{name}/validate", {
        params: { path: { name } },
        body: { spec: specRef.current as unknown as Record<string, unknown> },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      setReport(d as ValReport);
      if ((d as ValReport & { degraded?: boolean }).degraded) toast(tr("校验后端不可用（degraded）", "Validation backend unavailable (degraded)"), "err");
      else toast(d?.ok ? tr("校验通过 ✓", "Validation passed ✓") : tr("校验未通过，详见右栏", "Validation failed; see the right panel"), d?.ok ? "ok" : "err");
    },
    onError: (e) => toast(e instanceof Error ? e.message : tr("校验失败", "Validation failed"), "err"),
  });

  // ── restore (server) ──
  const restoreMut = useMutation({
    mutationFn: async (version: number) => {
      const name = specRef.current.name;
      if (!name) throw new Error(tr("当前工作流未命名/未发布，无法回滚", "The current workflow is unnamed / unpublished and cannot be rolled back"));
      const { data, error } = await api.POST("/api/composites/{name}/restore/{version}", {
        params: { path: { name, version } },
      });
      if (error) throw error;
      if (!data?.ok) throw new Error(data?.error || tr("回滚失败", "Rollback failed"));
      return data;
    },
    onSuccess: (d, version) => {
      toast(d?.message || tr(`已回滚到 v${version}`, `Rolled back to v${version}`), "ok");
      qc.invalidateQueries({ queryKey: ["composites"] });
      if (specRef.current.name) openWorkflow(specRef.current.name);
    },
    onError: (e) => toast(e instanceof Error ? e.message : tr("回滚失败", "Rollback failed"), "err"),
  });

  // ── clone (server) ──
  const [cloneOpen, setCloneOpen] = useState(false);
  const [cloneName, setCloneName] = useState("");
  const [cloneAuthor, setCloneAuthor] = useState("");
  const cloneMut = useMutation({
    mutationFn: async () => {
      const src = specRef.current.name;
      if (!src || !baseVersion) throw new Error(tr("先打开一个已发布的工作流再克隆", "Open a published workflow before cloning"));
      if (!cloneName.trim()) throw new Error(tr("请填写新技能名称", "Enter a name for the new skill"));
      const { data, error } = await api.POST("/api/composites/{name}/clone", {
        params: { path: { name: src } },
        body: { new_name: cloneName.trim(), author: cloneAuthor.trim() },
      });
      if (error) throw error;
      if (!data?.ok) throw new Error(data?.error || tr("克隆失败", "Clone failed"));
      return data;
    },
    onSuccess: (d) => {
      setCloneOpen(false);
      setCloneName("");
      setCloneAuthor("");
      qc.invalidateQueries({ queryKey: ["composites"] });
      toast(d?.message || tr("已克隆", "Cloned"), "ok");
      if (d?.name) openWorkflow(d.name);
    },
    onError: (e) => toast(e instanceof Error ? e.message : tr("克隆失败", "Clone failed"), "err"),
  });

  // ── publish / save (server) — POST /api/composites (new) or PUT (update) ──
  type WriteResp = {
    ok?: boolean; version?: number; error?: string; message?: string;
    already_exists?: boolean; version_conflict?: boolean; stored_version?: number;
  };
  const publishMut = useMutation({
    mutationFn: async () => {
      const s = specRef.current;
      if (!s.name?.trim()) throw new Error(tr("请先填写工作流名称再发布", "Enter a workflow name before publishing"));
      if (baseVersion > 0) {
        const { data, error } = await api.PUT("/api/composites/{name}", {
          params: { path: { name: s.name } },
          body: { spec: s as unknown as Record<string, unknown>, base_version: baseVersion },
        });
        if (error) throw error;
        const d = data as WriteResp;
        if (!d?.ok) throw new Error(d?.error || (d?.version_conflict ? tr(`版本冲突（服务器为 v${d?.stored_version}），请重新打开后再保存`, `Version conflict (server has v${d?.stored_version}); reopen the workflow and save again`) : tr("保存失败", "Save failed")));
        return d;
      }
      const { data, error } = await api.POST("/api/composites", {
        body: { name: s.name, spec: s as unknown as Record<string, unknown> },
      });
      if (error) throw error;
      const d = data as WriteResp;
      if (!d?.ok) throw new Error(d?.error || (d?.already_exists ? tr("同名复合技能已存在（请改名，或打开它后用「保存」更新）", "A composite skill with this name already exists (rename it, or open it and use Save to update)") : tr("发布失败", "Publish failed")));
      return d;
    },
    onSuccess: (d) => {
      toast(d?.message || (baseVersion > 0 ? tr(`已保存 v${d?.version ?? ""}`, `Saved v${d?.version ?? ""}`) : tr(`已发布 v${d?.version ?? ""}`, `Published v${d?.version ?? ""}`)), "ok");
      setDirty(false);
      if (d?.version) setBaseVersion(d.version);
      qc.invalidateQueries({ queryKey: ["composites"] });
    },
    onError: (e) => toast(e instanceof Error ? e.message : tr("发布失败", "Publish failed"), "err"),
  });

  // ── delete (server) — DELETE /api/composites/{name} (history retained) ──
  const deleteMut = useMutation({
    mutationFn: async () => {
      const name = specRef.current.name;
      if (!name || baseVersion === 0) throw new Error(tr("仅可删除已发布的工作流", "Only published workflows can be deleted"));
      const { data, error } = await api.DELETE("/api/composites/{name}", { params: { path: { name } } });
      if (error) throw error;
      const d = data as WriteResp;
      if (!d?.ok) throw new Error(d?.error || tr("删除失败", "Delete failed"));
      return d;
    },
    onSuccess: (d) => {
      toast(d?.message || tr("已删除（历史快照保留）", "Deleted (history snapshots kept)"), "ok");
      qc.invalidateQueries({ queryKey: ["composites"] });
      setSpecRaw(emptySpec(""));
      setBaseVersion(0);
      setDirty(false);
      setSelId(null);
    },
    onError: (e) => toast(e instanceof Error ? e.message : tr("删除失败", "Delete failed"), "err"),
  });

  // dropdown open states + outside-click
  const [wfOpen, setWfOpen] = useState(false);
  const [dfOpen, setDfOpen] = useState(false);
  const [wfQ, setWfQ] = useState("");
  const wfBox = useRef<HTMLDivElement>(null);
  const dfBox = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (wfBox.current && !wfBox.current.contains(e.target as Node)) setWfOpen(false);
      if (dfBox.current && !dfBox.current.contains(e.target as Node)) setDfOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);
  // CN/EN fuzzy search across name + description + tags; ranked by score.
  const wfFiltered = useMemo(() => {
    const q = wfQ.trim();
    if (!q) return workflows;
    return workflows
      .map((w) => ({
        w,
        score: fuzzyScoreFields(q, w.name, w.description, (w.tags || []).join(" ")),
      }))
      .filter((x) => x.score > 0)
      .sort((a, b) => b.score - a.score)
      .map((x) => x.w);
  }, [workflows, wfQ]);

  return (
    <Section title={tr("技能构建器", "Skill builder")}>
      {toastNode}

      {/* save / publish / delete are wired; only AI generation and sharing are not exposed */}
      <Card className="mb-3 border-mast-border">
        <p className="text-xs text-mast-muted">
          {getLang() === "zh" ? (
            <>
              支持：<b>打开 / 校验 / 发布 / 保存（新版本）/ 删除 / 克隆 / 回滚</b>（全部经 <code>/api/composites</code>，
              带 <code>_history</code> 版本与乐观并发）。仅 <b>AI 生成（NL→spec）/ 分享到实验室</b> 仍为
              live-only（需在线模型 / 云推送），未在此暴露。编辑可随时存为<b>本地草稿</b>（仅本机浏览器）。
            </>
          ) : (
            <>
              Supported: <b>open / validate / publish / save (new version) / delete / clone / roll back</b> (all via{" "}
              <code>/api/composites</code>, with <code>_history</code> versions and optimistic concurrency). Only{" "}
              <b>AI generation (NL→spec) / sharing to the lab</b> are not exposed here (they need an online model /
              cloud push). Edits can be kept at any time as a <b>local draft</b> (this browser only).
            </>
          )}
        </p>
      </Card>

      {/* topbar */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="font-semibold text-mast-text">{tr("⛏ 工作流", "⛏ Workflow")}</span>

        {/* open workflow picker */}
        <div className="relative" ref={wfBox}>
          <Button onClick={() => { setWfOpen((o) => !o); setWfQ(""); }}>
            {tr("打开工作流", "Open workflow")} ▾{workflows.length ? <span className="ml-1 text-mast-muted">{workflows.length}</span> : null}
          </Button>
          {wfOpen && (
            <div className="absolute z-30 mt-1 w-72 rounded-md border border-mast-border bg-mast-panel p-1 shadow-xl">
              <input
                autoFocus
                value={wfQ}
                onChange={(e) => setWfQ(e.target.value)}
                placeholder={tr(
                  `搜索 ${workflows.length} 个工作流（中/英/模糊，匹配名称+描述+标签）…`,
                  `Search ${workflows.length} workflows (fuzzy; matches name + description + tags)…`,
                )}
                className="mb-1 w-full rounded border border-mast-border bg-mast-bg px-2 py-1 text-sm outline-none focus:border-mast-accent"
              />
              <div className="max-h-72 overflow-auto">
                {wfFiltered.length ? (
                  wfFiltered.map((w) => (
                    <div
                      key={w.name}
                      onClick={() => openWorkflow(w.name)}
                      className="cursor-pointer rounded px-2 py-1 text-sm hover:bg-mast-bg"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate">{w.name}</span>
                        <span className="shrink-0 text-xs text-mast-muted">v{w.version}</span>
                      </div>
                      {w.description && (
                        <div className="truncate text-[11px] text-mast-muted/80" title={w.description}>
                          {w.description}
                        </div>
                      )}
                    </div>
                  ))
                ) : (
                  <div className="px-2 py-1 text-xs text-mast-muted">{tr("无匹配工作流", "No matching workflows")}</div>
                )}
              </div>
            </div>
          )}
        </div>

        {/* drafts picker */}
        <div className="relative" ref={dfBox}>
          <Button onClick={() => { setDfOpen((o) => !o); setDrafts(listDrafts()); }}>
            {tr("草稿", "Drafts")} ▾{drafts.length ? <span className="ml-1 text-mast-muted">{drafts.length}</span> : null}
          </Button>
          {dfOpen && (
            <div className="absolute z-30 mt-1 w-72 rounded-md border border-mast-border bg-mast-panel p-1 shadow-xl">
              <div className="max-h-72 overflow-auto">
                {drafts.length ? (
                  drafts.map((d) => (
                    <div key={d.key} className="flex items-center gap-1 rounded px-2 py-1 text-sm hover:bg-mast-bg">
                      <span className="flex-1 cursor-pointer truncate" onClick={() => openDraft(d)} title={tr("载入此草稿", "Load this draft")}>
                        {d.name === "__new__" ? tr("（未命名草稿）", "(untitled draft)") : d.name}
                        <span className="ml-1 text-xs text-mast-muted">{tr(`${d.nodes} 节点`, `${d.nodes} nodes`)}</span>
                      </span>
                      <span
                        className="cursor-pointer text-mast-muted hover:text-mast-danger"
                        title={tr("删除此草稿", "Delete this draft")}
                        onClick={() => {
                          removeDraft(d.key);
                          setDrafts(listDrafts());
                        }}
                      >
                        ✕
                      </span>
                    </div>
                  ))
                ) : (
                  <div className="px-2 py-1 text-xs text-mast-muted">{tr("无本地草稿", "No local drafts")}</div>
                )}
              </div>
            </div>
          )}
        </div>

        <Button onClick={newWorkflow}>{tr("新建", "New")}</Button>
        <Button onClick={clearCurrentDraft}>{tr("清空草稿", "Clear draft")}</Button>

        <span className="font-mono text-sm text-mast-accent">{spec.name || tr("（未命名）", "(untitled)")}</span>
        {baseVersion ? <Badge>v{baseVersion}</Badge> : <Badge tone="WARN">{tr("未发布", "Unpublished")}</Badge>}
        {dirty && <span title={tr("有未保存改动", "Unsaved changes")} className="h-2 w-2 rounded-full bg-mast-warn" />}

        <span className="flex-1" />

        <Button variant="default" disabled={validateMut.isPending} onClick={() => validateMut.mutate()}>
          {validateMut.isPending ? tr("校验中…", "Validating…") : tr("校验", "Validate")}
        </Button>
        <Button
          variant="primary"
          disabled={publishMut.isPending || !spec.name?.trim()}
          onClick={() => publishMut.mutate()}
        >
          {publishMut.isPending ? tr("发布中…", "Publishing…") : baseVersion > 0 ? tr("💾 保存（新版本）", "💾 Save (new version)") : tr("🚀 发布为技能", "🚀 Publish as skill")}
        </Button>
        <Button
          variant="default"
          disabled={!baseVersion}
          onClick={() => {
            setCloneName(spec.name ? `${spec.name}${tr("_副本", "_copy")}` : "");
            setCloneAuthor("");
            setCloneOpen(true);
          }}
        >
          {tr("克隆为新技能", "Clone as new skill")}
        </Button>
        {baseVersion > 0 && (
          <Button
            variant="danger"
            disabled={deleteMut.isPending}
            onClick={() => {
              if (window.confirm(tr(`删除复合技能「${spec.name}」？（历史快照保留）`, `Delete composite skill "${spec.name}"? (History snapshots are kept.)`))) deleteMut.mutate();
            }}
          >
            {tr("删除", "Delete")}
          </Button>
        )}
        <Button variant="default" onClick={saveDraft}>{tr("💾 保存草稿", "💾 Save draft")}</Button>
        <Button variant="default" onClick={() => setFullscreen((f) => !f)}>
          {fullscreen ? tr("🗗 退出全屏", "🗗 Exit fullscreen") : tr("⛶ 全屏", "⛶ Fullscreen")}
        </Button>
      </div>

      {/* degraded / loading notes */}
      {catalogQ.isError && <ErrorNote error={catalogQ.error} />}
      {catalogQ.data?.degraded && <DegradedNote what={tr("技能目录", "skill catalog")} />}
      {compositesQ.data?.degraded && <DegradedNote what={tr("复合技能列表", "composite skill list")} />}

      {/* main 3-pane layout — wrapped so it can fill the viewport in fullscreen.
          In fullscreen: fixed inset-0 overlay, panes grow to fill height. */}
      <div
        className={clsx(
          fullscreen &&
            "fixed inset-0 z-40 flex flex-col gap-3 overflow-hidden bg-mast-bg p-4",
        )}
      >
        {fullscreen && (
          <div className="flex shrink-0 items-center gap-2">
            <span className="font-semibold text-mast-text">{tr("⛏ 技能构建器 · 全屏", "⛏ Skill builder · fullscreen")}</span>
            <span className="font-mono text-sm text-mast-accent">{spec.name || tr("（未命名）", "(untitled)")}</span>
            {baseVersion ? <Badge>v{baseVersion}</Badge> : <Badge tone="WARN">{tr("未发布", "Unpublished")}</Badge>}
            {dirty && <span title={tr("有未保存改动", "Unsaved changes")} className="h-2 w-2 rounded-full bg-mast-warn" />}
            <span className="flex-1" />
            <Button variant="default" disabled={validateMut.isPending} onClick={() => validateMut.mutate()}>
              {validateMut.isPending ? tr("校验中…", "Validating…") : tr("校验", "Validate")}
            </Button>
            <Button variant="default" onClick={saveDraft}>{tr("💾 保存草稿", "💾 Save draft")}</Button>
            <Button variant="primary" onClick={() => setFullscreen(false)}>
              {tr("🗗 退出全屏（Esc）", "🗗 Exit fullscreen (Esc)")}
            </Button>
          </div>
        )}
        <div
          className={clsx(
            "grid gap-3 lg:grid-cols-[280px_1fr_360px]",
            fullscreen && "min-h-0 flex-1",
          )}
        >
          <Card className={clsx("overflow-hidden p-3", fullscreen ? "h-full min-h-0" : "h-[640px]")}>
            <BuilderPalette
              catalog={catalog}
              favorites={favorites}
              onToggleFav={toggleFav}
              staging={staging}
              setStaging={setStaging}
              onAddSkill={onAddSkill}
              onAddAtEnd={() => setMenu({ addr: { containerId: null, slot: "root" }, index: null })}
              onAddKind={onAddKind}
              onHover={onHover}
              loading={catalogQ.isPending}
            />
          </Card>

          <div className={clsx("flex flex-col gap-3", fullscreen ? "h-full min-h-0" : "h-[640px]")}>
            <div className="min-h-0 flex-1">
              <BuilderCanvas
                spec={spec}
                selId={selId}
                errorsById={errorsById}
                onSelect={(id) => setSelId(id || null)}
                onMove={onMove}
                onRemove={onRemove}
                onAddInto={onAddInto}
                onAddAfter={onAddAfter}
                onDropSkill={onDropSkill}
                onDropKind={onDropKind}
              />
            </div>
          </div>

          <Card className={clsx("overflow-auto p-3", fullscreen ? "h-full min-h-0" : "h-[640px]")}>
            <BuilderInspector
              spec={spec}
              setSpec={setSpec}
              selId={selId}
              cardOf={cardOf}
              report={report}
              onFocusNode={(id) => setSelId(id)}
            />
          </Card>
        </div>
      </div>

      {/* DAG preview + version history below */}
      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        <Card>
          <h4 className="mb-2 text-sm font-medium text-mast-text">{tr("控制流 DAG（只读预览）", "Control-flow DAG (read-only preview)")}</h4>
          <CompositeDag spec={spec} />
        </Card>
        <Card>
          <h4 className="mb-2 text-sm font-medium text-mast-text">{tr("版本历史", "Version history")}</h4>
          {!versions.length ? (
            <p className="text-sm text-mast-muted">
              {baseVersion ? tr("暂无版本历史", "No version history yet") : tr("打开一个已发布的工作流后显示其版本历史。", "Open a published workflow to see its version history.")}
            </p>
          ) : (
            <div className="overflow-auto rounded border border-mast-border">
              <table className="w-full text-xs">
                <thead className="bg-mast-bg text-mast-muted">
                  <tr>
                    <th className="px-2 py-1.5 text-left">{tr("版本", "Version")}</th>
                    <th className="px-2 py-1.5 text-left">{tr("保存时间", "Saved at")}</th>
                    <th className="px-2 py-1.5 text-left">{tr("节点数", "Nodes")}</th>
                    <th className="px-2 py-1.5 text-left">{tr("操作", "Actions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {versions.map((v) => (
                    <tr key={v.version} className="border-t border-mast-border">
                      <td className="px-2 py-1.5 font-medium">v{v.version}</td>
                      <td className="px-2 py-1.5 text-mast-muted">{(v.saved_at || "—").slice(0, 19)}</td>
                      <td className="px-2 py-1.5 tabular-nums text-mast-muted">{v.n_nodes}</td>
                      <td className="px-2 py-1.5">
                        <button
                          disabled={restoreMut.isPending}
                          onClick={() => restoreMut.mutate(v.version)}
                          className="rounded border border-mast-border px-2 py-0.5 text-xs text-mast-accent hover:bg-mast-accent/10 disabled:opacity-50"
                        >
                          {tr("回滚到此", "Roll back to this")}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      {/* add-node menu modal */}
      <Modal open={!!menu} onClose={() => setMenu(null)} title={tr("添加节点", "Add node")}>
        {menu && (
          <div className="space-y-1">
            <p className="text-xs text-mast-muted">
              {tr("添加到", "Add to")}{" "}
              {menu.addr.containerId
                ? `${menu.addr.containerId}.${menu.addr.slot}`
                : menu.index != null
                  ? tr("（插入到所选之后）", "(insert after the selected node)")
                  : tr("末尾", "the end")}
            </p>
            {!!staging.length && (
              <div>
                <div className="px-1 py-1 text-xs text-mast-muted">{tr("🧺 候选技能", "🧺 Staged skills")}</div>
                {staging.map((s) => (
                  <button
                    key={s}
                    onClick={() => addNode("step", s, menu.addr, menu.index)}
                    className="block w-full rounded px-2 py-1 text-left text-sm hover:bg-mast-bg"
                  >
                    step · {s}
                  </button>
                ))}
              </div>
            )}
            {!!favorites.length && (
              <div>
                <div className="px-1 py-1 text-xs text-mast-muted">{tr("★ 收藏技能", "★ Favorite skills")}</div>
                {favorites.slice(0, 8).map((s) => (
                  <button
                    key={s}
                    onClick={() => addNode("step", s, menu.addr, menu.index)}
                    className="block w-full rounded px-2 py-1 text-left text-sm hover:bg-mast-bg"
                  >
                    step · {s}
                  </button>
                ))}
              </div>
            )}
            <div className="my-1 border-t border-mast-border" />
            {ADD_KINDS.map((k) => (
              <button
                key={k.kind}
                onClick={() => addNode(k.kind, undefined, menu.addr, menu.index)}
                className="block w-full rounded px-2 py-1 text-left text-sm hover:bg-mast-bg"
              >
                {k.label}
              </button>
            ))}
            <p className="px-1 pt-1 text-xs text-mast-muted">{tr("（更多技能：在左侧搜索后双击添加，或先 🧺 存入候选）", "(More skills: search on the left and double-click to add, or stage them with 🧺 first)")}</p>
          </div>
        )}
      </Modal>

      {/* clone modal */}
      <Modal open={cloneOpen} onClose={() => setCloneOpen(false)} title={tr(`克隆为新复合技能（源：${spec.name}）`, `Clone as new composite skill (source: ${spec.name})`)}>
        <div className="space-y-3">
          <label className="block text-sm">
            <span className="text-mast-muted">{tr("新技能名称", "New skill name")}</span>
            <input
              value={cloneName}
              onChange={(e) => setCloneName(e.target.value)}
              placeholder="MyNewWorkflow"
              className="mt-1 w-full rounded-md border border-mast-border bg-mast-bg px-2.5 py-1.5 text-sm outline-none focus:border-mast-accent"
            />
            <span className="text-xs text-mast-muted/80">{tr("将作为新蓝本（v1），可在“打开工作流”中选择并继续编辑", "Becomes a new blueprint (v1); pick it under “Open workflow” to keep editing")}</span>
          </label>
          <label className="block text-sm">
            <span className="text-mast-muted">{tr("作者（可选）", "Author (optional)")}</span>
            <input
              value={cloneAuthor}
              onChange={(e) => setCloneAuthor(e.target.value)}
              className="mt-1 w-full rounded-md border border-mast-border bg-mast-bg px-2.5 py-1.5 text-sm outline-none focus:border-mast-accent"
            />
          </label>
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" onClick={() => setCloneOpen(false)}>{tr("取消", "Cancel")}</Button>
            <Button variant="primary" disabled={cloneMut.isPending} onClick={() => cloneMut.mutate()}>
              {cloneMut.isPending ? tr("克隆中…", "Cloning…") : tr("克隆", "Clone")}
            </Button>
          </div>
        </div>
      </Modal>

      {/* hover card popup */}
      {hover && (
        <div
          className="pointer-events-none fixed z-50 w-80 rounded-lg border border-mast-border bg-mast-panel p-3 shadow-2xl"
          style={{ left: hover.x, top: hover.y }}
        >
          <div className="font-semibold text-mast-text">{hover.name}</div>
          {hover.card.description && <div className="mt-1 text-xs text-mast-muted">{hover.card.description}</div>}
          {!!hover.card.parameters?.length && (
            <table className="mt-2 w-full text-xs">
              <tbody>
                {hover.card.parameters.slice(0, 8).map((p) => (
                  <tr key={p.name}>
                    <td className="py-0.5 pr-2 font-mono text-mast-text">{p.name}</td>
                    <td className="py-0.5 text-mast-muted">
                      {p.type}
                      {p.unit ? ` (${p.unit})` : ""}
                      {p.min != null ? ` [${p.min},${p.max}]` : ""}
                      {p.required ? " *" : ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {hover.card.extra && (hover.card.extra as any).when_use ? (
            <div className="mt-1 text-xs text-mast-muted">{tr("适用：", "Use when: ")}{String((hover.card.extra as any).when_use)}</div>
          ) : null}
        </div>
      )}

      {compositesQ.isPending && <Spinner />}
    </Section>
  );
}
