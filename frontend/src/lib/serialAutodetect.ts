/**
 * Serial auto-detection on the Settings page (generic_station.md §1.4 / §4.2). Pure functions
 * only (test/serialAutodetect.test.ts).
 *
 * Auto-detection sends `*IDN?` and Modbus frames to every idle serial port to find
 * thermometers and vacuum gauges. Each kind of station starts from a different default
 * (`GET /api/health` `station_serial_autodetect`):
 *
 *   default_on   STM: as configured (the sensors file decides). The Settings page shows
 *                nothing new here: the STM page does not change by a pixel.
 *   forced_off   optical: always off. Shown as a state with its reason, no switch.
 *   default_off  generic: off unless the operator switches it on (setting `serial_autodetect`,
 *                admin PIN). Shown with the switch.
 *
 * A service that does not send the field (an older backend) gets nothing drawn either.
 * What is in effect comes from `GET /api/environment/sensors` `autodetect_effective` /
 * `autodetect_reason`; the stored choice from `GET /api/settings` `serial_autodetect`. The
 * two differ until the next start or rescan, and the page says so.
 */

import { tr } from "./i18n.ts";
import type { SerialAutodetect } from "./stationCapabilities.ts";

/** `GET /api/settings` `serial_autodetect`: true / false, or null (not set, or not sent). */
export function readStoredAutodetect(settings: unknown): boolean | null {
  const v = (typeof settings === "object" && settings !== null ? settings : {}) as { serial_autodetect?: unknown };
  return typeof v.serial_autodetect === "boolean" ? v.serial_autodetect : null;
}

/** `GET /api/environment/sensors` `autodetect_effective` (null when not sent or degraded). */
export function readEffectiveAutodetect(sensors: unknown): boolean | null {
  const v = (typeof sensors === "object" && sensors !== null ? sensors : {}) as {
    degraded?: unknown;
    autodetect_effective?: unknown;
  };
  if (v.degraded === true) return null;
  return typeof v.autodetect_effective === "boolean" ? v.autodetect_effective : null;
}

/** `GET /api/environment/sensors` `autodetect_reason`, trimmed; null when absent or empty. */
export function readAutodetectReason(sensors: unknown): string | null {
  const v = (typeof sensors === "object" && sensors !== null ? sensors : {}) as { autodetect_reason?: unknown };
  return typeof v.autodetect_reason === "string" && v.autodetect_reason.trim() ? v.autodetect_reason.trim() : null;
}

export interface AutodetectView {
  /** Draw the row at all: only where auto-detection is forced off or off by default. */
  shown: boolean;
  /** The on/off switch: only where it is off by default (`default_off`). */
  switchable: boolean;
  /** The switch's position: what the operator chose (not what runs). */
  checked: boolean;
  /** One line: what is in effect and why. */
  status: string;
  /** The choice and what runs differ until the next start or rescan. */
  pendingNote: string | null;
}

const HIDDEN: AutodetectView = Object.freeze({ shown: false, switchable: false, checked: false, status: "", pendingNote: null });

export function autodetectView(o: {
  mode: SerialAutodetect | null;
  effective: boolean | null;
  stored: boolean | null;
}): AutodetectView {
  if (o.mode === "forced_off") {
    return {
      shown: true,
      switchable: false,
      checked: false,
      status: tr("串口自动探测：强制关闭（光学站）", "Serial auto-detection: forced off (optical station)"),
      pendingNote: null,
    };
  }
  if (o.mode !== "default_off") return HIDDEN;
  const wanted = o.stored === true;
  // What runs: the service's word when it gives one, else the default (off) unless switched on.
  const on = o.effective ?? false;
  const status = on
    ? tr("串口自动探测：已打开（在设置里打开的）", "Serial auto-detection: on (switched on in the settings)")
    : tr("串口自动探测：关闭（通用站默认关）", "Serial auto-detection: off (off by default on a generic station)");
  const pendingNote =
    (o.effective === null && wanted) || (o.effective !== null && wanted !== o.effective)
      ? tr(
          "已保存的选择和现在运行的不一样：下次启动或在「重新探测」时生效。",
          "The saved choice differs from what runs now: it takes effect at the next start or rescan.",
        )
      : null;
  return { shown: true, switchable: true, checked: wanted, status, pendingNote };
}

/** What the confirmation says before the choice is saved. */
export function autodetectConfirmCopy(next: boolean): { title: string; body: string; confirmLabel: string } {
  return next
    ? {
        title: tr("打开串口自动探测？", "Switch serial auto-detection on?"),
        body: tr(
          "打开后，MAST 会向每个空闲串口发送 *IDN? 和 Modbus 帧，用来识别温控仪和真空计；被厂商软件占着的串口打不开，请先关掉对应软件。下次启动或在「重新探测」时生效。",
          "Once on, MAST sends *IDN? and Modbus frames to every idle serial port to identify thermometers and vacuum gauges; a port held by vendor software cannot be opened, so close that software first. It takes effect at the next start or rescan.",
        ),
        confirmLabel: tr("确认打开并保存", "Switch on and save"),
      }
    : {
        title: tr("关闭串口自动探测？", "Switch serial auto-detection off?"),
        body: tr(
          "关闭后，MAST 不再自己扫描串口；已经接入的传感器照常读数。下次启动或在「重新探测」时生效。",
          "Once off, MAST no longer scans the serial ports by itself; the sensors already connected keep being read. It takes effect at the next start or rescan.",
        ),
        confirmLabel: tr("确认关闭并保存", "Switch off and save"),
      };
}
