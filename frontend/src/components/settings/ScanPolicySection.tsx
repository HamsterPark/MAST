import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { Field } from "../controls";
import { ErrorNote, Spinner } from "../ui";
import type { SettingsPatch } from "@/lib/settingsWrite";
import { getLang, tr } from "@/lib/i18n";

// ── Scan parameter tier table (by scale) ────────────────────────────────────
//
// "How fast to scan and how many pixels at this scale" is knowledge that depends
// on the instrument and the sample. Without a place to store it, an LLM would
// have to invent a number each time; this section is that table.
//
// The number of tiers is variable (1..8): people split scales differently, so a
// fixed four-tier layout would not fit every workflow.
//
// Bias is deliberately not in the table: it selects the probed electronic states
// and imaging contrast, so it is a physics-intent parameter rather than a
// function of scale. It stays in the experiment defaults section.
//
// Whole-table replace semantics: SettingsStore does not merge, so a save must
// send the complete tiers list. The backend validates structure BEFORE
// persisting (monotonic bounds, exactly one catch-all tier, tier-count limit)
// and rejects an invalid table as a whole; a half-written table is more
// dangerous than none.

type Tier = {
  name?: string | null;
  upper_size_m?: number | null;
  pixels?: number | null;
  line_time_s?: number | null;
  setpoint_a?: number | null;
  p_gain?: number | null;
  time_constant_s?: number | null;
  source?: string | null;
  _factory_filled?: string[] | null;
};

type PolicyPayload = {
  tiers: Tier[];
  stored: Tier[];
  factory: Tier[];
  customised: boolean;
  min_tiers: number;
  max_tiers: number;
};

function useScanPolicy() {
  return useQuery({
    queryKey: ["settings", "scan-policy"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/settings/scan-policy");
      if (error) throw error;
      return data as unknown as PolicyPayload;
    },
  });
}

function usePreview(sizeNm: number) {
  return useQuery({
    queryKey: ["settings", "scan-policy", "preview", sizeNm],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/settings/scan-policy/preview", {
        params: { query: { size_nm: sizeNm } },
      });
      if (error) throw error;
      return data as unknown as {
        ok: boolean;
        error?: string;
        tier_name?: string;
        pixels?: number;
        line_time_s?: number;
        estimated_scan_s?: number;
        summary?: string[];
        warnings?: string[];
      };
    },
    enabled: Number.isFinite(sizeNm) && sizeNm > 0,
  });
}

const nmOf = (m: number | null | undefined): string =>
  m === null || m === undefined ? "" : String(Math.round(m * 1e9 * 1e6) / 1e6);

const paOf = (a: number | null | undefined): string =>
  a === null || a === undefined ? "" : String(Math.round(a * 1e12 * 1e6) / 1e6);

const numOr = (s: string): number | null => {
  const t = s.trim();
  if (t === "") return null;
  const n = Number.parseFloat(t);
  return Number.isFinite(n) ? n : null;
};

const fmtDuration = (s: number | undefined): string => {
  if (!s || s <= 0) return "—";
  if (s < 90) return tr(`${Math.round(s)} 秒`, `${Math.round(s)} s`);
  return tr(`${(s / 60).toFixed(1)} 分钟`, `${(s / 60).toFixed(1)} min`);
};

/** One tier row. Fields are shown in human units (nm / pA) and converted to SI on commit. */
function TierRow({
  tier,
  index,
  isLast,
  factory,
  disabled,
  onChange,
  onRemove,
}: {
  tier: Tier;
  index: number;
  isLast: boolean;
  factory: Tier | undefined;
  disabled: boolean;
  onChange: (patch: Partial<Tier>) => void;
  onRemove: () => void;
}) {
  return (
    <div className="rounded border border-mast-border p-2">
      <div className="mb-2 flex items-center gap-2">
        <input
          type="text"
          value={tier.name ?? ""}
          disabled={disabled}
          placeholder={factory?.name ?? tr(`档 ${index + 1}`, `Tier ${index + 1}`)}
          onChange={(e) => onChange({ name: e.target.value })}
          className="w-28 rounded border border-mast-border bg-mast-bg px-2 py-1 text-sm text-mast-text disabled:opacity-50"
        />
        <span className="text-xs text-mast-muted">{tr("边长 ≤", "Size ≤")}</span>
        {isLast ? (
          <span className="text-xs text-mast-muted">
            {tr("(兜底档 — 比上一档更大的尺寸全部落这里)", "(catch-all tier: every size larger than the previous tier lands here)")}
          </span>
        ) : (
          <>
            <input
              type="text"
              inputMode="decimal"
              value={nmOf(tier.upper_size_m)}
              disabled={disabled}
              placeholder={tr("如 100", "e.g. 100")}
              onChange={(e) =>
                onChange({
                  upper_size_m: (() => {
                    const n = numOr(e.target.value);
                    return n === null ? null : n * 1e-9;
                  })(),
                })
              }
              className="w-24 rounded border border-mast-border bg-mast-bg px-2 py-1 font-mono text-sm tabular-nums text-mast-text disabled:opacity-50"
            />
            <span className="text-xs text-mast-muted">nm</span>
          </>
        )}
        <button
          type="button"
          disabled={disabled}
          onClick={onRemove}
          className="ml-auto rounded border border-mast-border px-2 py-0.5 text-xs text-mast-muted hover:text-mast-text disabled:opacity-50"
        >
          {tr("删除", "Delete")}
        </button>
      </div>

      <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
        <label className="text-xs text-mast-muted">
          {tr("像素 / 线数", "Pixels / lines")}
          <input
            type="text"
            inputMode="numeric"
            value={tier.pixels ?? ""}
            disabled={disabled}
            placeholder={String(factory?.pixels ?? 256)}
            onChange={(e) => onChange({ pixels: numOr(e.target.value) })}
            className="mt-0.5 w-full rounded border border-mast-border bg-mast-bg px-2 py-1 font-mono text-sm tabular-nums text-mast-text disabled:opacity-50"
          />
        </label>
        <label className="text-xs text-mast-muted">
          {tr("每线时间 (s)", "Line time (s)")}
          <input
            type="text"
            inputMode="decimal"
            value={tier.line_time_s ?? ""}
            disabled={disabled}
            placeholder={String(factory?.line_time_s ?? 1)}
            onChange={(e) => onChange({ line_time_s: numOr(e.target.value) })}
            className="mt-0.5 w-full rounded border border-mast-border bg-mast-bg px-2 py-1 font-mono text-sm tabular-nums text-mast-text disabled:opacity-50"
          />
        </label>
        <label className="text-xs text-mast-muted">
          setpoint (pA)
          <input
            type="text"
            inputMode="decimal"
            value={paOf(tier.setpoint_a)}
            disabled={disabled}
            placeholder={tr("留空 = 不改", "blank = unchanged")}
            onChange={(e) =>
              onChange({
                setpoint_a: (() => {
                  const n = numOr(e.target.value);
                  return n === null ? null : n * 1e-12;
                })(),
              })
            }
            className="mt-0.5 w-full rounded border border-mast-border bg-mast-bg px-2 py-1 font-mono text-sm tabular-nums text-mast-text disabled:opacity-50"
          />
        </label>
        <label className="text-xs text-mast-muted">
          {tr("Z 反馈 P", "Z feedback P")}
          <input
            type="text"
            inputMode="decimal"
            value={tier.p_gain ?? ""}
            disabled={disabled}
            placeholder={tr("留空 = 不改", "blank = unchanged")}
            onChange={(e) => onChange({ p_gain: numOr(e.target.value) })}
            className="mt-0.5 w-full rounded border border-mast-border bg-mast-bg px-2 py-1 font-mono text-sm tabular-nums text-mast-text disabled:opacity-50"
          />
        </label>
        <label className="text-xs text-mast-muted">
          {tr("Z 反馈 T (s)", "Z feedback T (s)")}
          <input
            type="text"
            inputMode="decimal"
            value={tier.time_constant_s ?? ""}
            disabled={disabled}
            placeholder={tr("留空 = 不改", "blank = unchanged")}
            onChange={(e) => onChange({ time_constant_s: numOr(e.target.value) })}
            className="mt-0.5 w-full rounded border border-mast-border bg-mast-bg px-2 py-1 font-mono text-sm tabular-nums text-mast-text disabled:opacity-50"
          />
        </label>
      </div>
      {(tier.p_gain === null || tier.p_gain === undefined) !==
        (tier.time_constant_s === null || tier.time_constant_s === undefined) && (
        <p className="mt-1 text-xs text-amber-500">
          {tr(
            "Z 反馈的 P 与时间常数必须成对填写(积分增益 I = P/T 由两者导出),只填一个时本档不会下发 PI 设置。",
            "Z feedback P and time constant must be filled in as a pair (the integral gain I = P/T is derived from both); with only one filled in, this tier will not send PI settings.",
          )}
        </p>
      )}
    </div>
  );
}

export function ScanPolicySection({
  saving,
  save,
}: {
  saving: boolean;
  save: (patch: SettingsPatch) => void;
}) {
  const policy = useScanPolicy();
  const [draft, setDraft] = useState<Tier[] | null>(null);
  const [previewNm, setPreviewNm] = useState(100);
  const preview = usePreview(previewNm);

  // The server is authoritative: reset the draft whenever new data arrives so
  // the editor never sits on a stale table.
  useEffect(() => {
    if (policy.data) setDraft(policy.data.tiers.map((t) => ({ ...t })));
  }, [policy.data]);

  const factoryByIndex = useMemo(() => policy.data?.factory ?? [], [policy.data]);

  if (policy.isLoading) return <Spinner />;
  if (policy.error) return <ErrorNote error={policy.error} />;
  if (!policy.data || !draft) return null;

  const { min_tiers: minTiers, max_tiers: maxTiers, customised } = policy.data;

  const commit = (next: Tier[]) => {
    // Whole-table replace: the store does not merge, so omitting a tier deletes it.
    const payload = next.map((t, i) => ({
      name: (t.name ?? "").trim() || `tier${i + 1}`,
      // The last tier is always the catch-all (upper bound left empty); every
      // other tier needs a bound.
      upper_size_m: i === next.length - 1 ? null : t.upper_size_m ?? null,
      pixels: t.pixels ?? null,
      line_time_s: t.line_time_s ?? null,
      setpoint_a: t.setpoint_a ?? null,
      p_gain: t.p_gain ?? null,
      time_constant_s: t.time_constant_s ?? null,
    }));
    save({ scan_policy: { tiers: payload } });
  };

  const updateRow = (index: number, patch: Partial<Tier>) => {
    const next = draft.map((t, i) => (i === index ? { ...t, ...patch } : t));
    setDraft(next);
  };

  const addRow = () => {
    if (draft.length >= maxTiers) return;
    // Insert the new tier second-to-last: the final slot belongs to the
    // catch-all tier. Its bound defaults to twice the previous tier's, so the
    // bounds stay monotonic (otherwise the backend rejects the whole table).
    const prevBound = draft.length >= 2 ? draft[draft.length - 2]?.upper_size_m : null;
    const seed = prevBound ? prevBound * 2 : 1e-7;
    const next = [...draft];
    next.splice(next.length - 1, 0, {
      name: `tier${draft.length}`,
      upper_size_m: seed,
      pixels: draft[draft.length - 1]?.pixels ?? 256,
      line_time_s: draft[draft.length - 1]?.line_time_s ?? 1.0,
      setpoint_a: null,
      p_gain: null,
      time_constant_s: null,
    });
    setDraft(next);
  };

  const removeRow = (index: number) => {
    if (draft.length <= minTiers) return;
    setDraft(draft.filter((_, i) => i !== index));
  };

  return (
    <div className="max-w-4xl space-y-3">
      <p className="text-xs text-mast-muted">
        {getLang() === "zh" ? (
          <>
            每个尺度下你惯用的<b>扫描速度与分辨率</b>。扫图时 agent 只说「扫哪、多大」,
            速度 / 像素 / 反馈增益由这张表<b>确定性地</b>决定 —— 那些量取决于这台仪器、
            这个针尖、这种样品,不该让模型去猜。
            <br />
            表按<b>扫描边长</b>查,取第一个「边长 ≤ 上界」的档;最后一档是兜底档,
            接住所有更大的尺寸。
            <br />
            <b>留空的 setpoint / PI 表示不改动硬件当前值</b>(不是设成 0)。
            <b>偏压不在这张表里</b> —— 它决定探测的电子态,是物理决策,不是尺度的函数。
          </>
        ) : (
          <>
            Your usual <b>scan speed and resolution</b> at each scale. When scanning, the agent
            only says where and how large; speed, pixels and feedback gains are decided{" "}
            <b>deterministically</b> by this table, because those values depend on this
            instrument, this tip and this sample and should not be guessed by the model.
            <br />
            The table is looked up by <b>scan size</b>: the first tier whose upper bound is ≥ the
            size is used; the last tier is the catch-all for every larger size.
            <br />
            <b>A blank setpoint / PI means the current hardware value is left unchanged</b> (it is
            not set to 0). <b>Bias is not in this table</b>: it selects the probed electronic
            states, which is a physics decision rather than a function of scale.
          </>
        )}
      </p>

      {!customised && (
        <p className="rounded border border-mast-border bg-mast-bg px-2 py-1 text-xs text-mast-muted">
          {getLang() === "zh" ? (
            <>
              当前用的是<b>出厂参考值</b>(取自渐进缩放协议的四级建议)。这些数字随仪器和
              样品变化,<b>请按本机实际情况校准</b>后再依赖它们。
            </>
          ) : (
            <>
              Currently using the <b>factory reference values</b> (the four-level suggestion from
              the progressive-zoom protocol). These numbers vary with the instrument and sample,
              so <b>calibrate them to this machine</b> before relying on them.
            </>
          )}
        </p>
      )}

      <div className="space-y-2">
        {draft.map((tier, i) => (
          <TierRow
            key={i}
            tier={tier}
            index={i}
            isLast={i === draft.length - 1}
            factory={factoryByIndex[Math.min(i, factoryByIndex.length - 1)]}
            disabled={saving}
            onChange={(patch) => updateRow(i, patch)}
            onRemove={() => removeRow(i)}
          />
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-3 pt-1">
        <button
          type="button"
          disabled={saving || draft.length >= maxTiers}
          onClick={addRow}
          className="rounded border border-mast-border px-3 py-1 text-sm text-mast-text disabled:opacity-50"
        >
          {tr("+ 增加一档", "+ Add tier")}
        </button>
        <button
          type="button"
          disabled={saving}
          onClick={() => commit(draft)}
          className="rounded border border-mast-accent bg-mast-accent/10 px-3 py-1 text-sm text-mast-text disabled:opacity-50"
        >
          {tr("保存档位表", "Save tier table")}
        </button>
        <button
          type="button"
          disabled={saving || !customised}
          onClick={() => save({ scan_policy: { tiers: [] } })}
          className="rounded border border-mast-border px-3 py-1 text-sm text-mast-muted disabled:opacity-50"
        >
          {tr("恢复出厂表", "Restore factory table")}
        </button>
        <span className="text-xs text-mast-muted">
          {tr(`${draft.length} / ${maxTiers} 档`, `${draft.length} / ${maxTiers} tiers`)}
        </span>
      </div>

      {/* Preview: see what parameters an X nm image would use right after editing,
          without scanning one to check. */}
      <div className="mt-3 rounded border border-mast-border p-2">
        <Field
          label={tr("预览", "Preview")}
          hint={tr(
            "输入一个扫描边长,看这张表会给出什么参数(零硬件成本)。",
            "Enter a scan size to see the parameters this table would give (no hardware cost).",
          )}
        >
          <div className="flex items-center gap-2">
            <input
              type="text"
              inputMode="decimal"
              defaultValue={String(previewNm)}
              onBlur={(e) => {
                const n = numOr(e.target.value);
                if (n !== null && n > 0) setPreviewNm(n);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") (e.currentTarget as HTMLInputElement).blur();
              }}
              className="w-24 rounded border border-mast-border bg-mast-bg px-2 py-1 font-mono text-sm tabular-nums text-mast-text"
            />
            <span className="text-xs text-mast-muted">nm</span>
          </div>
        </Field>
        {preview.data?.ok && (
          <div className="mt-1 space-y-1 text-xs text-mast-muted">
            <div>
              {tr("档位", "Tier")} <b className="text-mast-text">{preview.data.tier_name}</b>
              {" · "}
              {preview.data.pixels}px{" · "}
              {preview.data.line_time_s}{tr("s/线", "s/line")}{" · "}
              {tr("预计", "Estimated")} <b className="text-mast-text">{fmtDuration(preview.data.estimated_scan_s)}</b>
            </div>
            {(preview.data.warnings ?? []).map((w, i) => (
              <div key={i} className="text-amber-500">
                {w}
              </div>
            ))}
            <details>
              <summary className="cursor-pointer">{tr("每个参数的来源", "Source of each parameter")}</summary>
              <ul className="mt-1 space-y-0.5 font-mono">
                {(preview.data.summary ?? []).map((line, i) => (
                  <li key={i}>{line}</li>
                ))}
              </ul>
            </details>
          </div>
        )}
        {preview.data && !preview.data.ok && (
          <p className="mt-1 text-xs text-red-500">{preview.data.error}</p>
        )}
      </div>
    </div>
  );
}
