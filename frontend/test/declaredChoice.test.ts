// 设置页「必须由人声明」的两个下拉项（退针方向 retract_motor_dir、压电伸长符号 z_extend_sign）：
// 没声明时显示禁用的占位、不显示默认值；选哪个都会保存；已声明照常显示；不给「撤销声明」。
//
//     cd frontend && npm run test:unit

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { choiceToSave, declaredChoiceView, UNDECLARED_VALUE } from "../src/lib/declaredChoice.ts";

// 与 SettingsPage.tsx 的 RETRACT_DIR_CHOICES / Z_EXTEND_SIGN_CHOICES 同值
const RETRACT = [
  { value: "z+", label: "Z+ (Nanonis 标准: 远离样品)" },
  { value: "z-", label: "Z− (反向装置)" },
];
const ZSIGN = [
  { value: "+1", label: "伸长时 Z 增大 (+1)" },
  { value: "-1", label: "伸长时 Z 减小 (−1)" },
];

describe("必须由人声明的下拉项", () => {
  it("没声明：当前值是禁用的占位「（未声明，请选择）」，不是 Z+；真选项列在下面", () => {
    const v = declaredChoiceView(undefined, RETRACT);
    assert.equal(v.declared, false);
    assert.equal(v.value, UNDECLARED_VALUE);
    assert.notEqual(v.value, "z+", "不再把出厂默认当成当前值显示");
    assert.deepEqual(v.options[0], { value: UNDECLARED_VALUE, label: "（未声明，请选择）", disabled: true });
    assert.deepEqual(v.options.slice(1).map((o) => o.value), ["z+", "z-"]);
    assert.ok(v.options.slice(1).every((o) => !o.disabled));
    // 压电伸长符号同理：不显示 +1
    const z = declaredChoiceView(undefined, ZSIGN);
    assert.equal(z.value, UNDECLARED_VALUE);
    assert.equal(declaredChoiceView("", ZSIGN).declared, false, "空串 = 没声明");
  });

  it("没声明时选 z+：当前值是占位，所以选 z+ 是一次真改动（下拉框会发 change），而且会保存 z+", () => {
    const v = declaredChoiceView(undefined, RETRACT);
    assert.notEqual(v.value, "z+");
    assert.equal(choiceToSave("z+", RETRACT), "z+");
    assert.equal(choiceToSave("+1", ZSIGN), "+1");
    assert.equal(choiceToSave(UNDECLARED_VALUE, RETRACT), null, "占位不会被保存");
  });

  it("已声明 z−：照常显示 z−，只列真选项 —— 没有「撤销声明」的项", () => {
    const v = declaredChoiceView("z-", RETRACT);
    assert.equal(v.declared, true);
    assert.equal(v.value, "z-");
    assert.deepEqual(v.options.map((o) => o.value), ["z+", "z-"]);
    assert.ok(!v.options.some((o) => o.value === UNDECLARED_VALUE));
    assert.equal(declaredChoiceView("-1", ZSIGN).value, "-1");
  });

  it("存着认不出的值：照实显示出来（禁用），请人重新选；不冒充成某个真选项", () => {
    const v = declaredChoiceView("Z+", RETRACT);
    assert.equal(v.declared, false);
    assert.equal(v.value, UNDECLARED_VALUE);
    assert.match(v.options[0]!.label, /认不出的值「Z\+」/);
    assert.equal(v.options[0]!.disabled, true);
    assert.equal(choiceToSave("Z+", RETRACT), null);
  });
});
