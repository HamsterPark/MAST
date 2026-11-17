import { useMemo } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import { EmptyNote } from "@/components/ui";
import type { Diagnostic } from "@/lib/opticalLayout/indicators";
import type { Issue } from "@/lib/opticalLayout/validate";
import type { DetailedTrace } from "@/lib/opticalLayout/trace";
import type { OpticalLayout } from "@/lib/opticalLayout/types";
import { beamChecks, orderChecks, type BeamCheck } from "@/lib/opticalLayout/beam";

// The beam check: everything the alignment indicators found and the beam checks of v2
// (docs/v2/design/optical_layout_v2.md section 5: a spot past the aperture, light extinguished, a
// waveplate without retardance), in the order of the server's summary (beam.ts CHECK_ORDER), as a
// list that takes the operator to the component. Below it, what would make the server refuse a save.

const TONE = { ok: "border-mast-auto", warn: "border-mast-warn", bad: "border-mast-danger", info: "border-mast-info-border" } as const;

interface Row {
  key: string;
  kind: string;
  tone: keyof typeof TONE;
  componentId: string | null;
  message: string;
}

const fromDiagnostic = (d: Diagnostic, i: number): Row => ({ key: `d${i}`, kind: d.kind, tone: d.level, componentId: d.componentId, message: d.message });

/** A beam check as a row; its message already says how many beams have the problem (beam.ts beamChecks). */
function fromBeamCheck(c: BeamCheck): Row {
  return { key: `b|${c.kind}|${c.componentId}`, kind: c.kind, tone: c.level, componentId: c.componentId, message: c.message };
}

/** The rows of the beam check: the alignment diagnostics and the beam checks together, in the summary's order. */
export function checkRows(diagnostics: readonly Diagnostic[], beam: readonly BeamCheck[]): Row[] {
  return orderChecks([...diagnostics.map(fromDiagnostic), ...beam.map(fromBeamCheck)]);
}

export function CheckPanel({
  diagnostics,
  issues,
  trace,
  layout,
  pending = false,
  onSelect,
}: {
  diagnostics: readonly Diagnostic[];
  issues: readonly Issue[];
  trace: DetailedTrace;
  /** the layout the trace was made from: the beam checks need the components' apertures (without it they are left out) */
  layout?: OpticalLayout;
  /** a drag is in progress: nothing is checked until it ends (the panel says so rather than "no problems") */
  pending?: boolean;
  onSelect: (id: string) => void;
}) {
  const errors = issues.filter((i) => i.level === "error");
  const warns = issues.filter((i) => i.level === "warn");
  const beam = useMemo(() => (layout ? beamChecks(layout, trace) : []), [layout, trace]);
  const rows = checkRows(diagnostics, beam);
  return (
    <div className="space-y-4" data-panel="check">
      <section>
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-mast-muted">{tr("光束检查", "Beam check")}</h3>
        {trace.length === 0 ? (
          <EmptyNote label={tr("还没有光源，没有光可查。放一个激光器或光纤输出。", "No light source yet, so there is no beam to check. Add a laser or a fibre output.")} />
        ) : pending ? (
          <p className="px-1 py-1.5 text-sm text-mast-muted" data-check-pending="">
            {tr("拖动中，松手后重新检查。", "Dragging: the checks run again when you let go.")}
          </p>
        ) : rows.length === 0 ? (
          <p className="rounded-mast-ctl border border-mast-auto-border bg-mast-auto-bg px-2 py-1.5 text-sm text-mast-auto">{tr("没有发现问题：每束光都打在器件口径内，没有打在背面。", "No problems found: every beam meets its component inside the aperture, none at the back.")}</p>
        ) : (
          <ul className="space-y-1">
            {rows.map((r) => (
              <li key={r.key} className={clsx("border-l-2 pl-2 text-sm", TONE[r.tone])} data-check={r.key}>
                {r.componentId ? (
                  <button type="button" className="text-left text-mast-text hover:underline" onClick={() => onSelect(r.componentId as string)}>
                    {r.message}
                  </button>
                ) : (
                  <span className="text-mast-text">{r.message}</span>
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
