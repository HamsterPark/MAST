import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { components } from "@/api/schema";
import { Card, Badge, Spinner, ErrorNote, EmptyNote } from "@/components/ui";
import { Button, Field, NumberField, SelectField } from "@/components/controls";
import { tr } from "@/lib/i18n";

type DevicesResp = components["schemas"]["OpticsDevicesResponse"];
type Device = components["schemas"]["OpticsDevice"];
type ActionResp = components["schemas"]["OpticsActionResponse"];

// One selectable axis, flattened across devices for the jog/wiggle picker.
type AxisRef = {
  key: string; device_id: string; axis: string; unit: string;
  min: number | null; max: number | null; label: string;
};

const num = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;

/**
 * A position / delay reply can say "MAST has not opened this device" (data.connected
 * === false, position null): reads only use devices that are already open and never
 * open one themselves (opening would take the device away from the vendor software).
 * Show this text instead of "—".
 */
const NOT_OPEN_TEXT = tr("未连接（点移动或连接才会打开）", "Not connected (it opens on Move or Connect)");
const notOpen = (d: { [key: string]: unknown } | undefined): boolean => d?.connected === false;

function fmt(v: unknown, digits = 3): string {
  const n = num(v);
  return n === null ? "—" : n.toLocaleString(undefined, {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

export default function OpticsPanel() {
  const qc = useQueryClient();
  const devicesQ = useQuery({
    queryKey: ["optics", "devices"],
    queryFn: async (): Promise<DevicesResp> => {
      const { data, error } = await api.GET("/api/optics/devices");
      if (error) throw error;
      return data;
    },
    refetchInterval: 15000,
  });

  const stopMut = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/optics/stop", { body: {} });
      if (error) throw error;
      return data;
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ["optics"] }),
  });

  const devices = devicesQ.data?.devices ?? [];
  const delayLine = devicesQ.data?.delay_line ?? null;

  return (
    <div className="flex max-w-4xl flex-col gap-5">
      {/* header row: refresh state + panic stop */}
      <div className="flex items-center justify-between">
        <p className="text-sm text-mast-muted">
          {tr("手动控制 TERS/THz 位移台。所有移动经软限位（驱动层强制），越界会被拒绝而非执行。", "Manual control of the TERS/THz stages. All moves go through soft limits (enforced in the driver layer); out-of-range moves are rejected, not executed.")}
        </p>
        <Button
          variant="danger"
          onClick={() => stopMut.mutate()}
          loading={stopMut.isPending}
        >
          {tr("⏹ 全部急停", "⏹ Stop all")}
        </Button>
      </div>
      {stopMut.data && !stopMut.data.ok && (
        <ErrorNote error={stopMut.data.error} label={tr("急停失败", "Stop failed")} />
      )}

      {devicesQ.isLoading && <Spinner label={tr("读取设备清单…", "Reading device list…")} />}
      {devicesQ.error && <ErrorNote error={devicesQ.error} label={tr("设备清单读取失败", "Failed to read device list")} />}

      {devicesQ.data && devices.length === 0 && (
        <Card>
          <EmptyNote label={tr("尚未配置光学台", "No optical stage configured")} />
          <p className="mt-2 text-sm text-mast-muted">
            {tr("在 ", "In ")}<code className="font-mono text-mast-accent">config/optical_instruments.json</code>{tr(" 里把设备", ", set the device's")}
            <code className="font-mono text-mast-accent"> enabled</code>{tr(" 改为 true 并填好 COM 口 / 序列号，然后刷新。", " to true and fill in the COM port / serial number, then refresh.")}
          </p>
        </Card>
      )}

      {devices.length > 0 && (
        <>
          <DevicesOverview devices={devices} />
          {delayLine && <DelayLineControl delayLine={delayLine} />}
          <AxisControl devices={devices} />
          {delayLine && <DryRunControl delayLine={delayLine} />}
        </>
      )}
    </div>
  );
}

// ── 1. device overview ──────────────────────────────────────────────────
function DevicesOverview({ devices }: { devices: Device[] }) {
  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold text-mast-text">{tr("设备总览", "Device overview")}</h3>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {devices.map((d) => (
          <Card key={d.id}>
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="truncate font-medium text-mast-text">{d.name}</div>
                <div className="mt-0.5 font-mono text-xs text-mast-faint">
                  {d.id} · {d.type}
                </div>
              </div>
              {!d.enabled ? (
                <Badge tone="WARN">{tr("未启用", "Disabled")}</Badge>
              ) : d.connected ? (
                <Badge tone="AUTO">{tr("已连接", "Connected")}</Badge>
              ) : (
                <Badge tone="INFO">{tr("待连接", "Not connected")}</Badge>
              )}
            </div>
            <div className="mt-2.5 flex flex-col gap-1">
              {(d.axes ?? []).map((a) => (
                <div key={a.name} className="flex items-center justify-between text-xs">
                  <span className="font-mono text-mast-muted">
                    {a.name}
                    {a.role ? <span className="text-mast-faint"> · {a.role}</span> : null}
                  </span>
                  <span className="font-mono tabular-nums text-mast-faint">
                    {fmt(a.min_pos, 0)} … {fmt(a.max_pos, 0)} {a.unit}
                  </span>
                </div>
              ))}
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}

// ── 2. delay line ────────────────────────────────────────────────────────
function DelayLineControl({ delayLine }: { delayLine: NonNullable<DevicesResp["delay_line"]> }) {
  const qc = useQueryClient();
  const [target, setTarget] = useState("0");
  const delayQ = useQuery({
    queryKey: ["optics", "delay"],
    queryFn: async (): Promise<ActionResp> => {
      const { data, error } = await api.GET("/api/optics/delay");
      if (error) throw error;
      return data as ActionResp;
    },
  });
  const moveMut = useMutation({
    mutationFn: async (delay_ps: number) => {
      const { data, error } = await api.POST("/api/optics/delay/move", { body: { delay_ps } });
      if (error) throw error;
      return data;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["optics", "delay"] }),
  });

  const cur = delayQ.data?.data;
  // The /delay reply always carries the configured reachable range; older backends
  // do not, so fall back to the one in the device list.
  const range = (Array.isArray(cur?.delay_range_ps) ? (cur.delay_range_ps as number[]) : null) ?? delayLine.delay_range_ps ?? [];
  const [rangeLo, rangeHi] = range;
  const targetNum = Number(target);
  const outOfRange =
    rangeLo !== undefined && rangeHi !== undefined &&
    (targetNum < rangeLo || targetNum > rangeHi);

  return (
    <Card>
      <div className="mb-3 flex items-center gap-2">
        <h3 className="text-sm font-semibold text-mast-text">{tr("泵浦-探测延迟线", "Pump-probe delay line")}</h3>
        <Badge>{delayLine.device_id} · {delayLine.axis}</Badge>
      </div>
      <div className="flex flex-wrap items-end gap-x-8 gap-y-3">
        <div>
          <div className="text-xs text-mast-faint">{tr("当前延迟", "Current delay")}</div>
          {!delayQ.isLoading && notOpen(cur) ? (
            <div className="text-sm text-mast-warn">{NOT_OPEN_TEXT}</div>
          ) : (
            <>
              <div className="font-mono text-2xl tabular-nums text-mast-accent">
                {delayQ.isLoading ? "…" : fmt(cur?.delay_ps, 3)}
                <span className="ml-1 text-sm text-mast-muted">ps</span>
              </div>
              <div className="mt-0.5 font-mono text-xs text-mast-faint">
                {tr("台位", "Stage")} {fmt(cur?.stage_position, 2)} {String(cur?.stage_unit ?? "")}
              </div>
            </>
          )}
        </div>
        <div className="text-xs text-mast-faint">
          {tr("可达范围", "Reachable range")}<br />
          <span className="font-mono tabular-nums text-mast-muted">
            {range.length === 2 ? `${fmt(range[0], 1)} … ${fmt(range[1], 1)} ps` : "—"}
          </span>
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-end gap-3">
        <div className="w-40">
          <Field label={tr("移到延迟 (ps)", "Move to delay (ps)")} hint={outOfRange ? tr("超出可达范围", "Out of reachable range") : undefined}>
            <NumberField value={target} onChange={setTarget} step="1" />
          </Field>
        </div>
        <Button
          variant="primary"
          disabled={outOfRange || !Number.isFinite(targetNum)}
          loading={moveMut.isPending}
          onClick={() => moveMut.mutate(targetNum)}
        >
          {tr("移动", "Move")}
        </Button>
        {moveMut.data && (
          <span className={clsxOk(moveMut.data.ok)}>
            {moveMut.data.ok ? tr(`已到 ${fmt(moveMut.data.data?.delay_ps, 3)} ps`, `Reached ${fmt(moveMut.data.data?.delay_ps, 3)} ps`) : moveMut.data.error}
          </span>
        )}
      </div>
    </Card>
  );
}

// ── 3. axis jog + wiggle ─────────────────────────────────────────────────
function AxisControl({ devices }: { devices: Device[] }) {
  const qc = useQueryClient();
  const axes: AxisRef[] = useMemo(
    () =>
      devices.flatMap((d) =>
        (d.axes ?? []).map((a) => ({
          key: `${d.id}::${a.name}`,
          device_id: d.id, axis: a.name, unit: a.unit,
          min: a.min_pos ?? null, max: a.max_pos ?? null,
          label: `${d.name} · ${a.name}`,
        })),
      ),
    [devices],
  );
  const [sel, setSel] = useState(axes[0]?.key ?? "");
  const cur = axes.find((a) => a.key === sel) ?? axes[0];

  const [jog, setJog] = useState("10");
  const [absTarget, setAbsTarget] = useState("0");
  const [wiggleDelta, setWiggleDelta] = useState("50");

  const posQ = useQuery({
    queryKey: ["optics", "pos", cur?.device_id, cur?.axis],
    enabled: !!cur,
    queryFn: async (): Promise<ActionResp> => {
      const { data, error } = await api.GET("/api/optics/position", {
        params: { query: { device_id: cur!.device_id, axis: cur!.axis } },
      });
      if (error) throw error;
      return data as ActionResp;
    },
  });
  const refreshPos = () =>
    qc.invalidateQueries({ queryKey: ["optics", "pos", cur?.device_id, cur?.axis] });

  const moveMut = useMutation({
    mutationFn: async (v: { position: number; relative: boolean }) => {
      const { data, error } = await api.POST("/api/optics/move", {
        body: { device_id: cur!.device_id, axis: cur!.axis, position: v.position, relative: v.relative },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: refreshPos,
  });
  const wiggleMut = useMutation({
    mutationFn: async (delta: number) => {
      const { data, error } = await api.POST("/api/optics/wiggle", {
        body: { device_id: cur!.device_id, axis: cur!.axis, delta },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: refreshPos,
  });

  if (!cur) return null;
  const pos = num(posQ.data?.data?.position);
  const jogN = Number(jog);
  const absN = Number(absTarget);
  const absOut = cur.min !== null && cur.max !== null && (absN < cur.min || absN > cur.max);
  const w = wiggleMut.data?.data;

  return (
    <Card>
      <h3 className="mb-3 text-sm font-semibold text-mast-text">{tr("单轴控制 · 自检", "Single-axis control · self-test")}</h3>
      <div className="flex flex-wrap items-end gap-3">
        <div className="w-64">
          <Field label={tr("轴", "Axis")}>
            <SelectField
              value={sel}
              onChange={setSel}
              options={axes.map((a) => ({ value: a.key, label: a.label }))}
            />
          </Field>
        </div>
        <div>
          <div className="text-xs text-mast-faint">{tr("当前位置", "Current position")}</div>
          {!posQ.isFetching && notOpen(posQ.data?.data) ? (
            <div className="text-sm text-mast-warn">{NOT_OPEN_TEXT}</div>
          ) : (
            <div className="font-mono text-xl tabular-nums text-mast-accent">
              {posQ.isFetching ? "…" : fmt(pos, 3)}
              <span className="ml-1 text-xs text-mast-muted">{cur.unit}</span>
            </div>
          )}
          {cur.min !== null && cur.max !== null && (
            <div className="mt-0.5 font-mono text-[11px] text-mast-faint">
              {tr("限位", "Limits")} {fmt(cur.min, 0)} … {fmt(cur.max, 0)}
            </div>
          )}
        </div>
        <Button variant="ghost" onClick={refreshPos} loading={posQ.isFetching}>{tr("刷新", "Refresh")}</Button>
      </div>

      {/* jog */}
      <div className="mt-4 flex flex-wrap items-end gap-2 border-t border-mast-border pt-4">
        <div className="w-28">
          <Field label={tr(`步长 (${cur.unit})`, `Step (${cur.unit})`)}>
            <NumberField value={jog} onChange={setJog} step="1" />
          </Field>
        </div>
        <Button
          disabled={!Number.isFinite(jogN)}
          loading={moveMut.isPending}
          onClick={() => moveMut.mutate({ position: -jogN, relative: true })}
        >{tr("− 反向", "− Reverse")}</Button>
        <Button
          disabled={!Number.isFinite(jogN)}
          loading={moveMut.isPending}
          onClick={() => moveMut.mutate({ position: jogN, relative: true })}
        >{tr("+ 正向", "+ Forward")}</Button>
        <div className="mx-2 h-8 w-px bg-mast-border" />
        <div className="w-32">
          <Field label={tr(`移到 (${cur.unit})`, `Move to (${cur.unit})`)} hint={absOut ? tr("超出限位", "Beyond limits") : undefined}>
            <NumberField value={absTarget} onChange={setAbsTarget} step="1" />
          </Field>
        </div>
        <Button
          variant="primary"
          disabled={absOut || !Number.isFinite(absN)}
          loading={moveMut.isPending}
          onClick={() => moveMut.mutate({ position: absN, relative: false })}
        >{tr("移动", "Move")}</Button>
      </div>
      {moveMut.data && !moveMut.data.ok && (
        <p className="mt-2 text-sm text-mast-danger">{moveMut.data.error}</p>
      )}

      {/* wiggle self-test */}
      <div className="mt-4 flex flex-wrap items-end gap-2 border-t border-mast-border pt-4">
        <div className="w-28">
          <Field label={tr(`自检幅度 (${cur.unit})`, `Self-test amplitude (${cur.unit})`)}>
            <NumberField value={wiggleDelta} onChange={setWiggleDelta} step="1" />
          </Field>
        </div>
        <Button
          loading={wiggleMut.isPending}
          onClick={() => wiggleMut.mutate(Number(wiggleDelta))}
          disabled={!Number.isFinite(Number(wiggleDelta))}
        >
          {tr("🔧 Wiggle 自检", "🔧 Wiggle self-test")}
        </Button>
        {w && (
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            <Badge tone={w.moved ? "AUTO" : "DANGEROUS"}>
              {w.moved ? tr("动了", "Moved") : tr("没动", "Did not move")}
            </Badge>
            {w.moved != null && w.direction_ok != null && (
              <Badge tone={w.direction_ok ? "AUTO" : "WARN"}>
                {tr("方向", "Direction ")}{w.direction_ok ? "✓" : tr("反了", "reversed")}
              </Badge>
            )}
            <span className="font-mono tabular-nums text-mast-muted">
              {tr("位移", "Displacement")} {fmt(w.observed_delta, 3)} · {tr("标度比", "scale ratio")} {fmt(w.scale_ratio, 3)}
            </span>
          </div>
        )}
      </div>
      {wiggleMut.data && !wiggleMut.data.ok && (
        <p className="mt-2 text-sm text-mast-danger">{wiggleMut.data.error}</p>
      )}
    </Card>
  );
}

// ── 4. dry-run sweep ─────────────────────────────────────────────────────
function DryRunControl({ delayLine }: { delayLine: NonNullable<DevicesResp["delay_line"]> }) {
  const [start, setStart] = useState("0");
  const [stop, setStop] = useState("100");
  const [points, setPoints] = useState("11");
  const dryMut = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/optics/dry-run", {
        body: { delay_start_ps: Number(start), delay_stop_ps: Number(stop), points: Number(points) },
      });
      if (error) throw error;
      return data;
    },
  });
  const range = delayLine.delay_range_ps ?? [];
  const r = dryMut.data?.data;

  return (
    <Card>
      <div className="mb-1 flex items-center gap-2">
        <h3 className="text-sm font-semibold text-mast-text">{tr("泵浦-探测空跑 (dry run)", "Pump-probe dry run")}</h3>
        <Badge tone="INFO">{tr("不打激光 · 不读数 · 不存盘", "No laser · no readout · nothing saved")}</Badge>
      </div>
      <p className="mb-3 text-xs text-mast-muted">
        {tr("让延迟线走完整条扫描、每点等稳，只验证行程可达与耗时——真实（带激光）测量走 agent / 技能路径。", "Runs the delay line through the whole scan, settling at each point, to verify only that the travel is reachable and how long it takes. Real (laser-on) measurements go through the agent / skill path.")}
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <div className="w-28"><Field label={tr("起 (ps)", "Start (ps)")}><NumberField value={start} onChange={setStart} step="1" /></Field></div>
        <div className="w-28"><Field label={tr("止 (ps)", "Stop (ps)")}><NumberField value={stop} onChange={setStop} step="1" /></Field></div>
        <div className="w-24"><Field label={tr("点数", "Points")}><NumberField value={points} onChange={setPoints} step="1" /></Field></div>
        <Button variant="primary" loading={dryMut.isPending} onClick={() => dryMut.mutate()}>{tr("运行空跑", "Run dry run")}</Button>
        {range.length === 2 && (
          <span className="pb-2 font-mono text-xs text-mast-faint">
            {tr("可达", "Reachable")} {fmt(range[0], 0)}…{fmt(range[1], 0)} ps
          </span>
        )}
      </div>
      {dryMut.isPending && (
        <p className="mt-2 text-sm text-mast-muted">{tr("空跑中——延迟线正在逐点移动…", "Dry run in progress: the delay line is moving point by point…")}</p>
      )}
      {dryMut.data && (
        dryMut.data.ok ? (
          <div className="mt-3 rounded-mast-card border border-mast-auto-border bg-mast-auto-bg p-3 text-sm">
            <span className="font-semibold text-mast-auto">{tr("✓ 空跑完成", "✓ Dry run complete")}</span>
            <span className="ml-2 font-mono tabular-nums text-mast-text">
              {String(r?.n_points ?? "—")}/{String(r?.requested_points ?? "—")} {tr("点", "points")} · {fmt(r?.elapsed_s, 1)} s
            </span>
          </div>
        ) : (
          <p className="mt-2 text-sm text-mast-danger">{dryMut.data.error}</p>
        )
      )}
    </Card>
  );
}

// small helper: ok/err colored inline text
function clsxOk(ok: boolean): string {
  return ok ? "pb-2 text-sm text-mast-auto" : "pb-2 text-sm text-mast-danger";
}
