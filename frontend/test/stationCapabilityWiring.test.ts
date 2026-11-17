/**
 * 能力门在页面里的接线：每一处向 Nanonis 要东西的地方都在门后面，导航和路由按能力藏页面，首次使用横幅挂上了。
 *
 *     cd frontend && npm run test:unit
 *
 * React 组件在 node 下跑不了，所以和 stationProfileSwitch / operatorWiring 一样按源码文本断言（先剥注释：
 * 注释里常把错误写法原样写出来讲解）。纯函数的取值在 test/stationCapabilities.test.ts；这里只钉「接上了」。
 *
 * ── 最要紧的一条：扫一遍，不靠记性 ──
 * 门是一处一处加的，很容易漏：光学站上 TopBar 和 RightPanel 曾一直在轮询 Nanonis 接口。
 * 所以下面先把 src 里所有请求 /api/nanonis* 和 /api/hardware/live-readings 的文件列出来，
 * 和一份写明了「它在哪道门后面」的清单对账：多出一个文件，测试就红，逼着新代码的作者回答它在哪道门后面。
 * 端到端（e2e/station-capabilities.spec.ts）在真浏览器里再确认一次：非 STM 站一个这样的请求都不发。
 */
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

const SRC = join(fileURLToPath(new URL(".", import.meta.url)), "..", "src");

function stripComments(text: string): string {
  return text
    .replace(/\r\n/g, "\n")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:"'`\\])\/\/[^\n]*/g, "$1");
}

const code = (rel: string) => stripComments(readFileSync(join(SRC, rel), "utf8"));

function files(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) files(p, out);
    else if (/\.tsx?$/.test(name)) out.push(p);
  }
  return out;
}

/** Every module under src, as [relative path, comment-free text]. */
const SOURCES = files(SRC).map((f) => [relative(SRC, f).replace(/\\/g, "/"), stripComments(readFileSync(f, "utf8"))] as const);

/** The URL literals a module asks for. */
function urlLiterals(text: string): string[] {
  return [...text.matchAll(/(["'`])(\/api\/[^"'`]*)\1/g)].map((m) => m[2]!);
}

const STM_ENDPOINT = /^\/api\/(nanonis(\/|$)|hardware\/live-readings$)/;

describe("每一处 Nanonis 请求都在门后面（对账，不靠记性）", () => {
  // 文件 → 它在哪道门后面。新文件要进这张表，得先写清楚它的门。
  const GATED: Record<string, string> = {
    "components/shell/TopBar.tsx": "两个查询 enabled: stmReads（= station.ready && nanonis）",
    "components/shell/RightPanel.tsx": "两个查询 enabled: stmReads",
    "pages/DashboardPage.tsx": "useConnection(stmReads) / useLiveReadings(stmReads)",
    "api/manual.ts": "useTipState(enabled)，唯一的调用方传 station.ready && nanonis",
    "components/settings/NanonisPortsReadOnly.tsx": "只由设置页在 nanonisPorts 为真时挂载",
    "components/admin/HardwareManager.tsx": "其中的 NanonisCard 只在 nanonisReads 为真时挂载（传感器卡每种站都有）",
  };

  // dev-only mock backends answer these URLs, they do not ask them (and never reach a build)
  const DEV_MOCKS = new Set(["lib/opticalLayout/devMock.ts", "lib/manual/devMockManual.ts"]);

  it("请求这些接口的文件正好是清单上这几个", () => {
    const askers = SOURCES.filter(([name]) => !name.startsWith("api/schema") && !DEV_MOCKS.has(name))
      .filter(([, text]) => urlLiterals(text).some((u) => STM_ENDPOINT.test(u)))
      .map(([name]) => name)
      .sort();
    assert.deepEqual(askers, Object.keys(GATED).sort(), "有文件新请求了 Nanonis 接口：先把它放到能力门后面，再登记到 GATED");
  });

  it("TopBar：两个轮询和推送订阅都等能力；读数行和 Nanonis 状态只在有 Nanonis 时画；急停文案按能力", () => {
    const src = code("components/shell/TopBar.tsx");
    assert.match(src, /const station = useStationCapabilities\(\);/);
    assert.match(src, /const nanonis = station\.has\("nanonis"\);/);
    assert.match(src, /const stmReads = station\.ready && nanonis;/);
    assert.match(src, /queryKey: LIVE_READINGS_KEY,\s*enabled: stmReads,/);
    assert.match(src, /queryKey: \["nanonis", "connection"\],\s*enabled: stmReads,/);
    assert.match(src, /useWsEvent\("hardware_state", onHardwareState, stmReads\);/);
    assert.match(src, /\{nanonis && \(\s*<div className="mast-scroll flex items-center/, "第二行读数要被 nanonis 包住");
    assert.match(src, /\{nanonis \? \(\s*<span className="inline-flex items-center gap-1\.5 text-xs text-mast-text">/);
    assert.match(src, /const estopHint = estopTitle\(station\.has\);/);
    assert.match(src, /title=\{estopHint\}/);
    assert.doesNotMatch(src, /title=\{tr\("紧急停止：中止任务 \+ 停止运动 \+ 退针"/, "急停文案不能再写死成 STM 的");
  });

  it("RightPanel：两个轮询等能力；INSTRUMENT 与 TCP 只在有 Nanonis 时画；Noise 的链接只在那一页可见时给", () => {
    const src = code("components/shell/RightPanel.tsx");
    assert.match(src, /const stmReads = station\.ready && nanonis;/);
    assert.match(src, /queryKey: \["hardware", "live-readings"\],\s*enabled: stmReads,/);
    assert.match(src, /queryKey: \["nanonis", "connection"\],\s*enabled: stmReads,/);
    assert.match(src, /\{nanonis \? \(\s*<PanelSection title="INSTRUMENT">/);
    assert.match(src, /\{nanonis && \(\s*<PanelSection title="TCP">/);
    assert.match(src, /k\.key === "noise_level" && pathVisible\("\/monitoring\/current", legacy, station\.has\)/);
  });

  it("光学台的手动控制：针尖那一行只在有 Nanonis 时画、才去读", () => {
    const src = code("components/manual/ManualControlSection.tsx");
    assert.match(src, /const tip = useTipState\(station\.ready && nanonis\);/);
    assert.match(src, /\{nanonis && \(\s*<span className=\{clsx\(r\?\.withdrawn === false/);
    assert.match(code("api/manual.ts"), /export function useTipState\(enabled: boolean\) \{\s*return useQuery\(\{\s*queryKey: \["hardware", "live-readings"\],\s*enabled,/);
    const callers = SOURCES.filter(([name, text]) => name !== "api/manual.ts" && /useTipState\(/.test(text)).map(([n]) => n);
    assert.deepEqual(callers, ["components/manual/ManualControlSection.tsx"]);
  });

  it("设置页：初始化入口和 Nanonis 端口只在有对应能力、并且站已经说过之后才挂载", () => {
    const src = code("pages/SettingsPage.tsx");
    assert.match(src, /const stmSetup = station\.ready && station\.has\("stm_safety"\);/);
    assert.match(src, /const nanonisPorts = station\.ready && station\.has\("nanonis"\);/);
    assert.match(src, /\{stmSetup && <SetupEntryRow toast=\{toast\} \/>\}/);
    assert.match(src, /\{nanonisPorts && \(\s*<Accordion title=\{tr\("硬件连接 Hardware"/);
    const mounts = SOURCES.filter(([, text]) => /<NanonisPortsReadOnly\b/.test(text)).map(([n]) => n);
    assert.deepEqual(mounts, ["pages/SettingsPage.tsx"]);
  });

  it("右栏的针尖登记挂 nanonis（和后端同一个能力，契约 §1.2；它一挂就去要 /api/tips）", () => {
    const src = code("components/scope/ScopeControls.tsx");
    assert.match(src, /const hasTip = station\.ready && station\.has\("nanonis"\);/);
    assert.match(src, /\{hasTip && \(\s*<div className="border-t border-mast-border pt-2">\s*<TipControls \/>/);
    const mounts = SOURCES.filter(([, text]) => /<TipControls\b/.test(text)).map(([n]) => n);
    assert.deepEqual(mounts, ["components/scope/ScopeControls.tsx"]);
  });

  it("高级页的「硬件连接」每种站都有（通用站在这里管它的环境传感器）；只有里面的 Nanonis 卡挂 nanonis", () => {
    const admin = code("pages/AdminPage.tsx");
    assert.match(admin, /\{ id: "hardware", label: tr\("硬件连接", "Hardware connection"\) \}/);
    assert.match(admin, /\{sub === "hardware" && \(/);
    const hw = code("components/admin/HardwareManager.tsx");
    assert.match(hw, /const nanonisReads = station\.ready && station\.has\("nanonis"\);/);
    assert.match(hw, /\{nanonisReads && <NanonisCard readOnly=\{readOnly\} \/>\}/);
    assert.match(hw, /<SensorsCard readOnly=\{readOnly\} \/>/, "传感器卡不挂能力");
    assert.equal([...hw.matchAll(/<NanonisCard\b/g)].length, 1, "NanonisCard 只有这一处挂载");
  });

  it("传感器卡的「自动探测 / 手动」徽标：自动探测不按配置的站（强制关 / 默认关）用后端给的生效值，STM 站照旧看文件", () => {
    const hw = code("components/admin/HardwareManager.tsx");
    assert.match(
      hw,
      /\(station\.serialAutodetect === "forced_off" \|\| station\.serialAutodetect === "default_off"\) &&\s*typeof effective === "boolean"\s*\? effective\s*: q\.data\?\.autodetect;/,
    );
  });
});

describe("仪器工具：页面每种站都有，Nanonis 的两块挂 nanonis（图像拼接只读文件，保留）", () => {
  const src = code("pages/ExperimentalPage.tsx");

  it("信号捕获和长期监控要 nanonis，图像拼接不要", () => {
    assert.match(src, /\{ id: "capture", label: tr\("信号捕获 \+ FFT", "Signal capture \+ FFT"\), requires: "nanonis" \}/);
    assert.match(src, /\{ id: "monitor", label: tr\("长期监控", "Long-term monitoring"\), requires: "nanonis" \}/);
    assert.match(src, /\{ id: "mosaic", label: tr\("图像拼接", "Mosaic"\) \}/);
    assert.match(src, /const tabs = SUB_TABS\.filter\(\(t\) => t\.requires === undefined \|\| station\.has\(t\.requires\)\);/);
  });

  it("两块 Nanonis 面板一挂就要通道列表：等站说完话才挂；记住的子页被藏了就落到第一个可见的", () => {
    assert.match(src, /const nanonisReads = station\.ready && nanonis;/);
    assert.match(src, /\{tab === "capture" && nanonisReads && <SignalCapturePanel \/>\}/);
    assert.match(src, /\{tab === "monitor" && nanonisReads && \(/);
    assert.match(src, /const tab: SubTab = tabs\.some\(\(t\) => t\.id === remembered\) \? remembered : \(tabs\[0\]\?\.id \?\? "mosaic"\);/);
  });

  it("「直连 Nanonis 硬件」那句提醒只在有 Nanonis 时说", () => {
    assert.match(src, /\{nanonis\s*\? tr\(\s*"实验性功能 \/ Experimental — 直接调用仪器 skill/);
  });
});

describe("设置页里纯 STM 的几块：按能力藏；STM 站上和今天一模一样（快照）", () => {
  const src = code("pages/SettingsPage.tsx");
  /** The accordion titles, in order: what the page is made of. */
  const titles = (text: string) =>
    [...text.matchAll(/<Accordion\s+title=\{(?:tr\(\s*"([^"]+)"|([A-Z_]+))/g)].map((m) => m[1] ?? m[2]);

  it("快照：折叠块的标题和顺序与改动前一字不差（没有删、没有挪、没有改名）", () => {
    // 改动前设置页里的那一份
    assert.deepEqual(titles(src), [
      "模型 Model",
      "各 Agent 模型 / Thinking（与 Agents 页同源）",
      "外观 Appearance",
      "硬件连接 Hardware",
      "温度计 / 真空计 Instruments",
      "远程访问 Remote（Tailscale 跨网控制）",
      "卫星通道 Satellites（光谱电脑）",
      "实验默认参数 Experiment defaults",
      "Z 参数组 Z-control presets（AI 按名应用，数值由代码写）",
      "扫描参数档位 Scan policy（按尺度：速度 / 像素 / 反馈）",
      "仪器进退针 Instrument (退针方向 / dI/dV)",
      "语音 Voice",
      "自治与成本 Autonomy & cost",
      "CONDUCT_SETTINGS_TITLE",
      "对话处理与归档 Conversation & archiving",
      "视觉判别阈值 Vision",
      "经典针尖阈值 Classical（免模型 · 按仪器重标定）",
      "信号监控 Signal Monitor（常开 / 保留策略 / 告警阈值）",
      "环境历史 Environment History（记录粒度 / 保留期 / 噪声谱）",
      "硬件模块 Hardware",
      "其它 Other",
      "模型能力表 Capabilities",
    ]);
  });

  it("五块纯 STM 的各挂一个能力；这两个能力在 STM 站上都为真（画的东西不变），而且跟着 has() 走（STM 站第一帧就画）", () => {
    assert.match(src, /const stmControls = station\.has\("nanonis"\);/);
    assert.match(src, /const tipSafety = station\.has\("stm_safety"\);/);
    const gated: [string, string][] = [
      ["stmControls", "Z 参数组 Z-control presets"],
      ["stmControls", "扫描参数档位 Scan policy"],
      ["tipSafety", "仪器进退针 Instrument"],
      ["tipSafety", "经典针尖阈值 Classical"],
      ["tipSafety", "信号监控 Signal Monitor"],
    ];
    for (const [flag, title] of gated) {
      assert.match(
        src,
        new RegExp(`\\{${flag} && \\(\\s*<Accordion\\s+title=\\{tr\\(\\s*"${title.replace(/[()/]/g, "\\$&")}`),
        `「${title}」没有被 ${flag} 包住`,
      );
    }
  });

  it("藏只是不渲染：门里只有渲染，不碰任何保存（没有一处在门的条件里 save / mutate / 重置）", () => {
    for (const flag of ["stmControls", "tipSafety"]) {
      for (const m of src.matchAll(new RegExp(`\\b${flag}\\b[^\\n]*`, "g"))) {
        assert.doesNotMatch(m[0], /save\(|mutate\(|reset\(|delete|remove/, m[0]);
      }
    }
    // 门的定义处只读 has()，不写任何东西
    assert.doesNotMatch(src, /useEffect\([^)]*\b(stmControls|tipSafety)\b/);
  });
});

describe("串口自动探测：只在强制关 / 默认关的站上出现；只有它写 serial_autodetect", () => {
  const comp = code("components/settings/SerialAutodetectSetting.tsx");

  it("挂在设置页「温度计 / 真空计」里；STM 站（default_on）和老后端什么都不画、什么都不要", () => {
    assert.match(code("pages/SettingsPage.tsx"), /<SerialAutodetectSetting \/>/);
    assert.match(
      comp,
      /const mode = station\.serialAutodetect;\s*if \(mode !== "forced_off" && mode !== "default_off"\) return null;\s*return <SerialAutodetectRow mode=\{mode\} \/>;/,
    );
    // the queries live in the row, which an STM station never mounts
    const outer = comp.slice(comp.indexOf("export function SerialAutodetectSetting"), comp.indexOf("function SerialAutodetectRow"));
    assert.doesNotMatch(outer, /useQuery\(/);
  });

  it("开关只在默认关的站上；点一下只问，确认框里带 PIN 才写", () => {
    assert.match(comp, /\{view\.switchable && \(/);
    assert.match(comp, /onChange=\{\(v\) => \{\s*write\.reset\(\);\s*setAsking\(v\);\s*\}\}/);
    assert.match(comp, /write\.mutate\(\{ next: asking, pin \}, \{ onSuccess: \(\) => setAsking\(null\) \}\)/);
    assert.match(comp, /const body = \{ serial_autodetect: next, admin_pin: pin \} as SettingsPatch;/);
    assert.match(comp, /settingsWriteProblem\(data\)/);
    assert.match(comp, /const blocker = switchBlocker\(\{ pin, pinIsSet, busy \}\);/);
    assert.match(comp, /<SetPinForm onDone=\{\(\) => setPin\(""\)\} \/>/, "没 PIN 时同一份设 PIN 表单");
  });

  it("全 src 只有这一处写 serial_autodetect（对象字面量里的键，不算类型注解和读取）", () => {
    const writers = SOURCES.filter(([name]) => !name.startsWith("api/schema"))
      .filter(([, text]) => /[{,]\s*serial_autodetect\s*:/.test(text))
      .map(([name]) => name);
    assert.deepEqual(writers, ["components/settings/SerialAutodetectSetting.tsx"]);
  });
});

describe("导航与路由按能力藏页面", () => {
  it("顶栏标签和二级标签都按站的能力过滤", () => {
    assert.match(code("layout/AppLayout.tsx"), /const tabs = visibleTabs\(legacy, station\.has\);/);
    assert.match(code("layout/TabGroup.tsx"), /return visibleTabs\(enabled, station\.has\)\.find\(\(t\) => t\.to === to\);/);
  });

  it("组根地址等站说完话再跳，记住的段被藏了就去第一个可见段", () => {
    const src = code("layout/TabGroup.tsx");
    const group = src.slice(src.indexOf("export function GroupIndex"), src.indexOf("export function LegacyRedirect"));
    assert.match(group, /if \(!station\.ready\) return null;/);
    assert.match(group, /const target = ids\.includes\(remembered\) \? remembered : ids\[0\]!;/);
    assert.ok(group.indexOf("if (!station.ready) return null;") < group.indexOf("<Navigate"), "等能力必须在跳转之前");
  });

  it("要能力的段和标签经 CapabilityOnly 渲染；旧版页仍在 LegacyOnly 外层", () => {
    const src = code("router.tsx");
    assert.match(src, /const forStation = entry\.requires && el \? <CapabilityOnly cap=\{entry\.requires\}>\{el\}<\/CapabilityOnly> : el;/);
    assert.match(src, /return entry\.legacy \? <LegacyOnly>\{forStation\}<\/LegacyOnly> : forStation;/);
    assert.match(src, /return \{ path: sec\.seg, element: gated\(sec, el\) \};/);
    assert.match(src, /element: gated\(\{ legacy: LEGACY_FLAT\.has\(p\), requires: findTab\(p\)\?\.requires \}, element\),/);
  });

  it("CapabilityOnly：站没说话之前什么都不挂（不让页面抢先发请求），没有能力就只给说明", () => {
    const src = code("components/shell/CapabilityOnly.tsx");
    assert.match(src, /if \(!station\.ready\) return null;\s*return station\.has\(cap\) \? <>\{children\}<\/> : <CapabilityNotice cap=\{cap\} \/>;/);
  });
});

describe("能力 hook：一份 health，两种用法", () => {
  const src = code("hooks/useStationCapabilities.ts");

  it("读共用的 [\"health\"] 查询，能力由纯函数 capabilitiesFrom 算", () => {
    assert.match(src, /const HEALTH_KEY = \["health"\] as const;/);
    assert.match(src, /capabilitiesFrom\(q\.data\)/);
  });

  it("ready：答过了、或者已经失败过一次（不等 react-query 三次重试）", () => {
    assert.match(src, /if \(q\.data !== undefined \|\| q\.isError \|\| q\.failureCount > 0\) seen\.current = true;/);
  });

  it("ready 是个闩：答过或失败过一次就一直是 true（health 一直不成功时，keepAsking 的重取会把查询重置回 pending，没有闩的话每 10 s 掉一次、读数轮询的计时器跟着重来）", () => {
    assert.match(src, /const seen = useRef\(false\);/);
    assert.match(src, /const ready = seen\.current;/);
  });

  it("发请求的门 = ready && has", () => {
    assert.match(src, /return s\.ready && s\.has\(cap\);/);
  });
});

describe("首次使用", () => {
  it("横幅挂在壳上，在初始化横幅之前", () => {
    const layout = code("layout/AppLayout.tsx");
    const station = layout.indexOf("<StationSetupBanner />");
    const setup = layout.indexOf("<SetupBanner />");
    assert.ok(station > 0 && setup > station, "首次使用横幅要在初始化横幅前面");
  });

  it("横幅只给全新的机器：没选过画像，而且初始化记录读得出、从没完成过；在设置页本身不重复出现；hook 都在早退之前", () => {
    const src = code("components/shell/StationSetupBanner.tsx");
    const body = src.slice(src.indexOf("export function StationSetupBanner"));
    const hook = body.indexOf("useStationProfile()");
    const init = body.indexOf("useInstrumentInit({ enabled: station.firstRun })");
    const gate = body.indexOf(
      "if (!firstRunBannerShown({ neverChosen: station.firstRun, init: init.data, initFailed: init.isError })) return null;",
    );
    assert.ok(hook > 0 && init > hook, "初始化记录只在没选过画像时才去要");
    assert.ok(gate > init, "判据用纯函数 firstRunBannerShown，排在所有 hook 之后");
    assert.ok(body.indexOf('if (loc.pathname.startsWith("/settings/general")) return null;') > gate);
    assert.match(src, /export const STATION_CARD_PATH = "\/settings\/general#station-profile";/);
  });

  it("设置卡上的两步说明不受「全新机器」限制：只要没选过画像就显示", () => {
    const card = code("pages/SettingsPage.tsx");
    assert.match(card, /\{firstRun && <StationSetupSteps where="card" profile=\{profile\} \/>\}/);
  });

  it("判据是后端明说的 default（缺字段不算）", () => {
    const hook = code("hooks/useStationProfile.ts");
    assert.match(hook, /const firstRun = profileNeverChosen\(/);
    assert.match(code("lib/stationProfile.ts"), /export function profileNeverChosen\(rawSource: unknown\): boolean \{\s*return rawSource === "default";\s*\}/);
  });
});
