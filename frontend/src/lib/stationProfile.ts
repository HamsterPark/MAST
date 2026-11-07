/**
 * Station profile ("stm" | "optical") and the optical-bench device roll-ups the
 * optical-station pages show. Pure functions only: no React, no fetch, so
 * `node --test` can run them (test/stationProfile.test.ts).
 *
 * ── What a profile is ──
 * The same MAST build runs on two kinds of machine. An STM station has a Nanonis
 * controller; an optical station (the WITec Raman computer) has no STM at all and
 * drives the optical bench (WITec, laser, power meter, temperature controller)
 * instead. The backend decides which one it is (`mast/station.py`: environment
 * variable > the stored setting `station_profile` > "stm") and reports it in
 * `GET /api/health` (`station_profile`, `station_restart_pending`). The stored
 * value is `GET /api/settings` `station_profile`.
 *
 * Whatever is unreadable falls back to "stm": every machine that existed before
 * profiles keeps behaving exactly as it did, and a typo never turns a working STM
 * station into an optical one. This mirrors `station.normalize()` on the backend.
 *
 * "stm" / "optical" are identity strings (stored, compared, sent over the API).
 * They are never wrapped in `tr()`; only the labels below are.
 */

import { tr } from "./i18n.ts";

export type StationProfile = "stm" | "optical";

export const DEFAULT_PROFILE: StationProfile = "stm";

/**
 * Any value -> a profile. Only "stm" / "optical" are recognised (case and
 * surrounding whitespace ignored); everything else, including non-strings and
 * the empty string, is the default.
 */
export function normalizeProfile(v: unknown): StationProfile {
  if (typeof v !== "string") return DEFAULT_PROFILE;
  return v.trim().toLowerCase() === "optical" ? "optical" : "stm";
}

/** Where the configured profile comes from (`GET /api/health` `station_profile_source`):
 * "env" = the MAST_STATION_PROFILE environment variable pins it, so the choice saved
 * on the Settings page cannot change what runs. Unknown / missing is "default". */
export type ProfileSource = "env" | "settings" | "default";

export function normalizeSource(v: unknown): ProfileSource {
  return v === "env" || v === "settings" ? v : "default";
}

export function profileLabel(p: StationProfile): string {
  return p === "optical" ? tr("光学站", "Optical station") : tr("STM 站", "STM station");
}

/** One sentence on what the profile is, for the Settings card. */
export function profileBlurb(p: StationProfile): string {
  return p === "optical"
    ? tr(
        "光学站：没有 STM / Nanonis；AI 通过 MCP 编排 WITec 拉曼与激光、功率计、温控。",
        "Optical station: no STM / Nanonis; the AI orchestrates the WITec Raman system, the laser, the power meter and the temperature controller through MCP.",
      )
    : tr(
        "STM 站：连接 Nanonis 控制器，提供扫描、谱学、退针等 STM 技能。",
        "STM station: connects to a Nanonis controller and offers the STM skills (scanning, spectroscopy, withdrawing the tip, ...).",
      );
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

/** What a profile means for the machine that runs it, as the confirmation lists it. */
export function profileEffects(p: StationProfile): string[] {
  return p === "optical"
    ? [
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
      ]
    : [
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

/** What choosing `to` does if this machine is really the other kind of station. */
export function switchRisk(to: StationProfile): string {
  return to === "optical"
    ? tr(
        "如果这台机器其实是 STM 站：下次启动后它不会连接 Nanonis，撞针看门狗和电流监控都不会起来。",
        "If this machine is really an STM station: after the next start it will not connect to Nanonis, and neither the tip-crash watchdog nor the current monitor will run.",
      )
    : tr(
        "如果这台机器其实是光学站：串口自动探测会恢复，向每个空闲串口发送 *IDN? 和 Modbus 帧；操作员提示服务和激光撤销监听也不再安装。",
        "If this machine is really an optical station: serial auto-detection comes back and sends *IDN? and Modbus frames to every idle serial port, and the operator prompt service and the laser revocation listener are no longer installed.",
      );
}

export type SwitchCopy = {
  title: string;
  intro: string;
  /** Both profiles, in a fixed order (stm, optical); `selected` marks the one being switched to. */
  profiles: { profile: StationProfile; label: string; selected: boolean; lines: string[] }[];
  risk: string;
  restart: string;
  /** Only when the environment variable pins the profile: saving cannot change what runs. */
  envNote: string | null;
  confirmLabel: string;
};

/** Everything the confirmation dialog says, in one place so a test can read all of it. */
export function confirmCopy(
  from: StationProfile,
  to: StationProfile,
  source: ProfileSource = "default",
): SwitchCopy {
  const target = profileLabel(to);
  return {
    title: tr(`把这台机器改成「${target}」？`, `Make this machine an "${target}"?`),
    intro: tr(
      `现在保存的是「${profileLabel(from)}」。这个选择决定下次启动时这个进程是什么，所以要先确认，再输入管理 PIN。`,
      `The saved profile is "${profileLabel(from)}". This choice decides what this process is at the next start, so it needs a confirmation and the admin PIN.`,
    ),
    profiles: (["stm", "optical"] as const).map((p) => ({
      profile: p,
      label: profileLabel(p),
      selected: p === to,
      lines: profileEffects(p),
    })),
    risk: switchRisk(to),
    restart: tr(
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
    confirmLabel: tr(`确认改为「${target}」并保存`, `Save as "${target}"`),
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
