import { tr } from "@/lib/i18n";
import {
  SLIDER_STEPS,
  TIME_PRESETS,
  presetOf,
  rangeFromSlider,
  timeToSlider,
  windowLabel,
  type TimeDomain,
  type TimeFilter,
  type TimeSummary,
  type TimeWindow,
} from "@/lib/scanMapTime";

/**
 * The scan map's time window: quick chips plus a two-handle slider over the
 * recorded times, and a plain statement of what the window holds.
 *
 * Only history is filtered (recorded operations and saved images). The route,
 * the live frame, the tip and the analysis overlays describe NOW and are always
 * drawn — a time filter that hid the tip would hide the one thing that is
 * certainly current.
 *
 * The decisions themselves (what "last 1 h" means after a clock step, what an
 * undated marker does) live in lib/scanMapTime.ts with their tests; this only
 * draws them.
 */
export function ScanMapTimeBar({
  filter,
  onChange,
  timeWindow,
  domain,
  summary,
  now,
}: {
  filter: TimeFilter;
  onChange: (f: TimeFilter) => void;
  timeWindow: TimeWindow;
  domain: TimeDomain | null;
  summary: TimeSummary;
  now: number;
}) {
  const active = presetOf(filter);
  const usable = !!domain && domain.max > domain.min;
  const lo = usable ? timeToSlider(timeWindow.from, domain!, "from") : 0;
  const hi = usable ? timeToSlider(timeWindow.to, domain!, "to") : SLIDER_STEPS;
  const empty = summary.total > 0 && summary.shown === 0;

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs">
      <span className="text-mast-muted">{tr("时间", "Time")}</span>
      <div
        role="radiogroup"
        aria-label={tr("时间范围", "Time range")}
        className="inline-flex flex-wrap gap-[3px] rounded-mast-ctl border border-mast-border bg-mast-panel-2 p-[3px]"
      >
        {TIME_PRESETS.map((p) => (
          <button
            key={p.id}
            type="button"
            role="radio"
            aria-checked={active === p.id}
            onClick={() => onChange(p.filter)}
            className={
              "rounded-md px-2.5 py-1 transition-colors " +
              (active === p.id
                ? "bg-mast-accent font-semibold text-mast-accent-ink"
                : "text-mast-muted hover:text-mast-text")
            }
          >
            {p.label}
          </button>
        ))}
      </div>
      <DualRange
        lo={lo}
        hi={hi}
        disabled={!usable}
        onChange={(a, b) => {
          if (usable) onChange(rangeFromSlider(a, b, domain!));
        }}
      />
      <span className="tabular-nums text-mast-text">{windowLabel(timeWindow, domain, now)}</span>
      <span className="tabular-nums text-mast-muted">
        {tr(`${summary.shown}/${summary.total} 个标记`, `${summary.shown}/${summary.total} markers`)}
      </span>
      {summary.undated > 0 && (
        <span
          className="text-mast-muted"
          title={tr(
            "这些标记的时间缺失或无法解析；它们不会被时间筛选隐藏。",
            "These markers have a missing or unreadable time; the time filter never hides them.",
          )}
        >
          {tr(
            `${summary.undated} 个标记没有时间（照常显示）`,
            `${summary.undated} markers have no time (always shown)`,
          )}
        </span>
      )}
      {empty && (
        <span className="text-mast-warn">
          {tr("这段时间里没有标记。", "No markers in this window.")}{" "}
          <button type="button" className="underline hover:text-mast-text" onClick={() => onChange({ kind: "all" })}>
            {tr("显示全部", "Show all")}
          </button>
        </span>
      )}
    </div>
  );
}

// Two native range inputs on one track: keyboard-operable as they are, and
// no new dependency. The inputs ignore the pointer except on their thumbs, so
// either thumb can be grabbed where they overlap. `border-solid` on the thumb
// is not optional: Tailwind's preflight sets the border style on `*`, which
// does not reach the ::-webkit-slider-thumb pseudo-element, so without it the
// accent border is never drawn and a white thumb vanishes on a light page.
const THUMB =
  "pointer-events-none absolute inset-x-0 top-1/2 m-0 h-3.5 w-full -translate-y-1/2 cursor-pointer appearance-none bg-transparent " +
  "disabled:cursor-not-allowed " +
  "[&::-webkit-slider-runnable-track]:h-3.5 [&::-webkit-slider-runnable-track]:bg-transparent " +
  "[&::-webkit-slider-thumb]:pointer-events-auto [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:w-3.5 " +
  "[&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full " +
  "[&::-webkit-slider-thumb]:border-2 [&::-webkit-slider-thumb]:border-solid [&::-webkit-slider-thumb]:border-mast-accent [&::-webkit-slider-thumb]:bg-mast-panel " +
  "[&::-moz-range-thumb]:pointer-events-auto [&::-moz-range-thumb]:h-3 [&::-moz-range-thumb]:w-3 " +
  "[&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-2 [&::-moz-range-thumb]:border-solid [&::-moz-range-thumb]:border-mast-accent " +
  "[&::-moz-range-thumb]:bg-mast-panel [&::-moz-range-track]:bg-transparent";

function DualRange({
  lo,
  hi,
  disabled,
  onChange,
}: {
  lo: number;
  hi: number;
  disabled: boolean;
  onChange: (lo: number, hi: number) => void;
}) {
  return (
    <div
      className={"relative h-5 w-44 " + (disabled ? "opacity-40" : "")}
      title={
        disabled
          ? tr("标记的时间不够两个，没有可拖的范围", "Fewer than two distinct marker times; nothing to drag over")
          : tr("拖两端选时间窗；拖到头 = 不设限", "Drag the ends to pick a window; an end at the stop is open")
      }
    >
      <div className="absolute inset-x-0 top-1/2 h-1 -translate-y-1/2 rounded-full bg-mast-border" />
      <div
        className="absolute top-1/2 h-1 -translate-y-1/2 rounded-full bg-mast-accent"
        style={{ left: `${(lo / SLIDER_STEPS) * 100}%`, right: `${100 - (hi / SLIDER_STEPS) * 100}%` }}
      />
      <input
        type="range"
        min={0}
        max={SLIDER_STEPS}
        step={1}
        value={lo}
        disabled={disabled}
        aria-label={tr("时间窗起点", "Window start")}
        onChange={(e) => onChange(Math.min(Number(e.target.value), hi), hi)}
        className={THUMB}
        // Both thumbs parked at the right stop: the start thumb must be on top,
        // or it could never be dragged back out.
        style={{ zIndex: lo >= SLIDER_STEPS - 5 ? 3 : 2 }}
      />
      <input
        type="range"
        min={0}
        max={SLIDER_STEPS}
        step={1}
        value={hi}
        disabled={disabled}
        aria-label={tr("时间窗终点", "Window end")}
        onChange={(e) => onChange(lo, Math.max(Number(e.target.value), lo))}
        className={THUMB}
        style={{ zIndex: 2 }}
      />
    </div>
  );
}
