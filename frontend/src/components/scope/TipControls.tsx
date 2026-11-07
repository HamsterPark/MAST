// Tip card for the right panel: which tip is mounted now, plus tip-change registration.
//
// It sits next to experiment/sample but is not their child: the tip is
// instrument-level. Changing experiment or sample does not necessarily change the
// tip, so it has no experiment_id and no "switch tip" action; tip service is linear,
// and mounting the next tip retires the previous one.
//
// The warning in the register dialog about clearing learned calibrations is
// load-bearing: the dI/dV contact calibration and the qPlus free-amplitude baseline
// belong to the previous tip, and would otherwise keep being used as truth (the
// denominator of the tip-crash criterion in particular). The user must see this
// before confirming, so it is written inline above the button rather than in a toast.

import { useState } from "react";
import clsx from "clsx";
import { Button, Field, SelectField, TextField } from "../controls";
import { ConfirmDialog } from "./ConfirmDialog";
import { tr } from "@/lib/i18n";
import {
  serviceDays,
  tipSummary,
  useCurrentTip,
  useRegisterTip,
  useRemoveCurrentTip,
  useTipHistory,
  useTipVocabulary,
  type Tip,
} from "@/api/tips";

function errText(e: unknown): string {
  if (!e) return "";
  if (e instanceof Error) return e.message;
  return typeof e === "string" ? e : JSON.stringify(e);
}

/** Numeric input: empty string = unknown (the wire diameter is often really unknown); invalid input returns null. */
function numOrNull(s: string): number | null {
  const t = s.trim();
  if (!t) return null;
  const n = Number.parseFloat(t);
  return Number.isFinite(n) && n > 0 ? n : null;
}

export function TipControls() {
  const cur = useCurrentTip();
  const [register, setRegister] = useState(false);
  const [history, setHistory] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const remove = useRemoveCurrentTip();

  const tip = cur.data?.tip ?? null;
  const days = serviceDays(tip);

  const flash = (m: string) => {
    setMsg(m);
    setTimeout(() => setMsg(null), 4000);
  };

  return (
    <div className="mt-2 space-y-2">
      <button
        className="flex w-full items-center justify-between gap-2 rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-2.5 py-1.5 text-left text-xs hover:bg-mast-panel-2"
        onClick={() => setRegister(true)}
        data-testid="tip-chip"
      >
        <span className="text-mast-muted">{tr("针尖", "Tip")}</span>
        <span
          className={
            "min-w-0 flex-1 truncate text-right " +
            (tip ? "text-mast-text" : "text-mast-faint")
          }
        >
          {tip ? tipSummary(tip) : tr("未登记", "Not registered")}
          {tip?.form === "qplus" && " ⚡"}
        </span>
        <span className="text-mast-faint">＋</span>
      </button>

      {tip && (
        <div className="px-0.5 text-[10.5px] leading-snug text-mast-faint">
          {tr(`「${tip.name}」`, `"${tip.name}"`)}
          {days !== null && <>{tr(`，已服役 ${days} 天`, `, in service ${days} d`)}</>}
          {tip.wire_diameter_mm ? <>{tr(`，线径 ${tip.wire_diameter_mm} mm`, `, wire diameter ${tip.wire_diameter_mm} mm`)}</> : null}
        </div>
      )}

      <div className="flex gap-2">
        <Button variant="ghost" onClick={() => setHistory(true)}>
          {tr("换针史", "Tip history")}
        </Button>
        {tip && (
          <Button
            variant="ghost"
            onClick={async () => {
              try {
                const r = await remove.mutateAsync();
                flash(r.ok ? tr("已记录针尖取出", "Tip removal recorded") : r.error || tr("未生效", "No effect"));
              } catch (e) {
                flash(errText(e) || tr("未送达，请重试", "Not delivered, please retry"));
              }
            }}
          >
            {tr("记录取出", "Record removal")}
          </Button>
        )}
      </div>

      <div className="text-[10.5px] leading-snug text-mast-faint">
        {tr(
          "针尖是仪器级的：换实验、换样品都不必重新登记。装了新针请登记一次——修针方案会按针尖的材料/制备/形态取参数，样品会话也会自动带上针尖信息。",
          "The tip is instrument-level: no need to re-register when you change experiment or sample. Register once when a new tip is mounted: tip-conditioning recipes take their parameters from the tip's material/preparation/form, and sample sessions carry the tip info automatically.",
        )}
      </div>
      {msg && <div className="text-[11px] text-mast-muted">{msg}</div>}

      <RegisterTipDialog open={register} onClose={() => setRegister(false)} />
      <TipHistoryDialog open={history} onClose={() => setHistory(false)} />
    </div>
  );
}

export function RegisterTipDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const vocab = useTipVocabulary(open);
  const register = useRegisterTip();
  const cur = useCurrentTip();

  const [material, setMaterial] = useState("W");
  const [fabrication, setFabrication] = useState("etched");
  const [form, setForm] = useState("stm_wire");
  const [name, setName] = useState("");
  const [diameter, setDiameter] = useState("");
  const [installedAt, setInstalledAt] = useState("");
  const [sensorModel, setSensorModel] = useState("");
  const [f0, setF0] = useState("");
  const [q, setQ] = useState("");
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const prev = cur.data?.tip ?? null;

  function reset() {
    setName("");
    setDiameter("");
    setInstalledAt("");
    setSensorModel("");
    setF0("");
    setQ("");
    setNote("");
    setErr(null);
    onClose();
  }

  async function doRegister() {
    setErr(null);
    try {
      const r = await register.mutateAsync({
        material,
        fabrication,
        form,
        name: name.trim(),
        wire_diameter_mm: numOrNull(diameter),
        installed_at: installedAt.trim(),
        qplus_sensor_model: form === "qplus" ? sensorModel.trim() : "",
        qplus_f0_hz: form === "qplus" ? numOrNull(f0) : null,
        qplus_q: form === "qplus" ? numOrNull(q) : null,
        note: note.trim(),
      });
      // Keep the dialog open and show the error inline; a silent failure is the
      // costliest failure mode here.
      if (!r.ok) {
        setErr(r.error || tr("登记未生效", "Registration had no effect"));
        return;
      }
      reset();
    } catch (e) {
      setErr(errText(e) || tr("登记未送达——网络或后端不可达，请重试", "Registration was not delivered: network or backend unreachable. Please retry."));
    }
  }

  const materials = vocab.data?.materials ?? [{ value: "W", label: tr("钨 (W)", "Tungsten (W)") }];
  const fabrications = vocab.data?.fabrications ?? [
    { value: "etched", label: tr("电化学腐蚀", "Electrochemically etched") },
  ];
  const forms = vocab.data?.forms ?? [
    { value: "stm_wire", label: tr("普通 STM 金属丝针尖", "Standard STM wire tip") },
  ];

  return (
    <ConfirmDialog
      open={open}
      title={tr("登记装入针尖", "Register mounted tip")}
      confirmLabel={tr("登记这根针尖", "Register this tip")}
      busy={register.isPending}
      busyLabel={tr("登记中…", "Registering…")}
      error={err}
      onConfirm={doRegister}
      onClose={reset}
    >
      <div className="space-y-3">
        <p className="text-xs text-mast-muted">
          {tr(
            "记录「装入了一根新针尖」。针尖是仪器级的，与当前实验/样品无关。",
            "Records that a new tip was mounted. The tip is instrument-level and independent of the current experiment/sample.",
          )}
        </p>

        <Field label={tr("材料", "Material")}>
          <SelectField value={material} onChange={setMaterial} options={materials} />
        </Field>
        <Field label={tr("制备方式", "Preparation")}>
          <SelectField
            value={fabrication}
            onChange={setFabrication}
            options={fabrications}
          />
        </Field>
        <Field label={tr("形态", "Form")} hint={tr("qPlus 型针尖会对「戳表面」类处理自动加一道门。", "qPlus tips automatically get an extra gate on tip-poking treatments.")}>
          <SelectField value={form} onChange={setForm} options={forms} />
        </Field>
        <Field label={tr("名称（可留空）", "Name (optional)")} hint={tr("留空会按材料和序号自动生成，如 W-etched #3。", "If empty, a name is generated from material and index, e.g. W-etched #3.")}>
          <TextField value={name} onChange={setName} placeholder={tr("如 W-etched #3", "e.g. W-etched #3")} />
        </Field>
        <Field label={tr("线材直径 mm（如果知道）", "Wire diameter, mm (if known)")}>
          <TextField value={diameter} onChange={setDiameter} placeholder={tr("如 0.25", "e.g. 0.25")} />
        </Field>
        <Field
          label={tr("装入日期（可留空 = 现在）", "Installed on (empty = now)")}
          hint={tr("可以回填——过一两天才想起来记是常事。格式 2026-07-28。", "Backfilling is fine. Format: 2026-07-28.")}
        >
          <TextField
            value={installedAt}
            onChange={setInstalledAt}
            placeholder="2026-07-28"
          />
        </Field>

        {form === "qplus" && (
          <div className="space-y-3 rounded border border-mast-border bg-mast-panel-2 px-3 py-2">
            <p className="text-[11px] text-mast-muted">
              {tr(
                "qPlus 传感器的标称参数（铭牌/型录值）。实测的共振频率和 Q 由 PLL 频率扫描自动写入，与这里分开存。",
                "Nominal qPlus sensor parameters (nameplate/catalog values). The measured resonance frequency and Q are written automatically by the PLL frequency sweep and stored separately.",
              )}
            </p>
            <Field label={tr("音叉型号", "Tuning-fork model")}>
              <TextField
                value={sensorModel}
                onChange={setSensorModel}
                placeholder={tr("如 qPlus TF-32k", "e.g. qPlus TF-32k")}
              />
            </Field>
            <Field label={tr("标称共振频率 f₀ (Hz)", "Nominal resonance frequency f₀ (Hz)")}>
              <TextField value={f0} onChange={setF0} placeholder={tr("如 32768", "e.g. 32768")} />
            </Field>
            <Field label={tr("标称 Q", "Nominal Q")}>
              <TextField value={q} onChange={setQ} placeholder={tr("如 30000", "e.g. 30000")} />
            </Field>
          </div>
        )}

        <Field label={tr("备注", "Note")}>
          <TextField value={note} onChange={setNote} placeholder={tr("可留空", "Optional")} />
        </Field>

        {prev && (
          <div className="rounded border border-mast-warn/40 bg-mast-warn/10 px-3 py-2 text-xs text-mast-text">
            {tr(
              `当前登记的是「${prev.name}」。登记新针尖会把它标记为已取出，并清除绑它的学习标定（到样品 dI/dV 标定值、qPlus 自由振幅基线）——那些量是上一根针学出来的，换针后继续用就是错的。旧值会归档进那一行，日后仍可查。`,
              `The currently registered tip is "${prev.name}". Registering a new tip marks it as removed and clears the learned calibrations bound to it (tip-to-sample dI/dV calibration, qPlus free-amplitude baseline): those values were learned on the previous tip and are wrong for the new one. The old values are archived on that tip's row and remain available.`,
            )}
          </div>
        )}
      </div>
    </ConfirmDialog>
  );
}

/**
 * Tip history: the single tip-card rendering in the app.
 *
 * Exported so the experiment-records page can link to the tip card (tips have no
 * route of their own) instead of redrawing it. A second copy would drift, and the
 * card shows material/preparation/service period, facts that are used to make
 * decisions.
 *
 * `highlightId` outlines one row. Without a per-tip route this is the closest form
 * of a link: it opens the real card, not a lookalike.
 */
export function TipHistoryDialog({
  open,
  onClose,
  highlightId,
}: {
  open: boolean;
  onClose: () => void;
  highlightId?: string | null;
}) {
  const history = useTipHistory(open);
  const tips = history.data?.tips ?? [];

  return (
    <ConfirmDialog
      open={open}
      title={tr("换针史", "Tip history")}
      confirmLabel={tr("关闭", "Close")}
      cancelLabel={tr("返回", "Back")}
      wide
      onConfirm={onClose}
      onClose={onClose}
    >
      {history.isLoading ? (
        <p className="text-sm text-mast-muted">{tr("加载中…", "Loading…")}</p>
      ) : tips.length === 0 ? (
        <p className="text-sm text-mast-muted">{tr("还没有登记过针尖。", "No tips registered yet.")}</p>
      ) : (
        <div className="max-h-96 space-y-2 overflow-y-auto">
          {tips.map((t: Tip) => (
            <div
              key={t.id}
              className={clsx(
                "rounded border px-3 py-2 text-xs",
                t.id === highlightId
                  ? "border-mast-accent bg-mast-accent-soft"
                  : "border-mast-border bg-mast-panel",
              )}
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="font-semibold text-mast-text">
                  {t.tip_index ? `T${String(t.tip_index).padStart(2, "0")} ` : ""}
                  {t.name || tr("未命名", "Unnamed")}
                </span>
                <span
                  // The success colour token is `mast-auto` (`mast-ok` does not exist
                  // and would compile to nothing).
                  className={t.removed_at ? "text-mast-faint" : "text-mast-auto"}
                >
                  {t.removed_at ? tr("已取出", "Removed") : tr("在用", "In use")}
                </span>
              </div>
              <div className="mt-0.5 text-mast-muted">{tipSummary(t)}</div>
              <div className="mt-0.5 text-mast-faint">
                {t.installed_at ? tr(`${t.installed_at.slice(0, 10)} 装入`, `Installed ${t.installed_at.slice(0, 10)}`) : tr("装入日期未记录", "Install date not recorded")}
                {t.removed_at ? tr(` → ${t.removed_at.slice(0, 10)} 取出`, ` → removed ${t.removed_at.slice(0, 10)}`) : ""}
                {t.wire_diameter_mm ? tr(` · 线径 ${t.wire_diameter_mm} mm`, ` · wire diameter ${t.wire_diameter_mm} mm`) : ""}
              </div>
              {t.note && <div className="mt-0.5 text-mast-muted">{t.note}</div>}
              {Object.keys(t.retire_snapshot || {}).length > 0 && (
                <div className="mt-1 text-[10.5px] text-mast-faint">
                  {tr("退役时归档的标定：", "Calibrations archived at retirement: ")}{Object.keys(t.retire_snapshot).join(tr("、", ", "))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </ConfirmDialog>
  );
}
