// What one device card draws (ManualDeviceCard only draws it). A pure function so that unit tests
// without a DOM can pin "rendered entirely from the description": which stops go in the card
// header, which buttons are per axis, signed actions drawn as - / +, which can be held, which need
// confirmation first, and unavailable cards still drawn with the reason stated.

import { tr } from "../i18n.ts";
import {
  MANUAL_KIND_LABEL,
  TIP_REQUIREMENT_LABEL,
  actionGate,
  canHold,
  inputParams,
  needsUnlock,
  type Gate,
  type ManualAction,
  type ManualAxis,
  type ManualDevice,
  type ManualParamSpec,
} from "./protocol.ts";

export interface PlannedControl {
  key: string;
  action: ManualAction;
  axis: string | null;
  /** signed -> "-" input "+"; button -> input + one button. */
  kind: "signed" | "button";
  inputs: ManualParamSpec[];
  holdable: boolean;
  gate: Gate;
  disabled: boolean;
}

export interface CardPlan {
  title: string;
  kindLabel: string;
  badges: Array<{ tone: string; text: string }>;
  /** Stop actions in the card header: always enabled, no dialog, ignore the lock. */
  stops: ManualAction[];
  axes: Array<{ axis: ManualAxis; controls: PlannedControl[] }>;
  deviceControls: PlannedControl[];
  showUnlock: boolean;
  unavailableText: string | null;
  /** No buttons at all (status only). */
  readOnly: boolean;
  /** What each button actually does (the hint from the description): listed at the bottom of the card (stop hints always shown); not repeated when the button label already says it. */
  hints: Array<{ label: string; hint: string; stop: boolean }>;
}

export function actionHints(actions: ManualAction[]): CardPlan["hints"] {
  const out: CardPlan["hints"] = [];
  const seen = new Set<string>();
  for (const a of actions) {
    const h = a.hint?.trim();
    if (!h || a.label.includes(h)) continue;
    const k = `${a.label}|${h}`;
    if (seen.has(k)) continue;
    seen.add(k);
    out.push({ label: a.label, hint: h, stop: isStopAction(a) });
  }
  return out;
}

export function isStopAction(a: ManualAction): boolean {
  return a.bypassLock || a.id === "stop" || a.id === "abort";
}

export function controlKey(a: ManualAction, axis: string | null): string {
  return `${a.id}|${axis ?? ""}`;
}

export function cardPlan(d: ManualDevice, unlocked: boolean): CardPlan {
  const stops = d.actions.filter(isStopAction);
  const motion = d.actions.filter((a) => !isStopAction(a));
  const control = (a: ManualAction, axis: string | null): PlannedControl => ({
    key: controlKey(a, axis),
    action: a,
    axis,
    kind: a.direction === "signed" ? "signed" : "button",
    inputs: inputParams(a),
    holdable: d.available && canHold(a, unlocked),
    gate: actionGate(a, unlocked),
    disabled: !d.available,
  });
  const badges: CardPlan["badges"] = [];
  badges.push(d.available ? { tone: "AUTO", text: tr("可用", "Available") } : { tone: "WARN", text: tr("不可用", "Unavailable") });
  if (d.occupiedBy) badges.push({ tone: "WARN", text: tr(`被 ${d.occupiedBy} 占用`, `Held by ${d.occupiedBy}`) });
  if (d.tipRequirement) badges.push({ tone: "INFO", text: tr(`针尖：${TIP_REQUIREMENT_LABEL[d.tipRequirement] ?? d.tipRequirement}`, `Tip: ${TIP_REQUIREMENT_LABEL[d.tipRequirement] ?? d.tipRequirement}`) });
  return {
    title: d.label,
    kindLabel: MANUAL_KIND_LABEL[d.kind] ?? d.kind,
    badges,
    stops,
    axes: d.axes.map((axis) => ({ axis, controls: motion.filter((a) => a.perAxis).map((a) => control(a, axis.name)) })),
    deviceControls: motion.filter((a) => !a.perAxis).map((a) => control(a, null)),
    showUnlock: needsUnlock(d),
    unavailableText: d.available ? null : tr(`不可用：${d.unavailableReason || "后端没给原因"}`, `Unavailable: ${d.unavailableReason || "no reason given by the backend"}`),
    readOnly: d.actions.length === 0,
    hints: actionHints(d.actions),
  };
}
