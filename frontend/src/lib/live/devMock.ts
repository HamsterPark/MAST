// ════════════════════════════════════════════════════════════════════════════
// 开发用的模拟数据源 —— **只在 `vite dev` 下、地址带 `?livemock=1` 时启用，不进生产包**。
//
// main.tsx 用 `if (import.meta.env.DEV) await import("./lib/live/devMock")` 加载它：构建时
// import.meta.env.DEV 被替换成 false，整个分支连同这个模块被摇掉（npm run build 之后在 dist
// 里搜 LIVE_DEV_MOCK_MARKER 应当搜不到）。
//
// 它在浏览器里冒充后端：
//   · /ws/live 的 WebSocket（只截这一个地址，/ws/events 照常走真的）
//   · GET /api/live/streams[/…]、/api/satellites、/api/port-guard、/api/settings/hardware-modules
//   · TERS 页用到的技能：GET /api/skills/{name}（技能卡）与 POST /api/skills/{name}/execute
// 帧格式照契约 §7 与 mast/live 的实现（streams 带 hub 与每个流的 uid；增量流合批成 frames；
// spectrum/scalar 每拍只推最新一帧；15 s 没别的消息发 ping）。数据是编的，只用来看画法。
// ════════════════════════════════════════════════════════════════════════════

import type { Middleware } from "openapi-fetch";
import { manualMockResponse } from "../manual/devMockManual.ts";

export const LIVE_DEV_MOCK_MARKER = "LIVE_DEV_MOCK_MARKER";

type Json = Record<string, unknown>;

interface MockStream {
  meta: Json & { id: string; kind: string };
  seq: number;
  frames: Array<{ seq: number; frame: Json }>;
  retain: number;
  axis: { ver: number; unit: string; x: number[] } | null;
}

const HUB_ID = `mock-${Math.random().toString(16).slice(2, 8)}`;
const streams = new Map<string, MockStream>();
const sockets = new Set<MockLiveSocket>();
const nowS = () => Date.now() / 1000;
const uid = () => Math.random().toString(16).slice(2, 10);

function declare(id: string, kind: string, extra: Json = {}, retain = 5000): MockStream {
  let s = streams.get(id);
  if (!s) {
    s = {
      meta: { id, kind, title: id, source: "devMock", meta: {}, retain, uid: uid(), created_t: nowS(), ended: false, ...extra },
      seq: 0,
      frames: [],
      retain,
      axis: null,
    };
    streams.set(id, s);
    for (const ws of sockets) ws.dirty = true;
  }
  return s;
}

function describe(s: MockStream): Json {
  return {
    ...s.meta,
    last_seq: s.seq,
    first_seq: s.frames[0]?.seq ?? null,
    n_frames: s.frames.length,
    axis_ver: s.axis?.ver ?? 0,
    last_t: s.frames.at(-1)?.frame.t ?? null,
  };
}

function publish(id: string, frame: Json): void {
  const s = streams.get(id);
  if (!s) return;
  s.seq += 1;
  const item = { seq: s.seq, frame: { t: nowS(), ...frame } };
  if (s.meta.kind === "map2d") {
    const key = `${frame.ix},${frame.iy}`;
    s.frames = s.frames.filter((f) => `${f.frame.ix},${f.frame.iy}` !== key);
  }
  s.frames.push(item);
  if (s.frames.length > s.retain) s.frames.splice(0, s.frames.length - s.retain);
  for (const ws of sockets) ws.enqueue(id, item);
}

function select(s: MockStream, since: number | undefined): Array<{ seq: number; frame: Json }> {
  const sinceEff = since ?? (s.meta.kind === "spectrum" ? -1 : 0);
  if (sinceEff < 0) return s.frames.slice(-1);
  return s.frames.filter((f) => f.seq > sinceEff);
}

// ── 假的 /ws/live ──────────────────────────────────────────────────────────

class MockLiveSocket {
  readyState = 0;
  url: string;
  onopen: ((ev?: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onerror: ((ev?: unknown) => void) | null = null;
  onclose: ((ev?: { code?: number }) => void) | null = null;
  dirty = true;
  private wanted = new Set<string>();
  private all = false;
  private latest = new Map<string, { seq: number; frame: Json }>();
  private queues = new Map<string, Array<{ seq: number; frame: Json }>>();
  private timer: ReturnType<typeof setInterval> | undefined;
  private lastSend = Date.now();
  private interval: number;

  constructor(url: string) {
    this.url = url;
    const hz = Number(new URL(url).searchParams.get("max_hz") || "20");
    this.interval = 1000 / Math.min(60, Math.max(0.5, hz || 20));
    sockets.add(this);
    setTimeout(() => {
      this.readyState = 1;
      this.onopen?.();
      this.timer = setInterval(() => this.tick(), this.interval);
    }, 30);
  }

  private emit(obj: unknown): void {
    if (this.readyState !== 1) return;
    this.lastSend = Date.now();
    this.onmessage?.({ data: JSON.stringify(obj) });
  }

  enqueue(id: string, item: { seq: number; frame: Json }): void {
    if (!(this.all || this.wanted.has(id))) return;
    const kind = streams.get(id)?.meta.kind;
    if (kind === "spectrum" || kind === "scalar") this.latest.set(id, item);
    else {
      const q = this.queues.get(id) ?? [];
      q.push(item);
      this.queues.set(id, q);
    }
  }

  private tick(): void {
    if (this.dirty) {
      this.dirty = false;
      this.emit({ kind: "streams", hub: HUB_ID, items: [...streams.values()].map(describe) });
    }
    for (const [id, item] of this.latest) this.emit({ kind: "frame", stream: id, seq: item.seq, frame: item.frame });
    this.latest.clear();
    for (const [id, q] of this.queues) {
      if (q.length === 1) this.emit({ kind: "frame", stream: id, seq: q[0]!.seq, frame: q[0]!.frame });
      else if (q.length) this.emit({ kind: "frames", stream: id, items: q });
    }
    this.queues.clear();
    if (Date.now() - this.lastSend > 15_000) this.emit({ kind: "ping" });
  }

  send(data: string): void {
    let msg: Json;
    try {
      msg = JSON.parse(data) as Json;
    } catch {
      this.emit({ kind: "error", op: null, message: "不是 JSON" });
      return;
    }
    const ids = Array.isArray(msg.streams) ? (msg.streams as string[]) : [];
    const since = (msg.since ?? {}) as Record<string, number>;
    if (msg.op === "subscribe") {
      if (ids.includes("*")) this.all = true;
      for (const id of ids) if (id !== "*") this.wanted.add(id);
      const targets = ids.includes("*") ? [...streams.keys()] : ids.filter((id) => streams.has(id));
      for (const id of targets) {
        const s = streams.get(id)!;
        this.latest.delete(id);
        this.queues.delete(id);
        if (s.axis) this.emit({ kind: "axis", stream: id, ver: s.axis.ver, x: s.axis.x, unit: s.axis.unit });
        const frames = select(s, since[id] ?? since["*"]);
        if (frames.length === 1) this.emit({ kind: "frame", stream: id, seq: frames[0]!.seq, frame: frames[0]!.frame });
        else if (frames.length) this.emit({ kind: "frames", stream: id, items: frames });
      }
    } else if (msg.op === "unsubscribe") {
      if (ids.includes("*")) {
        this.all = false;
        this.wanted.clear();
      }
      for (const id of ids) this.wanted.delete(id);
    }
  }

  close(): void {
    if (this.timer) clearInterval(this.timer);
    sockets.delete(this);
    const was = this.readyState;
    this.readyState = 3;
    if (was !== 3) setTimeout(() => this.onclose?.({ code: 1000 }), 0);
  }
}

// ── 模拟设备 ───────────────────────────────────────────────────────────────

const N_PX = 1340;
const LASER = 632.8;
const sim = {
  acquiring: true,
  framesLeft: Infinity,
  exposureMs: 50,
  angle: 0,
  angleTarget: 0,
  homed: true,
  pico: { "1": 0, "2": 0 } as Record<string, number>,
  xyz: { x: 5, y: 2.5, z: 4.8 },
  ix: 0,
  iy: 0,
  delay: -5,
  /** AcquireSpectrum 进行中：累加卫星的逐帧，采完把平均谱发到 local/spectra/ccd（真实技能对卫星设备就这么做）。 */
  run: null as { id: string; sum: number[]; n: number } | null,
};

const AXIS_NM = Array.from({ length: N_PX }, (_, i) => 640 + (80 * i) / (N_PX - 1));
const ACQUIRED = "local/spectra/ccd";

function ramanSpectrum(t: number): number[] {
  const out = new Array<number>(N_PX);
  const peaks = [
    { shift: 520.7, w: 4, a: 900 }, // Si
    { shift: 1350, w: 25, a: 260 * (1 + 0.3 * Math.sin(t / 3)) }, // D
    { shift: 1590, w: 14, a: 420 * (1 + 0.2 * Math.cos(t / 4)) }, // G
  ];
  for (let i = 0; i < N_PX; i++) {
    const nm = 640 + (80 * i) / (N_PX - 1);
    const shift = 1e7 / LASER - 1e7 / nm;
    let y = 600 + 120 * Math.exp(-(((i - 900) / 600) ** 2));
    for (const p of peaks) y += p.a / (1 + ((shift - p.shift) / p.w) ** 2);
    y += (Math.random() - 0.5) * 40;
    if (Math.random() < 0.0004) y += 5000; // 宇宙线
    out[i] = Math.round(y);
  }
  return out;
}

let started = false;

function startSim(): void {
  if (started) return;
  started = true;
  const spec = declare(
    "sat1/ccd/spectrum",
    "spectrum",
    { title: "sat1 ccd 光谱", source: "satellite:sat1", x_label: "波长", x_unit: "nm", y_label: "计数", y_unit: "counts", meta: { laser_line_nm: LASER, grating: "600 g/mm", center_nm: 680 } },
    200,
  );
  spec.axis = { ver: 1, unit: "nm", x: AXIS_NM };
  declare("sat1/pol/angle", "scalar", { title: "sat1 pol", source: "satellite:sat1", y_label: "angle", y_unit: "deg" });
  declare("sat1/xyz/position", "scalar", { title: "sat1 xyz", source: "satellite:sat1", y_label: "position", y_unit: "mm" });
  declare("sat1/pico/steps", "scalar", { title: "sat1 pico", source: "satellite:sat1", y_label: "steps", y_unit: "steps" });
  // 没有轴流的设备（光谱仪）：device_state 的数值字段（与 satellite/live_bridge.py 的 <sat>/<dev>/state 同形）
  declare("sat1/ccd/state", "scalar", { title: "sat1 ccd", source: "satellite:sat1", y_label: "state" });
  declare("sat1/health", "scalar", { title: "sat1 链路", source: "satellite:sat1", y_label: "时延 / 偏差", y_unit: "ms" });
  declare("sat1/events", "status", { title: "sat1 事件", source: "satellite:sat1" }, 500);
  declare("sat1/pmt/counts", "scalar", { title: "PMT 计数", source: "satellite:sat1", y_label: "计数率", y_unit: "cps" });
  declare(
    "local/ters_map/mock-run",
    "map2d",
    { title: "TERS 图（模拟）", source: "TERSGridMap", x_label: "X", x_unit: "nm", y_label: "Y", y_unit: "nm", meta: { nx: 48, ny: 32, x0: 0, y0: 0, dx: 0.5, dy: 0.5, x_unit: "nm", y_unit: "nm" } },
    48 * 32,
  );
  declare(
    "local/pump_probe/mock-run",
    "xy",
    // 与 PumpProbeScan（skills/builtins/optics_pump_probe.py）一致：值的单位在 meta.units 里按通道给，没有 y_unit
    { title: "泵浦-探测 延时扫描（模拟）", source: "PumpProbeScan", x_label: "Delay", x_unit: "ps", y_label: "Signal", meta: { channels: ["current_a", "lockin_v"], units: { current_a: "A", lockin_v: "V" } } },
    20000,
  );
  declare(
    "local/optics_scan/mock-run",
    "map2d",
    { title: "光学台扫描 x×y（模拟）", source: "OpticalStageScan", x_label: "x", x_unit: "µm", y_label: "y", y_unit: "µm", meta: { nx: 32, ny: 32, x0: -16, y0: -16, dx: 1, dy: 1, x_unit: "µm", y_unit: "µm", channels: ["lockin_v"], units: { lockin_v: "V" } } },
    32 * 32,
  );
  publish("sat1/events", { text: "模拟数据源已启动（?livemock=1）", level: "info" });

  let k = 0;
  setInterval(() => {
    k += 1;
    const t = nowS();
    if (sim.acquiring && sim.framesLeft > 0 && k % Math.max(1, Math.round(sim.exposureMs / 50)) === 0) {
      const y = ramanSpectrum(t);
      publish("sat1/ccd/spectrum", { y, axis_ver: 1, meta: { exposure_ms: sim.exposureMs, frame_index: k, manual: false } });
      const run = sim.run;
      if (run) {
        y.forEach((v, i) => (run.sum[i] = (run.sum[i] ?? 0) + v));
        run.n += 1;
      }
      sim.framesLeft -= 1;
      if (sim.framesLeft <= 0) {
        sim.acquiring = false;
        if (run && run.n > 0) {
          publish(ACQUIRED, {
            y: run.sum.map((v) => v / run.n),
            axis_ver: 1,
            meta: { run_id: run.id, final: true, frames_averaged: run.n, exposure_ms: sim.exposureMs },
          });
        }
        sim.run = null;
        publish("sat1/events", { text: "ccd 采集完成", level: "info" });
      }
    }
    if (Math.abs(sim.angle - sim.angleTarget) > 0.01) {
      sim.angle += Math.sign(sim.angleTarget - sim.angle) * Math.min(1.5, Math.abs(sim.angleTarget - sim.angle));
    }
    if (k % 2 === 0) {
      publish("sat1/pol/angle", { values: { position_deg: Number(sim.angle.toFixed(3)) } });
      publish("sat1/pmt/counts", { values: { counts: 2e4 + 3e3 * Math.sin(t / 2) + Math.random() * 800 } });
    }
    if (k % 20 === 0) {
      publish("sat1/xyz/position", { values: { x_mm: sim.xyz.x, y_mm: sim.xyz.y, z_mm: sim.xyz.z } });
      publish("sat1/pico/steps", { values: { ...sim.pico } });
      publish("sat1/ccd/state", { values: { temperature_c: Number((-70 + Math.random() * 0.2).toFixed(2)), exposure_ms: sim.exposureMs, center_nm: 680 } });
      publish("sat1/health", { values: { rtt_ms: 2 + Math.random() * 2, clock_offset_ms: -1.2 + Math.random() * 0.4 } });
    }
    // TERS 图：蛇形逐像素
    if (k % 2 === 0) {
      const nx = 48;
      const ny = 32;
      const ix = sim.iy % 2 === 0 ? sim.ix : nx - 1 - sim.ix;
      const r = Math.hypot(ix - 20, sim.iy - 14);
      const v = 1000 + 3000 * Math.exp(-((r / 6) ** 2)) + 400 * Math.cos(ix / 3) + Math.random() * 150;
      publish("local/ters_map/mock-run", { ix, iy: sim.iy, values: { intensity: v, current_pa: 100 + Math.random() * 5 } });
      sim.ix += 1;
      if (sim.ix >= nx) {
        sim.ix = 0;
        sim.iy = (sim.iy + 1) % ny;
      }
    }
    // 泵浦-探测：-5…20 ps
    if (k % 4 === 0) {
      const d = sim.delay;
      const sig = d < 0 ? 0 : Math.exp(-d / 6) * (1 - Math.exp(-d / 0.4));
      publish("local/pump_probe/mock-run", {
        x: Number(d.toFixed(3)),
        values: { current_a: 1e-10 * (1 + 0.15 * sig) + (Math.random() - 0.5) * 2e-13, lockin_v: 3e-4 * sig + (Math.random() - 0.5) * 2e-5 },
      });
      sim.delay = d >= 20 ? -5 : d + 0.1;
    }
    // 光学台扫描：逐行
    if (k % 3 === 0) {
      const s = streams.get("local/optics_scan/mock-run")!;
      const n = s.seq;
      const ix = n % 32;
      const iy = Math.floor(n / 32) % 32;
      const v = Math.exp(-(((ix - 18) / 5) ** 2 + ((iy - 12) / 7) ** 2)) * 5e-4 + Math.random() * 3e-5;
      publish("local/optics_scan/mock-run", { ix, iy, values: { lockin_v: v } });
    }
  }, 50);
}

// ── 假的 REST ──────────────────────────────────────────────────────────────

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const MOCK_T0 = Date.now();

function satellitesBody(): Json {
  const now = Date.now();
  return {
    hub: { started: true, enabled: true, hub_boot_id: "mockhub", configured: 1, connected: 1, config_error: null },
    degraded: false,
    satellites: [
      {
        id: "sat1",
        configured: true,
        enabled: true,
        note: "光谱电脑（模拟）",
        state: "connected",
        ever_connected: true,
        boot_id: "boot-7f3a91c2",
        connected_since: now - 754_000,
        last_seen_ms: now - 300,
        sw: "MastSatellite 1.0 (mock)",
        // 占位值（RFC 5737 文档地址段）：这个文件经 source map 进包，不许写站点的真实主机名 / 地址
        host: "spectro-pc",
        peer: "192.0.2.10",
        rtt_ms: 2.7,
        clock_offset_ms: -1.1,
        clock_samples: 32,
        pending_commands: sim.acquiring ? 1 : 0,
        counters: { connects: 2, restarts: 1 },
        // 一次较早的急停：真卫星的 xyz 没有远程停止，回执里总会点它的名（它闲着，所以只留记录）
        last_estop: {
          reason: "顶栏急停（模拟）",
          sent_ms: MOCK_T0 - 300_000,
          ack: {
            stopped: ["ccd", "pol", "pico", "aotf", "spe"],
            active: ["ccd"],
            errors: [{ device: "xyz", message: "Motion_Manipulator 没有远程停止 —— 请在它自己的窗口里按 STOP（模拟）" }],
            rx_ms: MOCK_T0 - 299_600,
          },
        },
        devices: [
          {
            id: "ccd", kind: "spectrometer", driver: "lightfield", model: "PyLoN 100B eXcelon", serial: "00000", sim: true,
            state: sim.acquiring ? "busy" : "ready", stale: false, latest_ts_ms: now - 800,
            latest: { temperature_c: -120, temperature_locked: true, exposure_ms: sim.exposureMs, frames: 1, grating: "600 g/mm", center_wavelength_nm: 680, exit_port: "front", laser_line_nm: LASER, is_running: sim.acquiring },
          },
          {
            id: "pol", kind: "rotation_stage", driver: "kinesis_kdc101", model: "KDC101 + PRM1-Z8", serial: "00000000", sim: true,
            state: "ready", stale: false, latest: { position_deg: sim.angle, homed: sim.homed, moving: false },
            axis: { position_deg: sim.angle, moving: Math.abs(sim.angle - sim.angleTarget) > 0.01 },
          },
          { id: "pico", kind: "openloop_stepper", driver: "picomotor8742", model: "8742", sim: true, state: "ready", stale: false, latest: { axes: { "1": { steps: sim.pico["1"] }, "2": { steps: sim.pico["2"] } } } },
          { id: "xyz", kind: "xyz_stage", driver: "motion_manipulator", model: "UIRobot", sim: true, state: "operator_hold", operator_hold: true, stale: false, latest: { x_mm: sim.xyz.x, y_mm: sim.xyz.y, z_mm: sim.xyz.z, moving: false } },
          { id: "aotf", kind: "aotf", driver: "ds1", model: "Brimrose DS1", sim: false, state: "not_started", stale: true, latest: null },
          { id: "spe", kind: "file_watcher", driver: "spe_watcher", sim: true, state: "ready", stale: false, latest: { root: "<数据目录>", seen: 12, queued: 0, uploaded: 12 } },
        ],
      },
    ],
  };
}

const PORT_GUARD = {
  checked_ms: Date.now(),
  ok: false,
  enforce: true,
  conflicts: [],
  warnings: [{ kind: "vendor_gui", resource: "PDXC2（USB）", severity: "warn", pid: 5120, process: "Kinesis.exe", detail: "厂家界面在运行，可能占着控制器" }],
  infos: [],
  errors: [],
  message: "1 条提醒",
};

const SKILLS: Record<string, { safety: string; zh: string; description: string }> = {
  StopSpectrometer: { safety: "auto", zh: "停止光谱仪", description: "停止采集（免令牌）" },
  StopSatelliteDevices: { safety: "auto", zh: "停止卫星设备", description: "停止全部卫星设备（免令牌）" },
  AcquireSpectrum: { safety: "confirm", zh: "采集光谱", description: "（模拟）采一条光谱并推到 LiveHub" },
  GetSpectrometerStatus: { safety: "auto", zh: "读光谱仪状态", description: "只读" },
  SetSpectrometerCenter: { safety: "confirm", zh: "设中心波长", description: "（模拟）" },
  SetSpectrometerGrating: { safety: "confirm", zh: "换光栅", description: "（模拟）" },
  SetSpectrometerExitPort: { safety: "confirm", zh: "切出口", description: "（模拟）" },
  SetPolarizerAngle: { safety: "confirm", zh: "设偏振角", description: "（模拟）转偏振片" },
  HomePolarizer: { safety: "confirm", zh: "偏振片回零", description: "（模拟）" },
  GetPolarizerAngle: { safety: "auto", zh: "读偏振角", description: "只读" },
  PicomotorJog: { safety: "confirm", zh: "Picomotor 走步", description: "（模拟）" },
  GetPicomotorSteps: { safety: "auto", zh: "读 Picomotor 步数", description: "只读" },
  MoveXYZStage: { safety: "confirm", zh: "移动 XYZ 台", description: "（模拟）xyz 处于人工接管时会被拒绝" },
  OpticalStageGetPos: { safety: "auto", zh: "读台子位置", description: "只读" },
  ListNanonisMotorGroups: { safety: "auto", zh: "列出 Nanonis 电机组", description: "只读" },
  NanonisMotorGroupMove: { safety: "confirm", zh: "Nanonis 光学组走步", description: "（模拟）只允许光学用途的组" },
  StopMotor: { safety: "auto", zh: "停止电机", description: "Motor_StopMove，停所有电机组" },
};

function runSkill(name: string, p: Json): Json {
  const ok = (summary: string, data: Json = {}) => ({ ok: true, success: true, skill: name, summary, data, error: "", elapsed_s: 0.4, nanonis_calls: 0, degraded: false });
  const bad = (error: string) => ({ ok: true, success: false, skill: name, summary: "", data: {}, error, elapsed_s: 0.1, nanonis_calls: 0, degraded: false });
  switch (name) {
    case "AcquireSpectrum":
      if (p.dry_run) return ok("试跑通过：限值与状态正常（没有曝光）");
      sim.exposureMs = typeof p.exposure_ms === "number" ? p.exposure_ms : sim.exposureMs;
      sim.framesLeft = typeof p.frames === "number" ? p.frames : 1;
      sim.acquiring = true;
      sim.run = { id: `mock-${Date.now().toString(36)}`, sum: [], n: 0 };
      declare(
        ACQUIRED,
        "spectrum",
        { title: "光谱 · ccd", source: "AcquireSpectrum", x_label: "Wavelength", x_unit: "nm", y_label: "Counts", y_unit: "counts", meta: { laser_line_nm: LASER, device_id: "ccd", satellite: "sat1" } },
        200,
      ).axis = { ver: 1, unit: "nm", x: AXIS_NM };
      publish("sat1/events", { text: `ccd 开始采集（${sim.framesLeft} 帧 × ${sim.exposureMs} ms）`, level: "info" });
      return ok(`已开始：${sim.framesLeft} 帧`, { live_stream: ACQUIRED, run_id: sim.run.id });
    case "StopSpectrometer":
    case "StopSatelliteDevices":
      sim.acquiring = false;
      sim.run = null;
      sim.angleTarget = sim.angle;
      publish("sat1/events", { text: "停止：采集已中止", level: "warn" });
      return ok("已停止");
    case "SetPolarizerAngle": {
      const a = Number(p.angle_deg);
      sim.angleTarget = p.relative ? sim.angle + a : a;
      return ok(`polariser: ${sim.angle.toFixed(3)}° → ${sim.angleTarget.toFixed(3)}°`);
    }
    case "HomePolarizer":
      sim.angleTarget = 0;
      sim.homed = true;
      return ok("polariser homed");
    case "GetPolarizerAngle":
      return ok(`polariser at ${sim.angle.toFixed(3)}°`);
    case "PicomotorJog": {
      const ax = String(p.axis);
      sim.pico[ax] = (sim.pico[ax] ?? 0) + Number(p.steps);
      return ok(`picomotor ${ax}: ${Number(p.steps) >= 0 ? "+" : ""}${p.steps} steps（开环）`);
    }
    case "GetPicomotorSteps":
      return ok(`picomotor: ${Object.entries(sim.pico).map(([k, v]) => `${k}=${v}`).join(", ")}`);
    case "MoveXYZStage":
      return bad("[MoveXYZStage] operator_hold：卫星上 xyz 处于人工接管，枢纽命令一律拒绝（停止除外）");
    case "GetSpectrometerStatus":
      return ok(`ccd: -120.0 °C（锁定）· 600 g/mm · 中心 680 nm · 曝光 ${sim.exposureMs} ms`);
    case "OpticalStageGetPos":
      return ok("", { position: 12.345, unit: "µm" });
    case "ListNanonisMotorGroups":
      return ok("stm_coarse_group=0；光学组：1 Mirror X/Y（laser_mirror）、2 Beam Deflection、3 Lens");
    case "NanonisMotorGroupMove":
      if (typeof p.group !== "string") return bad("[NanonisMotorGroupMove] Parameter 'group' must be str");
      if (p.group.trim() === "0") return bad("[NanonisMotorGroupMove] group 0 是 STM 粗动组 —— 光学电机组技能一律拒绝");
      return ok(`${p.dry_run ? "[DRY RUN] " : ""}group ${p.group} ${p.axis} ${p.direction}${p.steps} steps`);
    case "StopMotor":
      return ok("", { stopped: true });
    default:
      return ok("（模拟）完成");
  }
}

function skillCard(name: string): Json {
  const s = SKILLS[name];
  if (!s) return { found: false, degraded: false, name };
  return {
    found: true, degraded: false, name, zh: s.zh, safety: s.safety, level: 0, version: "1.0.0",
    description: s.description, description_zh: s.description, domain: "光学", source: "builtin", source_zh: "内置",
    category: "", parameters: [], preconditions: [], postconditions: [], outputs: [], tags: [],
  };
}

function restFrames(id: string, url: URL): Response {
  const s = streams.get(id);
  if (!s) return json({ detail: `没有这个实时流：${id}` }, 404);
  const sinceRaw = url.searchParams.get("since");
  const since = sinceRaw === null ? undefined : Number(sinceRaw);
  const limit = Number(url.searchParams.get("limit") || "500");
  const all = select(s, since);
  const frames = all.slice(0, limit);
  return json({
    hub: HUB_ID,
    meta: describe(s),
    axis: s.axis ? { ...s.axis, n: s.axis.x.length } : null,
    axes: [],
    frames,
    more: all.length > limit,
    dropped: null,
  });
}

// ── 安装 ───────────────────────────────────────────────────────────────────

function requested(): boolean {
  try {
    const q = new URLSearchParams(window.location.search);
    if (q.get("livemock") === "1") sessionStorage.setItem("mast.livemock", "1");
    if (q.get("livemock") === "0") sessionStorage.removeItem("mast.livemock");
    return sessionStorage.getItem("mast.livemock") === "1";
  } catch {
    return false;
  }
}

/** STM 电脑上的本机光学台（只读的清单与读数，给页面看个样子）。 */
const OPTICS_DEVICES = {
  degraded: false,
  devices: [
    { id: "pdxc2", name: "Thorlabs PDXC2 延迟线", type: "thorlabs_pdxc2", enabled: true, connected: true, axes: [{ name: "x", unit: "um", min_pos: 0, max_pos: 20000, role: "delay" }] },
    { id: "e861", name: "PI E-861", type: "pi_gcs", enabled: true, connected: true, axes: [{ name: "1", unit: "mm", min_pos: -6.5, max_pos: 6.5 }] },
    { id: "zeyou2d", name: "泽攸二维台", type: "pztc_nm003", enabled: true, connected: false, axes: [{ name: "x", unit: "um", min_pos: -5000, max_pos: 5000 }, { name: "y", unit: "um", min_pos: -5000, max_pos: 5000 }] },
  ],
  delay_line: { device_id: "pdxc2", axis: "x", delay_range_ps: [0, 133.4] },
};

/** 冒充后端的那几个地址；别的返回 null（交给真的后端）。 */
async function mockResponse(url: URL, body: () => Promise<unknown>): Promise<Response | null> {
  // 手动控制面板（装置描述、相机画面、令牌持有者、针尖状态、手动控制的技能）
  const manual = await manualMockResponse(url, body);
  if (manual) return manual;
  const path = url.pathname;
  if (path === "/api/optics/devices") return json(OPTICS_DEVICES);
  if (path === "/api/optics/delay") return json({ ok: true, data: { delay_ps: 33.35, stage_position: 5000, stage_unit: "um" } });
  if (path === "/api/optics/position") return json({ ok: true, data: { position: 12.345 } });
  if (path === "/api/live/streams") return json({ hub: HUB_ID, items: [...streams.values()].map(describe) });
  if (path.startsWith("/api/live/streams/")) {
    return restFrames(decodeURIComponent(path.slice("/api/live/streams/".length)), url);
  }
  if (path === "/api/satellites") return json(satellitesBody());
  if (path === "/api/port-guard") return json({ ...PORT_GUARD, checked_ms: Date.now() });
  // 出厂默认：ters_optics 关着（只影响 agent 的工具表），/ters 页会写一行说明
  if (path === "/api/settings/hardware-modules") {
    const skills = Object.keys(SKILLS).filter((s) => !["OpticalStageGetPos", "StopMotor"].includes(s));
    return json({
      enabled_count: 0,
      gated_skill_count: skills.length,
      modules: [{ id: "ters_optics", name: "TERS 光学 / 光谱（模拟）", hardware: "卫星节点", description: "", enabled: false, skill_count: skills.length, skills }],
    });
  }
  const m = /^\/api\/skills\/([A-Za-z0-9_]+)(\/execute)?$/.exec(path);
  if (m && SKILLS[m[1]!]) {
    if (!m[2]) return json(skillCard(m[1]!));
    const b = (await body().catch(() => ({}))) as Json;
    await new Promise((r) => setTimeout(r, 400));
    return json(runSkill(m[1]!, (b?.params ?? {}) as Json));
  }
  return null;
}

export function installLiveDevMockIfRequested(apiClient?: { use: (...m: Middleware[]) => void }): boolean {
  if (typeof window === "undefined" || !requested()) return false;
  const RealWS = window.WebSocket;
  const realFetch = window.fetch.bind(window);

  const Patched = function (this: unknown, url: string | URL, protocols?: string | string[]) {
    const u = new URL(String(url), window.location.href);
    if (u.pathname === "/ws/live") return new MockLiveSocket(u.toString()) as unknown as WebSocket;
    return new RealWS(url, protocols);
  } as unknown as typeof WebSocket;
  Object.assign(Patched, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 });
  window.WebSocket = Patched;

  // 直接用 fetch 的调用方（liveClient、卫星状态）
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const hit = await mockResponse(new URL(raw, window.location.href), async () =>
      init?.body ? JSON.parse(String(init.body)) : input instanceof Request ? input.clone().json() : {},
    );
    return hit ?? realFetch(input, init);
  };
  // openapi-fetch 在建客户端时就拿走了 fetch —— 走它的中间件（onRequest 可以直接回一个 Response）
  const middleware: Middleware = {
    onRequest: async ({ request }) =>
      (await mockResponse(new URL(request.url), () => request.clone().json())) ?? undefined,
  };
  apiClient?.use(middleware);

  startSim();
  // eslint-disable-next-line no-console
  console.info(`[${LIVE_DEV_MOCK_MARKER}] 实时数据模拟源已启用（?livemock=0 关闭）`);
  return true;
}
