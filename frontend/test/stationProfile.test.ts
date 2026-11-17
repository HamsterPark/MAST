/**
 * 站点画像（stm / optical / generic）的纯函数 + 它在页面里的接线（src/lib/stationProfile.ts）。
 * 门按能力判断（src/lib/stationCapabilities.ts），三种画像 × 有 / 无能力字段在
 * test/stationCapabilities.test.ts；这里只管画像本身和首页的接线。
 *
 *     cd frontend && npm run test:unit
 *
 * ── 为什么 normalizeProfile 的「未知值 → stm」要钉死 ──
 * 后端 `mast/station.py` 对不认识的值的态度是：不报错、不猜，当 stm 并警告一次——
 * 一个拼错的值不能把一台正在工作的 STM 站悄悄变成光学站。前端读到什么都是同一条规矩：
 * 读不出来（health 没回、settings 里没有这个键、值是脏的）就是今天所有机器都在用的 stm。
 * 这里不把默认值写成「optical」的任何一种可能性放过去。
 *
 * ── 设备汇总里最容易写错的两处 ──
 *   * 「已连接」只在启用的设备里数：未启用的设备不可能被 MAST 打开。
 *   * 设备是第一次用才打开，所以「启用但未连接」是闲置，不是故障——汇总不能把它记成坏的。
 *
 * node 下没有存下的语言选择，`tr()` 返回中文（见 src/lib/i18n.ts），下面的标签断言据此。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import {
  KIND_ORDER,
  countDevices,
  describeDeviceCounts,
  deviceStatus,
  deviceStatusLabel,
  groupByKind,
  kindLabel,
  normalizeKind,
  normalizeProfile,
  normalizeSource,
  profileBlurb,
  profileLabel,
  restartNeeded,
} from "../src/lib/stationProfile.ts";

describe("normalizeProfile", () => {
  it("认 stm / optical / generic 三个值", () => {
    assert.equal(normalizeProfile("stm"), "stm");
    assert.equal(normalizeProfile("optical"), "optical");
    assert.equal(normalizeProfile("generic"), "generic");
  });

  it("忽略大小写与首尾空白（后端存的是规范形式，但手改过的文件不一定）", () => {
    for (const v of [" optical", "optical ", " OPTICAL ", "Optical", "optical\n", "\tOpticaL\t", "\r\noptical\r\n"]) {
      assert.equal(normalizeProfile(v), "optical", JSON.stringify(v));
    }
    for (const v of [" stm", "STM", "Stm ", "\tstm\n"]) {
      assert.equal(normalizeProfile(v), "stm", JSON.stringify(v));
    }
  });

  it("未知值一律 stm —— 拼错不会把 STM 站变成光学站", () => {
    for (const v of [
      "",
      " ",
      "opitcal", // 拼错
      "optical2",
      "optic",
      "opt ical", // 中间的空白不算
      "stm optical",
      "optical,stm",
      "光学站",
      "nanonis",
      "none",
      "null",
      "undefined",
      "true",
      "generic2",
      "genric", // 拼错
      "general",
      "通用站",
      "generic optical",
    ]) {
      assert.equal(normalizeProfile(v), "stm", JSON.stringify(v));
    }
  });

  it("非字符串一律 stm（读不到不是 optical）", () => {
    for (const v of [null, undefined, 0, 1, NaN, true, false, {}, [], ["optical"], { profile: "optical" }, Symbol("optical")]) {
      assert.equal(normalizeProfile(v), "stm", String(typeof v));
    }
  });

  it("返回值只有三种，而且是幂等的", () => {
    const samples: unknown[] = ["optical", "stm", "generic", " Optical ", " GENERIC", "x", "", null, undefined, 3, {}, "STM", "oPtIcAl"];
    for (const v of samples) {
      const p = normalizeProfile(v);
      assert.ok(p === "stm" || p === "optical" || p === "generic", String(v));
      assert.equal(normalizeProfile(p), p);
    }
  });
});

describe("restartNeeded", () => {
  it("已存储值与生效值不同 = 需要重启，相同 = 不需要", () => {
    assert.equal(restartNeeded("optical", "stm"), true);
    assert.equal(restartNeeded("stm", "optical"), true);
    assert.equal(restartNeeded("stm", "stm"), false);
    assert.equal(restartNeeded("optical", "optical"), false);
  });

  it("从没设置过（null / undefined）按 stm 算", () => {
    assert.equal(restartNeeded(null, "stm"), false);
    assert.equal(restartNeeded(undefined, "stm"), false);
    assert.equal(restartNeeded(null, "optical"), true);
    assert.equal(restartNeeded(undefined, "optical"), true);
  });

  it("大小写与空白不构成差异", () => {
    assert.equal(restartNeeded(" Optical ", "optical"), false);
    assert.equal(restartNeeded("STM", " stm"), false);
  });

  it("脏值两边都当 stm，不会凭空要求重启", () => {
    assert.equal(restartNeeded("foo", "stm"), false);
    assert.equal(restartNeeded("foo", "optical"), true);
    assert.equal(restartNeeded("optical", null), true);
    assert.equal(restartNeeded("stm", "garbage"), false);
  });
});

describe("profileLabel / profileBlurb", () => {
  it("三种画像各有自己的名字", () => {
    assert.equal(profileLabel("stm"), "STM 站");
    assert.equal(profileLabel("optical"), "光学站");
    assert.equal(profileLabel("generic"), "通用站");
  });

  it("说明各自提到各自的仪器", () => {
    assert.match(profileBlurb("stm"), /Nanonis/);
    assert.match(profileBlurb("optical"), /WITec/);
    assert.match(profileBlurb("optical"), /没有 STM/);
    assert.match(profileBlurb("generic"), /没有 STM/);
    assert.match(profileBlurb("generic"), /也没有 WITec/);
    assert.equal(new Set(["stm", "optical", "generic"].map((p) => profileBlurb(p as "stm"))).size, 3);
  });
});

describe("countDevices", () => {
  it("没有设备 / 读不到 → 全 0", () => {
    const zero = { total: 0, enabled: 0, disabled: 0, connected: 0 };
    assert.deepEqual(countDevices([]), zero);
    assert.deepEqual(countDevices(undefined), zero);
    assert.deepEqual(countDevices(null), zero);
  });

  it("总数 / 启用 / 未启用 / 已连接", () => {
    const c = countDevices([
      { id: "a", enabled: true, connected: true },
      { id: "b", enabled: true, connected: false },
      { id: "c", enabled: true, connected: true },
      { id: "d", enabled: false, connected: false },
    ]);
    assert.deepEqual(c, { total: 4, enabled: 3, disabled: 1, connected: 2 });
  });

  it("已连接只在启用的设备里数（未启用的不可能被打开）", () => {
    const c = countDevices([
      { id: "a", enabled: false, connected: true }, // 自相矛盾的一行，不能被记成已连接
      { id: "b", enabled: true, connected: true },
    ]);
    assert.equal(c.connected, 1);
    assert.equal(c.enabled, 1);
    assert.equal(c.disabled, 1);
  });

  it("enabled 缺省按后端默认（True）算；connected 只认明确的 true", () => {
    const c = countDevices([{ id: "a" }, { id: "b", enabled: null, connected: null }, { id: "c", connected: undefined }]);
    assert.deepEqual(c, { total: 3, enabled: 3, disabled: 0, connected: 0 });
  });

  it("启用但没连上是闲置，不是坏：enabled 里照样计入", () => {
    const c = countDevices([{ id: "a", enabled: true, connected: false }]);
    assert.equal(c.enabled, 1);
    assert.equal(c.connected, 0);
  });
});

describe("describeDeviceCounts", () => {
  it("已配置 N 台 / 已连接 M 台（N 是启用的数目）", () => {
    assert.equal(
      describeDeviceCounts({ total: 5, enabled: 3, disabled: 2, connected: 1 }),
      "已配置 3 台 / 已连接 1 台",
    );
    assert.equal(
      describeDeviceCounts(countDevices([])),
      "已配置 0 台 / 已连接 0 台",
    );
  });
});

describe("normalizeKind / kindLabel", () => {
  it("缺失或空白是 motion（后端自己的默认）", () => {
    for (const v of [undefined, null, "", "   ", 3, {}]) {
      assert.equal(normalizeKind(v), "motion", String(v));
    }
  });

  it("大小写与空白被抹平", () => {
    assert.equal(normalizeKind(" Laser "), "laser");
    assert.equal(normalizeKind("POWER_METER"), "power_meter");
  });

  it("后端的 8 个 kind 都认得，顺序固定", () => {
    // mast/instruments/config.py:device_kind 的取值
    assert.deepEqual(
      [...KIND_ORDER],
      ["motion", "rotation", "openloop", "spectrometer", "microscope", "laser", "power_meter", "temperature"],
    );
  });

  it("每个 kind 有自己的标签：互不相同，也不是裸 id", () => {
    const labels = KIND_ORDER.map((k) => kindLabel(k));
    assert.equal(new Set(labels).size, KIND_ORDER.length);
    KIND_ORDER.forEach((k, i) => assert.notEqual(labels[i], k));
  });

  it("不认识的 kind 原样显示，不被吞成「其他」", () => {
    assert.equal(kindLabel("galvo"), "galvo");
  });
});

type Dev = { id: string; kind?: string | null; enabled?: boolean; connected?: boolean };

describe("groupByKind", () => {
  const devices: Dev[] = [
    { id: "tc", kind: "temperature", enabled: true, connected: true },
    { id: "laser1", kind: "laser", enabled: true, connected: false },
    { id: "stage1", kind: "motion", enabled: true, connected: true },
    { id: "witec", kind: "microscope", enabled: true, connected: true },
    { id: "pm", kind: "power_meter", enabled: true, connected: false },
    { id: "stage2", kind: "motion", enabled: false, connected: false },
    { id: "spec", kind: "spectrometer", enabled: true, connected: true },
  ];

  it("已知的 kind 按固定顺序，组内保持配置里的顺序", () => {
    const g = groupByKind(devices);
    assert.deepEqual(
      g.map((x) => x.kind),
      ["motion", "spectrometer", "microscope", "laser", "power_meter", "temperature"],
    );
    assert.deepEqual(
      g[0]!.devices.map((d) => d.id),
      ["stage1", "stage2"],
    );
  });

  it("每组带自己的汇总", () => {
    const motion = groupByKind(devices).find((x) => x.kind === "motion")!;
    assert.deepEqual(motion.counts, { total: 2, enabled: 1, disabled: 1, connected: 1 });
    assert.equal(motion.label, kindLabel("motion"));
  });

  it("缺省 kind 归入 motion；不认识的 kind 排在已知之后并按字母序", () => {
    const g = groupByKind<Dev>([
      { id: "z1", kind: "zeta" },
      { id: "a1" },
      { id: "b1", kind: "alpha" },
      { id: "l1", kind: "laser" },
      { id: "n1", kind: null },
    ]);
    assert.deepEqual(
      g.map((x) => x.kind),
      ["motion", "laser", "alpha", "zeta"],
    );
    assert.deepEqual(
      g[0]!.devices.map((d) => d.id),
      ["a1", "n1"],
    );
  });

  it("一台不丢，也不改输入", () => {
    const before = devices.map((d) => d.id);
    const g = groupByKind(devices);
    assert.equal(
      g.reduce((n, x) => n + x.devices.length, 0),
      devices.length,
    );
    assert.deepEqual(
      devices.map((d) => d.id),
      before,
    );
  });

  it("没有设备 → 没有组", () => {
    assert.deepEqual(groupByKind([]), []);
    assert.deepEqual(groupByKind(undefined), []);
    assert.deepEqual(groupByKind(null), []);
  });
});

describe("deviceStatus / deviceStatusLabel", () => {
  it("未启用优先于已连接；启用且打开是已连接；其余是待连接", () => {
    assert.equal(deviceStatus({ enabled: false, connected: true }), "disabled");
    assert.equal(deviceStatus({ enabled: false }), "disabled");
    assert.equal(deviceStatus({ enabled: true, connected: true }), "connected");
    assert.equal(deviceStatus({ enabled: true, connected: false }), "idle");
    assert.equal(deviceStatus({}), "idle"); // enabled 缺省 = 启用
  });

  it("三种状态三个不同的字", () => {
    const labels = (["disabled", "connected", "idle"] as const).map(deviceStatusLabel);
    assert.equal(new Set(labels).size, 3);
    assert.equal(deviceStatusLabel("idle"), "待连接");
  });
});

// ── 接线 ──────────────────────────────────────────────────────────────────────
// 这几条钉的是页面怎么用上面那些函数。React 渲染在 node 下跑不了，所以按源码文本断言
// （和 settingsWrite.test.ts / degradedWiring.test.ts 同一个办法），只钉三件最容易被悄悄
// 改坏的事：hook 顺序、STM 读取的 enabled 门、health 缓存键。

const SRC = join(fileURLToPath(new URL(".", import.meta.url)), "..", "src");

/** 去掉注释的源码（注释里常常把错误写法原样写出来讲解，不能让它触发断言）。 */
function code(rel: string): string {
  return readFileSync(join(SRC, rel), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1");
}

describe("接线", () => {
  it("SetupBanner：hook 先调，没有 STM 安全参数的站再早退（hook 个数不能随站变）；清单本身也不去要", () => {
    const src = code("components/shell/SetupBanner.tsx");
    const hook = src.indexOf("useStationCapabilities()");
    const init = src.indexOf("useInstrumentInit({ enabled: stmSafety })");
    const bail = src.search(/if \(!stmSafety\) return null/);
    const firstReturn = src.indexOf("return null");
    assert.ok(hook >= 0, "SetupBanner 没有调用 useStationCapabilities()");
    assert.match(src, /const stmSafety = station\.ready && station\.has\("stm_safety"\);/);
    assert.ok(init > hook, "初始化清单的请求要等能力：useInstrumentInit({ enabled: stmSafety })");
    assert.ok(bail > init, "早退必须在所有 hook 调用之后");
    assert.ok(hook < firstReturn && init < firstReturn, "hook 必须在第一个 return null 之前");
  });

  it("Dashboard：两个 STM 读取带 enabled，没有 Nanonis 的站不发；两节只在有 Nanonis 时渲染", () => {
    const src = code("pages/DashboardPage.tsx");
    for (const name of ["useConnection", "useLiveReadings"]) {
      const m = new RegExp(`function ${name}\\(enabled: boolean\\) \\{[\\s\\S]*?\\n\\}`).exec(src);
      assert.ok(m, `${name} 丢了 enabled 参数`);
      assert.match(m[0], /^\s*enabled,\s*$/m, `${name} 没把 enabled 交给 useQuery`);
      assert.match(src, new RegExp(`${name}\\(stmReads\\)`), `${name} 的调用没有走 stmReads`);
    }
    assert.match(src, /const kind = overviewKind\(station\.has\);/);
    assert.match(src, /const nanonis = kind === "stm";/);
    assert.match(src, /const stmReads = station\.ready && nanonis;/, "stmReads 要等能力，并且只在有 Nanonis 时为真");
    for (const title of ["硬件实时读数", "TCP 端口"]) {
      assert.match(
        src,
        new RegExp(`\\{nanonis && \\(\\s*<Section title=\\{tr\\("${title}"`),
        `「${title}」那一节没有被 nanonis 包住`,
      );
    }
  });

  it("Dashboard：设备卡和设备表挂 optical_bench（不是「没有 nanonis」），叫法看 witec", () => {
    const src = code("pages/DashboardPage.tsx");
    assert.match(src, /const bench = station\.has\("optical_bench"\);/);
    assert.match(src, /const benchReads = station\.ready && bench;/);
    assert.match(src, /useOpticsDevices\(benchReads\)/);
    assert.match(src, /\{bench && \(\s*<StatCard label=\{devicesTitle\}/);
    assert.match(src, /\{bench && \(\s*<Section\s+title=\{devicesTitle\}/);
    assert.match(src, /const devicesTitle = station\.has\("witec"\) \? tr\("光学设备"/);
    assert.doesNotMatch(src, /\{!nanonis && \(/, "不能再拿「没有 nanonis」去推别的东西");
  });

  it("Dashboard：环境传感器挂 sensor_echo；图库概况只在通用版里、只读轻量的两样（数据根、建索引状态）", () => {
    const src = code("pages/DashboardPage.tsx");
    assert.match(src, /const sensors = station\.has\("sensor_echo"\);/);
    assert.match(src, /const sensorReads = station\.ready && sensors;/);
    assert.match(src, /useEnvironmentReadings\(sensorReads\)/);
    assert.match(src, /\{sensors && \(\s*<Section\s+title=\{tr\("环境传感器"/);
    assert.match(src, /const galleryReads = station\.ready && kind === "generic";/);
    assert.match(src, /useGalleryOverview\(galleryReads\)/);
    assert.match(src, /\{kind === "generic" && \(\s*<Section\s+title=\{tr\("数据图库概况"/);
    const env = /function useEnvironmentReadings\(enabled: boolean\) \{[\s\S]*?\n\}/.exec(src);
    assert.ok(env && /^\s*enabled,\s*$/m.test(env[0]) && /refetchInterval: 5000,/.test(env[0]), "和右栏同一个间隔，不更短");
  });

  it("Dashboard：图库概况绝不去拉 /api/gallery/index，也不轮询（数据根大时 index 很大）", () => {
    const src = code("pages/DashboardPage.tsx");
    const gallery = /function useGalleryOverview\(enabled: boolean\) \{[\s\S]*?\n\}/.exec(src);
    assert.ok(gallery, "找不到 useGalleryOverview");
    const g = gallery[0];
    // config 与 status 带调用方的 enabled；index 一律 enabled: false（只读缓存，永不发请求）
    assert.match(g, /queryKey: GALLERY_KEYS\.config,\s*enabled,/);
    assert.match(g, /queryKey: GALLERY_KEYS\.status,\s*enabled,/);
    assert.match(g, /queryKey: GALLERY_KEYS\.index,\s*enabled: false,/);
    assert.doesNotMatch(g, /refetchInterval/, "首页不新增图库的轮询");
    assert.equal([...src.matchAll(/GALLERY_KEYS\.index/g)].length, 1, "首页只在那一个永不发请求的观察者里碰 index");
  });

  it("health 的缓存键与 Dashboard / TopBar / RightPanel 共用 [\"health\"]", () => {
    const hook = code("hooks/useStationProfile.ts");
    assert.match(hook, /const HEALTH_KEY = \["health"\] as const/);
    assert.match(hook, /queryKey: HEALTH_KEY/);
    assert.match(code("pages/DashboardPage.tsx"), /queryKey: \["health"\]/);
  });
});

describe("normalizeSource", () => {
  it("认 env / settings / default，其余（含非字符串）一律 default", () => {
    assert.equal(normalizeSource("env"), "env");
    assert.equal(normalizeSource("settings"), "settings");
    assert.equal(normalizeSource("default"), "default");
    for (const v of [undefined, null, "", "ENV", "environment", 1, {}]) {
      assert.equal(normalizeSource(v), "default", JSON.stringify(v));
    }
  });

  it("设置页在 source=env 时说明环境变量钉住了画像（选择不会改变运行的画像）", () => {
    const page = code("pages/SettingsPage.tsx");
    assert.match(page, /source === "env"/);
    assert.match(page, /MAST_STATION_PROFILE/);
  });
});
