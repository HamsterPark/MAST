import { useMemo } from "react";
import UplotReact from "uplot-react";
import type uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import type { components } from "@/api/schema";
import { useUiStore } from "@/store";
import { tr } from "@/lib/i18n";
import { Badge, EmptyNote } from "@/components/ui";
import { fmtCurrent, fmtLength } from "@/lib/units";
import {
  AUX_CHANNELS,
  AUX_WINDOWS,
  type AuxPlotScale,
  auxAmpSamplingNote,
  auxLockinSplit,
  auxSeries,
  auxVerdictView,
} from "@/lib/monitoring";
import { useElementWidth, useViewportHeight } from "@/hooks/useElementWidth";
import { SMALL_MULTIPLE_RATIO, sizeChart } from "@/lib/chartSize";

type AuxSnapshot = components["schemas"]["AuxSnapshot"];
type AuxChannelState = components["schemas"]["AuxChannelState"];
type AuxSeriesResponse = components["schemas"]["AuxSeriesResponse"];

// Auxiliary channels — Z position / qPlus amplitude / frequency shift.
//
// ── why these are not on the current chart ─────────────────────────────────
// Metres, metres and hertz do not share an axis with amperes, and they do not
// share one with each other either. Overlaying them needs two or three Y axes,
// and where two curves sit relative to one another on separate axes is a choice
// the person drawing made — it invites the reader to see a correlation nobody
// measured. Small multiples on ONE time axis show the same data and make no
// such claim.
//
// ── why the numbers arrive at 1 Hz and not 2 kHz ───────────────────────────
// The daemon samples these over `Signals_ValsGet` once per segment and never
// touches the oscilloscope, so the current channel keeps its whole acquisition
// cadence. That is a physics decision as much as a plumbing one: a high-Q qPlus
// amplitude cannot move faster than Q/(π f₀) — hundreds of milliseconds — and Z
// drift is a minutes-scale phenomenon. See docs/v2/design/monitoring_aux_channels.md.
//
// ── what must never happen ─────────────────────────────────────────────────
// A channel this rig does not have must read "not present", not "normal". A channel
// recorded but not judged must read "not graded", not "normal". Green here would be a
// claim that a judgement happened.

/** Width used only for the first frame, before the ResizeObserver has measured
 *  the container. uPlot given width 0 never recovers, so it needs a real number. */
const FALLBACK_W = 860;

function token(name: string): string {
  if (typeof window === "undefined") return "";
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Value in the channel's own unit, at a scale a human reads. */
function fmtAux(v: number | null | undefined, unit: string): string {
  if (v == null || !Number.isFinite(v)) return "—";
  if (unit === "m") return fmtLength(v);
  if (unit === "A") return fmtCurrent(v);
  if (unit === "Hz") return `${v.toPrecision(4)} Hz`;
  return v.toPrecision(4);
}

/** Drift in the unit an operator thinks in: nm per minute, not m per second. */
function fmtDrift(v: number | null | undefined): string | null {
  if (v == null || !Number.isFinite(v)) return null;
  const nmPerMin = v * 1e9 * 60;
  return `${nmPerMin >= 0 ? "+" : ""}${nmPerMin.toFixed(2)} nm/min`;
}

/**
 * Whether the lock-in modulation is on, shown right next to the reading.
 *
 * Three states, not collapsed to two: `undefined` means ``LockIn_ModOnOffGet``
 * could not be read this tick. Showing that as "off" would make a genuine dI/dV
 * look like a noise floor. The backend likewise stores it as NULL rather than 0
 * (``store`` schema v6).
 */
function LockinState({ mod }: { mod: number | undefined }) {
  if (mod === 1) {
    return (
      <div className="mt-0.5 text-xs text-mast-muted">
        {tr("调制开 · 这是 dI/dV", "Modulation on · this is dI/dV")}
      </div>
    );
  }
  if (mod === 0) {
    return (
      <div className="mt-0.5 text-xs text-mast-warn">
        {tr("调制关 · 此读数不是 dI/dV", "Modulation off · this reading is not dI/dV")}
      </div>
    );
  }
  return (
    <div className="mt-0.5 text-xs text-mast-muted">
      {tr("调制状态未读到（不等于关闭）", "Modulation state not read (not the same as off)")}
    </div>
  );
}

function ChannelTile({ ch, hint }: { ch: AuxChannelState | undefined; hint: string }) {
  const view = auxVerdictView(ch?.verdict);
  const drift = fmtDrift(ch?.metrics?.z_drift_m_per_s);
  const frac = ch?.metrics?.amp_frac_of_baseline;
  return (
    <div className="rounded-mast-card border border-mast-border bg-mast-panel p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-mast-muted">{ch?.label_zh || "—"}</span>
        <Badge tone={view.tone}>{view.label}</Badge>
      </div>
      <div className="mt-1 font-mono text-lg text-mast-text">
        {fmtAux(ch?.value, ch?.unit ?? "")}
      </div>
      {drift && (
        <div className="mt-0.5 font-mono text-xs text-mast-muted">{tr("漂移", "Drift")} {drift}</div>
      )}
      {/* The "baseline" is the non-contact noise floor, not a healthy driven amplitude:
          the qPlus sensor is not driven on the instrument, so it is the undriven demodulator's
          noise floor. Its only use is showing what "not zero" looks like, hence the label. */}
      {typeof frac === "number" && (
        <div className="mt-0.5 font-mono text-xs text-mast-muted">
          {tr(
            `未接触本底的 ${(frac * 100).toFixed(1)}%`,
            `${(frac * 100).toFixed(1)}% of the non-contact floor`,
          )}
        </div>
      )}
      {/* With modulation off the dI/dV channel still shows a plausible pA value that is not dI/dV,
          so the state sits right next to the number instead of only in the note below. */}
      {ch?.kind === "lockin" && <LockinState mod={ch?.metrics?.lockin_mod_on} />}
      <div className="mt-1 text-[11px] leading-snug text-mast-faint">
        {ch?.note || hint}
      </div>
    </div>
  );
}

/** One plotted curve: values plus line style. A second trace separates the segments that are not measurements. */
interface Trace {
  label: string;
  /** `null` = this point does not belong to the curve (or there was no reading at that moment). */
  v: (number | null)[];
  dashed?: boolean;
  muted?: boolean;
}

function MiniChart({
  label,
  plot,
  t,
  traces,
  n,
  gaps,
  footer,
}: {
  label: string;
  /** Fixed Y-axis prefix declared by the channel in `AUX_CHANNELS`. Not derived from the unit,
   *  which would force Z and qPlus amplitude (both metres, four decades apart) to share a prefix. */
  plot: AuxPlotScale;
  t: number[];
  /** One or more curves sharing `t`. A `null` must never be drawn as 0. */
  traces: Trace[];
  /** Number of real readings (excluding nulls inserted to break the line). */
  n: number;
  /** Number of acquisition gaps in the window. */
  gaps: number;
  /** Extra line below the chart (e.g. the lock-in modulation-state breakdown). */
  footer?: React.ReactNode;
}) {
  const theme = useUiStore((s) => s.theme);
  const [boxRef, measuredW] = useElementWidth<HTMLDivElement>();
  const viewportH = useViewportHeight();
  const { width, height } = sizeChart(measuredW, FALLBACK_W, {
    ratio: SMALL_MULTIPLE_RATIO,
    minHeight: 96,
    maxHeight: 200,
    viewportH,
  });

  const scale = plot.mul;
  const axisLabel = `${label} (${plot.suffix})`;

  const data = useMemo<uPlot.AlignedData>(
    () =>
      [
        t,
        ...traces.map((x) => x.v.map((y) => (y == null ? null : y * scale))),
      ] as uPlot.AlignedData,
    [t, traces, scale],
  );

  const shape = traces.map((x) => `${x.label}|${x.dashed}|${x.muted}`).join(",");
  const options = useMemo<uPlot.Options>(() => {
    const axisStroke = token("--mast-muted") || "#94a3b8";
    const gridStroke = token("--mast-border") || "#1e293b";
    const stroke = token("--mast-accent") || "#28d0e6";
    return {
      width,
      height,
      scales: { x: { time: true }, y: {} },
      axes: [
        { stroke: axisStroke, grid: { stroke: gridStroke } },
        {
          stroke: axisStroke,
          grid: { stroke: gridStroke },
          label: axisLabel,
          labelGap: 4,
          size: 62,
        },
      ],
      series: [
        { label: tr("时间", "Time") },
        ...traces.map((x) => ({
          label: x.label,
          // Non-measurement segments change both colour and line style: colour alone would
          // depend on token contrast in both themes, while a dashed line holds in either
          // theme and in print.
          stroke: x.muted ? axisStroke : stroke,
          width: x.muted ? 1 : 1.2,
          ...(x.dashed ? { dash: [4, 3] } : {}),
        })),
      ],
      legend: { show: false },
      cursor: { drag: { x: true, y: false } },
    };
  }, [theme, axisLabel, shape, width, height]);

  // Test the number of real readings, not t.length: nulls inserted to break the line also
  // occupy a timestamp, so the array length would report an all-gap curve as having data.
  if (!n) {
    return (
      <div className="rounded-lg border border-mast-border bg-mast-bg p-3 text-xs text-mast-muted">
        {label}{tr("：窗口内还没有采到数据。", ": no data collected in this window yet.")}
      </div>
    );
  }
  return (
    <div
      ref={boxRef}
      className="overflow-x-auto rounded-lg border border-mast-border bg-mast-bg p-2"
    >
      <UplotReact options={options} data={data} />
      {footer}
      {gaps > 0 && (
        // State it explicitly, otherwise a broken curve looks like a rendering fault.
        <div className="px-1 pt-1 text-[11px] text-mast-faint">
          {tr(
            `曲线断开 ${gaps} 处 = 那几段时间没有采到数据（守护停过 / 服务重启 / 窗口早于开始记录的时刻），不是读数为零。`,
            `The curve breaks at ${gaps} place(s) = no data was collected then (daemon stopped / service restarted / window starts before recording began); the reading is not zero.`,
          )}
        </div>
      )}
    </div>
  );
}

/**
 * One line under the dI/dV chart: what the dashed segments are, and how many
 * points fall in each of the three states in the window.
 *
 * The chart has only two line styles (modulation confirmed on vs the rest), since
 * "off" and "not read" both mean "do not read this as dI/dV". The follow-up
 * differs (off is a knob someone turned; not read is a link to investigate), so
 * the counts are reported separately. "Not read" is never written as "off",
 * which would make a genuine dI/dV look like a noise floor.
 */
function LockinLegend({ nOn, nOff, nUnknown }: {
  nOn: number; nOff: number; nUnknown: number;
}) {
  if (!nOn && !nOff && !nUnknown) return null;
  return (
    <div className="px-1 pt-1 text-[11px] leading-snug text-mast-faint">
      <span className="text-mast-muted">
        {tr("灰色虚线段＝未确认在调制，不是 dI/dV", "Gray dashed segments = modulation not confirmed, not dI/dV")}
      </span>
      {tr("（解调器的噪声与串扰底）。本窗口：调制开 ", " (demodulator noise and crosstalk floor). This window: modulation on ")}
      {nOn}
      {tr(" 点", " pts")}
      {nOff > 0 && <> · {tr(`调制关 ${nOff} 点`, `modulation off ${nOff} pts`)}</>}
      {nUnknown > 0 && (
        <> · {tr(`调制状态未读到 ${nUnknown} 点（不等于关闭）`, `modulation state not read ${nUnknown} pts (not the same as off)`)}</>
      )}
      {tr("。", ".")}
    </div>
  );
}

/** Short-term / long-term switcher for the charts below. Deliberately not labelled
 *  "statistics window": it changes how far back the small multiples are drawn and
 *  nothing else. The verdict tiles keep using the daemon's own window
 *  (`aux.window_s`), a threshold-calibration parameter that lives in Settings. */
function WindowPicker({
  windowS,
  onWindowChange,
}: {
  windowS: number;
  onWindowChange: (s: number) => void;
}) {
  return (
    <div className="inline-flex overflow-hidden rounded-mast-ctl border border-mast-border">
      {AUX_WINDOWS.map((w) => (
        <button
          key={w.s}
          type="button"
          aria-pressed={windowS === w.s}
          onClick={() => onWindowChange(w.s)}
          className={
            "px-2.5 py-1 text-xs " +
            (windowS === w.s
              ? "bg-mast-accent-soft font-medium text-mast-accent"
              : "text-mast-muted hover:text-mast-text")
          }
        >
          {w.label}
        </button>
      ))}
    </div>
  );
}

export function AuxChannels({
  aux,
  series,
  degraded,
  windowS,
  onWindowChange,
}: {
  aux: AuxSnapshot | null | undefined;
  series: AuxSeriesResponse | undefined;
  degraded: boolean;
  windowS: number;
  onWindowChange: (s: number) => void;
}) {
  if (degraded) {
    return <EmptyNote label={tr("监控模块未装载，无辅助通道。", "Monitoring module not loaded; no auxiliary channels.")} />;
  }
  if (!aux) {
    // Not the same as "this rig has no Z channel" — say which one it is.
    return (
      <EmptyNote
        label={tr(
          "采集守护未运行，辅助通道没有在采样。历史数据照常可浏览。",
          "The acquisition daemon is not running, so auxiliary channels are not being sampled. History can still be browsed.",
        )}
      />
    );
  }

  const byKind = new Map<string, AuxChannelState>();
  for (const c of aux.channels ?? []) byKind.set(c.kind, c);
  const available = AUX_CHANNELS.filter((c) => byKind.get(c.kind)?.available);
  // Judge "fast enough" against the observed cadence, not the configured value:
  // sampling is opportunistic and the setting is only a throttle ceiling, so the
  // two differ (e.g. configured 1.0 s, observed 1.33 s).
  const observed = aux.observed_interval_s ?? null;
  const sampling = auxAmpSamplingNote(aux.amp_tau_s, observed ?? aux.interval_s);

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {AUX_CHANNELS.map((c) => (
          <ChannelTile key={c.kind} ch={byKind.get(c.kind)} hint={c.hint} />
        ))}
      </div>

      <p className="text-xs leading-relaxed text-mast-muted">
        {/* Report both the configured and the observed interval: showing only the configured
            value would keep displaying a tidy number while sampling is actually slower.
            When the observed cadence cannot be measured (too few points), say so. */}
        {tr(`设定每 ${aux.interval_s.toFixed(2)} s 采一次`, `Configured to sample every ${aux.interval_s.toFixed(2)} s`)}
        {observed != null
          ? tr(`，实测 ${observed.toFixed(2)} s`, `, observed ${observed.toFixed(2)} s`)
          : tr("（实测节奏样本还不够，暂时给不出）", " (not enough samples yet to measure the observed cadence)")}
        {tr("，", ", ")}
        <strong>{tr("不占用示波器", "does not occupy the oscilloscope")}</strong>
        {tr(
          "——它搭在电流泵等缓冲的空隙里，与采集主环、急停看门狗都不共用锁。判级窗口 ",
          ": it rides in the gaps while the current pump waits for buffers and shares no lock with the main acquisition loop or the emergency-stop watchdog. Grading window ",
        )}
        {Math.round(aux.window_s)} s{tr("，已采 ", ", sampled ")}
        {aux.sampled}
        {tr(" 次", " times")}
        {aux.skipped_busy > 0 &&
          tr(`，因通道被占跳过 ${aux.skipped_busy} 次`, `, skipped ${aux.skipped_busy} time(s) because the channel was busy`)}
        {tr("。", ".")}
        {sampling && <> {sampling}{tr("。", ".")}</>}
      </p>

      {!aux.alerts_enabled && (
        <p className="rounded-mast-card border border-mast-warn-border bg-mast-warn-bg p-2 text-xs leading-relaxed text-mast-warn">
          {tr("辅助通道的", "Auxiliary-channel alerts are ")}
          <strong>{tr("告警当前是关的", "currently off")}</strong>
          {tr(
            "——出厂状态，不是故障。Z 与振幅的阈值从未在本机标定过，先跑",
            ": this is the factory state, not a fault. The Z and amplitude thresholds have never been calibrated on this machine; first run",
          )}
          <code className="mx-1">python -m mast.monitoring.commission</code>
          {tr("看【辅助通道】那一段，再到「设置 → 电流监控」把", "and read the [Auxiliary channels] section, then in Settings → Current monitor turn on")}
          <code className="mx-1">cm_aux_alerts_enabled</code>
          {tr("打开。记录一直在进行。", ". Recording continues regardless.")}
        </p>
      )}

      {available.length === 0 ? (
        // "we have not looked yet" and "this rig does not have one" get
        // different sentences. The second makes someone stop investigating, so
        // it must not be shown before the signal table has actually been read.
        <EmptyNote
          label={
            (aux.channels ?? []).some((c) => c.verdict === "unknown")
              ? tr(
                  `还没读到信号表（${aux.detail || "探测中"}）——尚未判定这台机器有没有这些通道。`,
                  `Signal table not read yet (${aux.detail || "probing"}); it is not yet known whether this machine has these channels.`,
                )
              : tr(
                  "这台机器的信号表里没有 Z / qPlus 振幅通道——不是故障，是本机没有这些信号。",
                  "This machine's signal table has no Z / qPlus amplitude channels. This is not a fault; the machine simply lacks these signals.",
                )
          }
        />
      ) : (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <WindowPicker windowS={windowS} onWindowChange={onWindowChange} />
            <span className="text-xs text-mast-faint">
              {tr(
                `图的时间跨度。判级仍按守护自己的 ${Math.round(aux.window_s)} s 窗口。`,
                `Chart time span. Grading still uses the daemon's own ${Math.round(aux.window_s)} s window.`,
              )}
            </span>
          </div>
          {available.map((c) => {
            const label = byKind.get(c.kind)?.label_zh || c.kind;
            // The lock-in is the only channel whose curve is sometimes a measurement and
            // sometimes not, so only it is split in two; splitting others would add an empty series.
            if (c.kind === "lockin") {
              const s = auxLockinSplit(series);
              return (
                <MiniChart
                  key={c.kind}
                  label={label}
                  plot={c.plot}
                  t={s.t}
                  n={s.n}
                  gaps={s.gaps}
                  traces={[
                    { label: tr("调制开（dI/dV）", "Modulation on (dI/dV)"), v: s.on },
                    { label: tr("未确认在调制", "Modulation not confirmed"), v: s.off, dashed: true, muted: true },
                  ]}
                  footer={<LockinLegend {...s} />}
                />
              );
            }
            const { t, v, n, gaps } = auxSeries(series, c.series);
            return (
              <MiniChart
                key={c.kind}
                label={label}
                plot={c.plot}
                t={t}
                n={n}
                gaps={gaps}
                traces={[{ label, v }]}
              />
            );
          })}
          <div className="text-xs text-mast-muted">
            {tr("共用同一条时间轴。", "They share one time axis. ")}
            <strong>{tr("刻意不叠在一起", "Deliberately not overlaid")}</strong>
            {tr(
              "：米、米、赫兹不通约，叠加要用双 Y 轴，而两条曲线在各自轴上的相对高低是画图人定的，会诱导出没人测过的相关性。",
              ": metres, metres and hertz are incommensurable; overlaying needs dual Y axes, and the relative heights of curves on separate axes are the plotter's choice, which suggests correlations nobody measured.",
            )}
          </div>
        </div>
      )}
    </div>
  );
}
