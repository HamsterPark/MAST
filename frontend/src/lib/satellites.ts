// ════════════════════════════════════════════════════════════════════════════
// GET /api/satellites and GET /api/port-guard → what the satellite and device
// status cards display (contract §5.3, §5.4, §6, §10.5; backend models live in
// mast/satellite/schemas.py).
//
// Pure functions, tested on their own (test/satellites.test.ts). This layer only
// answers "how to read", not "how to draw". Reading rules:
//   · Liveness comes only from the hub's state (connected / degraded / lost); the
//     frontend does not derive it from RTT — a derived value could differ from the
//     criterion the hub uses to reject commands, and the UI would say "connected"
//     while commands say "unavailable". A lost satellite that never connected
//     (ever_connected=false) is shown as "never connected": that is usually a
//     deployment problem, not a dropout;
//   · clock offset |θ| ≥ 500 ms is the threshold at which the HLC raises (contract
//     pitfall 11); ≥ 100 ms is flagged as a warning first;
//   · operator hold (operator_hold) is marked prominently: that device rejects all
//     hub commands (except stop), and without the marker the operator would think
//     MAST is broken;
//   · after a satellite restart or while disconnected, device state is stale
//     (contract §5.3): readings are still shown but marked stale;
//   · a field that cannot be read is "—", not 0.
// ════════════════════════════════════════════════════════════════════════════

import { tr } from "./i18n.ts";

export type Liveness = "connected" | "degraded" | "lost" | "never" | "disabled" | "unknown";

export interface DeviceInfo {
  id: string;
  kind: string;
  /** Purpose given in the hub config (satellites.json), e.g. raman_ccd / polarizer; empty string when not configured. */
  role: string;
  driver: string;
  model: string;
  serial: string;
  sim: boolean;
  /** ready / busy / offline / error / not_started / operator_hold / other */
  state: string;
  caps: string[];
  info: Record<string, unknown>;
  operatorHold: boolean;
  /** After a satellite restart / while disconnected: position and settings cannot be trusted. */
  stale: boolean;
  /** Data of the latest device_state (same shape as the status action's return). */
  status: Record<string, unknown> | null;
  /** Data of the latest axis_state. */
  axis: Record<string, unknown> | null;
  /** Timestamp of status (unix ms). */
  statusMs: number | null;
}

export interface SatelliteInfo {
  id: string;
  configured: boolean;
  enabled: boolean;
  note: string;
  state: Liveness;
  bootId: string | null;
  /** unix ms; null when not connected. */
  connectedSinceMs: number | null;
  lastSeenMs: number | null;
  rttMs: number | null;
  clockOffsetMs: number | null;
  clockSamples: number | null;
  /** Restart count recognised by the hub (boot_id changes). */
  restarts: number | null;
  pendingCommands: number | null;
  sw: string;
  host: string;
  peer: string;
  lastEstop: EstopInfo | null;
  devices: DeviceInfo[];
}

/** The hub's most recent emergency stop (last_estop) and the satellite's acknowledgement. */
export interface EstopInfo {
  reason: string;
  sentMs: number | null;
  /** null = the satellite has not acknowledged yet (or was offline when the stop was sent). */
  ack: {
    rxMs: number | null;
    /** Devices whose stop completed normally — including ones that were already idle. */
    stopped: string[];
    /** Devices that were actually moving / acquiring and were stopped by this; null when an older satellite does not report it. */
    active: string[] | null;
    /** Devices that could not be stopped, with the satellite's own message (e.g. xyz: Motion_Manipulator has no remote stop). */
    errors: Array<{ device: string; message: string }>;
  } | null;
}

export interface HubInfo {
  started: boolean;
  enabled: boolean;
  configError: string | null;
  configured: number | null;
  connected: number | null;
}

export interface SatellitesSnapshot {
  hub: HubInfo | null;
  satellites: SatelliteInfo[];
  degraded: boolean;
}

export interface PortFinding {
  kind: string;
  resource: string;
  severity: "conflict" | "warn" | "info" | string;
  process: string;
  pid: number | null;
  detail: string;
}

export interface PortGuardSnapshot {
  ok: boolean;
  enforce: boolean;
  /** False when a check could not run (insufficient permission, missing psutil, …) — "no conflict found" is not "no conflict". */
  complete: boolean;
  checkedMs: number | null;
  conflicts: PortFinding[];
  warnings: PortFinding[];
  errors: string[];
  message: string;
  degraded: boolean;
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function str(v: unknown): string {
  return typeof v === "string" ? v : typeof v === "number" ? String(v) : "";
}

/** Time: accepts unix seconds, unix milliseconds or an ISO string → unix ms. */
export function toEpochMs(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v) && v > 0) return v < 1e11 ? v * 1000 : v;
  if (typeof v === "string" && v) {
    const t = Date.parse(v);
    return Number.isFinite(t) ? t : null;
  }
  return null;
}

export function normalizeLiveness(v: unknown, enabled: boolean, everConnected = true): Liveness {
  if (!enabled) return "disabled";
  const s = str(v).toLowerCase();
  if (s === "connected" || s === "degraded") return s;
  if (s === "lost" || s === "disconnected" || s === "offline") return everConnected ? "lost" : "never";
  return "unknown";
}

function normalizeDevice(raw: unknown): DeviceInfo | null {
  if (!isObj(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  // Backend: latest = data of the latest device_state, axis = data of the latest
  // axis_state. The nested form latest.{device_state, axis_state} is also accepted
  // so that a rename in one place does not blank the whole card.
  const latest = isObj(raw.latest) ? raw.latest : null;
  const nested = latest && (isObj(latest.device_state) || isObj(latest.axis_state));
  const status = nested ? (isObj(latest!.device_state) ? latest!.device_state : null) : latest;
  const axis = isObj(raw.axis) ? raw.axis : nested && isObj(latest!.axis_state) ? latest!.axis_state : null;
  const state = str(raw.state) || str(status?.state) || "unknown";
  const hold = raw.operator_hold === true || status?.operator_hold === true || state === "operator_hold";
  return {
    id,
    kind: str(raw.kind),
    role: str(raw.role),
    driver: str(raw.driver),
    model: str(raw.model),
    serial: str(raw.serial),
    sim: raw.sim === true,
    state,
    caps: Array.isArray(raw.caps) ? raw.caps.map(str).filter(Boolean) : [],
    info: isObj(raw.info) ? raw.info : {},
    operatorHold: hold,
    stale: raw.stale === true,
    status: status as Record<string, unknown> | null,
    axis,
    statusMs: toEpochMs(raw.latest_ts_ms),
  };
}

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.map(str).filter(Boolean) : [];
}

function normalizeEstop(raw: unknown): EstopInfo | null {
  if (!isObj(raw)) return null;
  const a = isObj(raw.ack) ? raw.ack : null;
  return {
    reason: str(raw.reason),
    sentMs: toEpochMs(raw.sent_ms),
    ack: a
      ? {
          rxMs: toEpochMs(a.rx_ms),
          stopped: strList(a.stopped),
          active: Array.isArray(a.active) ? strList(a.active) : null,
          errors: (Array.isArray(a.errors) ? a.errors : [])
            .filter(isObj)
            .map((e) => ({ device: str(e.device) || "?", message: str(e.message) })),
        }
      : null,
  };
}

function normalizeSatellite(raw: unknown): SatelliteInfo | null {
  if (!isObj(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  const enabled = raw.enabled !== false;
  const counters = isObj(raw.counters) ? raw.counters : {};
  return {
    id,
    configured: raw.configured !== false,
    enabled,
    note: str(raw.note),
    state: normalizeLiveness(raw.state, enabled, raw.ever_connected !== false),
    bootId: str(raw.boot_id) || null,
    connectedSinceMs: toEpochMs(raw.connected_since),
    lastSeenMs: toEpochMs(raw.last_seen_ms),
    rttMs: num(raw.rtt_ms),
    clockOffsetMs: num(raw.clock_offset_ms),
    clockSamples: num(raw.clock_samples),
    restarts: num(counters.restarts),
    pendingCommands: num(raw.pending_commands),
    sw: str(raw.sw),
    host: str(raw.host),
    peer: str(raw.peer),
    lastEstop: normalizeEstop(raw.last_estop),
    devices: (Array.isArray(raw.devices) ? raw.devices : []).map(normalizeDevice).filter((d): d is DeviceInfo => d !== null),
  };
}

/** GET /api/satellites → snapshot. Accepts `{hub, satellites:[…]}` or a bare array; unrecognised items are dropped. */
export function normalizeSatellites(raw: unknown): SatellitesSnapshot {
  const list = Array.isArray(raw) ? raw : isObj(raw) && Array.isArray(raw.satellites) ? raw.satellites : [];
  const h = isObj(raw) && isObj(raw.hub) ? raw.hub : null;
  return {
    hub: h
      ? {
          started: h.started === true,
          enabled: h.enabled !== false,
          configError: str(h.config_error) || null,
          configured: num(h.configured),
          connected: num(h.connected),
        }
      : null,
    satellites: list.map(normalizeSatellite).filter((s): s is SatelliteInfo => s !== null),
    degraded: isObj(raw) && raw.degraded === true,
  };
}

function normalizeFinding(raw: unknown): PortFinding | null {
  if (!isObj(raw)) return null;
  const resource = str(raw.resource);
  if (!resource) return null;
  return {
    kind: str(raw.kind),
    resource,
    severity: str(raw.severity) || "warn",
    process: str(raw.process) || tr("未知进程", "Unknown process"),
    pid: num(raw.pid),
    detail: str(raw.detail),
  };
}

/** GET /api/port-guard → snapshot. */
export function normalizePortGuard(raw: unknown): PortGuardSnapshot {
  const r = isObj(raw) ? raw : {};
  const list = (v: unknown) =>
    (Array.isArray(v) ? v : []).map(normalizeFinding).filter((f): f is PortFinding => f !== null);
  return {
    ok: r.ok !== false,
    enforce: r.enforce !== false,
    complete: r.complete !== false,
    checkedMs: toEpochMs(r.checked_ms),
    conflicts: list(r.conflicts),
    warnings: list(r.warnings),
    errors: Array.isArray(r.errors) ? r.errors.map(str).filter(Boolean) : [],
    message: str(r.message),
    degraded: r.degraded === true,
  };
}

// ── display criteria ───────────────────────────────────────────────────────────────

export const LIVENESS_LABEL: Record<Liveness, string> = {
  connected: tr("已连接", "Connected"),
  degraded: tr("心跳延迟", "Heartbeat delayed"),
  lost: tr("失联", "Lost"),
  never: tr("从未连上", "Never connected"),
  disabled: tr("已停用", "Disabled"),
  unknown: tr("状态未知", "Unknown"),
};

export function livenessTone(s: Liveness): string {
  if (s === "connected") return "AUTO";
  if (s === "degraded") return "WARN";
  if (s === "lost") return "DANGEROUS";
  return "INFO";
}

/** Clock-offset level: < 100 ms normal; 100–500 ms warning; ≥ 500 ms makes the HLC raise. */
export function clockOffsetLevel(ms: number | null): "ok" | "warn" | "bad" | "unknown" {
  if (ms === null) return "unknown";
  const a = Math.abs(ms);
  if (a >= 500) return "bad";
  if (a >= 100) return "warn";
  return "ok";
}

export const DEVICE_STATE_LABEL: Record<string, string> = {
  ready: tr("就绪", "Ready"),
  busy: tr("忙", "Busy"),
  offline: tr("离线", "Offline"),
  error: tr("故障", "Error"),
  not_started: tr("未启动", "Not started"),
  operator_hold: tr("人工接管", "Operator hold"),
  unknown: tr("未知", "Unknown"),
};

export function deviceStateTone(state: string): string {
  if (state === "ready") return "AUTO";
  if (state === "busy") return "INFO";
  if (state === "error") return "DANGEROUS";
  if (state === "offline" || state === "operator_hold") return "WARN";
  return "default";
}

export const DEVICE_KIND_LABEL: Record<string, string> = {
  spectrometer: tr("光谱仪", "Spectrometer"),
  rotation_stage: tr("旋转台（偏振）", "Rotation stage (polarisation)"),
  openloop_stepper: tr("开环步进（Picomotor）", "Open-loop stepper (Picomotor)"),
  xyz_stage: tr("XYZ 台", "XYZ stage"),
  aotf: "AOTF",
  pmt: "PMT",
  file_watcher: tr("文件监视", "File watcher"),
};

export interface Readout {
  label: string;
  value: string;
  /** Note next to the readout (e.g. "accumulated step count, not position feedback"). */
  note?: string;
  moving?: boolean;
}

function fixed(v: unknown, digits: number, unit: string): string {
  const n = num(v);
  return n === null ? "—" : `${n.toFixed(digits)}${unit}`;
}

/**
 * Axis position readouts of one device (polarisation angle, XYZ, Picomotor steps).
 * axis_state is preferred when it is newer than device_state (≤ 20 Hz while moving).
 */
export function axisReadouts(d: DeviceInfo): Readout[] {
  const s = { ...(d.status ?? {}), ...(d.axis ?? {}) };
  const moving = s.moving === true;
  switch (d.kind) {
    case "rotation_stage":
      return [
        {
          label: tr("角度", "Angle"),
          value: fixed(s.position_deg, 3, "°"),
          moving,
          note: s.homed === false ? tr("未回零：读数没有意义", "Not homed: the reading is meaningless") : undefined,
        },
      ];
    case "xyz_stage":
      return (["x", "y", "z"] as const).map((k) => ({ label: k.toUpperCase(), value: fixed(s[`${k}_mm`], 4, " mm"), moving }));
    case "openloop_stepper": {
      const axes = isObj(s.axes) ? s.axes : {};
      const out: Readout[] = Object.keys(axes)
        .sort()
        .map((k) => {
          const a = isObj(axes[k]) ? (axes[k] as Record<string, unknown>) : {};
          return { label: tr(`轴 ${k}`, `Axis ${k}`), value: fixed(a.steps, 0, tr(" 步", " steps")), moving: a.moving === true };
        });
      if (out.length) out[0]!.note = tr("卫星累计的步数，不是位置反馈", "Step count accumulated by the satellite, not position feedback");
      return out;
    }
    default:
      return [];
  }
}

/** The spectrometer status fields worth showing permanently. */
export function spectrometerReadouts(d: DeviceInfo): Readout[] {
  const s = d.status ?? {};
  const out: Readout[] = [];
  const temp = num(s.temperature_c);
  out.push({
    label: tr("CCD 温度", "CCD temperature"),
    value: temp === null ? "—" : `${temp.toFixed(1)} °C`,
    note:
      s.temperature_locked === true
        ? tr("已锁定", "Locked")
        : s.temperature_locked === false
          ? tr("未锁定", "Not locked")
          : undefined,
  });
  out.push({ label: tr("曝光", "Exposure"), value: fixed(s.exposure_ms, 0, " ms") });
  out.push({ label: tr("帧数", "Frames"), value: fixed(s.frames, 0, "") });
  out.push({ label: tr("光栅", "Grating"), value: str(s.grating) || "—" });
  out.push({ label: tr("中心波长", "Center wavelength"), value: fixed(s.center_wavelength_nm, 2, " nm") });
  out.push({ label: tr("出口", "Exit port"), value: str(s.exit_port) || "—" });
  out.push({ label: tr("激发线", "Excitation line"), value: fixed(s.laser_line_nm, 1, " nm") });
  if (s.is_running === true) out.push({ label: tr("采集", "Acquisition"), value: tr("进行中", "In progress"), moving: true });
  return out;
}

/**
 * Whether this device is moving / acquiring right now: stages use moving (Picomotor
 * checks each axis), the spectrometer uses is_running. A state of "ready" is not
 * used as a fallback: a stage without remote stop can be started from its own
 * window without the satellite's state knowing. When the link is down or the
 * reading is stale the answer is unknown (null) — a last-known "not moving" must
 * not be used to clear the emergency-stop warning.
 */
export function deviceActive(d: DeviceInfo, linkUp: boolean): boolean | null {
  if (!linkUp || d.stale) return null;
  const s = { ...(d.status ?? {}), ...(d.axis ?? {}) };
  if (typeof s.moving === "boolean") return s.moving;
  const axes = isObj(s.axes) ? Object.values(s.axes).filter(isObj) : [];
  if (axes.length && axes.every((a) => typeof a.moving === "boolean")) return axes.some((a) => a.moving === true);
  if (typeof s.is_running === "boolean") return s.is_running;
  return null;
}

export interface EstopLeftover {
  device: string;
  /** The satellite's own message (e.g. "no remote stop, press STOP in its own window"). */
  message: string;
  /** Now: true = still moving, false = not moving, null = cannot tell. */
  active: boolean | null;
}

/**
 * Devices the satellite reported as not stopped in the last emergency stop, and
 * their current state. On a real satellite the xyz stage (Motion_Manipulator) has
 * no remote stop and appears here after every emergency stop — if it was idle
 * there is no need to alarm anyone, hence the check on whether it is moving now.
 */
export function estopLeftovers(s: SatelliteInfo): EstopLeftover[] {
  const linkUp = s.state === "connected" || s.state === "degraded";
  return (s.lastEstop?.ack?.errors ?? []).map((e) => {
    const d = s.devices.find((x) => x.id === e.device);
    return { device: e.device, message: e.message, active: d ? deviceActive(d, linkUp) : null };
  });
}

/** Warnings for the top of the status card: lost, operator hold, stale, excessive clock offset, not stopped by e-stop, port taken, bad hub config. */
export function attentionItems(snap: SatellitesSnapshot, guard: PortGuardSnapshot | null = null): string[] {
  const out: string[] = [];
  if (snap.hub?.configError) out.push(tr(`卫星枢纽配置有误：${snap.hub.configError}`, `Satellite hub configuration error: ${snap.hub.configError}`));
  for (const s of snap.satellites) {
    if (!s.enabled) continue;
    if (s.state === "lost") out.push(
        tr(
          `卫星 ${s.id} 失联：发给它的新命令会直接失败（unavailable）`,
          `Satellite ${s.id} is lost: new commands sent to it fail immediately (unavailable)`,
        ),
      );
    if (s.state === "never") out.push(
        tr(
          `卫星 ${s.id} 从未连上过：检查卫星程序是否在运行、令牌与证书钉值`,
          `Satellite ${s.id} has never connected: check that the satellite program is running, and its token and certificate pin`,
        ),
      );
    if (s.state === "degraded") out.push(tr(`卫星 ${s.id} 心跳延迟（3–10 s 没有回音）`, `Satellite ${s.id} heartbeat delayed (no reply for 3–10 s)`));
    if (clockOffsetLevel(s.clockOffsetMs) === "bad") {
      out.push(
        tr(
          `卫星 ${s.id} 时钟偏差 ${Math.round(s.clockOffsetMs!)} ms，超过 500 ms`,
          `Satellite ${s.id} clock offset ${Math.round(s.clockOffsetMs!)} ms, above 500 ms`,
        ),
      );
    }
    for (const d of s.devices) {
      if (d.operatorHold) out.push(
          tr(
            `${s.id}/${d.id} 处于人工接管：MAST 的命令会被拒绝（停止除外）`,
            `${s.id}/${d.id} is under operator hold: MAST commands are rejected (except stop)`,
          ),
        );
    }
    // Clear the warning only when the device is read as not moving; an uncertain
    // state (disconnected, stale, no reading) keeps it lit.
    for (const e of estopLeftovers(s)) {
      if (e.active === false) continue;
      out.push(
        tr(
          `急停时 ${s.id}/${e.device} 没停下来${e.active ? "，现在还在动" : ""}：${e.message.slice(0, 200) || "卫星没给原因"} —— 请到它自己的控制界面上停下它`,
          `${s.id}/${e.device} did not stop on emergency stop${e.active ? " and is still moving" : ""}: ${e.message.slice(0, 200) || "the satellite gave no reason"} — stop it from its own control interface`,
        ),
      );
    }
  }
  // Port conflicts are listed one by one in the "Port ownership" section below;
  // here they only need to be visible — several are summarised in one line so
  // the alert box does not fill up.
  const conflicts = guard?.conflicts ?? [];
  const c = conflicts[0];
  if (c) {
    const first = tr(
      `${c.resource} 被 ${c.process}${c.pid !== null ? `（pid ${c.pid}）` : ""} 占用`,
      `${c.resource} is in use by ${c.process}${c.pid !== null ? ` (pid ${c.pid})` : ""}`,
    );
    out.push(
      conflicts.length === 1
        ? `${first}${c.detail ? `${tr("：", ": ")}${c.detail}` : ""}`
        : tr(
            `端口归属有 ${conflicts.length} 处冲突：${first} 等（逐条见下面「端口归属」）`,
            `${conflicts.length} port ownership conflicts: ${first}, … (see "Port ownership" below for each)`,
          ),
    );
  }
  return out;
}

// ── hardware module ters_optics (GET /api/settings/hardware-modules) ───────────────────

/** Hardware module that the TERS optics skills belong to (skills/hardware_modules.py). */
export const TERS_OPTICS_MODULE = "ters_optics";

/** Whether a module is on; unreadable (older backend without the module, failed request) = null, not treated as "off". */
export function moduleEnabled(
  data: { modules?: Array<{ id: string; enabled: boolean }> } | null | undefined,
  id: string,
): boolean | null {
  const m = data?.modules?.find((x) => x.id === id);
  return m ? m.enabled : null;
}
