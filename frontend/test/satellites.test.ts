// 卫星状态卡的读法（src/lib/satellites.ts）—— 按 mast/satellite/schemas.py 的响应形状。
//
//     cd frontend && npm run test:unit
//
// 钉的是「哪些情况必须让操作员看见」：失联、从未连上、人工接管、陈旧、时钟偏差、端口被占。

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  attentionItems,
  axisReadouts,
  clockOffsetLevel,
  estopLeftovers,
  moduleEnabled,
  normalizeLiveness,
  normalizePortGuard,
  normalizeSatellites,
  spectrometerReadouts,
  toEpochMs,
} from "../src/lib/satellites.ts";

/** GET /api/satellites 的样子（SatellitesResponse）。 */
const SAMPLE = {
  hub: { started: true, enabled: true, hub_boot_id: "h1", configured: 2, connected: 1, config_error: null },
  degraded: false,
  satellites: [
    {
      id: "sat1",
      configured: true,
      enabled: true,
      note: "Spec 光谱电脑",
      state: "connected",
      ever_connected: true,
      boot_id: "b-123",
      connected_since: 1_760_000_000_000,
      rtt_ms: 3.2,
      clock_offset_ms: -1.5,
      clock_samples: 32,
      counters: { connects: 3, restarts: 1 },
      sw: "MastSatellite 0.1",
      host: "dell",
      devices: [
        {
          id: "ccd",
          kind: "spectrometer",
          driver: "lightfield",
          model: "PyLoN 100B",
          sim: false,
          state: "ready",
          caps: ["status", "acquire"],
          stale: false,
          latest: { temperature_c: -120, temperature_locked: true, exposure_ms: 1000, grating: "600 g/mm", laser_line_nm: 632.8 },
          latest_ts_ms: 1_760_000_001_000,
        },
        {
          id: "pol",
          kind: "rotation_stage",
          state: "operator_hold",
          stale: false,
          latest: { position_deg: 10, homed: true },
          axis: { position_deg: 12.3456, moving: true },
        },
        {
          id: "pico",
          kind: "openloop_stepper",
          state: "ready",
          stale: false,
          latest: { axes: { "2": { steps: -40 }, "1": { steps: 1200, moving: false } } },
        },
        { id: "xyz", kind: "xyz_stage", state: "offline", stale: true, latest: null },
        { kind: "no-id" },
      ],
    },
    { id: "spare", configured: true, enabled: true, state: "lost", ever_connected: false, devices: [] },
    { id: "old", configured: true, enabled: false, state: "lost", devices: [] },
  ],
};

const GUARD = {
  checked_ms: 1_760_000_000_000,
  ok: false,
  enforce: true,
  conflicts: [{ kind: "nanonis_tcp", resource: "Nanonis TCP 6501", severity: "conflict", pid: 4321, process: "LabVIEW.exe", detail: "Stage.vi" }],
  warnings: [{ kind: "serial_port", resource: "COM17", severity: "warn", process: "Kinesis.exe" }],
  errors: [],
  message: "1 处冲突",
};

describe("normalizeSatellites", () => {
  const snap = normalizeSatellites(SAMPLE);

  it("解出卫星、设备；没有 id 的设备丢掉", () => {
    assert.equal(snap.satellites.length, 3);
    const t1 = snap.satellites[0]!;
    assert.equal(t1.devices.length, 4);
    assert.equal(t1.connectedSinceMs, 1_760_000_000_000);
    assert.equal(t1.restarts, 1, "重启次数在 counters.restarts");
    assert.equal(snap.hub?.connected, 1);
  });

  it("device_state 的 data 在 latest，axis_state 的在 axis", () => {
    const ccd = snap.satellites[0]!.devices[0]!;
    assert.equal(ccd.status?.temperature_c, -120);
    assert.equal(ccd.statusMs, 1_760_000_001_000);
    const pol = snap.satellites[0]!.devices[1]!;
    assert.equal(pol.axis?.position_deg, 12.3456);
  });

  it("从没连上过的 lost 叫「从未连上」；停用的叫「已停用」", () => {
    assert.equal(snap.satellites[1]!.state, "never");
    assert.equal(snap.satellites[2]!.state, "disabled");
  });

  it("人工接管从 state 或 operator_hold 任一处都认得出", () => {
    const pol = snap.satellites[0]!.devices.find((d) => d.id === "pol")!;
    assert.equal(pol.operatorHold, true);
  });

  it("嵌套写法 latest.{device_state, axis_state} 也认", () => {
    const s = normalizeSatellites({
      satellites: [{ id: "a", devices: [{ id: "p", kind: "rotation_stage", latest: { device_state: { position_deg: 1 }, axis_state: { position_deg: 2 } } }] }],
    });
    const d = s.satellites[0]!.devices[0]!;
    assert.equal(d.status?.position_deg, 1);
    assert.equal(d.axis?.position_deg, 2);
  });

  it("裸数组也认；什么都认不出时是空快照而不是抛", () => {
    assert.equal(normalizeSatellites([{ id: "a" }]).satellites.length, 1);
    assert.deepEqual(normalizeSatellites("nope").satellites, []);
  });
});

describe("端口归属", () => {
  it("冲突和警告分开", () => {
    const g = normalizePortGuard(GUARD);
    assert.equal(g.ok, false);
    assert.equal(g.conflicts.length, 1);
    assert.equal(g.conflicts[0]!.process, "LabVIEW.exe");
    assert.equal(g.warnings[0]!.resource, "COM17");
  });

  it("读不出来的响应：ok 默认真（不凭空造冲突），但 degraded 照实", () => {
    const g = normalizePortGuard({ degraded: true, errors: ["boom"] });
    assert.equal(g.ok, true);
    assert.equal(g.degraded, true);
    assert.deepEqual(g.errors, ["boom"]);
  });

  it("检查没跑全（complete=false）要认出来 —— 没列出冲突不等于没有冲突", () => {
    assert.equal(normalizePortGuard({ ok: true, complete: false }).complete, false);
    assert.equal(normalizePortGuard({ ok: true }).complete, true);
  });

  it("设备的 role（satellites.json 里配的用途）", () => {
    const s = normalizeSatellites({ satellites: [{ id: "t", devices: [{ id: "ccd", kind: "spectrometer", role: "raman_ccd" }] }] });
    assert.equal(s.satellites[0]!.devices[0]!.role, "raman_ccd");
  });
});

describe("显示判据", () => {
  it("活性词表", () => {
    assert.equal(normalizeLiveness("connected", true), "connected");
    assert.equal(normalizeLiveness("lost", true, false), "never");
    assert.equal(normalizeLiveness("lost", true, true), "lost");
    assert.equal(normalizeLiveness("", true), "unknown");
    assert.equal(normalizeLiveness("connected", false), "disabled");
  });

  it("时钟偏差：100 ms 提醒，500 ms 是 HLC 的门槛", () => {
    assert.equal(clockOffsetLevel(null), "unknown");
    assert.equal(clockOffsetLevel(3), "ok");
    assert.equal(clockOffsetLevel(-150), "warn");
    assert.equal(clockOffsetLevel(500), "bad");
  });

  it("时间格式：秒、毫秒、ISO", () => {
    assert.equal(toEpochMs(1_760_000_000), 1_760_000_000_000);
    assert.equal(toEpochMs(1_760_000_000_123), 1_760_000_000_123);
    assert.equal(toEpochMs("2026-10-19T00:00:00Z"), Date.parse("2026-10-19T00:00:00Z"));
    assert.equal(toEpochMs(null), null);
  });

  it("轴读数：axis_state 比 device_state 新；Picomotor 说明是累计步数；读不到是「—」", () => {
    const devs = normalizeSatellites(SAMPLE).satellites[0]!.devices;
    const pol = axisReadouts(devs.find((d) => d.id === "pol")!);
    assert.equal(pol[0]!.value, "12.346°");
    assert.equal(pol[0]!.moving, true);
    const pico = axisReadouts(devs.find((d) => d.id === "pico")!);
    assert.deepEqual(pico.map((r) => [r.label, r.value]), [["轴 1", "1200 步"], ["轴 2", "-40 步"]]);
    assert.match(pico[0]!.note ?? "", /不是位置反馈/);
    const xyz = axisReadouts(devs.find((d) => d.id === "xyz")!);
    assert.deepEqual(xyz.map((r) => r.value), ["—", "—", "—"]);
  });

  it("光谱仪读数", () => {
    const ccd = normalizeSatellites(SAMPLE).satellites[0]!.devices[0]!;
    const r = spectrometerReadouts(ccd);
    assert.equal(r.find((x) => x.label === "CCD 温度")!.value, "-120.0 °C");
    assert.equal(r.find((x) => x.label === "CCD 温度")!.note, "已锁定");
    assert.equal(r.find((x) => x.label === "激发线")!.value, "632.8 nm");
  });

  it("要提醒的：人工接管、从未连上、端口被占；停用的卫星不提醒", () => {
    const items = attentionItems(normalizeSatellites(SAMPLE), normalizePortGuard(GUARD));
    assert.ok(items.some((s) => s.includes("sat1/pol") && s.includes("人工接管")));
    assert.ok(items.some((s) => s.includes("spare") && s.includes("从未连上")));
    assert.ok(items.some((s) => s.includes("LabVIEW.exe") && s.includes("4321")));
    assert.ok(!items.some((s) => s.includes("old")));
  });

  it("多处端口冲突：提醒框里只一行汇总（逐条在「端口归属」里）", () => {
    const c0 = GUARD.conflicts[0]!;
    const many = { ...GUARD, conflicts: [6501, 6502, 6503, 6504].map((p) => ({ ...c0, resource: `tcp:${p}` })) };
    const items = attentionItems(normalizeSatellites(SAMPLE), normalizePortGuard(many));
    const lines = items.filter((s) => s.includes("tcp:65"));
    assert.equal(lines.length, 1);
    assert.ok(lines[0]!.includes("4 处冲突") && lines[0]!.includes("tcp:6501"));
  });
});

describe("硬件模块 ters_optics 的开关", () => {
  const RESP = {
    enabled_count: 1,
    gated_skill_count: 20,
    modules: [
      { id: "kpfm", enabled: true },
      { id: "ters_optics", enabled: false },
    ],
  };

  it("关着 = false，开着 = true", () => {
    assert.equal(moduleEnabled(RESP, "ters_optics"), false);
    assert.equal(moduleEnabled(RESP, "kpfm"), true);
  });

  it("读不到不当成关着：旧后端没有这个模块、请求还没回来 / 失败", () => {
    assert.equal(moduleEnabled(RESP, "no_such_module"), null);
    assert.equal(moduleEnabled({}, "ters_optics"), null);
    assert.equal(moduleEnabled(undefined, "ters_optics"), null);
  });
});

// 急停回执点名没停下来的设备。真卫星的 xyz（Motion_Manipulator）没有远程停止，
// 每次急停都会出现在 errors 里 —— 当时闲着就不该惊动人，还在动（或说不准）就必须亮出来。
describe("急停回执：没停下来的设备", () => {
  const sat = (opts: { state?: string; moving?: boolean | null; stale?: boolean; ack?: unknown }) => ({
    satellites: [
      {
        id: "sat1",
        enabled: true,
        ever_connected: true,
        state: opts.state ?? "connected",
        last_estop: {
          reason: "顶栏急停",
          sent_ms: 1_760_000_000_000,
          ack:
            opts.ack !== undefined
              ? opts.ack
              : {
                  stopped: ["ccd", "pol"],
                  active: ["ccd"],
                  errors: [{ device: "xyz", message: "没有远程停止，请在它自己的窗口里按 STOP" }],
                  rx_ms: 1_760_000_000_400,
                },
        },
        devices: [
          {
            id: "xyz",
            kind: "xyz_stage",
            state: "ready",
            stale: opts.stale ?? false,
            axis: opts.moving === null ? null : { x_mm: 1, moving: opts.moving ?? false },
          },
        ],
      },
    ],
  });

  it("回执原样读出来：已停、当时在动的、没停下来的与卫星原话", () => {
    const e = normalizeSatellites(sat({})).satellites[0]!.lastEstop!;
    assert.equal(e.reason, "顶栏急停");
    assert.equal(e.sentMs, 1_760_000_000_000);
    assert.deepEqual(e.ack?.stopped, ["ccd", "pol"]);
    assert.deepEqual(e.ack?.active, ["ccd"]);
    assert.deepEqual(e.ack?.errors, [{ device: "xyz", message: "没有远程停止，请在它自己的窗口里按 STOP" }]);
    assert.equal(e.ack?.rxMs, 1_760_000_000_400);
  });

  it("还在动：亮警示，带卫星原话", () => {
    const items = attentionItems(normalizeSatellites(sat({ moving: true })));
    const hit = items.filter((s) => s.includes("sat1/xyz") && s.includes("没停下来"));
    assert.equal(hit.length, 1);
    assert.ok(hit[0]!.includes("现在还在动") && hit[0]!.includes("按 STOP"));
  });

  it("读到它没在动（链路连着、不陈旧）：不亮警示，只留记录", () => {
    const snap = normalizeSatellites(sat({ moving: false }));
    assert.ok(!attentionItems(snap).some((s) => s.includes("没停下来")));
    assert.deepEqual(estopLeftovers(snap.satellites[0]!).map((x) => x.active), [false]);
  });

  it("说不准就照样亮：断线、陈旧、没有轴读数", () => {
    for (const o of [{ state: "lost", moving: false }, { stale: true, moving: false }, { moving: null }]) {
      const items = attentionItems(normalizeSatellites(sat(o)));
      assert.ok(items.some((s) => s.includes("sat1/xyz") && s.includes("没停下来")), JSON.stringify(o));
    }
  });

  it("还没回执、回执里没有错误：没有可点名的设备", () => {
    assert.deepEqual(estopLeftovers(normalizeSatellites(sat({ ack: null })).satellites[0]!), []);
    const clean = { stopped: ["ccd", "pol", "xyz"], errors: [], rx_ms: 1 };
    assert.deepEqual(estopLeftovers(normalizeSatellites(sat({ ack: clean })).satellites[0]!), []);
  });
});
