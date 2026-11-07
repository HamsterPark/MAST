// ════════════════════════════════════════════════════════════════════════════
// Structured operator prompts — the pure logic behind the Operator Requests board and
// the app-wide alert (src/lib/operatorPrompts.ts).
//
//     cd frontend && npm run test:unit
//
// An agent posts a prompt (confirm / choice / number / text / attest) and waits for a
// person; the person answers on this page or in the launcher's popup. What is tested
// here is what can go wrong between the service's JSON and a request body:
//
//   · the request body per kind, and that a bad number / unlisted option / missing name
//     is refused BEFORE any request is made;
//   · which prompts count as new (toast, beep, notification) and which are the page's
//     starting point (a reload must not re-announce what is already open);
//   · the nav badge, which used to be a 60 s poll of the free-text board only;
//   · the small decisions around it: ordering, the beep per severity, when a desktop
//     notification is worth raising, how a failed call reads.
//
// Outside a browser `tr()` returns the Chinese text; assertions therefore pin numbers,
// ids and structure, and only quote Chinese where the wording itself is the behaviour.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  NO_PROMPT_FEED,
  OPERATOR_NAME_KEY,
  OPERATOR_SOURCE,
  answerSummary,
  beepPlan,
  buildAnswerBody,
  buildDismissBody,
  checkRange,
  cleanOperatorName,
  deadlineLeftMs,
  errorDetail,
  failureText,
  formatDuration,
  freshPrompts,
  hasSafetyPrompt,
  isAlreadyHandled,
  metaLine,
  normalizePrompt,
  notificationText,
  openPromptFeed,
  openPrompts,
  parseNumberInput,
  parsePrompts,
  pollDelayMs,
  promptFields,
  rangeHint,
  readOperatorName,
  rememberSeen,
  shouldDesktopNotify,
  sortForAnswering,
  sortHistory,
  titleWithCount,
  toastLifetimeMs,
  waitingFor,
  wishlistBadge,
  wishlistBadgeLabel,
  writeOperatorName,
  type OperatorPrompt,
} from "../src/lib/operatorPrompts.ts";

/** A prompt the way the service sends it; overrides win. */
function raw(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "op-0001",
    kind: "confirm",
    title: "Change the waveplate",
    message: "Put the 532 nm half-wave plate in the excitation path.",
    severity: "action",
    status: "open",
    created_at: "2026-11-03T10:00:00+00:00",
    answer: null,
    ...over,
  };
}

function P(over: Record<string, unknown> = {}): OperatorPrompt {
  const p = normalizePrompt(raw(over));
  assert.ok(p, "fixture must normalise");
  return p;
}

describe("parsing what the service sends", () => {
  it("normalises every field and defaults the missing ones", () => {
    const p = P({
      kind: "number",
      number_min: "0",
      number_max: 20,
      unit: "mW",
      attest_slots: ["a", "", "b"],
      job_id: "j-1",
      timeout_s: 300,
      options: [{ id: "x", label: "X" }, { id: "y" }, { label: "no id" }, "junk"],
    });
    assert.equal(p.number_min, 0);
    assert.equal(p.number_max, 20);
    assert.deepEqual(p.attest_slots, ["a", "b"]);
    assert.deepEqual(p.options, [{ id: "x", label: "X" }, { id: "y", label: "y" }]);
    assert.equal(p.job_id, "j-1");
    assert.equal(p.skill, null);
    assert.equal(p.timeout_s, 300);
  });

  it("an unknown severity must not hide a prompt: it becomes an ordinary action", () => {
    assert.equal(P({ severity: "catastrophic" }).severity, "action");
    assert.equal(P({ severity: undefined }).severity, "action");
  });

  it("a number bound that is not a finite number is no bound", () => {
    const p = P({ kind: "number", number_min: null, number_max: Infinity });
    assert.equal(p.number_min, null);
    assert.equal(p.number_max, null);
  });

  it("an unknown kind is kept (the card then says to answer it elsewhere) instead of vanishing", () => {
    assert.equal(P({ kind: "telepathy" }).kind, "telepathy");
    assert.equal(promptFields(P({ kind: "telepathy" })).mode, "unsupported");
  });

  it("rows without an id are dropped; seq is optional", () => {
    const { prompts, seq } = parsePrompts({ prompts: [raw(), { kind: "confirm" }, null, "x", raw({ id: "  " })], seq: 9 });
    assert.deepEqual(prompts.map((p) => p.id), ["op-0001"]);
    assert.equal(seq, 9);
    assert.equal(parsePrompts({ prompts: [] }).seq, null);
    assert.deepEqual(parsePrompts(null), { prompts: [], seq: null, persistError: null });
    assert.equal(parsePrompts([raw()]).prompts.length, 1); // a bare list is accepted too
  });

  it("the service says when it could not save its prompt list", () => {
    assert.equal(parsePrompts({ prompts: [], seq: 1, persist_error: "disk full" }).persistError, "disk full");
    assert.equal(parsePrompts({ prompts: [], seq: 1, persist_error: null }).persistError, null);
    assert.equal(parsePrompts({ prompts: [], persistent: true, open_count: 0 }).persistError, null);
    assert.equal(parsePrompts([raw()]).persistError, null);
  });

  it("the answer of a closed prompt comes along", () => {
    const p = P({ status: "answered", answer: { prompt_id: "op-0001", status: "answered", choice: "yes", answered_by: "Ana", source: "launcher", answered_at: "2026-11-03T10:05:00+00:00" } });
    assert.equal(p.answer?.answered_by, "Ana");
    assert.equal(p.answer?.source, "launcher");
    assert.equal(P({ answer: { nonsense: true } }).answer, null);
  });
});

describe("what is open, and in what order", () => {
  const list = [
    P({ id: "old-info", severity: "info", created_at: "2026-11-03T09:00:00+00:00" }),
    P({ id: "new-action", created_at: "2026-11-03T10:05:00+00:00" }),
    P({ id: "old-action", created_at: "2026-11-03T10:00:00+00:00" }),
    P({ id: "late-safety", severity: "safety", created_at: "2026-11-03T10:30:00+00:00" }),
    P({ id: "closed", status: "answered", answer: { status: "answered", answered_at: "2026-11-03T11:00:00+00:00" } }),
  ];

  it("safety first, then the one that has waited longest", () => {
    assert.deepEqual(sortForAnswering(openPrompts(list)).map((p) => p.id), ["late-safety", "old-action", "new-action", "old-info"]);
  });

  it("only open prompts are open", () => {
    assert.equal(openPrompts(list).length, 4);
    assert.equal(hasSafetyPrompt(list), true);
    assert.equal(hasSafetyPrompt(list.filter((p) => p.id !== "late-safety")), false);
    assert.equal(hasSafetyPrompt([P({ id: "s", severity: "safety", status: "answered" })]), false, "a handled safety prompt is not an alarm");
  });

  it("history is the closed ones, most recently handled first", () => {
    const h = sortHistory([
      P({ id: "a", status: "answered", answer: { status: "answered", answered_at: "2026-11-03T10:00:00+00:00" } }),
      P({ id: "b", status: "dismissed", answer: { status: "dismissed", answered_at: "2026-11-03T12:00:00+00:00" } }),
      P({ id: "c", status: "timeout", created_at: "2026-11-03T11:00:00+00:00", answer: null }),
      P({ id: "still-open" }),
    ]);
    assert.deepEqual(h.map((p) => p.id), ["b", "c", "a"]);
  });
});

describe("which prompts are new — toast, beep, desktop notification", () => {
  const a = P({ id: "a" });
  const b = P({ id: "b", severity: "safety" });

  it("the page's starting point is not news; an id that appears afterwards is, once", () => {
    const seen = rememberSeen(new Set(), [a]); // what was open at page load
    assert.deepEqual(freshPrompts(seen, [a]), []);
    assert.deepEqual(freshPrompts(seen, [a, b]).map((p) => p.id), ["b"]);
    const seen2 = rememberSeen(seen, [a, b]);
    assert.deepEqual(freshPrompts(seen2, [a, b]), [], "announced once, not on every poll");
  });

  it("a prompt answered elsewhere and gone from the list is not announced again", () => {
    const seen = rememberSeen(new Set(), [a, b]);
    assert.deepEqual(freshPrompts(seen, [a]), []);
    assert.deepEqual(freshPrompts(seen, []), []);
  });

  it("several newcomers come most severe first (the beep takes the first one's pattern)", () => {
    const c = P({ id: "c", severity: "info" });
    assert.deepEqual(freshPrompts(new Set(), [c, a, b]).map((p) => p.id), ["b", "a", "c"]);
  });

  it("a closed prompt is never fresh", () => {
    assert.deepEqual(freshPrompts(new Set(), [P({ id: "z", status: "answered" })]), []);
  });

  it("the seen set is bounded, dropping the oldest", () => {
    let seen = new Set<string>();
    for (let i = 0; i < 12; i++) seen = rememberSeen(seen, [P({ id: `p${i}` })], 5);
    assert.equal(seen.size, 5);
    assert.deepEqual([...seen], ["p7", "p8", "p9", "p10", "p11"]);
  });
});

describe("the nav badge", () => {
  it("counts the structured prompts and the free-text agent requests together", () => {
    assert.equal(wishlistBadge(2, 3), 5);
    assert.equal(wishlistBadge(0, 0), 0);
    assert.equal(wishlistBadge(1, 0), 1, "a lone prompt shows, whatever the slow board says");
    assert.equal(wishlistBadge(0, 4), 4, "the old badge keeps working");
  });

  it("garbage counts as zero", () => {
    assert.equal(wishlistBadge(Number.NaN, 2), 2);
    assert.equal(wishlistBadge(-3, 1), 1);
    assert.equal(wishlistBadge(2.9, 0), 2);
  });

  it("the label carries the total and names a safety prompt", () => {
    assert.ok(wishlistBadgeLabel(2, 1, false).includes("3"));
    assert.ok(wishlistBadgeLabel(2, 1, true).includes("安全"));
    assert.ok(!wishlistBadgeLabel(2, 1, false).includes("安全"));
  });

  it("the tab title carries the count only while something waits", () => {
    assert.equal(titleWithCount("MAST", 0), "MAST");
    assert.equal(titleWithCount("MAST", 3), "(3) MAST");
    assert.equal(titleWithCount("MAST", 250), "(99+) MAST");
  });
});

describe("the feed the shell reads — only an optical station has one", () => {
  const open = () => [
    P({ id: "a", severity: "info", created_at: "2026-11-03T10:00:00+00:00" }),
    P({ id: "s", severity: "safety", created_at: "2026-11-03T10:05:00+00:00" }),
    P({ id: "done", status: "answered", answer: { prompt_id: "done", status: "answered", answered_at: "2026-11-03T10:06:00+00:00" } }),
  ];
  const input = (over: Record<string, unknown> = {}) => ({
    optical: true,
    list: open(),
    loaded: true,
    errorStatus: undefined as unknown,
    persistError: null as string | null,
    ...over,
  });

  it("on an optical station it is the open prompts, safety first, with their counts", () => {
    const feed = openPromptFeed(input());
    assert.equal(feed.enabled, true);
    assert.deepEqual(feed.prompts.map((p) => p.id), ["s", "a"], "the closed one is not in it");
    assert.equal(feed.openCount, 2);
    assert.equal(feed.hasSafety, true);
    assert.equal(feed.loaded, true);
    assert.equal(feed.unsupported, false);
  });

  it("it is enabled but empty (and not loaded) before the first answer", () => {
    const feed = openPromptFeed(input({ list: undefined, loaded: false }));
    assert.deepEqual([feed.enabled, feed.openCount, feed.loaded, feed.hasSafety], [true, 0, false, false]);
  });

  it("a 404 means the service has no operator API; a save error is passed on", () => {
    assert.equal(openPromptFeed(input({ errorStatus: 404 })).unsupported, true);
    assert.equal(openPromptFeed(input({ errorStatus: 500 })).unsupported, false);
    assert.equal(openPromptFeed(input({ persistError: "disk full" })).persistError, "disk full");
  });

  it("on any other station it is NO_PROMPT_FEED, whatever an earlier answer left in the cache", () => {
    for (const over of [
      {},                                                   // a cached list with a safety prompt in it
      { loaded: true, persistError: "disk full" },
      { errorStatus: 404 },
      { list: undefined, loaded: false },
    ]) {
      const feed = openPromptFeed(input({ optical: false, ...over }));
      assert.equal(feed, NO_PROMPT_FEED, "the very same object, so no effect depending on it re-runs");
    }
  });

  it("NO_PROMPT_FEED is empty, switched off, and cannot be changed", () => {
    assert.deepEqual(
      { ...NO_PROMPT_FEED, prompts: [...NO_PROMPT_FEED.prompts] },
      { enabled: false, prompts: [], openCount: 0, hasSafety: false, loaded: false, unsupported: false, persistError: null },
    );
    assert.ok(Object.isFrozen(NO_PROMPT_FEED) && Object.isFrozen(NO_PROMPT_FEED.prompts));
    assert.throws(() => (NO_PROMPT_FEED.prompts as OperatorPrompt[]).push(P()), TypeError);
  });

  it("what the nav badge, the tab title and the alert host make of it is nothing at all", () => {
    const stm = openPromptFeed(input({ optical: false }));
    // the tab badge is the free-text requests alone, as before operator prompts existed
    assert.equal(wishlistBadge(stm.openCount, 3), 3);
    assert.equal(wishlistBadge(stm.openCount, 0), 0);
    assert.equal(stm.hasSafety, false, "so the badge is never the red one");
    // the tab title keeps its own text
    assert.equal(titleWithCount("MAST", stm.openCount), "MAST");
    // the alert host: never "loaded" (no baseline is taken), nothing fresh to announce, nothing to beep for
    assert.equal(stm.loaded, false);
    assert.deepEqual(freshPrompts(new Set(), stm.prompts), []);
    // ... while the same cache on an optical station would have shown 2 and announced both
    const optical = openPromptFeed(input());
    assert.equal(wishlistBadge(optical.openCount, 3), 5);
    assert.deepEqual(freshPrompts(new Set(), optical.prompts).map((p) => p.id), ["s", "a"]);
  });
});

describe("what to draw for each kind", () => {
  it("confirm / choice / number / text", () => {
    assert.deepEqual(promptFields(P()), { mode: "confirm" });
    const choice = promptFields(P({ kind: "choice", options: [{ id: "a", label: "A" }, { id: "b", label: "B" }] }));
    assert.deepEqual(choice, { mode: "options", options: [{ id: "a", label: "A" }, { id: "b", label: "B" }], send: "choice" });
    assert.deepEqual(promptFields(P({ kind: "number", number_min: 0, number_max: 20, unit: "mW" })), { mode: "number", min: 0, max: 20, unit: "mW" });
    assert.deepEqual(promptFields(P({ kind: "text" })), { mode: "text" });
  });

  it("an attestation with options or a unit is not a plain flag", () => {
    const withOptions = promptFields(P({ kind: "attest", attest_slots: ["objective"], options: [{ id: "10x" }, { id: "100x" }] }));
    assert.equal(withOptions.mode, "options");
    const withUnit = promptFields(P({ kind: "attest", attest_slots: ["deg"], unit: "deg", number_min: 0, number_max: 360 }));
    assert.deepEqual(withUnit, { mode: "number", min: 0, max: 360, unit: "deg" });
  });

  it("a plain attestation follows the slot's kind when the slot is known, else it is a signed flag", () => {
    const p = P({ kind: "attest", attest_slots: ["s"] });
    assert.deepEqual(promptFields(p), { mode: "flag" });
    assert.deepEqual(promptFields(p, { s: { kind: "flag" } }), { mode: "flag" });
    assert.deepEqual(promptFields(p, { s: { kind: "text" } }), { mode: "text" });
    assert.deepEqual(promptFields(p, { s: { kind: "number", min: 0, max: 100, unit: "%" } }), { mode: "number", min: 0, max: 100, unit: "%" });
    const c = promptFields(p, { s: { kind: "choice", options: [{ id: "hwp_532", label: "532" }, { id: "none" }] } });
    assert.deepEqual(c, { mode: "options", send: "value", options: [{ id: "hwp_532", label: "532" }, { id: "none", label: "none" }] });
  });
});

describe("the answer body, per kind", () => {
  it("confirm sends the clicked choice and nothing else", () => {
    const r = buildAnswerBody(P(), { choice: "yes" }, "  Ana   Ruiz ");
    assert.deepEqual(r, { ok: true, value: { by: "Ana Ruiz", source: OPERATOR_SOURCE, choice: "yes" } });
    assert.equal(OPERATOR_SOURCE, "web");
    const no = buildAnswerBody(P(), { choice: "no", note: " too dark " }, "A");
    assert.deepEqual(no, { ok: true, value: { by: "A", source: "web", choice: "no", note: "too dark" } });
    assert.equal(buildAnswerBody(P(), {}, "A").ok, false);
    assert.equal(buildAnswerBody(P(), { choice: "maybe" }, "A").ok, false);
  });

  it("the operator's name is required, and a blank one is refused before any request", () => {
    for (const by of ["", "   ", null, undefined]) {
      assert.equal(buildAnswerBody(P(), { choice: "yes" }, by).ok, false);
      assert.equal(buildDismissBody(by).ok, false);
    }
  });

  it("a choice sends the option id, and only a listed one", () => {
    const p = P({ kind: "choice", options: [{ id: "hwp_532" }, { id: "hwp_1064" }] });
    const ok = buildAnswerBody(p, { choice: "hwp_1064" }, "A");
    assert.deepEqual(ok, { ok: true, value: { by: "A", source: "web", choice: "hwp_1064" } });
    assert.equal(buildAnswerBody(p, { choice: "" }, "A").ok, false);
    assert.equal(buildAnswerBody(p, { choice: null }, "A").ok, false);
    assert.equal(buildAnswerBody(p, { choice: "hwp_355" }, "A").ok, false);
  });

  it("a number is parsed and held to the prompt's range before it is sent", () => {
    const p = P({ kind: "number", number_min: 0, number_max: 20, unit: "mW" });
    const ok = buildAnswerBody(p, { text: " 12.5 " }, "A");
    assert.deepEqual(ok, { ok: true, value: { by: "A", source: "web", value: 12.5 } });
    assert.equal((buildAnswerBody(p, { text: "12,5" }, "A") as { ok: true; value: { value: number } }).value.value, 12.5);
    assert.equal((buildAnswerBody(p, { text: "0" }, "A") as { ok: true; value: { value: number } }).value.value, 0, "the lower bound itself is fine");
    assert.equal((buildAnswerBody(p, { text: "2e1" }, "A") as { ok: true; value: { value: number } }).value.value, 20, "so is the upper bound");
    const high = buildAnswerBody(p, { text: "20.1" }, "A");
    assert.equal(high.ok, false);
    assert.ok(!high.ok && high.error.includes("20.1") && high.error.includes("20") && high.error.includes("mW"));
    const low = buildAnswerBody(p, { text: "-0.1" }, "A");
    assert.ok(!low.ok && low.error.includes("-0.1"));
  });

  it("anything that is not a finite number is refused", () => {
    const p = P({ kind: "number" });
    for (const text of ["", "   ", "abc", "nan", "NaN", "inf", "-inf", "Infinity", "1_0", "0x10", "1.2.3", "12 mW", "1e", "--1"]) {
      assert.equal(buildAnswerBody(p, { text }, "A").ok, false, `"${text}" must be refused`);
    }
    assert.equal((buildAnswerBody(p, { text: "-1e9" }, "A") as { ok: true; value: { value: number } }).value.value, -1e9, "unbounded: any finite number");
    assert.equal(parseNumberInput("−3.5").ok, true, "a typographic minus is a minus");
  });

  it("text is trimmed, required and capped", () => {
    const p = P({ kind: "text" });
    assert.deepEqual(buildAnswerBody(p, { text: "  Nikon 40x  " }, "A"), { ok: true, value: { by: "A", source: "web", value: "Nikon 40x" } });
    assert.equal(buildAnswerBody(p, { text: " \n " }, "A").ok, false);
    const long = buildAnswerBody(p, { text: "x".repeat(9000) }, "A");
    assert.ok(long.ok && typeof long.value.value === "string" && long.value.value.length === 4000);
  });

  it("attestations send what the service checks", () => {
    const options = P({ kind: "attest", attest_slots: ["objective"], options: [{ id: "10x" }, { id: "100x" }] });
    assert.deepEqual(buildAnswerBody(options, { choice: "100x" }, "A"), { ok: true, value: { by: "A", source: "web", choice: "100x" } });
    const unit = P({ kind: "attest", attest_slots: ["deg"], unit: "deg", number_min: 0, number_max: 360 });
    assert.deepEqual(buildAnswerBody(unit, { text: "45" }, "A"), { ok: true, value: { by: "A", source: "web", value: 45 } });
    assert.equal(buildAnswerBody(unit, { text: "361" }, "A").ok, false);
    const flag = P({ kind: "attest", attest_slots: ["laser_area_safe"] });
    assert.equal(buildAnswerBody(flag, { confirmed: false }, "A").ok, false, "a signature needs the box ticked");
    assert.deepEqual(buildAnswerBody(flag, { confirmed: true }, "A"), { ok: true, value: { by: "A", source: "web", value: true } });
  });

  it("an attestation on a choice slot sends the id as a value, not as a choice", () => {
    const p = P({ kind: "attest", attest_slots: ["excitation_waveplate"] });
    const slots = { excitation_waveplate: { kind: "choice", options: [{ id: "hwp_532" }, { id: "none" }] } };
    const r = buildAnswerBody(p, { choice: "hwp_532" }, "A", slots);
    assert.deepEqual(r, { ok: true, value: { by: "A", source: "web", value: "hwp_532" } });
    assert.equal(buildAnswerBody(p, { choice: "hwp_355" }, "A", slots).ok, false);
  });

  it("an unknown kind is never answered blind", () => {
    assert.equal(buildAnswerBody(P({ kind: "telepathy" }), { choice: "yes" }, "A").ok, false);
  });

  it("the note is trimmed and left out when empty", () => {
    const r = buildAnswerBody(P(), { choice: "yes", note: "   " }, "A");
    assert.ok(r.ok && !("note" in r.value));
  });

  it("a dismissal carries the name, where it came from and, if given, the reason", () => {
    // the service records "web" when source is left out; the page says so itself
    assert.deepEqual(buildDismissBody("A"), { ok: true, value: { by: "A", source: "web" } });
    assert.deepEqual(buildDismissBody("A", " no goggles here "), { ok: true, value: { by: "A", source: "web", reason: "no goggles here" } });
  });
});

describe("number input and ranges", () => {
  it("parses ordinary notations", () => {
    for (const [text, v] of [["12", 12], ["-0.5", -0.5], [".5", 0.5], ["5.", 5], ["+3", 3], ["1e-3", 0.001], ["12,5", 12.5], [" 7 ", 7]] as const) {
      const r = parseNumberInput(text);
      assert.ok(r.ok && r.value === v, `${text} → ${v}`);
    }
    assert.equal(parseNumberInput("1,234,5").ok, false, "two commas is not a number");
  });

  it("a range check is inclusive at both ends and says which bound was crossed", () => {
    assert.equal(checkRange(0, 0, 20).ok, true);
    assert.equal(checkRange(20, 0, 20).ok, true);
    const hi = checkRange(20.5, 0, 20, "mW");
    assert.ok(!hi.ok && hi.error.includes("上限") && hi.error.includes("20.5"));
    const lo = checkRange(-1, 0, 20, "mW");
    assert.ok(!lo.ok && lo.error.includes("下限"));
    assert.equal(checkRange(1e9, null, null).ok, true);
  });

  it("the hint under the box names bounds and unit", () => {
    assert.ok(rangeHint(0, 20, "mW").includes("0") && rangeHint(0, 20, "mW").includes("20 mW"));
    assert.ok(rangeHint(0.5, null, "").includes("0.5"));
    assert.ok(rangeHint(null, 360, "deg").includes("360 deg"));
    assert.ok(rangeHint(null, null, "deg").includes("deg"));
    assert.equal(rangeHint(null, null, ""), "");
  });

  it("names are tidied", () => {
    assert.equal(cleanOperatorName("  Ana \t Ruiz  "), "Ana Ruiz");
    assert.equal(cleanOperatorName("x".repeat(200)).length, 80);
    assert.equal(cleanOperatorName(null), "");
  });
});

describe("words for a closed prompt", () => {
  const closed = (over: Record<string, unknown>, answer: Record<string, unknown>) =>
    P({ status: String(answer.status), answer: { prompt_id: "op-0001", answered_by: "Ana", source: "web", answered_at: "2026-11-03T10:05:00+00:00", ...answer }, ...over });

  it("a confirm reads Yes / No; a choice reads the option's label", () => {
    assert.equal(answerSummary(closed({}, { status: "answered", choice: "yes" })), "是");
    assert.equal(answerSummary(closed({}, { status: "answered", choice: "no" })), "否");
    const c = closed({ kind: "choice", options: [{ id: "hwp_532", label: "λ/2 @532 nm" }] }, { status: "answered", choice: "hwp_532" });
    assert.equal(answerSummary(c), "λ/2 @532 nm");
  });

  it("a number carries its unit; a signature reads signed", () => {
    assert.equal(answerSummary(closed({ kind: "number", unit: "mW" }, { status: "answered", value: 12.5 })), "12.5 mW");
    assert.equal(answerSummary(closed({ kind: "attest" }, { status: "answered", value: true })), "已签署");
  });

  it("how a prompt ended without an answer", () => {
    assert.ok(answerSummary(closed({}, { status: "dismissed" })).includes("拒绝"));
    assert.ok(answerSummary(closed({}, { status: "cancelled" })).includes("取消"));
    assert.ok(answerSummary(closed({}, { status: "timeout" })).includes("超时"));
  });
});

describe("time", () => {
  it("durations are coarse on purpose", () => {
    assert.equal(formatDuration(45_000), "45 秒");
    assert.equal(formatDuration(190_000), "3 分 10 秒");
    assert.equal(formatDuration(12 * 60_000 + 20_000), "12 分");
    assert.equal(formatDuration(2 * 3600_000 + 5 * 60_000), "2 小时 5 分");
    assert.equal(formatDuration(3 * 3600_000), "3 小时");
    assert.equal(formatDuration(-5), "0 秒");
  });

  it("how long a prompt has waited, and when the agent gives up", () => {
    const p = P({ created_at: "2026-11-03T10:00:00+00:00", timeout_s: 600 });
    const t0 = Date.parse("2026-11-03T10:00:00+00:00");
    assert.equal(waitingFor(p, t0 + 130_000), "2 分 10 秒");
    assert.equal(deadlineLeftMs(p, t0 + 130_000), 470_000);
    assert.equal(deadlineLeftMs(p, t0 + 700_000)! < 0, true, "negative once the agent has stopped waiting");
    assert.equal(deadlineLeftMs(P({ timeout_s: null }), t0), null, "an agent that waits for ever has no deadline");
    assert.equal(waitingFor(P({ created_at: "garbage" }), t0), null);
    const line = metaLine(P({ agent_id: "claude-code", job_id: "j-12", skill: "AskOperator", timeout_s: 600 }), t0 + 60_000);
    for (const part of ["claude-code", "j-12", "AskOperator", "1 分", "9 分"]) assert.ok(line.includes(part), `${part} in ${line}`);
  });
});

describe("the alert", () => {
  it("each severity has its own beep: urgent for safety, a double for an action, one for information", () => {
    const [s, a, i] = [beepPlan("safety"), beepPlan("action"), beepPlan("info")];
    assert.ok(s.length > a.length && a.length > i.length);
    for (const step of [...s, ...a, ...i]) assert.ok(step.freq > 100 && step.ms > 0 && step.gapMs >= 0);
    assert.ok(s.reduce((n, x) => n + x.ms + x.gapMs, 0) < 3000, "an alert is not a siren");
  });

  it("a safety toast stays until it is handled; the others fade", () => {
    assert.equal(toastLifetimeMs("safety"), null);
    assert.ok((toastLifetimeMs("action") ?? 0) > 0);
    assert.ok((toastLifetimeMs("info") ?? 0) > 0);
  });

  it("a desktop notification is for someone who is not looking at this page", () => {
    const base = { supported: true, permission: "granted", hidden: false, hasFocus: true };
    assert.equal(shouldDesktopNotify(base), false, "the toast is enough while the page is in front");
    assert.equal(shouldDesktopNotify({ ...base, hidden: true }), true, "tab in the background");
    assert.equal(shouldDesktopNotify({ ...base, hasFocus: false }), true, "browser behind the vendor software");
    assert.equal(shouldDesktopNotify({ ...base, hidden: true, permission: "default" }), false, "never without permission");
    assert.equal(shouldDesktopNotify({ ...base, hidden: true, permission: "denied" }), false);
    assert.equal(shouldDesktopNotify({ ...base, hidden: true, supported: false }), false);
  });

  it("the notification names the prompt and shortens a long message", () => {
    const n = notificationText(P({ severity: "safety", title: "Laser area safe?", message: "x ".repeat(300) }));
    assert.ok(n.title.includes("Laser area safe?") && n.title.includes("安全"));
    assert.ok(n.body.length <= 160);
    assert.ok(notificationText(P({ message: "" })).body.length > 0, "never an empty body");
  });
});

describe("who is answering", () => {
  const memory = (initial: Record<string, string> = {}) => {
    const data = { ...initial };
    return { getItem: (k: string) => data[k] ?? null, setItem: (k: string, v: string) => void (data[k] = v), data };
  };

  it("the name is remembered tidied, and read back", () => {
    const store = memory();
    assert.equal(writeOperatorName("  Ana   Ruiz ", store), "Ana Ruiz");
    assert.equal(store.data[OPERATOR_NAME_KEY], "Ana Ruiz");
    assert.equal(readOperatorName(store), "Ana Ruiz");
    assert.equal(readOperatorName(memory()), "");
  });

  it("storage that is off or throws is not an error", () => {
    assert.equal(readOperatorName(null), "");
    assert.equal(writeOperatorName("Ana", null), "Ana");
    const broken = { getItem: () => { throw new Error("denied"); }, setItem: () => { throw new Error("denied"); } };
    assert.equal(readOperatorName(broken), "");
    assert.equal(writeOperatorName("Ana", broken), "Ana");
  });
});

describe("calls to /api/operator", () => {
  it("the reason in an error body", () => {
    assert.equal(errorDetail({ detail: "7 is above the maximum 5" }), "7 is above the maximum 5");
    assert.equal(errorDetail({ detail: [{ loc: ["body", "by"], msg: "field required" }, { msg: "bad" }] }), "field required; bad");
    assert.equal(errorDetail({ error: "boom" }), "boom");
    assert.equal(errorDetail({ message: "m" }), "m");
    assert.equal(errorDetail(null), "");
    assert.equal(errorDetail("text"), "");
  });

  it("a dead service is not hammered, and an older one without the API is asked once a minute", () => {
    assert.equal(pollDelayMs(null), 2500);
    assert.equal(pollDelayMs(undefined, 5000), 5000);
    assert.equal(pollDelayMs(new Error("x")), 10_000);
    assert.equal(pollDelayMs({ status: 500 }), 10_000);
    assert.equal(pollDelayMs({ status: 404 }), 60_000);
  });

  it("somebody else got there first: 404 / 409 / 410", () => {
    for (const s of [404, 409, 410]) assert.equal(isAlreadyHandled(s), true);
    for (const s of [400, 401, 403, 422, 500, 503, 0, undefined]) assert.equal(isAlreadyHandled(s), false);
    assert.ok(failureText({ status: 409, message: "already answered" }).includes("别处"));
  });

  it("for a slot write a 404 is a missing slot, not a prompt handled elsewhere", () => {
    assert.equal(failureText({ status: 404, message: "no optical-state slot 'x'" }, "slot"), "no optical-state slot 'x'");
    assert.ok(failureText({ status: 404, message: "no prompt" }, "prompt").includes("别处"));
  });

  it("a refused value (422) and a failed save (503) show the service's own words", () => {
    assert.equal(failureText({ status: 422, message: "7 is above the maximum 5" }), "7 is above the maximum 5");
    assert.equal(failureText({ status: 503, message: "Could not save this. Nothing was changed." }), "Could not save this. Nothing was changed.");
  });

  it("other failures say what the service said, or that it could not be reached", () => {
    assert.equal(failureText({ status: 400, message: "5 is not allowed here" }), "5 is not allowed here");
    assert.ok(failureText({ status: 0, message: "Failed to fetch" }).includes("主服务"));
    assert.equal(failureText("plain"), "plain");
  });
});
