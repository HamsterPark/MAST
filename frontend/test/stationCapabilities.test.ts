/**
 * 站点能力（src/lib/stationCapabilities.ts）：界面上每一道「这台站有没有 X」的门，以及它在三种画像
 * （stm / optical / generic）× 后端带不带 `station_capabilities` 下的取值。契约：
 * docs/v2/design/generic_station.md §1.2（能力表）、§1.4（串口自动检测）、§3.7（急停）、§4.1（health）。
 *
 *     cd frontend && npm run test:unit
 *
 * ── 为什么门按能力、不按画像名 ──
 * 以前八个文件各自问「是不是光学站」，加第三种站就得把八处都改一遍，漏一处就是一块 STM 读数在通用站上
 * 轮询 Nanonis。现在后端报能力清单，界面每道门只问一种能力。
 *
 * ── 这里钉死的几件事 ──
 *   · 老后端（不带能力字段）：按画像推默认能力，表和后端契约是同一张（DEFAULT_CAPABILITIES 原样钉住），
 *     每种画像的能力集合对依赖闭合；
 *   · 读不出来（health 没回、字段是脏的）一律按 stm：今天每台机器都是 STM 站，不能因为一个 bug
 *     把 STM 站的读数藏掉；
 *   · 字段是空列表是真答案（什么能力都没有），不是「读不出来」；
 *   · 后端给了清单就以清单为准，不再看画像名；
 *   · 急停文案：STM 站和今天逐字一致（快照）；别的站只写后端在那里真正做的事；
 *   · 首次使用横幅只给全新的机器（没选过画像 + 从没做过初始化），读不出来就不出。
 *
 * node 下没有存下的语言选择，`tr()` 返回中文（见 src/lib/i18n.ts），文案断言据此。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import {
  CAPABILITIES,
  CAPABILITY_REQUIRES,
  DEFAULT_CAPABILITIES,
  capabilitiesFrom,
  capabilityLabel,
  estopTitle,
  overviewKind,
  readCapabilityList,
  readSerialAutodetect,
  type Capability,
} from "../src/lib/stationCapabilities.ts";
import {
  PROFILES,
  firstRunBannerShown,
  initHistory,
  normalizeProfile,
  profileNeverChosen,
  type StationProfile,
} from "../src/lib/stationProfile.ts";
import { filterTabs, pathVisible, visibleTabs, type NavTab } from "../src/lib/nav.ts";

// generic_station.md §1.2, in table order
const ALL_IN_ORDER: Capability[] = [
  "nanonis", "stm_safety", "stm_actions", "scan_map", "witec",
  "optical_bench", "operator_prompts", "laser_safety", "sensor_echo",
];
const STM_SET: Capability[] = ["nanonis", "stm_safety", "stm_actions", "scan_map"];
const OPTICAL_SET: Capability[] = ["witec", "optical_bench", "operator_prompts", "laser_safety"];
const GENERIC_SET: Capability[] = ["optical_bench", "operator_prompts", "laser_safety", "sensor_echo"];

/** has() over every known capability, as a sorted list (easier to read in a failure). */
function present(caps: { has(c: Capability): boolean }): Capability[] {
  return CAPABILITIES.filter((c) => caps.has(c)).sort();
}

describe("DEFAULT_CAPABILITIES：画像 → 默认能力（与后端契约同一张表）", () => {
  it("能力名和顺序就是契约 §1.2 那张表", () => {
    assert.deepEqual([...CAPABILITIES], ALL_IN_ORDER);
  });

  it("三种画像各一行，原样钉住（按表的顺序）", () => {
    assert.deepEqual(Object.keys(DEFAULT_CAPABILITIES).sort(), [...PROFILES].sort());
    assert.deepEqual([...DEFAULT_CAPABILITIES.stm], STM_SET);
    assert.deepEqual([...DEFAULT_CAPABILITIES.optical], OPTICAL_SET);
    assert.deepEqual([...DEFAULT_CAPABILITIES.generic], GENERIC_SET);
    for (const p of PROFILES) {
      const order = DEFAULT_CAPABILITIES[p].map((c) => ALL_IN_ORDER.indexOf(c));
      assert.deepEqual(order, [...order].sort((a, b) => a - b), `${p} 不是按表的顺序`);
    }
  });

  it("表里只有本构建认识的能力名", () => {
    for (const p of PROFILES) {
      for (const c of DEFAULT_CAPABILITIES[p]) assert.ok((CAPABILITIES as readonly string[]).includes(c), `${p}: ${c}`);
    }
  });

  it("每种画像的能力集合对依赖闭合（stm_safety / stm_actions / scan_map 要 nanonis，laser_safety 要 operator_prompts）", () => {
    assert.deepEqual(
      Object.fromEntries(Object.entries(CAPABILITY_REQUIRES).map(([k, v]) => [k, [...(v ?? [])]])),
      { stm_safety: ["nanonis"], stm_actions: ["nanonis"], scan_map: ["nanonis"], laser_safety: ["operator_prompts"] },
    );
    for (const p of PROFILES) {
      const have = new Set<Capability>(DEFAULT_CAPABILITIES[p]);
      for (const c of have) {
        for (const need of CAPABILITY_REQUIRES[c] ?? []) assert.ok(have.has(need), `${p}: ${c} 要 ${need}`);
      }
    }
  });

  it("表是冻结的：一处调用改不了它", () => {
    assert.ok(Object.isFrozen(DEFAULT_CAPABILITIES));
    for (const p of PROFILES) assert.ok(Object.isFrozen(DEFAULT_CAPABILITIES[p]), p);
  });

  it("STM 站和另外两种站没有交集；光学站和通用站只差 witec / sensor_echo", () => {
    const stm = new Set<string>(DEFAULT_CAPABILITIES.stm);
    assert.deepEqual(DEFAULT_CAPABILITIES.optical.filter((c) => stm.has(c)), []);
    assert.deepEqual(DEFAULT_CAPABILITIES.generic.filter((c) => stm.has(c)), []);
    const optical = new Set<string>(DEFAULT_CAPABILITIES.optical);
    const generic = new Set<string>(DEFAULT_CAPABILITIES.generic);
    assert.deepEqual([...optical].filter((c) => !generic.has(c)), ["witec"]);
    assert.deepEqual([...generic].filter((c) => !optical.has(c)), ["sensor_echo"]);
  });
});

describe("readCapabilityList：本地类型守卫", () => {
  it("字符串列表原样收下，去首尾空白、转小写、去重、丢空名", () => {
    assert.deepEqual(readCapabilityList(["nanonis", "scan_map"]), ["nanonis", "scan_map"]);
    assert.deepEqual(readCapabilityList([" Nanonis ", "NANONIS", "", "  ", "witec"]), ["nanonis", "witec"]);
  });

  it("空列表是真答案：这台站一种能力都没有", () => {
    assert.deepEqual(readCapabilityList([]), []);
  });

  it("不是列表 → null（调用方按画像推）", () => {
    for (const v of [undefined, null, "nanonis", "nanonis,witec", 0, 1, true, {}, { nanonis: true }]) {
      assert.equal(readCapabilityList(v), null, JSON.stringify(v));
    }
  });

  it("列表里混进非字符串 → 整个不认（不把脏数据当成「什么都没有」）", () => {
    for (const v of [["nanonis", 1], [null], ["witec", { name: "nanonis" }], [["nanonis"]], [undefined]]) {
      assert.equal(readCapabilityList(v), null, JSON.stringify(v));
    }
  });

  it("不认识的能力名保留（后端可以比前端先多一种能力）", () => {
    assert.deepEqual(readCapabilityList(["nanonis", "heater_control"]), ["nanonis", "heater_control"]);
  });
});

describe("capabilitiesFrom：三种画像 × 有 / 无能力字段", () => {
  const health = (profile: unknown, caps?: unknown) =>
    caps === undefined ? { station_profile: profile } : { station_profile: profile, station_capabilities: caps };

  for (const p of PROFILES) {
    it(`${p}，没有能力字段（老后端）：按画像推默认能力`, () => {
      const c = capabilitiesFrom(health(p));
      assert.equal(c.profile, p);
      assert.equal(c.source, "derived");
      assert.deepEqual(present(c), [...DEFAULT_CAPABILITIES[p]].sort());
      assert.deepEqual([...c.names], [...DEFAULT_CAPABILITIES[p]]);
    });

    it(`${p}，有能力字段：以字段为准，不看画像名`, () => {
      // 故意给一个和画像默认值不同的清单：门只能听清单的
      const reported = p === "stm" ? ["witec"] : ["nanonis", "scan_map"];
      const c = capabilitiesFrom(health(p, reported));
      assert.equal(c.profile, p);
      assert.equal(c.source, "server");
      assert.deepEqual(present(c), [...reported].sort());
    });

    it(`${p}，有能力字段且就是契约的那一行：和推出来的完全一样`, () => {
      const c = capabilitiesFrom(health(p, [...DEFAULT_CAPABILITIES[p]]));
      assert.equal(c.source, "server");
      assert.deepEqual(present(c), present(capabilitiesFrom(health(p))));
    });

    it(`${p}，能力字段是空列表：什么都没有`, () => {
      const c = capabilitiesFrom(health(p, []));
      assert.equal(c.source, "server");
      assert.deepEqual(present(c), []);
    });

    it(`${p}，能力字段是脏的：当没带，按画像推`, () => {
      for (const bad of [null, "nanonis", { nanonis: true }, ["nanonis", 3]]) {
        const c = capabilitiesFrom(health(p, bad));
        assert.equal(c.source, "derived", JSON.stringify(bad));
        assert.deepEqual(present(c), [...DEFAULT_CAPABILITIES[p]].sort(), JSON.stringify(bad));
      }
    });
  }

  it("health 还没回 / 不是对象 / 画像读不出来：STM 站的全套能力（今天每台机器都是 STM 站）", () => {
    for (const h of [undefined, null, 0, "ok", [], {}, { station_profile: "opitcal" }, { station_profile: 7 }]) {
      const c = capabilitiesFrom(h);
      assert.equal(c.profile, "stm", JSON.stringify(h));
      assert.equal(c.source, "derived");
      assert.deepEqual(present(c), [...STM_SET].sort(), JSON.stringify(h));
    }
  });

  it("画像名照 normalizeProfile 读：大小写和首尾空白不影响", () => {
    assert.equal(capabilitiesFrom({ station_profile: " Generic " }).profile, "generic");
    assert.deepEqual(present(capabilitiesFrom({ station_profile: "OPTICAL" })), [...OPTICAL_SET].sort());
  });

  it("不认识的能力名留在 names 里，但不让任何一道已知的门打开", () => {
    const c = capabilitiesFrom({ station_profile: "generic", station_capabilities: ["heater_control"] });
    assert.deepEqual([...c.names], ["heater_control"]);
    assert.deepEqual(present(c), []);
  });

  it("names 是冻结的：调用方改不了别人看到的清单", () => {
    const c = capabilitiesFrom({ station_profile: "stm" });
    assert.ok(Object.isFrozen(c.names));
  });
});

describe("readSerialAutodetect：station_serial_autodetect（§1.4，不是能力）", () => {
  it("三个取值原样收下", () => {
    for (const v of ["default_on", "default_off", "forced_off"] as const) {
      assert.equal(readSerialAutodetect({ station_serial_autodetect: v }), v);
    }
  });

  it("没带、脏值、health 没回 → null（老后端：界面什么都不画）", () => {
    for (const h of [undefined, null, {}, { station_serial_autodetect: "on" }, { station_serial_autodetect: true }, { station_serial_autodetect: "DEFAULT_OFF" }]) {
      assert.equal(readSerialAutodetect(h), null, JSON.stringify(h));
    }
  });
});

describe("normalizeProfile / profileNeverChosen：第三种画像与首次使用判据", () => {
  it("认 generic（大小写、首尾空白不算）", () => {
    for (const v of ["generic", " Generic ", "GENERIC\n"]) assert.equal(normalizeProfile(v), "generic", JSON.stringify(v));
  });

  it("只有后端明确说 default 才算没选过；缺字段、env、settings、脏值都不算", () => {
    assert.equal(profileNeverChosen("default"), true);
    for (const v of [undefined, null, "", "env", "settings", "Default", " default", 0, {}]) {
      assert.equal(profileNeverChosen(v), false, JSON.stringify(v));
    }
  });
});

describe("首次使用横幅：只给全新的机器", () => {
  const fresh = { degraded: false, completed_at: null, rig_fingerprint: null, acknowledged: [] };

  it("没选过画像，而且从没做过初始化 → 显示", () => {
    assert.equal(initHistory(fresh), "never");
    assert.equal(firstRunBannerShown({ neverChosen: true, init: fresh, initFailed: false }), true);
  });

  it("没选过画像，但初始化做过（在用的 STM 站升级上来）→ 不显示", () => {
    for (const used of [
      { ...fresh, completed_at: 1_760_000_000 },
      // 「重新提醒」只清完成戳，指纹留着：仍然是做过初始化的机器
      { ...fresh, completed_at: null, rig_fingerprint: "nanonis:abc123" },
      // 核对过某几项：有人用过
      { ...fresh, acknowledged: ["signal_chain.preamp_gain_v_per_a"] },
    ]) {
      assert.equal(initHistory(used), "used", JSON.stringify(used));
      assert.equal(firstRunBannerShown({ neverChosen: true, init: used, initFailed: false }), false, JSON.stringify(used));
    }
  });

  it("初始化状态读不出来 → 不显示（宁可少提醒，不误提醒）", () => {
    const unreadable: [unknown, boolean][] = [
      [undefined, false], // 还没回
      [undefined, true], // 请求失败
      [fresh, true], // 有旧缓存但最近一次失败
      [{ ...fresh, degraded: true }, false], // 后端说读不出
      [{ degraded: false, acknowledged: [] }, false], // 老后端：根本没有 completed_at 这个字段
      [null, false],
      ["oops", false],
    ];
    for (const [init, failed] of unreadable) {
      assert.equal(initHistory(init, failed), "unknown", JSON.stringify([init, failed]));
      assert.equal(firstRunBannerShown({ neverChosen: true, init, initFailed: failed }), false, JSON.stringify([init, failed]));
    }
  });

  it("选过画像（或环境变量钉住）→ 不管初始化怎样都不显示", () => {
    assert.equal(firstRunBannerShown({ neverChosen: false, init: fresh, initFailed: false }), false);
  });

  it("完成戳是 0 或负数不算完成（后端 sanitize 也只收正数）", () => {
    assert.equal(initHistory({ ...fresh, completed_at: 0 }), "never");
    assert.equal(initHistory({ ...fresh, completed_at: -5 }), "never");
  });
});

describe("overviewKind：首页按能力选哪一版", () => {
  const kindOf = (caps: Capability[]) => overviewKind((c) => caps.includes(c));

  it("有 Nanonis 是 STM 版，有 WITec 没 Nanonis 是光学版，两样都没有是通用版", () => {
    assert.equal(kindOf(STM_SET), "stm");
    assert.equal(kindOf(OPTICAL_SET), "optical");
    assert.equal(kindOf(GENERIC_SET), "generic");
    assert.equal(kindOf([]), "generic");
  });

  it("Nanonis 优先：同时有 Nanonis 和 WITec 的站看到的是 STM 读数", () => {
    assert.equal(kindOf(["nanonis", "witec"]), "stm");
  });

  it("三种画像的默认能力各落到自己那一版", () => {
    for (const p of PROFILES) {
      assert.equal(overviewKind(capabilitiesFrom({ station_profile: p }).has), p as StationProfile);
    }
  });
});

describe("estopTitle：急停按钮说的是这台站上按下去真正会做的事（§3.7）", () => {
  const titleOf = (caps: Capability[]) => estopTitle((c) => caps.includes(c));
  // 快照：此前 TopBar 写死的那一句，一个字都不能变
  const STM_TODAY = "紧急停止：中止任务 + 停止运动 + 退针";
  const ELSEWHERE = "紧急停止：中止任务 + 停止运动 + 关激光与快门 + 磁体停止扫场 + 加热关闭";

  it("STM 站：和今天逐字一致", () => {
    assert.equal(titleOf(STM_SET), STM_TODAY);
    assert.equal(titleOf(["nanonis"]), STM_TODAY, "只看 nanonis，不看别的能力");
    assert.equal(titleOf(["nanonis", "laser_safety", "operator_prompts"]), STM_TODAY);
  });

  it("光学站和通用站：同一句，后端在两种站上做的事一样；不提退针", () => {
    assert.equal(titleOf(OPTICAL_SET), ELSEWHERE);
    assert.equal(titleOf(GENERIC_SET), ELSEWHERE);
    assert.ok(!ELSEWHERE.includes("退针"));
  });

  it("关激光不挂在 laser_safety 上：没有它的站，后端照样跑 safe_off_all", () => {
    assert.equal(titleOf([]), ELSEWHERE);
    assert.equal(titleOf(["optical_bench"]), ELSEWHERE);
  });
});

describe("capabilityLabel", () => {
  it("每种能力有自己的名字，不是裸 id", () => {
    const labels = CAPABILITIES.map((c) => capabilityLabel(c));
    assert.equal(new Set(labels).size, CAPABILITIES.length);
    CAPABILITIES.forEach((c, i) => assert.notEqual(labels[i], c));
  });
});

describe("导航门：filterTabs / visibleTabs / pathVisible", () => {
  const TABS_FIXTURE: NavTab[] = [
    { to: "/", label: "home", end: true },
    { to: "/flat-stm", label: "flat", requires: "nanonis" },
    { to: "/old", label: "old", legacy: true },
    {
      to: "/group",
      label: "group",
      sections: [
        { seg: "always", label: "a" },
        { seg: "stm", label: "s", requires: "nanonis" },
        { seg: "map", label: "m", requires: "scan_map" },
        { seg: "old", label: "o", legacy: true },
        { seg: "old-stm", label: "os", legacy: true, requires: "nanonis" },
      ],
    },
    { to: "/only-stm", label: "only", sections: [{ seg: "x", label: "x", requires: "stm_safety" }] },
  ];
  const view = (legacy: boolean, caps: Capability[]) =>
    filterTabs(TABS_FIXTURE, legacy, (c) => caps.includes(c)).map((t) =>
      t.sections ? `${t.to}[${t.sections.map((s) => s.seg).join(",")}]` : t.to,
    );

  it("STM 站：能力段全在，旧版段照旧只看旧版开关", () => {
    assert.deepEqual(view(false, STM_SET), ["/", "/flat-stm", "/group[always,stm,map]", "/only-stm[x]"]);
    assert.deepEqual(view(true, STM_SET), ["/", "/flat-stm", "/old", "/group[always,stm,map,old,old-stm]", "/only-stm[x]"]);
  });

  it("没有这些能力的站：要能力的段和标签都不在；一个段都不剩的大标签整个不在", () => {
    for (const caps of [OPTICAL_SET, GENERIC_SET, [] as Capability[]]) {
      assert.deepEqual(view(false, caps), ["/", "/group[always]"]);
      assert.deepEqual(view(true, caps), ["/", "/old", "/group[always,old]"], "旧版开着也不放出 STM 段");
    }
  });

  it("每种能力各管各的段", () => {
    assert.deepEqual(view(false, ["scan_map"]), ["/", "/group[always,map]"]);
    assert.deepEqual(view(false, ["stm_safety"]), ["/", "/group[always]", "/only-stm[x]"]);
  });

  it("不改输入表", () => {
    const before = JSON.stringify(TABS_FIXTURE);
    view(false, []);
    assert.equal(JSON.stringify(TABS_FIXTURE), before);
  });

  it("visibleTabs 不给能力时按全都有算（STM 站的导航），这是给老调用方的默认值", () => {
    assert.deepEqual(visibleTabs(false), visibleTabs(false, () => true));
  });

  it("pathVisible：大标签和二级段都按同一套规则判", () => {
    assert.equal(pathVisible("/settings/general", false, () => false), true);
    assert.equal(pathVisible("/no/such/page", false, () => true), false);
  });
});

describe("真正的导航表：每种站藏掉哪几页", () => {
  /** Every visible page (tab or section) for a profile's default capabilities. */
  const pages = (profile: StationProfile, legacy = false) => {
    const has = capabilitiesFrom({ station_profile: profile }).has;
    return visibleTabs(legacy, has).flatMap((t) =>
      t.sections ? t.sections.map((s) => `${t.to}/${s.seg}`) : [t.to],
    );
  };
  const HIDDEN_WITHOUT_STM = ["/monitoring/current", "/records/map", "/settings/setup"];

  it("STM 站：一页不少（和加能力门之前一样）", () => {
    assert.deepEqual(pages("stm"), visibleTabs(false).flatMap((t) => (t.sections ? t.sections.map((s) => `${t.to}/${s.seg}`) : [t.to])));
    for (const p of HIDDEN_WITHOUT_STM) assert.ok(pages("stm").includes(p), p);
  });

  for (const profile of ["optical", "generic"] as const) {
    it(`${profile}：正好藏掉仪器初始化、信号监控、扫描地图这三页`, () => {
      const missing = pages("stm").filter((p) => !pages(profile).includes(p)).sort();
      assert.deepEqual(missing, HIDDEN_WITHOUT_STM);
    });

    it(`${profile}：大标签一个不少（每个都还有能进的页），旧版开着也不放出 STM 页`, () => {
      assert.deepEqual(
        visibleTabs(false, capabilitiesFrom({ station_profile: profile }).has).map((t) => t.to),
        visibleTabs(false).map((t) => t.to),
      );
      for (const p of HIDDEN_WITHOUT_STM) assert.ok(!pages(profile, true).includes(p), p);
    });
  }

  it("每个挂了能力的页，挂的就是后端门住同一样东西的那个能力（契约 §1.2 / §2）", () => {
    // 三种默认画像上 nanonis 和 stm_safety 的结果一样，所以上面几条分不出挂错了哪个；这里直接钉名字。
    // 信号监控读的是电流监控的库，后端只在有 stm_safety 时起它（§2 第 6 项）；初始化清单是针尖的安全参数；
    // 扫描地图是 scan_map 本身。
    const gated = visibleTabs(true).flatMap((t) => [
      ...(t.requires ? [[t.to, t.requires] as const] : []),
      ...(t.sections ?? []).filter((s) => s.requires).map((s) => [`${t.to}/${s.seg}`, s.requires!] as const),
    ]);
    assert.deepEqual(Object.fromEntries(gated), {
      "/records/map": "scan_map",
      "/settings/setup": "stm_safety",
      "/monitoring/current": "stm_safety",
    });
  });

  it("仪器工具（图像拼接只读文件）、光路图、光学台、环境历史、数据图库、光谱 / TERS 不挂能力：哪种站都有", () => {
    for (const profile of PROFILES) {
      for (const p of ["/experimental/tools", "/experimental/layout", "/experimental/optics", "/monitoring/env", "/records/gallery", "/ters", "/wishlist"]) {
        assert.ok(pages(profile).includes(p), `${profile}: ${p}`);
      }
    }
  });
});

describe("生成的能力表（src/lib/stationCapabilities.json，后端脚本产出，原样放进来）", () => {
  const raw = readFileSync(new URL("../src/lib/stationCapabilities.json", import.meta.url), "utf8");
  const table = JSON.parse(raw) as {
    capabilities: string[];
    profiles: Record<string, string[]>;
    requires: Record<string, string[]>;
  };

  it("文件里的名字就是代码里的 CAPABILITIES，也就是契约 §1.2 的顺序", () => {
    assert.deepEqual(table.capabilities, [...CAPABILITIES]);
    assert.deepEqual(table.capabilities, ALL_IN_ORDER);
  });

  it("三种画像的默认能力、依赖表和契约逐项相同", () => {
    assert.deepEqual(table.profiles, { stm: STM_SET, optical: OPTICAL_SET, generic: GENERIC_SET });
    assert.deepEqual(table.requires, {
      stm_safety: ["nanonis"],
      stm_actions: ["nanonis"],
      scan_map: ["nanonis"],
      laser_safety: ["operator_prompts"],
    });
  });

  it("每个画像的集合对依赖封闭，用到的名字都在能力表里", () => {
    for (const [p, set] of Object.entries(table.profiles)) {
      for (const c of set) {
        assert.ok(table.capabilities.includes(c), `${p}: ${c} 不在能力表里`);
        for (const need of table.requires[c] ?? []) assert.ok(set.includes(need), `${p}: ${c} 要 ${need}`);
      }
    }
  });

  it("文件末尾有换行（行尾在 Windows 检出里可能被换成 CRLF，不比）", () => {
    assert.ok(/\}\r?\n$/.test(raw));
  });
});
