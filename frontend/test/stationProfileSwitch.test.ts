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
  profileEffects,
  profileLabel,
  switchBlocker,
  switchRisk,
} from "../src/lib/stationProfile.ts";

describe("profileEffects：两种画像各意味着什么", () => {
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

  it("两边条数一样多、没有空句", () => {
    const [a, b] = [profileEffects("stm"), profileEffects("optical")];
    assert.equal(a.length, b.length);
    for (const line of [...a, ...b]) assert.ok(line.trim().length > 10, JSON.stringify(line));
  });
});

describe("switchRisk：选错的后果按方向说", () => {
  it("STM → optical：不拨 Nanonis，撞针看门狗和电流监控都不起", () => {
    const t = switchRisk("optical");
    assert.ok(t.includes("其实是 STM 站"));
    for (const needle of ["不会连接 Nanonis", "撞针看门狗", "电流监控"]) assert.ok(t.includes(needle), needle);
  });

  it("optical → stm：串口自动探测恢复，*IDN? 和 Modbus 帧，操作员服务与撤销监听不再安装", () => {
    const t = switchRisk("stm");
    assert.ok(t.includes("其实是光学站"));
    for (const needle of ["串口自动探测会恢复", "*IDN?", "Modbus", "撤销监听"]) assert.ok(t.includes(needle), needle);
  });

  it("两个方向说的不是同一句", () => {
    assert.notEqual(switchRisk("stm"), switchRisk("optical"));
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

  it("两种画像都列出来，顺序固定（stm、optical），只有要改成的那个 selected", () => {
    assert.deepEqual(c.profiles.map((p) => p.profile), ["stm", "optical"]);
    assert.deepEqual(c.profiles.map((p) => p.selected), [false, true]);
    assert.deepEqual(confirmCopy("optical", "stm").profiles.map((p) => p.selected), [true, false]);
    for (const p of c.profiles) {
      assert.equal(p.label, profileLabel(p.profile));
      assert.deepEqual(p.lines, profileEffects(p.profile));
    }
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

  it("stm / optical 是身份字符串，不经过 tr()", () => {
    for (const p of c.profiles) assert.ok(p.profile === "stm" || p.profile === "optical");
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

  it("文案来自 confirmCopy，两种画像都渲染出来", () => {
    assert.match(dlg, /const copy = confirmCopy\(from, to, source\)/);
    assert.match(dlg, /copy\.profiles\.map/);
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
