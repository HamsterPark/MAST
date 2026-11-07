import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import { useCurrentScope } from "@/api/scope";
import { ScopeControls } from "@/components/scope/ScopeControls";
import { fmtBias, fmtCurrent, fmtSI } from "@/lib/units";
import { LIVE_READINGS_POLL_MS, LIVE_READINGS_STALE_AFTER_MS } from "@/lib/pollRates";
import { headlineSensors, readingRows, stubKinds, type EnvSensor } from "@/lib/envPanel";
import { tr } from "@/lib/i18n";

// Persistent right rail: INSTRUMENT / ENVIRONMENT / EXPERIMENT (active experiment +
// sample) / SETTINGS (read-only summary) / SYSTEM / TCP. Visible on every tab.
// Read-only summaries here; editing lives in the Settings and Records tabs.

// One label/value pair rendered as two cells of the parent's auto/1fr grid.
// The value is right-aligned, font-mono tabular-nums; an optional sparkline or
// a status-pill node may replace the plain text value.
function Row({
  label,
  value,
  tone,
  spark,
  sparkColor,
  valueNode,
}: {
  label: string;
  value?: string;
  tone?: string;
  spark?: number[];
  sparkColor?: string;
  valueNode?: React.ReactNode;
}) {
  return (
    <>
      <span className="text-mast-muted">{label}</span>
      <div className="flex items-center justify-end gap-2 text-right">
        {spark && spark.length > 1 && <Sparkline data={spark} color={sparkColor} />}
        {valueNode ?? (
          <span className={`font-mono tabular-nums ${tone ?? "text-mast-text"}`}>{value}</span>
        )}
      </div>
    </>
  );
}

// Section label — mono micro-caps with wide tracking (canvas right-panel labels).
function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <span className="font-mono text-[9.5px] tracking-[1.4px] text-mast-muted">{children}</span>
  );
}

// Inline status pill: semantic triple (warn / muted), used for the Z ctrl OFF and
// Tip withdrawn badges.
function StatusPill({ tone, children }: { tone: "warn" | "muted" | "auto" | "danger"; children: React.ReactNode }) {
  const cls =
    tone === "warn"
      ? "text-mast-warn bg-mast-warn-bg border-mast-warn-border"
      : tone === "auto"
        ? "text-mast-auto bg-mast-auto-bg border-mast-auto-border"
        : tone === "danger"
          ? "text-mast-danger bg-mast-danger-bg border-mast-danger-border"
          : "text-mast-muted border-mast-border-strong";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-mast-badge border px-[7px] py-px text-[10.5px] ${cls}`}
    >
      {children}
    </span>
  );
}

// Compact inline sparkline next to Bias / Current / Z pos.
function Sparkline({ data, color = "var(--mast-accent)" }: { data: number[]; color?: string }) {
  const w = 48;
  const h = 14;
  const lo = Math.min(...data);
  const hi = Math.max(...data);
  const span = hi - lo || 1;
  const pts = data
    .map((v, i) => {
      const x = (i / (data.length - 1)) * w;
      const y = h - ((v - lo) / span) * h;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={w} height={h} className="shrink-0">
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1" />
    </svg>
  );
}

function PanelSection({
  title,
  action,
  grid = true,
  children,
}: {
  title: string;
  action?: React.ReactNode;
  /** Wrap children in the auto/1fr readout grid (default). Set false for free-form bodies. */
  grid?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="border-b border-mast-border px-[18px] py-4 last:border-0">
      <div className="mb-3 flex items-center justify-between">
        <SectionLabel>{title}</SectionLabel>
        {action}
      </div>
      {grid ? (
        <div className="grid grid-cols-[auto_1fr] items-center gap-x-2.5 gap-y-[var(--mast-row-py)] text-[12.5px]">
          {children}
        </div>
      ) : (
        children
      )}
    </div>
  );
}

export function RightPanel() {
  const readings = useQuery({
    queryKey: ["hardware", "live-readings"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/hardware/live-readings");
      if (error) throw error;
      return data;
    },
    // 500 ms rather than 2 s: the endpoint is a cached snapshot with no TCP on the
    // request path, and the producer refreshes at about 1 s, so polling at half the
    // producer's rate would discard every other reading. Costs the instrument
    // nothing. See lib/pollRates.ts.
    refetchInterval: LIVE_READINGS_POLL_MS,
  });
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/settings");
      if (error) throw error;
      return data;
    },
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

  const health = useQuery({
    queryKey: ["health"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/health");
      if (error) throw error;
      return data;
    },
    staleTime: 5 * 60_000,
  });

  // Live ENVIRONMENT readings (vacuum / temperature / helium / noise).
  // Cached relay endpoint (no serial I/O on the request); degrades to N/A.
  const env = useQuery({
    queryKey: ["environment", "readings"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/environment/readings");
      if (error) throw error;
      return data;
    },
    refetchInterval: 5000,
  });
  const qcEnv = useQueryClient();
  const rescan = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.GET("/api/environment/sensors/rescan");
      if (error) throw error;
      return data;
    },
    onSettled: () => qcEnv.invalidateQueries({ queryKey: ["environment", "readings"] }),
  });

  const r = readings.data?.readings;
  const s = settings.data;
  const biasHist = readings.data?.bias_history ?? [];
  const curHist = readings.data?.current_history ?? [];
  const zHist = readings.data?.z_history ?? [];
  const zCtrl = r?.z_controller_status
    ? r.z_controller_status.toUpperCase()
    : r?.z_controller_on == null
      ? "---"
      : r.z_controller_on
        ? "ON"
        : "OFF";
  // The current experiment/sample comes from the server's single pointer. Inferring
  // "current" locally (from experiments[0] here, from a status column in TopBar) let
  // the two show different experiments, and encoding it in a status column meant a
  // crash could leave a stale "active" row. An explicit active_scope pointer is read
  // by both from the same endpoint and queryKey, so they cannot diverge.
  const scope = useCurrentScope();
  const activeExp = scope.data?.experiment
    ? {
        id: scope.data.experiment.id,
        name: scope.data.experiment.name,
        sample_name: scope.data.sample?.name ?? null,
        sample_id: scope.data.sample?.id ?? null,
      }
    : null;

  // Render one headline gauge: live value when present, else N/A.
  // `degraded` (no monitor wired) collapses everything to a muted N/A.
  const envDegraded = env.data?.degraded ?? true;
  function gauge(
    sv?: {
      value?: number | null;
      unit?: string | null;
      connected?: boolean | null;
      status?: string | null;
    } | null,
  ) {
    if (envDegraded || !sv || sv.value == null) {
      return { value: "N/A", tone: "text-mast-muted" };
    }
    const unit = sv.unit ? ` ${sv.unit}` : "";
    const num = Math.abs(sv.value) >= 1e4 || (sv.value !== 0 && Math.abs(sv.value) < 1e-3)
      ? sv.value.toExponential(2)
      : sv.value.toFixed(2);
    // Reflect the sensor's alert status so a vacuum/thermal ALARM does not read the
    // same as a healthy value.
    const tone =
      sv.status === "alarm" || sv.status === "error"
        ? "text-red-400 font-semibold"
        : sv.status === "warning"
          ? "text-amber-400"
          : sv.connected
            ? "text-mast-text"
            : "text-mast-warn";
    return { value: `${num}${unit}`, tone };
  }
  // The four headline gauges, used ONLY as the fallback when no real sensor of
  // that kind is connected (see the ENVIRONMENT section below).
  const ENV_KINDS = [
    { key: "vacuum", label: "Vacuum", headline: gauge(env.data?.vacuum) },
    { key: "temperature", label: "Temperature", headline: gauge(env.data?.temperature) },
    { key: "helium_level", label: "Helium Level", headline: gauge(env.data?.helium_level) },
    { key: "noise_level", label: "Noise Level", headline: gauge(env.data?.noise_level) },
  ];
  const ENV_KIND_KEYS = ENV_KINDS.map((k) => k.key);
  // Row selection logic lives in lib/envPanel.ts (pure functions, unit-tested); this
  // component only renders.
  const envSensors: EnvSensor[] = (env.data?.sensors ?? []) as EnvSensor[];
  // Kinds backed only by a placeholder: they are not "unreadable"; MAST simply has
  // no driver for that quantity.
  const envStubbed = stubKinds(envSensors, ENV_KIND_KEYS);
  const envStubLabels = ENV_KINDS.filter((k) => envStubbed.includes(k.key));
  // Overall environment alert (worst sensor), surfaced as a banner so an unattended
  // vacuum failure / thermal runaway is visible, not just a number.
  const envOverall = env.data?.overall_status ?? "";
  const envAlert = envOverall === "alarm" || envOverall === "error"
    ? "alarm" : envOverall === "warning" ? "warning" : "";

  // Fast polling makes these numbers look more live, so they must never look live
  // when they are not. The backend flags `stale` when the monitor link is down and
  // it is carrying values forward; an errored or long-silent query means the same
  // thing from this side. TopBar applies the same gate.
  const readingsStale =
    (r as { stale?: boolean } | undefined)?.stale === true ||
    readings.isError ||
    (readings.dataUpdatedAt > 0 &&
      Date.now() - readings.dataUpdatedAt > LIVE_READINGS_STALE_AFTER_MS);

  return (
    <aside className="relative w-72 shrink-0 overflow-auto bg-mast-panel">
      {/* accent bar down the left edge (canvas right-panel) */}
      <span className="pointer-events-none absolute inset-y-0 left-0 w-1 bg-mast-accent" />
      <PanelSection title="INSTRUMENT">
        {readingsStale && (
          <div className="col-span-2 mb-1 rounded bg-mast-warn-bg px-1.5 py-1 text-[10.5px] leading-snug text-mast-warn">
            {tr("读数已停更 —— 下面是最后一次读到的值，不是当前值。", "Readings have stopped updating: the values below are the last ones read, not current.")}
          </div>
        )}
        <Row label="Bias"
          value={fmtBias(r?.bias_v)}
          tone={r?.bias_v == null ? "text-mast-muted" : "text-mast-text"}
          spark={biasHist.length ? biasHist : undefined} />
        <Row label="Current"
          value={fmtCurrent(r?.current_a)}
          tone={r?.current_a == null ? "text-mast-muted" : "text-mast-text"}
          spark={curHist.length ? curHist : undefined} sparkColor="var(--mast-accent)" />
        <Row label="Z pos."
          value={fmtSI(r?.z_m, "m", { digits: 5 })}
          tone={r?.z_m == null ? "text-mast-muted" : "text-mast-text"}
          spark={zHist.length ? zHist : undefined} />
        <Row label="Z ctrl"
          valueNode={
            <StatusPill tone={zCtrl === "ON" ? "auto" : zCtrl === "OFF" ? "warn" : "muted"}>{zCtrl}</StatusPill>
          } />
        <Row label="Setpoint" value={fmtCurrent(r?.setpoint_a)} />
        <Row label="Tip"
          valueNode={
            <StatusPill tone={r?.withdrawn ? "muted" : r?.withdrawn === false ? "auto" : "muted"}>
              {r?.withdrawn == null ? "---" : r.withdrawn ? tr("撤回 · 安全", "Withdrawn · safe") : tr("接近", "Approached")}
            </StatusPill>
          } />
        <Row label="Scan"
          valueNode={
            r?.scan_running ? (
              <StatusPill tone="auto">RUNNING</StatusPill>
            ) : (
              <span className="font-mono text-[11.5px] tabular-nums text-mast-muted">
                {r?.scan_running == null ? "---" : "STOPPED"}
              </span>
            )
          } />
      </PanelSection>

      <PanelSection
        title="ENVIRONMENT"
        action={
          <button
            type="button"
            onClick={() => rescan.mutate()}
            disabled={rescan.isPending}
            title={tr("重新扫描传感器", "Rescan sensors")}
            aria-label={tr("重新扫描传感器", "Rescan sensors")}
            className={`inline-flex text-mast-muted hover:text-mast-accent disabled:opacity-50 ${rescan.isPending ? "animate-spin" : ""}`}
          >
            ↻
          </button>
        }
      >
        {envAlert && (
          <div
            className={`col-span-2 mb-1 rounded px-2 py-1 text-[11px] font-semibold ${
              envAlert === "alarm"
                ? "bg-red-600/20 text-red-300"
                : "bg-amber-500/15 text-amber-300"
            }`}
          >
            {envAlert === "alarm"
              ? tr("⚠ 环境告警：某传感器越限（真空/温度）——已触发止损", "⚠ Environment alarm: a sensor is out of limits (vacuum/temperature); protective stop triggered")
              : tr("环境警告：某传感器接近限值", "Environment warning: a sensor is near its limit")}
          </div>
        )}
        {/* Every reading appears once, under the name the instrument reports. Printing
            both the four fixed headline rows and the raw sensor list showed the same
            value twice (once as "Temperature", once under the device name). A real
            sensor now replaces its headline row and keeps its own name (a two-input
            Lake Shore reads out as "SPM" and "Magnet", the names of its inputs). */}
        {ENV_KINDS.map(({ key, label, headline }) => {
          // A kind backed only by a placeholder gets no reading row: a stub that never
          // returned a number shows "N/A", which reads exactly like a broken gauge.
          // It goes into the "Not connected" line below instead.
          if (envStubbed.includes(key)) return null;
          const real = headlineSensors(envSensors, key);
          if (!real.length) {
            return <Row key={key} label={label} value={headline.value} tone={headline.tone} />;
          }
          return real.map((sn) => {
            const g = gauge({ value: sn.value, unit: sn.unit, connected: sn.connected, status: sn.status });
            return <Row key={`${key}:${sn.name}`} label={sn.name || label} value={g.value} tone={g.tone} />;
          });
        })}
        {/* Anything that is not one of the four known kinds (extra configured gauges)
            still gets a row. `readingRows` drops the InstrumentState mirrors here:
            tunnel_current is the same number as INSTRUMENT -> Current above. Only the
            display is dropped; acquisition and criteria are unchanged. */}
        {readingRows(envSensors)
          .filter((sn) => !ENV_KIND_KEYS.includes(sn.kind ?? ""))
          .map((sn) => {
            const g = gauge({ value: sn.value, unit: sn.unit, connected: sn.connected, status: sn.status });
            return <Row key={sn.name} label={sn.name} value={g.value} tone={g.tone} />;
          })}
        {envStubLabels.length > 0 && (
          <div className="col-span-2 mt-1 text-[11px] leading-snug text-mast-faint">
            {tr("未接入：", "Not connected: ")}
            {envStubLabels.map((k, i) => (
              <span key={k.key}>
                {i > 0 && " · "}
                {/* Noise has no dedicated hardware, but this machine's noise is measured:
                    the current monitor computes a detrended RMS. Link there instead of
                    inventing a number here. */}
                {k.key === "noise_level" ? (
                  <Link to="/monitoring/current" className="text-mast-accent hover:underline">
                    {k.label}
                  </Link>
                ) : (
                  k.label
                )}
              </span>
            ))}
          </div>
        )}
        {envDegraded && (
          <div className="col-span-2 mt-2 text-[11px] leading-snug text-mast-faint">
            {tr("传感器监控未连接", "Sensor monitoring not connected")}
          </div>
        )}
      </PanelSection>

      <PanelSection title="EXPERIMENT" grid={false}>
        {activeExp ? (
          <div className="grid grid-cols-[auto_1fr] items-center gap-x-2.5 gap-y-2 text-[12.5px]">
            <Row label={tr("项目", "Project")} value={activeExp.name ?? "—"} />
            {/* See ScopeControls below: display and switching both read
                /api/experiments/current instead of inferring from the experiment list. */}
            {/* The sample name replaces a bare UUID, which told the operator nothing
                (not even whether it was an experiment, sample or conversation id).
                The id stays reachable as a tooltip for when it is needed (filing a
                bug, grepping a log). The "└" indent is not decoration: showing
                project and sample as two equal rows reads as siblings, but one
                experiment has several samples, and every chat started under a sample
                hangs off it. */}
            <Row label={tr("└ 样品", "└ Sample")} value={activeExp.sample_name || "—"} />
            <Row
              label={tr("实验 ID", "Experiment ID")}
              valueNode={
                <span
                  className="cursor-help font-mono text-[11px] tabular-nums text-mast-muted"
                  title={tr(
                    `实验 ID: ${activeExp.id ?? "—"}${activeExp.sample_id ? `\n样品 ID: ${activeExp.sample_id}` : ""}`,
                    `Experiment ID: ${activeExp.id ?? "—"}${activeExp.sample_id ? `\nSample ID: ${activeExp.sample_id}` : ""}`,
                  )}
                >
                  {String(activeExp.id ?? "—").slice(0, 8)}…
                </span>
              }
            />
          </div>
        ) : (
          <div className="text-xs text-mast-muted">{tr("没有当前实验", "No active experiment")}</div>
        )}
        <ScopeControls />
        <a href="/records" className="mt-2 inline-block text-[11.5px] text-mast-accent hover:underline">{tr("实验记录 →", "Records →")}</a>
      </PanelSection>

      <PanelSection title="SETTINGS">
        <Row label="Model" value={s?.model_alias ?? "—"} tone="text-mast-accent text-[11.5px]" />
        <Row label="Thinking" value={s?.thinking ? s.thinking.charAt(0).toUpperCase() + s.thinking.slice(1) : "—"} />
        <Row label="Font" value={s?.font_scale ?? tr("中", "Medium")} />
        <Row label="Theme" value={s?.theme ?? "Dark"} />
        <p className="col-span-2 mt-2 text-[11px] leading-snug text-mast-faint">
          {tr("在「设置」标签中修改模型 / 外观 / 硬件连接 / 知识 / 语音。", "Change model / appearance / hardware connection / knowledge / voice in the Settings tab.")}
        </p>
      </PanelSection>

      <PanelSection title="SYSTEM" grid={false}>
        <div className="text-[12.5px] font-semibold text-mast-text">MAST v{health.data?.version ?? "?"}</div>
        <div className="mb-2 text-[11.5px] text-mast-muted">
          {health.data?.skill_registry_wired ? tr("技能注册已就绪", "Skill registry ready") : tr("技能注册未连接", "Skill registry not connected")}
        </div>
        <div className="flex gap-3">
          <a href="/settings" className="inline-block text-[11.5px] text-mast-accent hover:underline">{tr("设置 →", "Settings →")}</a>
          {/* `/dashboard` (bench overview: status cards / live readings / system
              self-check table / TCP / per-agent models) has no place in the top bar,
              so this link is the only rendered route to it; the self-check table has
              no other home. (test/scanMapPairing.test.ts guards that every page is
              reachable.) */}
          <Link to="/dashboard" className="inline-block text-[11.5px] text-mast-accent hover:underline">
            {tr("系统自检 →", "System check →")}
          </Link>
        </div>
      </PanelSection>

      {/* TCP footer — separate from System, like the old get_connection_compact_html */}
      <PanelSection title="TCP">
        <Row label={tr("连接", "Connection")}
          valueNode={
            <StatusPill tone={conn.data?.connected ? "auto" : "muted"}>
              {conn.data?.connected ? tr("已连接", "Connected") : tr("未连接", "Disconnected")}
            </StatusPill>
          } />
        {(conn.data?.ports ?? []).map((p) => (
          <Row key={p.role} label={p.role} value={`${p.port ?? ""} ${p.connected ? "●" : "○"}`}
            tone={p.connected ? "text-mast-auto" : "text-mast-muted"} />
        ))}
      </PanelSection>
    </aside>
  );
}
// ExperimentControls was replaced by components/scope/ScopeControls: free text + New
// was the source of duplicate experiments, and there is no "end experiment/sample"
// action any more; experiments can always be continued.
