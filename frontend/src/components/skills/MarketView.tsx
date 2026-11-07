import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ColumnDef } from "@tanstack/react-table";
import { api } from "@/api/client";
import { DataTable } from "@/components/DataTable";
import {
  Section,
  Card,
  Badge,
  Spinner,
  ErrorNote,
  DegradedNote,
  EmptyNote,
} from "@/components/ui";
import {
  Button,
  Modal,
  RadioGroup,
  SelectField,
  TextField,
  Toggle,
  useToast,
} from "@/components/controls";
import { useWsEvent } from "@/hooks/useWsEvents";
import { Field } from "@/components/controls";
import { PendingRecommendations } from "./PendingRecommendations";
import {
  canUnsubscribe,
  diffManifestAgainstCatalog,
  liveBadge,
  marketWriteProblem,
  parseSubscriptionManifest,
  pendingBadgeCount,
  locallyAuthoredUnsubscribed,
  type MarketWriteResult,
  type SubscriptionManifest,
} from "@/lib/skillMarket";
import { tr } from "@/lib/i18n";

// ── Skill market — all skills are the market, the subscription list is the subset in use ──
//
// Like the overlay panel, the page's first job is to answer "is it in effect now?". Changing the
// subscription only changes a holder, while the agent's tool table is frozen when the graph is
// built, so after a successful write the model may still hold the old table (a task is running
// and the rebuild is queued; the rebuild failed; there is no live runtime). All three paths
// return ok:true, so the UI follows agent_path_pending / fingerprint_matches rather than `ok`,
// and shows the backend's rebuild_note verbatim (the three cases are worded differently and
// rewriting them here would collapse them into one).
//
// The second job is to state that a subscription is not a safety mechanism: an unsubscribed
// skill can still be run manually from the direct-call page and can still be reached by composite
// substeps and conduct. If the UI did not say so, users would treat it as a permission gate.

type MarketRow = {
  name: string;
  zh: string;
  category: string;
  safety: string;
  level: number;
  source: string;
  source_zh: string;
  tags: string[];
  domain: string;
  subscribed: boolean;
  mandatory: boolean;
  pending_rec_id: string;
};

type Status = {
  customised: boolean;
  subscribed_count: number;
  market_total: number;
  mandatory: string[];
  missing_entries: string[];
  unreadable: string;
  pending_count: number;
  store_path: string;
  agent_path_pending: boolean | null;
  fingerprint_matches: boolean | null;
  degraded: boolean;
  reason: string;
};

type Rec = {
  id: string;
  skill: string;
  by_agent: string;
  reason: string;
  at: string;
  status: string;
  resolved_at: string | null;
};

const SAFETY_TONE: Record<string, string> = {
  auto: "AUTO",
  confirm: "WARN",
  dangerous: "DANGEROUS",
};

function useStatus() {
  return useQuery({
    queryKey: ["skill-market", "status"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/skill-market/status");
      if (error) throw error;
      return data as unknown as Status;
    },
  });
}

function useMarket(view: "all" | "on" | "off") {
  return useQuery({
    queryKey: ["skill-market", "catalog", view],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/skill-market/catalog", {
        params: { query: { subscribed: view === "all" ? "" : view === "on" ? "1" : "0" } },
      });
      if (error) throw error;
      return data as unknown as {
        total: number;
        skills: MarketRow[];
        customised: boolean;
        degraded: boolean;
        reason: string;
      };
    },
  });
}

function useRecommendations() {
  return useQuery({
    queryKey: ["skill-market", "recommendations"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/skill-market/recommendations");
      if (error) throw error;
      return data as unknown as { pending: Rec[]; resolved: Rec[]; degraded: boolean };
    },
  });
}

/** Effect-status bar: the authoritative three-state display. A toast has only two levels and cannot hold it. */
function EffectBanner({ st }: { st: Status }) {
  const live = liveBadge(st.fingerprint_matches);
  const tone = st.agent_path_pending ? "WARN" : live.tone === "ok" ? "AUTO" : "INFO";
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <Badge tone={tone}>
          {st.agent_path_pending ? tr("agent 侧尚未跟上", "Agent side not caught up yet") : tr(`agent 工具表：${live.text}`, `Agent tool table: ${live.text}`)}
        </Badge>
        <span className="text-mast-muted">
          {st.customised
            ? tr(`已定制：${st.subscribed_count} / ${st.market_total} 个技能在装载面上`, `Customised: ${st.subscribed_count} / ${st.market_total} skills loaded`)
            : tr(`未定制：全部 ${st.market_total} 个技能都在装载面上（出厂默认）`, `Not customised: all ${st.market_total} skills loaded (factory default)`)}
        </span>
        {st.missing_entries.length > 0 && (
          <Badge tone="INFO">
            {tr(`${st.missing_entries.length} 个订阅条目在本机找不到对应技能`, `${st.missing_entries.length} subscription entries have no matching skill on this machine`)}
          </Badge>
        )}
      </div>
      {st.unreadable && (
        <p className="mt-2 text-sm text-mast-warn">
          {tr(
            `⚠ 订阅文件读不出来（${st.unreadable}）—— 现在按全订阅在跑，不是空订阅。修好 ${st.store_path} 后重载即可。`,
            `⚠ The subscription file is unreadable (${st.unreadable}). It is currently running as a full subscription, not an empty one. Reload after fixing ${st.store_path}.`,
          )}
        </p>
      )}
      <p className="mt-2 text-xs text-mast-muted">
        {tr("订阅决定的是 agent 工具表里有什么，", "A subscription decides what is in the agent's tool table; it is ")}
        <b>{tr("不是权限", "not a permission")}</b>
        {tr(
          "：未订阅的技能仍可在「技能直调」手动执行，也仍会被复合技能的子步与 conduct 调用。",
          ". An unsubscribed skill can still be run manually from direct skill calls, and is still called by composite substeps and conduct.",
        )}
      </p>
    </Card>
  );
}

/** Resolved recommendations. Rejections stay listed too, so a past rejection remains visible. */
function ResolvedRecommendations({ recs }: { recs: Rec[] }) {
  if (recs.length === 0) return null;
  return (
    <Section title={tr(`已处理的推荐（${recs.length}）`, `Resolved recommendations (${recs.length})`)}>
      <div className="flex flex-col gap-1 text-sm">
        {recs.slice().reverse().map((r) => (
          <div key={r.id} className="flex flex-wrap items-center gap-2">
            <Badge tone={r.status === "accepted" ? "AUTO" : "default"}>
              {r.status === "accepted" ? tr("已接受", "Accepted") : tr("已拒绝", "Rejected")}
            </Badge>
            <code className="text-xs">{r.skill}</code>
            <span className="text-xs text-mast-muted">
              {r.by_agent || "agent"} · {r.resolved_at || r.at}
            </span>
            {r.reason && <span className="text-xs text-mast-faint">{r.reason}</span>}
          </div>
        ))}
      </div>
    </Section>
  );
}

/** Change history of the subscription set.
 *
 * This component is the only consumer of the audit stream; without it the audit log would be
 * written but never read.
 */
const AUDIT_LABEL: Record<string, string> = {
  materialise: tr("固化为明确名单", "Materialised as explicit list"),
  subscribe: tr("加入", "Added"),
  unsubscribe: tr("移出", "Removed"),
  set: tr("整体替换", "Replaced"),
  reset: tr("恢复全订阅", "Reset to full subscription"),
  accepted: tr("接受推荐", "Accepted recommendation"),
  rejected: tr("拒绝推荐", "Rejected recommendation"),
};

function AuditTrail() {
  const [open, setOpen] = useState(false);
  const q = useQuery({
    queryKey: ["skill-market", "audit"],
    enabled: open,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/skill-market/audit");
      if (error) throw error;
      return data as unknown as {
        entries: { at: string; action: string; skills: string[]; via: string }[];
        degraded: boolean;
        reason: string;
      };
    },
  });

  return (
    <Section
      title={tr("变更史", "Change history")}
      subtitle={tr("谁在什么时候动了订阅面、经的哪条路。reset 会清掉定制，但不清这份记录。", "Who changed the subscription set, when, and through which route. Reset clears the customisation but not this record.")}
      actions={<Button onClick={() => setOpen(!open)}>{open ? tr("收起", "Collapse") : tr("查看", "View")}</Button>}
    >
      {open && (
        <Card>
          {q.isPending && <Spinner />}
          {q.error && <ErrorNote error={q.error} />}
          {q.data?.degraded && <DegradedNote what={tr("变更史", "change history")} />}
          {q.data && !q.data.degraded && q.data.entries.length === 0 && (
            <EmptyNote label={tr("还没有改动过订阅面", "The subscription set has not been changed yet")} />
          )}
          <div className="flex flex-col gap-1 text-xs">
            {(q.data?.entries ?? []).slice().reverse().map((a, i) => (
              <div key={`${a.at}-${i}`} className="flex flex-wrap items-center gap-2">
                <span className="text-mast-faint">{a.at}</span>
                <Badge tone="default">{AUDIT_LABEL[a.action] ?? a.action}</Badge>
                {a.skills.length > 0 && (
                  <span className="text-mast-muted">
                    {a.skills.slice(0, 6).join(tr("、", ", "))}
                    {a.skills.length > 6 ? tr(` …共 ${a.skills.length} 个`, ` … ${a.skills.length} in total`) : ""}
                  </span>
                )}
                <span className="flex-1" />
                <span className="text-mast-faint">{a.via}</span>
              </div>
            ))}
          </div>
        </Card>
      )}
    </Section>
  );
}

/** Lab central index: publish your own subscription list, or browse what others published. */
function LabShare({ onFetched }: { onFetched: (m: SubscriptionManifest) => void }) {
  const { toast, node: toastNode } = useToast();
  const [label, setLabel] = useState("");
  const [note, setNote] = useState("");
  const [browsing, setBrowsing] = useState(false);

  const index = useQuery({
    queryKey: ["skill-market", "lab-index"],
    enabled: browsing,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/skill-market/lab-index");
      if (error) throw error;
      return data as unknown as {
        subscriptions: {
          id: string; label: string; note: string; machine: string;
          skill_count: number; embedded_specs: number; status: string; ts_server: string;
        }[];
        degraded: boolean;
        reason: string;
      };
    },
  });

  const publish = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/skill-market/share/publish", {
        body: { label, note },
      });
      if (error) throw error;
      return data as unknown as { ok: boolean; reason: string; id: string; skill_count: number };
    },
    onSuccess: (d) => {
      // Show the server's rejection reason verbatim: it carries the "code does not travel with
      // the manifest" rule, and collapsing it into "publish failed" would let users repeat the mistake.
      if (!d.ok) { toast(d.reason || tr("发布失败", "Publish failed"), "err"); return; }
      toast(tr(`已发布（${d.id}，${d.skill_count} 个技能），等管理员审核`, `Published (${d.id}, ${d.skill_count} skills), pending admin review`), "ok");
      setLabel(""); setNote("");
    },
    onError: (e: any) => toast(String(e?.message || e), "err"),
  });

  const fetchOne = useMutation({
    mutationFn: async (id: string) => {
      const { data, error } = await api.GET("/api/skill-market/lab-fetch/{sub_id}", {
        params: { path: { sub_id: id } },
      });
      if (error) throw error;
      return data as unknown as {
        ok: boolean; reason: string; manifest: SubscriptionManifest | null;
      };
    },
    onSuccess: (d) => {
      // Failure has its own field; success is not inferred from whether some manifest bit is empty.
      if (!d.ok || !d.manifest) { toast(d.reason || tr("取回失败", "Fetch failed"), "err"); return; }
      // Fetching is not importing: replacing the working set needs an explicit click (as with the
      // overlay, writing the manifest is an edit and making it take effect is a decision), so this
      // only loads it into the import preview.
      onFetched(d.manifest);
    },
    onError: (e: any) => toast(String(e?.message || e), "err"),
  });

  return (
    <Section
      title={tr("实验室中心索引", "Lab central index")}
      subtitle={tr("把你的订阅列表分享给同组的人，或看看别人在用哪些技能", "Share your subscription list with your group, or see which skills others use")}
      actions={
        <Button onClick={() => setBrowsing(!browsing)}>
          {browsing ? tr("收起", "Collapse") : tr("浏览别人的", "Browse others")}
        </Button>
      }
    >
      {toastNode}
      <Card>
        <div className="flex flex-wrap items-end gap-2">
          <div className="w-48">
            <Field label={tr("名字", "Name")}><TextField value={label} onChange={setLabel}
              placeholder={tr("如「qPlus 日常」", "e.g. \"qPlus daily\"")} /></Field>
          </div>
          <div className="min-w-0 flex-1">
            <Field label={tr("说明", "Description")}><TextField value={note} onChange={setNote}
              placeholder={tr("一句话说明这份列表适合谁", "One line on who this list suits")} /></Field>
          </div>
          <Button variant="primary" loading={publish.isPending}
                  onClick={() => publish.mutate()}>{tr("发布我的订阅", "Publish my subscription")}</Button>
        </div>
        <p className="mt-2 text-xs text-mast-muted">
          {tr("发布的是", "What is published is ")}<b>{tr("本机导出的那一份", "the copy exported from this machine")}</b>
          {tr(
            "（与「导出订阅」逐字节相同）。代码（.py）不随单走 —— 只有用户组合技能会带上它的纯数据 spec。",
            " (byte-identical to \"Export subscription\"). Code (.py) does not travel with the manifest; only user composite skills carry their plain-data spec.",
          )}
        </p>
      </Card>

      {browsing && (
        <div className="mt-3">
          {index.isPending && <Spinner />}
          {index.error && <ErrorNote error={index.error} />}
          {index.data?.degraded && (
            <Card><p className="text-sm text-mast-warn">{index.data.reason}</p></Card>
          )}
          {index.data && !index.data.degraded && index.data.subscriptions.length === 0 && (
            <EmptyNote label={tr("中心索引里还没有订阅列表", "The central index has no subscription lists yet")} />
          )}
          <div className="flex flex-col gap-2">
            {(index.data?.subscriptions ?? []).slice().reverse().map((s) => (
              <Card key={s.id}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <b className="text-sm">{s.label || s.id}</b>
                      <Badge tone="INFO">{tr(`${s.skill_count} 个技能`, `${s.skill_count} skills`)}</Badge>
                      {s.embedded_specs > 0 && (
                        <Badge tone="default">{tr(`含 ${s.embedded_specs} 个组合`, `includes ${s.embedded_specs} composites`)}</Badge>
                      )}
                    </div>
                    <p className="mt-0.5 text-xs text-mast-muted">
                      {s.note || tr("（没写说明）", "(no description)")} · {s.machine} · {s.ts_server}
                    </p>
                  </div>
                  <Button disabled={fetchOne.isPending}
                          onClick={() => fetchOne.mutate(s.id)}>{tr("取回并预览", "Fetch and preview")}</Button>
                </div>
              </Card>
            ))}
          </div>
        </div>
      )}
    </Section>
  );
}

export function MarketView() {
  const qc = useQueryClient();
  const { toast, node: toastNode } = useToast();
  const [view, setView] = useState<"all" | "on" | "off">("all");
  const [search, setSearch] = useState("");
  const [source, setSource] = useState("");
  const [note, setNote] = useState("");
  const [confirmReset, setConfirmReset] = useState(false);
  const [importPreview, setImportPreview] = useState<SubscriptionManifest | null>(null);
  const [importMode, setImportMode] = useState<"replace" | "merge">("replace");
  const fileRef = useRef<HTMLInputElement | null>(null);

  const st = useStatus();
  const cat = useMarket(view);
  const recs = useRecommendations();

  // Frames only act as a trigger: refetch on receipt rather than accumulating a list from frames
  // (the bus replays only 100).
  useWsEvent("skill_recommendation", () => {
    qc.invalidateQueries({ queryKey: ["skill-market", "recommendations"] });
    qc.invalidateQueries({ queryKey: ["skill-market", "catalog"] });
  });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["skill-market"] });
    qc.invalidateQueries({ queryKey: ["skills", "catalog"] });
    qc.invalidateQueries({ queryKey: ["agents", "tools"] });
  };

  /** Every write goes through here afterwards, so the criterion lives in one place (lib/skillMarket). */
  const settle = (d: MarketWriteResult | undefined) => {
    const problem = marketWriteProblem(d);
    // Show the backend sentence verbatim: the three "not in effect" cases are worded differently.
    setNote(d?.rebuild_note || problem?.message || "");
    refresh();
    if (!problem) {
      toast(d?.rebuild_note || tr("已生效", "In effect"), "ok");
      return;
    }
    // The toast only has ok/err levels, which cannot express three states. pending / unsure always
    // use err: it is better to look heavy than to have "scheduled" read as "done". The
    // authoritative three-state display is the EffectBanner above.
    toast(problem.message, "err");
  };

  const write = useMutation({
    mutationFn: async (v: { subscribe?: string[]; unsubscribe?: string[] }) => {
      const { data, error } = await api.POST("/api/skill-market/subscription", {
        body: { subscribe: v.subscribe ?? [], unsubscribe: v.unsubscribe ?? [] },
      });
      if (error) throw error;
      return data as unknown as MarketWriteResult;
    },
    onSuccess: settle,
    onError: (e: any) => toast(String(e?.message || e), "err"),
  });

  const reset = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/skill-market/subscription/reset", {});
      if (error) throw error;
      return data as unknown as MarketWriteResult;
    },
    onSuccess: (d) => {
      setConfirmReset(false);
      settle(d);
    },
    onError: (e: any) => toast(String(e?.message || e), "err"),
  });

  // Resolving recommendations has a single implementation (`PendingRecommendations`), shared by
  // the chat page and this page, so toasts and cache invalidation stay consistent.

  const doImport = useMutation({
    mutationFn: async (v: { manifest: SubscriptionManifest; dry: boolean }) => {
      const { data, error } = await api.POST("/api/skill-market/import", {
        body: { manifest: v.manifest as any, mode: importMode, dry_run: v.dry },
      });
      if (error) throw error;
      return data as unknown as MarketWriteResult & { report?: any; dry_run?: boolean };
    },
    onSuccess: (d) => {
      if (d?.dry_run) {
        const r = d.report ?? {};
        toast(tr(`预览：可用 ${r.matched?.length ?? 0} 个，缺 ${r.missing?.length ?? 0} 个`, `Preview: ${r.matched?.length ?? 0} available, ${r.missing?.length ?? 0} missing`), "ok");
        return;
      }
      setImportPreview(null);
      settle(d);
    },
    onError: (e: any) => toast(String(e?.message || e), "err"),
  });

  const onPickFile = async (f: File | null) => {
    if (!f) return;
    const parsed = parseSubscriptionManifest(await f.text());
    if ("error" in parsed) {
      toast(parsed.error, "err");
      return;
    }
    setImportPreview(parsed.manifest);
  };

  const exportNow = async () => {
    const { data, error } = await api.GET("/api/skill-market/export");
    if (error) {
      toast(tr("导出失败", "Export failed"), "err");
      return;
    }
    // Download through a temporary <a download> link.
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "mast-skill-subscription.json";
    a.click();
    URL.revokeObjectURL(url);
  };

  const rows = useMemo(() => {
    const all = cat.data?.skills ?? [];
    if (!source) return all;
    return all.filter((r) => r.source === source);
  }, [cat.data, source]);

  const sources = useMemo(
    () => Array.from(new Set((cat.data?.skills ?? []).map((r) => r.source))).sort(),
    [cat.data],
  );

  const columns = useMemo<ColumnDef<MarketRow, any>[]>(
    () => [
      {
        header: tr("订阅", "Subscribed"),
        accessorKey: "subscribed",
        cell: ({ row }) => {
          const r = row.original;
          const locked = !canUnsubscribe(r);
          return (
            <div className="flex items-center gap-2">
              <Toggle
                checked={r.subscribed}
                label={tr(`订阅 ${r.name}`, `Subscribe ${r.name}`)}
                onChange={(v) => {
                  if (locked && !v) {
                    toast(tr(`${r.name} 是必装技能，不能退订 —— 它保证工具面上永远有「停下来 / 退针」可用`, `${r.name} is a mandatory skill and cannot be unsubscribed: it guarantees that "stop / withdraw tip" is always available`), "err");
                    return;
                  }
                  write.mutate(v ? { subscribe: [r.name] } : { unsubscribe: [r.name] });
                }}
              />
              {locked && <Badge tone="INFO">{tr("必装", "Mandatory")}</Badge>}
            </div>
          );
        },
      },
      { header: tr("技能", "Skill"), accessorKey: "name",
        cell: ({ row }) => (
          <div className="min-w-0">
            <code className="text-sm">{row.original.name}</code>
            {row.original.zh && (
              <div className="text-xs text-mast-muted">{row.original.zh}</div>
            )}
          </div>
        ) },
      { header: tr("领域", "Domain"), accessorKey: "domain" },
      { header: tr("来源", "Source"), accessorKey: "source_zh" },
      {
        header: tr("安全级", "Safety"),
        accessorKey: "safety",
        cell: ({ getValue }) => {
          const v = String(getValue() ?? "");
          return <Badge tone={SAFETY_TONE[v] ?? "default"}>{v.toUpperCase() || "?"}</Badge>;
        },
      },
      {
        header: tr("推荐", "Recommended"),
        accessorKey: "pending_rec_id",
        cell: ({ getValue }) =>
          getValue() ? <Badge tone="WARN">{tr("待确认", "Pending")}</Badge> : null,
      },
    ],
    [write, toast],
  );

  if (st.isLoading) return <Spinner />;
  if (st.error) return <ErrorNote error={st.error} />;
  if (st.data?.degraded) return <DegradedNote what={tr("技能市场", "skill market")} />;

  const pendingRecs = recs.data?.pending ?? [];
  // Derived view: skills created locally that are not on the tool surface (criterion lives in lib, covered by node --test).
  const localNew = useMemo(
    () => locallyAuthoredUnsubscribed(cat.data?.skills ?? []),
    [cat.data],
  );
  const missing = st.data?.missing_entries ?? [];

  return (
    <div className="flex flex-col gap-4">
      {toastNode}
      {st.data && <EffectBanner st={st.data} />}
      {note && (
        <Card>
          <p className="text-sm">
            <span className="text-mast-muted">{tr("上一次操作：", "Last action: ")}</span>
            {note}
          </p>
        </Card>
      )}

      {/* Skills created on this machine that are not on the tool surface. After the subscription
          has been customised, a composite newly built by the agent in the skill workshop enters
          the market but not the tool surface, and nothing tells the user it appeared. This is a
          derived view, not a gate: what is missing is visibility (a gate would turn the tool
          surface back into a safety boundary, which it is not). */}
      {localNew.length > 0 && (
        <Section
          title={tr(`本机新出现、尚未在工具面上（${localNew.length}）`, `New on this machine, not on the tool surface yet (${localNew.length})`)}
          subtitle={tr("用户/agent 在这台机器上造的技能。它们不在 agent 的工具表里，但仍可手动执行、也仍可被工作流调用。", "Skills created on this machine by users or agents. They are not in the agent's tool table, but can still be run manually and called by workflows.")}
          actions={
            <Button
              variant="primary"
              disabled={write.isPending}
              onClick={() => write.mutate({ subscribe: localNew.map((r) => r.name) })}
            >
              {tr("全部加入工具面", "Add all to the tool surface")}
            </Button>
          }
        >
          <Card>
            <div className="flex flex-col gap-1 text-sm">
              {localNew.map((r) => (
                <div key={r.name} className="flex flex-wrap items-center gap-2">
                  <code className="text-xs">{r.name}</code>
                  {r.zh && <span className="text-xs text-mast-muted">{r.zh}</span>}
                  <Badge tone="INFO">{r.source_zh || r.source}</Badge>
                  <span className="flex-1" />
                  <Button disabled={write.isPending}
                          onClick={() => write.mutate({ subscribe: [r.name] })}>
                    {tr("加入", "Add")}
                  </Button>
                </div>
              ))}
            </div>
          </Card>
        </Section>
      )}

      {missing.length > 0 && (
        <Section
          title={tr(`订阅里有、本机却没有的条目（${missing.length}）`, `Entries in the subscription but not on this machine (${missing.length})`)}
          subtitle={tr("技能集合是动态的（覆盖层卸载 / 包被移除 / 自建技能停用），所以这些名字刻意保留不剔 —— 但要让你看得见是哪几个。", "The skill set is dynamic (overlay unloaded / pack removed / custom skill disabled), so these names are kept on purpose, but listed so you can see which ones.")}
          actions={
            <Button
              disabled={write.isPending}
              onClick={() => write.mutate({ unsubscribe: missing })}
            >
              {tr("从订阅里移除这些", "Remove these from the subscription")}
            </Button>
          }
        >
          <Card>
            <div className="flex flex-wrap gap-2 text-xs">
              {missing.map((n) => <code key={n}>{n}</code>)}
            </div>
          </Card>
        </Section>
      )}

      {pendingRecs.length > 0 && (
        <Section title={tr(`待确认的推荐（${pendingRecs.length}）`, `Pending recommendations (${pendingRecs.length})`)}>
          <PendingRecommendations />
        </Section>
      )}
      <ResolvedRecommendations recs={recs.data?.resolved ?? []} />
      <AuditTrail />
      <LabShare onFetched={(m) => { setImportMode("replace"); setImportPreview(m); }} />

      <Section
        title={tr(`市场（${cat.data?.total ?? 0}）`, `Market (${cat.data?.total ?? 0})`)}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={exportNow}>{tr("导出订阅", "Export subscription")}</Button>
            <Button onClick={() => fileRef.current?.click()}>{tr("导入订阅", "Import subscription")}</Button>
            <Button variant="danger" onClick={() => setConfirmReset(true)}
                    disabled={!st.data?.customised}>
              {tr("恢复全订阅", "Reset to full subscription")}
            </Button>
          </div>
        }
      >
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          className="hidden"
          onChange={(e) => {
            void onPickFile(e.target.files?.[0] ?? null);
            e.target.value = "";
          }}
        />
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <RadioGroup
            value={view}
            onChange={setView}
            options={[
              { value: "all", label: tr("全市场", "Whole market") },
              { value: "on", label: tr("已订阅", "Subscribed") },
              { value: "off", label: tr("未订阅", "Unsubscribed") },
            ]}
          />
          <div className="w-56">
            <TextField value={search} onChange={setSearch} placeholder={tr("搜索技能名 / 中文名…", "Search skill name…")} />
          </div>
          <div className="w-40">
            <SelectField
              value={source}
              onChange={setSource}
              options={[{ value: "", label: tr("全部来源", "All sources") },
                        ...sources.map((s) => ({ value: s, label: s }))]}
            />
          </div>
          {pendingBadgeCount(pendingRecs) > 0 && (
            <Badge tone="WARN">{tr(`${pendingBadgeCount(pendingRecs)} 条推荐待确认`, `${pendingBadgeCount(pendingRecs)} recommendation(s) pending`)}</Badge>
          )}
        </div>
        {cat.isLoading && <Spinner />}
        {cat.error && <ErrorNote error={cat.error} />}
        {cat.data?.degraded && <DegradedNote what={tr("技能目录", "skill catalog")} />}
        {cat.data && !cat.data.degraded && rows.length === 0 && <EmptyNote />}
        {cat.data && !cat.data.degraded && rows.length > 0 && (
          <DataTable data={rows} columns={columns} globalFilter={search} />
        )}
      </Section>

      <Modal open={confirmReset} onClose={() => setConfirmReset(false)} title={tr("恢复全订阅", "Reset to full subscription")}>
        <p className="text-sm">
          {tr("把订阅列表恢复成出厂态：", "Reset the subscription list to the factory state: ")}
          <b>{tr(`全部 ${st.data?.market_total ?? 0} 个技能`, `all ${st.data?.market_total ?? 0} skills`)}</b>
          {tr(
            "都回到 agent 的装载面上，你此前的定制会被丢弃（审计记录保留）。",
            " return to the agent's tool surface, and your earlier customisation is discarded (the audit record is kept).",
          )}
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <Button onClick={() => setConfirmReset(false)}>{tr("取消", "Cancel")}</Button>
          <Button variant="danger" loading={reset.isPending} onClick={() => reset.mutate()}>
            {tr("恢复", "Reset")}
          </Button>
        </div>
      </Modal>

      <Modal open={!!importPreview} onClose={() => setImportPreview(null)}
             title={tr("导入订阅列表", "Import subscription list")} wide>
        {importPreview && (
          <ImportPreview
            manifest={importPreview}
            known={new Set((cat.data?.skills ?? []).map((r) => r.name))}
            mode={importMode}
            onMode={setImportMode}
            busy={doImport.isPending}
            onDryRun={() => doImport.mutate({ manifest: importPreview, dry: true })}
            onConfirm={() => doImport.mutate({ manifest: importPreview, dry: false })}
          />
        )}
      </Modal>
    </div>
  );
}

function ImportPreview({
  manifest, known, mode, onMode, busy, onDryRun, onConfirm,
}: {
  manifest: SubscriptionManifest;
  known: Set<string>;
  mode: "replace" | "merge";
  onMode: (v: "replace" | "merge") => void;
  busy: boolean;
  onDryRun: () => void;
  onConfirm: () => void;
}) {
  const diff = diffManifestAgainstCatalog(manifest, known);
  return (
    <div className="flex flex-col gap-3 text-sm">
      <div className="text-mast-muted">
        {tr("来自 ", "From ")}<b>{manifest.machine || tr("未知机器", "unknown machine")}</b>
        {manifest.exported_at ? tr(`，导出于 ${manifest.exported_at}`, `, exported at ${manifest.exported_at}`) : ""}
        {tr(`，共 ${manifest.entries.length} 条。`, `, ${manifest.entries.length} entries.`)}
      </div>
      <div className="flex flex-wrap gap-2">
        <Badge tone="AUTO">{tr(`本机可用 ${diff.matched.length}`, `Available here ${diff.matched.length}`)}</Badge>
        <Badge tone={diff.missing.length ? "WARN" : "default"}>
          {tr(`本机缺 ${diff.missing.length}`, `Missing here ${diff.missing.length}`)}
        </Badge>
        {diff.embedded > 0 && <Badge tone="INFO">{tr(`内嵌组合技能 ${diff.embedded}`, `Embedded composites ${diff.embedded}`)}</Badge>}
      </div>
      {diff.missing.length > 0 && (
        <div>
          <div className="mb-1 text-mast-muted">{tr("本机没有的（导入后会被列在报告里，不会写进订阅）：", "Not on this machine (listed in the report after import, not written to the subscription):")}</div>
          <div className="max-h-40 overflow-auto rounded-mast-ctl border border-mast-border p-2">
            {diff.missing.slice(0, 50).map((m) => (
              <div key={m.name}>
                <code>{m.name}</code>{" "}
                <span className="text-xs text-mast-muted">{m.source}</span>
              </div>
            ))}
            {diff.missing.length > 50 && (
              <div className="text-xs text-mast-muted">{tr(`…还有 ${diff.missing.length - 50} 条`, `… ${diff.missing.length - 50} more`)}</div>
            )}
          </div>
        </div>
      )}
      <p className="text-xs text-mast-muted">
        {tr("以上是", "The above is a ")}<b>{tr("本地预览", "local preview")}</b>
        {tr(
          "：内嵌的组合技能会在导入时先落地，所以后端 dry-run 的数字可能比这里多。代码（.py）不随订阅单走 —— 覆盖层与自建技能需要单独获取。",
          ": embedded composites are written first during import, so the backend dry-run may report more than shown here. Code (.py) does not travel with the subscription; overlays and custom skills must be obtained separately.",
        )}
      </p>
      <RadioGroup
        value={mode}
        onChange={onMode}
        options={[
          { value: "replace", label: tr("替换我的订阅", "Replace my subscription") },
          { value: "merge", label: tr("并入我的订阅", "Merge into my subscription") },
        ]}
      />
      <div className="flex justify-end gap-2">
        <Button onClick={onDryRun} disabled={busy}>{tr("后端预览（不写入）", "Backend preview (no write)")}</Button>
        <Button variant="primary" loading={busy} onClick={onConfirm}>{tr("导入", "Import")}</Button>
      </div>
    </div>
  );
}
