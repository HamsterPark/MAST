// Card for one motorised device (contract §13.1): label, model, online state, holder, tip
// requirement, axis readouts, buttons.
//
// **Rendered entirely from the description**: buttons, skills, parameter names / units / ranges all
// come from GET /api/manual/devices; what to draw is decided by cardPlan in lib/manual/plan.ts
// (pinned by unit tests) and this file only draws it:
//   - direction = "signed" -> two buttons, - and +, with the magnitude field between them;
//     per_axis -> one row per axis;
//   - stop actions (bypass_lock) sit in the card header: always enabled, no dialog, ignore the lock;
//   - "Unlock manual control": confirmed once (lists device, tip requirement, per-step limits) and
//     re-locks after 10 minutes without activity; while unlocked CONFIRM-level actions are no
//     longer confirmed one by one, DANGEROUS-level ones still are; with hold_repeat_ms set, holding
//     a button repeats the jog.
//   - Confirmations are **blocks inside the card**, never modal overlays: an overlay would cover
//     the stop button (see the header of SkillRunButton).
//   - Execution is always POST /api/skills/{skill}/execute; when the token is taken (refused_busy)
//     the holder is named as reported.

import { useState, type ReactNode } from "react";
import clsx from "clsx";
import { Badge, Card } from "@/components/ui";
import { tr } from "@/lib/i18n";
import { NumberField, SelectField, TextField } from "@/components/controls";
import { runSkill, type RunOutcome } from "@/components/skills/SkillRunButton";
import { fetchInstrumentHolder, useRefreshManualDevicesSoon } from "@/api/manual";
import { useCardVisibility, useDeviceReadout, useUnlock } from "@/hooks/useManual";
import { fmtValue } from "@/lib/live/scale";
import {
  actionGate,
  axisExtraBits,
  buildActionParams,
  busyHolderText,
  defaultValues,
  holderBlocksStop,
  jogOutcome,
  limitLines,
  MIN_POLL_S,
  restLabel,
  stopHadNothingToStop,
  TIP_REQUIREMENT_LABEL,
  wantsBatchReadout,
  type ManualAction,
  type ManualDevice,
  type ManualParamSpec,
} from "@/lib/manual/protocol";
import { cardPlan, controlKey, type PlannedControl } from "@/lib/manual/plan";
import { HintList } from "./HintList";
import { HoldJogButton, type HoldSpec } from "./HoldJogButton";

type Outcome = { kind: "running" } | { kind: "ok"; text: string } | { kind: "note"; text: string } | { kind: "failed"; text: string };

interface Pending {
  action: ManualAction;
  axis: string | null;
  params: Record<string, unknown>;
}

const SAFETY_TONE: Record<string, string> = { auto: "AUTO", confirm: "WARN", dangerous: "DANGEROUS" };

function outcomeOf(o: RunOutcome): Outcome {
  if (o.kind === "ok") {
    const s = o.resp.summary ? `${tr("：", ": ")}${o.resp.summary}` : "";
    // Stopping a device MAST has not opened: nothing was stopped, so do not report "done"
    if (stopHadNothingToStop(o.resp.data)) return { kind: "note", text: tr(`没有可停的${s}`, `Nothing to stop${s}`) };
    return { kind: "ok", text: tr(`完成${s}（${o.resp.elapsed_s?.toFixed(1) ?? "?"} s）`, `Done${s} (${o.resp.elapsed_s?.toFixed(1) ?? "?"} s)`) };
  }
  const busy = busyHolderText(o.resp?.data);
  return { kind: "failed", text: busy ? tr(`被拒：${busy}。${o.message}`, `Refused: ${busy}. ${o.message}`) : o.message };
}

function fmtSeconds(s: number): string {
  return String(Math.max(MIN_POLL_S, s));
}

function fmtCountdown(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function paramsText(p: Record<string, unknown>): string {
  return Object.entries(p)
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(tr("，", ", "));
}

export function ManualDeviceCard({ d }: { d: ManualDevice }) {
  const u = useUnlock(d.id);
  const readout = useDeviceReadout(d);
  // A local card joins the batch readout only while on screen (the manual-control section sends one request covering all on-screen cards)
  const visRef = useCardVisibility(d.id, wantsBatchReadout(d));
  const plan = cardPlan(d, u.unlocked);
  const [values, setValues] = useState<Record<string, string>>({});
  const [outcomes, setOutcomes] = useState<Record<string, Outcome>>({});
  /** Per-control notice shown when a hold ends: placed in the result row, not under the button. */
  const [holdNotes, setHoldNotes] = useState<Record<string, string>>({});
  const setHoldNote = (k: string, note: string | null) =>
    setHoldNotes((m) => {
      const n = { ...m };
      if (note) n[k] = note;
      else delete n[k];
      return n;
    });
  const [pending, setPending] = useState<Pending | null>(null);
  const [askUnlock, setAskUnlock] = useState(false);
  const stopAct = plan.stops[0] ?? null;

  const vkey = (a: ManualAction, axis: string | null, p: string) => `${controlKey(a, axis)}|${p}`;
  const valuesFor = (a: ManualAction, axis: string | null): Record<string, string> => {
    const out = { ...defaultValues(a) };
    for (const p of a.params) {
      const v = values[vkey(a, axis, p.name)];
      if (v !== undefined) out[p.name] = v;
    }
    return out;
  };
  const setOutcome = (k: string, o: Outcome) => setOutcomes((m) => ({ ...m, [k]: o }));

  const refreshSoon = useRefreshManualDevicesSoon();
  const execute = async (a: ManualAction, axis: string | null, params: Record<string, unknown>): Promise<RunOutcome> => {
    const k = controlKey(a, axis);
    u.touch();
    setOutcome(k, { kind: "running" });
    const o = await runSkill(a.skill, params);
    setOutcome(k, outcomeOf(o));
    // State in the description (optics step counts, connected, holder) changes with actions: refresh the device list after a short pause
    refreshSoon();
    return o;
  };

  const trigger = (a: ManualAction, axis: string | null, sign?: 1 | -1) => {
    const built = buildActionParams(a, { axis, values: valuesFor(a, axis), sign });
    if (!built.ok) {
      setOutcome(controlKey(a, axis), { kind: "failed", text: built.error });
      return;
    }
    // Evaluate the gate at press time: the unlock may just have expired
    if (actionGate(a, u.unlocked) === "confirm") {
      setPending({ action: a, axis, params: built.params });
      return;
    }
    void execute(a, axis, built.params);
  };

  const stopParams = (a: ManualAction, axis: string | null) =>
    a.perAxis && axis ? { ...a.fixed, [a.axisParam]: axis } : { ...a.fixed };

  const holdFor = (c: PlannedControl, sign: 1 | -1): HoldSpec | null => {
    if (!c.holdable || c.action.holdRepeatMs === null) return null;
    const a = c.action;
    return {
      intervalMs: a.holdRepeatMs!,
      stopOnRelease: a.stopOnRelease && stopAct !== null,
      send: async () => {
        const built = buildActionParams(a, { axis: c.axis, values: valuesFor(a, c.axis), sign });
        if (!built.ok) {
          setOutcome(c.key, { kind: "failed", text: built.error });
          return "skipped";
        }
        // refused (refused_busy, execution entry unavailable, request rejected) = this jog did not run: no trailing stop on release, so someone else's motion is not cut short
        return jogOutcome(await execute(a, c.axis, built.params));
      },
      stop: stopAct ? () => runSkill(stopAct.skill, stopParams(stopAct, c.axis)) : undefined,
      // Read the token holder at release: if a composite skill or someone else holds it, send no stop (if unreadable, the usual rule applies)
      stopGuard: async () => {
        const reason = holderBlocksStop(await fetchInstrumentHolder());
        return reason ? { allow: false, reason } : { allow: true };
      },
    };
  };

  const input = (a: ManualAction, axis: string | null, p: ManualParamSpec) => {
    const k = vkey(a, axis, p.name);
    const v = values[k] ?? defaultValues(a)[p.name] ?? "";
    const set = (nv: string) => setValues((m) => ({ ...m, [k]: nv }));
    const unit = p.unit ? <span className="text-[11px] text-mast-faint">{p.unit}</span> : null;
    const title = [p.description, p.min !== null || p.max !== null ? tr(`范围 ${p.min ?? "−∞"} … ${p.max ?? "∞"}`, `range ${p.min ?? "−∞"} … ${p.max ?? "∞"}`) : ""]
      .filter(Boolean)
      .join(tr("；", "; "));
    if (p.allowed && p.allowed.length) {
      return (
        <span key={p.name} className="inline-flex items-center gap-1" title={title}>
          <span className="inline-block [&_select]:py-1">
            <SelectField value={v || p.allowed[0]!} onChange={set} options={p.allowed.map((x) => ({ value: x, label: x }))} />
          </span>
          {unit}
        </span>
      );
    }
    const numeric = p.type === "float" || p.type === "int";
    // The magnitude field of a signed action shows no parameter name (the action name already says what it is); the name goes in the hover title
    const magnitude = a.direction === "signed" && p.name === a.directionParam;
    return (
      <span key={p.name} className="inline-flex items-center gap-1" title={magnitude ? `${p.name}${title ? `${tr("；", "; ")}${title}` : ""}` : title}>
        {!magnitude && <span className="text-[11px] text-mast-faint">{p.name}</span>}
        <span className="inline-block w-24 [&_input]:w-full [&_input]:py-1">
          {numeric ? <NumberField value={v} onChange={set} /> : <TextField value={v} onChange={set} />}
        </span>
        {unit}
      </span>
    );
  };

  const controls = (c: PlannedControl): ReactNode => {
    const a = c.action;
    const ins = c.inputs.map((p) => input(a, c.axis, p));
    const tone = a.safety === "dangerous" ? tr("（危险）", " (dangerous)") : "";
    if (c.kind === "signed") {
      return (
        <span key={c.key} className="inline-flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] text-mast-muted">
            {a.label}
            {tone}
          </span>
          <HoldJogButton
            disabled={c.disabled}
            hold={holdFor(c, -1)}
            onSingle={() => trigger(a, c.axis, -1)}
            onNote={(n) => setHoldNote(c.key, n)}
            title={tr(`${a.label} 负方向${a.hint ? `：${a.hint}` : ""}`, `${a.label} negative direction${a.hint ? `: ${a.hint}` : ""}`)}
          >
            −
          </HoldJogButton>
          {ins}
          <HoldJogButton
            disabled={c.disabled}
            hold={holdFor(c, 1)}
            onSingle={() => trigger(a, c.axis, 1)}
            onNote={(n) => setHoldNote(c.key, n)}
            title={tr(`${a.label} 正方向${a.hint ? `：${a.hint}` : ""}`, `${a.label} positive direction${a.hint ? `: ${a.hint}` : ""}`)}
          >
            +
          </HoldJogButton>
        </span>
      );
    }
    return (
      <span key={c.key} className="inline-flex flex-wrap items-center gap-1.5">
        {ins}
        <button
          type="button"
          disabled={c.disabled}
          onClick={() => trigger(a, c.axis)}
          title={a.hint ?? undefined}
          className={clsx(
            "rounded-mast-ctl border px-2.5 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-50",
            a.safety === "dangerous"
              ? "border-mast-danger-border bg-mast-danger-bg text-mast-danger"
              : "border-mast-border-strong bg-mast-panel text-mast-text hover:bg-mast-panel-2",
          )}
        >
          {a.label}
          {tone}
        </button>
      </span>
    );
  };

  const outcomeLine = (a: ManualAction, axis: string | null) => {
    const k = controlKey(a, axis);
    const o = outcomes[k];
    const note = holdNotes[k];
    if (!o && !note) return null;
    const where = `${a.label}${axis ? ` (${axis})` : ""}`;
    return (
      <div key={`o:${k}`}>
        {o && (
          <div
            role={o.kind === "failed" ? "alert" : "status"}
            className={clsx(
              "flex items-start gap-1 text-[11px]",
              o.kind === "ok"
                ? "text-mast-auto"
                : o.kind === "failed"
                  ? "text-mast-danger"
                  : o.kind === "note"
                    ? "text-mast-warn"
                    : "text-mast-muted",
            )}
          >
            <span className="min-w-0 break-words">
              {where}{tr("：", ": ")}{o.kind === "running" ? tr("执行中…", "running…") : o.text}
            </span>
            {o.kind !== "running" && (
              <button
                type="button"
                className="shrink-0 text-mast-faint hover:text-mast-text"
                aria-label={tr("清除这条结果", "Dismiss this result")}
                onClick={() => {
                  setOutcomes((m) => {
                    const n = { ...m };
                    delete n[k];
                    return n;
                  });
                  setHoldNote(k, null);
                }}
              >
                ✕
              </button>
            )}
          </div>
        )}
        {note && (
          <div className="text-[10.5px] text-mast-faint">
            {tr(`按住${where}：${note}`, `Holding ${where}: ${note}`)}
          </div>
        )}
      </div>
    );
  };

  const limits = limitLines(d);
  const tip = d.tipRequirement ? TIP_REQUIREMENT_LABEL[d.tipRequirement] ?? d.tipRequirement : null;

  return (
    <div ref={visRef} className="min-w-0">
    <Card className="min-w-0">
      {/* Card header: name, status badges, unlock switch, stop */}
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-sm font-semibold text-mast-text">{plan.title}</span>
            <Badge>{plan.kindLabel}</Badge>
            {plan.badges.map((b) => (
              <Badge key={b.text} tone={b.tone}>
                {b.text}
              </Badge>
            ))}
          </div>
          <div className="mt-0.5 font-mono text-[11px] text-mast-faint">
            {[d.vendor, d.model, d.serial && `SN ${d.serial}`, d.id].filter(Boolean).join(" · ")}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {plan.showUnlock &&
            (u.unlocked ? (
              <span className="inline-flex items-center gap-1.5 text-xs text-mast-warn">
                {tr(`已解锁 · ${fmtCountdown(u.remainingMs ?? 0)} 后自动上锁`, `Unlocked · locks again in ${fmtCountdown(u.remainingMs ?? 0)}`)}
                <button
                  type="button"
                  onClick={() => {
                    u.lock();
                    setPending(null);
                  }}
                  className="rounded-mast-ctl border border-mast-border px-2 py-0.5 text-xs text-mast-muted hover:text-mast-text"
                >
                  {tr("上锁", "Lock")}
                </button>
              </span>
            ) : (
              <button
                type="button"
                disabled={!d.available}
                onClick={() => setAskUnlock(true)}
                className="rounded-mast-ctl border border-mast-border-strong px-2 py-1 text-xs text-mast-text hover:bg-mast-panel-2 disabled:opacity-50"
              >
                {tr("解锁手动控制", "Unlock manual control")}
              </button>
            ))}
          {plan.stops.map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => void execute(s, null, stopParams(s, null))}
              className="rounded-mast-ctl border border-mast-danger-border bg-mast-danger-bg px-2.5 py-1 text-xs font-semibold text-mast-danger hover:opacity-90"
              title={tr(`${s.hint ? `${s.hint}。` : ""}${s.skill}（免令牌，不弹框）`, `${s.hint ? `${s.hint}. ` : ""}${s.skill} (no token needed, no dialog)`)}
            >
              {s.label || tr("停止", "Stop")}
            </button>
          ))}
        </div>
      </div>

      {plan.unavailableText && (
        <div className="mt-2 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-1.5 text-xs text-mast-warn">
          {plan.unavailableText}
        </div>
      )}

      {/* Unlock confirmation (inside the card, no overlay) */}
      {askUnlock && !u.unlocked && (
        <div className="mt-2 space-y-1 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-xs text-mast-warn">
          <div className="font-semibold">{tr(`解锁 ${d.label} 的手动控制？`, `Unlock manual control of ${d.label}?`)}</div>
          <div>{tr("设备：", "Device: ")}{d.id}</div>
          <div>{tr("针尖要求：", "Tip requirement: ")}{tip ?? tr("描述里没写", "not stated in the description")}{tr("（由技能判定，面板只显示）", " (judged by the skill; the panel only displays it)")}</div>
          {limits.length > 0 && <div>{tr("单次上限：", "Per-step limits: ")}{limits.join(tr("；", "; "))}</div>}
          <div>{tr("10 分钟没有操作自动上锁。解锁期间点动不再逐次确认，危险级照样每次确认；停止永远可按。", "Locks again after 10 minutes without activity. While unlocked, jogs are no longer confirmed one by one; dangerous-level actions are still confirmed every time; stop is always available.")}</div>
          <div className="flex gap-2 pt-1">
            <button
              type="button"
              className="rounded-mast-ctl border border-mast-warn-border bg-mast-panel px-2.5 py-1 font-semibold text-mast-warn"
              onClick={() => {
                u.unlock();
                setAskUnlock(false);
              }}
            >
              {tr("确认解锁", "Confirm unlock")}
            </button>
            <button type="button" className="rounded-mast-ctl px-2.5 py-1 text-mast-muted hover:text-mast-text" onClick={() => setAskUnlock(false)}>
              {tr("取消", "Cancel")}
            </button>
          </div>
        </div>
      )}

      {/* Per-action confirmation (CONFIRM level while locked, and every DANGEROUS level) */}
      {pending && (
        <div className="mt-2 space-y-1 rounded-mast-ctl border border-mast-border-strong bg-mast-panel-2 px-3 py-2 text-xs text-mast-text">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-semibold">{tr(`执行 ${pending.action.label}？`, `Run ${pending.action.label}?`)}</span>
            <Badge tone={SAFETY_TONE[pending.action.safety] ?? "default"}>{pending.action.safety}</Badge>
            <span className="font-mono text-mast-faint">{pending.action.skill}</span>
          </div>
          <div className="font-mono text-[11px] text-mast-muted">{paramsText(pending.params) || tr("（无参数）", "(no parameters)")}</div>
          <div className="flex gap-2 pt-1">
            <button
              type="button"
              className="rounded-mast-ctl border border-mast-accent bg-mast-accent px-2.5 py-1 font-semibold text-mast-accent-ink"
              onClick={() => {
                const p = pending;
                setPending(null);
                void execute(p.action, p.axis, p.params);
              }}
            >
              {tr("确认执行", "Confirm")}
            </button>
            <button type="button" className="rounded-mast-ctl px-2.5 py-1 text-mast-muted hover:text-mast-text" onClick={() => setPending(null)}>
              {tr("取消", "Cancel")}
            </button>
          </div>
        </div>
      )}

      {/* Axes: readout + per-axis buttons */}
      {plan.axes.length > 0 && (
        <div className="mt-3 divide-y divide-mast-border border-t border-mast-border">
          {plan.axes.map(({ axis: ax, controls: cs }) => {
            const v = readout.byAxis[ax.name];
            const mv = readout.moving[ax.name] ?? ax.moving;
            const homed = readout.homed[ax.name] ?? ax.homed;
            const ex = readout.extra[ax.name];
            const bits = ex ? axisExtraBits(ex, ax.unit) : [];
            return (
              <div key={ax.name} className="space-y-1 py-2">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span className="w-16 shrink-0 font-mono text-xs font-medium text-mast-text">{ax.name}</span>
                  <span className="font-mono text-sm tabular-nums text-mast-text">
                    {v === undefined || v === null ? "—" : fmtValue(v)}
                    {ax.unit && <span className="ml-1 text-[11px] text-mast-faint">{ax.unit}</span>}
                  </span>
                  {mv === true && <span className="text-[11px] text-mast-info">{tr("在动", "moving")}</span>}
                  {homed === false && <span className="text-[11px] text-mast-warn">{tr("未回零", "not homed")}</span>}
                  {(ax.min !== null || ax.max !== null) && (
                    <span className="text-[10.5px] text-mast-faint">
                      {tr("行程", "Travel")} {ax.min ?? "—"} … {ax.max ?? "—"}
                    </span>
                  )}
                </div>
                {bits.length > 0 && (
                  <div className="flex flex-wrap gap-x-2 gap-y-0.5 pl-16 text-[10.5px]">
                    {bits.map((b) => (
                      <span key={b.text} className={b.tone === "warn" ? "text-mast-warn" : "text-mast-faint"}>
                        {b.text}
                      </span>
                    ))}
                  </div>
                )}
                {cs.length > 0 && <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 pl-16">{cs.map(controls)}</div>}
                <div className="pl-16">{cs.map((c) => outcomeLine(c.action, c.axis))}</div>
              </div>
            );
          })}
        </div>
      )}
      {readout.error && <div className="mt-1 text-[11px] text-mast-warn">{tr("读数失败：", "Readout failed: ")}{readout.error}</div>}
      {Object.keys(readout.rest).length > 0 && (
        <div className="mt-1 font-mono text-[11px] text-mast-faint">
          {Object.entries(readout.rest)
            .map(([k, v]) => {
              const l = restLabel(k);
              return `${l.name} ${v === null ? "—" : fmtValue(v)}${l.unit ? ` ${l.unit}` : ""}`;
            })
            .join(" · ")}
        </div>
      )}
      {readout.source && (
        <div className="mt-1 text-[10.5px] text-mast-faint">
          {tr("读数：", "Readout: ")}
          {readout.source === "live"
            ? tr("实时流", "live stream")
            : readout.source === "poll"
              ? tr(`每 ${d.readout?.kind === "poll" ? fmtSeconds(d.readout.intervalS) : "?"} s 批量读取（只读，不入账）`, `batch read every ${d.readout?.kind === "poll" ? fmtSeconds(d.readout.intervalS) : "?"} s (read-only, not recorded)`)
              : readout.source === "cache"
                ? tr("枢纽缓存（实时流还没到）", "hub cache (live stream not here yet)")
                : tr("描述里的最近值", "latest value from the description")}
        </div>
      )}
      {d.kind === "nanonis_motor_group" && (
        <div className="mt-1 text-[10.5px] text-mast-faint">{tr("无位置反馈，只有命令过的步数（Motor_PosGet 在这类电机组上不可用）。", "No position feedback, only the number of steps commanded (Motor_PosGet is unavailable on this kind of motor group).")}</div>
      )}

      {/* Actions that are not per axis */}
      {plan.deviceControls.length > 0 && (
        <div className="mt-3 space-y-1.5 border-t border-mast-border pt-2">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">{plan.deviceControls.map(controls)}</div>
          {plan.deviceControls.map((c) => outcomeLine(c.action, null))}
        </div>
      )}
      {plan.stops.map((s) => outcomeLine(s, null))}

      <HintList hints={plan.hints} />
      {plan.readOnly && <div className="mt-2 text-[11px] text-mast-faint">{tr("只显示状态：后端没给这台装置任何按钮。", "Status only: the backend gave this device no buttons.")}</div>}
      {plan.showUnlock && !u.unlocked && d.available && (
        <div className="mt-2 text-[10.5px] text-mast-faint">{tr("上着锁：每次点动先在卡片里确认；解锁后可以直接点、按住连续走。", "Locked: every jog is confirmed in the card first; after unlocking you can click directly or hold to keep stepping.")}</div>
      )}
    </Card>
    </div>
  );
}
