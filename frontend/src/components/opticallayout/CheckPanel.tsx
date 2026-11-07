import clsx from "clsx";
import { tr } from "@/lib/i18n";
import { EmptyNote } from "@/components/ui";
import type { Diagnostic } from "@/lib/opticalLayout/indicators";
import type { Issue } from "@/lib/opticalLayout/validate";
import type { DetailedTrace } from "@/lib/opticalLayout/trace";

// The beam check: everything the alignment indicators found, worst first, as a list that
// takes the operator to the component. Below it, what would make the server refuse a save.

const TONE = { ok: "border-mast-auto", warn: "border-mast-warn", bad: "border-mast-danger" } as const;

export function CheckPanel({ diagnostics, issues, trace, onSelect }: { diagnostics: readonly Diagnostic[]; issues: readonly Issue[]; trace: DetailedTrace; onSelect: (id: string) => void }) {
  const errors = issues.filter((i) => i.level === "error");
  const warns = issues.filter((i) => i.level === "warn");
  return (
    <div className="space-y-4" data-panel="check">
      <section>
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-mast-muted">{tr("光束检查", "Beam check")}</h3>
        {trace.length === 0 ? (
          <EmptyNote label={tr("还没有光源，没有光可查。放一个激光器或光纤输出。", "No light source yet, so there is no beam to check. Add a laser or a fibre output.")} />
        ) : diagnostics.length === 0 ? (
          <p className="rounded-mast-ctl border border-mast-auto-border bg-mast-auto-bg px-2 py-1.5 text-sm text-mast-auto">{tr("没有发现问题：每束光都打在器件口径内，没有打在背面。", "No problems found: every beam meets its component inside the aperture, none at the back.")}</p>
        ) : (
          <ul className="space-y-1">
            {diagnostics.map((d, i) => (
              <li key={i} className={clsx("border-l-2 pl-2 text-sm", TONE[d.level])}>
                {d.componentId ? (
                  <button type="button" className="text-left text-mast-text hover:underline" onClick={() => onSelect(d.componentId as string)}>
                    {d.message}
                  </button>
                ) : (
                  <span className="text-mast-text">{d.message}</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
      {(errors.length > 0 || warns.length > 0) && (
        <section>
          <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-mast-muted">{tr("保存前的检查", "Before saving")}</h3>
          <ul className="space-y-1">
            {[...errors, ...warns].map((i, k) => (
              <li key={k} className={clsx("border-l-2 pl-2 text-sm", i.level === "error" ? "border-mast-danger" : "border-mast-warn")}>
                {i.componentId ? (
                  <button type="button" className="text-left text-mast-text hover:underline" onClick={() => onSelect(i.componentId as string)}>
                    <code className="text-xs text-mast-faint">{i.componentId}</code> {i.message}
                  </button>
                ) : (
                  <span className="text-mast-text">
                    <code className="text-xs text-mast-faint">{i.path}</code> {i.message}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
