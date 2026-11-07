import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { components } from "@/api/schema";
import {
  Badge,
  Card,
  DegradedNote,
  EmptyNote,
  ErrorNote,
  Section,
  Spinner,
} from "@/components/ui";
import { Button, useToast } from "@/components/controls";
import { tr } from "@/lib/i18n";

// Advanced → System / global → Context injection
//
// Every agent call carries a pile of text that is hard to read in one place: a
// static SYSTEM_PROMPT per agent plus whatever the middlewares append. A text
// injected next to a metre value (e.g. "(= 1253.1 nm)") can mislead the model about
// units, so the injected text needs to be inspectable.
//
// Two halves, deliberately unequal in prominence:
//   A. the inventory + editor (default view)
//   B. "what the agent actually received": real captured requests, collapsed by
//      default and fetched lazily. Most users do not need it.
//
// Honesty rule: a block that needs live hardware or per-request state is shown as
// unavailable with the reason, never as a plausible sample. This page only has
// value if everything on it is the real text.

type PromptSummary = components["schemas"]["PromptSummary"];
type PromptDetail = components["schemas"]["PromptDetail"];
type CaptureSummary = components["schemas"]["PromptCaptureSummary"];

const CATEGORY_LABEL: Record<string, string> = {
  agent_system: tr("Agent 系统提示", "Agent system prompts"),
  routing: tr("路由", "Routing"),
  middleware: tr("运行时注入 (middleware)", "Runtime injection (middleware)"),
  sub_llm: tr("辅助模型提示", "Auxiliary model prompts"),
};
const CATEGORY_ORDER = ["agent_system", "routing", "middleware", "sub_llm"];

type Availability = { label: string; tone: string };
const AVAILABILITY: Record<string, Availability> = {
  static: { label: tr("固定文本", "Static text"), tone: "AUTO" },
  live: { label: tr("实时渲染", "Rendered live"), tone: "INFO" },
  needs_hardware: { label: tr("需实时硬件", "Needs live hardware"), tone: "WARN" },
  needs_request: { label: tr("需运行时上下文", "Needs runtime context"), tone: "WARN" },
};
const UNKNOWN_AVAILABILITY: Availability = { label: tr("未知", "Unknown"), tone: "INFO" };

function availabilityOf(key: string | undefined): Availability {
  return AVAILABILITY[key ?? ""] ?? UNKNOWN_AVAILABILITY;
}

function fmtChars(n: number): string {
  return n >= 1000
    ? tr(`${(n / 1000).toFixed(1)}k 字符`, `${(n / 1000).toFixed(1)}k chars`)
    : tr(`${n} 字符`, `${n} chars`);
}

function fmtAge(sec: number): string {
  if (sec < 60) return tr(`${Math.round(sec)} 秒前`, `${Math.round(sec)} s ago`);
  if (sec < 3600) return tr(`${Math.round(sec / 60)} 分钟前`, `${Math.round(sec / 60)} min ago`);
  return tr(`${(sec / 3600).toFixed(1)} 小时前`, `${(sec / 3600).toFixed(1)} h ago`);
}

// ── A. inventory + editor ────────────────────────────────────────────────────

function PromptDetailPanel({ promptId, onClose }: { promptId: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { toast, node: toastNode } = useToast();
  const [draft, setDraft] = useState<string | null>(null);

  const q = useQuery({
    queryKey: ["admin", "prompt", promptId],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/admin/prompts/{prompt_id}", {
        params: { path: { prompt_id: promptId } },
      });
      if (error) throw error;
      return data as PromptDetail;
    },
  });

  const save = useMutation({
    mutationFn: async (text: string) => {
      const { data, error } = await api.POST("/api/admin/prompts/{prompt_id}", {
        params: { path: { prompt_id: promptId } },
        body: { text },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      setDraft(null);
      toast(data?.message || tr("已保存", "Saved"), data?.ok ? "ok" : "err");
      queryClient.invalidateQueries({ queryKey: ["admin", "prompt", promptId] });
      queryClient.invalidateQueries({ queryKey: ["admin", "prompts"] });
    },
    onError: () => toast(tr("保存失败", "Save failed"), "err"),
  });

  const reset = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.DELETE("/api/admin/prompts/{prompt_id}", {
        params: { path: { prompt_id: promptId } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      setDraft(null);
      toast(data?.message || tr("已恢复默认", "Restored to default"), "ok");
      queryClient.invalidateQueries({ queryKey: ["admin", "prompt", promptId] });
      queryClient.invalidateQueries({ queryKey: ["admin", "prompts"] });
    },
    onError: () => toast(tr("恢复默认失败", "Restore failed"), "err"),
  });

  const d = q.data;
  const editable = !!d?.overridable;
  const value = draft ?? d?.effective_text ?? "";
  const dirty = draft !== null && draft !== (d?.effective_text ?? "");
  const avail = availabilityOf(d?.availability);

  return (
    <Card className="mt-3">
      {toastNode}
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-base font-semibold text-mast-text">{d?.label ?? promptId}</h3>
            <Badge tone={avail.tone}>{avail.label}</Badge>
            {d?.overridden && <Badge tone="WARN">{tr("已覆写", "Overridden")}</Badge>}
          </div>
          <p className="mt-1 font-mono text-[0.7rem] text-mast-muted">{d?.source || promptId}</p>
        </div>
        <Button variant="ghost" onClick={onClose}>{tr("收起", "Collapse")}</Button>
      </div>

      {q.isPending && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      {d?.degraded && <DegradedNote what={tr("上下文注入", "Context injection")} />}

      {d && !d.degraded && (
        <>
          {d.note && <p className="mb-3 text-xs leading-relaxed text-mast-muted">{d.note}</p>}

          {/* The honesty surface: no body, and we say exactly why. */}
          {!d.effective_text && d.unavailable_reason && (
            <div className="mb-3 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2.5 text-sm text-mast-warn">
              {d.unavailable_reason}
            </div>
          )}

          {d.effective_text && (
            <>
              <div className="mb-1 flex items-center justify-between text-xs text-mast-muted">
                <span>{editable ? tr("生效中的文本（可编辑）", "Effective text (editable)") : tr("生效中的文本（只读）", "Effective text (read-only)")}</span>
                <span className="font-mono">{fmtChars(d.effective_chars ?? 0)}</span>
              </div>
              <textarea
                value={value}
                readOnly={!editable}
                spellCheck={false}
                onChange={(e) => setDraft(e.target.value)}
                rows={18}
                className="w-full resize-y rounded-mast-ctl border border-mast-border bg-mast-bg p-3 font-mono text-[12.5px] leading-relaxed text-mast-text outline-none focus:border-mast-accent read-only:text-mast-muted"
              />
            </>
          )}

          {!editable && d.effective_text && (
            <p className="mt-2 text-xs text-mast-muted">
              {tr(
                "这一块的内容由运行时状态计算，没有可替换的固定文本 —— 只读。",
                "This block is computed from runtime state and has no fixed text to replace, so it is read-only.",
              )}
            </p>
          )}

          {editable && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Button
                variant="primary"
                loading={save.isPending}
                disabled={!dirty}
                onClick={() => save.mutate(draft ?? "")}
              >
                {tr("保存覆写", "Save override")}
              </Button>
              <Button
                loading={reset.isPending}
                disabled={!d.overridden && !dirty}
                onClick={() => (dirty ? setDraft(null) : reset.mutate())}
              >
                {dirty ? tr("撤销未保存的修改", "Discard unsaved changes") : tr("恢复默认", "Reset to default")}
              </Button>
              {d.overridden && (
                <span className="text-xs text-mast-muted">
                  {tr(
                    `代码默认 ${fmtChars(d.default_chars ?? 0)}，当前覆写 ${fmtChars(d.effective_chars ?? 0)}。`,
                    `Code default ${fmtChars(d.default_chars ?? 0)}, current override ${fmtChars(d.effective_chars ?? 0)}.`,
                  )}
                </span>
              )}
            </div>
          )}

          {d.overridden && d.default_text && (
            <details className="mt-3">
              <summary className="cursor-pointer text-xs text-mast-muted hover:text-mast-text">
                {tr("查看代码默认文本（对照用）", "Show the code default text (for comparison)")}
              </summary>
              <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-mast-ctl border border-mast-border bg-mast-bg p-3 font-mono text-[12px] leading-relaxed text-mast-muted">
                {d.default_text}
              </pre>
            </details>
          )}
        </>
      )}
    </Card>
  );
}

function PromptInventory() {
  const [query, setQuery] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);

  const q = useQuery({
    queryKey: ["admin", "prompts"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/admin/prompts");
      if (error) throw error;
      return data;
    },
  });

  const groups = useMemo(() => {
    const items = (q.data?.items ?? []) as PromptSummary[];
    const s = query.trim().toLowerCase();
    const hit = (i: PromptSummary) =>
      !s ||
      [i.id, i.label, i.agent, i.source, i.note, i.preview]
        .some((f) => String(f ?? "").toLowerCase().includes(s));
    const filtered = items.filter(hit);
    return CATEGORY_ORDER
      .map((cat) => ({ cat, rows: filtered.filter((i) => i.category === cat) }))
      .filter((g) => g.rows.length > 0);
  }, [q.data, query]);

  return (
    <div>
      <p className="mb-3 text-xs leading-relaxed text-mast-muted">
        {tr("每次调用 agent 前注入上下文的", "All the wording injected into the context before each agent call (")}
        <strong className="text-mast-text">{tr("全部话术", "every prompt")}</strong>
        {tr(
          "。标注为「固定文本」的可以编辑并持久化（保存在 ",
          "). Items marked “Static text” can be edited and persisted (saved in ",
        )}
        <code>config/overrides/prompt_overrides.json</code>
        {tr(
          "，重启后仍在，且进入「覆盖历史」）；标注为「需实时硬件 / 需运行时上下文」的",
          ", kept across restarts and recorded in Override history). Items marked “Needs live hardware / Needs runtime context” ",
        )}
        <strong className="text-mast-text">{tr("不会伪造示例文本", "get no made-up sample text")}</strong>
        {tr(
          " —— 要看它们的真实内容，请展开页面下方的真实请求快照。",
          "; to see their real content, expand the captured requests below.",
        )}
      </p>

      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={tr("搜索…（agent 名、话术 ID、中英文关键词）", "Search… (agent name, prompt ID, keywords)")}
        className="mb-3 w-full rounded-mast-ctl border border-mast-border bg-mast-bg px-3 py-2 text-sm text-mast-text outline-none focus:border-mast-accent"
      />

      {q.isPending && <Spinner label={tr("读取注入清单…", "Loading injection inventory…")} />}
      {q.error && <ErrorNote error={q.error} />}
      {q.data?.degraded && <DegradedNote what={tr("上下文注入清单", "Context injection inventory")} />}
      {q.data && !q.data.degraded && groups.length === 0 && <EmptyNote label={tr("没有匹配的话术", "No matching prompts")} />}

      {q.data && !q.data.degraded && (
        <>
          <div className="mb-3 text-xs text-mast-muted">
            {tr(`共 ${q.data.count} 条`, `${q.data.count} prompts`)}
            {(q.data.overridden_count ?? 0) > 0 && (
              <> · <span className="text-mast-warn">{tr(`${q.data.overridden_count} 条已被覆写`, `${q.data.overridden_count} overridden`)}</span></>
            )}
          </div>

          {groups.map((g) => (
            <div key={g.cat} className="mb-4">
              <h3 className="mb-2 text-sm font-semibold text-mast-text">
                {CATEGORY_LABEL[g.cat] ?? g.cat}
                <span className="ml-2 text-xs font-normal text-mast-muted">{g.rows.length}</span>
              </h3>
              <div className="space-y-1.5">
                {g.rows.map((row) => {
                  const avail = availabilityOf(row.availability);
                  const isOpen = openId === row.id;
                  return (
                    <div key={row.id}>
                      <button
                        type="button"
                        onClick={() => setOpenId(isOpen ? null : row.id)}
                        className="w-full rounded-mast-ctl border border-mast-border bg-mast-panel px-3 py-2.5 text-left hover:bg-mast-panel-2"
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-mast-accent">{isOpen ? "▾" : "▸"}</span>
                          <span className="text-sm text-mast-text">{row.label}</span>
                          <Badge tone={avail.tone}>{avail.label}</Badge>
                          {row.overridden && <Badge tone="WARN">{tr("已覆写", "Overridden")}</Badge>}
                          <span className="ml-auto font-mono text-[0.7rem] text-mast-muted">
                            {row.effective_chars ? fmtChars(row.effective_chars) : "—"}
                          </span>
                        </div>
                        <p className="mt-1 truncate font-mono text-[0.7rem] text-mast-faint">{row.id}</p>
                        {row.preview ? (
                          <p className="mt-1 line-clamp-2 text-xs text-mast-muted">{row.preview}</p>
                        ) : (
                          <p className="mt-1 line-clamp-2 text-xs text-mast-warn">
                            {row.unavailable_reason}
                          </p>
                        )}
                      </button>
                      {isOpen && (
                        <PromptDetailPanel promptId={row.id} onClose={() => setOpenId(null)} />
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

// ── B. What the agent actually received: collapsed by default, lazily loaded ──

function CaptureDetail({ index }: { index: number }) {
  const q = useQuery({
    queryKey: ["admin", "prompt-capture", index],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/admin/prompt-capture/{index}", {
        params: { path: { index } },
      });
      if (error) throw error;
      return data;
    },
  });

  if (q.isPending) return <Spinner label={tr("读取快照…", "Loading snapshot…")} />;
  if (q.error) return <ErrorNote error={q.error} />;
  const d = q.data;
  if (!d || !d.found) return <EmptyNote label={tr("这条快照已被新的请求挤出缓冲区", "This snapshot has been pushed out of the buffer by newer requests")} />;

  return (
    <div className="mt-2 space-y-2">
      <p className="text-xs text-mast-muted">
        {new Date((d.ts ?? 0) * 1000).toLocaleString()} · {fmtAge(d.age_s ?? 0)} ·{" "}
        {d.source} · {d.model_id}
        {(d.dropped_messages ?? 0) > 0 && (
          <span className="text-mast-warn"> · {tr(`另有 ${d.dropped_messages} 条消息因体积上限未记录`, `${d.dropped_messages} more message(s) not recorded due to the size limit`)}</span>
        )}
      </p>
      {(d.messages ?? []).map((m, i) => (
        <div key={i} className="rounded-mast-ctl border border-mast-border bg-mast-bg">
          <div className="flex items-center justify-between border-b border-mast-border px-3 py-1.5 text-xs">
            <span className="font-mono text-mast-accent">{m.role}</span>
            <span className="font-mono text-mast-muted">
              {fmtChars(m.chars ?? 0)}
              {m.truncated && <span className="ml-2 text-mast-warn">{tr("已截断显示", "Display truncated")}</span>}
            </span>
          </div>
          <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-[12px] leading-relaxed text-mast-text">
            {m.content}
          </pre>
        </div>
      ))}
    </div>
  );
}

function CapturePanel() {
  const queryClient = useQueryClient();
  const [openIndex, setOpenIndex] = useState<number | null>(null);

  const q = useQuery({
    queryKey: ["admin", "prompt-capture"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/admin/prompt-capture");
      if (error) throw error;
      return data;
    },
  });

  const clear = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.DELETE("/api/admin/prompt-capture");
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      setOpenIndex(null);
      queryClient.invalidateQueries({ queryKey: ["admin", "prompt-capture"] });
    },
  });

  const items = (q.data?.items ?? []) as CaptureSummary[];

  return (
    <div>
      <p className="mb-3 text-xs leading-relaxed text-mast-muted">
        {tr("这里记录的是", "These are ")}
        <strong className="text-mast-text">{tr("真实发生过的模型请求", "model requests that really happened")}</strong>
        {tr(
          " —— provider 收到的原始消息列表，含所有 middleware 拼进去的内容。不是模拟渲染：模拟无法复现当次的硬件读数、对话历史与 middleware 顺序，看起来像却不是真的东西比没有更糟。代价是它",
          ": the raw message list the provider received, including everything the middlewares added. They are not simulated renderings, which cannot reproduce that call's hardware readings, conversation history or middleware order; something that looks real but is not is worse than nothing. The trade-off is that a snapshot ",
        )}
        <strong className="text-mast-text">{tr("可能过期", "may be stale")}</strong>
        {tr(
          `（每条都带时间戳），且本进程还没发生过调用时是空的。仅保存在内存里最近 ${q.data?.capacity ?? 0} 次，不落盘。`,
          ` (each one is timestamped), and the list is empty until this process has made a call. Only the last ${q.data?.capacity ?? 0} calls are kept, in memory, never written to disk.`,
        )}
      </p>
      <p className="mb-3 text-xs leading-relaxed text-mast-faint">
        {tr("范围说明：这里只有", "Scope: only the ")}
        <strong className="text-mast-muted">{tr("消息列表", "message list")}</strong>
        {tr(
          "。工具定义（IC 的 ~250 个工具 schema）是绑在模型上的，不走消息，因此不在这份记录里 —— 要看工具清单去「Agents → 工具」。",
          " is recorded. Tool definitions (the ~250 IC tool schemas) are bound to the model rather than sent as messages, so they are not included; see Agents → Tools for the tool list.",
        )}
      </p>

      <div className="mb-3 flex items-center gap-3">
        <Button onClick={() => q.refetch()} loading={q.isFetching}>{tr("刷新", "Refresh")}</Button>
        <Button
          variant="ghost"
          disabled={items.length === 0}
          loading={clear.isPending}
          onClick={() => clear.mutate()}
        >
          {tr("清空", "Clear")}
        </Button>
        {(q.data?.total_seen ?? 0) > 0 && (
          <span className="text-xs text-mast-muted">{tr(`本次运行累计 ${q.data?.total_seen} 次调用`, `${q.data?.total_seen} calls so far in this run`)}</span>
        )}
      </div>

      {q.isPending && <Spinner label={tr("读取快照列表…", "Loading snapshot list…")} />}
      {q.error && <ErrorNote error={q.error} />}
      {q.data?.degraded && <DegradedNote what={tr("真实请求快照", "Captured requests")} />}
      {q.data && !q.data.degraded && items.length === 0 && (
        <EmptyNote label={q.data.note || tr("暂无快照", "No snapshots yet")} />
      )}

      <div className="space-y-1.5">
        {items.map((row) => {
          const isOpen = openIndex === row.index;
          return (
            <div key={row.index}>
              <button
                type="button"
                onClick={() => setOpenIndex(isOpen ? null : (row.index ?? 0))}
                className="w-full rounded-mast-ctl border border-mast-border bg-mast-panel px-3 py-2.5 text-left hover:bg-mast-panel-2"
              >
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="text-mast-accent">{isOpen ? "▾" : "▸"}</span>
                  <span className="text-mast-text">{row.source}</span>
                  <Badge tone="INFO">{row.model_id || tr("未知模型", "Unknown model")}</Badge>
                  <span className="text-xs text-mast-muted">{fmtAge(row.age_s ?? 0)}</span>
                  <span className="ml-auto font-mono text-[0.7rem] text-mast-muted">
                    {tr(`${row.message_count} 条消息`, `${row.message_count} messages`)} · system {fmtChars(row.system_chars ?? 0)} · {tr("合计", "total")}{" "}
                    {fmtChars(row.total_chars ?? 0)}
                  </span>
                </div>
              </button>
              {isOpen && <CaptureDetail index={row.index ?? 0} />}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** The whole context-injection surface. Part B is collapsed until asked for: it
 *  neither renders nor fetches while closed. */
export function PromptInspector() {
  const [showCapture, setShowCapture] = useState(false);

  return (
    <div>
      <Section title={tr("上下文注入话术", "Context injection prompts")}>
        <PromptInventory />
      </Section>

      <div className="overflow-hidden rounded-mast-card border border-mast-border bg-mast-panel">
        <button
          type="button"
          onClick={() => setShowCapture((v) => !v)}
          className="flex w-full items-center justify-between px-4 py-3 text-left hover:bg-mast-panel-2"
        >
          <span className="text-sm text-mast-muted">
            <span className="text-mast-accent">{showCapture ? "▾" : "▸"}</span>{" "}
            {tr("agent 实际收到了什么（真实请求快照）", "What the agent actually received (captured requests)")}
            <span className="ml-2 text-xs text-mast-faint">{tr("排障用，一般不需要打开", "For troubleshooting; usually not needed")}</span>
          </span>
          <span className="text-mast-muted">{showCapture ? tr("收起", "Collapse") : tr("展开", "Expand")}</span>
        </button>
        {showCapture && (
          <div className="border-t border-mast-border p-4">
            <CapturePanel />
          </div>
        )}
      </div>
    </div>
  );
}

export default PromptInspector;
