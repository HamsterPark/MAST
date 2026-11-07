// ════════════════════════════════════════════════════════════════════════════
// Development only: a mock backend for the manual-control panel (installed by lib/live/devMock only
// under `npm run dev` with ?livemock=1; tree-shaken out of production builds). Shapes follow
// contract §13.1 and the response models of the endpoints:
//   GET  /api/manual/devices            device descriptions (stages on the STM computer, Nanonis optics groups, satellite sat1, camera)
//   GET  /api/manual/readouts?ids=...   local stage readings (batch read-only, not through the skill-execution entry)
//   GET  /api/camera/<id>/frame.jpg     draws a Gaussian spot (Mirror X/Y moves shift it); 404 when not live
//   GET  /api/camera/<id>/stats         state + spot statistics; still 200 when not live (statistics null), 404 for an unknown camera
//   GET  /api/scope/switch-advisory     instrument token holder (held during the 3 s of homing)
//   GET  /api/hardware/live-readings    tip withdrawn, Z feedback off
//   POST /api/skills/<manual-control skill>/execute
// All data is made up and only meant for checking the drawing and interaction. Values are placeholders
// throughout (this file reaches dev bundles through source maps). The Chinese strings below imitate
// backend responses, which are not translated by the frontend.
// ════════════════════════════════════════════════════════════════════════════

type Json = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const SENSOR_W = 1440;
const SENSOR_H = 1080;
const CAMERA_ID = "zelux1";

const st = {
  stages: {
    pdxc2: { x: 5000 },
    e861: { "1": 0 },
    zls: { x: 12.5 },
  } as Record<string, Record<string, number>>,
  /** MAST 有没有打开这台（读数不会去打开它；「连接」或任何运动按钮会打开）。 */
  connected: { pdxc2: true, e861: false, zls: true } as Record<string, boolean>,
  servo: { e861: true } as Record<string, boolean>,
  velocity: { e861: 1.5, zls: 0.8 } as Record<string, number>,
  /** Nanonis 光学组：命令过的步数（没有位置反馈）；在描述的 axes[].extra.odometer_steps 里。 */
  odometer: { "1": { X: 0, Y: 0 }, "2": { X: 0, Y: 0 } } as Record<string, Record<string, number>>,
  moving: new Map<string, number>(), // "<dev>|<axis>" → 停下的时刻
  holder: null as null | { owner: string; skill: string; since: number },
  /** 读数端点被调了几次、最近一次带了哪些卡（浏览器检查里看「一次带上所有可见的本机卡」）。 */
  readoutCalls: 0,
  lastReadoutIds: [] as string[],
  /** 最近 60 次读数请求（各带了哪些卡）：浏览器检查里看每台是不是按自己的 interval_s 读。 */
  readoutLog: [] as Array<{ t: number; ids: string[] }>,
  /** StopOpticalStage 被调了几次（浏览器检查里看「被拒的按住不补 stop」）。 */
  stopCalls: 0,
  /** 最近 10 次取画面的查询串（浏览器检查里看「拉伸亮度」带没带 scale=auto）。 */
  frameQueries: [] as string[],
  camera: {
    connected: false,
    live: false,
    exposureUs: 2000,
    gainDb: 0,
    seq: 0,
    cx: 0.56,
    cy: 0.44,
    roi: { x: 0, y: 0, w: SENSOR_W, h: SENSOR_H, binning: 1 },
    /** 改过曝光 / 增益 / ROI 之后这一刻之前的帧标 settled=false。 */
    settledAt: 0,
    /** 第一次连接时 ThorImageCAM 还开着（演示「被占用」的原话），之后就关了。 */
    thorImageOpen: true,
  },
};

const TRAVEL: Record<string, Record<string, [number, number]>> = {
  pdxc2: { x: [0, 20000] },
  e861: { "1": [-6.5, 6.5] },
  zls: { x: [0, 25] },
};

/** 技能 ParameterSpec 的拷贝（与 api/schemas_manual.py 的 ManualParamSpec 同名：min_value / max_value）。 */
function param(name: string, type: string, unit: string, min: number | null, max: number | null, def: unknown, extra: Json = {}) {
  return { name, type, unit, min_value: min, max_value: max, default: def, required: true, description: "", ...extra };
}

const ACTION_DEFAULTS = { fixed: {}, params: [], per_axis: false, direction: "none", direction_param: null, safety: "confirm", bypass_lock: false, hold_repeat_ms: null, stop_on_release: false, hint: "" };
const action = (o: Json): Json => ({ ...ACTION_DEFAULTS, ...o });

/** 一台本机台子的描述（devices 的 local_manual_devices() 的形状）：按钮只给驱动有文档依据的操作。 */
function stageDevice(
  id: string, label: string, vendor: string, model: string, axes: Json[], jogMax: number, jogDef: number, unit: string,
  opts: { home?: "frf" | "encoder" | "zero"; velocity?: boolean; servo?: boolean; hold: number; available?: boolean; unavailable_reason?: string; occupied_by?: string },
) {
  const fx = { device_id: id };
  const actions: Json[] = [
    action({ id: "jog", label: "点动", skill: "OpticalStageMove", fixed: { ...fx, relative: true }, params: [param("position", "float", unit, -jogMax, jogMax, jogDef)],
      per_axis: true, direction: "signed", direction_param: "position", hold_repeat_ms: opts.hold, stop_on_release: true, hint: "相对移动一步（软限位内；没打开的设备会先打开）" }),
    action({ id: "move_abs", label: "移到", skill: "OpticalStageMove", fixed: { ...fx, relative: false },
      params: [param("position", "float", unit, axes[0]?.min ?? null, axes[0]?.max ?? null, null)], per_axis: true, hint: "移到绝对位置（软限位内）" }),
  ];
  if (opts.home === "frf") actions.push(action({ id: "home", label: "寻零（会移动）", skill: "HomeOpticalStage", fixed: fx, per_axis: true, hint: "FRF：走到参考开关" }));
  if (opts.home === "encoder") actions.push(action({ id: "home", label: "寻零（会移动）", skill: "HomeOpticalStage", fixed: fx, per_axis: true, hint: "编码器找零" }));
  if (opts.home === "zero") actions.push(action({ id: "set_zero", label: "当前位置记为 0（不移动）", skill: "HomeOpticalStage", fixed: fx, per_axis: true, hint: "当前位置记为 0（不移动）" }));
  if (opts.velocity)
    actions.push(action({ id: "set_velocity", label: "设速度", skill: "SetOpticalStageVelocity", fixed: fx, params: [param("velocity", "float", `${unit}/s`, 0.001, 10, 1)], hint: "写入速度并读回；超过配置的上限一律拒绝" }));
  if (opts.servo) {
    actions.push(action({ id: "servo_on", label: "伺服开", skill: "OpticalStageServo", fixed: { ...fx, on: true }, hint: "SVO 1" }));
    actions.push(action({ id: "servo_off", label: "伺服关", skill: "OpticalStageServo", fixed: { ...fx, on: false }, hint: "SVO 0：关了以后位置不再闭环保持" }));
  }
  actions.push(action({ id: "wiggle", label: "Wiggle 自检", skill: "OpticalStageWiggle", fixed: fx, params: [param("delta", "float", unit, 0, jogMax, jogDef)], per_axis: true, hint: "±delta 往返一次，回到原位" }));
  actions.push(action({ id: "connect", label: "连接", skill: "GetOpticalStageStatus", fixed: { ...fx, connect: true }, safety: "auto", hint: "打开串口并做连接初始化，不移动" }));
  actions.push(action({ id: "disconnect", label: "断开（交给厂家软件）", skill: "DisconnectOpticalDevice", fixed: fx, safety: "auto", hint: "先停再关：厂家软件可以接管" }));
  actions.push(action({ id: "stop", label: "停止", skill: "StopOpticalStage", fixed: fx, safety: "auto", bypass_lock: true, hint: "只停这一台；MAST 没打开的不会去打开" }));
  const available = opts.available ?? true;
  return {
    id: `local/stage/${id}`, machine: "local", kind: "linear_stage", label, vendor, model, serial: "00000000",
    available, unavailable_reason: opts.unavailable_reason ?? null, occupied_by: opts.occupied_by ?? null,
    tip_requirement: "none",
    axes: axes.map((a) => ({ ...a, position: null, moving: null, homed: null, extra: { max_step: null } })),
    actions,
    readout: available ? { kind: "poll", interval_s: 1 } : null,
  };
}

function groupDevice(group: number, label: string, axes: string[]) {
  const cap = 200;
  const odo = st.odometer[String(group)] ?? {};
  return {
    id: `local/nanonis_group/${group}`,
    machine: "local",
    kind: "nanonis_motor_group",
    label: `${label}（组 ${group}，面板上的 Group ${group + 1}）`,
    vendor: "SPECS Nanonis",
    model: "PMD4",
    serial: "",
    available: true,
    unavailable_reason: null,
    occupied_by: null,
    tip_requirement: "tip_withdrawn",
    axes: axes.map((a) => ({
      name: a, unit: "步", min: null, max: null, position: null, moving: null, homed: null,
      extra: { max_steps_per_cmd: cap, odometer_steps: odo[a] ?? 0, position_feedback: false, drive_latched: false },
    })),
    actions: [
      action({
        id: "jog", label: `点动（单次 ≤ ${cap} 步）`, skill: "NanonisMotorGroupMove", fixed: { group: String(group) },
        params: [
          param("axis", "str", "", null, null, null),
          param("direction", "str", "", null, null, null, { allowed_values: ["+", "-"] }),
          param("steps", "int", "步", 1, 10_000, 10),
        ],
        per_axis: true, direction: "signed", direction_param: "direction",
        // StopMotor 没有组参数、会停所有组（含 STM 粗动）：松手不补发
        hold_repeat_ms: 150, stop_on_release: false, hint: "Motor_StartMove：开环走步，没有位置反馈",
      }),
      action({ id: "stop", label: "停止走步（所有组）", skill: "StopMotor", safety: "auto", bypass_lock: true, hint: "停所有电机组，含 STM 粗动组" }),
    ],
    // 没有位置反馈：步数里程在 axes[].extra 里，随装置清单刷新
    readout: null,
  };
}

const satFx = (dev: string) => ({ satellite: "sat1", device: dev });
const satStop = (dev: string) =>
  action({ id: "stop", label: "停止", skill: "StopSatelliteDevice", fixed: satFx(dev), safety: "auto", bypass_lock: true, hint: "只停这一台（卫星上减速停）" });

function devices(): Json[] {
  const cam = st.camera;
  const camFx = { camera_id: CAMERA_ID };
  return [
    stageDevice("pdxc2", "延迟线（PDXC2）", "Thorlabs", "PDXC2", [{ name: "x", unit: "um", min: 0, max: 20000 }], 1000, 10, "um", { home: "zero", hold: 200 }),
    stageDevice("e861", "PI E-861", "PI", "E-861 + N-470", [{ name: "1", unit: "mm", min: -6.5, max: 6.5 }], 0.5, 0.01, "mm", { home: "frf", velocity: true, servo: true, hold: 300 }),
    stageDevice("zls", "Zeptools 直线台", "Zeptools", "PZTC", [{ name: "x", unit: "mm", min: 0, max: 25 }], 2, 0.1, "mm", { home: "encoder", velocity: true, hold: 300 }),
    stageDevice(
      "zeyou2d", "Zeptools 二维台", "Zeptools", "PZTC-NM003",
      [{ name: "x", unit: "um", min: -5000, max: 5000 }, { name: "y", unit: "um", min: -5000, max: 5000 }], 500, 10, "um",
      { home: "encoder", velocity: true, hold: 300, available: false, unavailable_reason: "串口打不开：被另一个程序占着（模拟）", occupied_by: "LabVIEW.exe" },
    ),
    groupDevice(1, "Mirror X/Y", ["X", "Y"]),
    groupDevice(2, "Beam Deflection", ["X", "Y"]),
    {
      id: "sat1/pol", machine: "sat1", kind: "rotation_stage", label: "偏振片", vendor: "Thorlabs", model: "KDC101 + PRM1-Z8", serial: "00000000",
      available: true, unavailable_reason: null, occupied_by: null, tip_requirement: null,
      axes: [{ name: "angle", unit: "deg", min: -360, max: 360, homed: true }],
      actions: [
        action({ id: "jog", label: "转", skill: "SetPolarizerAngle", fixed: { relative: true }, params: [param("angle_deg", "float", "deg", 0, 90, 5)],
          direction: "signed", direction_param: "angle_deg", hold_repeat_ms: 300, stop_on_release: true }),
        action({ id: "move_rel", label: "相对转", skill: "SetPolarizerAngle", fixed: { relative: true }, params: [param("angle_deg", "float", "deg", -360, 360, null)] }),
        action({ id: "move_abs", label: "转到", skill: "SetPolarizerAngle", fixed: { relative: false }, params: [param("angle_deg", "float", "deg", -360, 360, null)] }),
        action({ id: "home", label: "回零", skill: "HomePolarizer", hint: "KDC101 回参考位，会转动" }),
        action({ id: "set_velocity", label: "设速度", skill: "SetPolarizerVelocity", params: [param("deg_s", "float", "deg/s", 0.1, 25, 10)] }),
        action({ id: "identify", label: "闪灯识别", skill: "IdentifySatelliteDevice", fixed: satFx("pol"), safety: "auto", hint: "前面板闪灯 3 s，不动" }),
        satStop("pol"),
      ],
      readout: { kind: "live", stream: "sat1/pol/angle" },
    },
    {
      id: "sat1/pico", machine: "sat1", kind: "openloop_stepper", label: "Picomotor", vendor: "Newport", model: "8742", serial: "00000",
      available: true, unavailable_reason: null, occupied_by: null, tip_requirement: null,
      axes: ["1", "2"].map((a) => ({ name: a, unit: "步", min: null, max: null })),
      actions: [
        action({ id: "jog", label: "走步", skill: "PicomotorJog", params: [param("axis", "str", "", null, null, null), param("steps", "int", "步", 1, 2000, 50)],
          per_axis: true, direction: "signed", direction_param: "steps", hold_repeat_ms: 250, stop_on_release: true }),
        action({ id: "move_abs", label: "走到计数", skill: "PicomotorMoveAbs", params: [param("axis", "str", "", null, null, null), param("position", "int", "步", null, null, null)], per_axis: true, hint: "差值按卫星此刻的计数算" }),
        action({ id: "set_velocity", label: "设速度", skill: "SetPicomotorVelocity", params: [param("axis", "str", "", null, null, null), param("steps_s", "int", "步/s", 1, 2000, 1750)], per_axis: true }),
        action({ id: "set_accel", label: "设加速度", skill: "SetPicomotorAccel", params: [param("axis", "str", "", null, null, null), param("steps_s2", "int", "步/s²", 1, 200_000, 100_000)], per_axis: true }),
        action({ id: "set_zero", label: "计数归零", skill: "PicomotorSetZero", params: [param("axis", "str", "", null, null, null)], per_axis: true, hint: "当前位置记为 0 步（不移动）" }),
        action({ id: "abort", label: "急停（不减速）", skill: "PicomotorAbort", safety: "auto", bypass_lock: true, hint: "立即停所有轴，不减速" }),
        satStop("pico"),
      ],
      readout: { kind: "live", stream: "sat1/pico/steps" },
    },
    {
      id: "sat1/xyz", machine: "sat1", kind: "xyz_stage", label: "XYZ 台（Motion_Manipulator）", vendor: "UIRobot", model: "（模拟）", serial: "",
      available: false, unavailable_reason: "等厂家协议（Motion_Manipulator）", occupied_by: null, tip_requirement: null,
      axes: ["x", "y", "z"].map((a) => ({ name: a, unit: "mm", min: 0, max: 10 })),
      actions: [satStop("xyz")],
      readout: { kind: "live", stream: "sat1/xyz/position" },
    },
    {
      id: "sat1/ccd", machine: "sat1", kind: "spectrometer", label: "光谱仪（HRS-300 电动部件）", vendor: "Princeton Instruments", model: "HRS-300", serial: "",
      available: true, unavailable_reason: null, occupied_by: null, tip_requirement: null, axes: [],
      actions: [
        action({ id: "set_grating", label: "换光栅", skill: "SetSpectrometerGrating", params: [param("grating", "str", "", null, null, "600 g/mm", { allowed_values: ["300 g/mm", "600 g/mm", "1200 g/mm"] })] }),
        action({ id: "set_center", label: "设中心波长", skill: "SetSpectrometerCenter", params: [param("center_nm", "float", "nm", 200, 1100, 680)] }),
        action({ id: "set_exit_port", label: "切出口", skill: "SetSpectrometerExitPort", params: [param("port", "str", "", null, null, "front", { allowed_values: ["front", "side"] })] }),
        action({ id: "stop", label: "停止采集", skill: "StopSpectrometer", safety: "auto", bypass_lock: true }),
      ],
      readout: { kind: "live", stream: "sat1/ccd/state" },
    },
    {
      id: "sat1/aotf", machine: "sat1", kind: "aotf", label: "AOTF", vendor: "Brimrose", model: "DS1", serial: "",
      available: false, unavailable_reason: "厂家协议未到：卫星驱动对它回 not_supported（只显示状态）", occupied_by: null, tip_requirement: null,
      axes: [], actions: [satStop("aotf")], readout: null,
    },
    {
      id: `local/camera/${CAMERA_ID}`, machine: "local", kind: "camera", label: "对光相机", vendor: "Thorlabs", model: "Zelux CS165CU", serial: "00000",
      // occupied_by 只表示那个程序在跑、MAST 没连这台相机；available 照样是 true
      available: true, unavailable_reason: null, occupied_by: !cam.connected && cam.thorImageOpen ? "ThorImageCAM" : null, tip_requirement: null, axes: [],
      actions: [
        action({ id: "camera_connect", label: "连接", skill: "ConnectCamera", fixed: camFx, safety: "auto", hint: "加载 SDK、打开相机（不开直播）" }),
        action({ id: "camera_disconnect", label: "断开", skill: "DisconnectCamera", fixed: camFx, safety: "auto", hint: "停直播并释放相机：ThorImageCAM 可以接管" }),
        action({ id: "live_start", label: "开始直播", skill: "CameraStartLive", fixed: camFx, safety: "auto" }),
        action({ id: "live_stop", label: "停止直播", skill: "CameraStopLive", fixed: camFx, safety: "auto" }),
        action({ id: "snapshot", label: "快照", skill: "CameraSnapshot", fixed: camFx, safety: "auto", params: [{ ...param("tag", "str", "", null, null, null), required: false }], hint: "存 .npy + PNG + JSON 进当前样品" }),
        action({ id: "set_exposure", label: "曝光", skill: "SetCameraExposure", fixed: camFx, safety: "auto", params: [param("exposure_us", "int", "µs", 40, 2_000_000, 2000)] }),
        action({ id: "set_gain", label: "增益", skill: "SetCameraGain", fixed: camFx, safety: "auto", params: [param("gain_db", "float", "dB", 0, 48, 0)] }),
        action({
          id: "set_roi", label: "设 ROI", skill: "SetCameraROI", fixed: camFx, safety: "auto", hint: "先停直播再改；彩色传感器取偶数",
          params: [
            param("x", "int", "px", 0, SENSOR_W, 0), param("y", "int", "px", 0, SENSOR_H, 0),
            param("w", "int", "px", 16, SENSOR_W, SENSOR_W), param("h", "int", "px", 16, SENSOR_H, SENSOR_H),
            { ...param("binning", "int", "", 1, 1, 1), required: false },
          ],
        }),
      ],
      readout: { kind: "poll", interval_s: 1 },
    },
  ];
}

const CAMERA_SKILLS = new Set([
  "ConnectCamera", "DisconnectCamera", "CameraStartLive", "CameraStopLive", "CameraSnapshot",
  "SetCameraExposure", "SetCameraGain", "SetCameraROI",
]);
export const MANUAL_MOCK_SKILLS = new Set([
  "OpticalStageMove", "HomeOpticalStage", "StopOpticalStage", "GetOpticalStageStatus", "DisconnectOpticalDevice",
  "SetOpticalStageVelocity", "OpticalStageServo", "OpticalStageWiggle",
  "SetPolarizerVelocity", "IdentifySatelliteDevice", "StopSatelliteDevice",
  "PicomotorSetZero", "PicomotorAbort", "PicomotorMoveAbs", "SetPicomotorVelocity", "SetPicomotorAccel",
  ...CAMERA_SKILLS,
]);
/** 免令牌：停止类（instrument_lock.BYPASS_NAMES）与根本不取令牌的相机技能。 */
const NO_TOKEN = new Set(["StopOpticalStage", "PicomotorAbort", "StopMotor", "StopSatelliteDevice", "StopSatelliteDevices", "StopSpectrometer", ...CAMERA_SKILLS]);

function res(name: string, ok: boolean, summary: string, data: Json = {}, error = "") {
  return { ok: true, success: ok, skill: name, summary: ok ? summary : "", data, error, elapsed_s: 0.3, nanonis_calls: 0, degraded: false, missing: [] };
}

function runManualSkill(name: string, p: Json): Json | null {
  const h = st.holder;
  if (h && !NO_TOKEN.has(name) && MANUAL_MOCK_SKILLS.has(name)) {
    const held = (Date.now() - h.since) / 1000;
    return res(name, false, "", { instrument_busy: true, holder: { owner: h.owner, skill: h.skill, held_s: held } },
      `仪器正被占用：${h.owner} 正在执行 ${h.skill}，已 ${held.toFixed(0)} 秒`);
  }
  const dev = String(p.device_id ?? "");
  const axis = String(p.axis ?? "");
  const cam = st.camera;
  switch (name) {
    case "GetOpticalStageStatus": {
      if (!st.stages[dev]) return res(name, false, "", {}, `${dev}：串口打不开（模拟）`);
      if (p.connect) st.connected[dev] = true;
      return res(name, true, `${dev} 已连接（连接初始化，不移动）`, { device_id: dev, connected: !!st.connected[dev] });
    }
    case "DisconnectOpticalDevice":
      st.connected[dev] = false;
      return res(name, true, `${dev} 已停下并断开：厂家软件可以接管了`);
    case "SetOpticalStageVelocity":
      st.velocity[dev] = Number(p.velocity);
      return res(name, true, `${dev} 速度 ${p.velocity}`);
    case "OpticalStageServo":
      st.servo[dev] = p.on === true;
      return res(name, true, `${dev} 伺服${p.on ? "开" : "关"}`);
    case "OpticalStageWiggle":
      if (!st.stages[dev]) return res(name, false, "", {}, `${dev}：串口打不开（模拟）`);
      st.connected[dev] = true; // 运动按钮会自己打开设备
      return res(name, true, `${dev}.${axis} ±${p.delta} 往返一次，回到原位（模拟）`);
    case "OpticalStageMove": {
      const s = st.stages[dev];
      if (!s) return res(name, false, "", {}, `${dev}：串口打不开（模拟）`);
      st.connected[dev] = true; // 运动按钮会自己打开设备
      const [lo, hi] = TRAVEL[dev]?.[axis] ?? [-Infinity, Infinity];
      const target = p.relative ? (s[axis] ?? 0) + Number(p.position) : Number(p.position);
      if (target < lo || target > hi) return res(name, false, "", {}, `目标 ${target} 超出行程 [${lo}, ${hi}]：拒绝（软限位）`);
      s[axis] = target;
      st.moving.set(`${dev}|${axis}`, Date.now() + 400);
      return res(name, true, `${dev}.${axis} → ${+target.toFixed(4)}`);
    }
    case "HomeOpticalStage": {
      const s = st.stages[dev];
      if (!s) return res(name, false, "", {}, `${dev}：没连上（模拟）`);
      st.connected[dev] = true;
      if (dev === "pdxc2") {
        s[axis] = 0; // PDXC2 的 Home 只把计数清零，不移动
        return res(name, true, "当前位置记为 0（不移动）");
      }
      st.holder = { owner: "技能直调 API", skill: "HomeOpticalStage", since: Date.now() };
      setTimeout(() => {
        s[axis] = TRAVEL[dev]?.[axis]?.[0] ?? 0;
        st.holder = null;
      }, 3000);
      return res(name, true, `${dev}.${axis} 寻零中（模拟 3 s，期间令牌被占）`);
    }
    case "StopOpticalStage": {
      st.stopCalls += 1;
      if (dev && !st.connected[dev]) {
        // 停一台 MAST 没打开的设备：没有自己的运动可停，也不会去打开它
        return res(name, true, "MAST 没有自己的运动可停；如果它还在动，就是别的程序在驱动，要在那边停",
          { stopped: null, open: false, enabled: true });
      }
      st.moving.clear();
      const opened = Object.keys(st.connected).filter((k) => st.connected[k]);
      return res(name, true, `已停：${dev || opened.join("、") || "（没有打开的设备）"}`, { stopped: dev || opened, open: true });
    }
    case "SetPolarizerVelocity":
      return res(name, true, `偏振片速度 ${p.deg_s} deg/s`);
    case "IdentifySatelliteDevice":
      return res(name, true, `${p.device} 前面板闪灯 3 s`);
    case "StopSatelliteDevice":
      return res(name, true, `已停：${p.satellite}/${p.device}`, { stopped: [p.device] });
    case "PicomotorSetZero":
      return res(name, true, `轴 ${axis} 计数归零`);
    case "PicomotorMoveAbs":
      return res(name, true, `轴 ${axis} 走到计数 ${p.position}`);
    case "SetPicomotorVelocity":
      return res(name, true, `轴 ${axis} 速度 ${p.steps_s} 步/s`);
    case "SetPicomotorAccel":
      return res(name, true, `轴 ${axis} 加速度 ${p.steps_s2} 步/s²`);
    case "PicomotorAbort":
      return res(name, true, "所有轴立即停（不减速）");
    case "ConnectCamera":
      if (cam.thorImageOpen) {
        cam.thorImageOpen = false; // 演示：用户随后关掉了 ThorImageCAM
        return res(name, false, "", {}, "相机被 ThorImageCAM 占用，请先关掉它（MAST 不会替你关）（模拟；再点一次就能连上）");
      }
      cam.connected = true;
      return res(name, true, "Zelux CS165CU 已连接（模拟）");
    case "DisconnectCamera":
      cam.connected = false;
      cam.live = false;
      return res(name, true, "已断开（ThorImageCAM 又可以用了）");
    case "CameraStartLive":
      if (!cam.connected) return res(name, false, "", {}, "相机没连上：先「连接」");
      cam.live = true;
      return res(name, true, "开始直播");
    case "CameraStopLive":
      cam.live = false;
      return res(name, true, "停止直播");
    case "CameraSnapshot":
      if (!cam.live) return res(name, false, "", {}, "没在直播");
      return res(name, true, `快照已存进当前样品（模拟）：帧 #${cam.seq}${p.tag ? `（${p.tag}）` : ""}`);
    case "SetCameraExposure":
      if (!cam.connected) return res(name, false, "", {}, "相机没连上：先「连接」");
      cam.exposureUs = Number(p.exposure_us);
      cam.settledAt = Date.now() + 600;
      return res(name, true, `曝光 ${cam.exposureUs} µs（读回）`);
    case "SetCameraGain":
      if (!cam.connected) return res(name, false, "", {}, "相机没连上：先「连接」");
      cam.gainDb = Number(p.gain_db);
      cam.settledAt = Date.now() + 600;
      return res(name, true, `增益 ${cam.gainDb} dB（读回）`);
    case "SetCameraROI": {
      if (!cam.connected) return res(name, false, "", {}, "相机没连上：先「连接」");
      const even = (v: unknown) => Math.max(0, Math.round(Number(v) / 2) * 2);
      cam.roi = { x: even(p.x), y: even(p.y), w: Math.max(16, even(p.w)), h: Math.max(16, even(p.h)), binning: 1 };
      cam.settledAt = Date.now() + 600;
      return res(name, true, `ROI ${cam.roi.w}×${cam.roi.h} @ (${cam.roi.x}, ${cam.roi.y})（取偶数）`);
    }
    default:
      return null;
  }
}

// ── 相机画面 ───────────────────────────────────────────────────────────────

const SIGMA = 26; // 传感器像素
/** 画 ROI 那一块（灰度，2×2 合成之后的一半大小，再按 max_w 缩）。光斑位置在传感器坐标里。 */
async function frameJpeg(maxW: number): Promise<Blob | null> {
  const c = st.camera;
  c.seq += 1;
  c.cx = Math.min(0.95, Math.max(0.05, c.cx + (Math.random() - 0.5) * 0.003));
  c.cy = Math.min(0.95, Math.max(0.05, c.cy + (Math.random() - 0.5) * 0.003));
  const { roi } = c;
  const w = Math.min(maxW, roi.w / 2, 480);
  const h = Math.round((w * roi.h) / roi.w);
  const cv = document.createElement("canvas");
  cv.width = w;
  cv.height = h;
  const ctx = cv.getContext("2d");
  if (!ctx) return null;
  ctx.fillStyle = "#0b0d10";
  ctx.fillRect(0, 0, w, h);
  const bright = Math.min(1, (c.exposureUs / 2000) * Math.pow(10, c.gainDb / 20));
  const px = ((c.cx * SENSOR_W - roi.x) / roi.w) * w;
  const py = ((c.cy * SENSOR_H - roi.y) / roi.h) * h;
  const r = (SIGMA * 3 * w) / roi.w;
  const g = ctx.createRadialGradient(px, py, 0, px, py, r);
  g.addColorStop(0, `rgba(255,255,255,${bright})`);
  g.addColorStop(0.35, `rgba(200,200,200,${bright * 0.45})`);
  g.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = "rgba(255,255,255,0.05)";
  for (let i = 0; i < 300; i++) ctx.fillRect(Math.random() * w, Math.random() * h, 1, 1);
  return new Promise((resolve) => cv.toBlob((b) => resolve(b), "image/jpeg", 0.8));
}

function cameraStats(): Json {
  const c = st.camera;
  const state = c.live ? "live" : c.connected ? "connected" : "disconnected";
  const base = { camera_id: CAMERA_ID, state, connected: c.connected, live: c.live, error: null };
  const settings = c.connected ? { exposure_us: c.exposureUs, gain_db: c.gainDb, roi: c.roi } : { exposure_us: null, gain_db: null, roi: null };
  if (!c.live) {
    return {
      ...base, ...settings, fps: null, max: null, saturated_frac: null, centroid_px: null, fwhm_px: null, centroid_sensor_px: null,
      fwhm_major_px: null, fwhm_minor_px: null, angle_deg: null, frame_w: null, frame_h: null, frame_seq: null, t: null, settled: null,
    };
  }
  const bright = (c.exposureUs / 2000) * Math.pow(10, c.gainDb / 20);
  const sx = c.cx * SENSOR_W;
  const sy = c.cy * SENSOR_H;
  return {
    ...base,
    ...settings,
    fps: 30,
    max: Math.min(1023, Math.round(800 * bright)),
    saturated_frac: bright > 1.28 ? 0.004 * (bright - 1.28) : 0,
    // 原点 = ROI 左上角像素的中心
    centroid_px: [sx - c.roi.x - 0.5, sy - c.roi.y - 0.5],
    centroid_sensor_px: [sx - 0.5, sy - 0.5],
    fwhm_px: [2.355 * SIGMA, 2.355 * SIGMA * 1.08],
    fwhm_major_px: 2.355 * SIGMA * 1.08,
    fwhm_minor_px: 2.355 * SIGMA,
    angle_deg: 90,
    frame_w: c.roi.w,
    frame_h: c.roi.h,
    frame_seq: c.seq,
    t: Date.now() / 1000,
    settled: Date.now() >= c.settledAt,
  };
}

/** 手动控制那几个地址；别的返回 null（交给 devMock 的其余部分或真后端）。 */
export async function manualMockResponse(url: URL, body: () => Promise<unknown>): Promise<Response | null> {
  const path = url.pathname;
  if (path === "/api/manual/devices")
    return json({ devices: devices(), degraded: false, degraded_sources: [], notes: ["（模拟）卫星 sat1 的 XYZ 台等厂家协议：只有停止"] });
  // 只给浏览器检查用：读数端点被调了几次、最近一次带了哪些卡
  if (path === "/api/__mock/manual")
    return json({
      readoutCalls: st.readoutCalls, lastReadoutIds: st.lastReadoutIds, readoutLog: st.readoutLog, stopCalls: st.stopCalls,
      frameQueries: st.frameQueries,
    });
  if (path === "/api/manual/readouts") {
    const ids = (url.searchParams.get("ids") ?? "").split(",").filter(Boolean);
    const now = Date.now();
    const readouts: Json = {};
    for (const id of ids) {
      const dev = id.split("/").pop() ?? "";
      if (id.startsWith("local/nanonis_group/")) {
        const odo = st.odometer[dev] ?? {};
        readouts[id] = {
          t: now / 1000,
          axes: Object.entries(odo).map(([a, n]) => ({ name: a, position: null, extra: { odometer_steps: n, position_feedback: false, drive_latched: false } })),
          error: null,
        };
        continue;
      }
      const s = st.stages[dev];
      if (!s) {
        readouts[id] = { t: null, axes: [], error: id.startsWith("local/camera/") ? "相机的读数在 /api/camera/{id}/stats" : `${dev}：没连上（模拟）` };
        continue;
      }
      const open = st.connected[dev] === true;
      const extraOf = (): Json => {
        if (!open) return { connected: false };
        if (dev === "e861") return { connected: true, servo: st.servo.e861 ?? false, velocity: st.velocity.e861, velocity_unit: "mm/s", travel_min_controller: -6.5, travel_max_controller: 6.5, has_limit_switches: true, has_reference_switch: true };
        if (dev === "pdxc2") return { connected: true, status_bits: 0x80000400, moving_forward: false, moving_backward: false, homing: false, closed_loop: true, errors: [], control_mode: "closed_loop", external_trigger: "Manual" };
        return { connected: true, velocity: st.velocity[dev] ?? 1, speed_now: 0, velocity_unit: "mm/s", zero_found: true, closed_loop: false };
      };
      readouts[id] = {
        t: now / 1000,
        axes: Object.entries(s).map(([a, v]) => ({ name: a, position: open ? v : null, moving: open ? (st.moving.get(`${dev}|${a}`) ?? 0) > now : null, homed: null, extra: extraOf() })),
        error: null,
      };
    }
    st.readoutCalls += 1;
    st.lastReadoutIds = ids;
    st.readoutLog.push({ t: now, ids });
    if (st.readoutLog.length > 60) st.readoutLog.shift();
    return json({ readouts });
  }
  if (path === "/api/scope/switch-advisory") {
    const h = st.holder;
    return json({
      busy: !!h, orchestrator_running: false, background_active: 0, blocking: !!h, can_force: true, reasons: [], degraded: false,
      instrument_holder: h ? { owner: h.owner, skill: h.skill, held_s: (Date.now() - h.since) / 1000 } : null,
    });
  }
  if (path === "/api/hardware/live-readings") {
    return json({
      connected: true, degraded: false,
      readings: { withdrawn: true, z_controller_on: false, scan_running: false, stale: false, bias_v: 2.0, current_a: 1e-12, setpoint_a: 5e-11, z_m: null },
      bias_history: [], current_history: [],
    });
  }
  const camRoute = /^\/api\/camera\/([A-Za-z0-9_-]+)\/(frame\.jpg|stats)$/.exec(path);
  if (camRoute) {
    if (camRoute[1] !== CAMERA_ID) return json({ detail: "没有这台相机" }, 404);
    if (camRoute[2] === "stats") return json(cameraStats());
    st.frameQueries.push(url.search);
    if (st.frameQueries.length > 10) st.frameQueries.shift();
    if (!st.camera.live) return json({ detail: "相机没在直播" }, 404);
    const blob = await frameJpeg(Number(url.searchParams.get("max_w")) || 960);
    return blob
      ? new Response(blob, { headers: { "content-type": "image/jpeg", "cache-control": "no-store", "x-frame-seq": String(st.camera.seq) } })
      : json({ detail: "画不出来" }, 500);
  }
  const m = /^\/api\/skills\/([A-Za-z0-9_]+)\/execute$/.exec(path);
  if (m) {
    const name = m[1]!;
    const b = ((await body().catch(() => ({}))) ?? {}) as Json;
    const p = (b.params ?? {}) as Json;
    if (name === "NanonisMotorGroupMove" && String(p.group) !== "0" && !p.dry_run) {
      // 步数里程跟着记（描述里的 axes[].extra.odometer_steps）；Mirror X/Y（组 1）走步时对光相机上的
      // 光斑也跟着走（演示「边看画面边对光」）
      const sign = p.direction === "-" ? -1 : 1;
      const odo = st.odometer[String(p.group)] ?? (st.odometer[String(p.group)] = {});
      odo[String(p.axis)] = (odo[String(p.axis)] ?? 0) + sign * Number(p.steps || 0);
      if (String(p.group) === "1") {
        const d = sign * Number(p.steps || 0) * 0.0008;
        if (p.axis === "X") st.camera.cx += d;
        if (p.axis === "Y") st.camera.cy += d;
      }
      return null; // 回包交给 devMock 原有的 NanonisMotorGroupMove
    }
    if (!MANUAL_MOCK_SKILLS.has(name)) return null;
    await new Promise((r) => setTimeout(r, 150));
    const out = runManualSkill(name, p);
    return out ? json(out) : null;
  }
  return null;
}
