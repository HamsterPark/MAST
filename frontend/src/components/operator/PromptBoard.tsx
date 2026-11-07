import { useMemo, useState } from "react";
import clsx from "clsx";
import { Section, Badge, EmptyNote, ErrorNote } from "@/components/ui";
import {
  useAnswerPrompt,
  useDismissPrompt,
  useIsOpticalStation,
  useOpenPromptFeed,
  useOpticalState,
  usePromptHistory,
} from "@/api/operator";
import { useNow } from "@/hooks/useNow";
import { useOperatorName } from "@/hooks/useOperatorPrefs";
import { tr } from "@/lib/i18n";
import { LASER_AREA_SAFE, laserSafeMeaning } from "@/lib/opticalState";
import {
  answerSummary,
  buildAnswerBody,
  buildDismissBody,
  failureText,
  fmtClock,
  metaLine,
  promptFields,
  rangeHint,
  severityLabel,
  sourceLabel,
  statusLabel,
  type OperatorPrompt,
  type Severity,
  type SlotHint,
} from "@/lib/operatorPrompts";
import { AlertControls } from "./AlertControls";
import { OperatorNameField } from "./OperatorNameField";

// Structured operator prompts on the Operator Requests page: the things an agent asked
// a person to do or decide (change a waveplate, read a power, sign the laser area as
// safe...) with the right control for each kind, plus what was handled recently. The
// same prompts appear in the launcher's desktop popup; whoever answers first counts.
//
// Only an optical station has these: on an STM station (or before the service has said
// what it is) the board is not rendered and nothing is requested for it, so that page
// stays exactly as it was.

const INPUT =
  "rounded border border-mast-border bg-mast-bg px-3 py-1.5 text-sm text-mast-text placeholder:text-mast-muted focus:border-mast-accent focus:outline-none";
const PRIMARY = "rounded bg-mast-accent px-4 py-1.5 text-sm font-semibold text-mast-accent-ink hover:opacity-90 disabled:opacity-50";
const SECONDARY = "rounded border border-mast-border-strong px-4 py-1.5 text-sm text-mast-text hover:bg-mast-panel-2 disabled:opacity-50";

const CARD: Record<Severity, string> = {
  safety: "border-2 border-mast-danger",
  action: "border border-mast-warn-border",
  info: "border border-mast-info-border",
};
const HEAD: Record<Severity, string> = {
  safety: "bg-mast-danger-bg",
  action: "bg-mast-warn-bg",
  info: "bg-mast-info-bg",
};
const BADGE: Record<Severity, string> = { safety: "DANGEROUS", action: "WARN", info: "INFO" };

function PromptCard({
  prompt,
  name,
  slots,
  nowMs,
}: {
  prompt: OperatorPrompt;
  name: string;
  slots: Record<string, SlotHint>;
  nowMs: number;
}) {
  const fields = promptFields(prompt, slots);
  const [choice, setChoice] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [declining, setDeclining] = useState(false);
  const answer = useAnswerPrompt();
  const dismiss = useDismissPrompt();
  const busy = answer.isPending || dismiss.isPending || answer.isSuccess;
  const sev = prompt.severity;
  const laserSlot = prompt.attest_slots.includes(LASER_AREA_SAFE);

  const submit = (picked?: string) => {
    setProblem(null);
    const r = buildAnswerBody(prompt, { choice: picked ?? choice, text, confirmed, note }, name, slots);
    if (!r.ok) {
      setProblem(r.error);
      return;
    }
    answer.mutate({ id: prompt.id, body: r.value }, { onError: (e) => setProblem(failureText(e)) });
  };

  const decline = () => {
    setProblem(null);
    const r = buildDismissBody(name, note);
    setDeclining(false);
    if (!r.ok) {
      setProblem(r.error);
      return;
    }
    dismiss.mutate({ id: prompt.id, body: r.value }, { onError: (e) => setProblem(failureText(e)) });
  };

  const onForm = (e: React.FormEvent) => {
    e.preventDefault();
    submit();
  };

  return (
    <div
      className={clsx("overflow-hidden rounded-mast-card bg-mast-panel shadow-mast", CARD[sev])}
      role={sev === "safety" ? "alert" : undefined}
      data-prompt-id={prompt.id}
    >
      <div className={clsx("flex flex-wrap items-center gap-2 px-4 py-2", HEAD[sev])}>
        <Badge tone={BADGE[sev]}>{severityLabel(sev)}</Badge>
        <b className="text-sm text-mast-text">{prompt.title}</b>
        <span className="ml-auto text-xs text-mast-muted">
          #{prompt.id} · {fmtClock(prompt.created_at)}
        </span>
      </div>

      <div className="space-y-3 p-4">
        {prompt.message && <p className="whitespace-pre-wrap text-sm text-mast-text">{prompt.message}</p>}
        <p className="text-xs text-mast-muted">{metaLine(prompt, nowMs)}</p>
        {prompt.attest_slots.length > 0 && (
          <p className="text-xs text-mast-muted">
            {tr("你的答复会记录到光学状态：", "Your answer is recorded in the optical state: ")}
            <code className="font-mono text-mast-text">{prompt.attest_slots.join(", ")}</code>
          </p>
        )}
        {laserSlot && (
          <div className="rounded-mast-ctl border border-mast-danger-border bg-mast-danger-bg px-3 py-2 text-xs text-mast-text">
            <p className="font-semibold text-mast-danger">{tr("签署的含义", "What signing means")}</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-4">
              {laserSafeMeaning().map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </div>
        )}

        {/* the control for this kind of prompt */}
        {fields.mode === "confirm" && (
          <div className="flex flex-wrap gap-2">
            <button type="button" disabled={busy} onClick={() => submit("yes")} className={PRIMARY}>
              {tr("是", "Yes")}
            </button>
            <button type="button" disabled={busy} onClick={() => submit("no")} className={SECONDARY}>
              {tr("否", "No")}
            </button>
          </div>
        )}
        {fields.mode === "options" && (
          <form onSubmit={onForm} className="space-y-2">
            <fieldset className="space-y-1.5">
              <legend className="sr-only">{prompt.title}</legend>
              {fields.options.map((o) => (
                <label key={o.id} className="flex items-center gap-2 text-sm text-mast-text">
                  <input type="radio" name={`prompt-${prompt.id}`} checked={choice === o.id} onChange={() => setChoice(o.id)} />
                  {o.label}
                </label>
              ))}
            </fieldset>
            <button type="submit" disabled={busy} className={PRIMARY}>
              {tr("提交", "Submit")}
            </button>
          </form>
        )}
        {fields.mode === "number" && (
          <form onSubmit={onForm} className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <input
                value={text}
                onChange={(e) => setText(e.target.value)}
                inputMode="decimal"
                spellCheck={false}
                aria-label={prompt.title}
                className={clsx(INPUT, "w-40 font-mono")}
              />
              {fields.unit && <span className="text-sm text-mast-text">{fields.unit}</span>}
              <button type="submit" disabled={busy} className={PRIMARY}>
                {tr("提交", "Submit")}
              </button>
            </div>
            <p className="text-xs text-mast-muted">{rangeHint(fields.min, fields.max, fields.unit)}</p>
          </form>
        )}
        {fields.mode === "text" && (
          <form onSubmit={onForm} className="space-y-2">
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={3}
              aria-label={prompt.title}
              className={clsx(INPUT, "w-full resize-y")}
            />
            <button type="submit" disabled={busy} className={PRIMARY}>
              {tr("提交", "Submit")}
            </button>
          </form>
        )}
        {fields.mode === "flag" && (
          <form onSubmit={onForm} className="space-y-2">
            <label className="flex items-center gap-2 text-sm font-medium text-mast-text">
              <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
              {tr("我确认以上情况属实", "I confirm that the above is true")}
            </label>
            <button type="submit" disabled={busy} className={PRIMARY}>
              {tr("确认并签署", "Confirm and sign")}
            </button>
          </form>
        )}
        {fields.mode === "unsupported" && (
          <p className="text-sm text-mast-warn">
            {tr("这个页面还不会回答这类提示；请在 agent 所在的工具里处理，或升级 MAST。", "This page cannot answer this kind of prompt yet; handle it where the agent runs, or update MAST.")}
          </p>
        )}

        {fields.mode !== "unsupported" && (
          <div className="flex flex-wrap items-center gap-2 border-t border-mast-border pt-3">
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={tr("备注（可选）", "Note (optional)")}
              aria-label={tr("备注", "Note")}
              className={clsx(INPUT, "min-w-0 flex-1")}
            />
            {declining ? (
              <>
                <span className="text-xs text-mast-warn">{tr("确定无法完成？agent 会收到“已拒绝”。", "Really cannot do it? The agent is told it was declined.")}</span>
                <button type="button" disabled={busy} onClick={decline} className={SECONDARY}>
                  {tr("确认拒绝", "Yes, decline")}
                </button>
                <button type="button" onClick={() => setDeclining(false)} className="text-xs text-mast-muted hover:text-mast-text">
                  {tr("取消", "Cancel")}
                </button>
              </>
            ) : (
              <button type="button" disabled={busy} onClick={() => setDeclining(true)} className={SECONDARY}>
                {tr("无法完成 / 拒绝", "Can't do it / decline")}
              </button>
            )}
          </div>
        )}

        {answer.isSuccess && <p className="text-sm text-mast-auto">{tr("已提交，正在刷新…", "Sent; refreshing…")}</p>}
        {problem && (
          <p role="alert" className="text-sm text-mast-danger">
            {problem}
          </p>
        )}
      </div>
    </div>
  );
}

function HistoryRow({ p }: { p: OperatorPrompt }) {
  const a = p.answer;
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 border-b border-mast-border py-1.5 text-xs last:border-b-0">
      <Badge tone={p.status === "answered" ? "AUTO" : p.status === "dismissed" ? "WARN" : "default"}>{statusLabel(p.status)}</Badge>
      <b className="text-mast-text">{p.title}</b>
      <span className="text-mast-text">→ {answerSummary(p)}</span>
      <span className="ml-auto text-mast-muted">
        {a?.answered_by ? `${a.answered_by} · ` : ""}
        {fmtClock(a?.answered_at || p.created_at)}
        {a?.source ? ` · ${sourceLabel(a.source)}` : ""}
      </span>
      {a?.note && <span className="basis-full text-mast-muted">{tr("备注：", "Note: ")}{a.note}</span>}
    </li>
  );
}

/** The board: open prompts as cards (safety first), then what was handled recently.
 *  Optical stations only; anywhere else this renders nothing, and the hooks below (history,
 *  slot definitions, clocks) are never even mounted. */
export function OperatorPromptBoard() {
  if (!useIsOpticalStation()) return null;
  return <PromptBoardBody />;
}

function PromptBoardBody() {
  const feed = useOpenPromptFeed({ poll: false }); // AppLayout polls; this reads the shared cache
  const { history } = usePromptHistory(true);
  const [name] = useOperatorName();
  const nowMs = useNow(5_000);

  // An attestation with nothing to choose or type sets a slot to a bare value; the slot
  // definition says which kind of value, so fetch the slots only while one is open.
  const needSlots = feed.prompts.some((p) => p.kind === "attest" && p.options.length === 0 && !p.unit);
  const optical = useOpticalState(needSlots);
  const slots = useMemo<Record<string, SlotHint>>(
    () => Object.fromEntries((optical.data?.slots ?? []).map((s) => [s.id, s])),
    [optical.data],
  );

  return (
    <Section
      title={tr("需要你当场处理", "Needs your action now")}
      actions={feed.openCount > 0 ? <Badge tone={feed.hasSafety ? "DANGEROUS" : "WARN"}>{tr(`${feed.openCount} 待处理`, `${feed.openCount} waiting`)}</Badge> : undefined}
    >
      <p className="mb-3 text-sm text-mast-muted">
        {tr(
          "agent 请你动手或确认的步骤（换镜片、读数、签署激光区安全……）会出现在这里，同时在这台电脑的启动器里弹窗响铃。网页和弹窗谁先答，算谁的。",
          "Steps an agent asks you to do or confirm (change a waveplate, read a value, sign the laser area as safe...) appear here and, with a sound, as a popup from this computer's launcher. Whichever you answer first counts.",
        )}
      </p>
      <div className="mb-3 space-y-2">
        <OperatorNameField />
        <AlertControls />
      </div>

      {feed.unsupported && <ErrorNote error={tr("这个 MAST 版本还没有操作员提示接口。", "This MAST version has no operator prompt API yet.")} label={tr("不可用", "Unavailable")} />}
      {feed.persistError && (
        <p className="mb-3 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-xs text-mast-warn">
          {tr(
            "服务没能把提示列表存盘（仍在内存里工作，但这时重启会丢掉未处理的提示）：",
            "The service could not save its prompt list (it keeps working from memory, but a restart now would lose what is open): ",
          )}
          {feed.persistError}
        </p>
      )}
      {!feed.unsupported && feed.openCount === 0 && (
        <EmptyNote label={tr("现在没有等你处理的提示。", "Nothing is waiting for you right now.")} />
      )}
      <div className="space-y-3">
        {feed.prompts.map((p) => (
          <PromptCard key={p.id} prompt={p} name={name} slots={slots} nowMs={nowMs} />
        ))}
      </div>

      {history.length > 0 && (
        <details className="mt-4 rounded-mast-card border border-mast-border bg-mast-panel px-4 py-2">
          <summary className="cursor-pointer text-sm text-mast-muted hover:text-mast-text">
            {tr(`最近已处理（${history.length}）`, `Recently handled (${history.length})`)}
          </summary>
          <ul className="mt-2">
            {history.slice(0, 50).map((p) => (
              <HistoryRow key={p.id} p={p} />
            ))}
          </ul>
        </details>
      )}
    </Section>
  );
}
