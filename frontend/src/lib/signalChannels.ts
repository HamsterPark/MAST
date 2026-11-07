/**
 * Signal channel catalogue — pure logic behind the channel dropdowns.
 *
 * Wherever a channel is chosen, the user picks a name from a dropdown instead of
 * typing a bare index such as 86. The React part lives in
 * components/signals/SignalIndexField.tsx; what is here is the part that can be
 * wrong without anything erroring, and is therefore the part
 * `npm run test:unit` covers.
 */

import { tr } from "./i18n.ts";

export interface SignalChannelOption {
  index: number;
  name: string;
}

/**
 * Profile keys whose value is a 0-127 signal slot.
 *
 * One list, imported by every page that renders these — SettingsPage and
 * SetupPage each used to decide independently, and adding a user-editable key is
 * always a two-sided change (frontend and backend). A key missing from here does not
 * error; it silently keeps the bare number box, which looks exactly like a page
 * that was never updated. `test/signalIndex.test.ts` derives the expected set
 * from `instrument_profile._CONFIG_SPEC` rather than trusting this copy.
 */
export const SIGNAL_INDEX_KEYS: ReadonlySet<string> = new Set([
  "lockin_signal_index",
  "lockin_x_signal_index",
  "lockin_y_signal_index",
  "qplus_amplitude_signal_index",
]);

/** Keys whose −1 means "auto: look up by channel name" rather than a slot number. */
export const SIGNAL_AUTO_VALUE: Readonly<Record<string, number>> = {
  qplus_amplitude_signal_index: -1,
};

/** `86 · LI Demod 1 X (A)` — index first, because the index is what gets stored. */
export function channelLabel(c: SignalChannelOption): string {
  return `${c.index} · ${c.name}`;
}

/**
 * Options for the dropdown, given the live table and the currently stored value.
 *
 * The stored value is always selectable even when the table does not contain it.
 * Otherwise opening the dropdown would show an already-entered setting as
 * blank, and the operator's next move is to re-enter it — with a number they
 * cannot look up at that moment, which is the problem the dropdown exists to avoid.
 */
export function channelOptions(
  channels: readonly SignalChannelOption[],
  value: string,
  autoValue?: number,
): { value: string; label: string }[] {
  const out: { value: string; label: string }[] = [{ value: "", label: tr("— 未选择 —", "— Not selected —") }];
  if (autoValue != null) {
    out.push({ value: String(autoValue), label: tr("自动（按通道名查找）", "Auto (look up by channel name)") });
  }
  const known =
    channels.some((c) => String(c.index) === value) ||
    (autoValue != null && value === String(autoValue));
  if (value !== "" && !known) {
    out.push({ value, label: tr(`${value} ·（不在当前名单里）`, `${value} · (not in the current list)`) });
  }
  for (const c of channels) out.push({ value: String(c.index), label: channelLabel(c) });
  return out;
}

/**
 * Why the dropdown is refusing to be a dropdown, or "" when it is fine.
 *
 * Three different reasons, kept apart on purpose. "List not fully decoded" in
 * particular must never read as "this machine has no such channel": the backend cross-checks the count the
 * instrument declared against the count it decoded, and a short decode is a
 * parse failure, not a fact about the hardware.
 */
export function channelListNote(meta: {
  pending?: boolean;
  error?: boolean;
  degraded?: boolean | null;
  truncated?: boolean | null;
  declared_n?: number | null;
  n_channels?: number | null;
  count: number;
}): string {
  if (meta.pending) return tr("正在读通道名单…", "Reading the channel list…");
  if (meta.truncated) {
    return tr(
      `通道名单没解全（仪器声明 ${meta.declared_n ?? "?"} 路，只读到 ` +
        `${meta.n_channels ?? 0} 路）——先按索引填，` +
        "别把「名单里没有」当成「本机没有这个通道」。",
      `The channel list was not fully decoded (the instrument declared ${meta.declared_n ?? "?"} channels, only ` +
        `${meta.n_channels ?? 0} were read) — enter the index directly, ` +
        'and do not treat "not in the list" as "this machine has no such channel".',
    );
  }
  if (meta.error || meta.degraded || meta.count === 0) {
    return tr(
      "读不到通道名单（未连接仪器或接口降级）——请直接填索引。",
      "The channel list is unavailable (instrument not connected or interface degraded) — enter the index directly.",
    );
  }
  return "";
}
