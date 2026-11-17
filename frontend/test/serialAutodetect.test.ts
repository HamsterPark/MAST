/**
 * 设置页的串口自动探测（src/lib/serialAutodetect.ts；契约 generic_station.md §1.4 / §4.2）。
 *
 *     cd frontend && npm run test:unit
 *
 * 三种站的出发点不一样（health 的 station_serial_autodetect）：
 *   default_on   STM 站：按配置。设置页上什么都不加 —— STM 的设置页一个像素都不变；
 *   forced_off   光学站：永远关。只显示状态和原因，没有开关；
 *   default_off  通用站：默认关，操作员可以打开（serial_autodetect，要管理 PIN）。显示状态、原因和开关。
 * 后端不带这个字段（老后端）也什么都不画。
 * 「生效的」来自 /api/environment/sensors 的 autodetect_effective，「存下的」来自设置 serial_autodetect；
 * 两者在下次启动或重新探测之前可以不一样，界面要说出来。
 *
 * node 下 `tr()` 返回中文。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  autodetectConfirmCopy,
  autodetectView,
  readAutodetectReason,
  readEffectiveAutodetect,
  readStoredAutodetect,
} from "../src/lib/serialAutodetect.ts";

describe("读字段：类型守卫", () => {
  it("serial_autodetect：只认布尔，没设 / 没带 / 脏值都是 null", () => {
    assert.equal(readStoredAutodetect({ serial_autodetect: true }), true);
    assert.equal(readStoredAutodetect({ serial_autodetect: false }), false);
    for (const v of [undefined, null, {}, { serial_autodetect: null }, { serial_autodetect: "true" }, { serial_autodetect: 1 }]) {
      assert.equal(readStoredAutodetect(v), null, JSON.stringify(v));
    }
  });

  it("autodetect_effective：只认布尔；degraded 的回答不算数", () => {
    assert.equal(readEffectiveAutodetect({ autodetect_effective: true }), true);
    assert.equal(readEffectiveAutodetect({ autodetect_effective: false, degraded: false }), false);
    assert.equal(readEffectiveAutodetect({ autodetect_effective: true, degraded: true }), null);
    for (const v of [undefined, null, {}, { autodetect: true }, { autodetect_effective: "yes" }]) {
      assert.equal(readEffectiveAutodetect(v), null, JSON.stringify(v));
    }
  });

  it("autodetect_reason：去首尾空白，空串当没有", () => {
    assert.equal(readAutodetectReason({ autodetect_reason: "  forced off by the optical station profile " }), "forced off by the optical station profile");
    for (const v of [undefined, {}, { autodetect_reason: "" }, { autodetect_reason: "   " }, { autodetect_reason: 3 }]) {
      assert.equal(readAutodetectReason(v), null, JSON.stringify(v));
    }
  });
});

describe("autodetectView：画什么", () => {
  it("STM 站（default_on）和老后端（null）：什么都不画", () => {
    for (const mode of ["default_on", null] as const) {
      for (const effective of [true, false, null]) {
        for (const stored of [true, false, null]) {
          const v = autodetectView({ mode, effective, stored });
          assert.equal(v.shown, false, JSON.stringify({ mode, effective, stored }));
          assert.equal(v.switchable, false);
        }
      }
    }
  });

  it("光学站（forced_off）：显示「强制关闭」，没有开关，存的值不影响", () => {
    for (const stored of [true, false, null]) {
      const v = autodetectView({ mode: "forced_off", effective: false, stored });
      assert.deepEqual([v.shown, v.switchable, v.pendingNote], [true, false, null]);
      assert.ok(v.status.includes("强制关闭") && v.status.includes("光学站"), v.status);
    }
  });

  it("通用站（default_off），没打开：显示「默认关」，有开关、开关在关", () => {
    const v = autodetectView({ mode: "default_off", effective: false, stored: null });
    assert.deepEqual([v.shown, v.switchable, v.checked, v.pendingNote], [true, true, false, null]);
    assert.ok(v.status.includes("关闭") && v.status.includes("通用站默认关"), v.status);
  });

  it("通用站，打开了且已生效：显示「已打开」，开关在开", () => {
    const v = autodetectView({ mode: "default_off", effective: true, stored: true });
    assert.deepEqual([v.checked, v.pendingNote], [true, null]);
    assert.ok(v.status.includes("已打开"), v.status);
  });

  it("通用站，存的和运行的不一样（下次启动 / 重新探测才生效）：说出来；开关跟着存的值走", () => {
    const on = autodetectView({ mode: "default_off", effective: false, stored: true });
    assert.equal(on.checked, true, "开关是操作员的选择");
    assert.ok(on.status.includes("关闭"), "状态是现在运行的");
    assert.ok(on.pendingNote && on.pendingNote.includes("下次启动"), String(on.pendingNote));
    const off = autodetectView({ mode: "default_off", effective: true, stored: false });
    assert.equal(off.checked, false);
    assert.ok(off.status.includes("已打开"));
    assert.ok(off.pendingNote);
  });

  it("通用站，读不到生效状态：按存的值推，存了打开就提示待生效", () => {
    const v = autodetectView({ mode: "default_off", effective: null, stored: true });
    assert.ok(v.status.includes("关闭"), "不知道就当默认关");
    assert.ok(v.pendingNote);
    assert.equal(autodetectView({ mode: "default_off", effective: null, stored: null }).pendingNote, null);
  });
});

describe("确认框文案", () => {
  it("打开：说清会向空闲串口发 *IDN? 和 Modbus 帧、厂商软件占着的口打不开、什么时候生效", () => {
    const c = autodetectConfirmCopy(true);
    for (const needle of ["*IDN?", "Modbus", "厂商软件", "下次启动"]) assert.ok(c.body.includes(needle), needle);
    assert.ok(c.title.includes("打开") && c.confirmLabel.includes("打开"));
  });

  it("关闭：已经接入的传感器照常读数", () => {
    const c = autodetectConfirmCopy(false);
    assert.ok(c.body.includes("照常读数") && c.title.includes("关闭") && c.confirmLabel.includes("关闭"));
  });
});
