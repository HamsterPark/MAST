import { useId } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import { Badge } from "@/components/ui";
import type { Links, LinkState } from "@/lib/opticalLayout/types";
import { deviceStatus, deviceStatusLabel, kindLabel } from "@/lib/stationProfile";
import { displayValue, slotStatus, type OpticalSlot, type SlotValue } from "@/lib/opticalState";
import { CommitText, Row } from "./fields";

// The two links of a component (section 3): a device of the MAST registry and a slot of
// the manual optical state. Both are only references: an id that is not in the lists is
// kept as typed (the device may be switched off, the slot may belong to another build),
// and the lists are suggestions in a native combo box. The live status next to each box
// comes from the polled lists; the server's snapshot is the fallback.

export interface DeviceOption {
  id: string;
  name: string;
  kind: string;
  enabled: boolean;
  connected: boolean;
}

interface Props {
  links: Links;
  devices: readonly DeviceOption[] | null;
  deviceError: string | null;
  slots: readonly OpticalSlot[] | null;
  slotValues: Readonly<Record<string, SlotValue>> | null;
  slotError: string | null;
  snapshot: LinkState | undefined;
  disabled: boolean;
  onChange: (patch: Partial<Links>) => void;
  nowMs: number;
}

function DeviceStatus({ id, devices, snapshot }: { id: string; devices: readonly DeviceOption[] | null; snapshot: LinkState["device"] | null | undefined }) {
  const live = devices?.find((d) => d.id === id);
  if (live) {
    const s = deviceStatus({ enabled: live.enabled, connected: live.connected });
    return (
      <span className="flex flex-wrap items-center gap-1.5 text-xs text-mast-muted">
        <Badge tone={s === "connected" ? "AUTO" : s === "disabled" ? "default" : "INFO"}>{deviceStatusLabel(s)}</Badge>
        {live.name !== live.id && <span>{live.name}</span>}
        <span className="text-mast-faint">{kindLabel(live.kind)}</span>
      </span>
    );
  }
  if (snapshot && snapshot.id === id && snapshot.known === false) {
    return <span className="text-xs text-mast-faint">{tr("设备注册表里没有这个编号；这只是一个引用，保存时不要求它存在。", "The device registry has no device with this id. This is only a reference; it need not exist to save.")}</span>;
  }
  if (devices && snapshot && snapshot.id === id) {
    return (
      <span className="text-xs text-mast-faint">
        {tr("不在当前设备列表里（服务器上次读到：", "Not in the current device list (the server last saw: ")}
        {snapshot.enabled === false ? tr("未启用", "disabled") : snapshot.connected ? tr("已连接", "connected") : tr("待连接", "not connected")}
        {tr("）", ")")}
      </span>
    );
  }
  if (devices) return <span className="text-xs text-mast-faint">{tr("不在当前设备列表里；这只是一个引用，保存时不要求它存在。", "Not in the current device list. This is only a reference; it need not exist to save.")}</span>;
  return null;
}

export function LinkFields({ links, devices, deviceError, slots, slotValues, slotError, snapshot, disabled, onChange, nowMs }: Props) {
  const devId = useId().replace(/:/g, "");
  const slotId = useId().replace(/:/g, "");
  const slot = links.slot ? slots?.find((s) => s.id === links.slot) : undefined;
  const value = links.slot && slotValues ? slotValues[links.slot] : undefined;
  const status = slot ? slotStatus(slot, value, nowMs) : null;

  return (
    <div className="space-y-2">
      <Row label={tr("MAST 设备", "MAST device")} hint={deviceError ? tr(`设备列表读不到（${deviceError}），可以直接填编号。`, `The device list could not be read (${deviceError}); type the id.`) : undefined}>
        <CommitText
          value={links.device_id ?? ""}
          list={devId}
          mono
          disabled={disabled}
          placeholder={tr("设备编号，如 witec、superk", "device id, e.g. witec, superk")}
          aria-label={tr("MAST 设备", "MAST device")}
          onCommit={(v) => onChange({ device_id: v.trim() === "" ? null : v.trim() })}
        />
        <datalist id={devId}>
          {(devices ?? []).map((d) => (
            <option key={d.id} value={d.id}>
              {d.name}
            </option>
          ))}
        </datalist>
        {links.device_id && (
          <div className="mt-1">
            <DeviceStatus id={links.device_id} devices={devices} snapshot={snapshot?.device} />
          </div>
        )}
      </Row>
      <Row label={tr("状态槽位", "State slot")} hint={slotError ? tr(`槽位列表读不到（${slotError}），可以直接填编号。`, `The slot list could not be read (${slotError}); type the id.`) : undefined}>
        <CommitText
          value={links.slot ?? ""}
          list={slotId}
          mono
          disabled={disabled}
          placeholder={tr("手动光学状态槽位，如 excitation_waveplate", "manual optical-state slot, e.g. excitation_waveplate")}
          aria-label={tr("状态槽位", "State slot")}
          onCommit={(v) => onChange({ slot: v.trim() === "" ? null : v.trim() })}
        />
        <datalist id={slotId}>
          {(slots ?? []).map((s) => (
            <option key={s.id} value={s.id}>
              {s.label}
            </option>
          ))}
        </datalist>
        {links.slot && (
          <div className="mt-1 text-xs text-mast-muted">
            {slot ? (
              <span className="flex flex-wrap items-center gap-1.5">
                <span className="text-mast-text">{slot.label}</span>
                <span>{tr("当前：", "now: ")}</span>
                <span className={clsx("font-medium", status === "expired" ? "text-mast-danger" : "text-mast-text")}>{status === "unset" ? "—" : displayValue(slot, value)}</span>
                {status === "expired" && <Badge tone="DANGEROUS">{tr("已过期", "Expired")}</Badge>}
                {status === "unset" && <Badge tone="default">{tr("未设置", "Not set")}</Badge>}
              </span>
            ) : snapshot?.slot && snapshot.slot.id === links.slot ? (
              <span>
                {tr("服务器上次读到：", "The server last saw: ")}
                <span className="text-mast-text">{snapshot.slot.value || "—"}</span>
              </span>
            ) : slots ? (
              <span className="text-mast-faint">{tr("不在当前槽位列表里；这只是一个引用。", "Not in the current slot list. This is only a reference.")}</span>
            ) : null}
          </div>
        )}
      </Row>
    </div>
  );
}
