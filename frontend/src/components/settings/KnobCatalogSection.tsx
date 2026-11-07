import { useEffect, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { ErrorNote, Spinner } from "../ui";
import { Field, Toggle } from "../controls";
import { fmtCurrent, fmtLength } from "../../lib/units";
import { buildKnobPayload, clampKnob, fmtKnobValue, type KnobLike } from "../../lib/knobs";
import type { SettingsPatch } from "@/lib/settingsWrite";
import { tr } from "@/lib/i18n";

// Settings -> a backend knob catalogue: generic renderer for any config endpoint
// that ships a knob catalogue (current monitoring, environment history, conduct).
//
// The rows come from the catalogue rather than a hard-coded list, so adding a
// knob to the backend's EDITABLE_KEYS makes it appear here with no frontend
// change. Generic rather than copied per subsystem because the whole-replace
// guard is subtle and a copied guard is one that drifts — env_history would
// have been that copy.
//
// Everything commits through buildKnobPayload; the guard is documented in
// lib/knobs.ts. No PIN: these are thresholds and retention settings, the same
// class as the classical tip knobs, not a dangerous-capability switch.

/** A knob as the backend catalogues describe it. Structurally identical across
 *  `ThresholdKnob` and `EnvHistoryKnob`, so one row component serves both. */
export interface CatalogKnob {
  key: string;
  label_zh: string;
  hint_zh: string;
  min: number;
  max: number;
  step: number;
  default: number;
  value: number;
  is_bool: boolean;
  /** `current` | `aux` on the monitoring catalogue; absent elsewhere. */
  group?: string;
  /** Finite set of levels (currently only conduct's `cd_autonomy`). Empty or
   *  absent means a continuous value, rendered as a number box.
   *
   *  The level names are owned by the backend (`autonomy.describe`). Duplicating
   *  them here would create a second definition that can drift without any error:
   *  the UI would show one level name while a different level is stored. */
  choices?: Array<{ value: number; label_zh: string }>;
}

/** Amperes get an engineering-notation echo next to the box. A dropped exponent
 *  (typing 1.2531 where 1.2531e-6 was meant) is an easy slip, and a value that
 *  reads back as "20.0 pA" is the cheapest guard against it.
 *
 *  ORDER MATTERS. `_m_per_s` has to be tested before `_s`, or a Z drift rate of
 *  5e-11 m/s would echo back as "5e-11 s"; an echo with the wrong unit is worse
 *  than no echo. Likewise `_m` is tested next to `_a`: metres and amperes both
 *  read as tiny numbers and the SI prefix hides which one is being shown. */
function unitEcho(key: string, value: number): string | null {
  if (key.endsWith("_m_per_s")) return `${fmtLength(value)}/s`;
  if (key.endsWith("_a")) return fmtCurrent(value, { placeholder: "—" });
  if (key.endsWith("_m")) return fmtLength(value);
  if (key.endsWith("_hz")) return value > 0 ? `${value} Hz` : tr("自动", "Auto");
  if (key.endsWith("_frac") || key.endsWith("_sd_warn")) {
    return `${(value * 100).toFixed(1)} %`;
  }
  if (key.endsWith("_s")) return tr(`${value} 秒`, `${value} s`);
  if (key.endsWith("_gb")) return `${value} GB`;
  if (key.endsWith("_hours")) return tr(`${value} 小时`, `${value} h`);
  if (key.endsWith("_days")) return tr(`${value} 天`, `${value} d`);
  if (key.endsWith("_bins") || key.endsWith("_segments")) return tr(`${value} 个`, `${value}`);
  if (key.endsWith("_k")) return `${value} σ`;
  return null;
}

/** Section heading per group, plus the sentence the operator needs BEFORE
 *  touching those rows. `undefined` means the catalogue has no groups
 *  (environment history) and everything renders as one flat list. */
const GROUP_META: Record<string, { title: string; note: string }> = {
  current: {
    title: tr("隧道电流", "Tunneling current"),
    note: tr(
      "示波器采下来的 2 kHz 电流。阈值单位是安培，按这台机器的真实底噪标定。",
      "The 2 kHz current captured by the oscilloscope. Thresholds are in amperes and should be calibrated to this machine's real noise floor.",
    ),
  },
  aux: {
    title: tr("辅助通道（Z 位置 / qPlus 振幅）", "Auxiliary channels (Z position / qPlus amplitude)"),
    note: tr(
      "1 Hz 低速旁路，不占示波器。阈值单位是米与赫兹——与上面那组毫无关系，不要照抄。告警出厂是关的：先跑 python -m mast.monitoring.commission 看【辅助通道】那段的建议值。",
      "A 1 Hz slow side path that does not use the oscilloscope. Thresholds are in metres and hertz, unrelated to the group above, so do not copy values across. Alarms are off by default: first run python -m mast.monitoring.commission and read the suggested values in its [auxiliary channels] section.",
    ),
  },
};

function KnobRow({
  knob,
  value,
  disabled,
  onCommit,
}: {
  knob: CatalogKnob;
  value: number;
  disabled: boolean;
  onCommit: (v: number) => void;
}) {
  const [text, setText] = useState(() => fmtKnobValue(value));
  useEffect(() => {
    setText(fmtKnobValue(value));
  }, [value]);

  const commit = () => {
    const t = text.trim();
    const n = Number.parseFloat(t);
    if (t === "" || Number.isNaN(n)) {
      setText(fmtKnobValue(value)); // revert junk rather than commit a NaN
      return;
    }
    const clamped = clampKnob(knob as KnobLike, n);
    setText(fmtKnobValue(clamped));
    if (clamped !== value) onCommit(clamped);
  };

  const choices = knob.choices ?? [];
  if (choices.length > 0) {
    // Finite levels get a segmented control, not a number box: typing "2" would
    // require remembering what each number means, and picking the wrong level on
    // conduct changes who may approve.
    const name = knob.label_zh || knob.key;
    return (
      <div className={"py-1" + (disabled ? " opacity-50" : "")}>
        <div className="text-sm text-mast-text">{name}</div>
        {knob.hint_zh && <div className="text-xs text-mast-faint">{knob.hint_zh}</div>}
        <div className="mt-1 flex flex-wrap gap-1" role="radiogroup" aria-label={name}>
          {choices.map((c) => {
            const on = Math.abs(c.value - value) < 1e-9;
            return (
              <button
                key={c.value}
                type="button"
                role="radio"
                aria-checked={on}
                disabled={disabled}
                onClick={() => !disabled && !on && onCommit(c.value)}
                className={
                  "rounded-mast-ctl border px-2 py-1 text-xs " +
                  (on
                    ? "border-mast-accent bg-mast-accent/10 text-mast-accent"
                    : "border-mast-border text-mast-muted hover:text-mast-text")
                }
              >
                {c.label_zh || String(c.value)}
              </button>
            );
          })}
        </div>
      </div>
    );
  }

  if (knob.is_bool) {
    const on = value >= 0.5;
    const name = knob.label_zh || knob.key;
    return (
      <div className={"flex items-center justify-between gap-3 py-1" + (disabled ? " opacity-50" : "")}>
        <div>
          <div className="text-sm text-mast-text">{name}</div>
          {knob.hint_zh && <div className="text-xs text-mast-faint">{knob.hint_zh}</div>}
        </div>
        <span className="flex shrink-0 items-center gap-2">
          {/* Toggle's `label` is only an aria-label, so the state gets a visible
              word too: the knob position and accent color must not be the only
              indication of whether the setting is on. */}
          <span className={"text-xs " + (on ? "text-mast-accent" : "text-mast-muted")}>
            {on ? tr("开", "On") : tr("关", "Off")}
          </span>
          <Toggle
            checked={on}
            onChange={(v) => !disabled && onCommit(v ? 1 : 0)}
            label={`${name}: ${on ? tr("开", "On") : tr("关", "Off")}`}
          />
        </span>
      </div>
    );
  }

  const echo = unitEcho(knob.key, value);
  const bounds =
    knob.max > 0 ? `${tr("范围", "Range")} ${fmtKnobValue(knob.min)} – ${fmtKnobValue(knob.max)}` : "";
  const hint = [knob.hint_zh, bounds, echo && `${tr("当前 ≈", "Now ≈")} ${echo}`]
    .filter(Boolean)
    .join(" · ");

  return (
    <Field label={knob.label_zh || knob.key} hint={hint}>
      <input
        type="text"
        inputMode="decimal"
        value={text}
        disabled={disabled}
        placeholder={fmtKnobValue(knob.default)}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.currentTarget as HTMLInputElement).blur();
        }}
        className="w-36 rounded border border-mast-border bg-mast-bg px-2 py-1 font-mono text-sm tabular-nums text-mast-text disabled:opacity-50"
      />
    </Field>
  );
}

export function KnobCatalogSection({
  configPath,
  queryKey,
  settingsKey,
  blurb,
  unavailableNote,
  settings,
  saving,
  save,
}: {
  /** The config endpoint that ships the catalogue. */
  configPath:
    | "/api/monitoring/config"
    | "/api/env-history/config"
    | "/api/conducts/config";
  queryKey: readonly unknown[];
  /** The settings-store dict this catalogue writes to, WHOLE. */
  settingsKey: "current_monitor" | "env_history" | "conduct";
  blurb: ReactNode;
  unavailableNote: string;
  settings: { data?: Record<string, unknown> | null };
  saving: boolean;
  save: (patch: SettingsPatch) => void;
}) {
  const config = useQuery({
    queryKey,
    // The catalogue is shape, not telemetry — it only changes when the backend
    // gains a knob. The effective `value`s ride along, but the persisted
    // settings outrank them in buildKnobPayload, so a stale minute is harmless.
    staleTime: 60_000,
    queryFn: async () => {
      const { data, error } = await api.GET(configPath);
      if (error) throw error;
      // conduct reports its degradation reason as `reason` (monitoring and
      // env-history use `detail`). Missing one of the names does not raise an
      // error; it silently drops the "why it cannot be read" message.
      return data as {
        knobs?: CatalogKnob[];
        degraded?: boolean;
        detail?: string;
        reason?: string;
      };
    },
  });

  if (config.isPending) return <Spinner />;
  if (config.isError) return <ErrorNote error={config.error} />;

  const knobs = config.data?.knobs ?? [];
  const persisted = (settings.data?.[settingsKey] ?? {}) as Record<string, number>;

  if (config.data?.degraded || knobs.length === 0) {
    return (
      <p className="text-sm text-mast-muted">
        {unavailableNote}
        {(() => {
          const why = config.data?.detail || config.data?.reason;
          return why ? ` (${why})` : "";
        })()}
      </p>
    );
  }

  const effective = (k: CatalogKnob): number => {
    const p = persisted[k.key];
    return typeof p === "number" && Number.isFinite(p) ? p : k.value;
  };

  const commit = (key: string, value: number) =>
    save({ [settingsKey]: buildKnobPayload(knobs as KnobLike[], persisted, { key, value }) });

  const resetDefaults = () => save({ [settingsKey]: {} });

  // Group in CATALOGUE ORDER, so the backend's EDITABLE_KEYS stays the single
  // place that decides what appears and in what sequence. A catalogue with no
  // `group` (environment history) collapses to one unlabelled section.
  const sections: Array<{ id: string | undefined; rows: CatalogKnob[] }> = [];
  for (const k of knobs) {
    const last = sections[sections.length - 1];
    if (last && last.id === k.group) last.rows.push(k);
    else sections.push({ id: k.group, rows: [k] });
  }
  const labelled = sections.some((s) => s.id && GROUP_META[s.id]);

  return (
    <div className="max-w-lg space-y-3">
      <p className="text-xs text-mast-muted">{blurb}</p>
      {sections.map((sec) => {
        const meta = sec.id ? GROUP_META[sec.id] : undefined;
        return (
          <div key={sec.id ?? "_"} className={labelled ? "space-y-3 pt-1" : "space-y-3"}>
            {meta && (
              <div className="border-t border-mast-border pt-3">
                <div className="text-sm font-medium text-mast-text">{meta.title}</div>
                <div className="mt-0.5 text-xs leading-snug text-mast-faint">
                  {meta.note}
                </div>
              </div>
            )}
            {sec.rows.map((k) => (
              <KnobRow
                key={k.key}
                knob={k}
                value={effective(k)}
                disabled={saving}
                onCommit={(v) => commit(k.key, v)}
              />
            ))}
          </div>
        );
      })}
      <div className="pt-1">
        <button
          type="button"
          onClick={resetDefaults}
          disabled={saving}
          className="rounded border border-mast-border bg-mast-panel px-3 py-1 text-xs text-mast-muted hover:text-mast-text disabled:opacity-50"
        >
          {tr("恢复默认", "Restore defaults")}
        </button>
      </div>
      {saving && <p className="text-xs text-mast-muted">{tr("保存中…", "Saving…")}</p>}
    </div>
  );
}
