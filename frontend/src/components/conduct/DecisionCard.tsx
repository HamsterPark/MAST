import { useState } from "react";
import { Badge, Card } from "@/components/ui";
import { Button, Field, Modal, TextField } from "@/components/controls";
import type { components } from "@/api/schema";
import { tr } from "@/lib/i18n";

type PendingDecision = components["schemas"]["PendingDecisionView"];

// Decision card: after a gate stops a conduct, the operator can see which decision
// it stopped on and release that one decision.
//
// Without this card a gated conduct had only two exits (abort, or take over for good),
// so a gate that stopped overnight could only be resolved by abandoning the conduct.
// A stop with no release is a deadlock.
//
// Display rules (shared with WaitCard, but answering a different question):
//
// 1. This is not a wait card. `waiting_operator` has two sources: a `wait` step
//    (ack + physical conditions, pollable) and an escalated verdict (a human judgement,
//    no pollable condition). The former is WaitCard, the latter is this card. They never
//    appear together: the backend's `pending_decision` is null while `active_wait` is set.
//
// 2. The gate's verdict is shown verbatim. The operator overrides that statement, so the
//    screen must show what is being overridden; otherwise an audit cannot tell what a
//    release overrode.
//
// 3. It releases this one decision, not the gate. The dialog says so explicitly, because
//    this is the button most easily mistaken for "skip this gate"; the next time the
//    conduct reaches the same gate, it is judged again.

const WHICH_LABEL: Record<string, string> = {
  entry: tr("阶段入口闸", "Stage entry gate"),
  step: tr("步后闸", "Post-step gate"),
  exit: tr("阶段出口闸", "Stage exit gate"),
};

export function DecisionCard({
  pending,
  onOverride,
  busy,
}: {
  pending: PendingDecision;
  onOverride: (reason: string) => void;
  busy: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const where = WHICH_LABEL[pending.which] ?? pending.which;

  return (
    <Card className="border-mast-warn-border bg-mast-warn-bg/30">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-[17px] font-semibold text-mast-text">
            {tr("等你看一眼：闸门停在这里了", "Needs your review: a gate has stopped here")}
          </h3>
          <p className="mt-1 text-sm text-mast-muted">
            {tr(`${pending.stage_id} 的 ${where}`, `${where} of ${pending.stage_id}`)}
            <span className="ml-1 font-mono text-xs">{pending.gate_id}</span>
          </p>
        </div>
        <Badge tone="WARN">{tr("等人判断", "Awaiting decision")}</Badge>
      </div>

      {/* What the gate decided and why. This is the statement being overridden, so it is shown verbatim. */}
      <div className="mt-4 rounded-mast-ctl border border-mast-border bg-mast-panel px-3 py-2">
        <p className="text-xs text-mast-faint">{tr("闸门的裁决", "Gate verdict")}</p>
        <p className="mt-0.5 font-mono text-sm text-mast-text">{pending.verdict}</p>
        <p className="mt-1 whitespace-pre-wrap text-sm text-mast-text">{pending.reason}</p>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button variant="primary" onClick={() => setOpen(true)} disabled={busy}>
          {tr("我看过了，继续", "I have reviewed it, continue")}
        </Button>
        <span className="text-xs text-mast-faint">
          {tr(
            `判定编号 #${pending.decision_id}　·　继续 = 只放行这一次；下一次走到这道闸，还会重新判`,
            `Decision #${pending.decision_id}  ·  Continue releases this one decision only; the gate is judged again next time`,
          )}
        </span>
      </div>

      <Modal open={open} onClose={() => setOpen(false)} title={tr("放行这一次闸门判定", "Release this gate decision")}>
        <p className="text-sm text-mast-muted">
          {tr(
            `这**不是**让这道闸失效：你放行的是**第 #${pending.decision_id} 次判定**这一次。下一次走到 `,
            `This does **not** disable the gate: you are releasing **decision #${pending.decision_id}** only. The next time the conduct reaches `,
          )}
          <span className="font-mono">{pending.gate_id}</span>
          {tr("，它照样重新判。", ", it is judged again.")}
        </p>
        <p className="mt-2 text-sm text-mast-muted">
          {tr(
            "闸门那条判定记录**原样留着**（它说的是闸门当时判了什么），你的决定另起一条。两者分开记，否则事后对账会看到一道从不判 fail 的闸——而那正是最该复核的那种记录。",
            "The gate's own verdict record is **kept as is** (it says what the gate decided), and your decision is recorded separately. They are kept apart; otherwise an audit would see a gate that never fails, which is exactly the record that most needs review.",
          )}
        </p>
        <div className="mt-4">
          <Field
            label={tr("理由（必填）", "Reason (required)")}
            hint={tr("写给三天后的自己看：当时凭什么认为可以继续。", "Write it for yourself three days from now: why was it fine to continue?")}
          >
            <TextField
              value={reason}
              onChange={setReason}
              placeholder={tr("例：n_keep=0 是因为这一批谱存到了另一个目录，人工核对过 12 条都可用", "e.g. n_keep=0 because this batch of spectra was saved to another directory; checked all 12 by hand")}
            />
          </Field>
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setOpen(false)}>
            {tr("取消", "Cancel")}
          </Button>
          <Button
            variant="primary"
            disabled={!reason.trim() || busy}
            onClick={() => {
              onOverride(reason.trim());
              setOpen(false);
              setReason("");
            }}
          >
            {tr("放行这一次并留痕", "Release this decision and log it")}
          </Button>
        </div>
      </Modal>
    </Card>
  );
}
