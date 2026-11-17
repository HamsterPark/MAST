import { test, expect, type Page } from "@playwright/test";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/**
 * Station capabilities in a real browser (contract: docs/v2/design/generic_station.md): the three
 * station profiles (stm / optical / generic), each from a service that sends
 * `station_capabilities` + `station_serial_autodetect` and from one that does not (an older
 * backend: the list is derived from the profile), plus the first-use flow, the serial
 * auto-detection switch and a snapshot of the STM station's interface.
 *
 * The unit tests pin the pure decisions (test/stationCapabilities.test.ts,
 * test/serialAutodetect.test.ts) and the wiring is pinned from source
 * (test/stationCapabilityWiring.test.ts); neither can show that a page opened on a non-STM
 * station really stays off the Nanonis endpoints. This does: the backend is stubbed (every /api
 * request is answered here and recorded), every /ws socket is mocked, and each case opens the
 * overview, the settings, the navigation and Advanced → Hardware, then checks
 *
 *   - not one request to /api/nanonis* or /api/hardware/live-readings on an optical or a
 *     generic station (and, so that "none" means something, that the STM station does make
 *     them); nor to the signal monitor, the STM setup checklist, the tip register, the Nanonis
 *     oscilloscope; and never the gallery index from the overview;
 *   - what is drawn: the STM readouts, the devices, the generic overview (environment sensors,
 *     external devices, data gallery), the STM pages and settings sections;
 *   - no console error and no page error anywhere.
 *
 * Screenshots go to $STATION_E2E_OUT when it is set (otherwise to the test's output dir).
 *
 *   npm run build && npm run test:e2e -- station-capabilities
 */

type Profile = "stm" | "optical" | "generic";
type Serial = "default_on" | "default_off" | "forced_off";

interface Station {
  profile: Profile;
  /** false = the service sends neither `station_capabilities` nor `station_serial_autodetect` */
  newBackend: boolean;
  source: "env" | "settings" | "default";
  stored: Profile | null;
  pinSet: boolean;
  restartPending: boolean;
  /** the instrument-init record: null = never set up, a number = completed at */
  initCompletedAt: number | null;
  /** setting `serial_autodetect` */
  serialStored: boolean | null;
}

// generic_station.md §1.2 / §1.4
const DEFAULT_CAPS: Record<Profile, string[]> = {
  stm: ["nanonis", "stm_safety", "stm_actions", "scan_map"],
  optical: ["witec", "optical_bench", "operator_prompts", "laser_safety"],
  generic: ["optical_bench", "operator_prompts", "laser_safety", "sensor_echo"],
};
const SERIAL: Record<Profile, Serial> = { stm: "default_on", optical: "forced_off", generic: "default_off" };

const STM_ENDPOINT = /^\/api\/(nanonis(\/|$)|hardware\/live-readings$)/;

// the five STM-only settings sections (title prefixes)
const STM_SETTINGS = ["Z 参数组", "扫描参数档位", "仪器进退针", "经典针尖阈值", "信号监控 Signal Monitor"];

const STM_ESTOP = "紧急停止：中止任务 + 停止运动 + 退针";
const OTHER_ESTOP = "紧急停止：中止任务 + 停止运动 + 关激光与快门 + 磁体停止扫场 + 加热关闭";

const SNAPSHOT_FILE = fileURLToPath(new URL("./fixtures/stm-ui-snapshot.json", import.meta.url));

const json = (status: number, body: unknown) => ({ status, contentType: "application/json", body: JSON.stringify(body) });

function shot(name: string) {
  const dir = process.env.STATION_E2E_OUT;
  return dir ? `${dir}/${name}.png` : test.info().outputPath(`${name}.png`);
}

/** Language, no onboarding modal; must be in place before the app's own scripts run. */
async function prepare(page: Page, lang: "zh" | "en" = "zh") {
  await page.addInitScript((l) => {
    window.localStorage.setItem("mast.lang", l);
    window.localStorage.setItem("mast.onboarding.v1.done", "1");
  }, lang);
}

function station(profile: Profile, over: Partial<Station> = {}): Station {
  return {
    profile,
    newBackend: true,
    source: "settings",
    stored: profile,
    pinSet: true,
    restartPending: false,
    initCompletedAt: 1_760_000_000,
    serialStored: null,
    ...over,
  };
}

/**
 * The stubbed service. `st` is live: the tests flip it the way the real service would (a PIN
 * set, a setting saved).
 */
/**
 * Real-backend run: STATION_REAL_HEALTH_DIR holds <profile>.json, each the answer of the
 * backend's own GET /api/health under MAST_STATION_PROFILE=<profile>. The two station fields then come from the backend instead of this file's
 * tables, so a drift between the two shows up as a failing case. Unset: the tables above.
 */
function realHealthFields(profile: Profile): Record<string, unknown> | null {
  const dir = process.env.STATION_REAL_HEALTH_DIR;
  if (!dir) return null;
  const real = JSON.parse(readFileSync(`${dir}/${profile}.json`, "utf8")) as Record<string, unknown>;
  return { station_capabilities: real.station_capabilities, station_serial_autodetect: real.station_serial_autodetect };
}

async function stubBackend(page: Page, st: Station) {
  const rec = {
    requests: [] as string[],
    stm: [] as string[],
    unhandled: new Set<string>(),
    writes: [] as { path: string; body: unknown }[],
    errors: [] as string[],
  };
  page.on("console", (m) => {
    if (m.type() === "error") rec.errors.push(m.text());
  });
  page.on("pageerror", (e) => rec.errors.push(String(e)));

  // every socket (/ws/events, the LiveHub's /ws/live): this mock IS the server, never connects
  // upstream, never sends anything
  await page.routeWebSocket(/\/ws\//, () => undefined);

  const health = () => ({
    status: "ok",
    service: "mast-api",
    version: "e2e",
    skill_registry_wired: true,
    settings_store_wired: true,
    experiment_storage_wired: true,
    station_profile: st.profile,
    station_profile_source: st.source,
    station_restart_pending: st.restartPending,
    ...(st.newBackend
      ? realHealthFields(st.profile) ?? {
          station_capabilities: DEFAULT_CAPS[st.profile],
          station_serial_autodetect: SERIAL[st.profile],
        }
      : {}),
  });

  const devices = {
    degraded: false,
    devices: [
      { id: "stage_xy", name: "XY stage", type: "sim_stage", kind: "motion", enabled: true, connected: false },
      { id: "tc1", name: "Sample heater", type: "sim_temperature", kind: "temperature", enabled: true, connected: true },
      { id: "pm1", name: "Power meter", type: "sim_power_meter", kind: "power_meter", enabled: false, connected: false },
    ],
  };
  const sensor = (name: string, kind: string, value: number, unit: string) => ({
    name, type: "Sim", kind, value, unit, status: "ok", connected: true, placeholder: false,
  });
  const envReadings = {
    degraded: false,
    overall_status: "ok",
    temperature: { value: 77.3, unit: "K", connected: true, status: "ok" },
    vacuum: { value: 2.1e-10, unit: "mbar", connected: true, status: "ok" },
    sensors: [sensor("Stage", "temperature", 77.3, "K"), sensor("Chamber", "vacuum", 2.1e-10, "mbar")],
  };
  const sensorsConfig = () => ({
    degraded: false,
    autodetect: true, // the file's flag: it does not decide on an optical or a generic station
    count: 1,
    sensors: [{ id: "ls335", name: "Lake Shore 335", type: "LakeShore335", port: "COM14", unit: "K", enabled: true }],
    ...(st.newBackend
      ? {
          autodetect_effective: st.profile === "stm",
          autodetect_reason:
            st.profile === "optical"
              ? "forced off by the optical station profile"
              : st.profile === "generic"
                ? "off by default on the generic station profile"
                : "as configured",
        }
      : {}),
  });

  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const p = url.pathname;
      const method = req.method();
      rec.requests.push(`${method} ${p}`);
      if (STM_ENDPOINT.test(p)) rec.stm.push(`${method} ${p}`);

      if (method !== "GET") {
        const body = req.postDataJSON?.() ?? null;
        rec.writes.push({ path: p, body });
        if (p === "/api/admin/set-pin") {
          st.pinSet = true;
          return route.fulfill(json(200, { ok: true, reason: "", message: "" }));
        }
        if (p === "/api/admin/unlock-pin") {
          return route.fulfill(json(200, { ok: true, token: "e2e", degraded: false }));
        }
        if (p === "/api/settings") {
          const b = (body ?? {}) as { station_profile?: Profile; serial_autodetect?: boolean };
          if (b.station_profile) {
            st.stored = b.station_profile;
            st.source = "settings";
            st.restartPending = b.station_profile !== st.profile;
          }
          if (typeof b.serial_autodetect === "boolean") st.serialStored = b.serial_autodetect;
          return route.fulfill(json(200, { ok: true, saved: Object.keys(b), rejected: {}, degraded: false }));
        }
        return route.fulfill(json(200, { ok: true, degraded: false }));
      }

      switch (p) {
        case "/api/health":
          return route.fulfill(json(200, health()));
        case "/api/settings":
          return route.fulfill(json(200, {
            legacy_features_enabled: false, station_profile: st.stored, theme: "dark", font_scale: "medium", degraded: false,
            ...(st.newBackend ? { serial_autodetect: st.serialStored } : {}),
          }));
        case "/api/admin/pin-status":
          return route.fulfill(json(200, { pin_is_set: st.pinSet }));
        case "/api/safety/emergency-latch":
          return route.fulfill(json(200, { latched: false, abort_set: false, why: "", degraded: false }));
        case "/api/experiments/current":
          return route.fulfill(json(200, { experiment: null, sample: null }));
        case "/api/system/check":
          return route.fulfill(json(200, { degraded: false, items: [{ name: "Settings store", status: "ok", detail: "e2e" }] }));
        case "/api/optics/devices":
          return route.fulfill(json(200, devices));
        case "/api/environment/readings":
          return route.fulfill(json(200, envReadings));
        case "/api/environment/sensors":
          return route.fulfill(json(200, sensorsConfig()));
        case "/api/gallery/config":
          return route.fulfill(json(200, {
            ok: true, degraded: false, auto_build: true, auto_interval_min: 10, state_dir: "D:/mast/gallery",
            roots: [{ name: "data", path: "D:/data", enabled: true, exists: true }, { name: "old", path: "D:/old", enabled: false, exists: true }],
            suggestions: [],
          }));
        case "/api/gallery/status":
          return route.fulfill(json(200, {
            degraded: false, running: false, phase: "done", finished: "2026-11-13 10:00:00", started: "2026-11-13 09:59:00",
            done: 0, total: 0, message: "", trigger: "auto", n_files: 1560, n_new: 0, n_changed: 0, n_removed: 0,
            n_failed: 0, n_render: 0, n_analysis: 0, n_deferred: 0,
          }));
        case "/api/gallery/index":
          // answered so the gallery page works, but the overview must never ask for it
          return route.fulfill(json(200, { built: true, degraded: false, generated: "2026-11-13T10:00:00", items: [], last_batch: "", n_frames: 1234, n_grids: 5, n_spectra: 321, roots: [], version: 1 }));
        case "/api/instrument-init":
          return route.fulfill(json(200, {
            degraded: false, should_prompt: false, needs_setup: false, fingerprint_changed: false,
            counts: { required: { total: 0, complete: 0 } }, items: [], groups: [], outstanding_required: [],
            completed_at: st.initCompletedAt, rig_fingerprint: null, acknowledged: [],
          }));
        case "/api/experimental/signals":
          // the STM station's instrument tools; the real service always sends every list, even degraded
          return route.fulfill(json(200, {
            degraded: true, detail: "e2e", channels: [], current_indices: [], timebases: [], output_modes: [], windows: [],
            n_channels: 0, osci_available: false, source: "e2e", truncated: false,
          }));
        case "/api/nanonis/connection":
          return route.fulfill(json(200, { connected: true, host: "127.0.0.1", degraded: false, ports: [{ role: "main", port: 6501, connected: true }] }));
        case "/api/hardware/live-readings":
          return route.fulfill(json(200, {
            connected: true, degraded: false, bias_history: [], current_history: [], z_history: [],
            readings: { bias_v: 0.5, current_a: 1e-10, z_m: 1e-8, setpoint_a: 1e-10, z_controller_on: true, withdrawn: false, scan_running: false, stale: false },
          }));
        default:
          rec.unhandled.add(p);
          return route.fulfill(json(200, { degraded: true }));
      }
    },
  );
  return rec;
}

/** A second-level tab (SubTabs renders buttons with aria-pressed). */
const subTab = (page: Page, name: string) => page.getByRole("button", { name, exact: true });

/** The open modal (Modal: a fixed overlay; it has no dialog role of its own). */
const modal = (page: Page) => page.locator("div.fixed.inset-0.z-50");

/** The settings page's accordion titles, in order. */
const accordionTitles = (page: Page) =>
  page.locator("main button[aria-expanded] > span:first-child").allTextContents();

/** The E-STOP button's tooltip (its accessible name stays "E-STOP", as on the STM station today). */
const estopTitle = (page: Page) => page.locator("header button", { hasText: "E‑STOP" }).getAttribute("title");

const CASES: { profile: Profile; newBackend: boolean }[] = [
  { profile: "stm", newBackend: false },
  { profile: "stm", newBackend: true },
  { profile: "optical", newBackend: false },
  { profile: "optical", newBackend: true },
  { profile: "generic", newBackend: false },
  { profile: "generic", newBackend: true },
];

for (const { profile, newBackend } of CASES) {
  const label = `${profile}, ${newBackend ? "with" : "without"} station_capabilities`;

  test(`${label}: overview, settings, nav and hardware follow the capabilities`, async ({ page }) => {
    test.setTimeout(90_000);
    await prepare(page);
    const st = station(profile, { newBackend });
    const rec = await stubBackend(page, st);
    const tag = `${profile}-${newBackend ? "caps" : "nocaps"}`;
    const stm = profile === "stm";

    // ── overview ──
    await page.goto("/");
    await expect(page.locator("main")).toBeVisible();
    await expect.poll(() => rec.requests.filter((r) => r === "GET /api/health").length).toBeGreaterThan(0);
    await page.waitForTimeout(2500); // a few polls of the shell
    if (stm) {
      await expect(page.getByTestId("reading-bias")).toBeVisible();
      await expect(page.getByText("Nanonis 连接", { exact: true })).toBeVisible();
      await expect(page.getByTestId("station-kind")).toHaveCount(0);
    } else {
      await expect(page.getByTestId("reading-bias")).toHaveCount(0);
      await expect(page.getByTestId("station-kind")).toHaveText(profile === "optical" ? "光学站" : "通用站");
      await expect(page.getByText("Nanonis 连接", { exact: true })).toHaveCount(0);
      await expect(page.locator("aside").getByText("INSTRUMENT", { exact: true })).toHaveCount(0);
      await expect(page.locator("aside").getByText("STATION", { exact: true })).toBeVisible();
    }
    await expect(page.getByRole("heading", { name: "光学设备", exact: true })).toHaveCount(profile === "optical" ? 1 : 0);
    await expect(page.getByRole("heading", { name: "外部设备", exact: true })).toHaveCount(profile === "generic" ? 1 : 0);
    await expect(page.getByTestId("env-sensors")).toHaveCount(profile === "generic" ? 1 : 0);
    if (profile === "generic") {
      await expect(page.getByTestId("gallery-overview")).toContainText("1 / 2"); // enabled / all roots
      await expect(page.getByTestId("gallery-overview")).toContainText("1560"); // files at the last build
      await expect(page.getByTestId("gallery-status")).toContainText("上次更新索引");
    }
    await page.screenshot({ path: shot(`${tag}-1-overview`), fullPage: true });

    // ── E-STOP says what it does here ──
    expect(await estopTitle(page)).toBe(stm ? STM_ESTOP : OTHER_ESTOP);

    // ── settings ──
    await page.goto("/settings/general");
    await expect(page.locator("#station-profile")).toBeVisible();
    const radios = page.locator("#station-profile").getByRole("radio");
    await expect(radios).toHaveText(["STM 站", "光学站", "通用站"]);
    await expect(page.locator("#station-profile").getByRole("radio", { checked: true })).toHaveText(
      stm ? "STM 站" : profile === "optical" ? "光学站" : "通用站",
    );
    const titles = await accordionTitles(page);
    for (const t of STM_SETTINGS) {
      expect(titles.some((x) => x.startsWith(t)), `${t} on ${profile}`).toBe(stm);
    }
    expect(titles.includes("硬件连接 Hardware"), "Nanonis ports").toBe(stm);
    // serial auto-detection: nothing on STM or with an older backend; forced off (no switch) on optical;
    // off by default with a switch on generic
    const serial = page.getByTestId("serial-autodetect");
    if (!newBackend || stm) {
      await expect(serial).toHaveCount(0);
    } else if (profile === "optical") {
      await expect(serial).toContainText("强制关闭");
      await expect(serial.locator("button")).toHaveCount(0);
    } else {
      await expect(serial).toContainText("通用站默认关");
      await expect(serial.locator("button")).toHaveCount(1);
    }
    await page.screenshot({ path: shot(`${tag}-2-settings`), fullPage: true });

    // ── nav: the STM pages only where they belong ──
    await expect(subTab(page, "仪器初始化")).toHaveCount(stm ? 1 : 0);
    await page.goto("/monitoring");
    await expect(page).toHaveURL(stm ? /\/monitoring\/(current|env)$/ : /\/monitoring\/env$/);
    await expect(subTab(page, "信号监控")).toHaveCount(stm ? 1 : 0);
    await page.goto("/records/log");
    await expect(subTab(page, "扫描地图")).toHaveCount(stm ? 1 : 0);
    // Instrument Tools is on every station (the mosaic reads saved files); only the Nanonis tools are gated
    await page.goto("/experimental");
    await expect(page).toHaveURL(/\/experimental\/tools$/);
    await expect(subTab(page, "仪器工具")).toHaveCount(1);
    await expect(subTab(page, "信号捕获 + FFT")).toHaveCount(stm ? 1 : 0);
    await expect(subTab(page, "图像拼接")).toHaveCount(1);
    await page.screenshot({ path: shot(`${tag}-3-tools`), fullPage: false });

    // a bookmark to an STM page on another station: an explanation, not the page
    await page.goto("/monitoring/current");
    if (stm) {
      await expect(page.getByText("这台机器上没有这一页")).toHaveCount(0);
    } else {
      await expect(page.getByText("这台机器上没有这一页")).toBeVisible();
      await page.screenshot({ path: shot(`${tag}-4-notice`), fullPage: false });
    }

    // ── Advanced → System / global → Hardware connection: the sensors on every station, Nanonis only with it ──
    await page.goto("/settings/admin");
    await page.getByPlaceholder("已设置的管理 PIN").fill("2468");
    await page.getByRole("button", { name: "解锁", exact: true }).click();
    await page.getByRole("radio", { name: "系统 / 全局" }).click();
    await subTab(page, "硬件连接").click();
    await expect(page.getByText("环境传感器", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("Nanonis 连接", { exact: true })).toHaveCount(stm ? 1 : 0);
    // the sensors card's badge: what is in effect where the file does not decide
    const badge = page.locator("main").getByText(/^(自动探测|手动)$/).first();
    await expect(badge).toHaveText(stm || !newBackend ? "自动探测" : "手动");
    await page.screenshot({ path: shot(`${tag}-5-hardware`), fullPage: false });
    await page.waitForTimeout(1000);

    // ── the network ──
    const asked = (part: string) => rec.requests.filter((r) => r.includes(part));
    if (stm) {
      expect(rec.stm.length, "the STM station does poll Nanonis (so that zero below means something)").toBeGreaterThan(0);
      expect(asked("/api/tips").length, "the STM station has its tip register").toBeGreaterThan(0);
      expect(asked("/api/experimental/signals").length, "the STM station's oscilloscope tools").toBeGreaterThan(0);
    } else {
      expect(rec.stm, "requests to /api/nanonis* or /api/hardware/live-readings").toEqual([]);
      expect(asked("/api/monitoring/"), "the signal monitor page was not mounted").toEqual([]);
      expect(asked("/api/instrument-init"), "the STM setup checklist was not asked for").toEqual([]);
      expect(asked("/api/tips"), "the tip register (no tip here)").toEqual([]);
      expect(asked("/api/experimental/signals"), "the Nanonis oscilloscope").toEqual([]);
    }
    expect(asked("/api/gallery/index"), "the overview never asks for the gallery index").toEqual([]);
    // what the stub answered with a bare `{degraded: true}`, for whoever reads the run
    test.info().annotations.push({ type: "unhandled", description: [...rec.unhandled].sort().join(" ") });
    expect(rec.errors, "console errors").toEqual([]);
  });
}

test("generic: switching serial auto-detection on asks for the PIN and says when it takes effect", async ({ page }) => {
  test.setTimeout(60_000);
  await prepare(page);
  const st = station("generic");
  const rec = await stubBackend(page, st);

  await page.goto("/settings/general");
  const row = page.getByTestId("serial-autodetect");
  await expect(row).toContainText("通用站默认关");
  await row.locator("button").click();
  const dialog = modal(page);
  await expect(dialog).toContainText("打开串口自动探测？");
  await expect(dialog).toContainText("*IDN?");
  await dialog.getByPlaceholder("保存需要管理 PIN").fill("2468");
  await page.screenshot({ path: shot("serial-1-dialog"), fullPage: false });
  await dialog.getByRole("button", { name: "确认打开并保存" }).click();
  await expect(modal(page)).toHaveCount(0);
  // saved, not yet in effect (the stub keeps autodetect_effective false, as the service does until a restart)
  await expect(row).toContainText("下次启动");
  await page.screenshot({ path: shot("serial-2-saved"), fullPage: false });

  expect(rec.writes.filter((w) => w.path === "/api/settings").map((w) => w.body)).toEqual([
    { serial_autodetect: true, admin_pin: "2468" },
  ]);
  expect(rec.errors, "console errors").toEqual([]);
});

test("first use on a brand-new machine: set the admin PIN in the confirmation, then save the station type", async ({ page }) => {
  test.setTimeout(60_000);
  await prepare(page);
  // a generic lab machine on its first start: nothing stored, no environment variable, no PIN, never set up
  const st = station("stm", { source: "default", stored: null, pinSet: false, initCompletedAt: null });
  const rec = await stubBackend(page, st);

  await page.goto("/");
  const banner = page.getByTestId("station-setup-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("这台机器还没确认站点类型");
  await expect(banner.locator("[data-step-open=pin]")).toBeVisible();
  await page.screenshot({ path: shot("firstrun-1-banner"), fullPage: false });

  await banner.getByRole("link", { name: "去选择 →" }).first().click();
  await expect(page).toHaveURL(/\/settings\/general#station-profile$/);
  await expect(page.getByTestId("station-setup-banner")).toHaveCount(0); // the card says it here
  const card = page.locator("#station-profile");
  await expect(card.getByTestId("station-setup-steps")).toBeVisible();

  // confirming the default is possible on first use (otherwise the prompt never goes away)
  await card.getByRole("radio", { name: "STM 站" }).click();
  const dialog = modal(page);
  await expect(dialog).toContainText("确认这台机器是「STM 站」？");
  await expect(dialog).toContainText("不需要重启");
  await dialog.getByRole("button", { name: "取消" }).click();
  await expect(modal(page)).toHaveCount(0);

  // the generic station: no PIN yet, so the dialog offers the first-PIN form instead of a PIN field
  await card.getByRole("radio", { name: "通用站" }).click();
  await expect(dialog).toContainText("把这台机器改成「通用站」？");
  await expect(dialog.locator("[data-profile]")).toHaveCount(3);
  await expect(dialog.locator('[data-profile="generic"][data-selected="true"]')).toBeVisible();
  await expect(dialog.locator('[data-profile="generic"]')).toContainText("串口自动探测默认关闭");
  await expect(dialog.locator('[data-profile="generic"]')).toContainText("安装操作员提示服务和激光撤销监听");
  await expect(dialog.getByText("在此设置管理 PIN")).toBeVisible();
  await expect(dialog.getByPlaceholder("保存需要管理 PIN")).toHaveCount(0);
  await page.screenshot({ path: shot("firstrun-2-dialog-no-pin"), fullPage: false });

  await dialog.getByPlaceholder("新 PIN（至少 4 位）").fill("2468");
  await dialog.getByPlaceholder("再输一次").fill("2468");
  await dialog.getByRole("button", { name: "设置 PIN" }).click();
  const pinField = dialog.getByPlaceholder("保存需要管理 PIN");
  await expect(pinField).toBeVisible();
  await pinField.fill("2468");
  await page.screenshot({ path: shot("firstrun-3-dialog-pin"), fullPage: false });
  await dialog.getByRole("button", { name: "确认改为「通用站」并保存" }).click();

  await expect(modal(page)).toHaveCount(0);
  await expect(card).toContainText("已保存，需要重启 MAST 服务后生效。");
  await expect(card.getByTestId("station-setup-steps")).toHaveCount(0);
  await page.screenshot({ path: shot("firstrun-4-saved"), fullPage: false });

  expect(rec.writes.map((w) => w.path)).toEqual(["/api/admin/set-pin", "/api/settings"]);
  expect(rec.writes[0]!.body).toEqual({ new_pin: "2468" });
  expect(rec.writes[1]!.body).toEqual({ station_profile: "generic", admin_pin: "2468" });

  await page.goto("/");
  await expect(page.locator("main")).toBeVisible();
  await page.waitForTimeout(1000);
  await expect(page.getByTestId("station-setup-banner")).toHaveCount(0);
  expect(rec.errors, "console errors").toEqual([]);
});

test("first use on an STM station that has been running for months: no banner, the card still says it", async ({ page }) => {
  test.setTimeout(60_000);
  await prepare(page);
  // upgraded: nothing stored (the profile setting did not exist), but the instrument was set up long ago
  const st = station("stm", { source: "default", stored: null, pinSet: false, initCompletedAt: 1_750_000_000 });
  const rec = await stubBackend(page, st);

  await page.goto("/");
  await expect(page.locator("main")).toBeVisible();
  await expect.poll(() => rec.requests.filter((r) => r === "GET /api/instrument-init").length).toBeGreaterThan(0);
  await page.waitForTimeout(1500);
  await expect(page.getByTestId("station-setup-banner")).toHaveCount(0);

  await page.goto("/settings/general");
  await expect(page.locator("#station-profile").getByTestId("station-setup-steps")).toBeVisible();
  expect(rec.errors, "console errors").toEqual([]);
});

/**
 * The STM station's interface, as it was before station capabilities: the tabs, the second-level tabs, the
 * top bar (E-STOP tooltip, readings row, Nanonis status), the right panel's sections, the
 * overview's headings and cards, the settings sections, the Instrument Tools sub-tabs.
 * The station-profile card (now three choices) and the admin-PIN note (now lists three
 * stations) are the two intended changes and are not in the snapshot.
 *
 * Recorded once against an export of the pre-capabilities source served by Vite, with
 * STATION_SNAPSHOT_RECORD=<file>; compared on every run after that.
 */
async function collectStmSnapshot(page: Page) {
  const snap: Record<string, unknown> = {};
  await page.goto("/");
  await expect(page.locator("main")).toBeVisible();
  await page.waitForTimeout(3000);
  snap.tabs = await page.locator("nav a").evaluateAll((as) => as.map((a) => `${a.getAttribute("href")} | ${a.textContent?.trim()}`));
  snap.estopTitle = await estopTitle(page);
  snap.readingsRow = await page.getByTestId("reading-bias").count();
  snap.status = (await page.locator("header span", { hasText: "Nanonis ·" }).last().textContent())?.trim() ?? null;
  snap.rightPanel = await page.locator("aside span").evaluateAll((ss) =>
    ss.filter((s) => s.className.includes("tracking-[1.4px]")).map((s) => s.textContent?.trim()),
  );
  snap.overviewHeadings = await page.locator("main h2").allTextContents();
  snap.overviewCards = await page.locator("main div.uppercase.tracking-wide").allTextContents();
  const subTabs: Record<string, string[]> = {};
  for (const path of ["/settings/general", "/monitoring/current", "/records/log", "/experimental/tools", "/skills/library"]) {
    await page.goto(path);
    await expect(page.locator("main")).toBeVisible();
    await page.waitForTimeout(800);
    subTabs[path] = await page.locator("main button[aria-pressed]").allTextContents();
    if (path === "/settings/general") snap.settingsSections = await accordionTitles(page);
  }
  snap.subTabs = subTabs;
  return snap;
}

for (const newBackend of [false, true]) {
  test(`STM UI snapshot (${newBackend ? "with" : "without"} station_capabilities): unchanged since before station capabilities`, async ({ page }) => {
    test.setTimeout(90_000);
    await prepare(page);
    const rec = await stubBackend(page, station("stm", { newBackend }));
    const snap = await collectStmSnapshot(page);
    const recordTo = process.env.STATION_SNAPSHOT_RECORD;
    if (recordTo) {
      writeFileSync(recordTo, JSON.stringify(snap, null, 2) + "\n", "utf8");
      test.info().annotations.push({ type: "recorded", description: recordTo });
      return;
    }
    expect(existsSync(SNAPSHOT_FILE), SNAPSHOT_FILE).toBe(true);
    expect(snap).toEqual(JSON.parse(readFileSync(SNAPSHOT_FILE, "utf8")));
    expect(rec.errors, "console errors").toEqual([]);
  });
}
