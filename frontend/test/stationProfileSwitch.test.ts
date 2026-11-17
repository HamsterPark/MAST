/**
 * 切换站点画像：确认文案、确认按钮的规则，以及它们在设置页里的接线。
 *
 *     cd frontend && npm run test:unit
 *
 * ── 为什么点一下不能直接存 ──
 * `station_profile` 在后端的 `admin_pin.GUARDED_KEYS` 里。它决定**下次启动**时这个
 * 进程是什么：STM 站误点成 optical，下次启动不拨 Nanonis、撞针看门狗和电流监控都不起；光学站误点成
 * stm，串口自动探测恢复，往每个空闲串口发 *IDN? 和 Modbus 帧，操作员提示服务和激光撤销监听也不再
 * 安装。误点的代价要到重启才显现，所以必须在写入口之前拦住：单选框只「问」，弹出确认（写清两种画像各
 * 意味着什么、选错的后果、要重启才生效），在确认里输入管理 PIN，只有确认按钮才写，而且写的时候带着
 * PIN。
 *
 * 下面分两半：
 *   · 纯函数（`lib/stationProfile.ts` 的 confirmCopy / switchBlocker 等）：文案里该有的句子都有，
 *     按钮什么时候能按；
 *   · 接线（React 渲染在 node 下跑不了，按源码文本断言，和 settingsWrite / degradedWiring 同一个办法）：
 *     单选框的 onChange 不再直接 mutate，唯一的 mutate 在确认回调里并且带 PIN；PIN 只活在对话框的
 *     组件状态里，不写进任何 store / storage；失败时对话框不关；除了那一个 hook，没有别处写这个键。
 *
 * node 下没有存下的语言选择，`tr()` 返回中文（见 src/lib/i18n.ts），下面的文案断言据此。
 */
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import {
  confirmCopy,
  profileBlurb,
  profileEffects,
  profileLabel,
  switchBlocker,
  switchRisk,
} from "../src/lib/stationProfile.ts";

describe("profileEffects：三种画像各意味着什么", () => {
  it("STM 站：连 Nanonis、起三个线程、自动探测按配置、STM 技能、不装操作员服务", () => {
    const text = profileEffects("stm").join("\n");
    for (const needle of ["连接 Nanonis", "撞针看门狗", "电流监控", "串口自动探测", "STM 技能", "不安装操作员提示服务"]) {
      assert.ok(text.includes(needle), `STM 站的说明里没有「${needle}」`);
    }
  });

  it("光学站：不连 Nanonis、线程不起、自动探测强制关、只有光学与通用技能、装操作员服务", () => {
    const text = profileEffects("optical").join("\n");
    for (const needle of ["不连接 Nanonis", "撞针看门狗", "强制关闭", "光学与通用技能", "安装操作员提示服务", "激光撤销监听"]) {
      assert.ok(text.includes(needle), `光学站的说明里没有「${needle}」`);
    }
  });

  it("通用站（契约 §1.2 / §1.4 / §3）：不连 Nanonis、线程不起、自动探测默认关但可以打开、外部设备技能、装操作员服务", () => {
    const text = profileEffects("generic").join("\n");
    for (const needle of [
      "不连接 Nanonis",
      "撞针看门狗",
      "串口自动探测默认关闭",
      "可以在设置里打开",
      "外部设备的技能",
      "看不到 STM 技能和 WITec 技能",
      "环境读数",
      "安装操作员提示服务和激光撤销监听",
    ]) {
      assert.ok(text.includes(needle), `通用站的说明里没有「${needle}」`);
    }
    // 两句旧的错误文案不能回来：通用站不是强制关，也装操作员服务
    assert.ok(!text.includes("强制关闭"), "通用站的自动探测是默认关（default_off），不是强制关");
    assert.ok(!text.includes("不安装操作员提示服务"), "通用站有 operator_prompts，装操作员服务");
  });

  it("三种画像条数一样多（并排比得了）、没有空句、互不相同", () => {
    const all = (["stm", "optical", "generic"] as const).map((p) => profileEffects(p));
    for (const lines of all) assert.equal(lines.length, all[0]!.length);
    for (const line of all.flat()) assert.ok(line.trim().length > 10, JSON.stringify(line));
    assert.equal(new Set(all.map((l) => l.join("\n"))).size, 3);
  });
});

describe("switchRisk：选错的后果按方向说", () => {
  it("STM / generic → optical：不拨 Nanonis，撞针看门狗和电流监控都不起；通用站的自动探测被强制关", () => {
    const t = switchRisk("optical");
    assert.ok(t.includes("其实是 STM 站"));
    for (const needle of ["不会连接 Nanonis", "撞针看门狗", "电流监控", "其实是通用站", "强制关闭"]) assert.ok(t.includes(needle), needle);
  });

  it("optical / generic → stm：串口自动探测恢复，*IDN? 和 Modbus 帧，会去连 Nanonis，操作员服务与撤销监听不再安装", () => {
    const t = switchRisk("stm");
    assert.ok(t.includes("其实是光学站或通用站"));
    for (const needle of ["串口自动探测会恢复", "*IDN?", "Modbus", "连接 Nanonis", "撤销监听"]) assert.ok(t.includes(needle), needle);
  });

  it("stm / optical → generic：STM 站的看门狗不起；光学站看不到 WITec 技能、手动状态只剩签署", () => {
    const t = switchRisk("generic");
    assert.ok(t.includes("其实是 STM 站") && t.includes("其实是光学站"), t);
    for (const needle of ["不会连接 Nanonis", "撞针看门狗", "电流监控", "看不到 WITec 技能", "激光区域签署"]) {
      assert.ok(t.includes(needle), needle);
    }
    // 通用站照样装撤销监听（laser_safety），所以「不再自动关光」不是它的风险
    assert.ok(!t.includes("不会再自动关光"), t);
  });

  it("三个方向说的各不相同", () => {
    assert.equal(new Set([switchRisk("stm"), switchRisk("optical"), switchRisk("generic")]).size, 3);
  });
});

describe("confirmCopy：确认框里的全部文字", () => {
  const c = confirmCopy("stm", "optical");

  it("标题和确认按钮都写明要改成什么", () => {
    assert.ok(c.title.includes(profileLabel("optical")), c.title);
    assert.ok(c.confirmLabel.includes(profileLabel("optical")), c.confirmLabel);
    const back = confirmCopy("optical", "stm");
    assert.ok(back.title.includes(profileLabel("stm")) && back.confirmLabel.includes(profileLabel("stm")));
  });

  it("三种画像都列出来，顺序固定（stm、optical、generic），只有要改成的那个 selected", () => {
    assert.deepEqual(c.profiles.map((p) => p.profile), ["stm", "optical", "generic"]);
    assert.deepEqual(c.profiles.map((p) => p.selected), [false, true, false]);
    assert.deepEqual(confirmCopy("optical", "stm").profiles.map((p) => p.selected), [true, false, false]);
    assert.deepEqual(confirmCopy("stm", "generic").profiles.map((p) => p.selected), [false, false, true]);
    for (const p of c.profiles) {
      assert.equal(p.label, profileLabel(p.profile));
      assert.equal(p.blurb, profileBlurb(p.profile), "每一栏先说这种站是什么");
      assert.deepEqual(p.lines, profileEffects(p.profile), "再说它开什么、关什么");
    }
  });

  it("改成通用站：标题、按钮、风险都按通用站说", () => {
    const g = confirmCopy("stm", "generic");
    assert.ok(g.title.includes("通用站") && g.confirmLabel.includes("通用站"), g.title);
    assert.equal(g.risk, switchRisk("generic"));
  });

  it("写了选错的后果，且是按目标方向说的", () => {
    assert.equal(c.risk, switchRisk("optical"));
    assert.equal(confirmCopy("optical", "stm").risk, switchRisk("stm"));
  });

  it("写明要重启才生效、保存时什么都不会立刻变", () => {
    assert.ok(c.restart.includes("重启 MAST 服务"), c.restart);
    assert.ok(c.restart.includes("不会立刻改变任何东西"), c.restart);
  });

  it("说明里提到管理 PIN（这是一道要 PIN 的保存）", () => {
    assert.ok(c.intro.includes("管理 PIN"), c.intro);
  });

  it("环境变量钉住画像时多一句：保存不改变实际运行的画像；别的来源没有这句", () => {
    assert.equal(c.envNote, null);
    assert.equal(confirmCopy("stm", "optical", "settings").envNote, null);
    assert.equal(confirmCopy("stm", "optical", "default").envNote, null);
    const env = confirmCopy("stm", "optical", "env").envNote;
    assert.ok(env && env.includes("MAST_STATION_PROFILE") && env.includes("不会改变实际运行的画像"), String(env));
  });

  it("stm / optical / generic 是身份字符串，不经过 tr()", () => {
    for (const p of c.profiles) assert.ok(p.profile === "stm" || p.profile === "optical" || p.profile === "generic");
  });
});

describe("confirmCopy：第一次使用、存的就是正在运行的画像", () => {
  it("第一次使用：说明还没存过、现在按默认运行，仍然要管理 PIN", () => {
    const c = confirmCopy("stm", "generic", "default", { firstRun: true, active: "stm" });
    assert.ok(c.intro.includes("还没有保存过站点类型"), c.intro);
    assert.ok(c.intro.includes("默认的「STM 站」"), c.intro);
    assert.ok(c.intro.includes("管理 PIN"), c.intro);
  });

  it("确认默认值（from === to）：标题和按钮是「确认是」，不是「改成」", () => {
    const c = confirmCopy("stm", "stm", "default", { firstRun: true, active: "stm" });
    assert.ok(c.title.startsWith("确认这台机器是「STM 站」"), c.title);
    assert.ok(c.confirmLabel.startsWith("确认是「STM 站」"), c.confirmLabel);
    assert.ok(!c.title.includes("改成"), c.title);
  });

  it("存的就是正在运行的画像：明说不需要重启；否则照旧说要重启才生效", () => {
    assert.ok(confirmCopy("stm", "stm", "default", { active: "stm", firstRun: true }).restart.includes("不需要重启"));
    assert.ok(confirmCopy("optical", "stm", "settings", { active: "stm" }).restart.includes("不需要重启"), "改回正在运行的那个也不用重启");
    const pending = confirmCopy("stm", "generic", "settings", { active: "stm" }).restart;
    assert.ok(pending.includes("重启 MAST 服务") && pending.includes("不会立刻改变任何东西"), pending);
    // 不知道正在运行什么（health 还没回）时，不能许诺「不需要重启」
    assert.ok(!confirmCopy("stm", "stm", "default", { firstRun: true }).restart.includes("不需要重启"));
  });

  it("不是第一次使用时，说明照旧是「现在保存的是…」", () => {
    assert.ok(confirmCopy("optical", "stm", "settings", { active: "optical" }).intro.startsWith("现在保存的是「光学站」"));
  });
});

describe("switchBlocker：确认按钮什么时候能按", () => {
  const ok = { pin: "4321", pinIsSet: true, busy: false };

  it("PIN 填了、机器有 PIN、没在保存 → 能按", () => {
    assert.equal(switchBlocker(ok), null);
  });

  it("没填 PIN（含只有空白）→ 不能按", () => {
    assert.equal(switchBlocker({ ...ok, pin: "" }), "empty_pin");
    assert.equal(switchBlocker({ ...ok, pin: "   " }), "empty_pin");
  });

  it("这台机器还没设 PIN → 不能按（后端会 fail-closed 拒绝，不如当场说清）", () => {
    assert.equal(switchBlocker({ ...ok, pinIsSet: false }), "no_pin_set");
    assert.equal(switchBlocker({ pin: "", pinIsSet: false, busy: false }), "no_pin_set", "没设 PIN 比没填更根本");
  });

  it("PIN 状态没查到（undefined）不挡：状态查询失败不能把操作员锁在对话框外，后端照样会拒绝并说明原因", () => {
    assert.equal(switchBlocker({ ...ok, pinIsSet: undefined }), null);
    assert.equal(switchBlocker({ pin: "", pinIsSet: undefined, busy: false }), "empty_pin");
  });

  it("正在保存 → 不能按（不重复提交）", () => {
    assert.equal(switchBlocker({ ...ok, busy: true }), "busy");
  });
});

describe("确认框的整个流程：未设 PIN → 先设 PIN → 再确认", () => {
  // 对话框的每一步由两样东西决定：pin-status 的回答（pinIsSet）和操作员敲进去的 PIN。
  // 这里按真实顺序把它们走一遍，看确认按钮在每一步的状态；对话框怎么把这几步接起来，下面「接线：对话框」
  // 钉住源码，浏览器里的完整一遍在 e2e/station-capabilities.spec.ts 的首次使用用例。
  it("每一步的确认按钮：先挡在「没设 PIN」，设好后挡在「没填」，填了才能按，提交中再挡住", () => {
    const steps: { what: string; pin: string; pinIsSet: boolean | undefined; busy: boolean; want: string | null }[] = [
      { what: "刚打开，PIN 状态还没回来", pin: "", pinIsSet: undefined, busy: false, want: "empty_pin" },
      { what: "回来了：这台机器没有 PIN", pin: "", pinIsSet: false, busy: false, want: "no_pin_set" },
      { what: "在框里的设 PIN 表单里设好了，pin-status 刷新", pin: "", pinIsSet: true, busy: false, want: "empty_pin" },
      { what: "再输一次 PIN", pin: "2468", pinIsSet: true, busy: false, want: null },
      { what: "按下确认，保存中", pin: "2468", pinIsSet: true, busy: true, want: "busy" },
    ];
    for (const s of steps) {
      assert.equal(switchBlocker({ pin: s.pin, pinIsSet: s.pinIsSet, busy: s.busy }), s.want, s.what);
    }
  });

  it("把这几步接起来的源码：设 PIN 表单刷新 pin-status、刷新后换出 PIN 输入框、确认把 PIN 交给卡片、卡片带着 PIN 写", () => {
    const lf = (rel: string) => readFileSync(join(SRC, rel), "utf8").replace(/\r\n/g, "\n");
    const gate = lf("components/admin/PinGate.tsx");
    assert.match(gate, /queryClient\.invalidateQueries\(\{ queryKey: \["admin", "pin-status"\] \}\)/);
    const dlg = lf("components/settings/StationProfileDialog.tsx");
    assert.match(dlg, /queryKey: \["admin", "pin-status"\]/, "对话框读的就是被刷新的那一个查询");
    assert.match(dlg, /\{pinIsSet === false \? \(/, "没 PIN 时是设 PIN 表单，有了 PIN（或状态未知）才是输入框");
    assert.match(dlg, /if \(blocker === null\) onConfirm\(pin\);/);
    const card = topLevelFunction(lf("pages/SettingsPage.tsx"), "StationProfileCard");
    assert.match(card, /setProfile\.mutate\(\{ profile: pending, pin \}/);
  });
});

// ── 接线 ──────────────────────────────────────────────────────────────────────
const HERE = fileURLToPath(new URL(".", import.meta.url));
const SRC = join(HERE, "..", "src");

/** 去掉注释的源码（注释里常把错误写法原样写出来讲解，不能让它触发断言）。
 *  工作树里的老文件是 CRLF（git 的 autocrlf），先统一成 LF，否则按行写的正则对不上。 */
function stripComments(text: string): string {
  return text
    .replace(/\r\n/g, "\n")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1");
}

function code(rel: string): string {
  return stripComments(readFileSync(join(SRC, rel), "utf8"));
}

function files(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) files(p, out);
    else if (/\.tsx?$/.test(name)) out.push(p);
  }
  return out;
}

/** 某个顶层 function 的函数体文本（到下一个顶格的 `}`）。 */
function topLevelFunction(src: string, name: string): string {
  const m = new RegExp(`\\nfunction ${name}\\([^)]*\\)[^{]*\\{[\\s\\S]*?\\n\\}\\n`).exec(src);
  assert.ok(m, `没找到 function ${name}`);
  return m[0];
}

describe("接线：点一下只问，只有确认才写，写的时候带 PIN", () => {
  const page = code("pages/SettingsPage.tsx");
  const card = topLevelFunction(page, "StationProfileCard");

  it("单选框的 onChange 是 choose，choose 不 mutate", () => {
    assert.match(card, /<RadioGroup value=\{shown\} onChange=\{choose\}/);
    const choose = /const choose = \(v: StationProfile\) => \{[\s\S]*?\n {2}\};/.exec(card);
    assert.ok(choose, "找不到 choose");
    assert.ok(!choose[0].includes(".mutate("), "choose 不能直接写");
    assert.match(choose[0], /setPending\(/, "choose 只是把选择挂起来等确认");
  });

  it("三个选项来自 PROFILES；第一次使用时点当前这一项也要确认（否则没法把默认值存下来，首次提示永远不消失）", () => {
    assert.match(page, /const STATION_CHOICES: \{ value: StationProfile; label: string \}\[\] = PROFILES\.map\(/);
    const choose = /const choose = \(v: StationProfile\) => \{[\s\S]*?\n {2}\};/.exec(card)!;
    assert.match(choose[0], /setPending\(v === configured && !firstRun \? null : v\)/);
    assert.match(card, /\{firstRun && <StationSetupSteps where="card" profile=\{profile\} \/>\}/);
  });

  it("对话框知道正在运行的画像和是不是第一次使用（文案要据此说要不要重启）", () => {
    assert.match(card, /active=\{loaded \? profile : undefined\}/);
    assert.match(card, /firstRun=\{firstRun\}/);
  });

  it("整张卡里只有一处 setProfile.mutate，在确认回调里，并且带着 PIN", () => {
    const calls = [...card.matchAll(/setProfile\.mutate\(/g)];
    assert.equal(calls.length, 1, "写站点画像的调用只能有一处");
    const confirm = /const confirm = \(pin: string\) => \{[\s\S]*?\n {2}\};/.exec(card);
    assert.ok(confirm, "找不到 confirm");
    assert.match(confirm[0], /setProfile\.mutate\(\{ profile: pending, pin \}/, "确认回调必须把 PIN 交给 mutate");
    assert.match(confirm[0], /onSuccess: \(\) => setPending\(null\)/, "成功才关对话框，失败时留着让用户看见原因");
  });

  it("确认对话框挂在卡里：挂起的选择非空才开，确认走 confirm，错误原样显示", () => {
    assert.match(card, /<StationProfileDialog/);
    assert.match(card, /open=\{pending !== null\}/);
    assert.match(card, /onConfirm=\{confirm\}/);
    assert.match(card, /error=\{setProfile\.isError \? String\(err\?\.message \?\? err\) : null\}/);
  });

  it("取消（含点遮罩）在保存期间不生效，之后清掉挂起的选择和上一次的错误", () => {
    const cancel = /const cancel = \(\) => \{[\s\S]*?\n {2}\};/.exec(card);
    assert.ok(cancel, "找不到 cancel");
    assert.match(cancel[0], /if \(busy\) return;/);
    assert.match(cancel[0], /setPending\(null\)/);
    assert.match(cancel[0], /setProfile\.reset\(\)/);
  });

  it("hook：mutation 的变量是 { profile, pin }，请求体同时带 station_profile 和 admin_pin，并且仍然查 settingsWriteProblem", () => {
    const hook = code("hooks/useStationProfile.ts");
    assert.match(hook, /export type SwitchRequest = \{ profile: StationProfile; pin: string \}/);
    assert.match(hook, /mutationFn: async \(\{ profile: next, pin \}: SwitchRequest\)/);
    assert.match(hook, /const body: SettingsPatch = \{ station_profile: next, admin_pin: pin \}/);
    assert.match(hook, /settingsWriteProblem\(data\)/, "PIN 被拒（pin_required）要变成错误，不能当成功");
  });

  it("除了那个 hook，没有别处写 station_profile（别的页面的通用保存不会顺带带上它）", () => {
    const writers = files(SRC)
      .filter((f) => !relative(SRC, f).startsWith("api"))
      // the optical layout page's dev mock puts station_profile in a faked /api/health answer, it writes no
      // setting; it never reaches a build (opticalLayoutWiring.test.ts: only main.tsx imports it, behind DEV)
      .filter((f) => relative(SRC, f).replace(/\\/g, "/") !== "lib/opticalLayout/devMock.ts")
      .filter((f) => /\bstation_profile\s*:/.test(stripComments(readFileSync(f, "utf8"))))
      .map((f) => relative(SRC, f).replace(/\\/g, "/"));
    assert.deepEqual(writers, ["hooks/useStationProfile.ts"]);
  });
});

describe("接线：对话框", () => {
  const dlg = code("components/settings/StationProfileDialog.tsx");

  it("用 ConfirmDialog：危险色按钮、失败时留着并显示后端给的原因、按钮由 switchBlocker 决定", () => {
    assert.match(dlg, /<ConfirmDialog/);
    assert.match(dlg, /tone="danger"/);
    assert.match(dlg, /error=\{error\}/);
    assert.match(dlg, /disabled=\{blocker !== null\}/);
    assert.match(dlg, /const blocker = switchBlocker\(\{ pin, pinIsSet, busy \}\)/);
  });

  it("文案来自 confirmCopy，三种画像都渲染出来，每栏有「是什么」和「开关什么」", () => {
    assert.match(dlg, /const copy = confirmCopy\(from, to, source, \{ active, firstRun \}\)/);
    assert.match(dlg, /copy\.profiles\.map/);
    assert.match(dlg, /\{p\.blurb\}/);
    assert.match(dlg, /p\.lines\.map/);
    for (const field of ["copy.title", "copy.intro", "copy.risk", "copy.restart", "copy.envNote", "copy.confirmLabel"]) {
      assert.ok(dlg.includes(field), `对话框没渲染 ${field}`);
    }
  });

  it("PIN 输入框是 password 类型，Enter 也走同一个规则", () => {
    assert.match(dlg, /type="password"/);
    assert.match(dlg, /e\.key === "Enter" && blocker === null/);
  });

  it("PIN 只活在组件状态里：对话框关掉、或被拒之后清空，不写进任何 store / storage", () => {
    assert.match(dlg, /useState\(""\)/);
    assert.match(dlg, /if \(!open\) setPin\(""\)/);
    assert.match(dlg, /if \(error\) setPin\(""\)/);
    for (const banned of ["localStorage", "sessionStorage", "useUiStore", "setPinUnlocked", "document.cookie", "window.name"]) {
      assert.ok(!dlg.includes(banned), `对话框不该碰 ${banned}`);
    }
  });

  it("机器还没设 PIN 时直接说明，并给一个去设置的入口", () => {
    assert.match(dlg, /pinIsSet === false/);
    assert.match(dlg, /<Link to="\/settings\/admin"/);
    assert.match(dlg, /queryKey: \["admin", "pin-status"\]/, "和高级页的 PIN 门共用同一个查询键");
  });

  it("首次使用把流程串起来：没 PIN 时就地设（高级页那一份表单，不是另写一份），设好之前不出现 PIN 输入框", () => {
    assert.match(dlg, /import \{ SetPinForm \} from "@\/components\/admin\/PinGate";/);
    assert.match(dlg, /\{pinIsSet === false \? \(/);
    const noPin = dlg.slice(dlg.indexOf("{pinIsSet === false ? ("), dlg.indexOf(") : (", dlg.indexOf("{pinIsSet === false ? (")));
    assert.match(noPin, /<SetPinForm onDone=\{\(\) => setPin\(""\)\} \/>/);
    assert.ok(!noPin.includes('type="password"'), "没 PIN 的分支里不该有确认用的 PIN 输入框");
    // 那份表单就是高级页 PinGate 里的同一个组件：同一个接口、同一条至少 4 位的规则
    const gate = readFileSync(join(SRC, "components/admin/PinGate.tsx"), "utf8").replace(/\r\n/g, "\n");
    assert.match(gate, /export function SetPinForm\(\{ onDone \}: \{ onDone: \(\) => void \}\)/);
    assert.match(gate, /api\.POST\("\/api\/admin\/set-pin"/);
    assert.match(gate, /queryClient\.invalidateQueries\(\{ queryKey: \["admin", "pin-status"\] \}\)/, "设好之后刷新 PIN 状态，对话框才会换出输入框");
  });
});

describe("接线：PIN 说明里列出了站点画像", () => {
  const lf = (rel: string) => readFileSync(join(SRC, rel), "utf8").replace(/\r\n/g, "\n");

  it("设置页顶上的 PIN 说明卡（AdminPinGuideCard 里面，不是整个文件）把它算进受保护的设置", () => {
    // 整个文件里到处有「站点画像」（卡片标题等），只查文件会在说明卡漏写时给出假绿
    // 说明卡里「站点画像」不止出现一次（后面的条目也提到它），所以钉的是受保护设置的**那份清单**：
    // 紧跟在「修针方案覆写」后面
    const guide = topLevelFunction(lf("pages/SettingsPage.tsx"), "AdminPinGuideCard");
    assert.match(guide, /修针方案覆写、站点画像/, "AdminPinGuideCard 的中文清单里没有站点画像");
    assert.match(guide, /tip-conditioning overrides, the station profile/, "AdminPinGuideCard 的英文清单里没有 station profile");
  });

  it("高级页的 PIN 门也一样", () => {
    const gate = lf("components/admin/PinGate.tsx");
    assert.match(gate, /修针方案覆写和站点画像/, "PinGate 的中文清单里没有站点画像");
    assert.match(gate, /tip-conditioning overrides and the station profile/, "PinGate 的英文清单里没有 station profile");
  });
});
