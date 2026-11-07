import { useMemo, useState } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import { Badge, EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { OperatorNameField } from "@/components/operator/OperatorNameField";
import { useOperatorName } from "@/hooks/useOperatorPrefs";
import { fmtClock } from "@/lib/operatorPrompts";
import type { LayoutComment, OpticalLayout } from "@/lib/opticalLayout/types";
import { withOperator } from "@/lib/opticalLayout/saveFlow";
import { classifySaveError, saveFailureText } from "@/lib/opticalLayout/saveFlow";
import { useAddComment, useLayoutComments, useResolveComment } from "@/api/opticalLayoutHooks";
import { INPUT_CLS } from "./fields";

// Comments on a layout (section 6.1 of docs/v2/design/optical_layout.md): the operator and
// the external agents talk about the setup here. An agent can write a comment but cannot
// change the geometry or resolve anything; only the operator resolves, under their name.

const MAX_COMMENT = 2000;

function CommentCard({ c, layout, onSelect, onResolve, busy, canResolve }: { c: LayoutComment; layout: OpticalLayout; onSelect: (id: string) => void; onResolve: (c: LayoutComment) => void; busy: boolean; canResolve: boolean }) {
  const comp = c.component_id ? layout.components.find((x) => x.id === c.component_id) : undefined;
  return (
    <li className={clsx("rounded-mast-ctl border p-2", c.resolved ? "border-mast-border bg-mast-panel-2 opacity-80" : c.kind === "agent" ? "border-mast-info-border bg-mast-panel" : "border-mast-border bg-mast-panel")} data-comment={c.id} data-kind={c.kind}>
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <span className="font-semibold text-mast-text">{c.by || "—"}</span>
        <Badge tone={c.kind === "agent" ? "INFO" : "default"}>{c.kind === "agent" ? tr("AI 助手", "Agent") : tr("操作员", "Operator")}</Badge>
        <span className="text-mast-faint">{c.created_at ? fmtClock(c.created_at) : ""}</span>
        {c.resolved && <Badge tone="AUTO">{tr("已解决", "Resolved")}</Badge>}
      </div>
      {c.component_id && (
        <button type="button" className="mt-1 text-xs text-mast-accent underline" onClick={() => comp && onSelect(comp.id)} disabled={!comp} title={comp ? tr("在画布上选中它", "Select it on the canvas") : tr("光路图里已经没有这个器件", "That component is no longer in the layout")}>
          {comp ? `${comp.label || comp.id} (${comp.id})` : `${c.component_id} ${tr("（已不在图中）", "(no longer in the layout)")}`}
        </button>
      )}
      <p className={clsx("mt-1 whitespace-pre-wrap break-words text-sm", c.resolved ? "text-mast-muted" : "text-mast-text")}>{c.text}</p>
      {c.resolved ? (
        <p className="mt-1 text-xs text-mast-faint">{tr(`由 ${c.resolved_by ?? "—"} 解决`, `resolved by ${c.resolved_by ?? "—"}`)}{c.resolved_at ? ` · ${fmtClock(c.resolved_at)}` : ""}</p>
      ) : (
        <div className="mt-1.5">
          <button type="button" disabled={busy || !canResolve} title={canResolve ? undefined : tr("先填写你的姓名", "Enter your name first")} className="rounded-mast-ctl border border-mast-border-strong px-2 py-0.5 text-xs text-mast-text hover:bg-mast-panel-2 disabled:opacity-50" onClick={() => onResolve(c)}>
            {tr("标为已解决", "Mark resolved")}
          </button>
        </div>
      )}
    </li>
  );
}

export function CommentsPanel({ layout, saved, selectedId, onSelect }: { layout: OpticalLayout; saved: boolean; selectedId: string | null; onSelect: (id: string) => void }) {
  const q = useLayoutComments(saved ? layout.id : null);
  const add = useAddComment(layout.id);
  const resolve = useResolveComment(layout.id);
  const [name] = useOperatorName();
  const [text, setText] = useState("");
  const [about, setAbout] = useState<string>("");
  const [showResolved, setShowResolved] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const comments = q.data ?? [];
  const shown = useMemo(() => [...comments].filter((c) => showResolved || !c.resolved).sort((a, b) => (a.resolved === b.resolved ? 0 : a.resolved ? 1 : -1)), [comments, showResolved]);
  const open = comments.filter((c) => !c.resolved).length;
  const target = about !== "" ? about : (selectedId ?? "");
  const hasName = name.trim() !== "";

  if (!saved) {
    return <EmptyNote label={tr("这张光路图还没有保存。保存后才能写评论。", "This layout is not saved yet. Save it to start commenting.")} />;
  }

  const post = () => {
    setProblem(null);
    const t = text.trim();
    if (!t) return setProblem(tr("评论不能为空。", "The comment is empty."));
    const body = withOperator(name, target ? { text: t.slice(0, MAX_COMMENT), component_id: target } : { text: t.slice(0, MAX_COMMENT) });
    if (!body.ok) return setProblem(body.error);
    add.mutate(body.value, {
      onSuccess: () => setText(""),
      onError: (e) => setProblem(saveFailureText(classifySaveError(e))),
    });
  };

  return (
    <div className="space-y-3" data-panel="comments">
      <OperatorNameField />
      <div className="flex items-center justify-between text-xs text-mast-muted">
        <span>{tr(`${open} 条未解决`, `${open} open`)} · {tr(`共 ${comments.length} 条`, `${comments.length} in all`)}</span>
        <label className="flex cursor-pointer items-center gap-1.5">
          <input type="checkbox" checked={showResolved} onChange={(e) => setShowResolved(e.target.checked)} />
          {tr("显示已解决的", "Show resolved")}
        </label>
      </div>
      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} label={tr("评论读取失败", "Could not read the comments")} />}
      {q.isSuccess && shown.length === 0 && <EmptyNote label={comments.length === 0 ? tr("还没有评论。", "No comments yet.") : tr("没有未解决的评论。", "No open comments.")} />}
      <ul className="space-y-2">
        {shown.map((c) => (
          <CommentCard
            key={c.id}
            c={c}
            layout={layout}
            onSelect={onSelect}
            busy={resolve.isPending}
            canResolve={hasName}
            onResolve={(cm) => {
              const body = withOperator(name, {});
              if (!body.ok) return setProblem(body.error);
              resolve.mutate({ commentId: cm.id, body: { by: body.value.by } }, { onError: (e) => setProblem(saveFailureText(classifySaveError(e))) });
            }}
          />
        ))}
      </ul>

      <form
        className="space-y-1.5 border-t border-mast-border pt-3"
        onSubmit={(e) => {
          e.preventDefault();
          post();
        }}
      >
        <label className="block text-xs text-mast-muted" htmlFor="layout-comment-text">
          {tr("写一条评论", "Write a comment")}
        </label>
        <textarea id="layout-comment-text" value={text} onChange={(e) => setText(e.target.value)} rows={3} maxLength={MAX_COMMENT} className={clsx(INPUT_CLS, "resize-y")} placeholder={tr("例如：M2 的角度需要再确认；这个滤光片换过了。", "e.g. M2's angle needs checking; this filter was swapped.")} />
        <div className="flex flex-wrap items-center gap-2">
          <select className={clsx(INPUT_CLS, "!w-auto max-w-[12rem]")} value={target} onChange={(e) => setAbout(e.target.value)} aria-label={tr("评论针对哪个器件", "Which component the comment is about")}>
            <option value="">{tr("整张光路图", "The whole layout")}</option>
            {layout.components.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label || c.id}
              </option>
            ))}
          </select>
          <button type="submit" disabled={add.isPending || !hasName} title={hasName ? undefined : tr("先填写你的姓名", "Enter your name first")} className="rounded-mast-ctl bg-mast-accent px-3 py-1 text-sm font-semibold text-mast-accent-ink hover:opacity-90 disabled:opacity-50">
            {tr("发表", "Post")}
          </button>
        </div>
        {problem && <p className="text-xs text-mast-danger">{problem}</p>}
      </form>
    </div>
  );
}
