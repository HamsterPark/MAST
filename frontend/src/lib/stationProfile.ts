/**
 * Station profile ("stm" | "optical" | "generic") and the optical-bench device roll-ups
 * the non-STM pages show. Pure functions only: no React, no fetch, so `node --test` can
 * run them (test/stationProfile.test.ts).
 *
 * ── What a profile is ──
 * The same MAST build runs on three kinds of machine. An STM station has a Nanonis
 * controller; an optical station (the WITec Raman computer) has no STM at all and
 * drives the optical bench (WITec, laser, power meter, temperature controller)
 * instead; a generic station (another lab's machine) has neither, and uses MAST for
 * its environment sensors, its configured devices, the data gallery and the records.
 * The backend decides which one it is (`mast/station.py`: environment variable > the
 * stored setting `station_profile` > "stm") and reports it in `GET /api/health`
 * (`station_profile`, `station_profile_source`, `station_restart_pending`). The stored
 * value is `GET /api/settings` `station_profile`.
 *
 * What the UI shows is decided by the station's capabilities, not by the profile name
 * (lib/stationCapabilities.ts); the profile is what the operator chooses and what the
 * capabilities default to.
 *
 * Whatever is unreadable falls back to "stm": every machine that existed before
 * profiles keeps behaving exactly as it did, and a typo never turns a working STM
 * station into another kind. This mirrors `station.normalize()` on the backend.
 *
 * "stm" / "optical" / "generic" are identity strings (stored, compared, sent over the
 * API). They are never wrapped in `tr()`; only the labels below are.
 */

import { tr } from "./i18n.ts";

export type StationProfile = "stm" | "optical" | "generic";

/** Every profile, in the order the Settings card and the confirmation list them. */
export const PROFILES: readonly StationProfile[] = Object.freeze(["stm", "optical", "generic"] as const);

export const DEFAULT_PROFILE: StationProfile = "stm";

/**
 * Any value -> a profile. Only "stm" / "optical" / "generic" are recognised (case and
 * surrounding whitespace ignored); everything else, including non-strings and the
 * empty string, is the default.
 */
export function normalizeProfile(v: unknown): StationProfile {
  if (typeof v !== "string") return DEFAULT_PROFILE;
  const s = v.trim().toLowerCase();
  return (PROFILES as readonly string[]).includes(s) ? (s as StationProfile) : DEFAULT_PROFILE;
}

/** Where the configured profile comes from (`GET /api/health` `station_profile_source`):
 * "env" = the MAST_STATION_PROFILE environment variable pins it, so the choice saved
 * on the Settings page cannot change what runs. Unknown / missing is "default". */
export type ProfileSource = "env" | "settings" | "default";

export function normalizeSource(v: unknown): ProfileSource {
  return v === "env" || v === "settings" ? v : "default";
}

/**
 * Nobody has chosen a profile for this machine yet: no stored setting and no environment
 * variable. Only an explicit `station_profile_source: "default"` from the service counts. A
 * service that does not send the field at all (a build from before profiles had a source)
 * says nothing about it, and must not make every existing station look brand new.
 */
export function profileNeverChosen(rawSource: unknown): boolean {
  return rawSource === "default";
}

/**
 * What the instrument-init record (`GET /api/instrument-init`) says about this machine's past:
 *
 *   "used"     it was set up at some point: a completion stamp (`completed_at`), or the rig
 *              fingerprint a completion leaves behind (it survives "remind me again", which only
 *              clears the stamp), or items someone acknowledged;
 *   "never"    a readable record with none of those: a fresh installation;
 *   "unknown"  the request failed, the record is degraded, or the answer has no completion
 *              field at all (a build from before the stamp existed).
 */
export type InitHistory = "never" | "used" | "unknown";

export function initHistory(init: unknown, failed = false): InitHistory {
  if (failed || typeof init !== "object" || init === null) return "unknown";
  const r = init as { degraded?: unknown; completed_at?: unknown; rig_fingerprint?: unknown; acknowledged?: unknown };
  if (r.degraded === true) return "unknown";
  if (typeof r.completed_at === "number" && r.completed_at > 0) return "used";
  if (typeof r.rig_fingerprint === "string" && r.rig_fingerprint.trim()) return "used";
  if (Array.isArray(r.acknowledged) && r.acknowledged.length > 0) return "used";
  if (!("completed_at" in r)) return "unknown";
  return "never";
}

/**
 * The first-use banner: nobody has chosen a profile (`neverChosen`, see profileNeverChosen)
 * AND this machine provably was never set up. An STM station that has been running for months
 * also has no stored profile after an upgrade, but it has been set up, so it is left alone;
 * and when the record cannot be read the banner stays away too (better one reminder too few
 * than one too many). The two steps on the Settings card are not bound by this: they show
 * whenever no profile was chosen.
 */
export function firstRunBannerShown(o: { neverChosen: boolean; init: unknown; initFailed: boolean }): boolean {
  return o.neverChosen && initHistory(o.init, o.initFailed) === "never";
}

export function profileLabel(p: StationProfile): string {
  switch (p) {
    case "optical":
      return tr("光学站", "Optical station");
    case "generic":
      return tr("通用站", "Generic station");
    default:
      return tr("STM 站", "STM station");
  }
}

/** One sentence on what the profile is, for the Settings card. */
export function profileBlurb(p: StationProfile): string {
  switch (p) {
    case "optical":
      return tr(
        "光学站：没有 STM / Nanonis；AI 通过 MCP 编排 WITec 拉曼与激光、功率计、温控。",
        "Optical station: no STM / Nanonis; the AI orchestrates the WITec Raman system, the laser, the power meter and the temperature controller through MCP.",
      );
    case "generic":
      return tr(
        "通用站：既没有 STM / Nanonis，也没有 WITec，给其他实验室用；仪器就是配置的外部设备（光谱仪、光源、功率计、温控、位移台、相机）和环境传感器，AI 通过 MCP 使用它们的技能。",
        "Generic station: neither an STM / Nanonis nor a WITec, for other labs; the instruments are the configured devices (spectrometers, light sources, power meters, temperature controllers, stages, cameras) and the environment sensors, and the AI uses their skills through MCP.",
      );
    default:
      return tr(
        "STM 站：连接 Nanonis 控制器，提供扫描、谱学、退针等 STM 技能。",
        "STM station: connects to a Nanonis controller and offers the STM skills (scanning, spectroscopy, withdrawing the tip, ...).",
      );
  }
}

/**
 * The stored profile and the one in force differ. `configured` null/undefined
 * (never set) is "stm", like everywhere else.
 */
export function restartNeeded(configured: unknown, active: unknown): boolean {
  return normalizeProfile(configured) !== normalizeProfile(active);
}

// ── switching the profile: what the confirmation says, and when it may be confirmed ──
//
// The stored profile is an admin-PIN setting (`admin_pin.GUARDED_KEYS` on the backend):
// it decides at the NEXT start what this process is, and a mis-click costs nothing until
// that restart, which is exactly why the write is where it has to be stopped. The Settings
// page therefore never saves a click. It opens a confirmation that spells out what each
// profile means, what a wrong choice does and that nothing happens before a restart, and
// only that dialog's confirm button, with the PIN typed into it, writes.

/**
 * What a profile means for the machine that runs it, as the confirmation lists it: what it
 * starts and, just as much, what it switches off. The confirmation shows all three side by
 * side, so each list says it in the same terms (Nanonis and its threads, serial
 * auto-detection, what the AI sees, the operator service).
 */
export function profileEffects(p: StationProfile): string[] {
  switch (p) {
    case "optical":
      return [
        tr(
          "不连接 Nanonis，也不启动它的线程：状态轮询、撞针看门狗、隧道电流监控都不起。",
          "Does not connect to Nanonis or start its threads: no state polling, no tip-crash watchdog, no tunnelling-current monitor.",
        ),
        tr("串口自动探测强制关闭。", "Serial auto-detection is forced off."),
        tr(
          "AI 只看到光学与通用技能；简报、观察和首页换成光学站内容。",
          "The AI sees only the optical and generic skills; the briefing, the observation and the home page show the optical station.",
        ),
        tr(
          "安装操作员提示服务和激光撤销监听。",
          "Installs the operator prompt service and the laser revocation listener.",
        ),
      ];
    case "generic":
      // generic_station.md §1.2 / §1.4 / §3.1-3.3
      return [
        tr(
          "不连接 Nanonis，也不启动它的线程：状态轮询、撞针看门狗、隧道电流监控都不起。",
          "Does not connect to Nanonis or start its threads: no state polling, no tip-crash watchdog, no tunnelling-current monitor.",
        ),
        tr(
          "串口自动探测默认关闭；需要时可以在设置里打开（要管理 PIN）。",
          "Serial auto-detection is off by default; it can be switched on in the settings when needed (admin PIN).",
        ),
        tr(
          "AI 看到通用技能和外部设备的技能（光谱、光源、功率计、温控、协议），看不到 STM 技能和 WITec 技能；简报和观察多一段环境读数，首页换成通用站内容。",
          "The AI sees the generic skills and the configured devices' skills (spectra, light sources, power meters, temperature, protocols), but neither the STM nor the WITec skills; the briefing and the observation add the environment readings, and the home page shows the generic station.",
        ),
        tr(
          "安装操作员提示服务和激光撤销监听。",
          "Installs the operator prompt service and the laser revocation listener.",
        ),
      ];
    default:
      return [
        tr(
          "连接 Nanonis，启动状态轮询、撞针看门狗和隧道电流监控。",
          "Connects to Nanonis and starts state polling, the tip-crash watchdog and the tunnelling-current monitor.",
        ),
        tr(
          "串口自动探测按配置运行：会向空闲串口发送 *IDN? 和 Modbus 帧。",
          "Serial auto-detection runs as configured: it sends *IDN? and Modbus frames to idle serial ports.",
        ),
        tr(
          "AI 看到 STM 技能；简报、观察和首页是 STM 内容。",
          "The AI sees the STM skills; the briefing, the observation and the home page show the STM.",
        ),
        tr(
          "不安装操作员提示服务和激光撤销监听。",
          "Does not install the operator prompt service or the laser revocation listener.",
        ),
      ];
  }
}

/** What choosing `to` does if this machine is really another kind of station. */
export function switchRisk(to: StationProfile): string {
  switch (to) {
    case "optical":
      return tr(
        "如果这台机器其实是 STM 站：下次启动后它不会连接 Nanonis，撞针看门狗和隧道电流监控都不会起来。如果其实是通用站：串口自动探测被强制关闭，在设置里也打不开。",
        "If this machine is really an STM station: after the next start it will not connect to Nanonis, and neither the tip-crash watchdog nor the tunnelling-current monitor will run. If it is really a generic station: serial auto-detection is forced off and cannot be switched on in the settings.",
      );
    case "generic":
      return tr(
        "如果这台机器其实是 STM 站：下次启动后它不会连接 Nanonis，撞针看门狗和隧道电流监控都不会起来。如果其实是光学站：AI 看不到 WITec 技能，手动光学状态也只剩激光区域签署这一项。",
        "If this machine is really an STM station: after the next start it will not connect to Nanonis, and neither the tip-crash watchdog nor the tunnelling-current monitor will run. If it is really an optical station: the AI no longer sees the WITec skills, and the manual optical state keeps only the laser-area attestation.",
      );
    default:
      return tr(
        "如果这台机器其实是光学站或通用站：串口自动探测会恢复，向每个空闲串口发送 *IDN? 和 Modbus 帧，下次启动还会去连接 Nanonis；操作员提示服务和激光撤销监听也不再安装。",
        "If this machine is really an optical or a generic station: serial auto-detection comes back and sends *IDN? and Modbus frames to every idle serial port, the next start dials Nanonis, and the operator prompt service and the laser revocation listener are no longer installed.",
      );
  }
}

export type SwitchCopy = {
  title: string;
  intro: string;
  /** Every profile, in PROFILES order; `selected` marks the one being saved. */
  profiles: { profile: StationProfile; label: string; blurb: string; selected: boolean; lines: string[] }[];
  risk: string;
  restart: string;
  /** Only when the environment variable pins the profile: saving cannot change what runs. */
  envNote: string | null;
  confirmLabel: string;
};

/** What the confirmation knows besides the two profiles. */
export type ConfirmContext = {
  /** The profile the running service is (GET /api/health); saving that one needs no restart. */
  active?: StationProfile;
  /** No profile was ever chosen on this machine (`profileNeverChosen`): this save chooses one. */
  firstRun?: boolean;
};

/**
 * Everything the confirmation dialog says, in one place so a test can read all of it.
 *
 * `from === to` happens only on first use: nothing is stored yet, the machine runs the
 * default, and the operator confirms that the default is right. Saving it is still a real
 * write (the choice stops being an accident of the default), so it gets the same dialog,
 * worded as a confirmation rather than a switch.
 */
export function confirmCopy(
  from: StationProfile,
  to: StationProfile,
  source: ProfileSource = "default",
  ctx: ConfirmContext = {},
): SwitchCopy {
  const target = profileLabel(to);
  const same = from === to;
  const noRestart = ctx.active !== undefined && ctx.active === to;
  return {
    title: same
      ? tr(`确认这台机器是「${target}」？`, `Confirm this machine as "${target}"?`)
      : tr(`把这台机器改成「${target}」？`, `Switch this machine to "${target}"?`),
    intro: ctx.firstRun
      ? tr(
          `这台机器还没有保存过站点类型，现在按默认的「${profileLabel(from)}」运行。这个选择决定下次启动时这个进程是什么，所以要先确认，再输入管理 PIN。`,
          `No station profile has been saved on this machine yet; it runs as the default "${profileLabel(from)}". This choice decides what this process is at the next start, so it needs a confirmation and the admin PIN.`,
        )
      : tr(
          `现在保存的是「${profileLabel(from)}」。这个选择决定下次启动时这个进程是什么，所以要先确认，再输入管理 PIN。`,
          `The saved profile is "${profileLabel(from)}". This choice decides what this process is at the next start, so it needs a confirmation and the admin PIN.`,
        ),
    profiles: PROFILES.map((p) => ({
      profile: p,
      label: profileLabel(p),
      blurb: profileBlurb(p),
      selected: p === to,
      lines: profileEffects(p),
    })),
    risk: switchRisk(to),
    restart: noRestart
      ? tr(
          "当前运行的就是这个画像：保存只是把它记下来，不需要重启。",
          "The running service already is this profile: saving only records it, no restart is needed.",
        )
      : tr(
          "保存后不会立刻改变任何东西：重启 MAST 服务之后才生效，在那之前仍按现在的画像运行。",
          "Nothing changes when you save: the new profile takes effect when the MAST service restarts, and until then it keeps running as it is now.",
        ),
    envNote:
      source === "env"
        ? tr(
            "这台机器的站点画像由环境变量 MAST_STATION_PROFILE 指定：重启后仍以环境变量为准，保存的选择不会改变实际运行的画像。",
            "This machine's station profile is pinned by the MAST_STATION_PROFILE environment variable: after a restart the variable still wins, so the saved choice does not change the profile that runs.",
          )
        : null,
    confirmLabel: same
      ? tr(`确认是「${target}」并保存`, `Save as "${target}"`)
      : tr(`确认改为「${target}」并保存`, `Save as "${target}"`),
  };
}

/** Why the confirm button is disabled (null = it may be pressed). */
export type SwitchBlocker = "busy" | "no_pin_set" | "empty_pin";

/**
 * `pinIsSet` is `GET /api/admin/pin-status`; `undefined` (not answered yet, or the request
 * failed) does not block: the backend refuses a missing PIN anyway and says why, and a failed
 * status query must not lock the operator out of the dialog.
 */
export function switchBlocker(o: {
  pin: string;
  pinIsSet: boolean | undefined;
  busy: boolean;
}): SwitchBlocker | null {
  if (o.busy) return "busy";
  if (o.pinIsSet === false) return "no_pin_set";
  if (o.pin.trim() === "") return "empty_pin";
  return null;
}

// ── optical-bench devices (GET /api/optics/devices) ───────────────────────────

/** The fields of an `OpticsDevice` these helpers read; the generated type fits. */
export type OpticalDeviceLike = {
  id?: string | null;
  name?: string | null;
  type?: string | null;
  kind?: string | null;
  role?: string | null;
  enabled?: boolean | null;
  connected?: boolean | null;
};

/** Kinds the backend can report (`instruments/config.py:device_kind`), in display order. */
export const KIND_ORDER: readonly string[] = [
  "motion",
  "rotation",
  "openloop",
  "spectrometer",
  "microscope",
  "laser",
  "power_meter",
  "temperature",
];

/** A missing / blank kind is "motion", the backend's own default. */
export function normalizeKind(kind: unknown): string {
  if (typeof kind !== "string") return "motion";
  return kind.trim().toLowerCase() || "motion";
}

/** Operator-facing name of a kind; a kind this build does not know is shown as reported. */
export function kindLabel(kind: unknown): string {
  const k = normalizeKind(kind);
  switch (k) {
    case "motion":
      return tr("位移台", "Stage");
    case "rotation":
      return tr("旋转台", "Rotation mount");
    case "openloop":
      return tr("开环步进台", "Open-loop stepper");
    case "spectrometer":
      return tr("光谱仪", "Spectrometer");
    case "microscope":
      return tr("显微镜", "Microscope");
    case "laser":
      return tr("激光器", "Laser");
    case "power_meter":
      return tr("功率计", "Power meter");
    case "temperature":
      return tr("温控", "Temperature controller");
    default:
      return k;
  }
}

export type DeviceCounts = {
  /** Every entry in the configuration, enabled or not. */
  total: number;
  /** Switched on in the configuration (`enabled` is not false). */
  enabled: number;
  disabled: number;
  /**
   * Enabled devices MAST has already opened. Devices open on first use, so an
   * enabled device that is not connected is idle, not broken.
   */
  connected: number;
};

export function countDevices(
  devices: readonly OpticalDeviceLike[] | null | undefined,
): DeviceCounts {
  let total = 0;
  let enabled = 0;
  let connected = 0;
  for (const d of devices ?? []) {
    total += 1;
    if (d.enabled === false) continue;
    enabled += 1;
    if (d.connected === true) connected += 1;
  }
  return { total, enabled, disabled: total - enabled, connected };
}

/** "Configured" means switched on; the disabled ones are reported separately. */
export function describeDeviceCounts(c: DeviceCounts): string {
  return tr(
    `已配置 ${c.enabled} 台 / 已连接 ${c.connected} 台`,
    `${c.enabled} configured / ${c.connected} connected`,
  );
}

export type KindGroup<D extends OpticalDeviceLike = OpticalDeviceLike> = {
  kind: string;
  label: string;
  devices: D[];
  counts: DeviceCounts;
};

/**
 * Devices grouped by kind: known kinds first in `KIND_ORDER`, then any other kind
 * alphabetically. Inside a group the configuration order is kept.
 */
export function groupByKind<D extends OpticalDeviceLike>(
  devices: readonly D[] | null | undefined,
): KindGroup<D>[] {
  const byKind = new Map<string, D[]>();
  for (const d of devices ?? []) {
    const k = normalizeKind(d.kind);
    const list = byKind.get(k);
    if (list) list.push(d);
    else byKind.set(k, [d]);
  }
  const known = KIND_ORDER.filter((k) => byKind.has(k));
  const other = [...byKind.keys()].filter((k) => !KIND_ORDER.includes(k)).sort();
  return [...known, ...other].map((kind) => {
    const list = byKind.get(kind) ?? [];
    return { kind, label: kindLabel(kind), devices: list, counts: countDevices(list) };
  });
}

export type DeviceStatus = "disabled" | "connected" | "idle";

export function deviceStatus(d: OpticalDeviceLike): DeviceStatus {
  if (d.enabled === false) return "disabled";
  return d.connected === true ? "connected" : "idle";
}

export function deviceStatusLabel(s: DeviceStatus): string {
  switch (s) {
    case "disabled":
      return tr("未启用", "Disabled");
    case "connected":
      return tr("已连接", "Connected");
    default:
      return tr("待连接", "Not connected");
  }
}
