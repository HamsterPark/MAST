// 手动控制面板的纯逻辑（src/lib/manual/*）：描述的读法与分组、按钮 → 技能参数、确认闸门、
// 按住连续点动（假时钟）、解锁超时、停止全部。
//
//     cd frontend && npm run test:unit

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  actionGate,
  axisExtraBits,
  buildActionParams,
  busyHolderText,
  canHold,
  defaultValues,
  DIRECT_EXEC_OWNER,
  dueReadouts,
  groupDevices,
  holderBlocksStop,
  inputParams,
  isRefusedBusy,
  jogOutcome,
  limitLines,
  matchAxisValues,
  needsUnlock,
  normalizeManualDevices,
  normalizeReadouts,
  readoutPlan,
  restLabel,
  SATELLITE_FALLBACK_POLL_S,
  stopHadNothingToStop,
  wantsBatchReadout,
  type ManualAction,
  type ManualDevice,
} from "../src/lib/manual/protocol.ts";
import { createHoldRepeat, type HoldClock, type HoldSendResult } from "../src/lib/manual/holdRepeat.ts";
import { cardPlan } from "../src/lib/manual/plan.ts";
import { UNLOCK_IDLE_MS, UnlockStore } from "../src/lib/manual/unlock.ts";
import { STOP_ALL_STEPS, stopAllOptics } from "../src/lib/manual/stopAll.ts";

// ── 一份按契约 §13.1 写的描述 ───────────────────────────────────────────────

const JOG = {
  id: "jog",
  label: "点动",
  skill: "OpticalStageMove",
  fixed: { device_id: "pdxc2", relative: true },
  params: [{ name: "position", type: "float", unit: "um", min: -500, max: 500, default: 10, required: true }],
  per_axis: true,
  direction: "signed",
  direction_param: "position",
  safety: "confirm",
  bypass_lock: false,
  hold_repeat_ms: 250,
  stop_on_release: true,
};
const GROUP_JOG = {
  id: "jog",
  label: "走步",
  skill: "NanonisMotorGroupMove",
  fixed: { group: "1" },
  params: [
    { name: "axis", type: "str", required: true },
    { name: "direction", type: "str", allowed_values: ["+", "-"], required: true },
    { name: "steps", type: "int", min: 1, max: 200, default: 10, required: true },
  ],
  per_axis: true,
  direction: "signed",
  direction_param: "direction",
  safety: "confirm",
  hold_repeat_ms: 400,
  stop_on_release: true,
};
const STOP = { id: "stop", label: "停止", skill: "StopOpticalStage", fixed: { device_id: "pdxc2" }, params: [], safety: "auto", bypass_lock: true };

const RESP = {
  devices: [
    { id: "local/camera/zelux1", machine: "local", kind: "camera", label: "对光相机", actions: [] },
    { id: "sat1/pol", machine: "sat1", kind: "rotation_stage", label: "偏振片", readout: { kind: "live", stream: "sat1/pol/angle" } },
    { id: "local/stage/pdxc2", machine: "local", kind: "linear_stage", label: "PDXC2", axes: [{ name: "x", unit: "um", min: 0, max: 20000, position: 5000 }], actions: [JOG, STOP], readout: { kind: "poll", skill: "GetOpticalStageStatus", params: { device_id: "pdxc2" }, interval_s: 0.1 } },
    { id: "local/nanonis_group/1", kind: "nanonis_motor_group", label: "Mirror X/Y", tip_requirement: "tip_withdrawn", actions: [GROUP_JOG] },
    { id: "ters9/xyz", kind: "xyz_stage", label: "XYZ", available: false, unavailable_reason: "协议未到，只读" },
    { label: "没有 id 的项要丢掉" },
  ],
  degraded: { satellite: "枢纽没起来" },
  notes: ["一条说明"],
};

describe("描述端点的读法", () => {
  const snap = normalizeManualDevices(RESP);

  it("认得出的设备都读出来；没有 id 的丢掉；machine 缺了从 id 推", () => {
    assert.deepEqual(snap.devices.map((d) => d.id), [
      "local/camera/zelux1",
      "sat1/pol",
      "local/stage/pdxc2",
      "local/nanonis_group/1",
      "ters9/xyz",
    ]);
    assert.equal(snap.devices[3]!.machine, "local");
    assert.equal(snap.devices[4]!.machine, "ters9");
    assert.equal(snap.devices[4]!.available, false);
    assert.equal(snap.devices[4]!.unavailableReason, "协议未到，只读");
    assert.equal(snap.devices[3]!.tipRequirement, "tip_withdrawn");
  });

  it("degraded 的三种写法：{类: 原因} 也认，原因进 notes", () => {
    assert.equal(snap.degraded, true);
    assert.deepEqual(snap.degradedParts, ["satellite"]);
    assert.ok(snap.notes.includes("satellite：枢纽没起来"));
    assert.equal(normalizeManualDevices({ devices: [], degraded: true }).degraded, true);
    assert.deepEqual(normalizeManualDevices({ devices: [], degraded: ["camera"] }).degradedParts, ["camera"]);
    assert.equal(normalizeManualDevices(null).devices.length, 0);
  });

  it("degraded_sources（hub 的写法：degraded 是布尔，哪一类读不到看来源列表）", () => {
    const s = normalizeManualDevices({ devices: [], degraded: true, degraded_sources: ["camera", "satellite"], notes: ["相机清单读不到"] });
    assert.equal(s.degraded, true);
    assert.deepEqual(s.degradedParts, ["camera", "satellite"]);
    assert.deepEqual(normalizeManualDevices({ devices: [], degraded: false, degraded_sources: [] }).degraded, false);
  });

  it("按钮的 hint 读进来；卡片底部列出，按钮文字已经说了的不重复", () => {
    const d = normalizeManualDevices({
      devices: [{
        id: "local/nanonis_group/1",
        kind: "nanonis_motor_group",
        actions: [
          { ...GROUP_JOG, hint: "StartMove(group=1)：开环走步，没有位置反馈" },
          { id: "stop", label: "停止走步（所有组）", skill: "StopMotor", bypass_lock: true, hint: "停所有电机组，含 STM 粗动组" },
          { id: "set_zero", label: "当前位置记为 0（不移动）", skill: "HomeOpticalStage", hint: "当前位置记为 0（不移动）" },
          { id: "home", label: "寻零", skill: "HomeOpticalStage", hint: "" },
        ],
      }],
    }).devices[0]!;
    assert.equal(d.actions[1]!.hint, "停所有电机组，含 STM 粗动组");
    assert.equal(d.actions[3]!.hint, null, "空串 = 没有");
    assert.deepEqual(cardPlan(d, false).hints, [
      { label: "走步", hint: "StartMove(group=1)：开环走步，没有位置反馈", stop: false },
      { label: "停止走步（所有组）", hint: "停所有电机组，含 STM 粗动组", stop: true },
    ]);
  });

  it("轮询读数不许比 0.5 s 更密（每张卡每次都要读一回硬件）", () => {
    const r = snap.devices[2]!.readout;
    assert.equal(r?.kind, "poll");
    if (r?.kind === "poll") assert.equal(r.intervalS, 1);
  });

  it("按机器分组，顺序固定：本机台子 → Nanonis 光学组 → 各卫星 → 相机", () => {
    const g = groupDevices(snap.devices);
    assert.deepEqual(g.map((x) => x.key), ["local", "nanonis", "sat:sat1", "sat:ters9", "camera"]);
    assert.deepEqual(g[0]!.devices.map((d) => d.id), ["local/stage/pdxc2"]);
    assert.ok(g[2]!.title.includes("sat1"), "卫星 id 照接口返回的显示");
  });
});

describe("按钮 → 技能参数", () => {
  const snap = normalizeManualDevices(RESP);
  const jog = snap.devices[2]!.actions[0]!;
  const gjog = snap.devices[3]!.actions[0]!;

  it("要填的只有幅度：轴参数、方向枚举由按钮决定，fixed 里的不填", () => {
    assert.deepEqual(inputParams(jog).map((p) => p.name), ["position"]);
    assert.deepEqual(inputParams(gjog).map((p) => p.name), ["steps"]);
    assert.deepEqual(defaultValues(jog), { position: "10" });
  });

  it("数值方向参数：− 按钮发负的幅度；轴名进 axis；fixed 原样带上", () => {
    const r = buildActionParams(jog, { axis: "x", values: { position: "25" }, sign: -1 });
    assert.deepEqual(r, { ok: true, params: { device_id: "pdxc2", relative: true, axis: "x", position: -25 } });
  });

  it("枚举方向参数：+ / - 按按钮填", () => {
    const r = buildActionParams(gjog, { axis: "X", values: { steps: "10" }, sign: 1 });
    assert.deepEqual(r, { ok: true, params: { group: "1", axis: "X", direction: "+", steps: 10 } });
  });

  it("挡住明显填错的：负幅度、超范围、非整数、不是数", () => {
    assert.equal(buildActionParams(jog, { axis: "x", values: { position: "-5" }, sign: 1 }).ok, false);
    const big = buildActionParams(jog, { axis: "x", values: { position: "600" }, sign: 1 });
    assert.equal(big.ok, false);
    if (!big.ok) assert.match(big.error, /超出 -500 … 500 um/);
    assert.equal(buildActionParams(gjog, { axis: "X", values: { steps: "2.5" }, sign: 1 }).ok, false);
    assert.equal(buildActionParams(gjog, { axis: "X", values: { steps: "abc" }, sign: 1 }).ok, false);
    assert.equal(buildActionParams(gjog, { axis: "X", values: {}, sign: 1 }).ok, true, "有缺省的必填项留空 = 用技能的缺省");
    assert.equal(buildActionParams(jog, { axis: null, values: { position: "1" }, sign: 1 }).ok, false, "逐轴动作缺轴名");
  });

  it("单次上限列出来（解锁确认时给人看）", () => {
    assert.deepEqual(limitLines(snap.devices[2]!), ["点动：position -500 … 500 um"]);
  });

  it("单次上限优先用轴上写的真上限：光学组看 max_steps_per_cmd（不是技能的 1…10000）；台子写明「无单独上限」", () => {
    const grp = normalizeManualDevices({
      devices: [{
        id: "local/nanonis_group/1", kind: "nanonis_motor_group",
        axes: [{ name: "X", unit: "步", extra: { max_steps_per_cmd: 200 } }, { name: "Y", unit: "步", extra: { max_steps_per_cmd: 200 } }],
        actions: [{ ...GROUP_JOG, params: GROUP_JOG.params.map((p) => (p.name === "steps" ? { ...p, max: 10_000 } : p)) }],
      }],
    }).devices[0]!;
    assert.deepEqual(limitLines(grp), ["X：单次 ≤ 200 步", "Y：单次 ≤ 200 步"]);
    const stage = normalizeManualDevices({
      devices: [{ id: "local/stage/pdxc2", kind: "linear_stage", axes: [{ name: "x", unit: "um", min: 0, max: 20000, extra: { max_step: null } }], actions: [JOG] }],
    }).devices[0]!;
    assert.deepEqual(limitLines(stage), ["x：无单独上限（受软限位 0 … 20000 um）"]);
  });
});

describe("确认闸门", () => {
  const act = (o: Partial<ManualAction>): ManualAction => ({
    id: "jog", label: "", skill: "S", fixed: {}, params: [], perAxis: false, axisParam: "axis", direction: "none",
    directionParam: null, safety: "confirm", bypassLock: false, holdRepeatMs: 200, stopOnRelease: false, ...o,
  });

  it("停止永远直接跑：不看锁、不弹框", () => {
    assert.equal(actionGate(act({ id: "stop", bypassLock: true, safety: "auto" }), false), "run");
    assert.equal(actionGate(act({ bypassLock: true, safety: "confirm" }), false), "run");
  });
  it("只读直接跑；DANGEROUS 每次都确认（解锁了也一样）", () => {
    assert.equal(actionGate(act({ safety: "auto" }), false), "run");
    assert.equal(actionGate(act({ safety: "dangerous" }), true), "confirm");
  });
  it("CONFIRM：解锁期间直接跑，上着锁逐次确认", () => {
    assert.equal(actionGate(act({}), true), "run");
    assert.equal(actionGate(act({}), false), "confirm");
  });
  it("按住连续点动：要有 hold_repeat_ms、要解锁、DANGEROUS 不行", () => {
    assert.equal(canHold(act({}), true), true);
    assert.equal(canHold(act({}), false), false);
    assert.equal(canHold(act({ holdRepeatMs: null }), true), false);
    assert.equal(canHold(act({ safety: "dangerous" }), true), false);
  });
  it("只有停止和只读按钮的卡不需要解锁开关", () => {
    const snap = normalizeManualDevices(RESP);
    assert.equal(needsUnlock(snap.devices[2]!), true);
    assert.equal(needsUnlock(snap.devices[0]!), false);
  });
});

describe("一张卡画什么（cardPlan，完全按描述）", () => {
  const snap = normalizeManualDevices(RESP);
  const pd = snap.devices[2]!;
  const grp = snap.devices[3]!;
  const xyz = snap.devices[4]!;

  it("停止类放卡头；逐轴的动作每个轴一行；signed 画成 − / +", () => {
    const p = cardPlan(pd, false);
    assert.deepEqual(p.stops.map((a) => a.skill), ["StopOpticalStage"]);
    assert.equal(p.axes.length, 1);
    assert.deepEqual(p.axes[0]!.controls.map((c) => [c.action.id, c.axis, c.kind]), [["jog", "x", "signed"]]);
    assert.deepEqual(p.deviceControls, []);
    assert.deepEqual(p.badges.map((b) => b.text), ["可用"]);
  });

  it("上着锁：要先确认、不能按住；解锁后直接跑、可以按住", () => {
    const locked = cardPlan(pd, false).axes[0]!.controls[0]!;
    assert.equal(locked.gate, "confirm");
    assert.equal(locked.holdable, false);
    const open = cardPlan(pd, true).axes[0]!.controls[0]!;
    assert.equal(open.gate, "run");
    assert.equal(open.holdable, true);
  });

  it("针尖要求与占用方进徽标；不可用的卡照样画，写明原因，按钮禁用", () => {
    assert.ok(cardPlan(grp, false).badges.some((b) => b.text === "针尖：须已退针"));
    const p = cardPlan({ ...pd, available: false, unavailableReason: "串口打不开", occupiedBy: "LabVIEW.exe" }, true);
    assert.equal(p.unavailableText, "不可用：串口打不开");
    assert.ok(p.badges.some((b) => b.text === "被 LabVIEW.exe 占用"));
    assert.equal(p.axes[0]!.controls[0]!.disabled, true);
    assert.equal(p.axes[0]!.controls[0]!.holdable, false, "不可用就不许按住");
    assert.equal(p.stops.length, 1, "停止照样在");
  });

  it("一个按钮都没有：只显示状态；也不出解锁开关", () => {
    const p = cardPlan(xyz, false);
    assert.equal(p.readOnly, true);
    assert.equal(p.showUnlock, false);
    assert.equal(p.unavailableText, "不可用：协议未到，只读");
  });

  it("面板不写死设备：描述里来什么就画什么（换一台没见过的装置照样出卡）", () => {
    const odd = normalizeManualDevices({
      devices: [{ id: "satX/thing", kind: "future_kind", label: "没见过的装置", actions: [{ id: "poke", label: "戳一下", skill: "PokeThing", safety: "auto" }] }],
    }).devices[0]!;
    const p = cardPlan(odd, false);
    assert.equal(p.kindLabel, "future_kind");
    assert.deepEqual(p.deviceControls.map((c) => [c.action.label, c.kind, c.gate]), [["戳一下", "button", "run"]]);
  });
});

describe("读数对到轴上", () => {
  const ax = (name: string) => ({ name, unit: "", min: null, max: null, position: null, moving: null, homed: null, extra: {} });

  it("通道名等于轴名，或以「轴名_」开头", () => {
    const r = matchAxisValues([ax("x"), ax("y"), ax("z")], { x_mm: 1, y_mm: 2, z_mm: 3, temp_c: 21 });
    assert.deepEqual(r.byAxis, { x: 1, y: 2, z: 3 });
    assert.deepEqual(r.rest, { temp_c: 21 }, "对不上的不丢");
    assert.deepEqual(matchAxisValues([ax("1"), ax("2")], { "1": 10, "2": -3 }).byAxis, { "1": 10, "2": -3 });
  });

  it("只有一个轴、只剩一个读数：就是它", () => {
    assert.deepEqual(matchAxisValues([ax("angle")], { position_deg: 45 }).byAxis, { angle: 45 });
    assert.deepEqual(matchAxisValues([ax("angle")], { a: 1, b: 2 }).byAxis, {}, "两个读数就不猜");
  });

  it("Picomotor 的 steps 流以卫星的通道号为键：按 axes[].extra.channel 对上（轴名与通道号可以不同）", () => {
    const pico = (name: string, channel: unknown) => ({ ...ax(name), extra: { channel } });
    const r = matchAxisValues([pico("pico_x", 2), pico("pico_y", "3")], { "2": 120, "3": -40, "4": 7 });
    assert.deepEqual(r.byAxis, { pico_x: 120, pico_y: -40 });
    assert.deepEqual(r.rest, { "4": 7 });
    assert.deepEqual(matchAxisValues([pico("2", 1)], { "2": 5, "1": 9 }).byAxis, { "2": 5 }, "轴名能对上就先按轴名");
  });

});

describe("本机读数：批量只读端点（不走技能执行入口）", () => {
  it("GET /api/manual/readouts 的读法：每台各自的读数或原话；认不出的丢掉", () => {
    const r = normalizeReadouts({
      readouts: {
        "local/stage/pdxc2": { t: 1760000000.5, axes: [{ name: "x", position: 5000, moving: true, homed: true }], error: null },
        "local/stage/zeyou2d": { t: null, axes: [], error: "串口打不开" },
        bad: "x",
      },
    });
    assert.deepEqual(Object.keys(r), ["local/stage/pdxc2", "local/stage/zeyou2d"]);
    assert.deepEqual(r["local/stage/pdxc2"]!.axes[0], { name: "x", position: 5000, moving: true, homed: true, extra: {} });
    assert.equal(r["local/stage/zeyou2d"]!.error, "串口打不开");
    assert.deepEqual(normalizeReadouts(null), {});
  });

  it("一拍一次请求，但每台按自己的间隔到点才带上：同屏有 1 s 的台子，3 s 的光学组也不被读成 1 s", () => {
    const mk = (id: string, s: number) => normalizeManualDevices({ devices: [{ id, kind: "linear_stage", readout: { kind: "poll", interval_s: s } }] }).devices[0]!;
    const run = (devs: ManualDevice[], spacings: number[]) => {
      const plan = readoutPlan(devs, new Set(devs.map((d) => d.id)))!;
      const last: Record<string, number> = {};
      const ticks: Record<string, number[]> = {};
      let t = 0;
      spacings.forEach((gap, i) => {
        for (const id of dueReadouts(plan, last, t)) {
          last[id] = t;
          (ticks[id] ??= []).push(i);
        }
        t += gap;
      });
      return { plan, ticks };
    };
    // 拍子 = 间隔 + 回包时间；计时器偶尔早到 1 ms
    const gaps = [999, 999, 1000, 1075, 999, 1010, 1060, 998, 1030, 1005, 1000, 1020];
    const a = run([mk("local/stage/a", 1), mk("local/nanonis_group/1", 3)], gaps);
    assert.equal(a.plan.intervalMs, 1000);
    assert.deepEqual(a.ticks["local/stage/a"], [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11], "1 s 的台子每拍都读（早到 1 ms 也不跳拍）");
    assert.deepEqual(a.ticks["local/nanonis_group/1"], [0, 3, 6, 9], "3 s 的光学组每 3 拍一次，不被拖到第 4 拍");
    // 不是拍子整数倍的间隔：宁慢不快（2.5 s 的读成 3 s，不读成 2 s）
    const b = run([mk("local/stage/a", 1), mk("local/stage/b", 2.5)], gaps.map(() => 1000));
    assert.deepEqual(b.ticks["local/stage/b"], [0, 3, 6, 9]);
  });

  it("相机不进批量读数（它看 /api/camera/{id}/stats）；卫星设备进来但只做兜底：5 s 一次读枢纽缓存", () => {
    const devs = normalizeManualDevices({
      devices: [
        { id: "local/stage/a", machine: "local", kind: "linear_stage", readout: { kind: "poll", interval_s: 1 } },
        { id: "local/camera/zelux1", machine: "local", kind: "camera", readout: { kind: "poll", interval_s: 1 } },
        { id: "sat1/pico", machine: "sat1", kind: "openloop_stepper", readout: { kind: "live", stream: "sat1/pico/steps" } },
        { id: "sat1/xyz", machine: "sat1", kind: "xyz_stage", available: false, readout: { kind: "live", stream: "sat1/xyz/position" } },
      ],
    }).devices;
    const plan = readoutPlan(devs, new Set(devs.map((d) => d.id)));
    assert.deepEqual(plan, { ids: ["local/stage/a", "sat1/pico"], intervalMs: 1000, everyMs: { "local/stage/a": 1000, "sat1/pico": SATELLITE_FALLBACK_POLL_S * 1000 } });
    assert.equal(readoutPlan([devs[1]!], new Set(devs.map((d) => d.id))), null, "只有相机：不轮询");
  });

  it("进不进批量读数只有一个判据（卡片报「在屏幕上」与读数计划都用它）", () => {
    const mk = (id: string, machine: string, kind: string, readout: unknown) =>
      normalizeManualDevices({ devices: [{ id, machine, kind, readout }] }).devices[0]!;
    assert.equal(wantsBatchReadout(mk("local/stage/a", "local", "linear_stage", { kind: "poll", interval_s: 1 })), true);
    assert.equal(wantsBatchReadout(mk("sim1/pol", "sim1", "rotation_stage", { kind: "live", stream: "sim1/pol/angle" })), true, "卫星设备：枢纽缓存兜底");
    assert.equal(wantsBatchReadout(mk("local/camera/zelux", "local", "camera", { kind: "poll", interval_s: 1 })), false, "相机看 /stats");
    assert.equal(wantsBatchReadout(mk("local/nanonis_group/1", "local", "nanonis_motor_group", null)), false, "光学组没有读数");
    assert.equal(wantsBatchReadout(mk("local/x", "local", "linear_stage", { kind: "live", stream: "local/x" })), false, "本机的实时流不走批量端点");
  });

  it("对不上轴的实时读数（光谱仪、AOTF 的 state 流）：认得的名字换中文、键名后缀读成单位", () => {
    assert.deepEqual(restLabel("temperature_c"), { name: "温度", unit: "°C" });
    assert.deepEqual(restLabel("exposure_ms"), { name: "曝光", unit: "ms" });
    assert.deepEqual(restLabel("rf_khz"), { name: "rf", unit: "kHz" }, "长后缀先认（_khz 不读成 _hz）");
    assert.deepEqual(restLabel("grating"), { name: "光栅", unit: "" });
    assert.deepEqual(restLabel("odd"), { name: "odd", unit: "" });
    assert.deepEqual(restLabel("3"), { name: "通道 3", unit: "" }, "纯数字 = 卫星的通道号");
    assert.deepEqual(restLabel("_c"), { name: "_c", unit: "" }, "只有后缀没有名字的照原样");
  });

  it("从没读过的卡（刚滚进屏幕）立刻读，不等下一轮", () => {
    const mk = (id: string, s: number) => normalizeManualDevices({ devices: [{ id, kind: "linear_stage", readout: { kind: "poll", interval_s: s } }] }).devices[0]!;
    const plan = readoutPlan([mk("local/stage/a", 1), mk("local/stage/b", 3)], new Set(["local/stage/a", "local/stage/b"]))!;
    assert.deepEqual(dueReadouts(plan, { "local/stage/a": 0 }, 100), ["local/stage/b"]);
  });

  it("一次请求带上所有在屏幕上、可用的本机 poll 卡；间隔取最短的，不低于 0.5 s", () => {
    const mk = (id: string, readout: unknown, available = true) =>
      normalizeManualDevices({ devices: [{ id, kind: "linear_stage", available, readout }] }).devices[0]!;
    const devs = [
      mk("local/stage/a", { kind: "poll", interval_s: 2 }),
      mk("local/stage/b", { kind: "poll", interval_s: 0.75 }),
      mk("local/stage/c", { kind: "poll", interval_s: 1 }, false), // 不可用：不读
      mk("sat1/pol", { kind: "live", stream: "sat1/pol/angle" }), // 卫星：订流；批量端点只做 5 s 一次的兜底
      mk("local/nanonis_group/1", null), // 没有位置反馈
      mk("local/stage/d", { kind: "poll", interval_s: 0.1 }), // 不在屏幕上
    ];
    const plan = readoutPlan(devs, new Set(["local/stage/b", "local/stage/a", "local/stage/c", "sat1/pol", "local/nanonis_group/1"]));
    assert.deepEqual(plan, {
      ids: ["local/stage/a", "local/stage/b", "sat1/pol"],
      intervalMs: 750,
      everyMs: { "local/stage/a": 2000, "local/stage/b": 750, "sat1/pol": SATELLITE_FALLBACK_POLL_S * 1000 },
    });
    assert.equal(readoutPlan(devs, new Set()), null, "屏幕上没有本机卡就不轮询");
    const fast = readoutPlan(devs, new Set(["local/stage/d"]));
    assert.equal(fast?.intervalMs, 1000, "描述给的 0.1 s 读成 1 s（低于 0.5 s 的一律不认）");
  });

  it("读数里的 extra：没打开的台子醒目地写「未连接」；驱动锁存、错误醒目；认不出的标量照原样列出", () => {
    const off = axisExtraBits({ connected: false });
    assert.equal(off.length, 1);
    assert.equal(off[0]!.tone, "warn");
    assert.match(off[0]!.text, /未连接/);
    const pi = axisExtraBits({ connected: true, servo: true, velocity: 1.5, velocity_unit: "mm/s", has_reference_switch: true }).map((b) => b.text);
    assert.deepEqual(pi, ["伺服开", "速度 1.5 mm/s", "有参考开关"]);
    const cfg = axisExtraBits({ max_steps_per_cmd: 200, odometer_steps: 0, nanonis_axis: "X", chunk_steps: 50, freq_hz: 442, amp_v: 200, drive_policy: "verify_only" }).map((b) => b.text);
    assert.deepEqual(cfg, ["命令过 0 步（不是位置）", "Nanonis 轴 X", "配置的驱动 442 Hz / 200 V", "只核对驱动（读回与配置一致才走步，不写）"], "光学组的配置项（真后端给的形状）");
    const grp = axisExtraBits({ odometer_steps: -40, position_feedback: false, drive_latched: true });
    assert.deepEqual(grp.map((b) => [b.text.slice(0, 4), b.tone ?? ""]), [["驱动锁存", "warn"], ["命令过 ", ""]]);
    const pd = axisExtraBits({ connected: true, status_bits: 0x400, errors: ["E1"], control_mode: "closed_loop", odd_flag: 3 }).map((b) => b.text);
    assert.deepEqual(pd, ["错误：E1", "状态位 0x400", "模式 closed_loop", "odd_flag 3"]);
    // 描述里的配置项（真后端给的形状）：速度上限带上轴的单位；用途标签、单次上限不重复显示
    const desc = axisExtraBits({ role: null, max_velocity: 90, connected: false, max_step: null }, "deg").map((b) => b.text);
    assert.deepEqual(desc, [
      "未连接（MAST 还没打开它，读数不会去打开它；点「连接」或直接操作都会打开它，厂家软件占着时会失败并说是谁）",
      "速度上限 90 deg/s",
    ]);
  });

  it("PDXC2：错误位译成中文、英文原词放括号里；状态位是十六进制文本；有 errors 却没有状态位 = 读不到，不是「无错误」", () => {
    const bits = axisExtraBits({ connected: true, status_bits: "0x04000400", errors: ["abnormal stage movement", "error", "something new"] });
    assert.deepEqual(bits.map((b) => [b.text, b.tone ?? ""]), [
      ["错误：台子运动异常（abnormal stage movement）；错误（厂家未细分）（error）；something new", "warn"],
      ["状态位 0x04000400", ""],
    ]);
    const ok = axisExtraBits({ connected: true, status_bits: "0x00000400", errors: [] }).map((b) => b.text);
    assert.deepEqual(ok, ["状态位 0x00000400"], "空 errors = 没有错误位，不写错误");
    const unread = axisExtraBits({ connected: true, errors: [] });
    assert.deepEqual(unread.map((b) => [b.text, b.tone ?? ""]), [["状态位读不到", "warn"]]);
    assert.deepEqual(axisExtraBits({ connected: false, status_bits: null }).map((b) => b.text).length, 1, "没打开：只写未连接");
  });

  it("readout 的旧写法（带 skill）照样读成 poll，但不再发技能", () => {
    const d = normalizeManualDevices({ devices: [{ id: "local/stage/a", readout: { kind: "poll", skill: "GetOpticalStageStatus", interval_s: 1 } }] }).devices[0]!;
    assert.deepEqual(d.readout, { kind: "poll", intervalS: 1 });
  });
});

describe("令牌被占", () => {
  it("照实写出持有者", () => {
    assert.equal(
      busyHolderText({ instrument_busy: true, holder: { owner: "TERSGridMap", skill: "AcquireSpectrum", held_s: 41.6 } }),
      "仪器令牌在 TERSGridMap 手里（正在执行 AcquireSpectrum，已 42 s）",
    );
    assert.equal(busyHolderText({ instrument_busy: true }), "仪器令牌在 另一个入口 手里");
    assert.equal(busyHolderText({ ok: 1 }), null);
  });

  it("停一台 MAST 没打开的设备（open=false、stopped=null）不算「已停」", () => {
    assert.equal(stopHadNothingToStop({ stopped: null, open: false, enabled: true }), true);
    assert.equal(stopHadNothingToStop({ open: false }), true);
    assert.equal(stopHadNothingToStop({ stopped: "pdxc2", open: true }), false);
    assert.equal(stopHadNothingToStop({ stopped: "pdxc2" }), false, "旧回包没有 open：照旧当已停");
    assert.equal(stopHadNothingToStop(null), false);
  });

  it("松手补 stop 前现读令牌持有者：在组合技能等别人手里 ⇒ 不补；在直调入口自己手里或没人拿着 ⇒ 按原规则", () => {
    assert.equal(holderBlocksStop({ owner: "TERSGridMap", skill: "AcquireSpectrum" }), "仪器令牌在 TERSGridMap 手里（正在执行 AcquireSpectrum）");
    assert.equal(holderBlocksStop({ owner: DIRECT_EXEC_OWNER, skill: "OpticalStageMove" }), null);
    assert.equal(holderBlocksStop(null), null);
  });

  it("refused_busy 只认 instrument_busy === true（按住时据此判断设备是否已归别人）", () => {
    assert.equal(isRefusedBusy({ instrument_busy: true, holder: {} }), true);
    assert.equal(isRefusedBusy({ instrument_busy: "true" }), false);
    assert.equal(isRefusedBusy({ error: "目标超出行程：拒绝（软限位）" }), false);
    assert.equal(isRefusedBusy(undefined), false);
  });
});

// ── 按住连续点动（假时钟）──────────────────────────────────────────────────

function fakeClock() {
  let t = 0;
  let id = 0;
  const timers = new Map<number, { at: number; fn: () => void }>();
  const clock: HoldClock = {
    now: () => t,
    setTimeout: (fn, ms) => {
      id += 1;
      timers.set(id, { at: t + ms, fn });
      return id;
    },
    clearTimeout: (h) => {
      timers.delete(h as number);
    },
  };
  /** 把时间推到 t+ms，到点的定时器按时间顺序执行。 */
  const advance = async (ms: number) => {
    const end = t + ms;
    for (;;) {
      const due = [...timers.entries()].filter(([, v]) => v.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      timers.delete(due[0]);
      t = due[1].at;
      due[1].fn();
      await flush();
    }
    t = end;
  };
  return { clock, advance, now: () => t };
}

const flush = () => new Promise<void>((r) => setImmediate(r));

/** 每次 send 都给一个手动结算的 promise，记下发出的时刻。 */
function sender(clockNow: () => number) {
  const calls: number[] = [];
  const pending: Array<(r: HoldSendResult | boolean) => void> = [];
  const send = () =>
    new Promise<HoldSendResult | boolean>((resolve) => {
      calls.push(clockNow());
      pending.push(resolve);
    });
  const settle = async (r: HoldSendResult | boolean = true) => {
    pending.shift()?.(r);
    await flush();
  };
  return { send, settle, calls, pending };
}

describe("按住连续点动", () => {
  const counter = () => {
    let n = 0;
    return { stop: async () => void (n += 1), get: () => n };
  };

  it("按下立刻发；上一次返回、且距上一次开始满 interval 才发下一次", async () => {
    const c = fakeClock();
    const s = sender(c.now);
    const h = createHoldRepeat({ intervalMs: 250, send: s.send, stopOnRelease: false, clock: c.clock });
    h.press();
    assert.deepEqual(s.calls, [0]);
    await c.advance(100);
    await s.settle(); // 100 ms 时返回 → 等到 250 ms
    await c.advance(149);
    assert.equal(s.calls.length, 1);
    await c.advance(1);
    assert.deepEqual(s.calls, [0, 250]);
    // 慢回包：这一次 600 ms 后才回 → 回来立刻发下一次，不补发、不堆积
    await c.advance(600);
    assert.equal(s.calls.length, 2, "上一次没回来就不发");
    await s.settle();
    assert.deepEqual(s.calls, [0, 250, 850]);
    h.release();
  });

  it("松手时没有在路上的：执行过就立刻补 stop", async () => {
    const c = fakeClock();
    const s = sender(c.now);
    const k = counter();
    const done: string[] = [];
    const h = createHoldRepeat({
      intervalMs: 200, send: s.send, stop: k.stop, stopOnRelease: true, clock: c.clock,
      onDone: (r, i) => done.push(`${r}:${i.sent}:${i.stopped}:${i.last}`),
    });
    h.press();
    await s.settle(true); // 第一下跑成；下一下排在 200 ms
    await c.advance(100);
    h.release(); // 这时没有在路上的
    assert.equal(k.get(), 1);
    assert.deepEqual(done, ["release:1:true:ok"]);
    await c.advance(1000);
    assert.equal(s.calls.length, 1, "松开之后一次都不再发");
  });

  it("松手时还有一下在路上：等它回来再判、只判一次（回来跑成了 → 补一次 stop）", async () => {
    const c = fakeClock();
    const s = sender(c.now);
    const k = counter();
    const ends: string[] = [];
    const done: string[] = [];
    const h = createHoldRepeat({
      intervalMs: 200, send: s.send, stop: k.stop, stopOnRelease: true, clock: c.clock,
      onEnd: (r) => ends.push(r),
      onDone: (r, i) => done.push(`${r}:${i.sent}:${i.stopped}`),
    });
    h.press();
    await s.settle(true);
    await c.advance(200); // 第二下发出去，还在路上
    h.release("leave");
    assert.deepEqual(ends, ["leave"], "停发是立刻的");
    assert.equal(k.get(), 0, "在路上的还没回来：先不发 stop");
    assert.deepEqual(done, []);
    await s.settle(true);
    assert.equal(k.get(), 1, "回来了 → 补一次（不是两次）");
    assert.deepEqual(done, ["leave:2:true"]);
    await c.advance(1000);
    assert.equal(s.calls.length, 2);
  });

  it("一下都没执行（全被 refused_busy：别人正拿着这台设备）就不发 stop —— 那会截断别人的运动", async () => {
    const c = fakeClock();
    const s = sender(c.now);
    const k = counter();
    const done: string[] = [];
    const h = createHoldRepeat({
      intervalMs: 200, send: s.send, stop: k.stop, stopOnRelease: true, clock: c.clock,
      onDone: (r, i) => done.push(`${r}:${i.stopped}:${i.last}`),
    });
    h.press();
    await s.settle("refused"); // 被拒 → 本次按住结束
    assert.equal(h.active(), false);
    assert.equal(k.get(), 0);
    assert.deepEqual(done, ["refused:false:refused"]);
    // 松手时在路上的那下也被拒：同样不发
    const h2 = createHoldRepeat({ intervalMs: 200, send: s.send, stop: k.stop, stopOnRelease: true, clock: c.clock });
    h2.press();
    h2.release();
    await s.settle("refused");
    assert.equal(k.get(), 0);
  });

  it("执行了几下之后被拒（组合技能中途拿走了这台设备）：不发 stop", async () => {
    const c = fakeClock();
    const s = sender(c.now);
    const k = counter();
    const done: string[] = [];
    const h = createHoldRepeat({
      intervalMs: 100, send: s.send, stop: k.stop, stopOnRelease: true, clock: c.clock,
      onDone: (r, i) => done.push(`${r}:${i.sent}:${i.stopped}`),
    });
    h.press();
    await s.settle(true);
    await c.advance(100);
    await s.settle(true);
    await c.advance(100);
    await s.settle("refused");
    assert.deepEqual(done, ["refused:3:false"]);
    assert.equal(k.get(), 0);
    // 松手时在路上的那下回来是被拒：之前跑成过也不补（等它回来再判，正是为了这一种）
    const s2 = sender(c.now);
    const h2 = createHoldRepeat({ intervalMs: 100, send: s2.send, stop: k.stop, stopOnRelease: true, clock: c.clock });
    h2.press();
    await s2.settle(true);
    await c.advance(100);
    h2.release();
    await s2.settle("refused");
    assert.equal(k.get(), 0);
  });

  it("没成、但不是被拒（软限位、驱动报错、等到位超时）→ 当作可能动过，照发 stop", async () => {
    const c = fakeClock();
    const s = sender(c.now);
    const k = counter();
    const done: string[] = [];
    const h = createHoldRepeat({
      intervalMs: 200, send: s.send, stop: k.stop, stopOnRelease: true, clock: c.clock,
      onDone: (r, i) => done.push(`${r}:${i.stopped}`),
    });
    h.press();
    await s.settle("failed");
    assert.deepEqual(done, ["failed:true"]);
    assert.equal(k.get(), 1);
  });

  it("参数没拼成（skipped，根本没发出去）不算执行：不发 stop", async () => {
    const c = fakeClock();
    const k = counter();
    const h = createHoldRepeat({ intervalMs: 200, send: async () => "skipped" as const, stop: k.stop, stopOnRelease: true, clock: c.clock });
    h.press();
    await flush();
    assert.equal(h.active(), false);
    assert.equal(k.get(), 0);
  });

  it("stopOnRelease=false（光学组：StopMotor 会停所有组）：松手只停发，从不补 stop", async () => {
    const c = fakeClock();
    const s = sender(c.now);
    const k = counter();
    const done: string[] = [];
    const h = createHoldRepeat({
      intervalMs: 150, send: s.send, stop: k.stop, stopOnRelease: false, clock: c.clock,
      onDone: (r, i) => done.push(`${r}:${i.stopped}`),
    });
    h.press();
    await s.settle(true);
    await c.advance(150);
    h.release();
    await s.settle(true);
    assert.equal(k.get(), 0);
    assert.deepEqual(done, ["release:false"]);
  });

  it("按住超过 30 s 自己停", async () => {
    const c = fakeClock();
    const s = sender(c.now);
    const ends: string[] = [];
    const h = createHoldRepeat({ intervalMs: 1000, send: s.send, stopOnRelease: false, clock: c.clock, onEnd: (r) => ends.push(r) });
    h.press();
    for (let i = 0; i < 40; i++) {
      await s.settle();
      await c.advance(1000);
    }
    assert.deepEqual(ends, ["timeout"]);
    assert.equal(h.active(), false);
    assert.ok(s.calls.length <= 31 && s.calls.length >= 29, `30 s 内约 30 次，实际 ${s.calls.length}`);
    assert.ok(s.calls.every((t) => t < 30_000), "30 s 之后一次都不发");
  });

  it("某一次没成（超限）或被拒就停，不接着撞", async () => {
    const c = fakeClock();
    const s = sender(c.now);
    const ends: string[] = [];
    const h = createHoldRepeat({ intervalMs: 100, send: s.send, stopOnRelease: false, clock: c.clock, onEnd: (r) => ends.push(r) });
    h.press();
    await s.settle(false);
    await c.advance(1000);
    const h2 = createHoldRepeat({ intervalMs: 100, send: s.send, stopOnRelease: false, clock: c.clock, onEnd: (r) => ends.push(r) });
    h2.press();
    await s.settle("refused");
    await c.advance(1000);
    assert.deepEqual(ends, ["failed", "refused"]);
    assert.equal(s.calls.length, 2);
  });

  it("send 抛异常当作没成、可能动过（补 stop）；重复 release 只收一次尾", async () => {
    const c = fakeClock();
    const ends: string[] = [];
    const done: string[] = [];
    const k = counter();
    const h = createHoldRepeat({
      intervalMs: 100, send: () => Promise.reject(new Error("boom")), stop: k.stop,
      stopOnRelease: true, clock: c.clock, onEnd: (r) => ends.push(r), onDone: (r) => done.push(r),
    });
    h.press();
    await flush();
    h.release();
    h.release("blur");
    assert.deepEqual(ends, ["failed"]);
    assert.deepEqual(done, ["failed"]);
    assert.equal(k.get(), 1);
  });

  it("补 stop 之前现问守卫：令牌在别人手里 ⇒ 不补、写明原因；守卫放行 ⇒ 补；守卫问不到 ⇒ 按原规则补", async () => {
    const c = fakeClock();
    for (const [guard, expectStops, expectBlocked] of [
      [async () => ({ allow: false, reason: "仪器令牌在 TERSGridMap 手里" }), 0, "仪器令牌在 TERSGridMap 手里"],
      [async () => ({ allow: true }), 1, null],
      [() => Promise.reject(new Error("读不到")), 1, null],
    ] as const) {
      const s = sender(c.now);
      const k = counter();
      const done: Array<string | null> = [];
      const h = createHoldRepeat({
        intervalMs: 100, send: s.send, stop: k.stop, stopOnRelease: true, clock: c.clock, stopGuard: guard,
        onDone: (_r, i) => done.push(i.blocked),
      });
      h.press();
      await s.settle(true);
      h.release();
      await flush();
      await flush();
      assert.equal(k.get(), expectStops, `守卫 ${expectBlocked ?? "放行/问不到"}`);
      assert.deepEqual(done, [expectBlocked]);
    }
  });

  it("守卫只在按原规则该补的时候才问（全被拒、stop_on_release=false 都不问）", async () => {
    const c = fakeClock();
    let asked = 0;
    const guard = async () => {
      asked += 1;
      return { allow: true };
    };
    const s = sender(c.now);
    const h = createHoldRepeat({ intervalMs: 100, send: s.send, stop: async () => undefined, stopOnRelease: true, clock: c.clock, stopGuard: guard });
    h.press();
    await s.settle("refused");
    const h2 = createHoldRepeat({ intervalMs: 100, send: s.send, stop: async () => undefined, stopOnRelease: false, clock: c.clock, stopGuard: guard });
    h2.press();
    await s.settle(true);
    h2.release();
    await flush();
    assert.equal(asked, 0);
  });

  it("按住的结果分类：被拒（busy、执行入口不可用、请求没回来）/ 没成（可能动过）/ 跑成", () => {
    assert.equal(jogOutcome({ kind: "ok" }), "ok");
    assert.equal(jogOutcome({ kind: "failed", resp: { ok: true, data: { instrument_busy: true, holder: {} } } }), "refused");
    assert.equal(jogOutcome({ kind: "failed", resp: { ok: false, data: {} } }), "refused", "ExecutionContext 不可用：什么都没跑");
    assert.equal(jogOutcome({ kind: "failed" }), "refused", "HTTP 层被拒或网络断：跑没跑不知道，按没跑算");
    assert.equal(jogOutcome({ kind: "failed", resp: { ok: true, data: {} } }), "failed", "技能没成：门口拒绝与执行失败分不开，按可能动过算");
    assert.equal(jogOutcome({ kind: "failed", resp: { ok: true, data: {}, refused_by: "sample_gate" } }), "refused", "响应里带了 refused_by：没执行");
    assert.equal(jogOutcome({ kind: "failed", resp: { ok: true, data: {}, refused_by: null } }), "failed");
    assert.equal(jogOutcome({ kind: "failed", resp: { ok: true, data: {}, refused_by: "" } }), "failed", "空串 = 真执行了");
    assert.equal(jogOutcome({ kind: "failed", resp: { ok: true, data: {}, refused_by: "needs_human_node" } }), "failed", "停在要人决定的节点：前面的步可能执行了");
  });
});

// ── 解锁超时 ───────────────────────────────────────────────────────────────

describe("解锁手动控制", () => {
  it("10 分钟没有操作自动上锁；操作续期；上着锁的 touch 不会解锁", () => {
    let t = 0;
    const u = new UnlockStore(() => t);
    assert.equal(u.isUnlocked("a"), false);
    u.touch("a");
    assert.equal(u.isUnlocked("a"), false, "点一下不等于解锁");
    u.unlock("a");
    t = UNLOCK_IDLE_MS - 1;
    assert.equal(u.isUnlocked("a"), true);
    u.touch("a");
    t += UNLOCK_IDLE_MS - 1;
    assert.equal(u.isUnlocked("a"), true, "操作续上了 10 分钟");
    assert.equal(u.remainingMs("a"), 1);
    t += 1;
    assert.equal(u.isUnlocked("a"), false);
    assert.equal(u.remainingMs("a"), null);
    u.touch("a");
    assert.equal(u.isUnlocked("a"), false, "过期后的 touch 不复活");
  });

  it("每张卡各管各的；手动上锁立即生效并通知", () => {
    const u = new UnlockStore(() => 0);
    let n = 0;
    u.subscribe(() => (n += 1));
    u.unlock("a");
    assert.equal(u.isUnlocked("b"), false);
    u.lock("a");
    assert.equal(u.isUnlocked("a"), false);
    assert.equal(n, 2);
  });
});

// ── 停止全部 ───────────────────────────────────────────────────────────────

describe("停止全部光学运动", () => {
  it("三个都调、按顺序；前一个失败或抛异常不跳过后面的", async () => {
    const called: string[] = [];
    const res = await stopAllOptics(async (skill) => {
      called.push(skill);
      if (skill === "StopSatelliteDevices") throw new Error("枢纽不可达");
      if (skill === "StopOpticalStage") return { ok: false, message: "PDXC2 没响应" };
      return { ok: true, message: "已停" };
    });
    assert.deepEqual(called, ["StopSatelliteDevices", "StopOpticalStage", "StopMotor"]);
    assert.deepEqual(res.map((r) => r.ok), [false, false, true]);
    assert.match(res[0]!.message, /没有送达：枢纽不可达/);
  });

  it("步骤清单就是契约写的那三个免令牌停止", () => {
    assert.deepEqual(STOP_ALL_STEPS.map((s) => s.skill), ["StopSatelliteDevices", "StopOpticalStage", "StopMotor"]);
  });
});
