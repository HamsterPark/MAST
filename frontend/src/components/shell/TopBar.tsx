import { useCallback, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import type { components } from "@/api/schema";
import { api } from "@/api/client";
import { useCurrentScope, useSwitchExperiment } from "@/api/scope";
import { ExperimentPicker } from "@/components/scope/ScopePickers";
import { useAutonomyMode } from "@/hooks/useAutonomyMode";
import { useWsConnection, useWsEvent } from "@/hooks/useWsEvents";
import { splitSI } from "@/lib/units";
import {
  LIVE_READINGS_POLL_MS,
  LIVE_READINGS_STALE_AFTER_MS,
  LIVE_READINGS_WS_KEEPALIVE_POLL_MS,
} from "@/lib/pollRates";
import { useUiStore } from "@/store";
import { langTag, tr } from "@/lib/i18n";
import { LanguageSwitch } from "@/components/shell/LanguageSwitch";
import { openOnboardingGuide } from "@/components/shell/OnboardingGuide";
import { useLegacyFeatures } from "@/hooks/useLegacyFeatures";

// Top header bar: logo + product name, inline live hardware readings
// (B / I / Z / setpoint), and right-aligned connection, model and clock.
//
// ── Live readings: WebSocket push, HTTP poll underneath ────────────────────
// This is the first consumer of the shared /ws/events socket (lib/ws.ts) and the
// reference for the rest.
//
// The poll is not removed, only de-rated. When push is live we fall back from
// 500 ms to a slow keep-alive poll instead of stopping outright, because the
// window between "socket silently died" and "idle watchdog noticed" is up to
// WS_IDLE_MS (45 s), and 45 s of numbers that are frozen but look live must never
// happen (the UI must not appear live while frozen). The keep-alive is cheap: the
// endpoint is a cached dict read with no TCP on the request path (see
// lib/pollRates.ts), and it self-heals any event the socket missed.
//
// Both sources write the same react-query cache entry, so there is one timeline
// and nothing to arbitrate; see the note on the push handler for why keeping the
// pushed value beside the cache does not work.

function useClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}

function fmt(v: number | null | undefined, digits: number, suffix = ""): string {
  return v == null ? "—" : `${v.toFixed(digits)}${suffix}`;
}

// Global operating-mode segmented control — safe (green) / semi (amber) / auto
// (none). Mirrors the belief + tip-processing gate wired on the backend
// (autonomy_mode). The active tint uses the semantic auto/warn/accent tokens;
// the whole-app colored border lives in AppLayout.
const MODES = ["safe", "semi", "auto"] as const;

// Open-loop coarse Z approach toward the sample (MotorMove z-approach) is blocked by
// SafetyGate layer 0 in all three modes, before the mode gate is consulted. The
// tooltips state this so that no mode suggests it will drive the coarse motor.
const COARSE_NOTE = tr(
  "开环粗逼近（MotorMove z-approach）在三种模式下一律禁止，须用户手动执行",
  "Open-loop coarse approach (MotorMove z-approach) is blocked in all three modes and must be performed manually by the user.",
);

type LiveReadings = components["schemas"]["LiveReadings"];
type LiveReadingsResponse = components["schemas"]["LiveReadingsResponse"];

/** Shared by TopBar + RightPanel + DashboardPage — one cache entry, one fetch.
 *  A WS push writes here too, so all three see it without changing. */
const LIVE_READINGS_KEY = ["hardware", "live-readings"] as const;

/**
 * Pull a readings object out of a `hardware_state` event payload.
 *
 * Accepts BOTH `{...fields}` and `{readings: {...fields}}` because the event's
 * envelope is the backend's to choose and this component must not break the top
 * bar over a wrapper key. Anything unrecognised returns null and the poll simply
 * stays authoritative — an unknown push shape degrades to the old behaviour
 * rather than blanking the readout.
 */
function readingsFromEvent(data: unknown): LiveReadings | null {
  if (typeof data !== "object" || data === null) return null;
  const d = data as Record<string, unknown>;
  const inner =
    typeof d.readings === "object" && d.readings !== null
      ? (d.readings as Record<string, unknown>)
      : d;
  // Require at least one field we actually render, so an unrelated payload that
  // happens to be an object can't blank the bar.
  const KNOWN = ["bias_v", "current_a", "z_m", "setpoint_a", "z_controller_on", "scan_running"];
  if (!KNOWN.some((k) => k in inner)) return null;
  return inner as LiveReadings;
}

const MODE_SEG: Record<
  (typeof MODES)[number],
  { label: string; title: string; active: string; dot: string }
> = {
  safe: {
    label: tr("安全", "Safe"),
    title: tr(
      `安全模式：视针尖良好，不修针、不电脉冲，专注实验。${COARSE_NOTE}`,
      `Safe mode: the tip is assumed good; no tip conditioning, no electrical pulses, just run the experiment. ${COARSE_NOTE}`,
    ),
    active: "bg-mast-auto-bg text-mast-auto",
    dot: "var(--mast-auto)",
  },
  semi: {
    label: tr("半自动", "Semi"),
    title: tr(
      `半自动模式：允许浅层机械修针；电脉冲需人工确认。${COARSE_NOTE}`,
      `Semi-automatic mode: shallow mechanical tip conditioning is allowed; electrical pulses need manual confirmation. ${COARSE_NOTE}`,
    ),
    active: "bg-mast-warn-bg text-mast-warn",
    dot: "var(--mast-warn)",
  },
  auto: {
    label: tr("自动", "Auto"),
    title: tr(
      `自动模式：修针与电脉冲均自动执行，不再逐条确认。${COARSE_NOTE}`,
      `Automatic mode: tip conditioning and electrical pulses run automatically without per-step confirmation. ${COARSE_NOTE}`,
    ),
    active: "bg-mast-accent-soft text-mast-accent",
    dot: "var(--mast-accent)",
  },
};

export function TopBar() {
  const theme = useUiStore((s) => s.theme);
  const toggleTheme = useUiStore((s) => s.toggleTheme);
  const { mode: autonomyMode, setMode: setAutonomyMode } = useAutonomyMode();
  const now = useClock();

  // Hardware emergency stop: the one-click safety action (abort run + stop motion +
  // retract tip). Always visible in the top bar.
  const [estopMsg, setEstopMsg] = useState<string>("");
  const estop = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/safety/emergency-stop");
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      const r = d as { retracted?: boolean; degraded?: boolean } | undefined;
      setEstopMsg(
        r?.degraded
          ? tr("急停：未连接硬件", "E-stop: hardware not connected")
          : r?.retracted
            ? tr("已急停并退针", "E-stopped and tip retracted")
            : tr("已急停", "E-stopped"),
      );
      setTimeout(() => setEstopMsg(""), 4000);
    },
    onError: () => {
      setEstopMsg(tr("急停请求失败", "E-stop request failed"));
      setTimeout(() => setEstopMsg(""), 4000);
    },
  });

  // Abort-latch state. Polled rather than discovered only on error: a latched state
  // with no UI trace shows up only as every instrument call being refused.
  const latch = useQuery({
    queryKey: ["safety", "emergency-latch"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/safety/emergency-latch");
      if (error) throw error;
      return data;
    },
    refetchInterval: 5000,
  });
  const clearLatch = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/safety/clear-emergency", {
        body: { reason: "cleared from top bar" },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      const r = d as { was_latched?: boolean } | undefined;
      setEstopMsg(r?.was_latched ? tr("中止锁已解除", "Abort latch cleared") : tr("本来就没锁", "The latch was not set"));
      setTimeout(() => setEstopMsg(""), 4000);
      void latch.refetch();
    },
    onError: () => {
      setEstopMsg(tr("解除失败", "Clear failed"));
      setTimeout(() => setEstopMsg(""), 4000);
    },
  });

  // ── live readings: pushed over /ws/events, polled as the floor ────────────
  //
  // The push is written into the react-query cache rather than kept beside it.
  // Keeping a local `pushed` value and rendering whichever of the two has the newer
  // timestamp does not work: RightPanel is mounted on the same screen and polls this
  // very queryKey every 500 ms, so a pushed value would be overwritten twice a
  // second and the display would flicker between two sources. One cache, one
  // timeline, no race.
  //
  // RightPanel and DashboardPage read this same key, so they receive the pushed
  // values too without changes. Their own polls keep running and would correct
  // anything wrong within 500 ms.
  const ws = useWsConnection();
  const queryClient = useQueryClient();
  const [source, setSource] = useState<"ws" | "poll">("poll");

  const onHardwareState = useCallback(
    (event: { data: unknown }) => {
      const r = readingsFromEvent(event.data);
      if (!r) return; // unrecognised payload → leave the poll authoritative
      queryClient.setQueryData(
        LIVE_READINGS_KEY,
        (prev: LiveReadingsResponse | undefined): LiveReadingsResponse | undefined =>
          // No envelope cached yet: decline. The response also carries
          // `connected` / `degraded`, which this event does not know; inventing
          // them to satisfy the type would put a fabricated connection state on
          // screen. Returning undefined tells react-query to leave the cache
          // alone, and the first poll (at most 500 ms away) fills it in properly.
          prev
            ? {
                // Merge, never replace: the sparkline histories belong to the
                // REST payload and a push that dropped them would blank
                // DashboardPage's charts.
                ...prev,
                readings: { ...(prev.readings ?? {}), ...r },
              }
            : undefined,
      );
      setSource("ws");
    },
    [queryClient],
  );
  useWsEvent("hardware_state", onHardwareState);

  const readings = useQuery({
    // Shares its queryKey with RightPanel + DashboardPage, so all three read one
    // deduplicated request; raising the rate does not multiply traffic.
    //
    // Each observer owns its own refetch timer, so de-rating this one does not slow
    // the other two down (nor they it): while RightPanel is mounted the shared cache
    // keeps ticking at 500 ms regardless. This component reads from the push, but the
    // traffic saving only materializes once those two switch over as well.
    queryKey: LIVE_READINGS_KEY,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/hardware/live-readings");
      if (error) throw error;
      setSource("poll");
      return data;
    },
    refetchInterval: ws.polling ? LIVE_READINGS_POLL_MS : LIVE_READINGS_WS_KEEPALIVE_POLL_MS,
  });

  const conn = useQuery({
    queryKey: ["nanonis", "connection"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/nanonis/connection");
      if (error) throw error;
      return data;
    },
    refetchInterval: 5000,
  });

  const models = useQuery({
    queryKey: ["config", "models"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/config/models");
      if (error) throw error;
      return data;
    },
  });

  const health = useQuery({
    queryKey: ["health"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/health");
      if (error) throw error;
      return data;
    },
    staleTime: 5 * 60_000,
  });

  const scope = useCurrentScope();
  const switchExp = useSwitchExperiment();
  const [scopePicker, setScopePicker] = useState(false);

  const r = readings.data?.readings;
  const connected = conn.data?.connected ?? false;
  const zOff = r?.z_controller_on === false || r?.withdrawn === true;
  const currentModel = models.data?.default_alias ?? "—";
  // The built-in agent model is only relevant with legacy features on.
  const { enabled: legacy } = useLegacyFeatures();
  const version = health.data?.version ?? "";

  // Live-reading staleness: the backend flags a snapshot `stale` when the monitor
  // link is down (values carried forward). An errored or stopped readings query is
  // also treated as stale so the top bar never shows hours-old numbers as if live.
  //
  // Unaffected by the WS switch, because pushes are written into the cache:
  // `dataUpdatedAt` advances on a push as well as on a fetch, so one age check
  // covers both transports. A push that stops arriving ages out exactly like a poll
  // that stops returning, so push cannot make stale numbers look permanently live.
  const readingsStale =
    (r as { stale?: boolean } | undefined)?.stale === true ||
    readings.isError ||
    (readings.dataUpdatedAt > 0 &&
      Date.now() - readings.dataUpdatedAt > LIVE_READINGS_STALE_AFTER_MS);

  // Mode pill text. localhost does not imply a simulator (Nanonis normally runs on the
  // same machine as MAST), so show a truthful connected/offline state; a real
  // simulator flag could drive a "Sim" tag later.
  const modeText = !connected
    ? "Nanonis · Offline"
    : readingsStale
      ? "Nanonis · Stale"
      : "Nanonis · Connected";

  // Scope is read from the server's single pointer. Inferring the current experiment
  // locally (and differently here and in RightPanel) let the top bar and the right
  // panel show different experiments; both now read /api/experiments/current.
  const expName = scope.data?.experiment?.name ?? "";
  const smpName = scope.data?.sample?.name ?? "";
  const hasExp = !!scope.data?.experiment;
  const hasSmp = !!scope.data?.sample;

  return (
    <header
      className="sticky top-0 z-30 flex flex-col border-b border-mast-border bg-mast-topbar text-sm"
      // Transport state as a DOM attribute: lets the e2e tests assert the WS/fallback
      // switch without a visible debug widget, and gives something concrete to read
      // back over a screen share when push misbehaves.
      data-ws-status={ws.status}
      data-readings-source={source}
    >
      {/* row 1 — logo · wordmark · subtitle · version | spacer | mode · model · clock · theme · exp chip */}
      <div className="flex items-center gap-[11px] px-[18px] pb-2 pt-[9px]">
        <svg width="24" height="24" viewBox="0 0 24 24" className="block shrink-0 text-mast-accent">
          <circle cx="12" cy="12" r="9.5" fill="none" stroke="currentColor" strokeWidth="1.7" />
          <circle cx="12" cy="12" r="2.9" fill="currentColor" />
        </svg>
        <span className="text-[18px] font-bold tracking-tight text-mast-text">MAST</span>
        <span className="hidden text-[11.5px] text-mast-muted sm:inline">Modular Autonomous SPM Toolkit</span>
        {version && <span className="font-mono text-[10.5px] text-mast-faint">v{version}</span>}

        {/* This row holds E-STOP, the three mode buttons, status dot, model, clock,
            theme and the scope chip. It wraps: without wrapping, a narrow window
            squeezed the only compressible element (the chip). Wrapping reads better
            than squeezing the experiment name down to two characters. */}
        <div className="ml-auto flex min-w-0 flex-wrap items-center justify-end gap-x-3 gap-y-1.5">
          {/* EMERGENCY STOP — always reachable; retracts the tip + aborts the run */}
          <button
            type="button"
            onClick={() => estop.mutate()}
            disabled={estop.isPending}
            title={tr("紧急停止：中止任务 + 停止运动 + 退针", "Emergency stop: abort task + stop motion + retract tip")}
            className="rounded-mast-badge border border-red-500/60 bg-red-600/90 px-2.5 py-0.5 text-[11.5px] font-bold uppercase tracking-wide text-white shadow-[0_0_8px_rgba(220,38,38,0.5)] hover:bg-red-600 disabled:opacity-60"
          >
            {estop.isPending ? "…" : "E‑STOP"}
          </button>
          {estopMsg && (
            <span className="text-[11px] font-medium text-red-400">{estopMsg}</span>
          )}
          {/* Emergency latch is set. Three sources can set it (the E-STOP button, any
              E_STOP event, an environment alarm), two of which need no human action,
              and while set every instrument action is refused. With no UI trace and
              no way to clear it, the symptom is "nothing happened" while the tip has
              been retracted and the instrument is locked until restart. So both facts
              are shown together: that it is latched, and why. */}
          {latch.data?.latched && (
            <span className="inline-flex items-center gap-1.5 rounded-mast-badge border border-amber-500/60 bg-amber-500/15 px-2 py-0.5 text-[11px] font-medium text-amber-300">
              <span
                title={
                  latch.data.why
                    ? tr(`中止原因：${latch.data.why}`, `Abort reason: ${latch.data.why}`)
                    : tr("没有留下中止原因——别假定是人停的。去看服务日志里最近的 CRITICAL 行。", "No abort reason was recorded; do not assume a person stopped it. Check the most recent CRITICAL lines in the service log.")
                }
              >
                {tr("中止锁定中", "Abort latch set")}{latch.data.why ? `${tr("：", ": ")}${latch.data.why}` : tr("（未留原因）", " (no reason recorded)")}
              </span>
              <button
                type="button"
                onClick={() => clearLatch.mutate()}
                disabled={clearLatch.isPending}
                title={tr("解除中止锁，让仪器动作重新被允许。只放开状态，不碰硬件——不进针、不开反馈、不恢复运行。", "Clear the abort latch so instrument actions are allowed again. Only releases the state and does not touch hardware: no tip approach, no feedback on, no run resumed.")}
                className="rounded-mast-badge border border-amber-400/70 px-1.5 py-px text-[10.5px] font-semibold text-amber-200 hover:bg-amber-500/25 disabled:opacity-60"
              >
                {clearLatch.isPending ? "…" : tr("解除", "Clear")}
              </button>
            </span>
          )}
          {/* Global operating mode: safe/semi/auto tip-processing gate. Writes
              autonomy_mode via POST /api/settings; the whole-app colored border
              (AppLayout) reflects the same value. */}
          <div
            className="inline-flex items-center gap-0.5 rounded-mast-ctl border border-mast-border bg-mast-panel-2 p-0.5"
            role="group"
            aria-label={tr("操作模式", "Operating mode")}
          >
            {MODES.map((m) => {
              const seg = MODE_SEG[m];
              const active = autonomyMode === m;
              return (
                <button
                  key={m}
                  type="button"
                  onClick={() => setAutonomyMode.mutate(m)}
                  disabled={setAutonomyMode.isPending}
                  title={seg.title}
                  aria-pressed={active}
                  className={clsx(
                    "inline-flex items-center gap-1 rounded px-2 py-[3px] text-[11px] font-medium transition-colors disabled:opacity-60",
                    active ? seg.active : "text-mast-muted hover:text-mast-text",
                  )}
                >
                  <span
                    className="h-[6px] w-[6px] rounded-full"
                    style={{ background: active ? seg.dot : "var(--mast-faint)" }}
                  />
                  {seg.label}
                </button>
              );
            })}
          </div>
          {/* A failed mode switch rolls the cache back; without a message the mode just
              snaps back and looks like a hang. Say what happened. */}
          {setAutonomyMode.isError && (
            <span
              className="text-[11px] font-medium text-mast-danger"
              title={String((setAutonomyMode.error as Error)?.message ?? "")}
            >
              {tr("模式未切换（后端无响应）· 请重试", "Mode not changed (backend not responding) · please retry")}
            </span>
          )}
          {/* Push is down and staying down. Say so: the readings below are still moving
              (the poll took over), so without this line the only evidence is a slower
              tick, which reads as a hang over a remote link. */}
          {ws.warn && (
            <span
              className="text-[11px] font-medium text-mast-warn"
              title={tr("实时推送连不上，已自动改用定时刷新：读数仍在更新，只是延迟略高。后台还在尝试恢复。", "Live push is unavailable; switched to periodic refresh automatically. Readings still update with slightly higher latency. Recovery is being retried in the background.")}
              data-testid="ws-degraded"
            >
              {tr("实时推送不可用 · 已改定时刷新", "Live push unavailable · using periodic refresh")}
            </span>
          )}
          <span className="inline-flex items-center gap-1.5 text-xs text-mast-text">
            <span
              className="h-[7px] w-[7px] rounded-full bg-mast-auto shadow-[0_0_7px_var(--mast-auto)]"
              style={
                !connected
                  ? { background: "var(--mast-muted)", boxShadow: "none" }
                  : readingsStale
                    ? { background: "var(--mast-warn, #d99)", boxShadow: "none" }
                    : undefined
              }
            />
            {modeText}
          </span>
          {legacy && (
            <span className="rounded-mast-badge bg-mast-accent-soft px-2 py-0.5 font-mono text-[11.5px] text-mast-accent">
              {currentModel}
            </span>
          )}
          <span className="font-mono text-[11px] tabular-nums text-mast-faint">
            {now.toLocaleDateString(langTag())} · {now.toLocaleTimeString(langTag(), { hour12: false })}
          </span>
          <span className="h-[18px] w-px bg-mast-border" />
          <LanguageSwitch />
          <button
            type="button"
            onClick={openOnboardingGuide}
            data-testid="open-guide"
            className="inline-flex items-center gap-1 rounded-mast-ctl border border-mast-border bg-mast-panel-2 px-2 py-[4px] text-[11.5px] text-mast-muted hover:border-mast-accent hover:text-mast-accent"
            title={tr("如何用 Claude Code 控制 MAST", "How to control MAST from Claude Code")}
          >
            ? {tr("引导", "Guide")}
          </button>
          <button
            onClick={toggleTheme}
            className="inline-flex rounded-mast-ctl border border-mast-border bg-mast-panel-2 px-[7px] py-[5px] text-mast-muted hover:border-mast-accent hover:text-mast-accent"
            title={tr("切换主题", "Toggle theme")}
          >
            {theme === "dark" ? "☀" : "🌙"}
          </button>
          {/* Two-part scope chip, always visible. Hiding it when expName is empty would
              be wrong under the hard sample gate: "no sample" blocks scans and
              spectroscopy and must always be shown. Clicking opens the same picker as
              the right panel. */}
          <button
            data-testid="scope-chip"
            onClick={() => setScopePicker(true)}
            /* Names get truncated, so the tooltip must give them back (IDs alone are
               harder to read than the names). */
            title={
              tr(
                `实验：${expName || tr("未选择", "None selected")}\n实验 ID: ${scope.data?.experiment?.id ?? "—"}` +
                  `\n样品：${smpName || tr("未选择", "None selected")}` +
                  (scope.data?.sample?.id ? `\n样品 ID: ${scope.data.sample.id}` : ""),
                `Experiment: ${expName || "None selected"}\nExperiment ID: ${scope.data?.experiment?.id ?? "—"}` +
                  `\nSample: ${smpName || "None selected"}` +
                  (scope.data?.sample?.id ? `\nSample ID: ${scope.data.sample.id}` : ""),
              )
            }
            /* The shell deliberately has no fixed ch cap. Any hard-coded cap must cover
               five children + four gaps + padding, and the two labels are CJK (a CJK
               character is about 1.8ch wide, since 1ch is the width of "0"); caps
               computed that way repeatedly came out too small and truncated the names.
               Instead the shell is bounded only by the row width (the parent wraps
               when it does not fit), and only the two names carry their own caps. */
            className="inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-mast-badge border border-mast-border px-[9px] py-[3px] text-[11.5px] text-mast-muted hover:border-mast-accent"
          >
            <span className="shrink-0 text-mast-faint">{tr("实验", "Experiment")}</span>
            {/* The name cap is not arbitrary: a typical experiment name mixing 7 Latin
                characters and 4 CJK characters is about 14.3 ch wide, so a 14 ch cap
                would truncate it. */}
            <span
              className={
                "min-w-0 max-w-[18ch] truncate font-medium " +
                (hasExp ? "text-mast-text" : "text-mast-warn")
              }
            >
              {expName || tr("未选择", "None selected")}
            </span>
            <span className="shrink-0 text-mast-faint">›</span>
            <span className="shrink-0 text-mast-faint">{tr("样品", "Sample")}</span>
            <span
              className={
                "min-w-0 max-w-[18ch] truncate " +
                (hasSmp ? "text-mast-text" : "animate-pulse text-mast-danger")
              }
            >
              {smpName || tr("未选择", "None selected")}
            </span>
          </button>
          <ExperimentPicker
            open={scopePicker}
            onClose={() => setScopePicker(false)}
            onPick={(id) => {
              setScopePicker(false);
              switchExp.mutate({ experimentId: id });
            }}
            onCreate={() => setScopePicker(false)}
            currentId={scope.data?.experiment?.id}
          />
        </div>
      </div>

      {/* row 2 — grouped live hardware readouts: [tip state] | [feedback loop] | [scan] */}
      <div className="mast-scroll flex items-center overflow-x-auto border-t border-mast-border px-[18px] py-[7px] font-mono text-xs tabular-nums">
        {/* group: tip state — Bias, Current */}
        <div className="flex flex-none items-center gap-3.5 whitespace-nowrap pr-4">
          <span className="flex items-center gap-1.5">
            <span className="text-mast-faint">B</span>
            <span className="text-mast-text" data-testid="reading-bias">{fmt(r?.bias_v, 4)}</span>
            <span className="text-[10.5px] text-mast-muted">V</span>
          </span>
          <span className="flex items-center gap-1.5">
            <span className="text-mast-faint">I</span>
            <span className="text-mast-text">{splitSI(r?.current_a, "A").num}</span>
            <span className="text-[10.5px] text-mast-muted">{splitSI(r?.current_a, "A").unit}</span>
          </span>
        </div>

        <span className="h-5 w-px flex-none bg-mast-border" />

        {/* group: feedback loop — Z pos, ctrl, setpoint */}
        <div className="flex flex-none items-center gap-3.5 whitespace-nowrap px-4">
          <span className="flex items-center gap-1.5">
            <span className="text-mast-faint">Z</span>
            <span className="text-mast-text">{splitSI(r?.z_m, "m", { digits: 5 }).num}</span>
            <span className="text-[10.5px] text-mast-muted">{splitSI(r?.z_m, "m", { digits: 5 }).unit}</span>
          </span>
          <span className="flex items-center gap-1.5">
            <span className="text-mast-faint">ctrl</span>
            <span className={zOff ? "text-mast-warn" : "text-mast-text"}>{zOff ? "OFF" : "ON"}</span>
          </span>
          <span className="flex items-center gap-1.5">
            <span className="text-mast-faint">setpoint</span>
            <span className="text-mast-text">{splitSI(r?.setpoint_a, "A").num}</span>
            <span className="text-[10.5px] text-mast-muted">{splitSI(r?.setpoint_a, "A").unit}</span>
          </span>
        </div>

        <span className="h-5 w-px flex-none bg-mast-border" />

        {/* group: scan */}
        <div className="flex flex-none items-center gap-3.5 whitespace-nowrap pl-4">
          <span className="flex items-center gap-1.5">
            <span className="text-mast-faint">scan</span>
            {r?.scan_running ? (
              <span className="inline-flex items-center gap-1.5 text-mast-auto">
                <span className="h-[7px] w-[7px] rounded-full bg-mast-auto" />
                RUNNING
              </span>
            ) : (
              <span className="text-mast-muted">STOPPED</span>
            )}
          </span>
        </div>
      </div>
    </header>
  );
}
