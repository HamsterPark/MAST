import clsx from "clsx";
import { tr } from "@/lib/i18n";
import { Badge, EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { fmtClock } from "@/lib/operatorPrompts";
import { useLayoutHistory } from "@/api/opticalLayoutHooks";

// The saved revisions of a layout (section 6.1: the server keeps the last 200). Look at
// one without changing anything, or go back to it: "going back" saves its content as a
// NEW revision on top of the current one, so nothing is ever rewritten and going back can
// itself be undone by going forward again.

export function HistoryPanel({
  layoutId,
  saved,
  currentRevision,
  viewing,
  busy,
  onView,
  onBack,
  onRestore,
}: {
  layoutId: string;
  saved: boolean;
  /** the revision our edits build on */
  currentRevision: number;
  viewing: number | null;
  busy: boolean;
  onView: (revision: number) => void;
  onBack: () => void;
  onRestore: (revision: number) => void;
}) {
  const q = useLayoutHistory(layoutId, saved);
  if (!saved) return <EmptyNote label={tr("这张光路图还没有保存，没有历史。", "This layout is not saved yet, so it has no history.")} />;

  return (
    <div className="space-y-2" data-panel="history">
      {viewing !== null && (
        <div className="rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg p-2 text-xs text-mast-warn">
          <p className="font-semibold">{tr(`正在查看第 ${viewing} 版（只读）`, `Viewing revision ${viewing} (read only)`)}</p>
          <div className="mt-1.5 flex flex-wrap gap-2">
            <button type="button" className="rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-2 py-0.5 text-mast-text hover:bg-mast-panel-2" onClick={onBack}>
              {tr("回到当前编辑", "Back to editing")}
            </button>
            <button type="button" disabled={busy} className="rounded-mast-ctl bg-mast-accent px-2 py-0.5 font-semibold text-mast-accent-ink hover:opacity-90 disabled:opacity-50" onClick={() => onRestore(viewing)}>
              {tr("回到这一版（存为新版）", "Go back to it (saves a new revision)")}
            </button>
          </div>
        </div>
      )}
      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} label={tr("历史读取失败", "Could not read the history")} />}
      {q.isSuccess && q.data.length === 0 && <EmptyNote label={tr("还没有历史版本。", "No revisions yet.")} />}
      <ul className="space-y-1">
        {(q.data ?? []).map((r) => (
          <li key={r.revision} className={clsx("rounded-mast-ctl border px-2 py-1.5", viewing === r.revision ? "border-mast-accent bg-mast-accent-soft" : "border-mast-border bg-mast-panel")} data-revision={r.revision}>
            <div className="flex flex-wrap items-center gap-1.5 text-xs">
              <span className="font-mono font-semibold text-mast-text">r{r.revision}</span>
              {r.revision === currentRevision && <Badge tone="AUTO">{tr("当前", "Current")}</Badge>}
              <span className="text-mast-muted">{r.updated_by || "—"}</span>
              <span className="text-mast-faint">{r.updated_at ? fmtClock(r.updated_at) : ""}</span>
              {r.components !== null && <span className="text-mast-faint">{tr(`${r.components} 个器件`, `${r.components} components`)}</span>}
            </div>
            {r.title && <p className="truncate text-xs text-mast-faint">{r.title}</p>}
            {r.revision !== currentRevision && (
              <div className="mt-1 flex gap-2">
                <button type="button" className="text-xs text-mast-accent underline" onClick={() => onView(r.revision)}>
                  {tr("查看", "View")}
                </button>
                <button type="button" disabled={busy} className="text-xs text-mast-accent underline disabled:opacity-50" onClick={() => onRestore(r.revision)}>
                  {tr("回到这一版", "Go back to it")}
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
