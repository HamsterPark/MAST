import { useState } from "react";
import { Card } from "@/components/ui";
import { Button, Field, TextField } from "@/components/controls";
import { approveBlockReason } from "@/lib/conduct";
import { tr } from "@/lib/i18n";

// Approving a draft. This card is the only entry point.
//
// The approve route accepts UI-originated requests only and is never registered
// as an agent tool: approving a multi-day procedure that moves the tip is a human
// decision. Because of that, the panel must offer the button itself; otherwise a
// draft conduct would sit in "draft" with no visible way to advance it.
//
// When approval is blocked, the card has to say why. The backend checks
// `approvable = ok AND complete`: "no errors found" does not mean "every check ran"
// (e.g. an unreadable registry skips rule 3 while `ok` stays true). The 409 body
// therefore carries two separate lists, findings (what was found) and checks_skipped
// (what was not checked), and they are shown in two blocks. Merging them into one
// "validation failed" line would erase that distinction.

export interface ApproveOutcome {
  ok?: boolean;
  validation_ok?: boolean;
  validation_complete?: boolean;
  findings?: string[];
  checks_skipped?: string[];
  errors?: string[];
  spec_doc_path?: string;
}

export function ApproveCard({
  onApprove,
  busy,
  outcome,
}: {
  onApprove: (by: string) => void;
  busy: boolean;
  outcome: ApproveOutcome | null;
}) {
  const [by, setBy] = useState("");
  const blocked = outcome && !outcome.ok;

  return (
    <Card className="border-mast-accent">
      <h3 className="text-[17px] font-semibold text-mast-text">{tr("这份 conduct 还是草稿", "This conduct is still a draft")}</h3>
      <p className="mt-1 text-sm text-mast-muted">
        {tr(
          "批准会**冻结参数**、渲染一份人读快照到实验文件夹，然后指挥线程才会接手。批准之后参数不能再改——要改就新建一份。",
          "Approving **freezes the parameters**, renders a human-readable snapshot into the experiment folder, and only then hands the conduct to the conductor thread. Parameters cannot be changed after approval; create a new conduct instead.",
        )}
      </p>
      <div className="mt-3 max-w-sm">
        <Field label={tr("批准人（必填）", "Approved by (required)")}
          hint={tr("进审计流。这是「谁让它开始跑的」那一栏。", "Goes into the audit trail: the \"who started this run\" entry.")}>
          <TextField value={by} onChange={setBy} placeholder={tr("你的名字", "Your name")} />
        </Field>
      </div>
      <div className="mt-3">
        <Button variant="primary" disabled={!by.trim() || busy} onClick={() => onApprove(by.trim())}>
          {tr("批准并交给指挥线程", "Approve and hand to conductor")}
        </Button>
      </div>

      {blocked && (
        <div className="mt-4 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2.5 text-sm text-mast-warn">
          <p className="font-semibold">{tr("批不下去", "Cannot approve")}</p>
          <p className="mt-0.5 text-xs">{approveBlockReason(outcome)}</p>

          {/* Findings and skipped checks are shown separately so "not checked"
              stays distinguishable from "check passed". */}
          {outcome.findings?.length ? (
            <>
              <p className="mt-2 text-xs text-mast-faint">{tr("发现：", "Findings:")}</p>
              <ul className="ml-4 list-disc text-xs">
                {outcome.findings.map((f) => (
                  <li key={f}>{f}</li>
                ))}
              </ul>
            </>
          ) : null}

          {outcome.checks_skipped?.length ? (
            <>
              <p className="mt-2 text-xs text-mast-faint">{tr("这些检查**没能跑**：", "These checks **did not run**:")}</p>
              <ul className="ml-4 list-disc text-xs">
                {outcome.checks_skipped.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </>
          ) : null}

          {outcome.errors?.length ? (
            <ul className="ml-4 mt-2 list-disc text-xs">
              {outcome.errors.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          ) : null}
        </div>
      )}
    </Card>
  );
}
