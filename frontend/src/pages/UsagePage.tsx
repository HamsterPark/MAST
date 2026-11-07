import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { Badge, Card, ErrorNote, Section, Spinner } from "../components/ui";
import { Accordion, useToast } from "../components/controls";
import { langTag, tr } from "@/lib/i18n";

// Usage & cost — records the cost of every paid API call (LLM turns across all
// agents + main chat, voice TTS/ASR, document OCR) and aggregates the spend.
// Data comes from the mast.billing ledger via /api/usage/*. Prices are an
// EDITABLE estimate (see the price-book section) — the ledger stores whether each
// row was authoritatively priced, surfaced here with a ≈ badge.

const RANGES = [
  { value: "today", label: tr("今日", "Today") },
  { value: "7d", label: tr("近 7 天", "Last 7 days") },
  { value: "30d", label: tr("近 30 天", "Last 30 days") },
  { value: "all", label: tr("全部", "All") },
];

const KIND_LABEL: Record<string, string> = {
  llm: tr("对话 LLM", "Chat LLM"),
  tts: tr("语音合成 TTS", "Speech synthesis (TTS)"),
  asr: tr("语音识别 ASR", "Speech recognition (ASR)"),
  ocr: tr("文献 OCR", "Literature OCR"),
  embedding: tr("向量 Embedding", "Embedding"),
};

const SOURCE_LABEL: Record<string, string> = {
  orchestrator: tr("编排 Orchestrator", "Orchestrator"),
  instrument_control: tr("仪器控制 IC", "Instrument control (IC)"),
  experiment_design: tr("实验设计 XD", "Experiment design (XD)"),
  data_processing: tr("数据处理 DP", "Data processing (DP)"),
  literature: tr("文献 Lit", "Literature"),
  paper_writing: tr("论文写作", "Paper writing"),
  paper_review: tr("论文评审", "Paper review"),
  chat: tr("主聊天", "Main chat"),
  quickask: tr("查询助手", "Query assistant"),
  voice: tr("语音", "Voice"),
  ocr: "OCR",
  adhoc: tr("临时调用", "Ad hoc"),
};

function money(cost: number, currency: string): string {
  const sym = currency === "USD" ? "$" : "¥";
  const digits = Math.abs(cost) < 1 ? 4 : 2;
  return `${sym}${cost.toFixed(digits)}`;
}

function fmtInt(n: number): string {
  return (n || 0).toLocaleString("en-US");
}

// ── total cards ────────────────────────────────────────────────────────────
function Totals({ data }: { data: any }) {
  const byCur = data.by_currency ?? {};
  const currencies = Object.keys(byCur);
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {currencies.length === 0 && (
        <Card>
          <div className="text-sm text-mast-muted">{tr("本区间暂无花销", "No spend in this period")}</div>
        </Card>
      )}
      {currencies.map((cur) => {
        const t = byCur[cur];
        return (
          <Card key={cur}>
            <div className="text-xs text-mast-muted">{tr(`${cur} 花销`, `${cur} spend`)}</div>
            <div className="mt-1 font-mono text-2xl font-semibold text-mast-text">
              {money(t.cost, cur)}
            </div>
            <div className="mt-1 text-xs text-mast-muted">
              {tr(`${fmtInt(t.count)} 次调用`, `${fmtInt(t.count)} calls`)}
              {t.estimated_cost > 0 && (
                <span className="ml-1 text-mast-warn">
                  · {tr("含估算", "incl. estimated")} {money(t.estimated_cost, cur)}
                </span>
              )}
            </div>
          </Card>
        );
      })}
      {data.combined_cny != null && currencies.length > 1 && (
        <Card>
          <div className="text-xs text-mast-muted">{tr("折算合计 ≈¥", "Converted total ≈¥")}</div>
          <div className="mt-1 font-mono text-2xl font-semibold text-mast-accent">
            ¥{Number(data.combined_cny).toFixed(2)}
          </div>
          <div className="mt-1 text-xs text-mast-muted">
            {tr(`按 1 USD ≈ ${data.usd_to_cny} CNY 折算`, `Converted at 1 USD ≈ ${data.usd_to_cny} CNY`)}
          </div>
        </Card>
      )}
    </div>
  );
}

// ── one breakdown table ──────────────────────────────────────────────────────
function Breakdown({
  title,
  rows,
  labelMap,
}: {
  title: string;
  rows: any[];
  labelMap?: Record<string, string>;
}) {
  return (
    <Card>
      <div className="mb-2 text-sm font-medium text-mast-text">{title}</div>
      {rows.length === 0 ? (
        <div className="text-xs text-mast-muted">—</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-mast-muted">
                <th className="py-1 pr-2 font-normal">{tr("名称", "Name")}</th>
                <th className="py-1 pr-2 text-right font-normal">{tr("花销", "Spend")}</th>
                <th className="py-1 pr-2 text-right font-normal">{tr("次数", "Calls")}</th>
                <th className="py-1 text-right font-normal">tokens (in/out)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={`${r.key}-${r.currency}-${i}`} className="border-t border-mast-border">
                  <td className="py-1 pr-2 text-mast-text">
                    {labelMap?.[r.key] ?? r.key}
                    {!r.all_priced && (
                      <span className="ml-1 align-middle">
                        <Badge tone="WARN">≈</Badge>
                      </span>
                    )}
                  </td>
                  <td className="py-1 pr-2 text-right font-mono text-mast-text">
                    {money(r.cost, r.currency)}
                  </td>
                  <td className="py-1 pr-2 text-right font-mono text-mast-muted">{fmtInt(r.count)}</td>
                  <td className="py-1 text-right font-mono text-xs text-mast-muted">
                    {r.input_tokens || r.output_tokens
                      ? `${fmtInt(r.input_tokens)} / ${fmtInt(r.output_tokens)}`
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

// ── recent calls ─────────────────────────────────────────────────────────────
function Recent({ events }: { events: any[] }) {
  if (events.length === 0) return <div className="text-xs text-mast-muted">{tr("暂无调用记录", "No calls recorded")}</div>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-mast-muted">
            <th className="py-1 pr-3 font-normal">{tr("时间", "Time")}</th>
            <th className="py-1 pr-3 font-normal">{tr("类型", "Type")}</th>
            <th className="py-1 pr-3 font-normal">{tr("模型", "Model")}</th>
            <th className="py-1 pr-3 font-normal">{tr("来源", "Source")}</th>
            <th className="py-1 pr-3 text-right font-normal">{tr("用量", "Usage")}</th>
            <th className="py-1 text-right font-normal">{tr("花销", "Spend")}</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e, i) => (
            <tr key={i} className="border-t border-mast-border">
              <td className="whitespace-nowrap py-1 pr-3 text-xs text-mast-muted">
                {new Date(e.ts * 1000).toLocaleString(langTag(), { hour12: false })}
              </td>
              <td className="py-1 pr-3 text-xs text-mast-text">{KIND_LABEL[e.kind] ?? e.kind}</td>
              <td className="py-1 pr-3 font-mono text-xs text-mast-text">{e.model}</td>
              <td className="py-1 pr-3 text-xs text-mast-muted">{SOURCE_LABEL[e.source] ?? e.source}</td>
              <td className="py-1 pr-3 text-right font-mono text-xs text-mast-muted">
                {e.kind === "tts"
                  ? tr(`${fmtInt(e.chars)} 字`, `${fmtInt(e.chars)} chars`)
                  : e.kind === "asr"
                    ? tr(`${Math.round(e.seconds)} 秒`, `${Math.round(e.seconds)} s`)
                    : `${fmtInt(e.input_tokens)}/${fmtInt(e.output_tokens)}`}
              </td>
              <td className="py-1 text-right font-mono text-xs text-mast-text">
                {money(e.cost, e.currency)}
                {!e.cost_known && <span className="ml-0.5 text-mast-warn">≈</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── editable price book ──────────────────────────────────────────────────────
function PricingEditor() {
  const qc = useQueryClient();
  const { toast, node } = useToast();
  const pricing = useQuery({
    queryKey: ["usage", "pricing"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/usage/pricing");
      if (error) throw error;
      return data;
    },
  });
  const [rows, setRows] = useState<any[]>([]);
  useEffect(() => {
    if (pricing.data?.models) setRows(pricing.data.models.map((m: any) => ({ ...m })));
  }, [pricing.data]);

  const save = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/usage/pricing", { body: { models: rows } });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      toast(tr("价格已保存（后续调用按新价计费）", "Prices saved (later calls are billed at the new prices)"), "ok");
      qc.invalidateQueries({ queryKey: ["usage"] });
    },
    onError: (e) =>
      toast(tr(`保存失败：${String((e as Error)?.message ?? e)}`, `Save failed: ${String((e as Error)?.message ?? e)}`), "err"),
  });

  const setField = (i: number, field: string, val: string) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, [field]: parseFloat(val) || 0 } : r)));

  if (pricing.isPending) return <Spinner />;
  if (pricing.isError) return <ErrorNote error={pricing.error} />;

  return (
    <div>
      <p className="mb-2 text-xs text-mast-muted">
        {tr("单价均为", "All unit prices are ")}
        <strong>{tr("估算默认值", "estimated defaults")}</strong>
        {tr(
          "（各家随档位 / 上下文 / 缓存变动）。你付账，你改价——改后仅影响",
          " (providers vary them by tier / context / caching). You pay, so you can edit them; changes only affect billing of ",
        )}
        <strong>{tr("之后", "later")}</strong>
        {tr(
          "的调用记账。LLM/OCR 按每百万 token；TTS 按每百万字符；ASR 按每分钟。",
          " calls. LLM/OCR are per million tokens; TTS per million characters; ASR per minute.",
        )}
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-mast-muted">
              <th className="py-1 pr-2 font-normal">{tr("模型", "Model")}</th>
              <th className="py-1 pr-2 font-normal">{tr("币种", "Currency")}</th>
              <th className="py-1 pr-2 text-right font-normal">{tr("输入/1M", "Input/1M")}</th>
              <th className="py-1 pr-2 text-right font-normal">{tr("输出/1M", "Output/1M")}</th>
              <th className="py-1 pr-2 text-right font-normal">{tr("字符/1M", "Chars/1M")}</th>
              <th className="py-1 text-right font-normal">{tr("每分钟", "Per minute")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.model} className="border-t border-mast-border">
                <td className="py-1 pr-2 font-mono text-xs text-mast-text">{r.model}</td>
                <td className="py-1 pr-2 text-xs text-mast-muted">{r.currency}</td>
                {(["input_per_m", "output_per_m", "char_per_m", "per_minute"] as const).map((f) => (
                  <td key={f} className="py-1 pr-2 text-right">
                    <input
                      type="number"
                      step="0.01"
                      value={r[f]}
                      onChange={(e) => setField(i, f, e.target.value)}
                      className="w-20 rounded border border-mast-border bg-mast-bg/40 px-1 py-0.5 text-right font-mono text-xs text-mast-text"
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-3">
        <button
          onClick={() => save.mutate()}
          disabled={save.isPending}
          className="rounded-md border border-mast-accent bg-mast-accent-soft px-3 py-1.5 text-sm font-medium text-mast-accent hover:opacity-90 disabled:opacity-50"
        >
          {save.isPending ? tr("保存中…", "Saving…") : tr("保存价格表", "Save price book")}
        </button>
      </div>
      {node}
    </div>
  );
}

// ── page ─────────────────────────────────────────────────────────────────────
export default function UsagePage() {
  const [range, setRange] = useState("7d");
  const [confirmReset, setConfirmReset] = useState(false);
  const qc = useQueryClient();
  const { toast, node } = useToast();

  const summary = useQuery({
    queryKey: ["usage", "summary", range],
    refetchInterval: 15000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/usage/summary", { params: { query: { range } } });
      if (error) throw error;
      return data;
    },
  });
  const recent = useQuery({
    queryKey: ["usage", "recent"],
    refetchInterval: 15000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/usage/recent", { params: { query: { limit: 60 } } });
      if (error) throw error;
      return data;
    },
  });
  const reset = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/usage/reset");
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      toast(tr(`已清空 ${d?.deleted ?? 0} 条记录`, `Cleared ${d?.deleted ?? 0} records`), "ok");
      setConfirmReset(false);
      qc.invalidateQueries({ queryKey: ["usage"] });
    },
    onError: (e) =>
      toast(tr(`重置失败：${String((e as Error)?.message ?? e)}`, `Reset failed: ${String((e as Error)?.message ?? e)}`), "err"),
  });

  return (
    <div>
      <Section title={tr("用量·花销", "Usage & cost")}>
        <p className="mb-4 text-sm text-mast-muted">
          <strong>{tr("接口花销记录器", "API cost recorder")}</strong>
          {tr(
            " — 统计每一次付费接口调用的花销：全 Agent + 主聊天的 LLM、语音 TTS/ASR、文献 OCR。金额按",
            " — tracks the cost of every paid API call: LLM calls from all agents and the main chat, voice TTS/ASR, and literature OCR. Amounts are estimated from an ",
          )}
          <strong>{tr("可编辑的价格表", "editable price book")}</strong>
          {tr("估算，不同币种分开合计（¥ 与 $）。带 ", "; currencies are totalled separately (¥ and $). A ")}
          <Badge tone="WARN">≈</Badge>
          {tr(
            " 表示该项用了兜底/未定价，数字仅供参考。",
            " badge means the item used a fallback or unpriced rate, so the number is indicative only.",
          )}
        </p>

        {/* range selector */}
        <div className="mb-4 flex items-center gap-2">
          <div className="inline-flex overflow-hidden rounded-md border border-mast-border">
            {RANGES.map((r) => (
              <button
                key={r.value}
                onClick={() => setRange(r.value)}
                className={
                  "px-3 py-1.5 text-sm " +
                  (range === r.value
                    ? "bg-mast-accent-soft font-medium text-mast-accent"
                    : "text-mast-muted hover:text-mast-text")
                }
              >
                {r.label}
              </button>
            ))}
          </div>
          <div className="ml-auto">
            {confirmReset ? (
              <span className="flex items-center gap-2 text-sm">
                <span className="text-mast-warn">{tr("确认清空全部记录？", "Clear all records?")}</span>
                <button
                  onClick={() => reset.mutate()}
                  disabled={reset.isPending}
                  className="rounded-md border border-mast-danger-border bg-mast-danger-bg px-2 py-1 text-xs text-mast-danger"
                >
                  {tr("确认重置", "Confirm reset")}
                </button>
                <button
                  onClick={() => setConfirmReset(false)}
                  className="rounded-md border border-mast-border px-2 py-1 text-xs text-mast-muted"
                >
                  {tr("取消", "Cancel")}
                </button>
              </span>
            ) : (
              <button
                onClick={() => setConfirmReset(true)}
                className="rounded-md border border-mast-border px-2 py-1 text-xs text-mast-muted hover:text-mast-text"
              >
                {tr("清空记录", "Clear records")}
              </button>
            )}
          </div>
        </div>

        {summary.isPending ? (
          <Spinner />
        ) : summary.isError ? (
          <ErrorNote error={summary.error} />
        ) : (
          <div className="space-y-4">
            <Totals data={summary.data} />

            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              <Breakdown title={tr("按供应商 Provider", "By provider")} rows={summary.data.by_provider ?? []} />
              <Breakdown title={tr("按类型 Kind", "By kind")} rows={summary.data.by_kind ?? []} labelMap={KIND_LABEL} />
              <Breakdown title={tr("按模型 Model", "By model")} rows={summary.data.by_model ?? []} />
              <Breakdown
                title={tr("按来源 Agent/来源", "By source (agent)")}
                rows={summary.data.by_source ?? []}
                labelMap={SOURCE_LABEL}
              />
            </div>

            <Card>
              <div className="mb-2 text-sm font-medium text-mast-text">{tr("最近调用", "Recent calls")}</div>
              {recent.isPending ? <Spinner /> : recent.isError ? (
                <ErrorNote error={recent.error} />
              ) : (
                <Recent events={recent.data?.events ?? []} />
              )}
            </Card>

            <Accordion title={tr("价格表 Pricing（可编辑 · 估算默认值）", "Price book (editable · estimated defaults)")} defaultOpen={false}>
              <PricingEditor />
            </Accordion>
          </div>
        )}
      </Section>
      {node}
    </div>
  );
}
