import { useState } from "react";
import { Button, OptionGroup } from "@/components/controls";
import { Badge } from "@/components/ui";
import { tr } from "@/lib/i18n";
import { normalizeAsk, type AskQuestion, type Pending } from "./runTaskStore";

// ════════════════════════════════════════════════════════════════════════════
// ONE card for every paused interrupt, wherever it is shown.
//
// It is shared by the group-chat inline card (RunTaskPanel), the per-agent modal
// (InterruptsModal) and the private-chat modal (HitlModal), so a new interrupt
// kind only needs handling here.
//
// Buttons are derived FROM what the interrupt says it accepts, not from a fixed
// verb list filtered by it. A fixed list such as
//
//     const canApprove = decisions.includes("approve")   // …etc
//
// breaks for a composite `workflow_human` node, which advertises its ROUTE NAMES
// in `allowed_decisions` (["继续", "换样品"]): every flag is false and the card
// renders no buttons, leaving the run waiting until the 900 s timeout. `ask_user`
// (allowed_decisions: ["answer"]) would hit the same hole.
// ════════════════════════════════════════════════════════════════════════════

const VERB_LABEL: Record<string, string> = {
  approve: tr("批准", "Approve"),
  reject: tr("拒绝", "Reject"),
  edit: tr("编辑", "Edit"),
};

export type ResolveArgs = {
  decision: string;
  editedArgs?: Record<string, unknown> | null;
  selected?: string[] | null;
  customText?: string | null;
  comment?: string | null;
};

/** A polled `GET /interrupts` row → the card's shape.
 *
 *  The SSE frame and the REST row describe the same interrupt with different
 *  key names (`interrupt_id`/`agent`/`interrupt_kind` vs
 *  `event_id`/`agent_id`/`kind`). Converting at the edge keeps one card
 *  instead of one per transport. */
export function fromPollRow(
  row: {
    event_id: string;
    kind?: string | null;
    agent_id?: string | null;
    skill?: string | null;
    params?: Record<string, unknown> | null;
    rationale?: string | null;
    allowed_decisions?: string[] | null;
    routes?: string[] | null;
    ask?: Record<string, unknown> | null;
  },
  fallbackAgent: string,
): Pending {
  return {
    interrupt_id: row.event_id,
    agent: row.agent_id || fallbackAgent,
    skill: row.skill ?? null,
    params: row.params ?? null,
    rationale: row.rationale ?? null,
    allowed_decisions: row.allowed_decisions ?? [],
    interrupt_kind: row.kind ?? null,
    routes: row.routes ?? [],
    ask: normalizeAsk(row.ask),
  };
}

/** The tone the kind badge carries. A question is INFO, not WARN: nothing is
 *  about to run and nothing is wrong; someone is waiting on the operator's
 *  judgement, and styling that as a hazard desensitizes operators to real ones. */
function kindTone(kind?: string | null): string {
  if (kind === "ask_user") return "INFO";
  if (kind === "buffer_hitl") return "DANGEROUS";
  return "WARN";
}

function kindLabel(kind?: string | null): string {
  if (kind === "ask_user") return tr("提问", "Question");
  if (kind === "workflow_human") return tr("工作流选择", "Workflow choice");
  if (kind === "buffer_hitl") return tr("关键事件", "Critical event");
  if (kind === "dangerous") return tr("危险操作", "Dangerous operation");
  return kind || "interrupt";
}

export function InterruptCard({
  it,
  busy,
  onResolve,
  agentLabel,
  compact,
}: {
  it: Pending;
  busy: boolean;
  onResolve: (args: ResolveArgs) => void;
  /** Renders the owning agent, when the surface shows more than one. */
  agentLabel?: (id: string) => string;
  compact?: boolean;
}) {
  const decisions = it.allowed_decisions ?? [];
  const isAsk = it.interrupt_kind === "ask_user";
  const ask: AskQuestion | null = isAsk ? it.ask ?? null : null;

  // Route-style verdicts: anything advertised that is not one of the three
  // approval verbs and not the ask_user marker. `routes` is carried separately
  // by the polling endpoint, so take either source.
  const routeVerbs = (it.routes?.length ? it.routes : decisions).filter(
    (d) => !["approve", "reject", "edit", "answer"].includes(d),
  );
  const showApprovalVerbs = decisions.some((d) => ["approve", "reject", "edit"].includes(d))
    // An interrupt that advertises nothing at all is a legacy/degraded row —
    // approve/reject is the only thing that could ever have applied to it.
    || (decisions.length === 0 && !isAsk && routeVerbs.length === 0);
  const canApprove = showApprovalVerbs && (decisions.length === 0 || decisions.includes("approve"));
  const canReject = showApprovalVerbs && (decisions.length === 0 || decisions.includes("reject"));
  const canEdit = decisions.includes("edit");

  const [answer, setAnswer] = useState<{ selected: string[]; customText: string }>({
    selected: [],
    customText: "",
  });
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const [editErr, setEditErr] = useState("");

  const answerEmpty = answer.selected.length === 0 && !answer.customText.trim();
  const done = !!it.resolved;

  function startEdit() {
    setEditing(true);
    setEditErr("");
    setEditText(JSON.stringify(it.params ?? {}, null, 2));
  }

  function submitEdit() {
    let parsed: Record<string, unknown> | null = null;
    if (editText.trim()) {
      try {
        parsed = JSON.parse(editText) as Record<string, unknown>;
      } catch {
        // Locally recoverable — say so next to the box the operator is typing
        // in and keep their text, rather than bouncing them out of the edit.
        setEditErr(tr("不是合法 JSON，未提交。", "Not valid JSON; not submitted."));
        return;
      }
    }
    setEditErr("");
    onResolve({ decision: "edit", editedArgs: parsed });
  }

  return (
    <div
      className={
        "space-y-2 rounded-lg border px-4 py-3 " +
        (done ? "border-mast-border opacity-60" : "border-mast-warn-border bg-mast-warn-bg/40")
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={kindTone(it.interrupt_kind)}>{kindLabel(it.interrupt_kind)}</Badge>
        {ask?.header && <span className="text-xs font-semibold text-mast-text">{ask.header}</span>}
        {!isAsk && it.skill && <span className="font-mono text-xs">{it.skill}</span>}
        {agentLabel && (
          <span className="text-xs text-mast-muted">@ {agentLabel(it.agent)}</span>
        )}
        {done && <Badge tone="INFO">{tr("已处理", "Handled")}</Badge>}
        {!compact && (
          <span className="ml-auto font-mono text-xs text-mast-muted">{it.interrupt_id}</span>
        )}
      </div>

      {/* The question / rationale. For ask_user the question IS the body; for an
          approval it is why the agent wants to do the thing. */}
      {(ask?.question || it.rationale) && (
        <p className="whitespace-pre-wrap text-sm text-mast-text">
          {ask?.question || it.rationale}
        </p>
      )}

      {/* Raw params are useful for an approval (what exactly will run) and just
          noise for a question, whose options are rendered properly below. */}
      {!isAsk && it.params && Object.keys(it.params).length > 0 && (
        <pre className="max-h-40 overflow-auto rounded bg-mast-bg/60 p-2 font-mono text-xs text-mast-muted">
          {JSON.stringify(it.params, null, 2)}
        </pre>
      )}

      {!done && isAsk && (
        <>
          {ask ? (
            <OptionGroup
              options={ask.options}
              multi={ask.multi_select}
              allowCustom={ask.allow_custom}
              value={answer}
              onChange={setAnswer}
              disabled={busy}
            />
          ) : (
            // The structured payload did not survive (old row / odd client).
            // A text box still lets the operator answer, which beats showing a
            // question with no way to reply.
            <textarea
              value={answer.customText}
              disabled={busy}
              onChange={(e) => setAnswer({ selected: [], customText: e.target.value })}
              rows={3}
              placeholder={tr("写下你的回答…", "Write your answer…")}
              className="w-full resize-y rounded-md border border-mast-border bg-mast-bg px-3 py-2 text-sm text-mast-text outline-none focus:border-mast-accent"
            />
          )}
          {ask?.multi_select && (
            <p className="text-xs text-mast-muted">{tr("可多选。", "Multiple selection allowed.")}</p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="primary"
              disabled={busy || answerEmpty}
              onClick={() =>
                onResolve({
                  decision: "answer",
                  selected: answer.selected,
                  customText: answer.customText.trim(),
                  comment: note.trim() || null,
                })
              }
            >
              {busy ? tr("提交中…", "Submitting…") : tr("提交回答", "Submit answer")}
            </Button>
            {answerEmpty && (
              <span className="text-xs text-mast-muted">{tr("请先选择一项或写下你的回答。", "Select an option or write your answer first.")}</span>
            )}
          </div>
        </>
      )}

      {/* workflow_human: each route is a real button (see the header comment). */}
      {!done && !isAsk && routeVerbs.length > 0 && (
        <>
          <input
            value={note}
            disabled={busy}
            onChange={(e) => setNote(e.target.value)}
            placeholder={tr("给智能体的说明（可选）", "Note to the agent (optional)")}
            className="w-full rounded-md border border-mast-border bg-mast-bg px-3 py-1.5 text-sm text-mast-text outline-none focus:border-mast-accent"
          />
          <div className="flex flex-wrap gap-2">
            {routeVerbs.map((r) => (
              <Button
                key={r}
                variant="default"
                disabled={busy}
                onClick={() => onResolve({ decision: r, comment: note.trim() || null })}
              >
                {VERB_LABEL[r] ?? r}
              </Button>
            ))}
          </div>
        </>
      )}

      {!done && editing && (
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-mast-muted">{tr("编辑工具参数 (JSON)", "Edit tool arguments (JSON)")}</span>
          <textarea
            value={editText}
            onChange={(e) => setEditText(e.target.value)}
            rows={6}
            className="w-full resize-y rounded-md border border-mast-border bg-mast-bg px-3 py-2 font-mono text-xs text-mast-text outline-none focus:border-mast-accent"
          />
          <span className="text-xs text-mast-muted/80">
            {tr("提交后合并覆盖原参数，由核心 SafetyGate 重新校验。", "Merged over the original arguments on submit and re-validated by the core SafetyGate.")}
          </span>
          {editErr && <span className="text-xs text-mast-danger">{editErr}</span>}
        </label>
      )}

      {!done && showApprovalVerbs && (
        <div className="flex flex-wrap gap-2">
          {!editing && (
            <>
              {canApprove && (
                <Button variant="primary" disabled={busy} onClick={() => onResolve({ decision: "approve" })}>
                  {busy ? tr("处理中…", "Processing…") : tr("批准", "Approve")}
                </Button>
              )}
              {canReject && (
                <Button variant="danger" disabled={busy} onClick={() => onResolve({ decision: "reject" })}>
                  {tr("拒绝", "Reject")}
                </Button>
              )}
              {canEdit && (
                <Button variant="default" disabled={busy} onClick={startEdit}>
                  {tr("编辑", "Edit")}
                </Button>
              )}
            </>
          )}
          {editing && (
            <>
              <Button variant="primary" disabled={busy} onClick={submitEdit}>
                {busy ? tr("处理中…", "Processing…") : tr("编辑并批准", "Edit and approve")}
              </Button>
              <Button variant="ghost" disabled={busy} onClick={() => setEditing(false)}>
                {tr("取消", "Cancel")}
              </Button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
