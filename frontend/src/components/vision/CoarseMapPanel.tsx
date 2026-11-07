import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { components } from "@/api/schema";
import { Card, Spinner, ErrorNote, DegradedNote } from "@/components/ui";
import { getLang, tr } from "@/lib/i18n";
import {
  COARSE_ZOOM_MAX,
  COARSE_ZOOM_RESET,
  type CoarseZoom,
  chromeAt,
  coarseViewBox,
  fitCoarse,
  fmtZoom,
  isZoomed,
  panByFraction,
  toStage,
  zoomAtPoint,
} from "@/lib/coarseMapView";

// Coarse map — the second, sample-stage-scale map.
//
// The scan map covers the piezo range, in metres, for the current
// coordinate generation only. This map covers the whole sample: every lateral
// coarse move is a new site, measured in coarse STEPS, across all generations.
// They answer different questions, so they are drawn as two panels, not merged.
//
// Key visual decision: a site is a blurred blob, not a point. Coarse motion is
// open loop, so position is an accumulated step count whose error grows with
// distance travelled. Drawing it as an exact point would present an estimate as a
// coordinate, and the map exists to avoid revisiting places, so the blur radius is
// the criterion itself.
//
// The piezo range is drawn to scale inside the current site: the barely visible
// small square is the entire extent of the scan map. The difference in scale is
// the most important thing to see — it explains why changing region cannot be
// done with the piezo.

type CoarseMap = components["schemas"]["CoarseMapResponse"];
type Site = components["schemas"]["CoarseSiteView"];

function fmtSteps(n: number): string {
  return n.toLocaleString();
}

/** Steps to µm is an annotation only: coarse motion is open loop and the step size drifts with drive amplitude, load and temperature. */
function approx(site: Site): string | null {
  if (site.approx_x_um == null || site.approx_y_um == null) return null;
  return `≈ (${site.approx_x_um.toFixed(1)}, ${site.approx_y_um.toFixed(1)}) µm`;
}

export function CoarseMapPanel({
  data,
  isPending,
  isError,
  error,
  onRefresh,
  isFetching,
  defaultCompact = false,
}: {
  data?: CoarseMap;
  isPending: boolean;
  isError: boolean;
  error?: unknown;
  onRefresh: () => void;
  isFetching: boolean;
  /**
   * Whether the panel starts compact.
   *
   * A prop rather than hard-coded: on the chat page the map competes with the
   * chat for space, while on `/vision` it is the main content. It is only the
   * initial value; the user can expand it at any time — compact mode hides size,
   * not information.
   */
  defaultCompact?: boolean;
}) {
  const [compact, setCompact] = useState(defaultCompact);
  const [zoom, setZoom] = useState<CoarseZoom>(COARSE_ZOOM_RESET);
  const svgRef = useRef<SVGSVGElement | null>(null);

  const fit = useMemo(
    () => fitCoarse(data?.sites ?? null, data?.site_spacing_steps),
    [data],
  );

  // Reset the zoom when the site set changes (new site, new sample). The zoom is
  // relative to the fit box at the time, so keeping it would leave the user in
  // some corner of the new map — the same reason double-click resets.
  const fitKey = fit ? `${fit.cx}|${fit.cy}|${fit.span}` : "";
  useEffect(() => {
    setZoom(COARSE_ZOOM_RESET);
  }, [fitKey]);

  /** Pointer position normalised within the SVG element. Both the element and its viewBox are square, so the mapping is plain linear. */
  const frac = useCallback((clientX: number, clientY: number) => {
    const el = svgRef.current;
    if (!el) return null;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return null;
    return { fx: (clientX - r.left) / r.width, fy: (clientY - r.top) / r.height };
  }, []);

  // Wheel zoom. Must use addEventListener with passive:false: React's onWheel is
  // passive at the root, so preventDefault() inside it has no effect and the wheel
  // would zoom the map and scroll the page at the same time.
  useEffect(() => {
    const el = svgRef.current;
    if (!el || !fit) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const f = frac(e.clientX, e.clientY);
      if (!f) return;
      const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
      setZoom((z) => zoomAtPoint(fit, z, f.fx, f.fy, factor));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [fit, frac]);

  // Drag to pan. Only meaningful once zoomed in (at k=1 panByFraction is a no-op;
  // the guard just avoids changing the cursor needlessly).
  const drag = useRef<{ x: number; y: number } | null>(null);
  const onPointerDown = (e: React.PointerEvent<SVGSVGElement>) => {
    if (!fit || zoom.k <= 1) return;
    drag.current = { x: e.clientX, y: e.clientY };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    const el = svgRef.current;
    if (!d || !fit || !el) return;
    const r = el.getBoundingClientRect();
    if (r.width <= 0) return;
    const dfx = (e.clientX - d.x) / r.width;
    const dfy = (e.clientY - d.y) / r.height;
    drag.current = { x: e.clientX, y: e.clientY };
    setZoom((z) => panByFraction(fit, z, dfx, dfy));
  };
  const endDrag = (e: React.PointerEvent<SVGSVGElement>) => {
    drag.current = null;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
  };

  // Button zoom, anchored at the view centre. The wheel is not available or
  // comfortable on every device, so zooming must not depend on it alone.
  const nudge = (factor: number) =>
    setZoom((z) => (fit ? zoomAtPoint(fit, z, 0.5, 0.5, factor) : z));

  const zoomed = isZoomed(zoom);
  /** Chrome (non-data drawing) sizes are all derived from the fit span; `ch()` is their only exit. */
  const ch = (denom: number) => (fit ? chromeAt(fit.span / denom, zoom) : 0);

  // The piezo range (metres) converted to steps; only drawable once a step-size calibration is set.
  const piezoSteps =
    data?.step_m && data.step_m > 0
      ? (data.piezo_half_range_m ?? 1.5e-6) / data.step_m
      : null;

  const btn =
    "rounded border border-mast-border bg-mast-panel px-2 py-1 text-xs text-mast-muted hover:text-mast-text disabled:opacity-50";

  return (
    <Card
      className={
        "min-w-[300px] flex-1 space-y-2 text-sm " + (compact ? "" : "lg:basis-full")
      }
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold">
          {tr("粗动大地图（样品台尺度）", "Coarse map (sample-stage scale)")}
        </span>
        <div className="flex items-center gap-1">
          {/* Zoom controls: buttons in addition to the wheel, since trackpad wheel semantics vary. */}
          <button
            type="button"
            onClick={() => nudge(1 / 1.6)}
            disabled={!fit || zoom.k <= 1}
            className={btn}
            aria-label={tr("缩小", "Zoom out")}
            title={tr("缩小", "Zoom out")}
          >
            −
          </button>
          <span className="min-w-[2.5rem] text-center text-xs tabular-nums text-mast-muted">
            {fmtZoom(zoom)}
          </span>
          <button
            type="button"
            onClick={() => nudge(1.6)}
            disabled={!fit || zoom.k >= COARSE_ZOOM_MAX}
            className={btn}
            aria-label={tr("放大", "Zoom in")}
            title={tr("放大", "Zoom in")}
          >
            +
          </button>
          <button
            type="button"
            onClick={() => setZoom(COARSE_ZOOM_RESET)}
            disabled={!zoomed}
            className={btn}
            title={tr("回到能看见全部站点的视野", "Return to the view that shows every site")}
          >
            {tr("复位", "Reset")}
          </button>
          <button
            type="button"
            onClick={() => setCompact((v) => !v)}
            className={btn}
            title={compact ? tr("展开成大图", "Expand to full map") : tr("收成小地图", "Collapse to mini map")}
          >
            {compact ? tr("展开", "Expand") : tr("紧凑", "Compact")}
          </button>
          <button
            type="button"
            onClick={onRefresh}
            disabled={isFetching}
            className={btn}
          >
            {isFetching ? tr("读取中…", "Loading…") : tr("刷新", "Refresh")}
          </button>
        </div>
      </div>

      {isPending && <Spinner />}
      {isError && <ErrorNote error={error} />}
      {data?.degraded && <DegradedNote what={tr("粗动大地图", "coarse map")} />}

      {data && !data.degraded && (() => {
        // Narrow once, up front: every optional field below is required for the
        // drawing to mean anything, and threading `?.` through the geometry is
        // how a missing site silently becomes a plotted point at the origin.
        const sites = data.sites ?? [];
        const current = sites.find((s) => s.is_current);
        const budget = data.axis_step_budget ?? 0;
        const lands = data.suggestion?.lands_at_steps ?? [];
        return (
        <>
          {!compact && (
            <p className="text-xs text-mast-muted">
              {getLang() === "zh" ? (
                <>
                  每一次横向粗动 = 一个新站点 = 一个新坐标代次。单位是<b>步</b>，不是米：
                  粗动开环，步长随驱动幅度 / 负载 / 温度漂移，所以站点画成<b>模糊斑</b>而不是点，
                  半径就是位置不确定量。
                </>
              ) : (
                <>
                  Every lateral coarse move = a new site = a new coordinate generation.
                  The unit is <b>steps</b>, not metres: coarse motion is open loop and the
                  step size drifts with drive amplitude / load / temperature, so each site
                  is drawn as a <b>blurred blob</b> rather than a point, and its radius is
                  the position uncertainty.
                </>
              )}
            </p>
          )}

          {fit && (
            <svg
              ref={svgRef}
              viewBox={coarseViewBox(fit, zoom)}
              className="w-full touch-none select-none rounded border border-mast-border bg-mast-bg"
              style={{
                aspectRatio: "1 / 1",
                // Limit the height rather than cutting content: with zoom available,
                // a small size no longer means unreadable.
                maxHeight: compact ? 280 : undefined,
                cursor: zoom.k > 1 ? (drag.current ? "grabbing" : "grab") : "default",
              }}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={endDrag}
              onPointerCancel={endDrag}
              onDoubleClick={() => setZoom(COARSE_ZOOM_RESET)}
              role="img"
              aria-label={tr(
                "粗动站点分布（滚轮缩放，拖动平移，双击复位）",
                "Coarse site distribution (wheel to zoom, drag to pan, double-click to reset)",
              )}
            >
              {/* Travel-budget boundary. The box is data (real step counts); the stroke is chrome. */}
              {budget > 0 && (
                <rect
                  x={-budget - fit.cx}
                  y={-budget + fit.cy}
                  width={budget * 2}
                  height={budget * 2}
                  fill="none"
                  stroke="currentColor"
                  strokeOpacity={0.18}
                  strokeDasharray={ch(60)}
                  strokeWidth={ch(300)}
                />
              )}

              {sites.map((s) => {
                const p = toStage(fit, s);
                const cur = s.is_current;
                // The radius is data (the real uncertainty); only its lower bound is
                // chrome — it keeps tiny blobs visible, and once zoomed in the real
                // radius is large enough on its own, so the floor must shrink too.
                const r = Math.max(s.uncertainty_steps, ch(90));
                return (
                  <g key={s.index}>
                    {/* Blur blob: position uncertainty, deliberately drawn larger than the marker. */}
                    <circle
                      cx={p.x}
                      cy={p.y}
                      r={r}
                      fill={cur ? "#14b8a6" : "#8b5cf6"}
                      fillOpacity={cur ? 0.22 : 0.12}
                      stroke={cur ? "#14b8a6" : "#8b5cf6"}
                      strokeOpacity={s.position_known ? 0.5 : 0.25}
                      strokeWidth={ch(400)}
                      strokeDasharray={s.position_known ? undefined : ch(80)}
                    />
                    <circle cx={p.x} cy={p.y} r={ch(160)} fill={cur ? "#14b8a6" : "#8b5cf6"} />
                    {/* Piezo range to scale — the small square is the full extent of the scan map. It is data; zoom makes it legible. */}
                    {cur && piezoSteps && (
                      <rect
                        x={p.x - piezoSteps}
                        y={p.y - piezoSteps}
                        width={piezoSteps * 2}
                        height={piezoSteps * 2}
                        fill="none"
                        stroke="#22d3ee"
                        strokeWidth={ch(400)}
                      />
                    )}
                    <text
                      x={p.x}
                      y={p.y - r - ch(60)}
                      textAnchor="middle"
                      fill="currentColor"
                      fillOpacity={0.65}
                      fontSize={ch(34)}
                    >
                      #{s.index}
                      {s.summary?.scans
                        ? tr(` · ${s.summary.scans}图`, ` · ${s.summary.scans} scans`)
                        : ""}
                      {s.summary?.damage
                        ? tr(` · ${s.summary.damage}破坏`, ` · ${s.summary.damage} damage`)
                        : ""}
                    </text>
                  </g>
                );
              })}

              {/* Suggested landing point: cross + dashed line */}
              {data.suggestion && current && (() => {
                const from = toStage(fit, current);
                const to = toStage(fit, {
                  x_steps: lands[0] ?? 0,
                  y_steps: lands[1] ?? 0,
                });
                const k = ch(45);
                return (
                  <g stroke="#f59e0b" strokeWidth={ch(300)} fill="none">
                    <line
                      x1={from.x}
                      y1={from.y}
                      x2={to.x}
                      y2={to.y}
                      strokeDasharray={ch(70)}
                    />
                    <line x1={to.x - k} y1={to.y} x2={to.x + k} y2={to.y} />
                    <line x1={to.x} y1={to.y - k} x2={to.x} y2={to.y + k} />
                  </g>
                );
              })()}
            </svg>
          )}

          <div className="space-y-1 text-xs">
            <Row
              label={tr("当前站点", "Current site")}
              value={`#${data.current_index}${data.position_known ? "" : tr("（里程表已失效）", " (odometer invalid)")}`}
            />
            <Row
              label={tr("已用行程", "Travel used")}
              value={tr(
                `x ${fmtSteps(data.budget_used_steps?.x ?? 0)} / y ${fmtSteps(
                  data.budget_used_steps?.y ?? 0,
                )} 步（预算 ${fmtSteps(budget)}）`,
                `x ${fmtSteps(data.budget_used_steps?.x ?? 0)} / y ${fmtSteps(
                  data.budget_used_steps?.y ?? 0,
                )} steps (budget ${fmtSteps(budget)})`,
              )}
            />
            {current?.temperature_k != null && (
              <Row label={tr("移动时温度", "Temperature at move")} value={`${current.temperature_k.toFixed(1)} K`} />
            )}
            {(() => {
              const a = current ? approx(current) : null;
              return a ? <Row label={tr("粗略位置", "Approx. position")} value={`${a}${tr("（仅注释，不作几何）", " (annotation only, not geometry)")}`} /> : null;
            })()}
          </div>

          {/* Site history (first_ts / last_ts / summary): which site had what done when is
              what you ask before changing site, and the SVG label is too small to read.
              Positions are always in steps — the same open-loop odometer as the map, never converted to metres. */}
          {sites.length > 0 && (
            <details className="text-xs" open={!compact}>
              {/* Compact mode collapses the history rather than removing it: a mini
                  map should not lose information. The <details> summary shows the
                  site count, so it is obvious there is more underneath. */}
              <summary
                className={
                  "cursor-pointer text-mast-muted marker:text-mast-muted " +
                  (compact ? "" : "list-none [&::-webkit-details-marker]:hidden")
                }
              >
                {compact
                  ? tr(`站点履历（共 ${sites.length} 个）`, `Site history (${sites.length} sites)`)
                  : ""}
              </summary>
              <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-mast-muted">
                    <th className="py-1 pr-3 font-normal">{tr("站点", "Site")}</th>
                    <th className="py-1 pr-3 font-normal">{tr("位置（步）", "Position (steps)")}</th>
                    <th className="py-1 pr-3 font-normal">{tr("扫描", "Scans")}</th>
                    <th className="py-1 pr-3 font-normal">STS</th>
                    <th className="py-1 pr-3 font-normal">{tr("破坏", "Damage")}</th>
                    <th className="py-1 font-normal">{tr("时间", "Time")}</th>
                  </tr>
                </thead>
                <tbody>
                  {sites.map((s) => (
                    <tr
                      key={s.index}
                      className={
                        "border-t border-mast-border " +
                        (s.is_current ? "bg-mast-accent-soft" : "")
                      }
                    >
                      <td className="py-1 pr-3 font-mono text-mast-text">
                        #{s.index}
                        {s.is_current ? tr(" ·当前", " · current") : ""}
                        {s.position_known ? "" : tr(" ·位置未知", " · position unknown")}
                      </td>
                      <td className="py-1 pr-3 font-mono tabular-nums text-mast-muted">
                        {s.x_steps}, {s.y_steps}
                        {s.uncertainty_steps > 0
                          ? ` ±${Math.round(s.uncertainty_steps)}`
                          : ""}
                      </td>
                      <td className="py-1 pr-3 tabular-nums">{s.summary?.scans ?? 0}</td>
                      <td className="py-1 pr-3 tabular-nums">{s.summary?.sts ?? 0}</td>
                      <td
                        className={
                          "py-1 pr-3 tabular-nums " +
                          (s.summary?.damage ? "text-mast-warn" : "")
                        }
                      >
                        {s.summary?.damage ?? 0}
                      </td>
                      <td className="py-1 tabular-nums text-mast-muted">
                        {tsRange(s.first_ts, s.last_ts)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            </details>
          )}

          <div
            className={`rounded border px-2 py-1.5 text-xs ${
              data.suggestion
                ? "border-mast-border bg-mast-panel"
                : "border-mast-warn/40 bg-mast-warn/10"
            }`}
          >
            {data.suggestion ? (
              <>
                <div className="font-semibold text-mast-text">
                  {tr("建议：", "Suggestion: ")}
                  {data.suggestion.direction} × {fmtSteps(data.suggestion.steps)} {tr("步", "steps")}
                </div>
                <div className="mt-0.5 text-mast-muted">{data.suggestion.reason}</div>
              </>
            ) : (
              <>
                <div className="font-semibold text-mast-warn">
                  {tr("无可用落点", "No usable landing point")}
                </div>
                <div className="mt-0.5 text-mast-muted">{data.note || "—"}</div>
              </>
            )}
          </div>

          {/* Two gates: a good landing point on the map says nothing about the interlock refusing. */}
          <div
            className={`rounded border px-2 py-1.5 text-xs ${
              data.vacuum_allow
                ? "border-mast-border bg-mast-panel text-mast-muted"
                : "border-mast-warn/40 bg-mast-warn/10 text-mast-warn"
            }`}
          >
            {data.vacuum_reason || tr("真空互锁状态未知", "Vacuum interlock state unknown")}
          </div>
          {!data.coarse_drive_declared && (
            <div className="rounded border border-mast-warn/40 bg-mast-warn/10 px-2 py-1.5 text-xs text-mast-warn">
              {data.coarse_drive_note}
            </div>
          )}

          <CoarseSelfCheck />
        </>
        );
      })()}
    </Card>
  );
}

// Acceptance self-check. One read-only probe serves both audiences (agents via a
// skill, operators via this button), so a conclusion can be checked before it is
// trusted.
//
// Triggered on demand and never polled: it queries the instrument every time, and
// it answers whether the assembly is correct, which does not change within a minute.
function CoarseSelfCheck() {
  const [open, setOpen] = useState(false);
  const q = useQuery({
    queryKey: ["coarse-map", "selfcheck"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/coarse-map/selfcheck");
      if (error) throw error;
      return data as {
        ready?: boolean;
        summary?: string;
        blocking?: string[];
        todo?: string[];
        degraded?: boolean;
        detail?: string;
      };
    },
    enabled: open,
    refetchOnWindowFocus: false,
  });

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="text-xs text-mast-muted underline hover:text-mast-text"
      >
        {tr("运行粗动自检（只读，进针/扫描中也可以跑）…", "Run coarse self-check (read-only; safe during approach or scanning)…")}
      </button>
    );
  }

  const d = q.data;
  return (
    <div className="space-y-2 rounded border border-mast-border bg-mast-panel p-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold">{tr("粗动自检", "Coarse self-check")}</span>
        <button
          type="button"
          onClick={() => void q.refetch()}
          disabled={q.isFetching}
          className="rounded border border-mast-border bg-mast-bg px-2 py-0.5 text-[11px] text-mast-muted hover:text-mast-text disabled:opacity-50"
        >
          {q.isFetching ? tr("检查中…", "Checking…") : tr("重跑", "Re-run")}
        </button>
      </div>
      {q.isPending && <Spinner />}
      {q.isError && <ErrorNote error={q.error} />}
      {d?.degraded && (
        <p className="text-xs text-mast-warn">{d.detail || tr("内核未就绪", "Core not ready")}</p>
      )}
      {d && !d.degraded && (
        <>
          <p className={`text-xs font-semibold ${d.ready ? "text-mast-accent" : "text-mast-warn"}`}>
            {d.summary}
          </p>
          {!!d.blocking?.length && (
            <ul className="space-y-1 text-xs text-mast-warn">
              {d.blocking.map((b, i) => (
                <li key={i}>· {b}</li>
              ))}
            </ul>
          )}
          {!!d.todo?.length && (
            <details className="text-xs text-mast-muted">
              <summary className="cursor-pointer">
                {tr(
                  `还有 ${d.todo.length} 项只有真机能回答`,
                  `${d.todo.length} more items only the real instrument can answer`,
                )}
              </summary>
              <ul className="mt-1 space-y-1">
                {d.todo.map((t, i) => (
                  <li key={i}>· {t}</li>
                ))}
              </ul>
            </details>
          )}
        </>
      )}
    </div>
  );
}

/**
 * The span "from when to when work was done at this site", as one cell.
 *
 * With a single timestamp no range is invented: a site may have just one record,
 * and "X ~ X" would suggest time spent there. With neither, show a dash.
 */
function tsRange(first?: string | null, last?: string | null): string {
  const a = short(first);
  const b = short(last);
  if (!a && !b) return "—";
  if (!a) return b;
  if (!b || a === b) return a;
  return `${a} ~ ${b}`;
}

function short(ts?: string | null): string {
  if (!ts) return "";
  // "2026-08-05T13:42:11" → "08-05 13:42"
  return ts.replace("T", " ").slice(5, 16);
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4">
      <span className="text-mast-muted">{label}</span>
      <span className="tabular-nums text-right">{value}</span>
    </div>
  );
}
