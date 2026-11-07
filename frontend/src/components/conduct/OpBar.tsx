import { useState } from "react";
import { Button, Field, Modal, TextField, Toggle } from "@/components/controls";
import { opEnabled, opLabel, type ConductOp } from "@/lib/conduct";
import { tr } from "@/lib/i18n";

// The five operation buttons: pause / resume / abort / takeover / attended.
//
// 1. A disabled button must say why. For an intent that makes no sense in the current
//    state the backend explicitly rejects it and records op_rejected (no silent no-op),
//    but the user would only see "I pressed it and nothing happened". The predicate
//    therefore also exists on this side (`lib/conduct.opEnabled`, kept in parity with
//    director.OP_VALID_STATUSES by a test) and the reason is put in the title.
//
// 2. The UI never freezes. The worst outcome of a button is "no effect plus a message":
//    a failed enqueue only shows a toast, with no lock and no spinner waiting for a
//    reply that never comes. Abort in particular must be possible from any non-terminal
//    state.
//
// Abort requires a reason: the backend schema makes `reason` mandatory, and an abort
// without one cannot be explained afterwards. A small dialog collects it rather than
// sending an empty string to be rejected with 422.

export function OpBar({
  status,
  attended,
  busy,
  onOp,
  onAbort,
  onAttended,
}: {
  status: string;
  attended: boolean;
  busy: boolean;
  // Only the ops that follow the generic "one op name = one route" shape. The others
  // have their own payloads and must not be mixed in: abort needs a reason, ack needs
  // wait_id, waive needs wait_id + reason, set_attended needs a boolean, and
  // override_decision needs decision_id + reason (its card is DecisionCard). Passing them
  // through this bar would leave the caller guessing the arguments.
  onOp: (
    op: Exclude<
      ConductOp,
      "abort" | "ack" | "waive_condition" | "set_attended" | "override_decision"
    >,
  ) => void;
  onAbort: (reason: string) => void;
  onAttended: (next: boolean) => void;
}) {
  const [abortOpen, setAbortOpen] = useState(false);
  const [reason, setReason] = useState("");

  const btn = (op: "pause" | "resume" | "takeover") => {
    const { enabled, why } = opEnabled(op, status);
    return (
      <span title={why || undefined}>
        <Button variant="default" disabled={!enabled || busy} onClick={() => onOp(op)}>
          {opLabel(op)}
        </Button>
      </span>
    );
  };

  const abortGate = opEnabled("abort", status);
  const attendedGate = opEnabled("set_attended", status);

  return (
    <div className="flex flex-wrap items-center gap-2">
      {btn("pause")}
      {btn("resume")}
      {btn("takeover")}
      <span title={abortGate.why || undefined}>
        <Button
          variant="danger"
          disabled={!abortGate.enabled || busy}
          onClick={() => setAbortOpen(true)}
        >
          {tr("中止", "Abort")}
        </Button>
      </span>

      <span className="ml-2 border-l border-mast-border pl-3" title={attendedGate.why || undefined}>
        <Toggle
          checked={attended}
          onChange={(v) => attendedGate.enabled && !busy && onAttended(v)}
          label={attended ? tr("有人值守", "Attended") : tr("无人值守", "Unattended")}
        />
      </span>
      <span className="text-xs text-mast-faint">
        {attended
          ? tr("判不了的时候会问你", "Asks you when it cannot decide")
          : tr(
              "判不了的时候直接走保守分支（不问人——无人窗口里问了也必然超时）",
              "Takes the conservative branch when it cannot decide (does not ask; asking would time out anyway while unattended)",
            )}
      </span>

      <Modal open={abortOpen} onClose={() => setAbortOpen(false)} title={tr("中止这份 conduct", "Abort this conduct")}>
        <p className="text-sm text-mast-muted">
          {tr(
            "中止会**立刻**给当前这一步发停止信号（不等下一个 tick——指挥线程可能正卡在一次长动作里），然后走确认式退针。等待态里针已经退了，直接结束。",
            "Aborting sends a stop signal to the current step **immediately** (without waiting for the next tick, since the conductor thread may be inside a long action), then withdraws the tip with confirmation. If the conduct is waiting, the tip is already withdrawn and it simply ends.",
          )}
        </p>
        <p className="mt-2 text-sm text-mast-muted">
          {tr("这是终态：**要接着做请新建一份。**", "This is a terminal state: **to continue, create a new conduct.**")}
        </p>
        <div className="mt-4">
          <Field
            label={tr("理由（必填）", "Reason (required)")}
            hint={tr("第二天早上唯一有用的东西就是这句话。", "This line is what you will want to read the next morning.")}
          >
            <TextField
              value={reason}
              onChange={setReason}
              placeholder={tr("例：针尖在 S2 第二个偏压上明显变钝，先停下来手工修", "e.g. tip clearly blunted at the second bias of S2; stopping to recondition by hand")}
            />
          </Field>
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setAbortOpen(false)}>
            {tr("取消", "Cancel")}
          </Button>
          <Button
            variant="danger"
            disabled={!reason.trim() || busy}
            onClick={() => {
              onAbort(reason.trim());
              setAbortOpen(false);
              setReason("");
            }}
          >
            {tr("确认中止", "Confirm abort")}
          </Button>
        </div>
      </Modal>
    </div>
  );
}
