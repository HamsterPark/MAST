import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { Badge, Card, DegradedNote, ErrorNote, Spinner } from "../ui";
import { tr } from "@/lib/i18n";

// Settings -> hardware connection: READ-ONLY Nanonis host + 4 TCP port display.
//
// The hardware editor (host/port edit, reconnect, sensors) lives under
// Advanced -> Hardware (PIN-gated). Here in Settings we only SHOW the resolved
// values so the operator can read them without unlocking admin. We read
// GET /api/nanonis/connection (which reports the defaults 6501-6504 even when
// no pool is wired) and render a small read-only table (host, 4 ports, live
// per-port status) plus a note pointing at Advanced for edits.
//
// admin/HardwareManager is deliberately not reused: this surface must never
// edit hardware.

// role (as returned by /api/nanonis/connection) -> human label, in the canonical
// order of the four Nanonis TCP roles. Mirrors the admin PORT_ROLES labels so
// the two surfaces read identically.
const PORT_ROLES: { key: string; label: string }[] = [
  { key: "main", label: tr("主控端口 (main)", "Main port (main)") },
  { key: "monitor", label: tr("监控端口 (monitor)", "Monitor port (monitor)") },
  { key: "data", label: tr("数据端口 (data)", "Data port (data)") },
  { key: "emergency", label: tr("急停端口 (emergency)", "Emergency port (emergency)") },
];

function StatusBadge({ connected }: { connected?: boolean | null }) {
  if (connected == null) return null;
  return connected ? <Badge tone="AUTO">{tr("已连", "Connected")}</Badge> : <Badge tone="DANGEROUS">{tr("未连", "Disconnected")}</Badge>;
}

export function NanonisPortsReadOnly() {
  // Slow poll so the live status badges stay fresh without hammering the API.
  // Degrade-safe: the endpoint never probes hardware from the standalone process.
  const connQ = useQuery({
    queryKey: ["nanonis", "connection"],
    refetchInterval: 5000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/nanonis/connection");
      if (error) throw error;
      return data;
    },
  });

  if (connQ.isPending) return <Spinner />;
  if (connQ.isError) return <ErrorNote error={connQ.error} />;

  const conn = connQ.data;
  const host = conn?.host && String(conn.host).trim() ? String(conn.host) : "—";
  const portByRole = new Map(
    (conn?.ports ?? []).map((p) => [p.role, p]),
  );

  return (
    <Card>
      <div className="mb-2 flex items-center gap-2">
        <span className="text-sm font-medium text-mast-text">{tr("Nanonis 连接（只读）", "Nanonis connection (read-only)")}</span>
        {conn && !conn.degraded &&
          (conn.connected ? <Badge tone="AUTO">{tr("在线", "Online")}</Badge> : <Badge tone="WARN">{tr("离线", "Offline")}</Badge>)}
      </div>

      {conn?.degraded && <DegradedNote what={tr("Nanonis 连接", "Nanonis connection")} />}

      <div className="space-y-3">
        {/* Host address, read-only */}
        <div className="flex flex-col gap-1 text-sm">
          <span className="text-mast-muted">{tr("主机地址 Host", "Host address")}</span>
          <span className="rounded-md border border-mast-border bg-mast-bg/40 px-2 py-1.5 font-mono text-mast-text">
            {host}
          </span>
        </div>

        {/* The 4 ports: read-only values + live status */}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {PORT_ROLES.map((r) => {
            const ps = portByRole.get(r.key);
            const portVal = ps?.port != null ? String(ps.port) : "—";
            return (
              <div key={r.key} className="flex flex-col gap-1 text-sm">
                <span className="text-mast-muted">{r.label}</span>
                <div className="flex items-center gap-2">
                  <span className="rounded-md border border-mast-border bg-mast-bg/40 px-2 py-1.5 font-mono text-mast-text">
                    {portVal}
                  </span>
                  <StatusBadge connected={ps?.connected} />
                </div>
              </div>
            );
          })}
        </div>

        <p className="rounded-md border border-mast-border bg-mast-bg/40 px-2 py-1.5 text-xs text-mast-muted">
          {tr(
            "这些为只读默认值（端口 6501–6504）。如需修改主机 / 端口或重新连接，请前往「高级管理 → 硬件」（需 PIN 解锁）。",
            "These are read-only defaults (ports 6501–6504). To change the host or ports, or to reconnect, go to Advanced → Hardware (PIN required).",
          )}
        </p>
      </div>
    </Card>
  );
}
