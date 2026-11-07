import { useState } from "react";
import { Badge, Card } from "@/components/ui";
import { Button, Field, Modal, TextField } from "@/components/controls";
import type { components } from "@/api/schema";
import { fmtClock, fmtDuration, waitGateRows } from "@/lib/conduct";
import { tr } from "@/lib/i18n";

type ActiveWait = components["schemas"]["ActiveWaitView"];

// Wait card: the one block of the panel that says "it is your turn now".
//
// It answers not "is it waiting?" but "which one is still missing?". The double gate is
// the operator's ack AND a physical condition; the two pieces of evidence answer
// different questions and do not substitute for each other (an operator confirming does
// not mean the sample reached temperature, and reaching temperature does not mean the
// sample was swapped). Each gate therefore gets its own row and its own message instead
// of a "1 item missing" count, which cannot say which one is missing.
//
// Display rules:
// 1. Stale is not "not reached". An expired temperature reading means "cannot judge";
//    waiting on it is wrong. It uses "unreadable" wording and its own color, and names the
//    next action (is the temperature logger still running?).
// 2. The waive marker stays visible. The gate was released by an operator through a logged
//    command; the report carries that, and the screen must keep showing it.
// 3. Pressing ack does not mean the conduct proceeds. It only enqueues the intent, which
//    takes effect on the next tick, and the other gate may still be missing. The message
//    that comes back says what is still missing, not "released".

export function WaitCard({
  wait,
  onAck,
  onWaive,
  busy,
}: {
  wait: ActiveWait;
  onAck: (note: string) => void;
  onWaive: (reason: string) => void;
  busy: boolean;
}) {
  const [waiveOpen, setWaiveOpen] = useState(false);
  const [reason, setReason] = useState("");
  const rows = waitGateRows(wait);
  const lacking = rows.filter((r) => !r.ok);
  const cond = wait.condition;
  const ackDone = wait.ack?.at != null;

  return (
    <Card className="border-mast-warn-border bg-mast-warn-bg/30">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-[17px] font-semibold text-mast-text">
            {lacking.length
              ? tr(
                  `等你：还缺 ${lacking.map((r) => r.label).join(" + ")}`,
                  `Waiting for you: still missing ${lacking.map((r) => r.label).join(" + ")}`,
                )
              : tr("两个闸都齐了，正在放行", "Both gates satisfied, releasing")}
          </h3>
          <p className="mt-1 whitespace-pre-wrap text-sm text-mast-text">{wait.message}</p>
        </div>
        <Badge tone="WARN">{tr("等待中", "Waiting")}</Badge>
      </div>

      <ul className="mt-4 space-y-2">
        {rows.map((r) => (
          <li
            key={r.label}
            className="flex items-start gap-2.5 rounded-mast-ctl border border-mast-border bg-mast-panel px-3 py-2"
          >
            <span
              className={
                r.ok
                  ? "mt-0.5 text-mast-auto"
                  : r.unreadable
                    ? "mt-0.5 text-mast-warn"
                    : "mt-0.5 text-mast-muted"
              }
              aria-hidden="true"
            >
              {r.ok ? "✓" : r.unreadable ? "?" : "○"}
            </span>
            <div className="min-w-0">
              <p className="text-sm font-medium text-mast-text">
                {r.label}
                {/* "Unreadable" is its own state, not folded into "not passed": the next action differs. */}
                {r.unreadable && <span className="ml-2 text-xs text-mast-warn">{tr("判不了", "Cannot judge")}</span>}
              </p>
              <p className="mt-0.5 text-xs text-mast-muted">{r.detail}</p>
            </div>
          </li>
        ))}
      </ul>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button variant="primary" onClick={() => onAck("")} disabled={busy || ackDone}>
          {ackDone ? tr("已确认", "Confirmed") : tr("我已确认", "I confirm")}
        </Button>
        {/* waive only appears when there is a condition gate that has not been waived yet:
            an inapplicable button is more confusing than no button. */}
        {cond && !cond.waived && (
          <Button variant="default" onClick={() => setWaiveOpen(true)} disabled={busy}>
            {tr("条件读不到，由我提供证据", "Condition unreadable, I will provide evidence")}
          </Button>
        )}
        <span className="text-xs text-mast-faint">
          {tr("进入于", "Entered at")} {fmtClock(wait.entered_at)}
          {wait.last_notified_at != null &&
            `　·　${tr("上次通知", "Last notified")} ${fmtClock(wait.last_notified_at)}`}
          {wait.request_id && `　·　${tr("心愿单", "Wishlist")} ${wait.request_id}`}
        </span>
      </div>

      {cond?.stale_after_s != null && (
        <p className="mt-2 text-xs text-mast-faint">
          {tr(
            `读数超过 ${fmtDuration(cond.stale_after_s)} 就算读不到（那时这条等待会降级为「要人来看」，而不是继续干等）。`,
            `A reading older than ${fmtDuration(cond.stale_after_s)} counts as unreadable (this wait then degrades to "needs a human" instead of waiting indefinitely).`,
          )}
        </p>
      )}

      <Modal open={waiveOpen} onClose={() => setWaiveOpen(false)} title={tr("由人提供证据放行条件闸", "Release the condition gate with operator-provided evidence")}>
        <p className="text-sm text-mast-muted">
          {tr(
            "这**不是**默默放行：谁放的、为什么放，会进审计流，面板上会**一直**显示这个标记，报告里也带着它。",
            "This is **not** a silent release: who released it and why goes into the audit trail, the panel shows this marker **permanently**, and the report carries it too.",
          )}
        </p>
        <p className="mt-2 text-sm text-mast-muted">
          {tr(
            "它存在的理由是「能停不能解 = 死锁」——传感器读不到时这个闸会永远 stale，人必须有一条显式、留痕的解锁路。",
            "It exists because a stop with no release is a deadlock: when the sensor cannot be read the gate stays stale forever, so the operator needs an explicit, logged way through.",
          )}
        </p>
        <div className="mt-4">
          <Field
            label={tr("理由（必填）", "Reason (required)")}
            hint={tr("写给三天后的自己看：当时凭什么认为条件其实满足了。", "Write it for yourself three days from now: why did you consider the condition met?")}
          >
            <TextField
              value={reason}
              onChange={setReason}
              placeholder={tr("例：温度计 COM13 被 另一个串口程序 占着，手工读数 4.3 K", "e.g. thermometer on COM13 is held by the 另一个串口程序; manual reading 4.3 K")}
            />
          </Field>
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setWaiveOpen(false)}>
            {tr("取消", "Cancel")}
          </Button>
          <Button
            variant="primary"
            disabled={!reason.trim() || busy}
            onClick={() => {
              onWaive(reason.trim());
              setWaiveOpen(false);
              setReason("");
            }}
          >
            {tr("确认放行并留痕", "Confirm release and log it")}
          </Button>
        </div>
      </Modal>
    </Card>
  );
}
