// remoteAccess.ts — pure helpers for Settings → Remote access (RemoteAccessSection.tsx).
//
// GET /api/remote-access carries three extra fields (lan_ips, viewer, tls).
// They are typed here as OPTIONAL extras so the panel compiles both before and
// after frontend/openapi.json + schema.d.ts are regenerated, and keeps working
// against an older backend that does not send them.
//
// Common causes of "the other computer cannot open it":
//   · the tunnel is fine but the BROWSER fails: a system proxy (e.g. Clash with a
//     bypass list lacking 100.*) sends https://100.x to a proxy node →
//     ERR_CONNECTION_CLOSED, while 10.* LAN addresses are bypassed and work;
//   · the STM PC is logged into a different tailnet than the viewing computer
//     (several Tailscale accounts can coexist on one machine);
//   · off campus the path goes through a relay: a 2.2 MB bundle takes 10–37 s on
//     a slow emulated link (2 → 0.5 Mbit/s, 90 ms RTT) before anything renders;
//     with the service's GZip (0.75 MB on the wire) the same links take 3.6–12 s.
// The troubleshooting list below covers these, in the order to check them.

import { tr } from "./i18n.ts";

export interface RemoteViewer {
  client_ip?: string;
  host?: string;
  /** local | tailscale | lan | other */
  via?: string;
}

export interface RemoteTls {
  spki_sha256: string;
  dns_names?: string[];
  ip_addresses?: string[];
  not_after?: string;
  issued_by_mast?: boolean;
  uncovered?: string[];
}

export interface RemoteAccessExtras {
  lan_ips?: string[];
  viewer?: RemoteViewer | null;
  tls?: RemoteTls | null;
}

/** One line saying how THIS page reached the service, or null when unknown. */
export function viewerLine(v: RemoteViewer | null | undefined): string | null {
  if (!v || !v.via) return null;
  const from = v.client_ip ? tr(`（来自 ${v.client_ip}）`, ` (from ${v.client_ip})`) : "";
  switch (v.via) {
    case "local":
      return tr("本次访问：本机", "This session: local machine");
    case "tailscale":
      return tr(`本次访问：经 Tailscale${from} —— 跨网链路是通的`, `This session: via Tailscale${from} — the cross-network link works`);
    case "lan":
      return tr(`本次访问：经局域网${from}`, `This session: via LAN${from}`);
    default:
      return v.client_ip ? tr(`本次访问：来自 ${v.client_ip}`, `This session: from ${v.client_ip}`) : null;
  }
}

/** LAN URLs to show, one per candidate address (a VPN adapter's address cannot
 *  be told apart from a NIC's, so every candidate is listed). Falls back to the
 *  single legacy `lan_ip`. */
export function lanUrls(scheme: string, port: number, ips: string[] | undefined, legacyIp?: string): string[] {
  const list = ips && ips.length > 0 ? ips : legacyIp ? [legacyIp] : [];
  return [...new Set(list)].map((ip) => `${scheme}://${ip}:${port}`);
}

/** "f5a0ebcc3d4c…" → "f5a0ebcc 3d4c2972 …" for reading aloud / comparing by eye.
 *  Copy buttons copy the raw hex. */
export function groupHex(hex: string, size = 8): string {
  const h = (hex || "").replace(/[^0-9a-fA-F]/g, "").toLowerCase();
  const out: string[] = [];
  for (let i = 0; i < h.length; i += size) out.push(h.slice(i, i + size));
  return out.join(" ");
}

/** The checklist for "the other computer cannot open it", most likely cause first. */
export function troubleshootTips(tailnet: string | undefined): string[] {
  const net = tailnet ? tr(`「${tailnet}」`, ` "${tailnet}"`) : "";
  return [
    tr(
      `两台电脑的 Tailscale 必须登录同一个网络${net}。一台电脑上可以有多个 Tailscale 账号，切换账号后它的 100.x 地址会变。`,
      `Both computers must be logged into the same Tailscale network${net}. One computer can hold several Tailscale accounts, and its 100.x address changes when you switch accounts.`,
    ),
    tr(
      "另一台电脑开着 Clash / V2Ray 等代理时，浏览器会把 100.x 地址也交给代理，结果打不开；把 100.64.0.0/10 加进系统代理的「绕过」列表（或设为直连规则）。校内用 10.x 局域网地址没事，是因为 10.* 默认就绕过代理。",
      "If the other computer runs a proxy such as Clash / V2Ray, the browser hands 100.x addresses to the proxy and the page fails to load. Add 100.64.0.0/10 to the system proxy's bypass list (or set it to a direct rule). On-campus 10.x LAN addresses work because 10.* is bypassed by default.",
    ),
    tr(
      "用带 ★ 的 Tailscale IP 打开，不要用 .ts.net 名字（自签证书 + HSTS，浏览器会直接拒绝、没有「继续」按钮）。",
      "Open the Tailscale IP marked with ★, not the .ts.net name (self-signed certificate + HSTS: the browser refuses outright and offers no \"continue\" button).",
    ),
    tr(
      "校外经中继时，首次打开可能要等几秒到十几秒才出界面（压缩后约 0.75 MB 要经中继下载），之后浏览器有缓存会快很多。",
      "Off campus, traffic goes through a relay, so the first load can take several to a dozen seconds (about 0.75 MB compressed must download via the relay). Later loads are much faster thanks to the browser cache.",
    ),
  ];
}
