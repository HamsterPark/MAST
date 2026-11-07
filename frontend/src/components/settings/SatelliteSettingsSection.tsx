// Settings -> satellite channel: setting key `satellite_hub_enabled` (default
// on), the master switch for /ws/satellite/v1.
//
// Turning it off makes the hub disconnect connected satellites at its next
// monitor pass (about 1-2 s) and answer new connections with 403; no MAST
// restart is needed. Satellites keep retrying and reconnect once it is turned
// back on. If a MAST-initiated motion or acquisition is running at the moment of
// disconnect, the satellite stops it via the dead-man switch after 5 s without a
// heartbeat (contract section 5.3). This is stated next to the switch because
// "closing the channel" can read as "nothing moves".

import type { components } from "@/api/schema";
import { Toggle } from "@/components/controls";
import { useSatellites } from "@/api/satellites";
import type { SettingsPatch } from "@/lib/settingsWrite";
import { tr } from "@/lib/i18n";

export function SatelliteSettingsSection({
  settings,
  saving,
  save,
}: {
  settings: { data?: components["schemas"]["SettingsResponse"] | null };
  saving: boolean;
  save: (patch: SettingsPatch) => void;
}) {
  const sats = useSatellites();
  // Unset (null / unreadable) means on, matching how the hub reads hub_enabled().
  const enabled = settings.data?.satellite_hub_enabled !== false;
  const hub = sats.data?.deployed ? sats.data.hub : null;

  const toggle = (v: boolean) => save({ satellite_hub_enabled: v });

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm">{tr("卫星通道（光谱电脑）", "Satellite channel (spectrometer PC)")}</span>
        <Toggle checked={enabled} onChange={toggle} label={tr("卫星通道", "Satellite channel")} />
      </div>
      <p className="text-xs text-mast-faint">
        {tr(
          "关掉后约 1–2 s 内断开已连着的卫星、拒绝新的连入，不需要重启 MAST；打开后卫星会自己重连。断开那一刻如果有 MAST 发起的运动或采集在进行，卫星 5 s 收不到心跳就会按死人开关停下它们。",
          "Turning it off disconnects connected satellites within about 1–2 s and rejects new connections; no MAST restart is needed, and satellites reconnect by themselves once it is on again. If a MAST-initiated motion or acquisition is running at the moment of disconnect, the satellite stops it via the dead-man switch after 5 s without a heartbeat.",
        )}
      </p>
      <p className="text-xs text-mast-muted">
        {sats.data && !sats.data.deployed
          ? tr("当前后端没有卫星模块。", "This backend has no satellite module.")
          : hub
            ? tr(
                `当前：枢纽${hub.started ? "运行中" : "未启动"}${hub.connected !== null ? ` · 在线 ${hub.connected} 颗` : ""}${hub.configured !== null ? ` / 配置 ${hub.configured} 颗` : ""}`,
                `Hub ${hub.started ? "running" : "not started"}${hub.connected !== null ? ` · ${hub.connected} online` : ""}${hub.configured !== null ? ` / ${hub.configured} configured` : ""}`,
              )
            : tr("读取卫星状态…", "Reading satellite status…")}
      </p>
      {saving && <p className="text-xs text-mast-muted">{tr("保存中…", "Saving…")}</p>}
    </div>
  );
}
