// ════════════════════════════════════════════════════════════════════════════
// The manual optical-state panel — the pure logic behind it (src/lib/opticalState.ts).
//
//     cd frontend && npm run test:unit
//
// A slot records something no software can read back: which λ/2 is in the beam, the
// manual polarizer angle, the objective, the laser-area-safe signature. Only the
// operator writes it; each value says who set it, when, from where and until when it
// holds. What is tested here:
//
//   · EXPIRY — the panel shows an out-of-date value in red. The service's own `valid`
//     flag wins when it says no, and the local clock may only make a value expire EARLIER
//     than the last poll said, never later: a signature must not look valid on screen
//     after its time is up just because the next poll has not arrived;
//   · what counts as "not signed" (a flag that is not exactly `true`);
//   · a value is checked against its slot (unlisted option, out of range, unticked
//     signature) before any request, and every write names the operator.
//
// `tr()` returns Chinese outside a browser; assertions pin numbers and structure.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  LASER_AREA_SAFE,
  isOpticalProfile,
  buildSlotClearBody,
  buildSlotPutBody,
  displayValue,
  formatRemaining,
  hasAnyValue,
  isExpired,
  laserSafeMeaning,
  normalizeValue,
  optionBand,
  parseHistory,
  parseOpticalState,
  remainingMs,
  setByLine,
  slotRangeHint,
  slotStatus,
  summarize,
  validateSlotInput,
  type OpticalSlot,
  type SlotValue,
} from "../src/lib/opticalState.ts";

const T0 = Date.parse("2026-11-03T10:00:00+00:00");
const iso = (ms: number) => new Date(ms).toISOString();

function slot(over: Partial<OpticalSlot> = {}): OpticalSlot {
  return { id: "s", label: "S", kind: "choice", options: [], unit: "", min: null, max: null, valid_for_s: null, description: "", ...over };
}

function value(over: Partial<SlotValue> = {}): SlotValue {
  return { value: "x", set_by: "Ana", set_at: iso(T0), source: "web", valid_until: null, valid: true, note: "", ...over };
}

describe("which kind of station this is", () => {
  it("only the optical profile is optical; anything unreadable is an STM station", () => {
    assert.equal(isOpticalProfile("optical"), true);
    assert.equal(isOpticalProfile(" Optical "), true, "case and padding do not matter");
    for (const v of ["stm", "", "optic", "opticals", "witec", null, undefined, 1, true, {}, []]) {
      assert.equal(isOpticalProfile(v), false, `${JSON.stringify(v)} must not turn an STM station optical`);
    }
  });
});

describe("parsing the optical state", () => {
  const snapshot = {
    slots: [
      { id: "excitation_waveplate", label: "λ/2", kind: "choice", options: [{ id: "hwp_532", label: "λ/2 @532 nm", meta: { valid_nm: [500, 580] } }, { id: "none" }, { label: "no id" }], valid_for_s: null },
      { id: "incident_polarizer_deg", label: "Polarizer", kind: "number", unit: "deg", min: 0, max: 360 },
      { id: LASER_AREA_SAFE, label: "Laser area safe", kind: "flag", valid_for_s: 28800, description: "signed for 8 h" },
      { label: "no id" },
      null,
    ],
    values: {
      excitation_waveplate: { value: "hwp_532", set_by: "Ana", set_at: iso(T0), source: "web", valid_until: null, valid: true, note: "" },
      incident_polarizer_deg: { value: null, valid: false }, // snapshot-style "not set"
      [LASER_AREA_SAFE]: { value: true, set_by: "Ana", set_at: iso(T0), source: "prompt:op-0001", valid_until: iso(T0 + 8 * 3600_000), valid: true },
    },
  };

  it("keeps the slots in the service's order and the set values by id", () => {
    const s = parseOpticalState(snapshot);
    assert.deepEqual(s.slots.map((x) => x.id), ["excitation_waveplate", "incident_polarizer_deg", LASER_AREA_SAFE]);
    assert.deepEqual(Object.keys(s.values).sort(), ["excitation_waveplate", LASER_AREA_SAFE]);
    assert.equal(hasAnyValue(s), true);
  });

  it("options keep their metadata; an option without an id is dropped", () => {
    const wp = parseOpticalState(snapshot).slots[0]!;
    assert.deepEqual(wp.options.map((o) => o.id), ["hwp_532", "none"]);
    assert.equal(optionBand(wp.options[0]!), "500–580 nm");
    assert.equal(optionBand(wp.options[1]!), "");
    assert.equal(optionBand({ id: "x", label: "x", meta: { valid_nm: ["a", "b"] } }), "");
  });

  it("a value that is absent, null or snapshot-style unset is not set", () => {
    assert.equal(normalizeValue({ value: null, valid: false }), null);
    assert.equal(normalizeValue({}), null);
    assert.equal(normalizeValue(null), null);
    assert.equal(normalizeValue({ value: 0 })?.value, 0, "zero is a value");
    assert.equal(normalizeValue({ value: false })?.value, false, "so is false");
  });

  it("garbage parses to an empty snapshot", () => {
    for (const bad of [null, undefined, "x", 3, [], { slots: "no", values: 3 }]) {
      const s = parseOpticalState(bad);
      assert.deepEqual(s, { slots: [], values: {} });
    }
  });

  it("the service's own history wrapper is {records: [...]}, oldest first", () => {
    const records = [
      { slot: "a", value: "1", set_by: "Ana", set_at: iso(T0), source: "web", valid_until: null, note: "" },
      { slot: "a", value: "2", set_by: "Ben", set_at: iso(T0 + 60_000), source: "launcher", valid_until: null, note: "" },
    ];
    assert.deepEqual(parseHistory({ records }).map((e) => e.value), ["2", "1"], "shown newest first");
  });

  it("history is newest first, whatever wrapper the list comes in", () => {
    const rows = [
      { slot: "a", value: "1", set_by: "Ana", set_at: iso(T0), source: "web", note: "" },
      { slot: "b", value: null, set_by: "Ben", set_at: iso(T0 + 60_000), source: "launcher", note: "cleared" },
      { slot: "", value: "ignored" },
      { slot: "c", value: 3, set_by: "Ana", set_at: iso(T0 + 30_000) },
    ];
    for (const wrap of [{ history: rows }, { entries: rows }, rows]) {
      assert.deepEqual(parseHistory(wrap).map((e) => e.slot), ["b", "c", "a"]);
    }
    assert.deepEqual(parseHistory(null), []);
    assert.equal(parseHistory({ history: rows })[0]!.value, null, "a cleared slot keeps its null");
  });
});

describe("expiry", () => {
  const until = (ms: number) => value({ valid_until: iso(ms) });

  it("the service's verdict wins: valid:false is expired even if the dates look fine", () => {
    assert.equal(isExpired(value({ valid: false, valid_until: iso(T0 + 3600_000) }), T0), true);
    assert.equal(isExpired(value({ valid: false }), T0), true);
  });

  it("the clock can only expire a value EARLIER than the last poll said", () => {
    const polled = until(T0 + 1000); // the service said valid, for one more second
    assert.equal(isExpired(polled, T0), false);
    assert.equal(isExpired(polled, T0 + 999), false);
    assert.equal(isExpired(polled, T0 + 1000), true, "exactly at its time it is out of date");
    assert.equal(isExpired(polled, T0 + 5000), true, "between two polls the screen must not keep it green");
  });

  it("no deadline means valid until changed; nothing set is not 'expired'", () => {
    assert.equal(isExpired(value(), T0 + 1e12), false);
    assert.equal(isExpired(null, T0), false);
    assert.equal(isExpired(undefined, T0), false);
    assert.equal(isExpired(value({ valid_until: "not a date" }), T0), false, "an unreadable date is not an expiry the clock can claim");
  });

  it("time left, negative once it has run out", () => {
    assert.equal(remainingMs(until(T0 + 90_000), T0), 90_000);
    assert.equal(remainingMs(until(T0 - 5_000), T0), -5_000);
    assert.equal(remainingMs(value(), T0), null);
    assert.equal(remainingMs(undefined, T0), null);
  });

  it("how the remaining time reads", () => {
    assert.equal(formatRemaining(null), "直到被改动");
    assert.equal(formatRemaining(7 * 3600_000 + 12 * 60_000), "还剩 7 小时 12 分");
    assert.equal(formatRemaining(45_000), "还剩 45 秒");
    assert.equal(formatRemaining(-5 * 60_000), "已过期 5 分");
    assert.equal(formatRemaining(0), "已过期 0 秒");
  });
});

describe("the status of a slot", () => {
  it("unset, valid, expired", () => {
    const choice = slot();
    assert.equal(slotStatus(choice, undefined, T0), "unset");
    assert.equal(slotStatus(choice, null, T0), "unset");
    assert.equal(slotStatus(choice, value(), T0), "valid");
    assert.equal(slotStatus(choice, value({ valid_until: iso(T0 - 1) }), T0), "expired");
    assert.equal(slotStatus(choice, value({ valid: false }), T0), "expired");
  });

  it("a flag that is not exactly true is 'not signed', never 'valid'", () => {
    const flag = slot({ id: LASER_AREA_SAFE, kind: "flag" });
    assert.equal(slotStatus(flag, value({ value: true }), T0), "valid");
    assert.equal(slotStatus(flag, value({ value: false }), T0), "unset");
    assert.equal(slotStatus(flag, value({ value: "yes" }), T0), "unset", "only the boolean true is a signature");
    assert.equal(slotStatus(flag, value({ value: true, valid_until: iso(T0 - 1) }), T0), "expired");
  });

  it("the header counts what needs attention", () => {
    const snap = {
      slots: [slot({ id: "a" }), slot({ id: "b" }), slot({ id: "c", kind: "flag" }), slot({ id: "d" })],
      values: {
        a: value(),
        b: value({ valid_until: iso(T0 - 1000) }),
        c: value({ value: true, valid_until: iso(T0 + 1000) }),
      },
    };
    assert.deepEqual(summarize(snap, T0), { unset: 1, valid: 2, expired: 1 });
    assert.deepEqual(summarize(snap, T0 + 2000), { unset: 1, valid: 1, expired: 2 });
  });
});

describe("showing a value", () => {
  it("a choice reads as its label, a number with its unit, a flag as signed", () => {
    const choice = slot({ options: [{ id: "hwp_532", label: "λ/2 @532 nm", meta: {} }] });
    assert.equal(displayValue(choice, value({ value: "hwp_532" })), "λ/2 @532 nm");
    assert.equal(displayValue(choice, value({ value: "gone_option" })), "gone_option", "an option the config no longer lists still shows what was recorded");
    assert.equal(displayValue(slot({ kind: "number", unit: "deg" }), value({ value: 45 })), "45 deg");
    assert.equal(displayValue(slot({ kind: "number", unit: "" }), value({ value: 0.1 + 0.2 })), "0.3", "no float noise on screen");
    assert.equal(displayValue(slot({ kind: "flag" }), value({ value: true })), "已签署");
    assert.equal(displayValue(slot({ kind: "text" }), value({ value: "Semrock 532 LP" })), "Semrock 532 LP");
    assert.equal(displayValue(choice, null), "—");
  });

  it("who set it, when and from where", () => {
    const line = setByLine(value({ set_by: "Ana", source: "prompt:op-0007" }));
    assert.ok(line.includes("Ana") && line.includes("op-0007"));
    assert.ok(setByLine(value({ source: "launcher" })).includes("启动器"));
  });

  it("signing the laser-area flag is explained in words that name what is being asserted", () => {
    const lines = laserSafeMeaning();
    assert.equal(lines.length, 3);
    const text = lines.join(" ");
    for (const word of ["护目镜", "光路", "8 小时", "撤销"]) assert.ok(text.includes(word), word);
  });
});

describe("writing a slot", () => {
  const waveplate = slot({ options: [{ id: "hwp_532", label: "532", meta: {} }, { id: "none", label: "none", meta: {} }] });

  it("a choice must be one of the slot's options", () => {
    assert.deepEqual(validateSlotInput(waveplate, { choice: "hwp_532" }), { ok: true, value: "hwp_532" });
    assert.equal(validateSlotInput(waveplate, { choice: "" }).ok, false);
    assert.equal(validateSlotInput(waveplate, {}).ok, false);
    assert.equal(validateSlotInput(waveplate, { choice: "hwp_355" }).ok, false);
  });

  it("a number is held to the slot's range", () => {
    const deg = slot({ kind: "number", unit: "deg", min: 0, max: 360 });
    assert.deepEqual(validateSlotInput(deg, { text: "45" }), { ok: true, value: 45 });
    assert.deepEqual(validateSlotInput(deg, { text: "360" }), { ok: true, value: 360 });
    assert.equal(validateSlotInput(deg, { text: "361" }).ok, false);
    assert.equal(validateSlotInput(deg, { text: "-1" }).ok, false);
    assert.equal(validateSlotInput(deg, { text: "abc" }).ok, false);
    assert.equal(slotRangeHint(deg).includes("360 deg"), true);
  });

  it("a signature needs the box ticked, and only ever sends true", () => {
    const flag = slot({ id: LASER_AREA_SAFE, kind: "flag" });
    assert.equal(validateSlotInput(flag, {}).ok, false);
    assert.equal(validateSlotInput(flag, { flag: false }).ok, false);
    assert.deepEqual(validateSlotInput(flag, { flag: true }), { ok: true, value: true });
  });

  it("text is trimmed, required and capped at what the service keeps", () => {
    const text = slot({ kind: "text" });
    assert.deepEqual(validateSlotInput(text, { text: "  Semrock LP  " }), { ok: true, value: "Semrock LP" });
    assert.equal(validateSlotInput(text, { text: "  " }).ok, false);
    assert.equal(validateSlotInput(text, { text: "x".repeat(501) }).ok, false);
    assert.equal(validateSlotInput(text, { text: "x".repeat(500) }).ok, true);
  });

  it("every write names the operator", () => {
    for (const by of ["", "  ", null, undefined]) {
      assert.equal(buildSlotPutBody(by, "hwp_532").ok, false);
      assert.equal(buildSlotClearBody(by).ok, false);
    }
    assert.deepEqual(buildSlotPutBody(" Ana  Ruiz ", 45), { ok: true, value: { by: "Ana Ruiz", source: "web", value: 45 } });
    assert.deepEqual(buildSlotPutBody("Ana", true, " new plate "), { ok: true, value: { by: "Ana", source: "web", value: true, note: "new plate" } });
    assert.deepEqual(buildSlotClearBody("Ana"), { ok: true, value: { by: "Ana", source: "web" } });
  });
});
