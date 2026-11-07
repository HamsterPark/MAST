import { test, expect, type Page } from "@playwright/test";

/**
 * Operator prompts exist on an optical station only -- rendered once, in a real browser.
 *
 * The unit tests pin the pure decisions (lib/operatorPrompts.ts) and the wiring is pinned
 * from source (test/operatorWiring.test.ts); neither can show that a page opened on an STM
 * station really stays silent on the network. This does: the backend is stubbed, every
 * request under /api/operator is recorded, and each case watches the page for a window
 * long enough for three of the shell's 2.5 s polls.
 *
 *   - STM, or a service that does not say what it is (or whose health check fails): not one
 *     request under /api/operator, no badge on the tab, no alert region, no count in the
 *     tab title, no sound, no prompt board and no optical-state panel. The stub would hand
 *     an open safety prompt to anything that asked, so a leak shows up everywhere at once.
 *   - optical: the open prompts are asked for again and again, and a prompt that appears
 *     after the page has loaded raises the badge, the toast, the title count and the sound.
 *   - an optical station whose service was not up yet when the page opened is picked up once
 *     it answers, and nothing under /api/operator was asked before it did.
 *
 * The audio context is replaced by a counter: headless Chromium neither plays nor
 * reports what it would have played.
 */

const POLL_WINDOW_MS = 7_500; // three polls of 2.5 s

const SAFETY_PROMPT = {
  id: "op-e2e-1",
  kind: "confirm",
  title: "Is the laser area clear?",
  message: "Look into the room before the shutter opens.",
  options: [],
  severity: "safety",
  status: "open",
  created_at: "2026-11-03T10:00:00+00:00",
  answer: null,
};

const OPTICAL_HEALTH = { status: "ok", version: "e2e", station_profile: "optical" };

type Health = { status: number; body: unknown };

/** The language and the audio stub must be in place before the app's own scripts run. */
async function prepare(page: Page) {
  await page.addInitScript(() => {
    const w = window as unknown as { __beeps: number; AudioContext: unknown };
    window.localStorage.setItem("mast.lang", "en");
    w.__beeps = 0;
    w.AudioContext = class {
      state = "running";
      currentTime = 0;
      destination = {};
      resume() {
        return Promise.resolve();
      }
      createOscillator() {
        w.__beeps += 1;
        return { type: "", frequency: { value: 0 }, connect: () => ({ connect: () => undefined }), start() {}, stop() {} };
      }
      createGain() {
        return { gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} } };
      }
    };
  });
}

const beeps = (page: Page) => page.evaluate(() => (window as unknown as { __beeps: number }).__beeps);

/**
 * Stub the backend. `health(n)` answers the n-th GET /api/health. Everything else under
 * /api answers 503 (the pages degrade, as in the other specs), except /api/operator, which
 * is recorded and answers as an optical station's service would.
 */
async function stubBackend(page: Page, health: (n: number) => Health) {
  const hits = { health: 0, operator: [] as string[], early: [] as string[] };
  const state = { promptOpen: false, profileServed: false };
  const json = (status: number, body: unknown) => ({ status, contentType: "application/json", body: JSON.stringify(body) });

  // The catch-all goes first: Playwright tries the routes registered last first.
  await page.route((url) => url.pathname.startsWith("/api/"), (r) => r.fulfill(json(503, {})));
  await page.route(
    (url) => url.pathname === "/api/health",
    (r) => {
      hits.health += 1;
      const h = health(hits.health);
      if ((h.body as { station_profile?: unknown } | null)?.station_profile === "optical" && h.status === 200) state.profileServed = true;
      return r.fulfill(json(h.status, h.body));
    },
  );
  await page.route(
    (url) => url.pathname.startsWith("/api/operator"),
    (r) => {
      const url = new URL(r.request().url());
      const what = `${r.request().method()} ${url.pathname}${url.search}`;
      hits.operator.push(what);
      if (!state.profileServed) hits.early.push(what);
      if (url.pathname.endsWith("/optical-state/history")) return r.fulfill(json(200, { records: [] }));
      if (url.pathname.endsWith("/optical-state")) return r.fulfill(json(200, { seq: 0, slots: [], values: {} }));
      const open = state.promptOpen ? [SAFETY_PROMPT] : [];
      return r.fulfill(json(200, { prompts: open, seq: 1, open_count: open.length, persistent: true }));
    },
  );
  return { hits, state };
}

const WISHLIST_TAB = 'nav a[href="/wishlist"]';
const BADGE = `${WISHLIST_TAB} span[aria-label]`;

const NOT_OPTICAL: [string, Health][] = [
  ["an STM station", { status: 200, body: { status: "ok", version: "e2e", station_profile: "stm" } }],
  ["a service that does not say what it is", { status: 200, body: { status: "ok", version: "e2e" } }],
  ["a profile it cannot make sense of", { status: 200, body: { status: "ok", version: "e2e", station_profile: 7 } }],
  ["a service whose health check fails", { status: 503, body: {} }],
];

for (const [name, health] of NOT_OPTICAL) {
  test(`${name}: nothing is asked for under /api/operator and nothing alerts`, async ({ page }) => {
    await prepare(page);
    const { hits, state } = await stubBackend(page, () => health);
    state.promptOpen = true; // anything that asked would be handed an open safety prompt

    await page.goto("/wishlist");
    await expect(page.locator(WISHLIST_TAB)).toBeVisible(); // the tab is there, so "no badge" below means something
    await expect.poll(() => hits.health, { timeout: 10_000 }).toBeGreaterThan(0); // the profile was asked for
    await page.waitForTimeout(POLL_WINDOW_MS);

    expect(hits.operator, "requests under /api/operator").toEqual([]);
    await expect(page.locator(BADGE)).toHaveCount(0);
    await expect(page.getByRole("region", { name: "Operator alerts" })).toHaveCount(0);
    await expect(page).not.toHaveTitle(/^\(\d/);
    expect(await beeps(page), "beeps").toBe(0);
    await expect(page.getByText("Needs your action now")).toHaveCount(0);
    await expect(page.getByText("Optical state (set by hand)")).toHaveCount(0);
    await expect(page.locator("main")).toBeVisible();
  });
}

test("an optical station: the open prompts are polled, and a new one raises badge, toast, title count and sound", async ({ page }) => {
  await prepare(page);
  const { hits, state } = await stubBackend(page, () => ({ status: 200, body: OPTICAL_HEALTH }));

  await page.goto("/wishlist");
  const polls = () => hits.operator.filter((h) => h.includes("/api/operator/prompts?status=open")).length;
  await expect.poll(polls, { timeout: 15_000 }).toBeGreaterThanOrEqual(3);
  expect(hits.early, "requests before the profile was known").toEqual([]);
  await expect(page.getByText("Needs your action now")).toBeVisible();
  await expect(page.getByText("Optical state (set by hand)")).toBeVisible();
  await expect(page.locator(BADGE)).toHaveCount(0); // nothing is waiting yet
  expect(await beeps(page)).toBe(0);

  state.promptOpen = true; // an agent asks the operator something, after the page has loaded

  await expect(page.locator(BADGE)).toHaveText("1", { timeout: 10_000 });
  await expect(page.getByRole("region", { name: "Operator alerts" }).getByText(SAFETY_PROMPT.title)).toBeVisible();
  await expect(page).toHaveTitle(/^\(1\) /);
  await expect.poll(() => beeps(page)).toBeGreaterThan(0);
});

test("an optical station that was not up when the page opened is picked up once it answers", async ({ page }) => {
  test.setTimeout(60_000);
  await prepare(page);
  // the first four health requests fail (the service is still starting), the fifth answers
  const { hits, state } = await stubBackend(page, (n) => (n < 5 ? { status: 503, body: {} } : { status: 200, body: OPTICAL_HEALTH }));
  state.promptOpen = true;

  await page.goto("/wishlist");
  await expect.poll(() => hits.health, { timeout: 30_000 }).toBeGreaterThanOrEqual(5);
  await expect.poll(() => hits.operator.length, { timeout: 20_000 }).toBeGreaterThan(0);
  expect(hits.early, "requests before the service had said it is an optical station").toEqual([]);
  await expect(page.getByText("Needs your action now")).toBeVisible();
});
