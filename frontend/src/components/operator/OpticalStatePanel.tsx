import { useState } from "react";
import clsx from "clsx";
import { Section, Badge, ErrorNote, Spinner, EmptyNote } from "@/components/ui";
import {
  useClearSlot,
  useIsOpticalStation,
  useOpticalHistory,
  useOpticalState,
  useSetSlot,
} from "@/api/operator";
import { useNow } from "@/hooks/useNow";
import { useOperatorName } from "@/hooks/useOperatorPrefs";
import { tr } from "@/lib/i18n";
import {
  LASER_AREA_SAFE,
  buildSlotClearBody,
  buildSlotPutBody,
  displayValue,
  formatRemaining,
  laserSafeMeaning,
  optionBand,
  remainingMs,
  setByLine,
  slotRangeHint,
  slotStatus,
  summarize,
  validateSlotInput,
  type HistoryEntry,
  type OpticalSlot,
  type SlotStatus,
  type SlotValue,
} from "@/lib/opticalState";
import { failureText, fmtClock, sourceLabel } from "@/lib/operatorPrompts";
import { OperatorNameField } from "./OperatorNameField";

// The manual optical state: what a person has physically set up and no software can
// read back (which λ/2 is in the beam, the manual polarizer angle, the objective, the
// edge filter, the NPI pump current, the laser-area-safe signature). Only the operator
// writes it; the AI reads it, and every spectrum's sidecar records the snapshot taken
// when it was acquired. A value that has run out of date is shown in red.
//
// Shown for optical stations only (`station_profile: "optical"`); an STM station never
// loads it.

const INPUT =
  "rounded border border-mast-border bg-mast-bg px-3 py-1.5 text-sm text-mast-text placeholder:text-mast-muted focus:border-mast-accent focus:outline-none";
const PRIMARY = "rounded bg-mast-accent px-4 py-1.5 text-sm font-semibold text-mast-accent-ink hover:opacity-90 disabled:opacity-50";
const SECONDARY = "rounded border border-mast-border-strong px-4 py-1.5 text-sm text-mast-text hover:bg-mast-panel-2 disabled:opacity-50";

const STATUS_TEXT: Record<SlotStatus, () => string> = {
  valid: () => tr("有效", "Valid"),
  expired: () => tr("已过期", "Expired"),
  unset: () => tr("未设置", "Not set"),
};
const STATUS_TONE: Record<SlotStatus, string> = { valid: "AUTO", expired: "DANGEROUS", unset: "default" };

function SlotRow({ slot, sv, name, nowMs }: { slot: OpticalSlot; sv: SlotValue | undefined; name: string; nowMs: number }) {
  const status = slotStatus(slot, sv, nowMs);
  const laser = slot.id === LASER_AREA_SAFE;
  const set = useSetSlot();
  const clear = useClearSlot();
  // The draft starts from what is recorded and is never overwritten by a poll while it is being edited.
  const [choice, setChoice] = useState<string>(slot.kind === "choice" && sv ? String(sv.value) : "");
  const [text, setText] = useState<string>((slot.kind === "number" || slot.kind === "text") && sv ? String(sv.value) : "");
  const [flag, setFlag] = useState(false);
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const busy = set.isPending || clear.isPending;
  const signed = slot.kind === "flag" && status !== "unset";

  const save = () => {
    setProblem(null);
    const v = validateSlotInput(slot, { choice, text, flag });
    if (!v.ok) return setProblem(v.error);
    const body = buildSlotPutBody(name, v.value, note);
    if (!body.ok) return setProblem(body.error);
    set.mutate(
      { slot: slot.id, body: body.value },
      { onSuccess: () => setNote(""), onError: (e) => setProblem(failureText(e, "slot")) },
    );
  };
  const wipe = () => {
    setProblem(null);
    const body = buildSlotClearBody(name);
    if (!body.ok) return setProblem(body.error);
    clear.mutate(
      { slot: slot.id, body: body.value },
      { onSuccess: () => { setFlag(false); setNote(""); }, onError: (e) => setProblem(failureText(e, "slot")) },
    );
  };

  const edge =
    status === "expired"
      ? "border-2 border-mast-danger"
      : laser
        ? status === "valid"
          ? "border-2 border-mast-auto"
          : "border-2 border-mast-warn"
        : "border border-mast-border";
  const left = remainingMs(sv, nowMs);

  return (
    <div className={clsx("rounded-mast-card bg-mast-panel p-3", edge)} data-slot={slot.id} data-status={status}>
      <div className="flex flex-wrap items-center gap-2">
        <b className="text-sm text-mast-text">{slot.label}</b>
        <Badge tone={STATUS_TONE[status]}>
          {laser && status === "unset" ? tr("未签署", "Not signed") : laser && status === "valid" ? tr("已签署", "Signed") : STATUS_TEXT[status]()}
        </Badge>
        {sv && status !== "unset" && <span className={clsx("text-xs", status === "expired" ? "font-semibold text-mast-danger" : "text-mast-muted")}>{formatRemaining(left)}</span>}
        <code className="ml-auto text-[11px] text-mast-faint">{slot.id}</code>
      </div>
      {slot.description && !laser && <p className="mt-1 text-xs text-mast-muted">{slot.description}</p>}

      <p className="mt-2 text-sm text-mast-text">
        <span className="text-mast-muted">{tr("当前：", "Now: ")}</span>
        <span className={clsx("font-medium", status === "expired" && "text-mast-danger")}>{status === "unset" && slot.kind !== "flag" ? "—" : displayValue(slot, sv)}</span>
        {sv && <span className="ml-2 text-xs text-mast-muted">{setByLine(sv)}</span>}
      </p>
      {sv?.note && <p className="text-xs text-mast-muted">{tr("备注：", "Note: ")}{sv.note}</p>}
      {sv?.valid_until && (
        <p className="text-xs text-mast-muted">
          {tr("有效至 ", "Valid until ")}
          {fmtClock(sv.valid_until)}
        </p>
      )}

      {laser && (
        <div className="mt-2 rounded-mast-ctl border border-mast-danger-border bg-mast-danger-bg px-3 py-2 text-xs text-mast-text">
          <p className="font-semibold text-mast-danger">{tr("签署「激光区安全」意味着什么", "What signing “laser area safe” means")}</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4">
            {laserSafeMeaning().map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
      )}

      <form
        className="mt-3 flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        {slot.kind === "choice" && (
          <select value={choice} onChange={(e) => setChoice(e.target.value)} aria-label={slot.label} className={clsx(INPUT, "min-w-[14rem]")}>
            <option value="">{tr("— 选择 —", "— choose —")}</option>
            {slot.options.map((o) => {
              const band = optionBand(o);
              return (
                <option key={o.id} value={o.id}>
                  {o.label}
                  {band ? ` (${band})` : ""}
                </option>
              );
            })}
          </select>
        )}
        {slot.kind === "number" && (
          <>
            <input value={text} onChange={(e) => setText(e.target.value)} inputMode="decimal" spellCheck={false} aria-label={slot.label} className={clsx(INPUT, "w-32 font-mono")} />
            {slot.unit && <span className="text-sm text-mast-text">{slot.unit}</span>}
            <span className="text-xs text-mast-muted">{slotRangeHint(slot)}</span>
          </>
        )}
        {slot.kind === "text" && <input value={text} onChange={(e) => setText(e.target.value)} aria-label={slot.label} className={clsx(INPUT, "min-w-[14rem] flex-1")} />}
        {slot.kind === "flag" && (
          <label className="flex items-center gap-2 text-sm font-medium text-mast-text">
            <input type="checkbox" checked={flag} onChange={(e) => setFlag(e.target.checked)} />
            {laser ? tr("我确认激光区安全（见上）", "I confirm the laser area is safe (see above)") : tr("已确认", "Confirmed")}
          </label>
        )}
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder={tr("备注（可选）", "Note (optional)")}
          aria-label={tr("备注", "Note")}
          className={clsx(INPUT, "min-w-[10rem] flex-1")}
        />
        <button type="submit" disabled={busy} className={PRIMARY}>
          {slot.kind === "flag" ? (signed ? tr("重新签署", "Sign again") : tr("签署", "Sign")) : tr("保存", "Save")}
        </button>
        {sv && (
          <button type="button" disabled={busy} onClick={wipe} className={SECONDARY}>
            {slot.kind === "flag" ? tr("撤销签署", "Revoke") : tr("清除", "Clear")}
          </button>
        )}
      </form>
      {problem && (
        <p role="alert" className="mt-2 text-sm text-mast-danger">
          {problem}
        </p>
      )}
    </div>
  );
}

function historyValue(slot: OpticalSlot | undefined, e: HistoryEntry): string {
  if (e.value === null) return tr("（已清除）", "(cleared)");
  if (!slot) return String(e.value);
  return displayValue(slot, { ...e, valid: true });
}

function OpticalStateBody() {
  const q = useOpticalState(true);
  const hist = useOpticalHistory(30, true);
  const [name] = useOperatorName();
  const nowMs = useNow(10_000);

  const snap = q.data;
  const counts = snap ? summarize(snap, nowMs) : null;
  const slotById = new Map((snap?.slots ?? []).map((s) => [s.id, s]));

  return (
    <Section
      title={tr("光学状态（手动登记）", "Optical state (set by hand)")}
      subtitle={tr("软件读不到、只有你知道的那些量", "What no software can read back; only you know it")}
      actions={
        counts && (counts.expired > 0 || counts.unset > 0) ? (
          <div className="flex gap-1.5">
            {counts.expired > 0 && <Badge tone="DANGEROUS">{tr(`${counts.expired} 项已过期`, `${counts.expired} expired`)}</Badge>}
            {counts.unset > 0 && <Badge>{tr(`${counts.unset} 项未设置`, `${counts.unset} not set`)}</Badge>}
          </div>
        ) : undefined
      }
    >
      <p className="mb-3 text-sm text-mast-muted">
        {tr(
          "装的是哪片 λ/2、检偏角度、物镜、滤光片、激光区是否安全……只有你能在这里写；AI 只能读。每条谱的 sidecar 会自动记下采集那一刻的快照。每次修改都要填姓名，并写入历史。",
          "Which λ/2 is in the beam, the polarizer angle, the objective, the edge filter, whether the laser area is safe... Only you can write them here; the AI can only read. Every spectrum's sidecar records the snapshot at acquisition. Each change needs your name and goes into the history.",
        )}
      </p>
      <div className="mb-3">
        <OperatorNameField />
      </div>

      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} label={tr("读取光学状态失败", "Could not read the optical state")} />}
      <div className="space-y-3">
        {snap?.slots.map((slot) => (
          <SlotRow key={slot.id} slot={slot} sv={snap.values[slot.id]} name={name} nowMs={nowMs} />
        ))}
      </div>
      {snap && snap.slots.length === 0 && <EmptyNote label={tr("这个站点没有定义光学状态槽位。", "This station defines no optical-state slots.")} />}

      <details className="mt-4 rounded-mast-card border border-mast-border bg-mast-panel px-4 py-2">
        <summary className="cursor-pointer text-sm text-mast-muted hover:text-mast-text">
          {tr(`最近的修改（${hist.data?.length ?? 0}）`, `Recent changes (${hist.data?.length ?? 0})`)}
        </summary>
        {hist.isError && <ErrorNote error={hist.error} label={tr("读取历史失败", "Could not read the history")} />}
        {hist.data && hist.data.length === 0 && <p className="mt-2 text-xs text-mast-muted">{tr("还没有修改记录。", "No changes recorded yet.")}</p>}
        <ul className="mt-2">
          {(hist.data ?? []).map((e, i) => (
            <li key={`${e.set_at}-${e.slot}-${i}`} className="flex flex-wrap items-baseline gap-x-2 border-b border-mast-border py-1.5 text-xs last:border-b-0">
              <span className="text-mast-muted">{fmtClock(e.set_at)}</span>
              <b className="text-mast-text">{slotById.get(e.slot)?.label ?? e.slot}</b>
              <span className="text-mast-text">→ {historyValue(slotById.get(e.slot), e)}</span>
              <span className="ml-auto text-mast-muted">
                {e.set_by}
                {e.source ? ` · ${sourceLabel(e.source)}` : ""}
              </span>
              {e.note && e.note !== "cleared" && <span className="basis-full text-mast-muted">{tr("备注：", "Note: ")}{e.note}</span>}
            </li>
          ))}
        </ul>
      </details>
    </Section>
  );
}

/** The optical-state section; nothing at all on an STM station. */
export function OpticalStatePanel() {
  if (!useIsOpticalStation()) return null;
  return <OpticalStateBody />;
}
