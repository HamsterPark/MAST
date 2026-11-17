import { useMemo, useState, type ReactNode } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import { EmptyNote } from "@/components/ui";
import type { OpticalLayout } from "@/lib/opticalLayout/types";
import type { DetailedTrace } from "@/lib/opticalLayout/trace";
import { beamProfile, branchPath, polarizationText, powerText, propagateQ, readPolarization, segmentStartPath, spotDiameterMm, spotText, waistDiameterMm, waistInSegment } from "@/lib/opticalLayout/beam";
import { heightText, usesHeights } from "@/lib/opticalLayout/heights";
import { segmentOf, type SegRef } from "@/lib/opticalLayout/beamDraw";
import { CHART_BOX, chartGeometry, profileAt } from "@/lib/opticalLayout/beamChart";
import { wavelengthColor, wavelengthLabel, type ThemeName } from "@/lib/opticalLayout/colors";
import { formatLength } from "@/lib/opticalLayout/view";
import { PolBadgeShape } from "./LayoutSvg";

// The beam inspector (docs/v2/design/optical_layout_v2.md section 8.4): click a beam on the canvas and this
// tab shows that piece of it: where it runs from and to, its power, polarisation and spot at both ends, its
// waist, the path from the source as a list of components (with the power, spot and polarisation arriving
// at each), and the spot size along the whole path as a chart. Every number is read off the trace with
// beam.ts; nothing is computed here that the tracer or the server's summary would compute differently.

export interface BeamInspectorProps {
  layout: OpticalLayout;
  trace: DetailedTrace;
  pick: SegRef | null;
  theme: ThemeName;
  onPick: (r: SegRef | null) => void;
  onSelectComponent: (id: string) => void;
}

function actionText(action: string): string {
  switch (action) {
    case "reflect":
      return tr("反射", "reflects");
    case "transmit":
      return tr("透射", "passes");
    case "split":
      return tr("分束", "splits");
    case "stop":
      return tr("终止", "stops");
    case "blocked":
      return tr("挡住", "blocks");
    case "back_hit":
      return tr("打在背面", "hits the back");
    default:
      return tr("作用", "acts on it");
  }
}

function endText(end: string): string {
  switch (end) {
    case "left_table":
      return tr("离开台面", "leaves the table");
    case "max_events":
      return tr("作用次数到上限", "64 interactions reached");
    case "budget":
      return tr("追迹在此截断", "the trace stops here");
    case "blocked":
      return tr("被挡住，光到此为止", "blocked: the light ends here");
    case "back_hit":
      return tr("打在器件背面，光到此为止", "hits the back of a component: the light ends here");
    case "stop":
      return tr("被器件接收", "received by a component");
    case "split":
      return tr("在这里分束", "splits here");
    default:
      return tr("光到此为止", "the light ends here");
  }
}

/** A name short enough for the top of the chart. */
const short = (s: string) => (s.length > 9 ? `${s.slice(0, 8)}…` : s);

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] items-baseline gap-x-2 py-0.5 text-sm">
      <span className="text-xs text-mast-muted">{label}</span>
      <span className="min-w-0 text-mast-text">{children}</span>
    </div>
  );
}

function Swatch({ color, dashed }: { color: string; dashed: boolean }) {
  return (
    <svg width={22} height={10} aria-hidden="true" className="shrink-0">
      <line x1={1} y1={5} x2={21} y2={5} stroke={color} strokeWidth={3} strokeDasharray={dashed ? "5 3" : undefined} strokeLinecap="round" />
    </svg>
  );
}

function SpotChart({ tree, branch, span, color, label }: { tree: DetailedTrace[number]; branch: string; span: { from: number; to: number } | null; color: string; label: (id: string) => string }) {
  const profile = useMemo(() => beamProfile(tree, branch), [tree, branch]);
  const geo = useMemo(() => (profile ? chartGeometry(profile, CHART_BOX, span) : null), [profile, span]);
  const [hoverX, setHoverX] = useState<number | null>(null);
  if (!profile || !geo) {
    return <p className="text-xs text-mast-faint">{tr("这束光没有光束直径（光源没填 beam_diameter_mm，或波长未知），所以没有光斑曲线。", "This beam has no size (its source has no beam_diameter_mm, or no wavelength), so there is no spot curve.")}</p>;
  }
  const yScale = geo.yUnit === "µm" ? 1000 : 1;
  const hover = hoverX !== null ? profileAt(profile, geo.pathAt(hoverX)) : null;
  return (
    <figure className="m-0">
      <svg
        viewBox={`0 0 ${CHART_BOX.width} ${CHART_BOX.height}`}
        className="block w-full"
        role="img"
        aria-label={tr("光斑直径沿光程的曲线", "Beam diameter along the path")}
        data-spot-chart=""
        onMouseMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          const x = ((e.clientX - r.left) / r.width) * CHART_BOX.width;
          setHoverX(x >= geo.x0 && x <= geo.x1 ? x : null);
        }}
        onMouseLeave={() => setHoverX(null)}
      >
        {geo.span && <rect x={geo.span.x0} y={geo.y1} width={Math.max(1, geo.span.x1 - geo.span.x0)} height={geo.y0 - geo.y1} fill="var(--mast-accent-soft)" />}
        {geo.yTicks.map((t) => (
          <g key={`y${t.label}`}>
            <line x1={geo.x0} x2={geo.x1} y1={t.y} y2={t.y} stroke="var(--mast-border)" strokeWidth={0.6} />
            <text x={geo.x0 - 4} y={t.y + 3} textAnchor="end" fontSize={8.5} fill="var(--mast-faint)" className="font-mono">
              {t.label}
            </text>
          </g>
        ))}
        {geo.marks.map((m, i) => (
          <g key={`m${i}`}>
            <line x1={m.x} x2={m.x} y1={geo.y1} y2={geo.y0} stroke="var(--mast-border-strong)" strokeWidth={0.8} strokeDasharray="2 2" />
            {m.labelled && (
              <text x={m.x} y={geo.y1 - 4} textAnchor="middle" fontSize={8} fill="var(--mast-muted)">
                {short(label(m.componentId))}
              </text>
            )}
          </g>
        ))}
        <path d={`${geo.d}L${geo.x1} ${geo.y0}L${geo.x0} ${geo.y0}Z`} fill={color} fillOpacity={0.12} stroke="none" />
        <path d={geo.d} fill="none" stroke={color} strokeWidth={1.6} strokeLinejoin="round" />
        {geo.waists.map((w, i) => (
          <circle key={`w${i}`} cx={w.x} cy={w.y} r={2.6} fill="var(--mast-panel)" stroke={color} strokeWidth={1.4} />
        ))}
        <line x1={geo.x0} x2={geo.x1} y1={geo.y0} y2={geo.y0} stroke="var(--mast-muted)" strokeWidth={0.8} />
        <line x1={geo.x0} x2={geo.x0} y1={geo.y1} y2={geo.y0} stroke="var(--mast-muted)" strokeWidth={0.8} />
        {geo.xTicks.map((t) => (
          <text key={`x${t.label}`} x={t.x} y={geo.y0 + 12} textAnchor="middle" fontSize={8.5} fill="var(--mast-faint)" className="font-mono">
            {t.label}
          </text>
        ))}
        <text x={4} y={geo.y1 - 4} fontSize={8.5} fill="var(--mast-faint)">
          Ø {geo.yUnit}
        </text>
        {hover && hoverX !== null && (
          <g pointerEvents="none">
            <line x1={geo.xOf(hover.pathMm)} x2={geo.xOf(hover.pathMm)} y1={geo.y1} y2={geo.y0} stroke="var(--mast-accent)" strokeWidth={0.8} />
            <circle cx={geo.xOf(hover.pathMm)} cy={geo.yOf(hover.dMm)} r={2.4} fill="var(--mast-accent)" />
          </g>
        )}
      </svg>
      <figcaption className="mt-0.5 flex justify-between font-mono text-[11px] text-mast-muted">
        <span>{tr("光程 (mm)", "path (mm)")}</span>
        <span>{hover ? `${formatLength(hover.pathMm)} · ${spotText(hover.dMm)}` : `${(geo.waists.length > 0 ? tr("腰 ", "waist ") + spotText(Math.min(...geo.waists.map((w) => w.dMm))) : "").trim()}`}</span>
      </figcaption>
      {yScale === 1000 && <p className="sr-only">{tr("纵轴单位是微米。", "The vertical axis is in micrometres.")}</p>}
    </figure>
  );
}

export function BeamInspector({ layout, trace, pick, theme, onPick, onSelectComponent }: BeamInspectorProps) {
  const label = (id: string) => layout.components.find((c) => c.id === id)?.label || id;
  const found = pick ? segmentOf(trace, pick) : null;

  if (trace.length === 0) {
    return <EmptyNote label={tr("还没有光源，没有光可看。放一个激光器或光纤输出。", "No light source yet, so there is no beam to look at. Add a laser or a fibre output.")} />;
  }

  if (!pick || !found) {
    return (
      <div className="space-y-3" data-panel="beam">
        <p className="text-sm text-mast-muted">{tr("点击画布上的一束光，看它这一段的功率、偏振和光斑，以及光斑沿整条光路的变化。也可以从下面选一束。", "Click a beam on the canvas to see the power, polarisation and spot of that piece of it, and the spot along its whole path. Or pick one below.")}</p>
        <ul className="space-y-1" data-beam-list="">
          {trace.map((t, i) => {
            const root = t.branches[0];
            const mw = t.beam?.source_mw ?? null;
            const color = wavelengthColor(t.wavelength_nm, theme);
            return (
              <li key={`${t.source}|${t.wavelength_nm}|${i}`}>
                <button type="button" disabled={!root || root.segments.length === 0} onClick={() => onPick({ tree: i, branch: "0", segment: 0 })} className="flex w-full items-center gap-2 rounded px-1.5 py-1 text-left text-sm text-mast-text hover:bg-mast-panel-2 disabled:opacity-50">
                  <Swatch color={color} dashed={!(t.wavelength_nm !== null && t.wavelength_nm >= 380 && t.wavelength_nm <= 780)} />
                  <span className="min-w-0 flex-1 truncate">{label(t.source)}</span>
                  <span className="font-mono text-xs text-mast-muted">{wavelengthLabel(t.wavelength_nm)}</span>
                  {mw !== null && <span className="font-mono text-xs text-mast-muted">{powerText(1, mw)}</span>}
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    );
  }

  const { tree, branch, segment } = found;
  const si = pick.segment;
  const wl = tree.wavelength_nm;
  const m2 = tree.beam?.m2 ?? null;
  const color = wavelengthColor(wl, theme);
  const L = Math.hypot(segment.x1 - segment.x0, segment.y1 - segment.y0);
  const b = segment.beam;
  const sourceMw = tree.beam?.source_mw ?? null;
  const mw = b && sourceMw !== null ? sourceMw * b.p : null;
  const q = b?.q ?? null;
  const d0 = spotDiameterMm(q, wl, m2);
  const d1 = q === null ? null : spotDiameterMm(propagateQ(q, L), wl, m2);
  const w0 = waistInSegment(q, L);
  // up to 0.001 mm past the end counts as this segment's (section 12): said as at its end
  const waistAt = w0 === null ? null : Math.min(L, Math.max(0, w0));
  const path = branchPath(tree, branch.branch);
  // where the segment starts: the source, the splitter it came out of, or the component before it
  const fromId = si > 0 ? branch.events[si - 1]?.component : branch.parent === null ? tree.source : (path[path.length - 2]?.events.at(-1)?.component ?? tree.source);
  const toEvent = branch.events[si];
  // after a periscope the segment starts its vertical run later (docs/v2/design/optical_layout_3d.md section 3.4)
  const startPath = segmentStartPath(branch, si);
  const reading = readPolarization(b?.s ?? null);

  return (
    <div className="space-y-3" data-panel="beam">
      <div className="flex items-center gap-2">
        <Swatch color={color} dashed={!(wl !== null && wl >= 380 && wl <= 780)} />
        <span className="min-w-0 flex-1 truncate text-sm font-semibold text-mast-text">
          {label(tree.source)} · {wavelengthLabel(wl)}
          {branch.branch !== "0" && <span className="ml-1 font-mono text-xs font-normal text-mast-muted">{tr(`分支 ${branch.branch}`, `branch ${branch.branch}`)}</span>}
        </span>
        <button type="button" className="shrink-0 rounded px-1.5 text-sm text-mast-muted hover:bg-mast-panel-2 hover:text-mast-text" aria-label={tr("不看这束光", "Close this beam")} onClick={() => onPick(null)}>
          ✕
        </button>
      </div>

      <section aria-label={tr("这一段", "This segment")} data-beam-segment="">
        <h3 className="mb-1 text-xs font-semibold text-mast-muted">{tr(`这一段（第 ${si + 1} / ${branch.segments.length} 段）`, `This segment (${si + 1} of ${branch.segments.length})`)}</h3>
        <Row label={tr("从 → 到", "From → to")}>
          {label(fromId ?? tree.source)} → {toEvent ? label(toEvent.component) : endText(branch.end)}
        </Row>
        <Row label={tr("长度", "Length")}>
          <span className="font-mono">{formatLength(L)}</span>
          <span className="ml-2 text-xs text-mast-muted">{tr(`光程 ${formatLength(startPath)} 起`, `from ${formatLength(startPath)} of path`)}</span>
        </Row>
        {segment.z_mm !== undefined && usesHeights(layout) && (
          <Row label={tr("高度", "Height")}>
            <span className="font-mono">{heightText(segment.z_mm)} mm</span>
            {segment.passes && segment.passes.length > 0 && (
              <span className="ml-2 text-xs text-mast-muted">{tr(`越过 ${segment.passes.map((p) => label(p.component)).join("、")}`, `passes ${segment.passes.map((p) => label(p.component)).join(", ")}`)}</span>
            )}
          </Row>
        )}
        <Row label={tr("功率", "Power")}>
          {b ? (
            <span className="font-mono">
              {powerText(b.p, mw)}
              {mw !== null && <span className="ml-2 text-xs text-mast-muted">{(b.p * 100).toFixed(1)} %</span>}
            </span>
          ) : (
            <span className="text-mast-faint">{tr("这条追迹没有光束数据", "this trace has no beam data")}</span>
          )}
        </Row>
        <Row label={tr("偏振", "Polarisation")}>
          <span className="flex items-center gap-2">
            {reading.kind !== "none" && (
              <svg width={22} height={22} viewBox="-11 -11 22 22" aria-hidden="true" className="shrink-0">
                <PolBadgeShape reading={reading} r={10} />
              </svg>
            )}
            <span>{b ? polarizationText(b.s) : "—"}</span>
          </span>
        </Row>
        <Row label={tr("光斑", "Spot")}>
          <span className="font-mono">{d0 === null ? tr("没有光束直径", "no beam size") : `${spotText(d0)} → ${spotText(d1)}`}</span>
        </Row>
        {q !== null && wl !== null && m2 !== null && (
          <Row label={tr("腰", "Waist")}>
            {waistAt !== null ? (
              <span className="font-mono">{tr(`${label(fromId ?? tree.source)} 之后 ${formatLength(waistAt)}，${spotText(waistDiameterMm(q, wl, m2))}`, `${formatLength(waistAt)} after ${label(fromId ?? tree.source)}, ${spotText(waistDiameterMm(q, wl, m2))}`)}</span>
            ) : (
              <span className="text-mast-muted">{q[0] >= 0 ? tr("这一段在腰之后（发散）", "past the waist here (diverging)") : tr("这一段还没到腰（会聚）", "before the waist here (converging)")}</span>
            )}
          </Row>
        )}
      </section>

      <section aria-label={tr("光斑沿光程", "Spot along the path")}>
        <h3 className="mb-1 text-xs font-semibold text-mast-muted">{tr("光斑沿光程（1/e² 直径）", "Spot along the path (1/e² diameter)")}</h3>
        <SpotChart tree={tree} branch={branch.branch} span={{ from: startPath, to: startPath + L }} color={color} label={label} />
      </section>

      <section aria-label={tr("光路", "Path")} data-beam-path="">
        <h3 className="mb-1 text-xs font-semibold text-mast-muted">{tr("光路（每个器件处到达的光）", "Path (the light arriving at each component)")}</h3>
        <ol className="space-y-0.5">
          {(() => {
            const first = path[0]?.segments[0]?.beam;
            return (
              <li className="rounded px-1.5 py-1 text-xs text-mast-muted">
                <span className="font-semibold text-mast-text">{label(tree.source)}</span>
                {first && <span className="ml-1">{[powerText(first.p, sourceMw === null ? null : sourceMw * first.p), spotText(spotDiameterMm(first.q, wl, m2)), polarizationText(first.s)].join(" · ")}</span>}
              </li>
            );
          })()}
          {path.flatMap((br) =>
            br.events.map((ev, i) => {
              const on = br.branch === branch.branch && i === si;
              const parts = ev.beam ? [powerText(ev.beam.p, ev.beam.mw), ...(ev.beam.d_mm !== null ? [spotText(ev.beam.d_mm)] : []), polarizationText(ev.beam.s)] : [];
              return (
                <li key={`${br.branch}|${i}`}>
                  <button
                    type="button"
                    onClick={() => onPick({ tree: pick.tree, branch: br.branch, segment: i })}
                    onDoubleClick={() => onSelectComponent(ev.component)}
                    title={tr("点击：看到这里的那一段；双击：选中这个器件", "Click: the segment that ends here; double-click: select this component")}
                    className={clsx("w-full rounded px-1.5 py-1 text-left text-xs hover:bg-mast-panel-2", on ? "bg-mast-accent-soft text-mast-text" : "text-mast-muted")}
                  >
                    <span className={clsx("font-semibold", on ? "text-mast-accent" : "text-mast-text")}>{label(ev.component)}</span>
                    <span className="ml-1">{actionText(ev.action)}</span>
                    {parts.length > 0 && <span className="block truncate font-mono">{parts.join(" · ")}</span>}
                  </button>
                </li>
              );
            }),
          )}
          {branch.end !== "split" && branch.end !== "stop" && <li className="px-1.5 py-1 text-xs text-mast-faint">{endText(branch.end)}</li>}
        </ol>
        {branch.end === "split" && (
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-mast-muted" data-beam-children="">
            <span>{tr("在这里分成：", "Splits here into:")}</span>
            {(["r", "t"] as const).map((side) => {
              const child = tree.branches.find((x) => x.branch === `${branch.branch}.${side}`);
              const first = child?.segments[0]?.beam;
              return child ? (
                <button key={side} type="button" className="rounded border border-mast-border px-1.5 py-0.5 text-mast-text hover:bg-mast-panel-2" onClick={() => onPick({ tree: pick.tree, branch: child.branch, segment: 0 })}>
                  {side === "r" ? tr("反射支", "reflected") : tr("透射支", "transmitted")}
                  {first && <span className="ml-1 font-mono text-mast-muted">{powerText(first.p, sourceMw === null ? null : sourceMw * first.p)}</span>}
                </button>
              ) : (
                <span key={side} className="text-mast-faint">{side === "r" ? tr("（反射支太弱，没有追）", "(reflected: too weak to trace)") : tr("（透射支太弱，没有追）", "(transmitted: too weak to trace)")}</span>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
