// ════════════════════════════════════════════════════════════════════════════
// Description of the manual-control panel (contract §13.1): GET /api/manual/devices -> which axes
// and buttons each motorised device has.
//
// The panel is **rendered entirely from the description** and the frontend hard-codes no device
// list: which device, which button, which skill is behind it, and which parameters (names, units,
// ranges taken verbatim from the skill's ParameterSpec) all come from the backend. This layer does
// three things only:
//   - read the response into a stable shape (safe defaults for missing fields, unrecognised
//     entries dropped, never throws);
//   - group by machine (stages on the STM computer, Nanonis optics groups, each satellite, the
//     alignment camera);
//   - assemble "button + user-entered values + direction + axis" into skill parameters, rejecting
//     obvious mistakes before sending.
// Limits, interlocks and tip requirements are **decided** on the server; the range checks here
// only report earlier.
// ════════════════════════════════════════════════════════════════════════════

import { tr } from "../i18n.ts";

export interface ManualParamSpec {
  name: string;
  /** float / int / str / bool (the type of the skill's ParameterSpec). */
  type: string;
  unit: string | null;
  min: number | null;
  max: number | null;
  default: unknown;
  required: boolean;
  description: string;
  /** Enumeration (e.g. direction "+" / "-", port front / side). */
  allowed: string[] | null;
}

export interface ManualAction {
  /** jog / move_abs / move_rel / home / stop / set_velocity / … / camera_connect / live_start / snapshot … */
  id: string;
  label: string;
  skill: string;
  fixed: Record<string, unknown>;
  params: ManualParamSpec[];
  /** Per-axis action: one row per axis, the axis name goes into axisParam. */
  perAxis: boolean;
  axisParam: string;
  direction: "none" | "signed";
  /** For signed actions, the parameter carrying the direction: a numeric one takes +/- magnitude, a "+"/"-" enum is filled by the button. */
  directionParam: string | null;
  /** auto / confirm / dangerous (the skill's safety_level). */
  safety: string;
  /** Token-free stop action (instrument_lock.BYPASS_NAMES). */
  bypassLock: boolean;
  /** Holding to repeat jogs is allowed only when set: at least this long between jogs, and the previous one must have returned. */
  holdRepeatMs: number | null;
  /** Send one stop on release. */
  stopOnRelease: boolean;
  /** What this button actually does (the real effect of the vendor call, e.g. "stops all motor groups, including the STM coarse group"). */
  hint: string | null;
}

export interface ManualAxis {
  name: string;
  unit: string;
  min: number | null;
  max: number | null;
  position: number | null;
  moving: boolean | null;
  homed: boolean | null;
  extra: Record<string, unknown>;
}

/**
 * Where readouts come from (contract §13.1): satellite devices subscribe to a LiveHub stream;
 * local devices poll the batch read-only endpoint GET /api/manual/readouts?ids=... (**not through
 * the skill-execution entry**, which records one action per execution);
 * null = no position feedback (e.g. Nanonis optics groups only have commanded step counts).
 */
export type ManualReadout = { kind: "live"; stream: string } | { kind: "poll"; intervalS: number } | null;

export interface ManualDevice {
  /** local/stage/<device_id>、local/nanonis_group/<tcp_group>、local/camera/<camera_id>、<satellite_id>/<device_id> */
  id: string;
  /** "local" or a satellite id. */
  machine: string;
  kind: string;
  label: string;
  vendor: string;
  model: string;
  serial: string;
  available: boolean;
  unavailableReason: string | null;
  /** Filled only for a holder with evidence; null = unknown, **which does not mean idle**. */
  occupiedBy: string | null;
  tipRequirement: string | null;
  axes: ManualAxis[];
  actions: ManualAction[];
  readout: ManualReadout;
}

export interface ManualSnapshot {
  devices: ManualDevice[];
  degraded: boolean;
  /** The categories that could not be read (e.g. ["satellite"]). */
  degradedParts: string[];
  notes: string[];
}

// ── Reading ────────────────────────────────────────────────────────────────

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
function str(v: unknown): string {
  return typeof v === "string" ? v : typeof v === "number" ? String(v) : "";
}
function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}
function bool(v: unknown): boolean | null {
  return typeof v === "boolean" ? v : null;
}
function strOrNull(v: unknown): string | null {
  const s = str(v).trim();
  return s ? s : null;
}

function normalizeParam(raw: unknown): ManualParamSpec | null {
  if (!isObj(raw)) return null;
  const name = str(raw.name);
  if (!name) return null;
  const allowed = raw.allowed_values ?? raw.allowed;
  return {
    name,
    type: str(raw.type) || "str",
    unit: strOrNull(raw.unit),
    min: num(raw.min ?? raw.min_value),
    max: num(raw.max ?? raw.max_value),
    default: raw.default ?? null,
    required: raw.required === true,
    description: str(raw.description),
    allowed: Array.isArray(allowed) ? allowed.map(str).filter(Boolean) : null,
  };
}

function normalizeAction(raw: unknown): ManualAction | null {
  if (!isObj(raw)) return null;
  const skill = str(raw.skill);
  const id = str(raw.id);
  if (!skill || !id) return null;
  const hold = num(raw.hold_repeat_ms);
  return {
    id,
    label: str(raw.label) || id,
    skill,
    fixed: isObj(raw.fixed) ? raw.fixed : {},
    params: (Array.isArray(raw.params) ? raw.params : []).map(normalizeParam).filter((p): p is ManualParamSpec => p !== null),
    perAxis: raw.per_axis === true,
    axisParam: str(raw.axis_param) || "axis",
    direction: raw.direction === "signed" ? "signed" : "none",
    directionParam: strOrNull(raw.direction_param),
    safety: (str(raw.safety) || "confirm").toLowerCase(),
    bypassLock: raw.bypass_lock === true,
    holdRepeatMs: hold !== null && hold > 0 ? hold : null,
    stopOnRelease: raw.stop_on_release === true,
    hint: strOrNull(raw.hint),
  };
}

function normalizeAxis(raw: unknown): ManualAxis | null {
  if (!isObj(raw)) return null;
  const name = str(raw.name);
  if (!name) return null;
  return {
    name,
    unit: str(raw.unit),
    min: num(raw.min),
    max: num(raw.max),
    position: num(raw.position),
    moving: bool(raw.moving),
    homed: bool(raw.homed),
    extra: isObj(raw.extra) ? raw.extra : {},
  };
}

/** Local readouts are polled no faster than this (one request covers all visible local cards, and each card reads the hardware every time). */
export const MIN_POLL_S = 0.5;

function normalizeReadout(raw: unknown): ManualReadout {
  if (!isObj(raw)) return null;
  if (raw.kind === "live" && str(raw.stream)) return { kind: "live", stream: str(raw.stream) };
  if (raw.kind === "poll") {
    const iv = num(raw.interval_s);
    return { kind: "poll", intervalS: iv !== null && iv >= MIN_POLL_S ? iv : 1 };
  }
  return null;
}

function normalizeDevice(raw: unknown): ManualDevice | null {
  if (!isObj(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  const machine = str(raw.machine) || id.split("/")[0] || "local";
  return {
    id,
    machine,
    kind: str(raw.kind),
    label: str(raw.label) || id,
    vendor: str(raw.vendor),
    model: str(raw.model),
    serial: str(raw.serial),
    available: raw.available !== false,
    unavailableReason: strOrNull(raw.unavailable_reason),
    occupiedBy: strOrNull(raw.occupied_by),
    tipRequirement: strOrNull(raw.tip_requirement),
    axes: (Array.isArray(raw.axes) ? raw.axes : []).map(normalizeAxis).filter((a): a is ManualAxis => a !== null),
    actions: (Array.isArray(raw.actions) ? raw.actions : []).map(normalizeAction).filter((a): a is ManualAction => a !== null),
    readout: normalizeReadout(raw.readout),
  };
}

/**
 * GET /api/manual/devices -> snapshot. Which categories could not be read is in `degraded_sources`
 * (local / camera / satellite); `degraded` also accepts the older list of category names and the
 * {category: reason} form.
 */
export function normalizeManualDevices(raw: unknown): ManualSnapshot {
  const r = isObj(raw) ? raw : {};
  const devices = (Array.isArray(r.devices) ? r.devices : []).map(normalizeDevice).filter((d): d is ManualDevice => d !== null);
  const notes = Array.isArray(r.notes) ? r.notes.map(str).filter(Boolean) : [];
  const parts: string[] = Array.isArray(r.degraded_sources) ? r.degraded_sources.map(str).filter(Boolean) : [];
  if (Array.isArray(r.degraded)) parts.push(...r.degraded.map(str).filter(Boolean));
  else if (isObj(r.degraded)) {
    parts.push(...Object.keys(r.degraded));
    for (const [k, v] of Object.entries(r.degraded)) if (str(v)) notes.push(tr(`${k}：${str(v)}`, `${k}: ${str(v)}`));
  }
  const degradedParts = [...new Set(parts)];
  return { devices, degraded: r.degraded === true || degradedParts.length > 0, degradedParts, notes };
}

/** Wording for degraded_sources. */
export const DEGRADED_SOURCE_LABEL: Record<string, string> = {
  local: tr("STM 电脑上的台子与 Nanonis 光学组", "stages on the STM computer and Nanonis optics groups"),
  camera: tr("对光相机", "alignment camera"),
  satellite: tr("卫星", "satellites"),
};

// ── Grouping ───────────────────────────────────────────────────────────────

export interface DeviceGroup {
  key: string;
  title: string;
  subtitle: string;
  devices: ManualDevice[];
}

function groupKey(d: ManualDevice): string {
  if (d.kind === "camera") return "camera";
  if (d.kind === "nanonis_motor_group") return "nanonis";
  if (d.machine !== "local") return `sat:${d.machine}`;
  return "local";
}

/** Groups by machine in a fixed order: stages on the STM computer -> Nanonis optics groups -> each satellite (by id) -> alignment camera. */
export function groupDevices(devices: ManualDevice[]): DeviceGroup[] {
  const by = new Map<string, ManualDevice[]>();
  for (const d of devices) {
    const k = groupKey(d);
    by.set(k, [...(by.get(k) ?? []), d]);
  }
  const sats = [...by.keys()].filter((k) => k.startsWith("sat:")).sort();
  const order = ["local", "nanonis", ...sats, "camera"];
  const out: DeviceGroup[] = [];
  for (const k of order) {
    const list = by.get(k);
    if (!list?.length) continue;
    if (k === "local") out.push({ key: k, title: tr("STM 电脑上的光学台", "Optical stages on the STM computer"), subtitle: tr("本机控制器（MAST 直接驱动）", "Local controllers (driven directly by MAST)"), devices: list });
    else if (k === "nanonis")
      out.push({ key: k, title: tr("Nanonis 光学组", "Nanonis optics groups"), subtitle: tr("PMD4 上标为光学用途的电机组（STM 粗动组不在这里）", "Motor groups on the PMD4 marked for optics (the STM coarse group is not here)"), devices: list });
    else if (k === "camera") out.push({ key: k, title: tr("对光相机", "Alignment camera"), subtitle: tr("聚焦光斑用（MAST 默认不开相机，按「连接」才打开）", "For focusing the spot (MAST does not open the camera by default; press Connect to open it)"), devices: list });
    else {
      const sat = k.slice(4);
      out.push({ key: k, title: tr(`光谱电脑（卫星 ${sat}）`, `Spectrometer computer (satellite ${sat})`), subtitle: tr("经卫星执行，枢纽只发命令", "Executed through the satellite; the hub only sends commands"), devices: list });
    }
  }
  return out;
}

export const MANUAL_KIND_LABEL: Record<string, string> = {
  linear_stage: tr("直线台", "Linear stage"),
  rotation_stage: tr("旋转台", "Rotation stage"),
  openloop_stepper: tr("开环步进", "Open-loop stepper"),
  xyz_stage: tr("XYZ 台", "XYZ stage"),
  nanonis_motor_group: tr("Nanonis 电机组", "Nanonis motor group"),
  camera: tr("相机", "Camera"),
  spectrometer: tr("光谱仪", "Spectrometer"),
  aotf: "AOTF",
};

export const TIP_REQUIREMENT_LABEL: Record<string, string> = {
  tip_withdrawn: tr("须已退针", "must be withdrawn"),
  z_feedback_on: tr("须 Z 反馈开着", "Z feedback must be on"),
  none: tr("不查针尖", "tip not checked"),
};

// ── Button -> skill parameters ─────────────────────────────────────────────

/** Parameters the user fills next to a button: the axis parameter of per-axis actions and a direction enum decided by the button are not asked for. */
export function inputParams(a: ManualAction): ManualParamSpec[] {
  return a.params.filter((p) => {
    if (a.perAxis && p.name === a.axisParam) return false;
    if (a.direction === "signed" && p.name === a.directionParam && isSignEnum(p)) return false;
    return !(p.name in a.fixed);
  });
}

function isSignEnum(p: ManualParamSpec): boolean {
  return !!p.allowed && p.allowed.includes("+") && p.allowed.includes("-");
}

function fmtNum(v: number): string {
  return Number.isInteger(v) ? String(v) : String(+v.toPrecision(6));
}

function rangeText(p: ManualParamSpec): string {
  const u = p.unit ? ` ${p.unit}` : "";
  if (p.min !== null && p.max !== null) return `${fmtNum(p.min)} … ${fmtNum(p.max)}${u}`;
  if (p.min !== null) return `≥ ${fmtNum(p.min)}${u}`;
  if (p.max !== null) return `≤ ${fmtNum(p.max)}${u}`;
  return "";
}

/**
 * Description of the per-step limit (listed in the unlock confirmation). The real limit written on
 * the axis takes priority, because the skill's ParamSpec range is not necessarily this device's
 * limit (the steps of a Nanonis optics group is 1...10000 in the skill, while the group's limit is
 * in axes[].extra.max_steps_per_cmd):
 *   - extra.max_steps_per_cmd -> "per step <= N steps"; extra.max_step (a number) -> "per step <= X unit";
 *   - extra explicitly has max_step: null -> "no separate limit (bounded by soft limits min ... max)";
 *   - only when the axis says nothing, fall back to the ParamSpec ranges of the motion parameters.
 */
export function limitLines(d: ManualDevice): string[] {
  const out: string[] = [];
  for (const ax of d.axes) {
    const per = num(ax.extra.max_steps_per_cmd);
    const step = num(ax.extra.max_step);
    const u = ax.unit ? ` ${ax.unit}` : "";
    if (per !== null) out.push(tr(`${ax.name}：单次 ≤ ${fmtNum(per)} 步`, `${ax.name}: per step ≤ ${fmtNum(per)} steps`));
    else if (step !== null) out.push(tr(`${ax.name}：单次 ≤ ${fmtNum(step)}${u}`, `${ax.name}: per step ≤ ${fmtNum(step)}${u}`));
    else if ("max_step" in ax.extra) {
      const soft = ax.min !== null || ax.max !== null ? tr(`（受软限位 ${ax.min ?? "−∞"} … ${ax.max ?? "∞"}${u}）`, ` (bounded by soft limits ${ax.min ?? "−∞"} … ${ax.max ?? "∞"}${u})`) : "";
      out.push(tr(`${ax.name}：无单独上限${soft}`, `${ax.name}: no separate limit${soft}`));
    }
  }
  if (out.length) return out;
  for (const a of d.actions) {
    if (a.bypassLock || a.safety === "auto") continue;
    for (const p of inputParams(a)) {
      const r = rangeText(p);
      if (r && (p.type === "float" || p.type === "int")) out.push(tr(`${a.label}：${p.name} ${r}`, `${a.label}: ${p.name} ${r}`));
    }
  }
  return [...new Set(out)];
}

/**
 * Each axis's extra in a readout -> a line of small text (only what the operator needs; unrecognised
 * scalars are listed as they are, not dropped). tone = "warn" must stand out (not connected, drive
 * latched, errors).
 */
/** Drive policy of a Nanonis optics group (drive_policy in instruments/motor_groups.py). */
const DRIVE_POLICY_LABEL: Record<string, string> = {
  verify_only: tr("只核对驱动（读回与配置一致才走步，不写）", "verify the drive only (steps only if the read-back matches the configuration; nothing is written)"),
  write_and_restore: tr("写入配置的驱动、走完还原", "write the configured drive, restore it after the move"),
};

/**
 * PDXC2 error bits (the English wording in the vendor header's PDXC2_GetStatusBits table) -> a
 * translation. In Chinese the original English wording is shown in parentheses so it can be looked
 * up in the vendor manual; entries outside the table are shown as is.
 */
export const PDXC2_ERROR_ZH: Record<string, string> = {
  "device in wrong mode": tr("设备工作模式不对", "Device in wrong mode"),
  "excessive current": tr("电流过大", "Excessive current"),
  "excessive temperature": tr("温度过高", "Excessive temperature"),
  "abnormal stage movement": tr("台子运动异常", "Abnormal stage movement"),
  "wrong stage detected": tr("检测到的台子型号不对", "Wrong stage detected"),
  error: tr("错误（厂家未细分）", "Error (not detailed by the vendor)"),
};

function errorText(e: unknown): string {
  const en = str(e);
  if (!en) return JSON.stringify(e);
  const zh = PDXC2_ERROR_ZH[en.trim().toLowerCase()];
  return zh ? tr(`${zh}（${en}）`, zh) : en;
}

export function axisExtraBits(extra: Record<string, unknown>, unit = ""): Array<{ text: string; tone?: "warn" }> {
  const out: Array<{ text: string; tone?: "warn" }> = [];
  const used = new Set<string>();
  const take = (k: string) => {
    used.add(k);
    return extra[k];
  };
  const c = take("connected");
  if (c === false)
    out.push({ text: tr("未连接（MAST 还没打开它，读数不会去打开它；点「连接」或直接操作都会打开它，厂家软件占着时会失败并说是谁）", "Not connected (MAST has not opened it and reading does not open it; pressing Connect or operating it directly does, and fails with the holder's name if the vendor software has it)"), tone: "warn" });
  const latched = take("drive_latched");
  if (latched === true) out.push({ text: tr("驱动锁存：驱动值没还原，所有粗动都会被拒（去 Nanonis 面板核对）", "Drive latched: the drive values were not restored and every coarse move will be refused (check the Nanonis panel)"), tone: "warn" });
  const odo = num(take("odometer_steps"));
  if (odo !== null) out.push({ text: tr(`命令过 ${fmtNum(odo)} 步（不是位置）`, `${fmtNum(odo)} steps commanded (not a position)`) });
  // Configuration of a Nanonis optics group: the Nanonis axis name, the configured drive (frequency / amplitude) and the drive policy
  const nax = str(take("nanonis_axis"));
  if (nax) out.push({ text: tr(`Nanonis 轴 ${nax}`, `Nanonis axis ${nax}`) });
  const fhz = num(take("freq_hz"));
  const av = num(take("amp_v"));
  if (fhz !== null || av !== null) out.push({ text: tr(`配置的驱动 ${fhz !== null ? `${fmtNum(fhz)} Hz` : "—"} / ${av !== null ? `${fmtNum(av)} V` : "—"}`, `Configured drive ${fhz !== null ? `${fmtNum(fhz)} Hz` : "—"} / ${av !== null ? `${fmtNum(av)} V` : "—"}`) });
  const pol = str(take("drive_policy"));
  if (pol) out.push({ text: DRIVE_POLICY_LABEL[pol] ?? tr(`驱动策略 ${pol}`, `Drive policy ${pol}`) });
  take("chunk_steps");
  take("position_feedback");
  const servo = take("servo");
  if (typeof servo === "boolean") out.push({ text: servo ? tr("伺服开", "Servo on") : tr("伺服关", "Servo off") });
  const vel = num(take("velocity"));
  const vu = str(take("velocity_unit"));
  if (vel !== null) out.push({ text: tr(`速度 ${fmtNum(vel)}${vu ? ` ${vu}` : ""}`, `Speed ${fmtNum(vel)}${vu ? ` ${vu}` : ""}`) });
  const now = num(take("speed_now"));
  if (now !== null) out.push({ text: tr(`当前速度 ${fmtNum(now)}${vu ? ` ${vu}` : ""}`, `Current speed ${fmtNum(now)}${vu ? ` ${vu}` : ""}`) });
  const zf = take("zero_found");
  if (typeof zf === "boolean") out.push(zf ? { text: tr("已找零", "Zero found") } : { text: tr("未找零", "Zero not found"), tone: "warn" });
  const cl = take("closed_loop");
  if (typeof cl === "boolean") out.push({ text: cl ? tr("闭环", "Closed loop") : tr("开环", "Open loop") });
  const onTarget = take("on_target");
  if (onTarget === false) out.push({ text: tr("没到位", "Not on target") });
  const fb = take("feedback");
  if (fb === false) out.push({ text: tr("开环：计数是命令过的步数，不是位置", "Open loop: the count is the number of steps commanded, not a position") });
  else if (fb === true) out.push({ text: tr("有位置反馈", "Has position feedback") });
  if (take("homing") === true) out.push({ text: tr("寻零中", "Homing") });
  if (take("moving_forward") === true) out.push({ text: tr("正向运动中", "Moving forward") });
  if (take("moving_backward") === true) out.push({ text: tr("反向运动中", "Moving backward") });
  const errs = take("errors");
  if (Array.isArray(errs) && errs.length) out.push({ text: tr(`错误：${errs.map(errorText).join("；")}`, `Errors: ${errs.map(errorText).join("; ")}`), tone: "warn" });
  const lo = num(take("travel_min_controller"));
  const hi = num(take("travel_max_controller"));
  if (lo !== null || hi !== null) out.push({ text: tr(`控制器行程 ${lo ?? "—"} … ${hi ?? "—"}`, `Controller travel ${lo ?? "—"} … ${hi ?? "—"}`) });
  if (take("has_limit_switches") === true) out.push({ text: tr("有限位开关", "Has limit switches") });
  if (take("has_reference_switch") === true) out.push({ text: tr("有参考开关", "Has reference switch") });
  // Status bits (PDXC2 gives hexadecimal text); errors without status bits = the status word could not be read, which must not be taken as "no errors"
  const hasBitsKey = "status_bits" in extra || Array.isArray(errs);
  const rawBits = take("status_bits");
  const bitsText = typeof rawBits === "string" && rawBits.trim() ? rawBits.trim() : num(rawBits) !== null ? `0x${Math.trunc(rawBits as number).toString(16)}` : null;
  if (bitsText) out.push({ text: tr(`状态位 ${bitsText}`, `Status bits ${bitsText}`) });
  else if (hasBitsKey && c !== false) out.push({ text: tr("状态位读不到", "Status bits unreadable"), tone: "warn" });
  const mode = str(take("control_mode"));
  if (mode) out.push({ text: tr(`模式 ${mode}`, `Mode ${mode}`) });
  const trig = take("external_trigger");
  if (trig !== undefined && trig !== null && trig !== "") out.push({ text: tr(`外触发 ${typeof trig === "object" ? JSON.stringify(trig) : String(trig)}`, `External trigger ${typeof trig === "object" ? JSON.stringify(trig) : String(trig)}`) });
  const vmax = num(take("max_velocity"));
  if (vmax !== null) out.push({ text: tr(`速度上限 ${fmtNum(vmax)}${unit ? ` ${unit}/s` : ""}`, `Max speed ${fmtNum(vmax)}${unit ? ` ${unit}/s` : ""}`) });
  // Role label, channel number and per-step limit from the configuration (the limit is listed in the unlock confirmation): not shown again
  take("role");
  take("channel");
  take("max_step");
  take("max_steps_per_cmd");
  for (const [k, v] of Object.entries(extra)) {
    if (used.has(k)) continue;
    if (typeof v === "number" || typeof v === "string" || typeof v === "boolean") out.push({ text: `${k} ${String(v)}` });
  }
  return out;
}

export type BuildResult = { ok: true; params: Record<string, unknown> } | { ok: false; error: string };

/**
 * Assembles the parameters of one execution: fixed + axis + user-entered values + direction. Only
 * obvious mistakes are rejected (not a number, outside the range the skill declares, a required
 * value missing); the real limits and interlocks are on the server.
 */
export function buildActionParams(
  a: ManualAction,
  opts: { axis?: string | null; values: Record<string, string>; sign?: 1 | -1 },
): BuildResult {
  const out: Record<string, unknown> = { ...a.fixed };
  if (a.perAxis) {
    if (!opts.axis) return { ok: false, error: tr("缺轴名", "Missing axis name") };
    out[a.axisParam] = opts.axis;
  }
  const signed = a.direction === "signed";
  if (signed && !opts.sign) return { ok: false, error: tr("缺方向", "Missing direction") };
  for (const p of a.params) {
    if (p.name in a.fixed || (a.perAxis && p.name === a.axisParam)) continue;
    if (signed && p.name === a.directionParam && isSignEnum(p)) {
      out[p.name] = opts.sign === -1 ? "-" : "+";
      continue;
    }
    const rawText = (opts.values[p.name] ?? "").trim();
    if (!rawText) {
      if (p.required && p.default == null) return { ok: false, error: tr(`填 ${p.name}${p.unit ? `（${p.unit}）` : ""}`, `Enter ${p.name}${p.unit ? ` (${p.unit})` : ""}`) };
      continue;
    }
    let v: unknown = rawText;
    if (p.type === "float" || p.type === "int") {
      const n = Number(rawText);
      if (!Number.isFinite(n)) return { ok: false, error: tr(`${p.name} 要是数`, `${p.name} must be a number`) };
      if (p.type === "int" && !Number.isInteger(n)) return { ok: false, error: tr(`${p.name} 要是整数`, `${p.name} must be an integer`) };
      let val = n;
      if (signed && p.name === a.directionParam) {
        if (n < 0) return { ok: false, error: tr(`${p.name} 填正数，方向由按钮决定`, `Enter a positive ${p.name}; the button decides the direction`) };
        val = (opts.sign ?? 1) * n;
      }
      if ((p.min !== null && val < p.min) || (p.max !== null && val > p.max)) {
        return { ok: false, error: tr(`${p.name} = ${fmtNum(val)} 超出 ${rangeText(p)}`, `${p.name} = ${fmtNum(val)} is outside ${rangeText(p)}`) };
      }
      v = val;
    } else if (p.type === "bool") {
      if (!["true", "false"].includes(rawText.toLowerCase())) return { ok: false, error: tr(`${p.name} 填 true / false`, `${p.name} must be true / false`) };
      v = rawText.toLowerCase() === "true";
    } else if (p.allowed && !p.allowed.includes(rawText)) {
      return { ok: false, error: tr(`${p.name} 只能是 ${p.allowed.join(" / ")}`, `${p.name} must be one of ${p.allowed.join(" / ")}`) };
    }
    out[p.name] = v;
  }
  return { ok: true, params: out };
}

/** Initial value of an input: the default the skill declares. */
export function defaultValues(a: ManualAction): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of inputParams(a)) {
    if (p.default !== null && p.default !== undefined && typeof p.default !== "object") out[p.name] = String(p.default);
  }
  return out;
}

export type Gate = "run" | "confirm";

/**
 * Whether this action needs confirmation first: stop actions always run directly (no dialog, ignore
 * the lock); read-only (auto) runs directly; DANGEROUS is confirmed every time (backend HITL
 * unchanged); CONFIRM runs directly while unlocked and is confirmed one by one otherwise.
 */
export function actionGate(a: ManualAction, unlocked: boolean): Gate {
  if (a.bypassLock || a.id === "stop" || a.id === "abort") return "run";
  if (a.safety === "auto") return "run";
  if (a.safety === "dangerous") return "confirm";
  return unlocked ? "run" : "confirm";
}

/** Hold-to-repeat jogging: only when the description gives hold_repeat_ms and this card is unlocked. */
export function canHold(a: ManualAction, unlocked: boolean): boolean {
  return a.holdRepeatMs !== null && unlocked && a.safety !== "dangerous";
}

/** Whether this card has actions that are convenient only when unlocked (stop and read-only excluded). */
export function needsUnlock(d: ManualDevice): boolean {
  return d.actions.some((a) => actionGate(a, false) === "confirm" && a.safety !== "dangerous");
}

// ── Readouts ───────────────────────────────────────────────────────────────

/**
 * Matches a set of readings (live-stream channel values, the batch readout endpoint's response) to
 * axes: the key equals the axis name, or starts with "<axis name>_" (x_mm for x), or equals the
 * axis's channel number (axes[].extra.channel; the Picomotor steps stream is keyed by the
 * satellite's channel number, so the axis name may be pico_x and the channel 2); with a single axis
 * and a single remaining reading, it is that axis's (position_deg for angle). Readings that match
 * nothing are still listed, not dropped.
 */
export function matchAxisValues(
  axes: ManualAxis[],
  values: Record<string, number | null>,
): { byAxis: Record<string, number | null>; rest: Record<string, number | null> } {
  const byAxis: Record<string, number | null> = {};
  const used = new Set<string>();
  const keys = Object.keys(values);
  for (const ax of axes) {
    const n = ax.name.toLowerCase();
    const ch = ax.extra.channel;
    const chKey = typeof ch === "number" || (typeof ch === "string" && ch) ? String(ch).toLowerCase() : null;
    const k =
      keys.find((k) => !used.has(k) && (k.toLowerCase() === n || k.toLowerCase().startsWith(`${n}_`))) ??
      (chKey !== null ? keys.find((k) => !used.has(k) && k.toLowerCase() === chKey) : undefined);
    if (k !== undefined) {
      byAxis[ax.name] = values[k] ?? null;
      used.add(k);
    }
  }
  const left = keys.filter((k) => !used.has(k));
  const only = axes[0];
  if (axes.length === 1 && only && !(only.name in byAxis) && left.length === 1) {
    byAxis[only.name] = values[left[0]!] ?? null;
    used.add(left[0]!);
  }
  const rest: Record<string, number | null> = {};
  for (const k of keys) if (!used.has(k)) rest[k] = values[k] ?? null;
  return { byAxis, rest };
}

/** One reading of a local device (an entry of GET /api/manual/readouts). */
export interface LocalReadout {
  /** Unix seconds; null if unreadable. */
  t: number | null;
  axes: Array<{ name: string; position: number | null; moving: boolean | null; homed: boolean | null; extra: Record<string, unknown> }>;
  /** Verbatim error of this device's failed read; null on success. */
  error: string | null;
}

/** GET /api/manual/readouts?ids=a,b -> {readouts: {id: {t, axes, error}}}. Unrecognised entries are dropped, never throws. */
export function normalizeReadouts(raw: unknown): Record<string, LocalReadout> {
  const out: Record<string, LocalReadout> = {};
  const r = isObj(raw) && isObj(raw.readouts) ? raw.readouts : {};
  for (const [id, v] of Object.entries(r)) {
    if (!isObj(v)) continue;
    out[id] = {
      t: num(v.t),
      axes: (Array.isArray(v.axes) ? v.axes : [])
        .filter(isObj)
        .filter((a) => str(a.name))
        .map((a) => ({
          name: str(a.name),
          position: num(a.position),
          moving: bool(a.moving),
          homed: bool(a.homed),
          extra: isObj(a.extra) ? a.extra : {},
        })),
      error: strOrNull(v.error),
    };
  }
  return out;
}

export interface ReadoutPlan {
  /** Cards to read: local, readout is poll, available, on screen now (sorted). */
  ids: string[];
  /** The beat: the shortest interval among them. At most one request per beat. */
  intervalMs: number;
  /** Each device's own interval (interval_s from the description, not below MIN_POLL_S). */
  everyMs: Record<string, number>;
}

/** The batch readout endpoint accepts at most this many ids per request (more are split into several requests). */
export const MAX_READOUT_IDS = 64;

/** Fallback interval for satellite devices at the batch endpoint: live curves still go through LiveHub, and this only reads the hub cache (no command is sent to the satellite). */
export const SATELLITE_FALLBACK_POLL_S = 5;

/** Whether this card joins the batch readout (the card uses it to decide whether to report "on screen"): local poll cards and satellite devices; the camera does not. */
export function wantsBatchReadout(d: ManualDevice): boolean {
  if (d.kind === "camera") return false;
  return d.readout?.kind === "poll" || (d.readout?.kind === "live" && d.machine !== "local");
}

/**
 * Which devices to read and how often (available ones on screen now):
 *   - readout is poll (local stages): per the interval_s in the description;
 *   - satellite devices (readout is live): the hub cache is read every SATELLITE_FALLBACK_POLL_S;
 *     when the link is down it says "no new readings", and before the live stream arrives it fills
 *     in the values;
 *   - the camera is excluded (its readout is /api/camera/{id}/stats; the batch endpoint answers a
 *     camera id with just an error).
 * Nothing to read = null (no polling).
 */
export function readoutPlan(devices: ManualDevice[], visible: ReadonlySet<string>): ReadoutPlan | null {
  const picked = devices.filter((d) => wantsBatchReadout(d) && d.available && visible.has(d.id));
  if (!picked.length) return null;
  const everyMs: Record<string, number> = {};
  for (const d of picked)
    everyMs[d.id] = Math.max(MIN_POLL_S, d.readout?.kind === "poll" ? d.readout.intervalS : SATELLITE_FALLBACK_POLL_S) * 1000;
  return { ids: picked.map((d) => d.id).sort(), intervalMs: Math.min(...Object.values(everyMs)), everyMs };
}

/**
 * Which devices this beat covers: those that are due (their own interval has elapsed since the last
 * read). A 3 s optics group must not be read every 1 s just because a 1 s stage is on screen. A
 * tenth of the beat is kept as tolerance: a timer firing 1 ms early does not push a 3 s device to
 * the 4th beat, and a 2.5 s device is not read as 2 s. Devices never read (the card just scrolled
 * into view) are read immediately.
 */
export function dueReadouts(plan: ReadoutPlan, lastReadMs: Readonly<Record<string, number>>, nowMs: number): string[] {
  const tolerance = plan.intervalMs / 10;
  return plan.ids.filter((id) => {
    const last = lastReadMs[id];
    return last === undefined || nowMs - last >= (plan.everyMs[id] ?? plan.intervalMs) - tolerance;
  });
}

/**
 * The stop reply says "MAST has not opened this device, so it has no motion of its own to stop"
 * (StopOpticalStage with open=false, stopped=null). This is not "stopped": if it is still moving,
 * another program is driving it and has to be stopped there, as the summary states.
 */
export function stopHadNothingToStop(data: unknown): boolean {
  return isObj(data) && data.open === false && (data.stopped === null || data.stopped === undefined);
}

/** The execution entry answered refused_busy (the token is held by someone else; nothing ran). */
export function isRefusedBusy(data: unknown): boolean {
  return isObj(data) && data.instrument_busy === true;
}

/**
 * Result of one jog (hold-to-repeat uses it to decide whether to send a stop on release,
 * contract §13.1):
 *   - ok: it ran;
 *   - refused: it did not run: the token is held by someone else (refused_busy), the execution
 *     entry is unavailable (ok=false), or the request was rejected at the HTTP layer or never came
 *     back (no response body, so it is unknown whether it ran; counted as not run, preferring no stop);
 *   - failed: the execution entry answered that the skill did not succeed. A rejection at the door
 *     (parameter validation, safety envelope, preconditions, hard gate) cannot be told apart from
 *     "ran but failed" in this response (only busy carries a structured marker), so it is always
 *     treated as possibly moved and the stop is sent.
 *     refused_by in the response: si_parse / sample_gate / busy (and unrecognised new values) =
 *     rejected at the door, did not run; needs_human_node means a composite skill stopped at a node
 *     that needs a human decision, and earlier steps may have run, so it does not count as not run.
 */
export function jogOutcome(
  o: { kind: "ok" } | { kind: "failed"; resp?: { ok?: boolean; data?: unknown; refused_by?: unknown } },
): "ok" | "failed" | "refused" {
  if (o.kind === "ok") return "ok";
  const r = o.resp;
  if (!r || r.ok === false || isRefusedBusy(r.data)) return "refused";
  const rb = typeof r.refused_by === "string" ? r.refused_by.trim() : "";
  if (rb && rb !== "needs_human_node") return "refused";
  return "failed";
}

/** The owner used when the direct skill-execution entry takes the token (OWNER in api/routes/skill_exec.py). Compared with backend data: do not translate. */
export const DIRECT_EXEC_OWNER = "技能直调 API";

/**
 * The token holder read right before a stop on release: if someone else holds it (the owner is not
 * the direct-execution entry itself, e.g. a composite skill) no stop is sent and a human-readable
 * reason is returned; nobody holding it, or the direct entry itself = null (usual rule applies).
 */
export function holderBlocksStop(h: { owner: string; skill: string } | null): string | null {
  if (!h || h.owner === DIRECT_EXEC_OWNER) return null;
  return tr(`仪器令牌在 ${h.owner} 手里${h.skill ? `（正在执行 ${h.skill}）` : ""}`, `The instrument token is held by ${h.owner}${h.skill ? ` (running ${h.skill})` : ""}`);
}

const REST_NAME: Record<string, string> = {
  temperature: tr("温度", "Temperature"),
  exposure: tr("曝光", "Exposure"),
  center: tr("中心波长", "Center wavelength"),
  center_wavelength: tr("中心波长", "Center wavelength"),
  wavelength: tr("波长", "Wavelength"),
  grating: tr("光栅", "Grating"),
  frequency: tr("频率", "Frequency"),
  power: tr("功率", "Power"),
  amplitude: tr("幅度", "Amplitude"),
  gain: tr("增益", "Gain"),
  slit: tr("狭缝", "Slit"),
  velocity: tr("速度", "Velocity"),
};
/** Key suffix -> unit (longer ones first: _khz before _hz). */
const REST_UNIT: Array<[string, string]> = (
  [
    ["_c", "°C"],
    ["_ms", "ms"],
    ["_us", "µs"],
    ["_s", "s"],
    ["_nm", "nm"],
    ["_um", "µm"],
    ["_mm", "mm"],
    ["_deg", "°"],
    ["_hz", "Hz"],
    ["_khz", "kHz"],
    ["_mhz", "MHz"],
    ["_mw", "mW"],
    ["_w", "W"],
    ["_v", "V"],
    ["_ma", "mA"],
    ["_db", "dB"],
    ["_pct", "%"],
  ] as Array<[string, string]>
).sort((a, b) => b[0].length - a[0].length);

/**
 * How to label readings in a live stream that match no axis (numeric device_state fields of the
 * spectrometer and AOTF): recognised names are translated and key suffixes are read as units
 * (temperature_c -> Temperature °C); unrecognised ones are shown as is.
 */
export function restLabel(key: string): { name: string; unit: string } {
  // A purely numeric key is a satellite channel number (a Picomotor channel not registered as an axis)
  if (/^\d+$/.test(key)) return { name: tr(`通道 ${key}`, `Channel ${key}`), unit: "" };
  const k = key.toLowerCase();
  for (const [suf, unit] of REST_UNIT) {
    if (k.endsWith(suf) && k.length > suf.length) {
      const base = key.slice(0, -suf.length);
      return { name: REST_NAME[base.toLowerCase()] ?? base, unit };
    }
  }
  return { name: REST_NAME[k] ?? key, unit: "" };
}

/** "The instrument token is held by someone else": data.instrument_busy / data.holder in an execution result (direct_exec). */
export function busyHolderText(data: unknown): string | null {
  if (!isObj(data) || data.instrument_busy !== true) return null;
  const h = isObj(data.holder) ? data.holder : {};
  const who = str(h.owner) || tr("另一个入口", "another client");
  const what = str(h.skill);
  const held = num(h.held_s);
  return tr(`仪器令牌在 ${who} 手里${what ? `（正在执行 ${what}${held !== null ? `，已 ${Math.round(held)} s` : ""}）` : ""}`, `The instrument token is held by ${who}${what ? ` (running ${what}${held !== null ? ` for ${Math.round(held)} s` : ""})` : ""}`);
}
