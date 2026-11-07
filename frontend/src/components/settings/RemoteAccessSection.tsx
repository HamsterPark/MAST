import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import {
  groupHex,
  lanUrls,
  troubleshootTips,
  viewerLine,
} from "../../lib/remoteAccess";
import { Badge, Card, ErrorNote, Spinner } from "../ui";
import { getLang, tr } from "@/lib/i18n";

// Settings -> remote access: cross-network remote control over Tailscale
// (READ-ONLY status).
//
// The bind itself (0.0.0.0 + basic-auth + self-signed TLS) is enabled in the
// desktop launcher (tick "Enable LAN / remote access" and restart the service)
// because it must happen at server start. Here we SHOW the live readiness the backend reports via
// GET /api/remote-access: whether the service is bound for remote access, the
// Tailscale status, the exact address(es) to open on the OTHER computer, how
// THIS page arrived (so the operator can confirm from the far end), and the
// certificate's SPKI pin (what a satellite stores as hub.spki_sha256).
//
// Three-step flow (also spelled out in the panel):
//   1. Install Tailscale on both computers and join the same tailnet
//   2. Tick "Enable LAN / remote access" in this machine's launcher and restart
//   3. Open the Tailscale address below in the other computer's browser
//      (accept the certificate warning the first time)
// and when ③ fails, the checklist in lib/remoteAccess.ts (tailnet, proxy, name).

type ToastFn = (msg: string, kind?: "ok" | "err") => void;

async function copyText(text: string, toast?: ToastFn) {
  try {
    await navigator.clipboard.writeText(text);
    toast?.(tr("已复制到剪贴板", "Copied to clipboard"), "ok");
  } catch {
    toast?.(tr("复制失败，请手动选择复制", "Copy failed; select and copy manually"), "err");
  }
}

export function RemoteAccessSection({ toast }: { toast?: ToastFn }) {
  const q = useQuery({
    queryKey: ["remote-access"],
    // Slow poll so the badges flip to "ready" shortly after Tailscale comes up /
    // logs in, without hammering the CLI. Degrade-safe: never raises server-side.
    refetchInterval: 10000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/remote-access");
      if (error) throw error;
      return data;
    },
  });

  if (q.isPending) return <Spinner />;
  if (q.isError) return <ErrorNote error={q.error} />;

  const d = q.data;
  const ts = d.tailscale;
  const urls = d.urls ?? [];
  const lan = lanUrls(d.scheme, d.port, d.lan_ips, d.lan_ip);
  const viewer = viewerLine(d.viewer);
  const tls = d.tls ?? null;

  const tsBadge = !ts.installed ? (
    <Badge tone="DANGEROUS">{tr("未安装", "Not installed")}</Badge>
  ) : ts.ready ? (
    <Badge tone="AUTO">{tr("就绪", "Ready")}</Badge>
  ) : (
    <Badge tone="WARN">{ts.backend_state || tr("未就绪", "Not ready")}</Badge>
  );

  return (
    <Card>
      <div className="mb-3 flex flex-wrap items-center gap-2" data-testid="remote-access-section">
        <span className="text-sm font-medium text-mast-text">{tr("跨网远程访问（Tailscale）", "Remote access across networks (Tailscale)")}</span>
        {d.lan_enabled ? (
          <Badge tone="AUTO">{tr("已开放远程", "Remote enabled")}</Badge>
        ) : (
          <Badge tone="WARN">{tr("仅本机", "Local only")}</Badge>
        )}
        <span className="text-mast-muted">·</span>
        <span className="text-xs text-mast-muted">Tailscale</span>
        {tsBadge}
        {ts.device_name && (
          <span className="text-xs text-mast-muted">
            {tr("本机", "This machine")} <span className="font-mono text-mast-text">{ts.device_name}</span>
            {ts.tailnet ? (
              <>
                {tr(" · 网络 ", " · network ")}
                <span className="font-mono text-mast-text">{ts.tailnet}</span>
              </>
            ) : null}
          </span>
        )}
        <button
          onClick={() => q.refetch()}
          className="ml-auto rounded-md border border-mast-border px-2 py-1 text-xs text-mast-muted hover:text-mast-text"
        >
          {tr("刷新", "Refresh")}
        </button>
      </div>

      {/* One-line status / guidance, always shown, from the backend's `note`. */}
      {d.note && (
        <p className="mb-3 rounded-md border border-mast-border bg-mast-bg/40 px-2 py-1.5 text-sm text-mast-text">
          {d.note}
        </p>
      )}

      {/* How THIS page arrived — the far-end confirmation that the path works. */}
      {viewer && (
        <p className="mb-3 text-xs text-mast-muted" data-testid="remote-access-viewer">
          {viewer}
        </p>
      )}

      {/* Remote address: what to open on the OTHER computer. */}
      {urls.length > 0 ? (
        <div className="space-y-2">
          <span className="text-xs text-mast-muted">{tr("在另一台电脑（已登录同一个 Tailscale 网络）打开：", "Open on the other computer (signed in to the same Tailscale network):")}</span>
          {urls.map((u, i) => (
            <div key={u.url} className="flex items-center gap-2 text-sm">
              <span className="w-32 shrink-0 text-xs text-mast-muted">
                {u.label}
                {i === 0 ? " ★" : ""}
              </span>
              <code className="flex-1 truncate rounded-md border border-mast-border bg-mast-bg/40 px-2 py-1.5 font-mono text-mast-text">
                {u.url}
              </code>
              <button
                onClick={() => copyText(u.url, toast)}
                className="shrink-0 rounded-md border border-mast-border px-2 py-1.5 text-xs text-mast-muted hover:text-mast-text"
              >
                {tr("复制", "Copy")}
              </button>
              <button
                onClick={() => window.open(u.url, "_blank", "noopener")}
                className="shrink-0 rounded-md border border-mast-border px-2 py-1.5 text-xs text-mast-muted hover:text-mast-text"
              >
                {tr("打开", "Open")}
              </button>
            </div>
          ))}
        </div>
      ) : null}

      {/* LAN — secondary; same-office fallback that doesn't need Tailscale. Every
          candidate is listed: a VPN adapter's address looks like a LAN one. */}
      {lan.length > 0 && (
        <p className="mt-3 text-xs text-mast-muted">
          {tr(
            `局域网地址（同一网络内，无需 Tailscale${lan.length > 1 ? "；有多个时用与对方同网段的那个" : ""}）：`,
            `LAN address (same network, no Tailscale needed${lan.length > 1 ? "; if there are several, use the one on the same subnet as the other computer" : ""}): `,
          )}
          {lan.map((u) => (
            <span key={u} className="ml-2 font-mono text-mast-text">
              {u}
            </span>
          ))}
        </p>
      )}

      {/* Certificate pin — what a satellite stores; stays the same across re-signs. */}
      {tls && (
        <div className="mt-3 text-xs text-mast-muted" data-testid="remote-access-tls">
          <div className="flex flex-wrap items-center gap-2">
            <span>{tr("证书指纹（SPKI SHA-256，卫星配置 hub.spki_sha256 填这个）：", "Certificate fingerprint (SPKI SHA-256; use this for hub.spki_sha256 in the satellite config):")}</span>
            <code className="font-mono text-mast-text">{groupHex(tls.spki_sha256)}</code>
            <button
              onClick={() => copyText(tls.spki_sha256, toast)}
              className="rounded-md border border-mast-border px-2 py-0.5 text-xs text-mast-muted hover:text-mast-text"
            >
              {tr("复制", "Copy")}
            </button>
          </div>
          {(tls.uncovered ?? []).length > 0 && (
            <div className="mt-1">
              {tr("证书还没列出 ", "The certificate does not list ")}
              <span className="font-mono">{(tls.uncovered ?? []).join(tr("、", ", "))}</span>
              {tr(
                "（浏览器照常可用；下次重启服务时自动补上，私钥和指纹不变）",
                " (browsers still work; it is added at the next service restart, and the private key and fingerprint do not change)",
              )}
            </div>
          )}
          {tls.issued_by_mast === false && (
            <div className="mt-1">{tr("这是另外提供的证书，MAST 不会自动重签它。", "This certificate was supplied separately; MAST will not re-sign it automatically.")}</div>
          )}
        </div>
      )}

      {/* Three-step recipe, always visible. */}
      <div className="mt-3 rounded-md border border-mast-border bg-mast-bg/40 px-3 py-2 text-xs text-mast-muted">
        <div className="mb-1 font-medium text-mast-text">{tr("怎么用（三步）", "How to use it (three steps)")}</div>
        <ol className="list-decimal space-y-0.5 pl-4">
          {getLang() === "zh" ? (
            <>
              <li>
                两台电脑都安装 Tailscale 并登录<strong>同一个 Tailscale 网络</strong>
                {ts.tailnet ? <>（本机：<span className="font-mono">{ts.tailnet}</span>）</> : null}
                （tailscale.com/download）。
              </li>
              <li>
                <strong>本机</strong>在桌面「MAST 启动器」勾选「启用局域网 / 远程访问」，设置账号密码，然后<strong>重启服务</strong>。
              </li>
              <li>
                <strong>另一台电脑</strong>浏览器打开上方带 <strong>★（推荐）</strong> 的 <strong>Tailscale IP</strong> 地址，输入账号密码即可控制；首次提示证书不受信任，点<strong>「高级 → 继续」</strong>。
                <br />
                <span className="text-mast-muted">
                  ⚠️ 别用 <code>.ts.net</code> 名字打开——自签证书下浏览器会因 HSTS 直接拒绝（除非用 <code>tailscale cert</code> 配了真证书）。
                </span>
              </li>
            </>
          ) : (
            <>
              <li>
                Install Tailscale on both computers and sign in to the <strong>same Tailscale network</strong>
                {ts.tailnet ? <> (this machine: <span className="font-mono">{ts.tailnet}</span>)</> : null}
                {" "}(tailscale.com/download).
              </li>
              <li>
                On <strong>this machine</strong>, tick "Enable LAN / remote access" in the desktop MAST launcher, set a username and password, then <strong>restart the service</strong>.
              </li>
              <li>
                On the <strong>other computer</strong>, open the <strong>Tailscale IP</strong> address marked <strong>★ (recommended)</strong> above in a browser and sign in with the username and password. The first time, the browser warns that the certificate is not trusted; click <strong>"Advanced → Proceed"</strong>.
                <br />
                <span className="text-mast-muted">
                  ⚠️ Do not open it by its <code>.ts.net</code> name: with a self-signed certificate the browser refuses outright because of HSTS (unless a real certificate was set up with <code>tailscale cert</code>).
                </span>
              </li>
            </>
          )}
        </ol>
        <p className="mt-2" data-testid="remote-access-remote-ai-hint">
          {tr(
            "只想让另一台电脑上的 Claude Code 或 Codex 操作这台仪器？不必开局域网访问：在启动器点「设置 AI 远程访问」按钮，按提示配好 SSH，把它给出的那行 ssh 命令和私钥（%USERPROFILE%\\.ssh\\ai-remote）交给那边的 AI 即可。",
            'Only want Claude Code or Codex on another computer to work with this instrument? LAN access is not needed: click "Set up AI remote access" in the launcher, follow the wizard to set up SSH, and give that AI the ssh command and private key (%USERPROFILE%\\.ssh\\ai-remote) it produces.',
          )}
        </p>
        <details className="mt-2">
          <summary className="cursor-pointer text-mast-text">{tr("另一台电脑打不开？按顺序查", "Cannot open it from the other computer? Check in order")}</summary>
          <ol className="mt-1 list-decimal space-y-0.5 pl-4" data-testid="remote-access-troubleshoot">
            {troubleshootTips(ts.tailnet).map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ol>
        </details>
      </div>
    </Card>
  );
}
