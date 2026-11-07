import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Badge, Card, DegradedNote, EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { Button, Field, TextField, useToast } from "@/components/controls";
import { DeviceScanButton, InstrumentStateCard } from "@/components/admin/DeviceScanner";
import { settingsWriteProblem, type SettingsPatch } from "@/lib/settingsWrite";
import { tr } from "@/lib/i18n";

/** Hardware: Nanonis connection (editable host + 4 TCP ports + reconnect) and
 *  environment sensors (CRUD + rescan). Host/ports persist via POST /api/settings; reconnect via
 *  POST /api/nanonis/connect (graceful, 202 + poll GET /api/nanonis/connection).
 *  Sensors: POST /api/environment/sensors (add/update),
 *  DELETE /api/environment/sensors/{id}, GET /api/environment/sensors/rescan.
 *
 *  `readOnly`: when true (the Settings → Hardware connection surface) the port/host
 *  editors and sensor CRUD are presented read-only. Ports are edited only in
 *  Advanced → Hardware connection (PIN-gated). In read-only mode the host + 4 ports
 *  are prefilled from GET /api/nanonis/connection (6501-6504) alongside live
 *  connection status, plus a note pointing to the Advanced tab. */

/** Setting names are these four literals, not `string`: the body is `SettingsPatch`
 *  (generated from the backend schema), so a misspelt name fails typecheck instead of
 *  becoming a 422 on the instrument. */
type PortSetting =
  | "nanonis_port_main"
  | "nanonis_port_monitor"
  | "nanonis_port_data"
  | "nanonis_port_emergency";

const PORT_ROLES: { key: "main" | "monitor" | "data" | "emergency"; label: string; setting: PortSetting }[] = [
  { key: "main", label: tr("主端口", "Main port"), setting: "nanonis_port_main" },
  { key: "monitor", label: tr("监控端口", "Monitor port"), setting: "nanonis_port_monitor" },
  { key: "data", label: tr("数据端口", "Data port"), setting: "nanonis_port_data" },
  { key: "emergency", label: tr("急停端口", "Emergency-stop port"), setting: "nanonis_port_emergency" },
];

/** Static read-only value box (visual parity with TextField, not editable). */
function ReadOnlyValue({ value, mono }: { value: string; mono?: boolean }) {
  return (
    <div
      className={`rounded-md border border-mast-border bg-mast-bg/40 px-2 py-1.5 text-sm text-mast-text ${
        mono ? "font-mono" : ""
      }`}
    >
      {value}
    </div>
  );
}

function NanonisCard({ readOnly = false }: { readOnly?: boolean }) {
  const queryClient = useQueryClient();
  const { toast, node } = useToast();

  const settingsQ = useQuery({
    queryKey: ["settings"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/settings");
      if (error) throw error;
      return data;
    },
  });

  const connQ = useQuery({
    queryKey: ["nanonis", "connection"],
    refetchInterval: 3000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/nanonis/connection");
      if (error) throw error;
      return data;
    },
  });

  const [host, setHost] = useState<string | null>(null);
  const [ports, setPorts] = useState<Record<string, string>>({});
  useEffect(() => {
    setHost(null);
    setPorts({});
  }, [settingsQ.data]);

  const sv = settingsQ.data as Record<string, unknown> | undefined;
  // Live connection port (6501-6504) for a given role, used as the prefilled
  // read-only value and as a fallback when settings hasn't persisted a port.
  const connPort = (key: string): string => {
    const p = (connQ.data?.ports ?? []).find((x) => x.role === key)?.port;
    return p != null ? String(p) : "";
  };
  const hostVal =
    host ??
    (sv?.nanonis_host != null
      ? String(sv.nanonis_host)
      : connQ.data?.host != null
        ? String(connQ.data.host)
        : "");
  const portVal = (setting: string, key: string): string => {
    if (setting in ports) return ports[setting] ?? "";
    if (sv?.[setting] != null) return String(sv[setting]);
    return connPort(key); // prefill from the live connection (6501-6504)
  };

  const save = useMutation({
    mutationFn: async () => {
      // SettingsPatch (generated from the backend schema) rather than
      // Record<string, unknown>: the backend uses extra='forbid', so one unknown key
      // makes the whole write a 422. Key names must be checked at typecheck time.
      const body: SettingsPatch = {};
      if (host != null && host.trim()) body.nanonis_host = host.trim();
      for (const r of PORT_ROLES) {
        const raw = (ports[r.setting] ?? "").trim();
        if (raw !== "" && !Number.isNaN(Number(raw))) body[r.setting] = Number(raw);
      }
      const { data, error } = await api.POST("/api/settings", { body });
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      // Checking only `degraded` would show a success toast for every refusal
      // (`ok:false, degraded:false`, e.g. port out of range or PIN), so use the shared
      // write-problem predicate. Edits are cleared only on success; dropping them after
      // a refusal would refill the form with the old values as if nothing had happened.
      const problem = settingsWriteProblem(res);
      if (problem) { toast(problem, "err"); return; }
      setHost(null); setPorts({});
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      toast(tr("已保存 Nanonis 连接配置。", "Nanonis connection settings saved."), "ok");
    },
    onError: (e) => toast(`${tr("保存失败：", "Save failed: ")}${String((e as Error)?.message ?? e)}`, "err"),
  });

  const reconnect = useMutation({
    mutationFn: async () => {
      const body: Record<string, unknown> = {};
      if (hostVal) body.host = hostVal;
      for (const r of PORT_ROLES) {
        const raw = portVal(r.setting, r.key).trim();
        if (raw !== "" && !Number.isNaN(Number(raw))) body[`port_${r.key}`] = Number(raw);
      }
      const { data, error } = await api.POST("/api/nanonis/connect", { body: body as never });
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["nanonis", "connection"] });
      res?.degraded ? toast(tr("重连未触发（内核未接入）。", "Reconnect not triggered (core not attached)."), "err") : toast(tr("已发起重连（后台优雅连接）。", "Reconnect started (graceful, in the background)."), "ok");
    },
    onError: (e) => toast(`${tr("重连失败：", "Reconnect failed: ")}${String((e as Error)?.message ?? e)}`, "err"),
  });

  const portStatus = (key: string): boolean | undefined =>
    (connQ.data?.ports ?? []).find((p) => p.role === key)?.connected;

  return (
    <Card>
      <div className="mb-2 flex items-center gap-2">
        <span className="text-sm font-medium text-mast-text">{tr("Nanonis 连接", "Nanonis connection")}</span>
        {connQ.data && !connQ.data.degraded &&
          (connQ.data.connected ? <Badge tone="AUTO">{tr("在线", "Online")}</Badge> : <Badge tone="WARN">{tr("离线", "Offline")}</Badge>)}
        {connQ.data?.host && <span className="text-xs text-mast-muted">{connQ.data.host}</span>}
      </div>
      {settingsQ.isPending && !readOnly && <Spinner />}
      {settingsQ.error && !readOnly && <ErrorNote error={settingsQ.error} />}
      {connQ.data?.degraded && <DegradedNote what={tr("Nanonis 连接", "Nanonis connection")} />}
      {/* In read-only (Settings) mode render from whatever is available (connection
          ports are prefilled 6501-6504); editing requires the settings query. */}
      {(readOnly ? connQ.data || settingsQ.data : settingsQ.data) && (
        <div className="space-y-3">
          <Field label={tr("主机地址", "Host address")}>
            {readOnly ? (
              <ReadOnlyValue value={hostVal || "—"} mono />
            ) : (
              <TextField value={hostVal} mono onChange={setHost} placeholder="127.0.0.1" />
            )}
          </Field>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {PORT_ROLES.map((r) => {
              const st = portStatus(r.key);
              return (
                <Field key={r.key} label={r.label}>
                  <div className="flex items-center gap-2">
                    {readOnly ? (
                      <ReadOnlyValue value={portVal(r.setting, r.key) || "—"} mono />
                    ) : (
                      <TextField
                        value={portVal(r.setting, r.key)}
                        onChange={(v) => setPorts((p) => ({ ...p, [r.setting]: v }))}
                      />
                    )}
                    {st != null && (st ? <Badge tone="AUTO">{tr("已连", "Connected")}</Badge> : <Badge tone="DANGEROUS">{tr("未连", "Disconnected")}</Badge>)}
                  </div>
                </Field>
              );
            })}
          </div>
          {readOnly ? (
            <p className="rounded-md border border-mast-border bg-mast-bg/40 px-2 py-1.5 text-xs text-mast-muted">
              {tr("端口在「高级管理 → 硬件」中修改。", "Ports are changed under Advanced → Hardware connection.")}
            </p>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
                {save.isPending ? tr("保存中…", "Saving…") : tr("保存配置", "Save settings")}
              </Button>
              <Button variant="default" disabled={reconnect.isPending} onClick={() => reconnect.mutate()}>
                {reconnect.isPending ? tr("重连中…", "Reconnecting…") : tr("重新连接", "Reconnect")}
              </Button>
            </div>
          )}
        </div>
      )}
      {node}
    </Card>
  );
}

type SensorRow = { id: string; name: string; type: string; port: string; unit: string };

function SensorsCard({ readOnly = false }: { readOnly?: boolean }) {
  const queryClient = useQueryClient();
  const { toast, node } = useToast();

  const q = useQuery({
    queryKey: ["environment", "sensors"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/environment/sensors");
      if (error) throw error;
      return data;
    },
  });

  const [editing, setEditing] = useState<SensorRow | null>(null);

  const upsert = useMutation({
    mutationFn: async (row: SensorRow) => {
      const { data, error } = await api.POST("/api/environment/sensors", {
        body: {
          id: row.id,
          name: row.name || null,
          type: row.type || null,
          port: row.port || null,
          unit: row.unit || null,
          extra: {},
        } as never,
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      setEditing(null);
      queryClient.invalidateQueries({ queryKey: ["environment", "sensors"] });
      res?.degraded ? toast(tr("写入未生效（内核未接入）。", "Write not applied (core not attached)."), "err") : toast(tr("已保存传感器。", "Sensor saved."), "ok");
    },
    onError: (e) => toast(`${tr("保存失败：", "Save failed: ")}${String((e as Error)?.message ?? e)}`, "err"),
  });

  const del = useMutation({
    mutationFn: async (id: string) => {
      const { data, error } = await api.DELETE("/api/environment/sensors/{sensor_id}", {
        params: { path: { sensor_id: id } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["environment", "sensors"] });
      res?.degraded ? toast(tr("删除未生效（内核未接入）。", "Delete not applied (core not attached)."), "err") : toast(tr("已删除传感器。", "Sensor deleted."), "ok");
    },
    onError: (e) => toast(`${tr("删除失败：", "Delete failed: ")}${String((e as Error)?.message ?? e)}`, "err"),
  });

  const rescan = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.GET("/api/environment/sensors/rescan");
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["environment", "sensors"] });
      res?.degraded ? toast(tr("重新探测未执行（内核未接入）。", "Rescan not run (core not attached)."), "err") : toast(tr(`重新探测完成：${res.count ?? 0} 个传感器。`, `Rescan complete: ${res.count ?? 0} sensors.`), "ok");
    },
    onError: (e) => toast(`${tr("重新探测失败：", "Rescan failed: ")}${String((e as Error)?.message ?? e)}`, "err"),
  });

  const sensors = q.data?.sensors ?? [];

  return (
    <Card>
      <div className="mb-2 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-mast-text">{tr("环境传感器", "Environment sensors")}</span>
          {q.data && !q.data.degraded && (
            <Badge tone={q.data.autodetect ? "AUTO" : "INFO"}>{q.data.autodetect ? tr("自动探测", "Auto-detect") : tr("手动", "Manual")}</Badge>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* Scan is offered on both surfaces: it is read-only until the user
              confirms in the dialog, and the Settings page is where people look to
              attach a new thermometer. Sensor row CRUD stays admin-only. */}
          <DeviceScanButton />
          {!readOnly && (
            <Button variant="default" disabled={rescan.isPending} onClick={() => rescan.mutate()}>
              {rescan.isPending ? tr("探测中…", "Scanning…") : tr("重新探测", "Rescan")}
            </Button>
          )}
        </div>
      </div>
      {q.isPending && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      {q.data?.degraded && <DegradedNote what={tr("传感器配置", "Sensor configuration")} />}
      {q.data && !q.data.degraded && (
        <div className="space-y-3">
          {sensors.length === 0 ? (
            <EmptyNote label={tr("暂无已配置传感器", "No sensors configured")} />
          ) : (
            <div className="overflow-auto rounded-md border border-mast-border">
              <table className="w-full text-sm">
                <thead className="text-mast-muted">
                  <tr className="border-b border-mast-border">
                    <th className="px-2 py-1 text-left">ID</th>
                    <th className="px-2 py-1 text-left">{tr("名称", "Name")}</th>
                    <th className="px-2 py-1 text-left">{tr("类型", "Type")}</th>
                    <th className="px-2 py-1 text-left">{tr("端口", "Port")}</th>
                    <th className="px-2 py-1 text-left">{tr("单位", "Unit")}</th>
                    {!readOnly && <th className="px-2 py-1" />}
                  </tr>
                </thead>
                <tbody>
                  {sensors.map((s) => (
                    <tr key={s.id} className="border-b border-mast-border/50">
                      <td className="px-2 py-1 font-mono text-xs">{s.id}</td>
                      <td className="px-2 py-1">{s.name ?? "—"}</td>
                      <td className="px-2 py-1">{s.type ?? "—"}</td>
                      <td className="px-2 py-1">{s.port ?? "—"}</td>
                      <td className="px-2 py-1">{s.unit ?? "—"}</td>
                      {!readOnly && (
                        <td className="px-2 py-1 text-right">
                          <div className="flex justify-end gap-1">
                            <Button variant="ghost" onClick={() => setEditing({
                              id: s.id, name: s.name ?? "", type: s.type ?? "", port: s.port ?? "", unit: s.unit ?? "",
                            })}>{tr("编辑", "Edit")}</Button>
                            <Button variant="danger" onClick={() => del.mutate(s.id)}>{tr("删除", "Delete")}</Button>
                          </div>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {readOnly ? null : editing ? (
            <div className="rounded-md border border-mast-accent/40 p-3">
              <div className="mb-2 text-sm font-medium text-mast-text">{tr("编辑 / 新增传感器", "Edit / add sensor")}</div>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                <Field label="ID"><TextField value={editing.id} mono onChange={(v) => setEditing({ ...editing, id: v })} /></Field>
                <Field label={tr("名称", "Name")}><TextField value={editing.name} onChange={(v) => setEditing({ ...editing, name: v })} /></Field>
                <Field label={tr("类型", "Type")}><TextField value={editing.type} onChange={(v) => setEditing({ ...editing, type: v })} /></Field>
                <Field label={tr("端口", "Port")}><TextField value={editing.port} onChange={(v) => setEditing({ ...editing, port: v })} /></Field>
                <Field label={tr("单位", "Unit")}><TextField value={editing.unit} onChange={(v) => setEditing({ ...editing, unit: v })} /></Field>
              </div>
              <div className="mt-2 flex gap-2">
                <Button variant="primary" disabled={upsert.isPending || !editing.id.trim()} onClick={() => upsert.mutate(editing)}>
                  {upsert.isPending ? tr("保存中…", "Saving…") : tr("保存", "Save")}
                </Button>
                <Button variant="ghost" onClick={() => setEditing(null)}>{tr("取消", "Cancel")}</Button>
              </div>
            </div>
          ) : (
            <Button variant="default" onClick={() => setEditing({ id: "", name: "", type: "", port: "", unit: "" })}>
              {tr("+ 新增传感器", "+ Add sensor")}
            </Button>
          )}
        </div>
      )}
      {node}
    </Card>
  );
}

/** `readOnly` (Settings → Hardware connection) renders host + 4 ports prefilled from
 *  the live connection (6501-6504) + status as read-only, with a note pointing edits
 *  to Advanced → Hardware connection; sensors become view-only. The admin tab passes
 *  the default (editable). */
export function HardwareManager({ readOnly = false }: { readOnly?: boolean }) {
  return (
    <div className="grid grid-cols-1 gap-4">
      <NanonisCard readOnly={readOnly} />
      <SensorsCard readOnly={readOnly} />
      {/* Read-only instrument readout (inputs + heater state). Shown on both
          surfaces: it is pure reference, and whether the heater is on is something
          people check from the settings page. */}
      <InstrumentStateCard />
    </div>
  );
}
