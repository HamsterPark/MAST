import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import { Section, Card, Badge, Spinner, ErrorNote, DegradedNote, EmptyNote } from "@/components/ui";
import { Sparkline } from "@/components/vision/Sparkline";
import { fmtBias, fmtCurrent, fmtSI } from "@/lib/units";
import { LIVE_READINGS_POLL_MS } from "@/lib/pollRates";
import { tr } from "@/lib/i18n";
import { Button } from "@/components/controls";
import { openOnboardingGuide } from "@/components/shell/OnboardingGuide";
import { useLegacyFeatures } from "@/hooks/useLegacyFeatures";
import { useStationProfile } from "@/hooks/useStationProfile";
import { useStationCapabilities } from "@/hooks/useStationCapabilities";
import { GALLERY_KEYS } from "@/components/gallery/useGalleryData";
import { ISSUES_URL, RECOMMENDED_MODEL_LABEL } from "@/lib/project";
import { readingRows, sensorLabel, type EnvSensor } from "@/lib/envPanel";
import { overviewKind } from "@/lib/stationCapabilities";
import {
  countDevices,
  describeDeviceCounts,
  deviceStatus,
  deviceStatusLabel,
  groupByKind,
  profileLabel,
  type DeviceStatus,
} from "@/lib/stationProfile";

// DashboardPage — overview (home page when legacy features are off).
//   · Getting-started card (control MAST from Claude Code through the plugin).
//   · Status cards: Nanonis connection / experiment storage / main model (IC, legacy only).
//   · Live hardware readings: bias / current / z / setpoint (+ x/y/controller/withdraw/scan),
//     /api/hardware/live-readings, polled at LIVE_READINGS_POLL_MS, with sparklines.
//   · System self-check table: /api/system/check.
//   · TCP ports + per-agent models (legacy only).
// Every read renders loading / error / degraded / empty.
//
// What the page shows follows the station's capabilities (lib/stationCapabilities.ts):
//   · `nanonis` (an STM station): the layout above, untouched;
//   · without it, the Nanonis connection card, the live hardware readings and the TCP
//     ports do not apply and the two STM reads (/api/hardware/live-readings,
//     /api/nanonis/connection) are not issued from this page at all; a station-profile
//     card takes the Nanonis card's place;
//   · `optical_bench` (optical and generic stations): a devices card and a devices table
//     (/api/optics/devices, a configuration read that opens no device), "optical devices"
//     on a station with `witec`, "external devices" otherwise;
//   · `sensor_echo` (a generic station): the environment sensors (/api/environment/
//     readings, the same cache and interval as the right panel);
//   · the generic overview (neither `nanonis` nor `witec`): a data gallery overview made of
//     light reads only (the data roots and the build status), never the gallery index.
// Every read waits until /api/health has said what the station has, so a non-STM
// station never sends even one STM request from here.

const POLL_MS = 2000;

function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/health");
      if (error) throw error;
      return data;
    },
    refetchInterval: POLL_MS,
  });
}
// `enabled` is false on a station without Nanonis: the read is never sent and
// nothing polls it from this page. The hooks are still called on every render, so
// the hook order does not depend on the station.
function useConnection(enabled: boolean) {
  return useQuery({
    queryKey: ["nanonis", "connection"],
    enabled,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/nanonis/connection");
      if (error) throw error;
      return data;
    },
    refetchInterval: POLL_MS,
  });
}
function useLiveReadings(enabled: boolean) {
  return useQuery({
    queryKey: ["hardware", "live-readings"],
    enabled,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/hardware/live-readings");
      if (error) throw error;
      return data;
    },
    // Faster than the other cards on purpose: this one is a cached snapshot the
    // request never pays TCP for, so it can track the ~1 s producer instead of
    // sampling it at half rate — see lib/pollRates.ts.
    refetchInterval: LIVE_READINGS_POLL_MS,
  });
}
// The optical bench inventory: a configuration read that never opens a device.
// Same key as OpticsPanel / AxisPanel, so they share one cached list.
function useOpticsDevices(enabled: boolean) {
  return useQuery({
    queryKey: ["optics", "devices"],
    enabled,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/optics/devices");
      if (error) throw error;
      return data;
    },
    refetchInterval: 15000,
  });
}
// Environment sensors: the cached relay the right panel polls (same key, so the two
// share one request). No serial I/O happens on the request path.
function useEnvironmentReadings(enabled: boolean) {
  return useQuery({
    queryKey: ["environment", "readings"],
    enabled,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/environment/readings");
      if (error) throw error;
      return data;
    },
    refetchInterval: 5000,
  });
}
// The data gallery at a glance, from light reads only. The index itself is the whole file
// list (hundreds of thousands of entries on a large data root), so this page never asks for
// it; it reads
//   · the configuration (the data roots: a few lines) and the build status (in-process
//     counters, no disk walk), each once when the page opens: no polling from here;
//   · the index counts only if the gallery page has already fetched the index this
//     session (`enabled: false` reads the shared cache and never fetches).
// Same keys as the gallery page (components/gallery/useGalleryData.ts).
function useGalleryOverview(enabled: boolean) {
  const config = useQuery({
    queryKey: GALLERY_KEYS.config,
    enabled,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/gallery/config");
      if (error) throw error;
      return data;
    },
  });
  const status = useQuery({
    queryKey: GALLERY_KEYS.status,
    enabled,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/gallery/status");
      if (error) throw error;
      return data;
    },
  });
  const cachedIndex = useQuery({
    queryKey: GALLERY_KEYS.index,
    // never fetches: only shows what the gallery page already has
    enabled: false,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/gallery/index");
      if (error) throw error;
      return data;
    },
  });
  return { config, status, cachedIndex: cachedIndex.data };
}
function useSystemCheck() {
  return useQuery({
    queryKey: ["system", "check"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/system/check");
      if (error) throw error;
      return data;
    },
    refetchInterval: 15000,
  });
}
function useAgentModels(enabled: boolean) {
  return useQuery({
    queryKey: ["agents", "models"],
    enabled,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/agents/models");
      if (error) throw error;
      return data;
    },
    refetchInterval: 10000,
  });
}

// ── value formatters ─────────────────────────────────────────────────────────
// Delegated to @/lib/units so that this page, TopBar and RightPanel share one
// implementation (separate copies had pinned currents to pA or fallen back to raw
// `toExponential` output).
const fmtSetpoint = fmtCurrent;
const fmtZ = (m?: number | null) => fmtSI(m, "m", { digits: 5 });

// ── small presentational helpers ─────────────────────────────────────────────
function StatCard({
  label,
  children,
  tone,
}: {
  label: string;
  children: ReactNode;
  tone?: "ok" | "err" | "warn" | "dim";
}) {
  const toneCls =
    tone === "ok"
      ? "text-mast-auto"
      : tone === "err"
        ? "text-mast-danger"
        : tone === "warn"
          ? "text-mast-warn"
          : "text-mast-text";
  return (
    <Card>
      <div className="text-xs uppercase tracking-wide text-mast-muted">{label}</div>
      <div className={`mt-2 text-lg font-semibold tabular-nums ${toneCls}`}>{children}</div>
    </Card>
  );
}

function Reading({
  label,
  value,
  valueClass,
  spark,
}: {
  label: ReactNode;
  value: ReactNode;
  valueClass?: string;
  spark?: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-mast-border py-2 last:border-b-0">
      <span className="text-sm text-mast-muted">{label}</span>
      <span className="flex items-center gap-3">
        {spark}
        <span className={`text-sm font-medium tabular-nums ${valueClass ?? "text-mast-text"}`}>
          {value}
        </span>
      </span>
    </div>
  );
}

const CHECK_ICON: Record<string, string> = {
  ok: "✅",
  warning: "⚠️",
  error: "❌",
  unavailable: "🔧",
};
const CHECK_TONE: Record<string, string> = {
  ok: "text-mast-auto",
  warning: "text-mast-warn",
  error: "text-mast-danger",
  unavailable: "text-mast-muted",
};

const DOT = "●";

// Badge tones for a configured device: switched off (needs attention) / MAST has it
// open / enabled but not opened yet (devices open on first use, so not an error).
const DEVICE_TONE: Record<DeviceStatus, string> = {
  disabled: "WARN",
  connected: "AUTO",
  idle: "INFO",
};

// Badge tones for an environment sensor's alert status.
const SENSOR_TONE: Record<string, string> = {
  ok: "AUTO",
  warning: "WARN",
  alarm: "DANGEROUS",
  error: "DANGEROUS",
};

function fmtSensorValue(s: EnvSensor): string {
  if (s.value == null || !Number.isFinite(s.value)) return "N/A";
  const v = s.value;
  const num = Math.abs(v) >= 1e4 || (v !== 0 && Math.abs(v) < 1e-3) ? v.toExponential(2) : v.toFixed(2);
  return s.unit ? `${num} ${s.unit}` : num;
}

export default function DashboardPage() {
  const health = useHealth();
  const profile = useStationProfile();
  const station = useStationCapabilities();
  // Which overview: STM (Nanonis), optical (WITec) or generic (neither).
  const kind = overviewKind(station.has);
  const nanonis = kind === "stm";
  // Each section follows its own capability.
  const bench = station.has("optical_bench");
  const sensors = station.has("sensor_echo");
  // Every read of this page waits for the first answer of /api/health (or its first
  // failure), so a station without Nanonis never sends the two STM reads, not even once,
  // and an STM station never asks for the device inventory. When health cannot be read
  // at all the station counts as STM and the STM reads run exactly as they always did.
  const stmReads = station.ready && nanonis;
  const benchReads = station.ready && bench;
  const sensorReads = station.ready && sensors;
  const galleryReads = station.ready && kind === "generic";
  const connection = useConnection(stmReads);
  const live = useLiveReadings(stmReads);
  const optics = useOpticsDevices(benchReads);
  const env = useEnvironmentReadings(sensorReads);
  const gallery = useGalleryOverview(galleryReads);
  const check = useSystemCheck();
  const legacy = useLegacyFeatures();
  const showAgents = legacy.enabled;
  const models = useAgentModels(showAgents);

  const conn = connection.data;
  const lr = live.data;
  const hw = lr?.readings;

  const expWired = health.data?.experiment_storage_wired ?? false;
  const connConnected = conn?.connected ?? false;
  const connPorts = conn?.ports ?? [];

  const devices = optics.data?.devices ?? [];
  const deviceCounts = countDevices(devices);
  const deviceGroups = groupByKind(devices);
  const opticsTone: "ok" | "err" | "dim" | undefined = optics.isError
    ? "err"
    : optics.data?.degraded || deviceCounts.enabled === 0
      ? "dim"
      : deviceCounts.connected === deviceCounts.enabled
        ? "ok"
        : undefined;

  // What the devices are called here: the optical bench on a station with WITec (the
  // backend's own wording, generic_station.md §2), the configured devices elsewhere.
  const devicesTitle = station.has("witec") ? tr("光学设备", "Optical devices") : tr("外部设备", "External devices");

  const envSensors: EnvSensor[] = (env.data?.sensors ?? []) as EnvSensor[];
  const envRows = readingRows(envSensors);
  const galleryRoots = gallery.config.data?.roots ?? [];
  const galleryStatus = gallery.status.data;
  const galleryIndex = gallery.cachedIndex;
  const galleryCells: { label: string; value: string }[] = [
    {
      label: tr("数据根目录（启用 / 全部）", "Data roots (enabled / all)"),
      value: `${galleryRoots.filter((r) => r.enabled).length} / ${galleryRoots.length}`,
    },
  ];
  if (galleryIndex && galleryIndex.built && !galleryIndex.degraded) {
    // the counts, only because the gallery page has already loaded the index this session
    galleryCells.push(
      { label: tr("图像帧", "Frames"), value: String(galleryIndex.n_frames) },
      { label: tr("谱", "Spectra"), value: String(galleryIndex.n_spectra) },
      { label: tr("网格谱", "Grids"), value: String(galleryIndex.n_grids) },
    );
  } else if (galleryStatus?.phase === "done") {
    galleryCells.push({ label: tr("上次建索引的文件数", "Files at the last index build"), value: String(galleryStatus.n_files) });
  }

  const agents = models.data?.agents ?? [];
  const icAgent = agents.find((a) => a.agent_id === "ic") ?? agents[0];
  // Full class names so Tailwind's scanner sees them. STM: 2 cards (3 with the
  // legacy model card), as before; elsewhere: profile / storage (+ devices) (+ model).
  const cardCount = 2 + (bench ? 1 : 0) + (showAgents ? 1 : 0);
  const gridCols =
    cardCount >= 4 ? "sm:grid-cols-4" : cardCount === 3 ? "sm:grid-cols-3" : "sm:grid-cols-2";

  const checkItems = check.data?.items ?? [];
  const checkCounts = checkItems.reduce<Record<string, number>>((acc, it) => {
    acc[it.status] = (acc[it.status] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="space-y-2">
      {/* ── Status cards ─────────────────────────────────────────────────── */}
      {/* ── Getting started ──────────────────────────────────────────────── */}
      <Card>
        <div className="flex flex-wrap items-start gap-x-6 gap-y-3">
          <div className="min-w-[16rem] flex-1 space-y-1.5">
            <div className="text-sm font-semibold text-mast-text">{tr("快速开始", "Getting started")}</div>
            <p className="text-sm text-mast-muted">
              {tr(
                `MAST 设计为通过 MAST 插件由 Claude Code 控制（推荐模型：${RECOMMENDED_MODEL_LABEL}）。`,
                `MAST is meant to be controlled from Claude Code through the MAST plugin (recommended model: ${RECOMMENDED_MODEL_LABEL}).`,
              )}
            </p>
            <p className="text-xs text-mast-muted">
              {kind === "optical"
                ? tr(
                    "这是一台光学站：没有 STM / Nanonis。AI 通过 MCP 编排 WITec 拉曼系统，以及激光、功率计和温控；需要人动手的步骤会弹窗提醒操作员。需求与反馈请到 ",
                    "This is an optical station: there is no STM / Nanonis. The AI orchestrates the WITec Raman system, the laser, the power meter and the temperature controller through MCP; steps that need a person's hands raise a pop-up for the operator. For requests and feedback, visit ",
                  )
                : kind === "generic"
                  ? tr(
                      "这是一台通用站：没有 STM / Nanonis，也没有 WITec。MAST 管环境传感器、配置的外部设备、数据图库和实验记录，AI 通过 MCP 使用通用技能。需求与反馈请到 ",
                      "This is a generic station: there is no STM / Nanonis and no WITec. MAST looks after the environment sensors, the configured devices, the data gallery and the records, and the AI uses the generic skills through MCP. For requests and feedback, visit ",
                    )
                  : tr(
                      "目前支持 Nanonis 控制器（通过 Nanonis TCP 接口）；其他仪器的支持可以后续添加。需求与反馈请到 ",
                      "MAST currently supports Nanonis controllers (via the Nanonis TCP interface); support for other instruments can be added. For requests and feedback, visit ",
                    )}
              <a
                href={ISSUES_URL}
                target="_blank"
                rel="noreferrer noopener"
                className="text-mast-accent hover:underline"
              >
                {tr("问题反馈页", "the issue tracker")}
              </a>
              {tr("。", ".")}
            </p>
          </div>
          <Button variant="primary" onClick={() => openOnboardingGuide()}>
            {tr("打开入门指南", "Open the getting-started guide")}
          </Button>
        </div>
      </Card>

      {/* ── Status cards ─────────────────────────────────────────────────── */}
      <Section title={tr("总览", "Overview")}>
        <div className={`grid grid-cols-1 gap-3 ${gridCols}`}>
          {!nanonis ? (
            <StatCard label={tr("站点画像", "Station profile")}>
              {profileLabel(station.profile)}
              {profile.restartPending ? (
                <span className="mt-1 block text-xs font-normal text-mast-warn">
                  {tr(
                    "站点画像已更改，需要重启 MAST 服务后完全生效",
                    "The station profile was changed; restart the MAST service for it to take full effect",
                  )}
                </span>
              ) : null}
            </StatCard>
          ) : (
            <StatCard
              label={tr("Nanonis 连接", "Nanonis connection")}
              tone={connection.isError ? "err" : connConnected ? "ok" : "dim"}
            >
              {connection.isPending ? (
                <span className="text-base font-normal text-mast-muted">{tr("加载中…", "Loading…")}</span>
              ) : connection.isError ? (
                <span className="text-base font-normal text-mast-danger">{tr("读取失败", "Read failed")}</span>
              ) : connConnected ? (
                <>
                  {tr("已连接", "Connected")}
                  {conn?.host ? <span className="ml-1 text-sm text-mast-muted">{conn.host}</span> : null}
                </>
              ) : (
                tr("未连接", "Not connected")
              )}
            </StatCard>
          )}

          <StatCard
            label={tr("实验存储", "Experiment storage")}
            tone={health.isError ? "err" : expWired ? "ok" : "dim"}
          >
            {health.isPending ? (
              <span className="text-base font-normal text-mast-muted">{tr("加载中…", "Loading…")}</span>
            ) : health.isError ? (
              <span className="text-base font-normal text-mast-danger">{tr("读取失败", "Read failed")}</span>
            ) : expWired ? (
              tr("已就绪", "Ready")
            ) : (
              tr("未接入", "Not wired")
            )}
          </StatCard>

          {bench && (
            <StatCard label={devicesTitle} tone={opticsTone}>
              {optics.isPending ? (
                <span className="text-base font-normal text-mast-muted">{tr("加载中…", "Loading…")}</span>
              ) : optics.isError ? (
                <span className="text-base font-normal text-mast-danger">{tr("读取失败", "Read failed")}</span>
              ) : optics.data?.degraded ? (
                <span className="text-base font-normal text-mast-muted">{tr("不可用", "Unavailable")}</span>
              ) : (
                <>
                  <span className="text-base">{describeDeviceCounts(deviceCounts)}</span>
                  {deviceCounts.disabled > 0 ? (
                    <span className="mt-1 block text-xs font-normal text-mast-muted">
                      {tr(`另有 ${deviceCounts.disabled} 台未启用`, `${deviceCounts.disabled} more disabled`)}
                    </span>
                  ) : null}
                </>
              )}
            </StatCard>
          )}

          {showAgents && (
            <StatCard
              label={tr("主模型 (IC)", "Main model (IC)")}
              tone={models.isError ? "err" : icAgent ? undefined : "dim"}
            >
              {models.isPending ? (
                <span className="text-base font-normal text-mast-muted">{tr("加载中…", "Loading…")}</span>
              ) : models.isError ? (
                <span className="text-base font-normal text-mast-danger">{tr("读取失败", "Read failed")}</span>
              ) : icAgent ? (
                <span className="text-base">
                  {icAgent.model}
                  {icAgent.thinking ? (
                    <span className="ml-2 text-sm text-mast-muted">{icAgent.thinking}</span>
                  ) : null}
                </span>
              ) : (
                <span className="text-base font-normal text-mast-muted">{tr("无", "None")}</span>
              )}
            </StatCard>
          )}
        </div>

        {health.data?.version && (
          <p className="mt-2 text-xs text-mast-muted">
            {health.data.service} · v{health.data.version} · {tr("技能注册表", "skill registry")}{" "}
            {health.data.skill_registry_wired ? tr("已接入", "wired") : tr("未接入", "not wired")} ·{" "}
            {tr("设置存储", "settings store")}{" "}
            {health.data.settings_store_wired ? tr("已接入", "wired") : tr("未接入", "not wired")}
          </p>
        )}
      </Section>

      {/* ── Hardware live readings ───────────────────────────────────────── */}
      {nanonis && (
        <Section title={tr("硬件实时读数", "Live hardware readings")}>
          {live.isPending && <Spinner />}
          {live.isError && <ErrorNote error={live.error} />}
          {lr?.degraded && <DegradedNote what={tr("硬件实时读数", "Live hardware readings")} />}
          {lr && !lr.degraded && (
            <Card>
              <div className="mb-2 flex items-center gap-2 text-xs text-mast-muted">
                <span className={lr.connected ? "text-mast-auto" : "text-mast-danger"}>{DOT}</span>
                {lr.connected
                  ? tr("已连接（每 2 秒刷新）", "Connected (refreshes every 2 s)")
                  : tr("主端口未连接", "Main port not connected")}
                {hw?.timestamp ? <span className="ml-auto">{hw.timestamp}</span> : null}
              </div>
              <Reading
                label="Bias"
                value={fmtBias(hw?.bias_v)}
                spark={<Sparkline values={lr.bias_history ?? []} color="var(--mast-ag-ic)" />}
              />
              <Reading
                label="Current"
                value={fmtCurrent(hw?.current_a)}
                spark={<Sparkline values={lr.current_history ?? []} color="var(--mast-dream)" />}
              />
              <Reading
                label="Z pos."
                value={fmtZ(hw?.z_m)}
                spark={<Sparkline values={lr.z_history ?? []} color="var(--mast-ag-ic)" />}
              />
              <Reading label="Setpoint" value={fmtSetpoint(hw?.setpoint_a)} />
              <Reading
                label={tr("Z 控制器", "Z controller")}
                value={
                  hw?.z_controller_status
                    ? hw.z_controller_status.toUpperCase()
                    : hw?.z_controller_on == null
                      ? "---"
                      : hw.z_controller_on
                        ? "ON"
                        : "OFF"
                }
                valueClass={
                  (hw?.z_controller_status ?? "").toLowerCase() === "on" || hw?.z_controller_on === true
                    ? "text-mast-auto"
                    : (hw?.z_controller_status ?? "").toLowerCase() === "off" || hw?.z_controller_on === false
                      ? "text-mast-danger"
                      : "text-mast-text"
                }
              />
              {hw?.x_m != null && <Reading label="X pos." value={fmtZ(hw.x_m)} />}
              {hw?.y_m != null && <Reading label="Y pos." value={fmtZ(hw.y_m)} />}
              {hw?.withdrawn != null && (
                <Reading
                  label={tr("探针", "Tip")}
                  value={hw.withdrawn ? "WITHDRAWN" : "ENGAGED"}
                  valueClass={hw.withdrawn ? "text-mast-danger" : "text-mast-auto"}
                />
              )}
              {hw?.scan_running != null && (
                <Reading
                  label={tr("扫描", "Scan")}
                  value={hw.scan_running ? "RUNNING" : "STOPPED"}
                  valueClass={hw.scan_running ? "text-mast-auto" : "text-mast-muted"}
                />
              )}
            </Card>
          )}
        </Section>
      )}

      {/* ── Configured devices (stations with the optical bench) ──────────── */}
      {bench && (
        <Section
          title={devicesTitle}
          subtitle={tr(
            "“已连接”表示 MAST 已经打开该设备；这里只读配置，不会主动打开设备。",
            "“Connected” means MAST has already opened the device; this only reads the configuration and never opens a device.",
          )}
        >
          {optics.isPending && <Spinner />}
          {optics.isError && <ErrorNote error={optics.error} />}
          {optics.data?.degraded && <DegradedNote what={tr("设备清单", "Device list")} />}
          {optics.data?.degraded && optics.data.detail ? (
            <p className="mt-2 text-xs text-mast-muted">{optics.data.detail}</p>
          ) : null}
          {optics.data && !optics.data.degraded && devices.length === 0 && (
            <EmptyNote
              label={tr(
                "尚未配置设备。请在 config/optical_instruments.json 里添加设备并把 enabled 设为 true，然后刷新本页。",
                "No devices are configured. Add them to config/optical_instruments.json with enabled set to true, then refresh this page.",
              )}
            />
          )}
          {optics.data && !optics.data.degraded && devices.length > 0 && (
            <div className="overflow-auto rounded-lg border border-mast-border">
              <table className="w-full text-sm">
                <thead className="bg-mast-panel text-mast-muted">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">{tr("设备", "Device")}</th>
                    <th className="px-3 py-2 text-left font-medium">{tr("类型", "Type")}</th>
                    <th className="px-3 py-2 text-left font-medium">{tr("类别", "Kind")}</th>
                    <th className="px-3 py-2 text-left font-medium">{tr("状态", "Status")}</th>
                  </tr>
                </thead>
                <tbody>
                  {deviceGroups.flatMap((g) =>
                    g.devices.map((d) => {
                      const status = deviceStatus(d);
                      return (
                        <tr key={d.id} className="border-t border-mast-border hover:bg-mast-bg/40">
                          <td className="px-3 py-2">
                            <div className="font-medium text-mast-text">{d.name}</div>
                            <div className="font-mono text-xs text-mast-faint">
                              {d.id}
                              {d.role ? ` · ${d.role}` : ""}
                            </div>
                          </td>
                          <td className="px-3 py-2 font-mono text-xs text-mast-muted">{d.type}</td>
                          <td className="px-3 py-2 text-mast-muted">{g.label}</td>
                          <td className="px-3 py-2">
                            <Badge tone={DEVICE_TONE[status]}>{deviceStatusLabel(status)}</Badge>
                          </td>
                        </tr>
                      );
                    }),
                  )}
                </tbody>
              </table>
            </div>
          )}
        </Section>
      )}

      {/* ── Environment sensors (stations whose readings ARE the sensors) ─── */}
      {sensors && (
        <Section
          title={tr("环境传感器", "Environment sensors")}
          subtitle={tr(
            "温度、真空等环境量的当前读数；长期曲线在「监控 → 环境历史」。",
            "Current readings of temperature, vacuum and the other environment quantities; the long-term curves are under Monitoring → Environment history.",
          )}
        >
          {env.isPending && <Spinner />}
          {env.isError && <ErrorNote error={env.error} />}
          {env.data?.degraded && <DegradedNote what={tr("环境传感器", "Environment sensors")} />}
          {env.data && !env.data.degraded && envRows.length === 0 && (
            <EmptyNote
              label={tr(
                "还没有接入环境传感器。可以在「设置 → 常规设置 → 温度计 / 真空计」里扫描串口、确认后接入。",
                "No environment sensor is connected yet. Scan the serial ports under Settings → General → Thermometer / vacuum gauge and confirm the ones found.",
              )}
            />
          )}
          {env.data && !env.data.degraded && envRows.length > 0 && (
            <div className="overflow-auto rounded-lg border border-mast-border" data-testid="env-sensors">
              <table className="w-full text-sm">
                <thead className="bg-mast-panel text-mast-muted">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">{tr("传感器", "Sensor")}</th>
                    <th className="px-3 py-2 text-left font-medium">{tr("类别", "Kind")}</th>
                    <th className="px-3 py-2 text-right font-medium">{tr("读数", "Reading")}</th>
                    <th className="px-3 py-2 text-left font-medium">{tr("状态", "Status")}</th>
                  </tr>
                </thead>
                <tbody>
                  {envRows.map((s) => (
                    <tr key={s.name} className="border-t border-mast-border hover:bg-mast-bg/40">
                      <td className="px-3 py-2 font-medium text-mast-text">{sensorLabel(s.name)}</td>
                      <td className="px-3 py-2 text-mast-muted">{s.kind ?? "—"}</td>
                      <td className="px-3 py-2 text-right font-mono tabular-nums text-mast-text">
                        {s.connected ? fmtSensorValue(s) : "N/A"}
                      </td>
                      <td className="px-3 py-2">
                        {s.connected ? (
                          <Badge tone={SENSOR_TONE[s.status ?? ""] ?? "INFO"}>{s.status ?? "—"}</Badge>
                        ) : (
                          <Badge tone="WARN">{tr("未连接", "Not connected")}</Badge>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Section>
      )}

      {/* ── Data gallery overview (generic station) ───────────────────────── */}
      {kind === "generic" && (
        <Section
          title={tr("数据图库概况", "Data gallery")}
          actions={
            <Link to="/records/gallery" className="text-xs text-mast-accent hover:underline">
              {tr("打开数据图库 →", "Open the data gallery →")}
            </Link>
          }
        >
          {gallery.config.isPending && <Spinner />}
          {gallery.config.isError && <ErrorNote error={gallery.config.error} />}
          {gallery.config.data?.degraded && <DegradedNote what={tr("数据图库", "Data gallery")} />}
          {gallery.config.data && !gallery.config.data.degraded && galleryRoots.length === 0 && (
            <EmptyNote
              label={tr(
                "数据图库还没有配置数据根目录。打开数据图库，在它的设置里添加要索引的文件夹。",
                "The data gallery has no data roots yet. Open the gallery and add the folders to index in its setup.",
              )}
            />
          )}
          {gallery.config.data && !gallery.config.data.degraded && galleryRoots.length > 0 && (
            <Card>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4" data-testid="gallery-overview">
                {galleryCells.map(({ label, value }) => (
                  <div key={label}>
                    <div className="text-xs uppercase tracking-wide text-mast-muted">{label}</div>
                    <div className="mt-1 text-lg font-semibold tabular-nums text-mast-text">{value}</div>
                  </div>
                ))}
              </div>
              <p className="mt-2 text-xs text-mast-muted" data-testid="gallery-status">
                {galleryStatus?.degraded || gallery.status.isError
                  ? tr("读不到建索引的状态。", "The index build status cannot be read.")
                  : galleryStatus?.running
                    ? tr("索引正在更新…", "The index is being updated…")
                    : galleryStatus?.phase === "done" && galleryStatus.finished
                      ? tr(`上次更新索引：${galleryStatus.finished}`, `Index last updated: ${galleryStatus.finished}`)
                      : galleryStatus?.phase === "error"
                        ? tr(`上次更新索引出错：${galleryStatus.message}`, `The last index update failed: ${galleryStatus.message}`)
                        : gallery.config.data.auto_build
                          ? tr(
                              `这次启动后还没有更新过索引；后台每 ${gallery.config.data.auto_interval_min} 分钟自动更新，打开数据图库时也会更新。`,
                              `The index has not been updated since this start; it updates in the background every ${gallery.config.data.auto_interval_min} min, and when the gallery is opened.`,
                            )
                          : tr(
                              "这次启动后还没有更新过索引；自动更新已关闭，可以在数据图库里手动更新。",
                              "The index has not been updated since this start; automatic updates are off, update it from the data gallery.",
                            )}
              </p>
            </Card>
          )}
        </Section>
      )}

      {/* ── System self-check ────────────────────────────────────────────── */}
      <Section
        title={tr("系统自检", "System self-check")}
        actions={
          checkItems.length > 0 ? (
            <span className="flex gap-3 text-xs">
              {(["ok", "warning", "error", "unavailable"] as const).map((s) =>
                checkCounts[s] ? (
                  <span key={s} className={CHECK_TONE[s]}>
                    {CHECK_ICON[s]} {checkCounts[s]}
                  </span>
                ) : null,
              )}
            </span>
          ) : undefined
        }
      >
        {check.isPending && <Spinner />}
        {check.isError && <ErrorNote error={check.error} />}
        {check.data?.degraded && <DegradedNote what={tr("系统自检", "System self-check")} />}
        {check.data && !check.data.degraded && checkItems.length === 0 && (
          <EmptyNote label={tr("暂无自检结果。", "No self-check results yet.")} />
        )}
        {check.data && !check.data.degraded && checkItems.length > 0 && (
          <div className="overflow-auto rounded-lg border border-mast-border">
            <table className="w-full text-sm">
              <thead className="bg-mast-panel text-mast-muted">
                <tr>
                  <th className="w-12 px-3 py-2 text-center font-medium">{tr("状态", "Status")}</th>
                  <th className="px-3 py-2 text-left font-medium">{tr("组件", "Component")}</th>
                  <th className="px-3 py-2 text-left font-medium">{tr("详情", "Details")}</th>
                </tr>
              </thead>
              <tbody>
                {checkItems.map((it, i) => (
                  <tr key={`${it.name}-${i}`} className="border-t border-mast-border hover:bg-mast-bg/40">
                    <td className="px-3 py-2 text-center" title={it.status}>
                      {CHECK_ICON[it.status] ?? "?"}
                    </td>
                    <td className={`px-3 py-2 font-medium ${CHECK_TONE[it.status] ?? ""}`}>{it.name}</td>
                    <td className="px-3 py-2 text-mast-muted">{it.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      {/* ── TCP ports detail ─────────────────────────────────────────────── */}
      {nanonis && (
        <Section title={tr("TCP 端口", "TCP ports")}>
          {connection.isPending && <Spinner />}
          {connection.isError && <ErrorNote error={connection.error} />}
          {conn?.degraded && <DegradedNote what={tr("Nanonis 连接", "Nanonis connection")} />}
          {conn && !conn.degraded && (
            <Card>
              {connPorts.length === 0 ? (
                <EmptyNote label={tr("无端口信息", "No port information")} />
              ) : (
                connPorts.map((p) => (
                  <Reading
                    key={p.role}
                    label={
                      <span>
                        <span className={p.connected ? "text-mast-auto" : "text-mast-danger"}>{DOT}</span>{" "}
                        <span className="capitalize">{p.role}</span>
                        {p.port != null ? <span className="ml-1 text-mast-muted">:{p.port}</span> : null}
                      </span>
                    }
                    value={
                      <Badge tone={p.connected ? "AUTO" : "DANGEROUS"}>
                        {p.connected ? tr("已连接", "Connected") : tr("断开", "Disconnected")}
                      </Badge>
                    }
                  />
                ))
              )}
              {conn.host && (
                <p className="mt-2 text-xs text-mast-muted">
                  {tr("主机：", "Host: ")}
                  {conn.host}
                </p>
              )}
            </Card>
          )}
        </Section>
      )}

      {/* ── Per-agent models (built-in agents are a legacy feature) ──────── */}
      {showAgents && (
        <Section title={tr("各 Agent 模型", "Per-agent models")}>
          {models.isPending && <Spinner />}
          {models.isError && <ErrorNote error={models.error} />}
          {models.data?.degraded && <DegradedNote what={tr("Agent 模型注册表", "Agent model registry")} />}
          {models.data &&
            !models.data.degraded &&
            (agents.length === 0 ? (
              <EmptyNote label={tr("暂无 Agent", "No agents")} />
            ) : (
              <Card>
                {agents.map((a) => (
                  <Reading
                    key={a.agent_id}
                    label={a.agent_id}
                    value={
                      <span>
                        {a.model}
                        {a.thinking ? <span className="ml-2 text-mast-muted">{a.thinking}</span> : null}
                      </span>
                    }
                  />
                ))}
              </Card>
            ))}
        </Section>
      )}
    </div>
  );
}
